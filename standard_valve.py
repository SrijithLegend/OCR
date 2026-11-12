import argparse
import collections
import csv
import re
import sys
from pathlib import Path

import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter, column_index_from_string

# ----------------------------------------------------------------------------- configuration
DEFAULT_PN_REGEX = r"\b\d{2}[A-Z]\d{4}\b"           # 59A3655, 50B2638. Change with --pn-regex
# known variant spellings -> standard spelling. UNCONFIRMED aliases are flagged in Notes.
VARIANTS = {"DP PG": "DP PG", "TAP": "TP", "TP": "TP"}
UNCONFIRMED_ALIASES = {"TAP": "TP"}                  # TAP -> TP is an assumption, not a fact
END_TOKEN = "W/END"
UNIT_MAP = {"IN": "IN", "INCH": "IN", '"': "IN", "MM": "MM"}
TERM_KEYS = ("item", "modifier", "size", "unit", "cls", "end", "variant")
COLS = ["Part No.", "Item", "Modifier", "Size", "Size Unit", "Class", "End", "Variant", "Extra (unparsed)",
        "Standard Description", "Status", "Notes"]
SEVERITY = ["UNPARSED", "NO PART NO.", "UNRESOLVED", "CONFLICT", "REVIEW", "FILLED", "OK"]

LINE_RE = re.compile(
    r"""^\s*(?P<left>[^~]+?)\s*~\s*
        (?P<size>\d+(?:\.\d+)?)\s*(?P<unit>INCH\b|IN\b|"|MM\b)?\s*,\s*
        (?P<cls>CL\s*\d+)\s*(?P<rest>.*?)\s*$""",
    re.I | re.X)


# ----------------------------------------------------------------------------- parsing
def num(x):
    f = float(x)
    return int(f) if f == int(f) else f


STD_SIZE_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*(IN|INCH|MM)$", re.I)


def parse_standard(text, pn_re):
    """Parse the OUTPUT syntax: ITEM[, MODIFIER...], SIZE UNIT, CLASS[, END][, VARIANT]."""
    toks = [t.strip().upper() for t in pn_re.sub(" ", text).split(",") if t.strip()]
    for i, tok in enumerate(toks):
        sm = STD_SIZE_RE.match(tok)
        if sm and i >= 1 and i + 1 < len(toks) and re.fullmatch(r"CL\s*\d+", toks[i + 1]):
            break
    else:
        return None
    rest = toks[i + 2:]
    extra = [x for x in rest if x != END_TOKEN and x not in VARIANTS]
    variant = next((VARIANTS[x] for x in rest if x in VARIANTS), None)
    return {"item": toks[0], "modifier": ", ".join(toks[1:i]) or None, "size": num(sm[1]),
            "unit": UNIT_MAP.get(sm[2].upper()), "cls": re.sub(r"\s+", "", toks[i + 1]),
            "end": END_TOKEN if END_TOKEN in rest else None, "variant": variant,
            "extra": " ".join(extra) or None, "notes": []}


def parse(text, pn_re):
    """-> dict of terms, or None if the text does not fit either syntax."""
    if not isinstance(text, str):
        return None
    m = LINE_RE.match(text.strip())
    if not m:
        return parse_standard(text, pn_re)
    left = [p.strip().upper() for p in m["left"].split(",") if p.strip()]
    rest = pn_re.sub(" ", m["rest"].upper())
    notes = []
    end = None
    e = re.search(r"\bW/END\b", rest)
    if e:
        end, rest = END_TOKEN, rest[:e.start()] + " " + rest[e.end():]
    variant = None
    for raw in sorted(VARIANTS, key=len, reverse=True):
        v = re.search(r"(?:&\s*)?\b" + re.escape(raw).replace(r"\ ", r"\s+") + r"\b", rest)
        if v:
            variant, rest = VARIANTS[raw], rest[:v.start()] + " " + rest[v.end():]
            if raw in UNCONFIRMED_ALIASES:
                notes.append(f"'{raw}' read as '{UNCONFIRMED_ALIASES[raw]}' (assumed alias)")
            break
    extra = re.sub(r"[\s&,]+", " ", rest).strip() or None
    unit = m["unit"].upper() if m["unit"] else None
    return {
        "item": left[0] if left else None,
        "modifier": ", ".join(left[1:]) or None,
        "size": num(m["size"]),
        "unit": UNIT_MAP.get(unit) if unit else None,
        "cls": re.sub(r"\s+", "", m["cls"].upper()),
        "end": end, "variant": variant, "extra": extra, "notes": notes,
    }


