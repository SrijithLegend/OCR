"""Every question here is answered by search.py and checked against an independent hand-written SQL query on shop.db.
Run: python -m pytest eval/test_search.py -q   (from the repo root)"""
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import os
os.chdir(ROOT)
import search  # noqa: E402

DB = sqlite3.connect(ROOT / "shop.db")
one = lambda sql, *a: DB.execute(sql, a).fetchone()[0]
allr = lambda sql, *a: DB.execute(sql, a).fetchall()
first_num = lambda r: float(r["text"].split()[0])
as_dict = lambda r: {row[0]: row[-1] for row in r["rows"]}  # first column -> last column (group value)

SCALARS = [  # (question, expected number)
    ("how many rows", one("select count(*) from test_data")),
    ("how many workspaces", one("select count(distinct WorkspaceId) from test_data")),
    ("how many items", one("select count(distinct ItemId) from test_data")),
    ("how many offices", one("select count(distinct OfficeId) from test_data")),
    ("how many users", one("select count(distinct WorkspaceCreatedBy) from test_data")),
    ("how many constructions", one("select count(distinct ConstructionId) from test_data")),
    ("exact number of worksapces created by dilla", one("select count(distinct WorkspaceId) from test_data where WorkspaceCreatedBy='dilla'")),
    ("how many items week 33", one("select count(distinct ItemId) from test_data where Week_Label='Week 33'")),
    ("how many items in week 33 AP", one("select count(distinct ItemId) from test_data where Week_Label='Week 33' and WorldArea='AP'")),
    ("number of workspaces in EUR", one("select count(distinct WorkspaceId) from test_data where WorldArea='EUR'")),
    ("how many workspaces in 2026", one("select count(distinct WorkspaceId) from test_data where ProdYear=2026")),
    ("how many primary constructions", one("select count(distinct ConstructionId) from test_data where ConstructionType='Primary'")),
    ("how many alternate constructions in AP", one("select count(distinct ConstructionId) from test_data where ConstructionType='Alternate' and WorldArea='AP'")),
    ("how many items in workspace 3344", one("select count(distinct ItemId) from test_data where WorkspaceId=3344")),
    ("how many items in pertamina pa", one("select count(distinct ItemId) from test_data where WorkspaceName='PERTAMINA PA'")),
    ("how many items in office 171", one("select count(distinct ItemId) from test_data where Office=171")),
    ("how many rows have price above 10000", one("select count(*) from test_data where ConstructionPrice>10000")),
    ("how many items with price below 100", one("select count(distinct ItemId) from test_data where ConstructionPrice<100")),
    ("number of unique values in officeID column", one("select count(distinct OfficeId) from test_data")),
    ("number of unique OfficeId week 33", one("select count(distinct OfficeId) from test_data where Week_Label='Week 33'")),
    ("how many distinct WorldArea", one("select count(distinct WorldArea) from test_data")),
    ("how many unique workspaces", one("select count(distinct WorkspaceId) from test_data")),
    ("how many unique items week 33", one("select count(distinct ItemId) from test_data where Week_Label='Week 33'")),
    ("total price", one("select round(sum(ConstructionPrice),2) from test_data")),
    ("total price in AP", one("select round(sum(ConstructionPrice),2) from test_data where WorldArea='AP'")),
    ("average price", one("select round(avg(ConstructionPrice),2) from test_data")),
    ("average price of primary constructions", one("select round(avg(ConstructionPrice),2) from test_data where ConstructionType='Primary'")),
    ("highest price", one("select round(max(ConstructionPrice),2) from test_data")),
    ("lowest price in EUR", one("select round(min(ConstructionPrice),2) from test_data where WorldArea='EUR'")),
    ("total price created by dilla", one("select round(sum(ConstructionPrice),2) from test_data where WorkspaceCreatedBy='dilla'")),
    ("sum of price week 30", one("select round(sum(ConstructionPrice),2) from test_data where Week_Label='Week 30'")),
]

