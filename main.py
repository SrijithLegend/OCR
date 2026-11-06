#!/usr/bin/env python3
"""
Extract SERVICE / PROCESS CONDITIONS from valve datasheets of ANY layout
(PDF or image) into a clean Excel workbook, with per-cell review flags.

    pip install google-genai pymupdf pillow openpyxl
    put GENAI_API_KEY=... in a .env file next to this script
    python main.py a.pdf b.png c.pdf -o process_conditions.xlsx
    (no files given = every pdf/image in this folder)

Options
    --passes {1,2}  2 = independent second read; disputed cells are re-read a
                    third time and decided by majority (default 2)
    --dpi N         PDF render resolution (default 300)
    --bands N       vertical zoom tiles per page (default 3, 0 = none)
    --cols N        horizontal zoom tiles per page (default 1; use 2 for dense,
                    two-column sheets)
    --model M       default env GEMINI_MODEL or gemini-3.5-flash-lite

Accuracy design
    * columns are mapped by HEADER TEXT (Max/Norm/Min order differs per vendor)
    * PDF text layer (layout preserved) is supplied when it exists, so digits
      come from real characters, not pixels
    * full page + overlapping zoom tiles are sent for legibility
    * values are copied as printed; illegible -> "UNREADABLE", blanks omitted
    * two independent reads, disputed cells re-read and majority-voted
    * engineering checks (P1 - dP = P2, unit sanity, ordering, ranges)
    * everything doubtful lands on the Review sheet and is coloured in Excel
"""
import argparse
import copy
import io
import json
import math
import os
import re
import sys
import time
from pathlib import Path

from google import genai
from google.genai import types
from PIL import Image
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

try:
    import pymupdf as fitz
except ImportError:  # older PyMuPDF
    import fitz

Image.MAX_IMAGE_PIXELS = None

_env = Path(__file__).with_name(".env")
if _env.exists():
    for _l in _env.read_text(encoding="utf-8").splitlines():
        if "=" in _l and not _l.lstrip().startswith("#"):
            _k, _v = _l.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip().strip("\"'"))

MODEL =os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")

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

# --------------------------------------------------------------------- prompts
SYSTEM = """You transcribe the SERVICE / PROCESS CONDITIONS of industrial valve datasheets (control valves, relief valves, ball/on-off valves) from many customers and vendors. Layouts differ; the job does not.

SCOPE - extract only:
- Sheet identity: title, tag / item / KKS number, service, fluid, line number, project, template or vendor name.
- The block(s) describing the duty of the valve, under headings such as PROCESS DATA, OPERATING DATA, OPERATING CONDITIONS, SERVICE CONDITIONS, UPSTREAM / DOWNSTREAM CONDITIONS, VALVE DESIGN CONDITIONS, or "Process Data" on relief-valve sheets. Include the fluid name / state and critical or shut-off pressure when printed with that block.
- IGNORE: pipe line, body, bonnet, trim, actuator, positioner, accessories, testing, notes, revision/approval tables, logos, charts.

OUTPUT: one entry per FILLED cell. Fields:
- section: block heading as printed (or null).
- row_no: printed line number at the left of the row, else null.
- parameter: row label EXACTLY as printed, symbols included, e.g. "Inlet Pressure (P1)".
- canonical: closest value from the enum. Use "other" if nothing fits; never force a fit. Flow: mass units (kg/h, lb/h) -> flow_rate_mass; volumetric liquid (m3/h, gpm) -> flow_rate_volume; gas at standard conditions (scfh, Nm3/h) -> flow_rate_gas_std. Upstream/inlet temperature T1 -> inlet_temperature; T2 -> outlet_temperature. dP / pressure differential / pressure drop -> pressure_drop. Cp/Cv, gamma, isentropic exponent -> specific_heat_ratio.
- kind: "condition" = specified duty value (flow, pressures, temperatures, fluid properties); "design" = design limits (design pressure/temperature, shut-off or max allowable differential pressure, corrosion allowance); "calculated" = results of sizing sitting in the same table (Cv, % open / travel, capacity required, noise / SPL, velocities, Reynolds number).
- case: which column the value is under. Map by the printed HEADER TEXT, never by position (some sheets print Max/Norm/Min, others Min/Normal/Max): max/maximum -> maximum; norm/normal/operating -> normal; min/minimum -> minimum; design -> design; "Others"/"Other"/"Notes" -> other; a single value column with no case header -> single.
- case_label: the column header exactly as printed (or null).
- value: the cell text EXACTLY as printed.
- unit: unit exactly as printed, taken from the Units column or from inside the label (e.g. "Flow Rate Mass kg/h"). Keep gauge/absolute markers: "bar(g)", "kPaa", "kg/cm2g". Write superscripts as plain digits. If one unit applies to the whole row, repeat it on every entry of the row. null if none.
- page: page number the cell is on.

TRANSCRIPTION RULES (accuracy matters more than completeness):
1. Copy values exactly: digits, decimals, exponents (1.4E+06), signs, "<", ">". Never round, convert units, compute, reorder or normalise.
2. Never infer, interpolate or copy a value from another cell or another sheet. Empty cell -> no entry.
3. If any character is not clearly legible, set value to "UNREADABLE". Do not guess digits.
4. Sub-headers such as "Liquid / Liquid / Liquid" under Max / Norm / Min are VALUES of a row (e.g. Inlet State), not case labels.
5. Watermarks, stamps, logos or revision clouds overlapping text are not data. Read through them; if a character stays unclear use "UNREADABLE". Black bars / redactions = empty cell.
6. Placeholder text such as "XXXX" or "xxxxxxxx" = empty. "N/A", "NA", "-", "NIL" are real answers: copy them.
7. A blank template (no values filled in) -> has_values=false and entries=[]. Do NOT invent example values.
8. A sheet's title or tag can be on a different page from its table. Attach each table to the correct sheet using the tag/title, not page order; list the pages used in source_pages. One record per sheet.
9. Sheets without a process/service-conditions block (or with nothing filled) still get a record with has_values=false.
10. TEXT LAYER (when supplied) is real text: use it for characters; the images decide which row/column a value belongs to. If they disagree on a digit, use "UNREADABLE". ZOOM tiles are overlapping crops of the same page for legibility; emit each cell once.

Return only JSON matching the provided schema."""

