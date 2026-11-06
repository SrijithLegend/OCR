"""OCR markdown -> valve-sheet entries. Pure Python (no torch), so it is unit-tested without a GPU.

DeepSeek-OCR-2 returns markdown / HTML tables, not structured data. This module turns each table
into entries using printed-header rules only: columns are mapped by header TEXT, values are copied
exactly as printed, and nothing is computed or inferred.
"""
import html
import re
from html.parser import HTMLParser

from ..constants import CASES
from ..textutil import norm, to_num, unit_norm

DESIGN_CANON = {"design_pressure", "design_temperature", "shutoff_pressure", "corrosion_allowance"}
KIND_CALC = re.compile(r"\b(cv|kv|cg)\b|travel|% ?open|noise|\bspl\b|velocity|reynolds|required|mach|dba")
CASE_RX = [("minimum", r"(min|minimum)"), ("normal", r"(norm|normal|nor|operating|oper)"),
           ("maximum", r"(max|maximum)"), ("design", r"design"), ("other", r"(others?|notes?)")]
UNIT_RX = re.compile(
    r"(kg/?h|lb/?h|t/?h|kg/?s|m3/?h|nm3/?h|sm3/?h|gpm|scfh|scfm|mmscfd|bar[ga]?|mbar|psi[ga]?|kpa[ga]?|"
    r"mpa[ga]?|pa|kg/?cm2[ga]?|kgf/?cm2[ga]?|atm|c|f|k|r|cp|cst|kg/m3|lb/ft3|g/mol|mm|in|ppm|%|cm2|m3/?s|"
    r"l/?s|l/?min|kw|m/?s)")
MASS_U, GAS_U = re.compile(r"(kg|lb|t)/?[hs]"), re.compile(r"(scf|nm3|sm3|mmscf|scm)")
PROCESS_RX = re.compile(r"process|operating|service|upstream|downstream|condition")
# rows under these headings are out of scope (body, trim, actuator ...) even inside one big table
SKIP_SECTION_RX = re.compile(r"body|bonnet|trim|actuator|positioner|accessor|testing|test |note|"
                             r"pipe|revision|approval|noise data|travel")