def build(t, default_unit):
    unit = t["unit"] or default_unit
    parts = [t["item"]] + ([t["modifier"]] if t["modifier"] else []) + [f"{t['size']} {unit}", t["cls"]]
    if t["end"]:
        parts.append(t["end"])
    if t["variant"]:
        parts.append(t["variant"])
    return ", ".join(parts)


def consensus(rows):
    """Plurality vote per term. blank-vs-one-value tie -> the value; any other tie -> unresolved."""
    out, ties = {}, []
    for k in TERM_KEYS:
        c = collections.Counter(r[k] for r in rows)
        ranked = c.most_common()
        top, n = ranked[0]
        tied = [v for v, cnt in ranked if cnt == n]
        if len(tied) > 1:
            nonblank = [v for v in tied if v is not None]
            if len(tied) == 2 and len(nonblank) == 1:
                top = nonblank[0]
            else:
                top = None
                ties.append(k)
        out[k] = top
    return out, ties


# ----------------------------------------------------------------------------- input
def load_table(path):
    path = Path(path)
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        ws = openpyxl.load_workbook(path, data_only=True).active
        return [list(r) for r in ws.iter_rows(values_only=True)]
    with path.open(newline="", encoding="utf-8-sig") as f:
        return [list(r) for r in csv.reader(f)]


def find_col(spec, header, ncols):
    if spec is None:
        return None
    if header:
        for i, h in enumerate(header):
            if h is not None and str(h).strip().lower() == spec.strip().lower():
                return i
    if spec.isalpha():
        i = column_index_from_string(spec.upper()) - 1
        if i < ncols:
            return i
    raise SystemExit(f"Column '{spec}' not found")


def load_master(path, pn_re):
    path = Path(path)
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        wb = openpyxl.load_workbook(path, data_only=True)
        ws = wb["Part No. master"] if "Part No. master" in wb.sheetnames else wb.active
        rows = [list(r) for r in ws.iter_rows(values_only=True)]
    else:
        rows = load_table(path)
    head = [str(h or "").strip().lower() for h in rows[0]]
    ip, idesc = head.index("part no."), head.index("standard description")
    out = {}
    for r in rows[1:]:
        if r[ip] and r[idesc]:
            t = parse(str(r[idesc]), pn_re)
            if t is None:
                raise SystemExit(f"Master entry for {r[ip]} does not fit the syntax: {r[idesc]!r}")
            out[str(r[ip]).strip().upper()] = t
    return out


