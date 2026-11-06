"""Extract valve-datasheet process conditions with DeepSeek-OCR-2 (local, 4-bit) into Excel.

Pipeline: PDF/image -> page PNG -> DeepSeek-OCR-2 markdown -> table parser -> rules -> xlsx.

    pip install -r requirements.txt
    python main.py a.pdf b.png -o process_conditions.xlsx
    (no files given = every pdf/image in this folder)
"""
import argparse
import html
import math
import os
import re
import sys
import tempfile
import time
from html.parser import HTMLParser
from pathlib import Path

from PIL import Image
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

try:
    import pymupdf as fitz
except ImportError:  # older PyMuPDF
    import fitz

Image.MAX_IMAGE_PIXELS = None

MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-ai/DeepSeek-OCR-2")
PROMPT = "<image>\n<|grounding|>Convert the document to markdown. "

CASES = ["minimum", "normal", "maximum", "design", "single", "other"]
CASE_TITLES = {"minimum": "Minimum", "normal": "Normal", "maximum": "Maximum",
               "design": "Design", "single": "Single", "other": "Other"}
KINDS = ["condition", "design", "calculated"]
CANON = [
    "fluid_name", "fluid_state", "flow_rate_mass", "flow_rate_volume", "flow_rate_gas_std",
    "inlet_pressure", "outlet_pressure", "pressure_drop", "shutoff_pressure", "design_pressure",
    "vapor_pressure", "critical_pressure", "inlet_temperature", "outlet_temperature",
    "design_temperature", "critical_temperature", "specific_gravity", "density",
    "molecular_weight", "viscosity", "compressibility_factor", "specific_heat_ratio",
    "specific_volume", "cavitation_factor", "percent_vapour", "percent_solids",
    "corrosion_allowance", "other",
]
PRESSURE = {"inlet_pressure", "outlet_pressure", "pressure_drop", "shutoff_pressure",
            "design_pressure", "vapor_pressure", "critical_pressure"}
TEMP = {"inlet_temperature", "outlet_temperature", "design_temperature", "critical_temperature"}
FLOW = {"flow_rate_mass", "flow_rate_volume", "flow_rate_gas_std"}
NON_NUMERIC = {"fluid_name", "fluid_state", "other", "corrosion_allowance"}
P_UNIT = re.compile(r"(bar|psi|kpa|mpa|pa|kg/?cm|kgf|atm|torr|mmhg|mmwc|inh|ksi)")
P_BASES = {"bar", "mbar", "kpa", "mpa", "pa", "psi", "kg/cm2", "kgf/cm2", "atm"}
T_UNITS = {"c", "f", "k", "r"}

# --------------------------------------------------------------------- helpers
def norm(s):
    return re.sub(r"\s+", " ", str(s or "")).strip().lower()


def unit_norm(u):
    s = str(u or "").lower()
    for a, b in (("²", "2"), ("³", "3"), ("°", ""), ("º", ""), ("deg", "")):
        s = s.replace(a, b)
    return re.sub(r"[\s_()\[\].]", "", s)


_NUM = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")
_THOU = re.compile(r"^[+-]?\d{1,3}(,\d{3})+(\.\d+)?$")


def to_num(s):
    if s is None:
        return None
    t = str(s).strip()
    if _THOU.match(t):
        t = t.replace(",", "")
    return float(t) if _NUM.match(t) else None


def same_value(a, b):
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    na, nb = to_num(a), to_num(b)
    if na is not None and nb is not None:
        return math.isclose(na, nb, rel_tol=1e-9, abs_tol=1e-12)
    return norm(a) == norm(b)


def cell_value(raw):
    n = to_num(raw)
    return n if n is not None else raw



# ----------------------------------------------------------------------- rules
KIND_CALC = re.compile(r"\b(cv|kv|cg)\b|travel|% ?open|noise|\bspl\b|velocity|reynolds|required|mach|dba")
DESIGN_CANON = {"design_pressure", "design_temperature", "shutoff_pressure", "corrosion_allowance"}
CASE_RX = [("minimum", r"(min|minimum)"), ("normal", r"(norm|normal|nor|operating|oper)"),
           ("maximum", r"(max|maximum)"), ("design", r"design"), ("other", r"(others?|notes?)")]
