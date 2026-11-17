"""Offline checks for the parts-id fixer (no Gemini call): python -m pytest eval/test_bikefix.py -q"""
import csv, sqlite3, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bikefix, gen_bikes
from bikeparts import id_ok

def test_dataset_30_percent_wrong_and_detected(tmp_path):
    db, tr = tmp_path / "b.db", tmp_path / "t.csv"
    n, w = gen_bikes.build(str(db), str(tr))
    assert round(w / n, 2) == 0.30
    res = bikefix.check(str(db), web=False)
    truth = {int(r["rowid"]): r for r in csv.DictReader(open(tr))}
    assert res["bad"] == w                                   # exactly the wrong rows are flagged
    assert all(truth[f["rowid"]]["id_wrong"] == "1" for f in res["fixes"])
    for f in res["fixes"]:                                    # a database hit gives the true id and price
        if f["how"] == "database":
            assert f["new_id"] == truth[f["rowid"]]["true_id"] and f["new_price"] == float(truth[f["rowid"]]["true_price"])
        else:                                                 # not in the database: new id, price left for the web step
            assert f["new_price"] is None and f["new_id"] != f["old_id"]
            assert id_ok(f["new_id"], f["name"], f["attrs"])
    assert any(f["how"] == "database" for f in res["fixes"]) and any(f["how"] != "database" for f in res["fixes"])