# ----------------------------------------------------------------------------- main logic
def run(rows, desc_spec=None, pn_spec=None, header_mode="auto", master=None, pn_regex=DEFAULT_PN_REGEX,
        default_unit="IN"):
    pn_re = re.compile(pn_regex)
    ncols = max(len(r) for r in rows)
    rows = [r + [None] * (ncols - len(r)) for r in rows]

    # description column
    if desc_spec is None:
        score = [sum(1 for r in rows if parse(r[c], pn_re)) for c in range(ncols)]
        desc_i = max(range(ncols), key=lambda c: score[c])
        if score[desc_i] == 0:
            raise SystemExit("No column looks like 'ITEM ~ SIZE,CLASS ...'. Pass --desc-col.")
    else:
        header_probe = rows[0] if header_mode != "no" else None
        desc_i = find_col(desc_spec, header_probe, ncols)
    # header row?
    if header_mode == "yes":
        has_header = True
    elif header_mode == "no":
        has_header = False
    else:
        has_header = parse(rows[0][desc_i], pn_re) is None and any(parse(r[desc_i], pn_re) for r in rows[1:])
    header = [str(h) if h is not None else f"Col {get_column_letter(i + 1)}" for i, h in enumerate(rows[0])] \
        if has_header else [f"Col {get_column_letter(i + 1)}" for i in range(ncols)]
    pn_i = find_col(pn_spec, rows[0] if has_header else None, ncols)
    body = rows[1:] if has_header else rows
    body = [r for r in body if any(c not in (None, "") for c in r)]

    # parse every row
    recs = []
    for r in body:
        text = r[desc_i]
        t = parse(text, pn_re)
        pn = None
        if pn_i is not None and r[pn_i] not in (None, ""):
            pn = str(r[pn_i]).strip().upper()
        elif isinstance(text, str):
            m = pn_re.search(text.upper())
            pn = m.group(0) if m else None
        recs.append({"row": r, "t": t, "pn": pn})

    # vote per part number
    groups = collections.defaultdict(list)
    for x in recs:
        if x["t"] and x["pn"]:
            groups[x["pn"]].append(x["t"])
    cons, ties, source = {}, {}, {}
    for pn, ts in groups.items():
        cons[pn], ties[pn] = consensus(ts)
        source[pn] = "vote"
    for pn, t in (master or {}).items():
        cons[pn], ties[pn], source[pn] = {k: t[k] for k in TERM_KEYS}, [], "master"

    # classify rows
    for x in recs:
        t, pn, notes = x["t"], x["pn"], []
        if t is None:
            x.update(status="UNPARSED", out=None, std=None, notes=["Does not fit 'ITEM ~ SIZE,CLASS ...'; left as is"])
            continue
        notes += t["notes"]
        if pn is None:
            x.update(status="NO PART NO.", out=t, std=None, notes=notes + ["No part number found"])
            continue
        c = cons.get(pn)
        if c is None:  # part number only in master but not in this row's group: cannot happen for parsed rows
            c = {k: t[k] for k in TERM_KEYS}
        if ties.get(pn):
            names = ", ".join(ties[pn])
            x.update(status="UNRESOLVED", out=t, std=None,
                     notes=notes + [f"Rows for {pn} disagree with no majority on: {names}"])
            continue
        fills = [k for k in TERM_KEYS if t[k] is None and c[k] is not None and k != "unit"]
        conflicts = [k for k in TERM_KEYS if t[k] is not None and c[k] != t[k]]
        for k in conflicts:
            notes.append(f"This row says {k}={t[k]!r}; part no. {pn} ({source[pn]}) says {c[k]!r}")
        for k in fills:
            notes.append(f"{k} missing here; filled with {c[k]!r} from part no. {pn}")
        if t["extra"]:
            notes.append(f"Unrecognised text kept in Extra: {t['extra']!r}")
        if c["variant"] is None:
            notes.append("No variant stated for this part no.")
        if source[pn] == "vote" and len(groups[pn]) == 1:
            notes.append("Only one row for this part no.; nothing to cross-check")
        status = "CONFLICT" if conflicts else "REVIEW" if t["extra"] else "FILLED" if fills else "OK"
        x.update(status=status, out=c, std=build(c, default_unit), notes=notes)

    master_rows = []
    for pn in sorted(cons):
        c = cons[pn]
        n = len(groups.get(pn, []))
        conf = sorted({f"{k}: {t[k]!r}" for t in groups.get(pn, []) for k in TERM_KEYS
                       if t[k] is not None and c[k] is not None and t[k] != c[k]})
        master_rows.append([pn, build(c, default_unit) if not ties.get(pn) else None, c["item"], c["modifier"],
                            c["size"], c["unit"] or default_unit, c["cls"], c["end"], c["variant"], n,
                            "; ".join(conf) or None, source[pn]])
    return header, desc_i, recs, master_rows, has_header


# ----------------------------------------------------------------------------- output
FILL = {"OK": None, "FILLED": "FFEB9C", "REVIEW": "FFEB9C", "CONFLICT": "FFC7CE", "UNRESOLVED": "FFC7CE",
        "NO PART NO.": "FFC7CE", "UNPARSED": "FFC7CE"}