UNIT_RX = re.compile(
    r"(kg/?h|lb/?h|t/?h|kg/?s|m3/?h|nm3/?h|sm3/?h|gpm|scfh|scfm|mmscfd|bar[ga]?|mbar|psi[ga]?|kpa[ga]?|"
    r"mpa[ga]?|pa|kg/?cm2[ga]?|kgf/?cm2[ga]?|atm|c|f|k|r|cp|cst|kg/m3|lb/ft3|g/mol|mm|in|ppm|%|cm2|m3/?s|"
    r"l/?s|l/?min|kw|m/?s)")
MASS_U, GAS_U = re.compile(r"(kg|lb|t)/?[hs]"), re.compile(r"(scf|nm3|sm3|mmscf|scm)")
PROCESS_RX = re.compile(r"process|operating|service|upstream|downstream|conditions")
IDENT = {  # field -> label regex (matched against the whole label cell)
    "tag": r"(tag|item|kks|instrument)( ?(no|number|#)\.?)?",
    "service": r"service", "fluid": r"fluid( name)?", "line_no": r"line( ?(no|number|#)\.?)?",
    "project": r"project( ?name)?",
}
# first match wins, so specific rules go before generic ones
CANON_RULES = [
    ("design_pressure", r"design pres"), ("design_temperature", r"design temp"),
    ("shutoff_pressure", r"shut.?off|allowable.*(d\.?p|differential)"),
    ("vapor_pressure", r"vapou?r pres|\bpv\b"), ("critical_pressure", r"critical pres|\bpc\b"),
    ("critical_temperature", r"critical temp"),
    ("percent_vapour", r"vapou?r.*%|% ?vapou?r|vapou?r fraction|quality"),
    ("percent_solids", r"solid"), ("corrosion_allowance", r"corrosion"),
    ("inlet_pressure", r"(inlet|upstream|\bp1\b).*pres|pres.*(inlet|upstream|\bp1\b)|^p1\b"),
    ("outlet_pressure", r"(outlet|downstream|\bp2\b).*pres|pres.*(outlet|downstream|\bp2\b)|^p2\b"),
    ("pressure_drop", r"press.*(drop|diff)|\bd\.?p\b|delta ?p|differential"),
    ("inlet_temperature", r"(inlet|upstream|\bt1\b).*temp|temp.*(inlet|upstream|\bt1\b)"),
    ("outlet_temperature", r"(outlet|downstream|\bt2\b).*temp|temp.*(outlet|downstream|\bt2\b)"),
    ("flow", r"flow|capacity"),
    ("specific_heat_ratio", r"cp/cv|gamma|isentropic|heat ratio"),
    ("specific_gravity", r"specific gravity|\bsg\b|spec\.? grav|relative density"),
    ("molecular_weight", r"mol(ecular)? ?w|\bmw\b"), ("viscosity", r"viscosity"),
    ("compressibility_factor", r"compress|\bz\b"), ("specific_volume", r"specific volume"),
    ("cavitation_factor", r"cavitation|\bxf\b|\bkc\b|sigma"), ("density", r"dens"),
    ("fluid_state", r"fluid state|phase|\bstate\b"), ("fluid_name", r"fluid|medium"),
]


def is_unit(s):
    u = unit_norm(s)
    return bool(u) and len(str(s)) <= 14 and UNIT_RX.fullmatch(u) is not None


def unit_from_label(label):
    m = re.search(r"[\(\[]\s*([^()\[\]]{1,12}?)\s*[\)\]]\s*$", label)
    if m and is_unit(m.group(1)):
        return m.group(1)
    last = label.split()[-1] if label.split() else ""
    return last if len(last) > 1 and is_unit(last) else None


def canonical(label, unit):
    t = norm(label)
    for canon, rx in CANON_RULES:
        if re.search(rx, t):
            if canon != "flow":
                return canon
            u = unit_norm(unit or unit_from_label(label))
            if MASS_U.fullmatch(u) or "mass" in t:
                return "flow_rate_mass"
            if GAS_U.match(u) or re.search(r"std|gas", t):
                return "flow_rate_gas_std"
            return "flow_rate_volume" if u or "volum" in t else "other"
    return "other"


