"""python eval/make_template.py  -> eval/truth_template.csv (blank `value`, prefilled=0 everywhere)

Row KEYS (file, valve_tag, section, parameter, case, unit) are the union of cells either pass produced, so `value` is never
copied from the tool. Limitation: a cell the tool never produced has no row. While keying, ADD a row for every printed value
the template lacks (keep the same section/parameter/case spelling as the sheet). Copy values as printed;
unreadable = UNREADABLE; empty cell = leave empty. Save as eval/truth.csv.
"""
import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
import openpyxl


def raw_keys(path, fname):
    ws = openpyxl.load_workbook(path)["Datasheet Data"]
    heads = []
    for c in ws[1][3:]:
        parts = (c.value or "").split("\n")
        unit = parts[-1].strip("()") if parts and parts[-1].startswith("(") else ""
        if unit:
            parts = parts[:-1]
        heads.append((parts[0] if len(parts) > 1 else "", parts[-1] if parts else "", unit))
    for row in ws.iter_rows(min_row=2):
        if str(row[0].value) == fname:
            for c, (sec, par, unit) in zip(row[3:], heads):
                if c.value not in (None, ""):
                    yield (fname, str(row[1].value or ""), sec, par, str(row[2].value or ""), unit)


split = json.loads((ROOT / "eval/split.json").read_text())
rows = []
for fname in split["dev"] + split["test"]:
    seen = {}
    for p in ("p1", "p2"):
        x = ROOT / f"results/{p}/{fname}.xlsx"
        if x.exists():
            for k in raw_keys(x, fname):
                seen.setdefault(k[:5], k)
    rows += sorted(seen.values())
with open(ROOT / "eval/truth_template.csv", "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(["file", "valve_tag", "section", "parameter", "case", "unit", "value", "prefilled"])
    w.writerows([*r, "", 0] for r in rows)
print(len(rows), "rows ->", "eval/truth_template.csv")