def write(path, header, desc_i, recs, master_rows, replace=False, like_input=False, header_row=True):
    """like_input: sheet 1 has exactly the input columns, description replaced by the standard one
    (yellow = changed, red = could not be standardized); the Part No. master sheet follows."""
    replace = replace or like_input
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Standardized"
    head = list(header)
    if replace and not like_input:
        head[desc_i] = "Description (standardized)"
    extra = [] if like_input else COLS + (["Original Description"] if replace else [])
    if header_row:
        ws.append(head + extra)
    for x in recs:
        r, t = x["row"], x["out"]
        std = x["std"]
        terms = [x["pn"], t and t["item"], t and t["modifier"], t and t["size"],
                 t and (t["unit"] or "IN"), t and t["cls"], t and t["end"], t and t["variant"],
                 x["t"] and x["t"]["extra"], std, x["status"], "; ".join(x["notes"]) or None]
        row = list(r)
        orig = row[desc_i]
        if replace and std:
            row[desc_i] = std
        ws.append(row + ([] if like_input else terms + ([orig] if replace else [])))
        if like_input:
            color = "FFC7CE" if not std else "FFEB9C" if str(std) != str(orig) else None
            cell = ws.cell(ws.max_row, desc_i + 1)
        else:
            color = FILL[x["status"]]
            cell = ws.cell(ws.max_row, len(head) + COLS.index("Status") + 1)
        if color:
            cell.fill = PatternFill("solid", fgColor=color)
    m = wb.create_sheet("Part No. master")
    m.append(["Part No.", "Standard Description", "Item", "Modifier", "Size", "Size Unit", "Class", "End",
              "Variant", "Rows", "Disagreeing values", "Source"])
    for r in master_rows:
        m.append(r)
    hf, th = PatternFill("solid", fgColor="1F3864"), Side(style="thin", color="BFBFBF")
    for sh in (ws, m):
        for c in sh[1]:
            c.fill, c.font = hf, Font(bold=True, color="FFFFFF")
            c.alignment = Alignment(horizontal="center", wrap_text=True)
        for row in sh.iter_rows():
            for c in row:
                c.border = Border(left=th, right=th, top=th, bottom=th)
        for i in range(1, sh.max_column + 1):
            width = max((len(str(c.value)) for c in sh[get_column_letter(i)] if c.value is not None), default=8)
            sh.column_dimensions[get_column_letter(i)].width = min(max(width + 2, 8), 60)
        sh.freeze_panes = "A2"
        sh.auto_filter.ref = sh.dimensions
    wb.save(path)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input")
    ap.add_argument("-o", "--output", required=True)
    ap.add_argument("--desc-col", help="description column: letter (B) or header name. Default: auto-detect")
    ap.add_argument("--pn-col", help="part number column. Default: extracted from the description")
    ap.add_argument("--header", choices=["auto", "yes", "no"], default="auto")
    ap.add_argument("--master", help="xlsx/csv with 'Part No.' and 'Standard Description' that overrides the vote")
    ap.add_argument("--pn-regex", default=DEFAULT_PN_REGEX)
    ap.add_argument("--size-unit", default="IN", help="unit to assume when the source has none (default IN)")
    ap.add_argument("--replace", action="store_true", help="overwrite the description column in the OUTPUT")
    a = ap.parse_args(argv)
    if Path(a.output).resolve() == Path(a.input).resolve():
        raise SystemExit("Output must differ from input; the input is never modified.")
    rows = load_table(a.input)
    if not rows:
        raise SystemExit("Input is empty.")
    master = load_master(a.master, re.compile(a.pn_regex)) if a.master else None
    header, desc_i, recs, master_rows, has_header = run(
        rows, a.desc_col, a.pn_col, a.header, master, a.pn_regex, a.size_unit.upper())
    write(a.output, header, desc_i, recs, master_rows, a.replace)
    counts = collections.Counter(x["status"] for x in recs)
    print(f"{len(recs)} rows, {len(master_rows)} part numbers -> {a.output}")
    print("  description column:", header[desc_i], "| header row:", "yes" if has_header else "no")
    for s in SEVERITY:
        if counts[s]:
            print(f"  {s:<12} {counts[s]}")
    print(f"  Assumptions: size unit = {a.size_unit.upper()} when absent; TAP read as TP. Check the Notes column.")


if __name__ == "__main__":
    main()