def kind_of(label, canon):
    t = norm(label)
    if KIND_CALC.search(t):
        return "calculated"
    return "design" if canon in DESIGN_CANON or "design" in t or "allowable" in t else "condition"


def case_of(cell):
    t = norm(cell).strip(" .:()")
    return next((c for c, rx in CASE_RX if re.match(rx + r"\b", t)), None)


# ------------------------------------------------------------- markdown -> tables
def clean(s):
    s = re.sub(r"<br\s*/?>", " ", s or "", flags=re.I)
    s = re.sub(r"<[^>]+>|\*\*|\\\*", "", s)
    s = re.sub(r"\s+", " ", html.unescape(s)).strip()
    return "" if re.fullmatch(r"x{3,}", s, re.I) else s  # XXXX placeholder = empty


class _Html(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows, self.cell, self.span = [], None, 1

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.rows.append([])
        elif tag in ("td", "th") and self.rows:
            self.cell, self.span = [], int(re.sub(r"\D", "", dict(attrs).get("colspan") or "1") or 1)
        elif tag == "br" and self.cell is not None:
            self.cell.append(" ")

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.cell is not None:
            self.rows[-1] += [clean("".join(self.cell))] + [""] * (self.span - 1)
            self.cell = None


def split_md(md):
    """-> (text_lines, tables); each table = (preceding_text, rows)."""
    md = re.sub(r"<\|ref\|>.*?<\|/ref\|>|<\|det\|>.*?<\|/det\|>", "", md, flags=re.S)
    md = re.sub(r"<table.*?</table>", lambda m: "\n\x00" + m.group(0).replace("\n", " ") + "\n",
                md, flags=re.S | re.I)
    texts, tables, pipe = [], [], []

    def flush():
        rows = [[clean(c) for c in l.strip().strip("|").split("|")] for l in pipe
                if not re.fullmatch(r"[\s|:\-]+", l)]
        if rows:
            tables.append((texts[-1] if texts else "", rows))
        pipe.clear()

    for line in md.splitlines():
        if line.startswith("|"):
            pipe.append(line)
            continue
        flush()
        if line.startswith("\x00"):
            p = _Html()
            p.feed(line[1:])
            tables.append((texts[-1] if texts else "", p.rows))
        elif line.strip():
            texts.append(clean(line.lstrip("# ")))
    flush()
    return texts, tables


def parse_table(rows, heading, page):
    """One table -> list of entries (empty if it does not look like a process-conditions block)."""
    cols, section, ents = None, heading, []
    for r in rows:
        cs = {j: case_of(c) for j, c in enumerate(r) if c and case_of(c)}
        if len(cs) >= 2:
            cols = cs
            continue
        ne = [c for c in r if c]
        if not ne:
            continue
        if len(ne) == 1:
            section = ne[0]
            continue
        if cols:
            left = [c for c in r[:min(cols)] if c]
            right = [c for c in r[max(cols) + 1:] if c]
            vals = [(cols[j], r[j]) for j in cols if j < len(r) and r[j]]
        else:  # key / value / unit row
            left, right, vals = ne[:1], ne[2:], [("single", ne[1])]
        row_no = next((int(c) for c in left if re.fullmatch(r"\d{1,3}", c)), None)
        left = [c for c in left if not re.fullmatch(r"\d{1,3}", c)]
        unit = next((c for c in left[1:] + right if is_unit(c)), None)
        names = [c for c in left if c != unit]
        label = max(names, key=len) if names else ""
        if not label or not vals:
            continue
        unit = unit or unit_from_label(label)
        canon = canonical(label, unit)
        for case, v in vals:
            if v != unit:
                ents.append({"section": section or None, "row_no": row_no, "parameter": label,
                             "canonical": canon, "kind": kind_of(label, canon), "case": case,
                             "case_label": None, "value": v, "unit": unit, "page": page})
    n_canon = len({e["canonical"] for e in ents} - {"other"})
    if n_canon >= 3 or (n_canon >= 1 and cols and PROCESS_RX.search(norm(heading))):
        return ents
    return []


def identity(texts, tables):
    out = {}
    lines = list(texts) + [" ".join(r) for _, rows in tables for r in rows]
    for f, rx in IDENT.items():
        for l in lines:
            m = re.match(rf"\s*({rx})\s*[:#]\s*(.+)", l, re.I)
            if m and not out.get(f):
                out[f] = m.group(m.lastindex).strip()
    for _, rows in tables:
        for r in rows:
            for i, c in enumerate(r):
                for f, rx in IDENT.items():
                    if not out.get(f) and re.fullmatch(rx, norm(c).strip(" :.#")):
                        v = next((x for x in r[i + 1:] if x), None)
                        if v and not any(re.fullmatch(x, norm(v).strip(" :.#")) for x in IDENT.values()):
                            out[f] = v
    out["sheet_title"] = texts[0] if texts else None
    return out


def parse_page(md, page):
    texts, tables = split_md(md)
    ents = [e for h, rows in tables for e in parse_table(rows, h, page)]
    return {**identity(texts, tables), "entries": ents}


# ------------------------------------------------------------ document -> OCR
def render_page(page, dpi):
    r = page.rect
    scale = dpi / 72
    if r.width * r.height * scale * scale > 40e6:  # cap at ~40 megapixels
        scale = math.sqrt(40e6 / (r.width * r.height))
    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), colorspace=fitz.csRGB, alpha=False)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