IDENT = {  # sheet field -> label regex, matched against a whole label cell
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


def is_unit(s) -> bool:
    u = unit_norm(s)
    return bool(u) and len(str(s)) <= 14 and UNIT_RX.fullmatch(u) is not None


def unit_from_label(label):
    m = re.search(r"[\(\[]\s*([^()\[\]]{1,12}?)\s*[\)\]]\s*$", label)
    if m and is_unit(m.group(1)):
        return m.group(1)
    words = label.split()
    return words[-1] if words and len(words[-1]) > 1 and is_unit(words[-1]) else None


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
    return "" if re.fullmatch(r"x{3,}", s, re.I) else s  # XXXX placeholder = empty cell


class _Html(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows, self.cell, self.span = [], None, 1

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.rows.append([])
        elif tag in ("td", "th") and self.rows:
            self.cell = []
            self.span = int(re.sub(r"\D", "", dict(attrs).get("colspan") or "1") or 1)
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
    md = re.sub(r"<\|ref\|>.*?<\|/ref\|>|<\|det\|>.*?<\|/det\|>", "", md or "", flags=re.S)
    md = re.sub(r"<table.*?</table>", lambda m: "\n\x00" + m.group(0).replace("\n", " ") + "\n",
                md, flags=re.S | re.I)
    texts, tables, pipe = [], [], []

    def flush():
        rows = [[clean(c) for c in ln.strip().strip("|").split("|")] for ln in pipe
                if not re.fullmatch(r"[\s|:\-]+", ln)]
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
    """One table -> entries; [] unless it looks like a process-conditions block."""
    cols, section, skip, ents = None, heading, False, []
    for r in rows:
        cs = {j: (case_of(c), c) for j, c in enumerate(r) if c and case_of(c)}
        if len(cs) >= 2:
            cols, skip = cs, False
            continue
        ne = [c for c in r if c]
        if not ne:
            continue
        if len(ne) == 1:  # section banner spanning the table
            section = ne[0]
            skip = bool(SKIP_SECTION_RX.search(norm(section)))
            continue
        if skip:
            continue
        if cols:
            left = [c for c in r[:min(cols)] if c]
            right = [c for c in r[max(cols) + 1:] if c]
            vals = [(cols[j][0], cols[j][1], r[j]) for j in cols if j < len(r) and r[j]]
        else:  # plain key / value / unit row (single value column)
            left, right, vals = ne[:1], ne[2:], [("single", None, ne[1])]
        row_no = next((int(c) for c in left if re.fullmatch(r"\d{1,3}", c)), None)
        left = [c for c in left if not re.fullmatch(r"\d{1,3}", c)]
        unit = next((c for c in left[1:] + right if is_unit(c)), None)
        names = [c for c in left if c != unit]
        label = max(names, key=len) if names else ""
        if not label or not vals:
            continue
        unit = unit or unit_from_label(label)
        canon = canonical(label, unit)
        for case, case_label, v in vals:
            if v != unit:
                ents.append({"section": section or None, "row_no": row_no, "parameter": label,
                             "canonical": canon, "kind": kind_of(label, canon), "case": case,
                             "case_label": case_label, "value": v, "unit": unit, "page": page})
    n_canon = len({e["canonical"] for e in ents} - {"other"})
    process_head = PROCESS_RX.search(norm(heading)) or any(
        PROCESS_RX.search(norm(e["section"])) for e in ents if e["section"])
    if n_canon >= 3 or (n_canon >= 1 and cols and process_head):
        return ents
    return []


def identity(texts, tables):
    out = {}
    lines = list(texts) + [" ".join(r) for _, rows in tables for r in rows]
    for f, rx in IDENT.items():
        for ln in lines:
            m = re.match(rf"\s*({rx})\s*[:#]\s*(.+)", ln, re.I)
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


# ------------------------------------------------------------ pages -> valve sheets
def build_valves(identities, entry_lists):
    """identities[i] / entry_lists[i] belong to page i+1. A page without a tag continues the previous
    sheet. Two reads that share `identities` produce valves in the same order with the same tags."""
    valves = []
    for n, (ident, ents) in enumerate(zip(identities, entry_lists), 1):
        tag = (ident.get("tag") or "").strip()
        v = next((v for v in valves if tag and norm(v["tag"]) == norm(tag)), None)
        if v is None and not tag and valves:  # ponytail: continuation page assumed, no title matching
            v = valves[-1]
        if v is None:
            v = {k: ident.get(k) for k in ("sheet_title", "service", "fluid", "line_no", "project")}
            v.update(tag=tag or None, format_name=None, item_no=None, source_pages=[], entries=[])
            valves.append(v)
        v["source_pages"].append(n)
        v["entries"] += [dict(e) for e in ents]
    for v in valves:
        v["has_values"] = bool(v["entries"])
    return valves


# ----------------------------------------------------- provenance (text layer / tiles)
_NUMTOK = re.compile(r"(?<![\w.])[+-]?(?:\d[\d,]*\.?\d*|\.\d+)(?:[eE][+-]?\d+)?(?![\w])")


def _core(value):
    """The numeric part of a printed value ('<75' -> '75'); None for text values."""
    m = re.fullmatch(r"\s*[<>≤≥~]?\s*([+-]?(?:\d[\d,]*\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)\s*", str(value or ""))
    return m.group(1).replace(",", "") if m else None


def numbers_in(text):
    return {t.replace(",", "") for t in _NUMTOK.findall(text or "")}


def in_evidence(value, nums):
    c = _core(value)
    return c is None or c in nums


def near_numbers(core, nums):
    """Numbers in the evidence one character away from `core` (same length)."""
    return sorted(n for n in nums if len(n) == len(core) and n != core
                  and sum(a != b for a, b in zip(n, core)) == 1)
