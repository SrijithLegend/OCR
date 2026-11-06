"""Score model-output.csv against truth.csv (both exported from the UI).

    python score.py truth.csv model-output.csv

Reports accuracy, SILENT errors (wrong or missing and not flagged - the number that costs money),
flag recall / precision, and accuracy of the unflagged cells.
"""
import csv
import sys
from collections import defaultdict

from app.textutil import norm, same_value, unit_norm


def load(path):
    out = defaultdict(list)
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            out[(norm(r["valve"]), norm(r["parameter"]), r["case"])].append(r)
    return out


def main(truth_path, model_path):
    truth, model = load(truth_path), load(model_path)
    n = ok = silent = flagged = flagged_wrong = 0
    unflagged = unflagged_ok = 0
    for key, rows in truth.items():
        t = rows[0]
        m = (model.get(key) or [None])[0]
        right = m is not None and same_value(m["value"] or None, t["value"] or None) \
            and unit_norm(m["unit"]) == unit_norm(t["unit"])
        flag = bool(m and m.get("flag"))
        n += 1
        ok += right
        flagged += flag
        flagged_wrong += flag and not right
        if not flag:
            unflagged += 1
            unflagged_ok += right
            silent += not right
    wrong = n - ok
    print(f"cells in truth      : {n}")
    print(f"accuracy            : {ok / n:.1%}" if n else "no truth cells")
    print(f"SILENT errors       : {silent}   (target: 0)")
    print(f"flag recall         : {flagged_wrong / wrong:.1%}" if wrong else "flag recall         : n/a (no errors)")
    print(f"flag precision      : {flagged_wrong / flagged:.1%}" if flagged else "flag precision      : n/a (nothing flagged)")
    print(f"unflagged accuracy  : {unflagged_ok / unflagged:.1%}" if unflagged else "unflagged accuracy  : n/a")
    extra = [k for k in model if k not in truth]
    print(f"model cells not in truth (unreviewed): {len(extra)}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
