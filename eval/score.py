"""Score tool workbooks against hand-keyed ground truth.

    python eval/score.py                      # truth.csv + results/p1, results/p2 -> results/scores.json, scores.md

Truth cell key: (file, valve_tag, section, parameter, case). `case` is the row label the workbook shows:
Minimum/Normal/Maximum/Design/Single/Other, or "<stream> - Minimum" etc. when the sheet has several streams.
Normalisation: strip, uppercase, collapse whitespace, drop thousands separators; numbers compared by float
(exact, and within 0.5 % reported separately); units compared after main.unit_norm. Flag = red or yellow fill.
Only files present in truth.csv with at least one keyed value are scored.
"""
import argparse
import csv
import json
import random
import re
import sys
from pathlib import Path

import openpyxl

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
RED, YELLOW = "FFC7CE", "FFEB9C"  # RGB only: openpyxl stores the alpha byte as 00
THOU = re.compile(r"^[+-]?\d{1,3}(,\d{3})+(\.\d+)?$")


def unit_norm(u):  # same rule as main.unit_norm (copied so scoring needs no API libraries)
    s = str(u or "").lower()
    for a, b in (("²", "2"), ("³", "3"), ("°", ""), ("º", ""), ("deg", "")):
        s = s.replace(a, b)
    return re.sub(r"[\s_()\[\].]", "", s)


def norm(v):
    s = " ".join(str(v if v is not None else "").split()).upper()
    return s.replace(",", "") if THOU.match(s) else s


def num(s):
    try:
        return float(s)
    except ValueError:
        return None


def equal(a, b, tol=0.0):
    if a == b:
        return True
    x, y = num(a), num(b)
    return x is not None and y is not None and abs(x - y) <= tol * max(abs(x), abs(y))


def ptype(parameter, unit):
    t = f"{parameter} {unit}".lower()
    for name, rx in (("pressure", r"press|bar|kpa|psi|mpa"), ("temperature", r"temp|°c|degc|\bk\b|°f"),
                     ("flow", r"flow|m3/h|kg/h|nm3|gpm|l/s|lb/h")):
        if re.search(rx, t):
            return name
    return "other/text"


def load_truth(path):
    files = {}
    for r in csv.DictReader(open(path, newline="", encoding="utf-8-sig")):
        files.setdefault(r["file"], []).append(r)
    return {f: rs for f, rs in files.items() if any(r["value"].strip() for r in rs)}


def load_tool(path, fname):
    """-> {(valve, section, parameter, case): (value, unit, flagged)} from the 'Datasheet Data' sheet."""
    out = {}
    ws = openpyxl.load_workbook(path)["Datasheet Data"]
    heads = []
    for c in ws[1][3:]:
        parts = (c.value or "").split("\n")
        unit = parts[-1].strip("()") if parts and parts[-1].startswith("(") else ""
        if unit:
            parts = parts[:-1]
        heads.append((parts[0] if len(parts) > 1 else "", parts[-1] if parts else "", unit))
    for row in ws.iter_rows(min_row=2):
        if str(row[0].value) != fname:
            continue
        for c, (sec, par, unit) in zip(row[3:], heads):
            if c.value in (None, ""):
                continue
            fill = c.fill.fgColor.rgb if c.fill and c.fill.fill_type else None
            out[(norm(row[1].value), norm(sec), norm(par), norm(row[2].value))] = (
                norm(c.value), unit, str(fill)[-6:] in (RED, YELLOW))
    return out


def score_file(truth_rows, tool):
    """Counts for one file; ratios are formed after (bootstrap) summation."""
    c = dict(gt=0, unreadable=0, ok=0, ok_tol=0, ok_readable=0, gt_readable=0, wrong=0, wrong_flagged=0,
             tool_cells=0, tool_matched=0, flagged=0, flagged_wrong=0, missing=0, spurious=0)
    by_type, gt_keys = {}, set()
    for r in truth_rows:
        val = norm(r["value"])
        if not val:
            continue
        key = (norm(r["valve_tag"]), norm(r["section"]), norm(r["parameter"]), norm(r["case"]))
        gt_keys.add(key)
        t = tool.get(key)
        unit_ok = not r["unit"].strip() or (t is not None and unit_norm(t[1]) == unit_norm(r["unit"]))
        ok = t is not None and unit_ok and equal(t[0], val)
        ok_tol = t is not None and unit_ok and equal(t[0], val, 0.005)
        flagged = bool(t and t[2])
        unread = val == "UNREADABLE"
        c["gt"] += 1
        c["unreadable"] += unread
        c["gt_readable"] += not unread
        c["ok"] += ok
        c["ok_tol"] += ok_tol
        c["ok_readable"] += ok and not unread
        c["wrong"] += not ok
        c["wrong_flagged"] += (not ok) and flagged
        c["missing"] += t is None
        pt = by_type.setdefault(ptype(r["parameter"], r["unit"]), [0, 0])
        pt[0] += 1
        pt[1] += ok
    for key, t in tool.items():
        c["tool_cells"] += 1
        if key in gt_keys:
            c["tool_matched"] += 1
        else:
            c["spurious"] += 1
    # flagged cells = tool cells carrying red/yellow; wrong ones = flagged cells that failed (or have no truth)
    truth_by = {(norm(r["valve_tag"]), norm(r["section"]), norm(r["parameter"]), norm(r["case"])): r
                for r in truth_rows if norm(r["value"])}
    for key, t in tool.items():
        if not t[2]:
            continue
        c["flagged"] += 1
        r = truth_by.get(key)
        good = r is not None and equal(t[0], norm(r["value"])) and (
            not r["unit"].strip() or unit_norm(t[1]) == unit_norm(r["unit"]))
        c["flagged_wrong"] += not good
    c["by_type"] = by_type
    return c