ADJ_SYSTEM = """You re-read specific cells of valve datasheets. For each numbered question, locate the cell using the page, section, row label and column header given, and report the value and unit EXACTLY as printed.
- Do not infer from neighbouring cells, other columns or physics.
- If the cell is empty, return value null. If any character is not clearly legible, return "UNREADABLE".
- A TEXT LAYER, when supplied, holds real characters; the images decide the row/column.
Return only JSON matching the provided schema."""

STR = {"type": ["string", "null"]}
ENTRY = {
    "type": "object",
    "properties": {
        "section": STR,
        "row_no": {"type": ["integer", "null"]},
        "parameter": {"type": "string"},
        "canonical": {"type": "string", "enum": CANON},
        "kind": {"type": "string", "enum": KINDS},
        "case": {"type": "string", "enum": CASES},
        "case_label": STR,
        "value": STR,
        "unit": STR,
        "page": {"type": "integer"},
    },
    "required": ["parameter", "canonical", "kind", "case", "value", "unit", "page"],
}
RECORD_SCHEMA = {
    "type": "object",
    "properties": {
        "valves": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "sheet_title": STR, "format_name": STR, "tag": STR, "item_no": STR,
                    "service": STR, "fluid": STR, "line_no": STR, "project": STR,
                    "source_pages": {"type": "array", "items": {"type": "integer"}},
                    "has_values": {"type": "boolean"},
                    "entries": {"type": "array", "items": ENTRY},
                },
                "required": ["tag", "source_pages", "has_values", "entries"],
            },
        }
    },
    "required": ["valves"],
}
ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "answers": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "integer"}, "value": STR, "unit": STR},
                "required": ["id", "value", "unit"],
            },
        }
    },
    "required": ["answers"],
}


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


# ------------------------------------------------------------ document -> content
def layout_text(page):
    """Rebuild text rows from word boxes; ' | ' marks a column gap."""
    words = page.get_text("words")
    if not words:
        return ""
    words = sorted(words, key=lambda w: ((w[1] + w[3]) / 2, w[0]))
    rows = []
    for w in words:
        yc, h = (w[1] + w[3]) / 2, max(w[3] - w[1], 1.0)
        if rows and abs(yc - rows[-1]["yc"]) <= 0.5 * max(h, rows[-1]["h"]):
            r = rows[-1]
            r["w"].append(w)
            r["yc"] = (r["yc"] * (len(r["w"]) - 1) + yc) / len(r["w"])
            r["h"] = max(r["h"], h)
        else:
            rows.append({"yc": yc, "h": h, "w": [w]})
    lines = []
    for r in rows:
        ws = sorted(r["w"], key=lambda w: w[0])
        parts = [ws[0][4]]
        for a, b in zip(ws, ws[1:]):
            parts.append(" | " if b[0] - a[2] > 1.2 * r["h"] else " ")
            parts.append(b[4])
        lines.append("".join(parts))
    return "\n".join(lines)