def load_model(name):
    import torch
    from transformers import AutoModel, AutoTokenizer, BitsAndBytesConfig
    tok = AutoTokenizer.from_pretrained(name, trust_remote_code=True)
    q = BitsAndBytesConfig(  # 4-bit LLM so the 3B model fits 6 GB; vision tower stays bf16
        load_in_4bit=True, bnb_4bit_quant_type="nf4", bnb_4bit_compute_dtype=torch.bfloat16,
        llm_int8_skip_modules=["sam_model", "qwen2_model", "projector", "lm_head"])
    model = AutoModel.from_pretrained(
        name, trust_remote_code=True, use_safetensors=True, quantization_config=q,
        torch_dtype=torch.bfloat16, device_map={"": 0}, attn_implementation="eager")
    return model.eval(), tok


def ocr_image(model, tok, img, tmp):
    p = Path(tmp) / "page.png"
    img.save(p)
    return model.infer(tok, prompt=PROMPT, image_file=str(p), output_path=str(tmp),
                       base_size=1024, image_size=768, crop_mode=True, eval_mode=True)


def page_images(path: Path, dpi):
    if path.suffix.lower() == ".pdf":
        return [render_page(p, dpi) for p in fitz.open(path)]
    return [Image.open(path).convert("RGB")]


def normalize(valves):
    for i, v in enumerate(valves, 1):
        tag = (v.get("tag") or "").strip()
        title = (v.get("sheet_title") or "").strip()
        pages = v.get("source_pages") or []
        v["_name"] = tag or (f"{title} (p{min(pages)})" if title and pages else f"(untitled sheet {i})")
        v["_id"] = norm(v["_name"])
    return valves


def extract(model, tok, path: Path, dpi):
    """-> (valves, raw markdown). Pages without a tag continue the previous sheet."""
    valves, raw = [], []
    with tempfile.TemporaryDirectory() as tmp:
        for n, img in enumerate(page_images(path, dpi), 1):
            md = ocr_image(model, tok, img, tmp)
            raw.append(f"<!-- PAGE {n} -->\n{md}")
            pg = parse_page(md, n)
            tag = (pg.get("tag") or "").strip()
            v = next((v for v in valves if tag and norm(v["tag"]) == norm(tag)), None)
            if v is None and not tag and valves:  # ponytail: assumes continuation page, no title matching
                v = valves[-1]
            if v is None:
                v = {k: pg.get(k) for k in ("sheet_title", "service", "fluid", "line_no", "project")}
                v.update(tag=tag or None, format_name=None, item_no=None, source_pages=[], entries=[])
                valves.append(v)
            v["source_pages"].append(n)
            v["entries"] += pg["entries"]
    for v in valves:
        v["has_values"] = bool(v["entries"])
    return normalize(valves), "\n\n".join(raw)


