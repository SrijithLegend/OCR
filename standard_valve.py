"""Standard description per part number, for any valve datasheet export.

Groups rows by part number and takes the most common description (ignoring case/spacing) as the standard.
Output: Part No., Standard Description, Rows, Disagreeing values. Nothing about the description's format is assumed.
"""
import argparse
import collections
import csv
import re
from pathlib import Path

import openpyxl
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter, column_index_from_string

PN_HEAD = re.compile(r"part|p/?n|material|item\s*(no|code|#)|code", re.I)
DESC_HEAD = re.compile(r"desc", re.I)


def norm(s):
    return " ".join(str(s).upper().split())


def load_table(path):
    path = Path(path)
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        ws = openpyxl.load_workbook(path, data_only=True).active
        return [list(r) for r in ws.iter_rows(values_only=True)]
    with path.open(newline="", encoding="utf-8-sig") as f:
        return [list(r) for r in csv.reader(f)]


def find_col(spec, header, ncols):
    for i, h in enumerate(header or []):
        if h is not None and str(h).strip().lower() == spec.strip().lower():
            return i
    if spec.isalpha() and column_index_from_string(spec.upper()) <= ncols:
        return column_index_from_string(spec.upper()) - 1
    raise SystemExit(f"Column '{spec}' not found")


def load_master(path):
    """xlsx/csv with 'Part No.' and 'Standard Description' -> {part no.: description}."""
    rows = load_table(path)
    head = [str(h or "").strip().lower() for h in rows[0]]
    ip, idesc = head.index("part no."), head.index("standard description")
    return {str(r[ip]).strip().upper(): " ".join(str(r[idesc]).split()) for r in rows[1:] if r[ip] and r[idesc]}


def run(rows, desc_spec=None, pn_spec=None, header_mode="auto", master=None, pn_regex=None):
    """-> (rows read, [[part no., standard description or None, row count, disagreeing values or None], ...])"""
    ncols = max(len(r) for r in rows)
    rows = [r + [None] * (ncols - len(r)) for r in rows]
    first = [str(c or "") for c in rows[0]]
    has_header = header_mode == "yes" or (header_mode == "auto" and any(PN_HEAD.search(c) or DESC_HEAD.search(c) for c in first))
    head = first if has_header else None
    body = [r for r in (rows[1:] if has_header else rows) if any(c not in (None, "") for c in r)]
    pn_re = re.compile(pn_regex) if pn_regex else None

    def guess(rx):
        return next((i for i, h in enumerate(first) if has_header and rx.search(h)), None)

    desc_i = find_col(desc_spec, head, ncols) if desc_spec else guess(DESC_HEAD)
    if desc_i is None:  # no header to go by: the column with the longest text
        desc_i = max(range(ncols), key=lambda c: sum(len(str(r[c])) for r in body if isinstance(r[c], str)))
    pn_i = find_col(pn_spec, head, ncols) if pn_spec else (None if pn_re else guess(PN_HEAD))
    if pn_i is None and not pn_re:
        raise SystemExit("Could not find the part number column. Pass --pn-col (letter or header name) or --pn-regex.")

    groups = collections.defaultdict(lambda: collections.defaultdict(list))  # pn -> normalized desc -> originals
    for r in body:
        text = r[desc_i]
        if text in (None, ""):
            continue
        text = " ".join(str(text).split())
        if pn_i is not None:
            pn = str(r[pn_i]).strip().upper() if r[pn_i] not in (None, "") else None
        else:
            m = pn_re.search(text.upper())
            pn = m.group(0) if m else None
            text = text.strip(" ,;") if not m else (text[:m.start()] + " " + text[m.end():]).strip(" ,;")
        if pn:
            groups[pn][norm(text)].append(text)

    out = []
    for pn in sorted(set(groups) | set(master or {})):
        votes = sorted(groups.get(pn, {}).values(), key=len, reverse=True)
        n = sum(len(v) for v in votes)
        if master and pn in master:
            std, others = master[pn], [v for v in votes if norm(v[0]) != norm(master[pn])]
        elif not votes:
            continue
        elif len(votes) > 1 and len(votes[0]) == len(votes[1]):
            std, others = None, votes  # tie: no majority, a person decides
        else:
            std, others = votes[0][0], votes[1:]
        out.append([pn, std, n, "; ".join(f"{v[0]} (x{len(v)})" for v in others) or None])
    return len(body), out


# ----------------------------------------------------------------------------- output
def write_summary(path, master_rows):
    """One row per part number: Part No., Standard Description, Rows, Disagreeing values."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Standard descriptions"
    ws.append(["Part No.", "Standard Description", "Rows", "Disagreeing values"])
    for r in master_rows:
        ws.append(r)
        if not r[1]:
            ws.cell(ws.max_row, 2).fill = PatternFill("solid", fgColor="FFC7CE")
        elif r[3]:
            ws.cell(ws.max_row, 4).fill = PatternFill("solid", fgColor="FFEB9C")
    for c in ws[1]:
        c.fill, c.font = PatternFill("solid", fgColor="1F3864"), Font(bold=True, color="FFFFFF")
    for i in range(1, 5):
        w = max((len(str(c.value)) for c in ws[get_column_letter(i)] if c.value is not None), default=8)
        ws.column_dimensions[get_column_letter(i)].width = min(max(w + 2, 8), 70)
    ws.freeze_panes = "A2"
    wb.save(path)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input")
    ap.add_argument("-o", "--output", required=True)
    ap.add_argument("--desc-col", help="description column: letter (B) or header name. Default: header containing 'desc', else the longest-text column")
    ap.add_argument("--pn-col", help="part number column: letter or header name. Default: header containing 'part'/'material'/'code'")
    ap.add_argument("--pn-regex", help="no part number column? pull it out of the description with this regex")
    ap.add_argument("--header", choices=["auto", "yes", "no"], default="auto")
    ap.add_argument("--master", help="xlsx/csv with 'Part No.' and 'Standard Description' that overrides the vote")
    a = ap.parse_args(argv)
    if Path(a.output).resolve() == Path(a.input).resolve():
        raise SystemExit("Output must differ from input; the input is never modified.")
    rows = load_table(a.input)
    if not rows:
        raise SystemExit("Input is empty.")
    n, master_rows = run(rows, a.desc_col, a.pn_col, a.header, load_master(a.master) if a.master else None, a.pn_regex)
    write_summary(a.output, master_rows)
    print(f"{n} rows, {len(master_rows)} part numbers -> {a.output}")
    print(f"  no majority: {sum(1 for r in master_rows if not r[1])} | with disagreements: {sum(1 for r in master_rows if r[1] and r[3])}")


if __name__ == "__main__":
    main()