def ratios(c):
    d = lambda a, b: a / b if b else None
    return {"cell_accuracy": d(c["ok"], c["gt"]), "cell_accuracy_0.5pct": d(c["ok_tol"], c["gt"]),
            "accuracy_non_unreadable": d(c["ok_readable"], c["gt_readable"]),
            "unreadable_rate": d(c["unreadable"], c["gt"]),
            "extraction_recall": d(c["gt"] - c["missing"], c["gt"]),
            "extraction_precision": d(c["tool_matched"], c["tool_cells"]),
            "error_recall": d(c["wrong_flagged"], c["wrong"]),
            "flag_precision": d(c["flagged_wrong"], c["flagged"]),
            "silent_error_rate": d(c["wrong"] - c["wrong_flagged"], c["gt"])}


def total(cs):
    t = {k: sum(c[k] for c in cs) for k in cs[0] if k != "by_type"}
    t["by_type"] = {}
    for c in cs:
        for k, (n, ok) in c["by_type"].items():
            a = t["by_type"].setdefault(k, [0, 0])
            a[0] += n
            a[1] += ok
    return t


def bootstrap(cs, n=10000, seed=0):
    rng, samples = random.Random(seed), {}
    for _ in range(n):
        for k, v in ratios(total([rng.choice(cs) for _ in cs])).items():
            if v is not None:
                samples.setdefault(k, []).append(v)
    ci = {}
    for k, xs in samples.items():
        xs.sort()
        ci[k] = [xs[int(0.025 * len(xs))], xs[int(0.975 * len(xs)) - 1]]
    return ci


def score_dir(truth, tool_dir):
    per, cs = {}, []
    for f, rows in truth.items():
        p = Path(tool_dir) / f"{f}.xlsx"
        if not p.exists():
            print(f"  no workbook for {f} in {tool_dir}", file=sys.stderr)
            continue
        c = score_file(rows, load_tool(p, f))
        cs.append(c)
        per[f] = {**ratios(c), "counts": {k: v for k, v in c.items() if k != "by_type"}}
    if not cs:
        return {"n_files": 0}
    t = total(cs)
    return {"n_files": len(cs), "overall": ratios(t), "counts": {k: v for k, v in t.items() if k != "by_type"},
            "ci95_bootstrap_files": bootstrap(cs), "by_param_type": {k: {"cells": n, "accuracy": ok / n}
                                                                      for k, (n, ok) in t["by_type"].items()},
            "per_file": per}


def md(res):
    pc = lambda v: "n/a" if v is None else f"{100 * v:.1f}%"
    lines = ["# Scores (generated by eval/score.py)", ""]
    names = list(res)
    lines += ["| Metric | " + " | ".join(names) + " |", "|---|" + "---|" * len(names)]
    keys = ["cell_accuracy", "cell_accuracy_0.5pct", "accuracy_non_unreadable", "unreadable_rate", "extraction_recall",
            "extraction_precision", "error_recall", "flag_precision", "silent_error_rate"]
    for k in keys:
        cells = []
        for n in names:
            r = res[n]
            if not r.get("n_files"):
                cells.append("not measured")
                continue
            ci = r["ci95_bootstrap_files"].get(k)
            cells.append(pc(r["overall"][k]) + (f" [{pc(ci[0])}, {pc(ci[1])}]" if ci else ""))
        lines.append(f"| {k} | " + " | ".join(cells) + " |")
    lines.append("| files / GT cells | " + " | ".join(
        f"{r['n_files']} / {r['counts']['gt']}" if r.get("n_files") else "0" for r in res.values()) + " |")
    return "\n".join(lines) + "\n"


def main():
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--truth", default=here / "truth.csv")
    ap.add_argument("--runs", nargs="*", default=[f"p1={here.parent / 'results/p1'}", f"p2={here.parent / 'results/p2'}"])
    ap.add_argument("--out", default=here.parent / "results")
    a = ap.parse_args()
    if not Path(a.truth).exists():
        sys.exit(f"{a.truth} not found: key eval/truth_template.csv by hand and save it as truth.csv")
    truth = load_truth(a.truth)
    print(f"{len(truth)} keyed file(s)", file=sys.stderr)
    res = {n: score_dir(truth, d) for n, d in (r.split("=", 1) for r in a.runs)}
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "scores.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
    (out / "scores.md").write_text(md(res), encoding="utf-8")
    print(md(res))


if __name__ == "__main__":
    main()