# ------------------------------------------------------------------ validation
def pressure_base(un):
    if un in P_BASES:
        return un
    if un[:-1] in P_BASES and un[-1:] in ("g", "a"):
        return un[:-1]
    return un


def validate(valves, fname):
    issues = []

    def add(v, e, sev, msg):
        issues.append({"file": fname, "valve": v["_name"],
                       "section": (e.get("section") if e else "") or "",
                       "parameter": e["parameter"] if e else "", "case": e["case"] if e else "",
                       "severity": sev, "issue": msg})

    for v in valves:
        ents = v["entries"]
        if not v.get("has_values") and not ents:
            add(v, None, "info", "No filled process-condition values (blank template or no such block)")
        elif not v.get("has_values") and ents:
            add(v, None, "warning", "Sheet marked blank but entries were returned")
        if v.get("has_values") and not (v.get("tag") or "").strip():
            add(v, None, "warning", "Tag missing")

        seen = set()
        for e in ents:
            key = (norm(e.get("section")), norm(e["parameter"]), e["case"])
            if key in seen:
                add(v, e, "warning", "Duplicate entry for the same row and column")
            seen.add(key)
            val = e.get("value")
            if val is None or str(val).strip() == "":
                add(v, e, "warning", "Entry has no value")
                continue
            if "UNREADABLE" in str(val).upper():
                add(v, e, "error", "Model could not read this cell")
                continue
            canon, un, num = e["canonical"], unit_norm(e.get("unit")), to_num(val)
            if canon not in NON_NUMERIC and num is None:
                add(v, e, "warning", f"Non-numeric value {val!r}; confirm against the sheet")
            if num is None:
                continue
            if canon in PRESSURE | TEMP | FLOW and not un:
                add(v, e, "warning", "No unit captured")
            if canon in PRESSURE and un and not P_UNIT.search(un):
                add(v, e, "warning", f"Unit {e.get('unit')!r} is not a pressure unit")
            if canon in TEMP and un and un not in T_UNITS:
                add(v, e, "warning", f"Unit {e.get('unit')!r} is not a temperature unit")
            if canon in TEMP and un == "c" and num < -273.15:
                add(v, e, "error", "Temperature below absolute zero")
            if canon in FLOW and num < 0:
                add(v, e, "error", "Negative flow rate")
            if canon == "specific_gravity" and not (0.05 <= num <= 20):
                add(v, e, "warning", "Specific gravity outside 0.05-20")

        # P1 - dP = P2 per column (only when unambiguous and units compatible)
        for case in CASES:
            d, dup = {}, set()
            for e in ents:
                if e["case"] == case and e["kind"] == "condition" and \
                        e["canonical"] in ("inlet_pressure", "outlet_pressure", "pressure_drop"):
                    if e["canonical"] in d:
                        dup.add(e["canonical"])
                    d[e["canonical"]] = e
            if len(d) == 3 and not dup:
                p1, p2, dp = d["inlet_pressure"], d["outlet_pressure"], d["pressure_drop"]
                n1, n2, nd = (to_num(x["value"]) for x in (p1, p2, dp))
                u1, u2, ud = (unit_norm(x.get("unit")) for x in (p1, p2, dp))
                if None not in (n1, n2, nd) and u1 == u2 and \
                        pressure_base(u1) == pressure_base(ud) and \
                        abs((n1 - nd) - n2) > max(0.02 * abs(n1), 0.05):
                    add(v, p2, "error", f"P1 {n1} - dP {nd} != P2 {n2} ({case}); one of the three is misread")

        # ordering of flow / temperature across min <= normal <= max
        for canon in FLOW | TEMP:
            by = {e["case"]: e for e in ents
                  if e["canonical"] == canon and e["kind"] == "condition" and to_num(e["value"]) is not None}
            if {"minimum", "normal", "maximum"} <= set(by) and \
                    len({unit_norm(by[c].get("unit")) for c in by}) == 1:
                lo, no, hi = (to_num(by[c]["value"]) for c in ("minimum", "normal", "maximum"))
                if not (lo <= no <= hi):
                    add(v, by["normal"], "warning", f"Min/Normal/Max not ordered: {lo}, {no}, {hi}")

        # unit differs between columns of one row
        rows = {}
        for e in ents:
            rows.setdefault((norm(e.get("section")), norm(e["parameter"])), []).append(e)
        for es in rows.values():
            us = {unit_norm(e.get("unit")) for e in es if e.get("unit")}
            if len(us) > 1:
                add(v, es[0], "warning", "Different units across columns of the same row")
    return issues


