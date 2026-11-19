"""Offline checks for what-if estimates (model call stubbed): python -m pytest eval/test_estimate.py -q"""
import sqlite3, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import estimate


def db(tmp_path):
    p = tmp_path / "e.db"
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE parts(part_id TEXT, part_name TEXT, part_description TEXT, part_attribute TEXT, part_price_india REAL, part_price_china REAL)")
    c.executemany("INSERT INTO parts VALUES(?,?,?,?,?,?)", [
        ("S1", "Seat Assembly", "Seat", "Colour: Black", 3200, 2000), ("S2", "Seat Assembly", "Seat", "Colour: Brown", 3500, 2200),
        ("S3", "Seat Assembly", "Seat", "Colour: Yellow", 3700, 2400), ("G1", "Handlebar Grip Set", "Pair of grips", "Colour: Black", 360, 250),
        ("G2", "Handlebar Grip Set", "Pair of grips", "Colour: Brown", 400, 280), ("G2", "Handlebar Grip Set", "Pair of grips", "Colour: Brown", 400, 280)])
    c.commit(); c.close()
    return str(p)


def parsed(value, cols=("part_price_india",)):
    return lambda *a, **k: {"is_what_if": True, "part": "grips", "attribute_key": "Colour", "value": value, "price_columns": list(cols)}


def test_ratio_estimate(tmp_path, monkeypatch):
    monkeypatch.setattr(estimate.nlsql, "_model", parsed("Yellow"))
    r = estimate.what_if("prices of grips if there were yellow in india", db(tmp_path))
    assert [x[-1] for x in r["rows"]] == ["Stored", "Stored", "Estimate"]          # black, brown, then the estimate
    assert r["rows"][2][1] == "Colour: Yellow" and r["rows"][2][2] == round((360 * 3700 / 3200 + 400 * 3700 / 3500) / 2)   # 420
    assert r["estimated"] and "Yellow" in r["basis"][0]


def test_both_countries_and_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(estimate.nlsql, "_model", parsed("Pink", ()))
    r = estimate.what_if("grips in pink", db(tmp_path))
    assert r["rows"][-1][2:4] == [380.0, 265.0] and "rough" in r["basis"][0]       # nobody has pink: the average of the existing variants


def test_existing_or_not_a_what_if(tmp_path, monkeypatch):
    path = db(tmp_path)
    monkeypatch.setattr(estimate.nlsql, "_model", parsed("Black"))
    assert estimate.what_if("grips in black", path) is None                # it exists: the normal answer covers it
    monkeypatch.setattr(estimate.nlsql, "_model", lambda *a, **k: {"is_what_if": False, "part": "", "attribute_key": "", "value": "", "price_columns": []})
    assert estimate.what_if("how many rows", path) is None


def market(part="hand brake", mk="Korea"):
    return lambda *a, **k: {"is_what_if": True, "part": part, "attribute_key": "", "value": "", "price_columns": [], "market": mk}


def test_web_price_for_another_market(tmp_path, monkeypatch):
    path, seen = db(tmp_path), {}
    monkeypatch.setattr(estimate.nlsql, "_model", market("grips"))
    monkeypatch.setattr(estimate.idfix, "web_prices", lambda parts, context="", market="": seen.update(parts=parts, market=market) or [(15000.0, "KRW, coupang.com")])
    r = estimate.what_if("how much may grips cost in korea", path)
    assert seen["market"] == "Korea" and seen["parts"][0][:2] == ("Handlebar Grip Set", "Pair of grips")
    assert r["columns"][-3:] == ["price_in_korea", "currency", "Source"] and r["estimated"]
    assert r["rows"][-1][-3:] == [15000.0, "KRW", "Estimate"] and [x[-1] for x in r["rows"]][:2] == ["Stored", "Stored"]


def test_web_price_not_in_database_and_failure(tmp_path, monkeypatch):
    path = db(tmp_path)
    monkeypatch.setattr(estimate.nlsql, "_model", market("spark plug"))
    monkeypatch.setattr(estimate.idfix, "web_prices", lambda parts, context="", market="": [(9.5, "USD, amazon.com")])
    r = estimate.what_if("how much may a spark plug cost in the us", path)
    assert [x[-1] for x in r["rows"]] == ["Estimate"] and r["rows"][0][0] == "spark plug" and r["rows"][0][-2] == "USD"
    monkeypatch.setattr(estimate.idfix, "web_prices", lambda parts, context="", market="": [(None, "Google Search quota exhausted")])
    r = estimate.what_if("how much may a spark plug cost in the us", path)
    assert r["kind"] == "empty" and "quota exhausted" in r["text"]