GROUPS = [  # (question, {group value: expected number})
    ("total workspace names per world area", dict(allr("select WorldArea, count(distinct WorkspaceId) from test_data group by 1"))),
    ("items per week", dict(allr("select Week_Label, count(distinct ItemId) from test_data group by 1"))),
    ("how many workspaces by world area", dict(allr("select WorldArea, count(distinct WorkspaceId) from test_data group by 1"))),
    ("rows per construction type", dict(allr("select ConstructionType, count(*) from test_data group by 1"))),
    ("number of unique OfficeId per area", dict(allr("select WorldArea, count(distinct OfficeId) from test_data group by 1"))),
    ("total price per world area", dict(allr("select WorldArea, round(sum(ConstructionPrice),2) from test_data group by 1"))),
    ("average price by construction type", dict(allr("select ConstructionType, round(avg(ConstructionPrice),2) from test_data group by 1"))),
    ("highest price per area", dict(allr("select WorldArea, round(max(ConstructionPrice),2) from test_data group by 1"))),
    ("how many items by world area week 33", dict(allr("select WorldArea, count(distinct ItemId) from test_data where Week_Label='Week 33' group by 1"))),
    ("workspaces per user", dict(allr("select WorkspaceCreatedBy, count(distinct WorkspaceId) from test_data group by 1"))),
]

WINNERS = [  # (question, expected top group value, expected number)
    ("which area has the most workspaces", *allr("select WorldArea, count(distinct WorkspaceId) c from test_data where WorldArea is not null group by 1 order by 2 desc limit 1")[0]),
    ("which week has the most items", *allr("select Week_Label, count(distinct ItemId) from test_data group by 1 order by 2 desc limit 1")[0]),
    ("which user created the most workspaces", *allr("select WorkspaceCreatedBy, count(distinct WorkspaceId) from test_data where WorkspaceCreatedBy is not null group by 1 order by 2 desc limit 1")[0]),
    ("which area has the fewest workspaces", *allr("select WorldArea, count(distinct WorkspaceId) from test_data where WorldArea is not null group by 1 order by 2 asc limit 1")[0]),
]


@pytest.mark.parametrize("q,expected", SCALARS, ids=[s[0] for s in SCALARS])
def test_scalar(q, expected):
    r = search.search(q)
    assert r["kind"] == "count", r["text"]
    assert first_num(r) == pytest.approx(expected, abs=0.011), r["text"]


@pytest.mark.parametrize("q,expected", GROUPS, ids=[g[0] for g in GROUPS])
def test_group(q, expected):
    r = search.search(q)
    assert r["kind"] == "group", r["text"]
    got = {k: (round(v, 2) if isinstance(v, float) else v) for k, v in as_dict(r).items()}
    exp = {k: (round(v, 2) if isinstance(v, float) else v) for k, v in expected.items() if v is not None}
    assert {k: v for k, v in got.items() if v is not None} == exp, (q, r["text"])


@pytest.mark.parametrize("q,value,n", WINNERS, ids=[w[0] for w in WINNERS])
def test_winner(q, value, n):
    r = search.search(q)
    assert r["text"].startswith(f"{value} has the"), r["text"]
    assert f": {n} " in r["text"], r["text"]


def test_distinct_list_is_complete():
    r = search.search("list unique officeID")
    assert len(r["rows"]) == one("select count(distinct OfficeId) from test_data")
    assert sum(row[-1] for row in r["rows"]) == one("select count(*) from test_data")


def test_exact_id_lookup():
    r = search.search("1B5993X0042")
    assert r["kind"] == "exact" and len(r["rows"]) == one("select count(*) from test_data where upper(ItemName)='1B5993X0042'")


def test_highest_price_rows_are_the_top_ones():
    r = search.search("highest price")
    assert [row[4] for row in r["rows"]] == [x[0] for x in allr("select ConstructionPrice from test_data where ConstructionPrice is not null order by 1 desc limit 5")]