# ----------------------------------------------------------------------- Excel
HDR_FILL = PatternFill("solid", fgColor="1F3864")
HDR_FONT = Font(bold=True, color="FFFFFF")
FILLS = {2: PatternFill("solid", fgColor="FFC7CE"), 1: PatternFill("solid", fgColor="FFEB9C")}
THIN = Side(style="thin", color="BFBFBF")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
SEV = {"error": 2, "warning": 1, "info": 0}


def style_sheet(ws, widths):
    for c in ws[1]:
        c.fill, c.font = HDR_FILL, HDR_FONT
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    for row in ws.iter_rows(min_row=1):
        for c in row:
            c.border = BORDER
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions


def pivot(fname, valves, kinds):
    rows = []
    for v in valves:
        groups = {}
        for e in v["entries"]:
            if e["kind"] not in kinds:
                continue
            g = groups.setdefault((norm(e.get("section")), norm(e["parameter"])), {
                "file": fname, "valve": v["_name"], "section": e.get("section") or "",
                "row_no": e.get("row_no"), "parameter": e["parameter"], "canonical": e["canonical"],
                "kind": e["kind"], "units": [], "cells": {}, "page": e.get("page")})
            if e.get("unit") and e["unit"] not in g["units"]:
                g["units"].append(e["unit"])
            g["cells"].setdefault(e["case"], []).append(e.get("value"))
        rows.extend(groups.values())
    return rows


def write_conditions(ws, rows, cell_sev, row_sev):
    ws.append(["Source File", "Valve", "Section", "Row #", "Parameter (as printed)", "Canonical",
               "Kind", "Unit"] + [CASE_TITLES[c] for c in CASES] + ["Page", "Review"])
    for r in rows:
        rk = (r["file"], r["valve"], norm(r["section"]), norm(r["parameter"]))
        vals = []
        for c in CASES:
            xs = [x for x in r["cells"].get(c, []) if x not in (None, "")]
            vals.append(cell_value(xs[0]) if len(xs) == 1 else (" | ".join(map(str, xs)) or None))
        sev = row_sev.get(rk, -1)
        ws.append([r["file"], r["valve"], r["section"], r["row_no"], r["parameter"], r["canonical"],
                   r["kind"], " / ".join(r["units"]) or None] + vals + [r["page"],
                   {2: "ERROR", 1: "CHECK"}.get(sev, "")])
        n = ws.max_row
        for j, c in enumerate(CASES):
            s = cell_sev.get(rk + (c,), -1)
            if s in FILLS:
                ws.cell(n, 9 + j).fill = FILLS[s]
        if sev in FILLS:
            ws.cell(n, 9 + len(CASES) + 1).fill = FILLS[sev]
    style_sheet(ws, [20, 18, 22, 7, 38, 20, 11, 12, 14, 14, 14, 12, 12, 12, 6, 9])


