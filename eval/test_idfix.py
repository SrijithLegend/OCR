"""Offline checks for the id fixer (no Gemini call): python -m pytest eval/test_idfix.py -q"""
import csv, sqlite3, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import idfix, gen_bikes


def test_sample_dataset_30_percent_wrong_and_detected(tmp_path):
    db, tr = tmp_path / "b.db", tmp_path / "t.csv"
    n, w = gen_bikes.build(str(db), str(tr))
    assert round(w / n, 2) == 0.30
    res = idfix.check_table(str(db), web=False)
    assert (res["table"], res["mode"]) == ("parts", "check characters")
    truth = {int(r["rowid"]): r for r in csv.DictReader(open(tr))}
    assert res["bad"] == w and all(truth[f["rowid"]]["id_wrong"] == "1" for f in res["fixes"])
    for f in res["fixes"]:
        if f["how"] == "database":                            # the same part elsewhere: its true id and price
            assert f["new_id"] == truth[f["rowid"]]["true_id"] and f["new_price"] == float(truth[f["rowid"]]["true_price"])
        else:                                                 # not in the database: a new id with a valid check, price left for the web
            assert f["new_price"] is None and f["new_id"] != f["old_id"] and f["new_id"][-2:] == idfix.check(f["name"], f["attrs"])
    assert {f["how"] for f in res["fixes"]} >= {"database", "unresolved"}


def test_any_schema_without_check_characters(tmp_path):
    db = tmp_path / "other.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE products(sku TEXT, title TEXT, specs TEXT, cost REAL, stock INT)")
    rows = [("A-100", "Lamp", "colour: red", 10), ("A-100", "Lamp", "colour: red", 10), ("A-101", "Lamp", "colour: blue", 12),
            ("A-101", "Lamp", "colour: blue", 12), ("A-102", "Desk", "size: L", 90), ("A-102", "Desk", "size: L", 90),
            ("A-100", "Lamp", "colour: green", 99)]  # a green lamp wearing the red lamp's id and price
    c.executemany("INSERT INTO products VALUES(?,?,?,?,1)", rows); c.commit(); c.close()
    res = idfix.check_table(str(db), web=False)
    assert (res["table"], res["mode"], res["columns"]["id"]) == ("products", "duplicate ids", "sku")
    assert [(f["rowid"], f["new_id"]) for f in res["fixes"]] == [(7, "A-103")]   # new id continues the scheme
    idfix.apply(str(db), [{**res["fixes"][0], "new_price": 11.0}])
    assert sqlite3.connect(db).execute("SELECT sku, cost FROM products WHERE rowid=7").fetchone() == ("A-103", 11.0)


def test_no_catalogue_table(tmp_path):
    db = tmp_path / "x.db"
    sqlite3.connect(db).execute("CREATE TABLE t(a INT, b TEXT)").connection.commit()
    try:
        idfix.check_table(str(db)); assert False
    except ValueError as e:
        assert "No table" in str(e)


def test_web_reply_parsing_and_failure(monkeypatch):
    class Fake:
        def __init__(s, text): s.models = s; s.t = text
        def generate_content(s, **k):
            if s.t is None: raise RuntimeError("429 RESOURCE_EXHAUSTED")
            return type("R", (), {"text": s.t})
    parts = [("Seat", "d", "Colour: White"), ("Horn", "d", "Finish: Gold")]
    monkeypatch.setattr(idfix.nlsql, "_get_client", lambda: Fake("1|1,250.50|a shop\n2|none|none"))
    assert idfix.web_prices(parts) == [(1250.5, "a shop"), (None, "no price found on the web")]
    monkeypatch.setattr(idfix.nlsql, "_get_client", lambda: Fake(None))
    assert all(p is None and "quota" in s for p, s in idfix.web_prices(parts))