def render_page(page, dpi):
    r = page.rect
    scale = dpi / 72
    if r.width * r.height * scale * scale > 40e6:  # cap at ~40 megapixels
        scale = math.sqrt(40e6 / (r.width * r.height))
    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), colorspace=fitz.csRGB, alpha=False)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


def fit(img, lo=1400, hi=1568):
    """Keep the long edge in [lo, hi] px: small images get enough tokens to read,
    large ones are not silently downscaled by the API."""
    w, h = img.size
    long_edge = max(w, h)
    if lo <= long_edge <= hi:
        return img
    s = (lo if long_edge < lo else hi) / long_edge
    return img.resize((max(1, round(w * s)), max(1, round(h * s))), Image.LANCZOS)


def img_block(img):
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return types.Part.from_bytes(data=buf.getvalue(), mime_type="image/png")


def tiles(img, rows, cols, overlap=0.12):
    w, h = img.size

    def spans(total, n):
        if n <= 1:
            return [(0, total)]
        size = total / (n - (n - 1) * overlap)
        step = size * (1 - overlap)
        return [(int(k * step), min(total, int(k * step + size))) for k in range(n)]

    for ri, (t, b) in enumerate(spans(h, rows), 1):
        for ci, (l, r) in enumerate(spans(w, cols), 1):
            yield ri, ci, img.crop((l, t, r, b))


def build_content(path: Path, dpi=300, bands=3, cols=1):
    pages = []
    if path.suffix.lower() == ".pdf":
        doc = fitz.open(path)
        for page in doc:
            pages.append((render_page(page, dpi), layout_text(page)))
    else:
        pages.append((Image.open(path).convert("RGB"), ""))

    blocks = []
    for i, (img, text) in enumerate(pages, 1):
        blocks.append(f"=== PAGE {i} of {len(pages)} ===")
        blocks.append(img_block(fit(img)))
        if len(text) >= 80:
            blocks.append(f"TEXT LAYER of page {i} (real characters; ' | ' = column gap):\n{text}")
        if bands > 0 or cols > 1:
            for ri, ci, t in tiles(img, max(bands, 1), max(cols, 1)):
                blocks.append(f"ZOOM tile row {ri} col {ci} of page {i} (overlaps neighbours)")
                blocks.append(img_block(fit(t)))
    return blocks


# ---------------------------------------------------------------------- API calls
def call_json(client, model, system, schema, content):
    for attempt in range(6):
        try:
            resp = client.models.generate_content(
                model=model, contents=content,
                config=types.GenerateContentConfig(
                    system_instruction=system, temperature=0,
                    response_mime_type="application/json", response_json_schema=schema))
            if not resp.text:
                raise RuntimeError("Model returned no output (blocked or truncated).")
            return json.loads(resp.text)
        except Exception as e:
            transient = any(c in str(e) for c in ("503", "429", "500", "disconnected", "timed out"))
            if attempt == 5 or not transient:
                raise
            time.sleep(10 * (attempt + 1))


def normalize(valves):
    for i, v in enumerate(valves, 1):
        tag = (v.get("tag") or "").strip()
        title = (v.get("sheet_title") or "").strip()
        pages = v.get("source_pages") or []
        v["_name"] = tag or (f"{title} (p{min(pages)})" if title and pages else f"(untitled sheet {i})")
        v["_id"] = norm(v["_name"])
        v["entries"] = v.get("entries") or []
    return valves


def extract_once(client, model, shared):
    out = call_json(client, model, SYSTEM, RECORD_SCHEMA, shared + [
        "Extract the service/process conditions of every valve sheet in this document."])
    return normalize(out.get("valves", []))


# ------------------------------------------------------- reconcile two passes
def keyed(v):
    out, seen = {}, {}
    for e in v["entries"]:
        k = (norm(e.get("section")), norm(e["parameter"]), e["case"])
        n = seen.get(k, 0)
        seen[k] = n + 1
        out[k + (n,)] = e
    return out


def find_disputes(a_valves, b_valves, fname, issues):
    disputes = []
    idx_b = {v["_id"]: v for v in b_valves}
    for va in a_valves:
        vb = idx_b.pop(va["_id"], None)
        if vb is None:
            issues.append({"file": fname, "valve": va["_name"], "section": "", "parameter": "",
                           "case": "", "severity": "error", "issue": "Sheet not found in second pass"})
            continue
        ka, kb = keyed(va), keyed(vb)
        for k in sorted(set(ka) | set(kb), key=str):
            a, b = ka.get(k), kb.get(k)
            if a and b and same_value(a.get("value"), b.get("value")) \
                    and unit_norm(a.get("unit")) == unit_norm(b.get("unit")):
                continue
            disputes.append({"id": len(disputes), "valve": va, "a": a, "b": b})
    for vb in idx_b.values():
        issues.append({"file": fname, "valve": vb["_name"], "section": "", "parameter": "",
                       "case": "", "severity": "error", "issue": "Sheet only found in second pass"})
    return disputes