def build_workbook(results, issues, out):
    cell_sev, row_sev = {}, {}
    for i in issues:
        if not i.get("parameter"):
            continue
        rk = (i["file"], i["valve"], norm(i.get("section")), norm(i["parameter"]))
        s = SEV[i["severity"]]
        row_sev[rk] = max(row_sev.get(rk, -1), s)
        if i.get("case"):
            ck = rk + (i["case"],)
            cell_sev[ck] = max(cell_sev.get(ck, -1), s)

    wb = Workbook()
    ws = wb.active
    ws.title = "Process Conditions"
    write_conditions(ws, [r for f, vs in results for r in pivot(f, vs, {"condition", "design"})],
                     cell_sev, row_sev)

    ws2 = wb.create_sheet("Calculated (in table)")
    write_conditions(ws2, [r for f, vs in results for r in pivot(f, vs, {"calculated"})],
                     cell_sev, row_sev)

    ws3 = wb.create_sheet("Sheet Info")
    ws3.append(["Source File", "Valve", "Sheet Title", "Template / Vendor", "Item", "Service",
                "Fluid", "Line No", "Project", "Pages", "Has Values", "Entries"])
    for f, vs in results:
        for v in vs:
            ws3.append([f, v["_name"], v.get("sheet_title"), v.get("format_name"), v.get("item_no"),
                        v.get("service"), v.get("fluid"), v.get("line_no"), v.get("project"),
                        ", ".join(map(str, v.get("source_pages", []))),
                        "yes" if v.get("has_values") else "no", len(v["entries"])])
    style_sheet(ws3, [20, 18, 28, 22, 8, 24, 16, 14, 18, 10, 10, 8])

    ws4 = wb.create_sheet("Long Format")
    ws4.append(["Source File", "Valve", "Section", "Row #", "Parameter", "Canonical", "Kind",
                "Case", "Column Header", "Value", "Unit", "Page"])
    for f, vs in results:
        for v in vs:
            for e in v["entries"]:
                ws4.append([f, v["_name"], e.get("section"), e.get("row_no"), e["parameter"],
                            e["canonical"], e["kind"], e["case"], e.get("case_label"),
                            cell_value(e.get("value")), e.get("unit"), e.get("page")])
    style_sheet(ws4, [20, 18, 22, 7, 38, 20, 11, 10, 16, 16, 12, 6])

    ws5 = wb.create_sheet("Review")
    ws5.append(["Severity", "Source File", "Valve", "Section", "Parameter", "Column", "Issue"])
    for i in sorted(issues, key=lambda x: -SEV[x["severity"]]):
        ws5.append([i["severity"].upper(), i["file"], i["valve"], i.get("section"),
                    i.get("parameter"), i.get("case"), i["issue"]])
        if SEV[i["severity"]] in FILLS:
            ws5.cell(ws5.max_row, 1).fill = FILLS[SEV[i["severity"]]]
    if not issues:
        ws5.append(["", "", "", "", "", "", "Nothing flagged (still spot-check against the source)."])
    style_sheet(ws5, [10, 20, 18, 22, 34, 10, 90])
    try:
        wb.save(out)
    except PermissionError:
        out = f"{Path(out).stem}_{time.strftime('%H%M%S')}.xlsx"
        wb.save(out)
        print(f"Original file is open in Excel - saved as {out} instead.", file=sys.stderr)




# ------------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("files", nargs="*", type=Path)
    ap.add_argument("-o", "--out", default="process_conditions.xlsx")
    ap.add_argument("--dpi", type=int, default=200)
    ap.add_argument("--model", default=MODEL)
    args = ap.parse_args()

    if not args.files:
        here = Path(__file__).parent
        args.files = sorted(p for p in here.iterdir() if p.suffix.lower() in (".pdf", ".png", ".jpg", ".jpeg"))
        if not args.files:
            sys.exit("No image/PDF files found next to main.py.")
    print(f"Loading {args.model} (4-bit)...", file=sys.stderr)
    model, tok = load_model(args.model)
    results, all_issues = [], []
    for f in args.files:
        print(f"[{f.name}] OCR...", file=sys.stderr)
        valves, raw = extract(model, tok, f, args.dpi)
        f.with_suffix(".ocr.md").write_text(raw, encoding="utf-8")  # raw OCR, for checking the parser
        issues = validate(valves, f.name)
        results.append((f.name, valves))
        all_issues += issues
        n_err = sum(1 for i in issues if i["severity"] == "error")
        print(f"  {len(valves)} sheet(s), {sum(len(v['entries']) for v in valves)} cells, "
              f"{n_err} error(s), {len(issues) - n_err} other flag(s)", file=sys.stderr)

    build_workbook(results, all_issues, args.out)
    print(f"Wrote {args.out}. Review the 'Review' sheet first.", file=sys.stderr)


if __name__ == "__main__":
    main()