def adjudicate(client, model, shared, disputes):
    lines = []
    for d in disputes:
        ref = d["a"] or d["b"]
        lines.append(f'Q{d["id"]}: sheet "{d["valve"]["_name"]}", page {ref.get("page")}, '
                     f'section "{ref.get("section") or "-"}", row {ref.get("row_no") or "-"} '
                     f'"{ref["parameter"]}", column "{ref.get("case_label") or ref["case"]}": '
                     f'value and unit as printed?')
    out = call_json(client, model, ADJ_SYSTEM, ANSWER_SCHEMA, shared + [
        "Re-read these cells. Use the question number as id.\n" + "\n".join(lines)])
    return {a["id"]: a for a in out.get("answers", [])}


def apply_adjudication(disputes, answers, fname, issues):
    for d in disputes:
        a, b, v = d["a"], d["b"], d["valve"]
        ref = a or b
        base = {"file": fname, "valve": v["_name"], "section": ref.get("section") or "",
                "parameter": ref["parameter"], "case": ref["case"]}
        ans = answers.get(d["id"]) or {}
        av, bv = (a or {}).get("value"), (b or {}).get("value")
        ua, ub = (a or {}).get("unit"), (b or {}).get("unit")
        rv = ans.get("value")
        readable = rv is not None and "UNREADABLE" not in str(rv).upper()
        win = None
        if readable and a and av is not None and same_value(rv, av):
            win = "a"
        elif readable and b and bv is not None and same_value(rv, bv):
            win = "b"
        if win is None:
            if a is None and b is not None:
                v["entries"].append(copy.deepcopy(b))
            issues.append({**base, "severity": "error",
                           "issue": f"Unresolved: pass 1 {av!r}, pass 2 {bv!r}, re-read {rv!r}"})
            continue
        t = a if a else copy.deepcopy(b)
        if not a:
            v["entries"].append(t)
        if win == "b":
            t["value"], t["unit"] = bv, ub
        msg = f"Passes disagreed (1: {av!r}, 2: {bv!r}); re-read decided {t['value']!r}"
        if a and b and unit_norm(ua) != unit_norm(ub):
            t["unit"] = ub if unit_norm(ans.get("unit")) == unit_norm(ub) else ua
            msg += f"; unit differed ({ua!r} vs {ub!r}) -> {t['unit']!r}"
        issues.append({**base, "severity": "warning", "issue": msg})


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
def process_file(client, model, path, args):
    shared = build_content(path, args.dpi, args.bands, args.cols)
    issues = []
    runs = [extract_once(client, model, shared) for _ in range(args.passes)]
    valves = runs[0]
    if len(runs) == 2:
        disputes = find_disputes(runs[0], runs[1], path.name, issues)
        if disputes:
            answers = adjudicate(client, model, shared, disputes)
            apply_adjudication(disputes, answers, path.name, issues)
    issues += validate(valves, path.name)
    return valves, issues


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("files", nargs="*", type=Path)
    ap.add_argument("-o", "--out", default="process_conditions.xlsx")
    ap.add_argument("--passes", type=int, choices=[1, 2], default=2)
    ap.add_argument("--dpi", type=int, default=300)
    ap.add_argument("--bands", type=int, default=3)
    ap.add_argument("--cols", type=int, default=1)
    ap.add_argument("--model", default=MODEL)
    args = ap.parse_args()

    if not args.files:
        here = Path(__file__).parent
        args.files = sorted(p for p in here.iterdir() if p.suffix.lower() in (".pdf", ".png", ".jpg", ".jpeg"))
        if not args.files:
            sys.exit("No image/PDF files found next to main.py.")
    client = genai.Client(api_key=os.environ["GENAI_API_KEY"])
    results, all_issues = [], []
    for f in args.files:
        print(f"[{f.name}] extracting...", file=sys.stderr)
        valves, issues = process_file(client, args.model, f, args)
        results.append((f.name, valves))
        all_issues += issues
        n_err = sum(1 for i in issues if i["severity"] == "error")
        print(f"  {len(valves)} sheet(s), {sum(len(v['entries']) for v in valves)} cells, "
              f"{n_err} error(s), {len(issues) - n_err} other flag(s)", file=sys.stderr)

    build_workbook(results, all_issues, args.out)
    print(f"Wrote {args.out}. Review the 'Review' sheet first.", file=sys.stderr)


if __name__ == "__main__":
    main()