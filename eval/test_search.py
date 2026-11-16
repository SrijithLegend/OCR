"""Accuracy of the plain-English database search (nlsql.py), checked against independent hand-written SQL.

Needs GENAI_API_KEY (skipped otherwise). Makes ~65 small API calls, paced to stay under the free-tier limit (~7 minutes).
Run: python -m pytest eval/test_search.py -q   (from the repo root)

Part 1 asks questions of shop.db. Part 2 builds a completely different database (a library) to show nothing is tied to
one schema. Where a question is honestly ambiguous (workspace ids vs workspace names) either answer is accepted."""
import os
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
import nlsql  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("GENAI_API_KEY"), reason="GENAI_API_KEY not set")
_gate, _last = threading.Lock(), [0.0]
GAP = 60.0 / float(os.environ.get("NLSQL_TEST_PER_MIN", "10"))  # the free Gemini tier allows ~15 requests/minute


def ask(q, db):
    """nlsql.ask, but calls start at least GAP seconds apart so the rate limit isn't hit."""
    with _gate:
        time.sleep(max(0.0, _last[0] + GAP - time.time()))
        _last[0] = time.time()
    return nlsql.ask(q, db)


SHOP = ROOT / "shop.db"
have_shop = pytest.mark.skipif(not SHOP.exists(), reason="shop.db not present")

DB = sqlite3.connect(SHOP) if SHOP.exists() else None
one = lambda sql, *a: DB.execute(sql, a).fetchone()[0]
allr = lambda sql, *a: DB.execute(sql, a).fetchall()
OFFICE = "171 - PT CONTROL SYSTEMS JAKARTA,INDONESIA"
ws = lambda where="1=1": (one(f"select count(distinct WorkspaceId) from test_data where {where}"), one(f"select count(distinct WorkspaceName) from test_data where {where}"))
rd = lambda x: round(x, 2) if isinstance(x, float) else x

SCALARS = [  # (question, [acceptable numbers])
    ("how many rows are there", [one("select count(*) from test_data")]),
    ("how many workspaces are there", list(ws())),
    ("how many different items are there", [one("select count(distinct ItemId) from test_data")]),
    ("how many offices are there", [one("select count(distinct OfficeId) from test_data")]),
    ("how many different users created workspaces", [one("select count(distinct WorkspaceCreatedBy) from test_data")]),
    ("exact number of worksapces created by dilla", list(ws("WorkspaceCreatedBy='dilla'"))),
    ("how many items week 33", [one("select count(distinct ItemId) from test_data where Week_Label='Week 33'")]),
    ("how many items in week 33 AP", [one("select count(distinct ItemId) from test_data where Week_Label='Week 33' and WorldArea='AP'")]),
    ("number of workspaces in EUR", list(ws("WorldArea='EUR'"))),
    ("how many workspaces in 2026", list(ws("ProdYear=2026"))),
    ("how many rows have construction type Primary", [one("select count(*) from test_data where ConstructionType='Primary'")]),
    ("how many items in workspace 3344", [one("select count(distinct ItemId) from test_data where WorkspaceId=3344")]),
    ("how many items in pertamina pa", [one("select count(distinct ItemId) from test_data where WorkspaceName='PERTAMINA PA'")]),
    ("how many rows have price above 10000", [one("select count(*) from test_data where ConstructionPrice>10000")]),
    ("number of unique values in officeID column", [one("select count(distinct OfficeId) from test_data")]),
    ("number of unique OfficeId in week 33", [one("select count(distinct OfficeId) from test_data where Week_Label='Week 33'")]),
    ("how many distinct WorldArea values", [one("select count(distinct WorldArea) from test_data")]),
    (f"how many {OFFICE} are there in officeid column", [one("select count(*) from test_data where OfficeId=?", OFFICE)]),
    (f"how many unique workspaces in {OFFICE}", list(ws(f"OfficeId='{OFFICE}'"))),
    ("how many AP in worldarea column", [one("select count(*) from test_data where WorldArea='AP'")]),
    ("total price", [rd(one("select sum(ConstructionPrice) from test_data"))]),
    ("total price in AP", [rd(one("select sum(ConstructionPrice) from test_data where WorldArea='AP'"))]),
    ("average price", [rd(one("select avg(ConstructionPrice) from test_data"))]),
    ("average price of Primary constructions", [rd(one("select avg(ConstructionPrice) from test_data where ConstructionType='Primary'"))]),
    ("highest price", [rd(one("select max(ConstructionPrice) from test_data"))]),
    ("lowest price in EUR", [rd(one("select min(ConstructionPrice) from test_data where WorldArea='EUR'"))]),
    ("total price of workspaces created by dilla", [rd(one("select sum(ConstructionPrice) from test_data where WorkspaceCreatedBy='dilla'"))]),
]

GROUPS = [  # (question, [acceptable {group value: number}])
    ("how many workspaces per world area", [dict(allr("select WorldArea, count(distinct WorkspaceId) from test_data group by 1")), dict(allr("select WorldArea, count(distinct WorkspaceName) from test_data group by 1"))]),
    ("how many items per week", [dict(allr("select Week_Label, count(distinct ItemId) from test_data group by 1"))]),
    ("how many rows per construction type", [dict(allr("select ConstructionType, count(*) from test_data group by 1"))]),
    ("number of unique OfficeId per area", [dict(allr("select WorldArea, count(distinct OfficeId) from test_data group by 1"))]),
    ("total price per world area", [dict(allr("select WorldArea, sum(ConstructionPrice) from test_data group by 1"))]),
    ("average price by construction type", [dict(allr("select ConstructionType, avg(ConstructionPrice) from test_data group by 1"))]),
    ("highest price per area", [dict(allr("select WorldArea, max(ConstructionPrice) from test_data group by 1"))]),
    ("how many items by world area in week 33", [dict(allr("select WorldArea, count(distinct ItemId) from test_data where Week_Label='Week 33' group by 1"))]),
    ("how many workspaces did each user create", [dict(allr("select WorkspaceCreatedBy, count(distinct WorkspaceId) from test_data group by 1")), dict(allr("select WorkspaceCreatedBy, count(distinct WorkspaceName) from test_data group by 1"))]),
]

WINNERS = [  # (question, [(group value, number)] acceptable)
    ("which world area has the most workspaces", [(a, b) for a, b in [allr("select WorldArea, count(distinct WorkspaceId) from test_data where WorldArea is not null group by 1 order by 2 desc limit 1")[0], allr("select WorldArea, count(distinct WorkspaceName) from test_data where WorldArea is not null group by 1 order by 2 desc limit 1")[0]]]),
    ("which week has the most items", [tuple(allr("select Week_Label, count(distinct ItemId) from test_data group by 1 order by 2 desc limit 1")[0]), tuple(allr("select Week_Label, count(ItemId) from test_data group by 1 order by 2 desc limit 1")[0])]),
    ("which user created the most workspaces", [tuple(allr("select WorkspaceCreatedBy, count(distinct WorkspaceId) from test_data where WorkspaceCreatedBy is not null group by 1 order by 2 desc limit 1")[0]), tuple(allr("select WorkspaceCreatedBy, count(distinct WorkspaceName) from test_data where WorkspaceCreatedBy is not null group by 1 order by 2 desc limit 1")[0])]),
    ("which world area has the fewest workspaces", [tuple(allr("select WorldArea, count(distinct WorkspaceId) from test_data where WorldArea is not null group by 1 order by 2 asc limit 1")[0]), tuple(allr("select WorldArea, count(distinct WorkspaceName) from test_data where WorldArea is not null group by 1 order by 2 asc limit 1")[0])]),
]


@pytest.fixture(scope="session")
def answers():
    """Ask every shop.db question once, concurrently."""
    qs = [s[0] for s in SCALARS] + [g[0] for g in GROUPS] + [w[0] for w in WINNERS] + ["list the unique officeID values", "highest price item"]
    with ThreadPoolExecutor(6) as ex:
        return dict(zip(qs, ex.map(lambda q: ask(q, str(SHOP)), qs)))


def num(r):
    assert r["kind"] == "scalar", (r["kind"], r["text"], r["sql"])
    return rd(r["rows"][0][0])


@have_shop
@pytest.mark.parametrize("q,acc", SCALARS, ids=[s[0] for s in SCALARS])
def test_scalar(answers, q, acc):
    got = num(answers[q])
    assert any(abs(got - a) <= 0.011 for a in acc), (got, acc, answers[q]["sql"])


@have_shop
@pytest.mark.parametrize("q,acc", GROUPS, ids=[g[0] for g in GROUPS])
def test_group(answers, q, acc):
    r = answers[q]
    assert r["kind"] == "table", (r["kind"], r["text"], r["sql"])
    got = {row[0]: rd(row[-1]) for row in r["rows"] if row[0] is not None}
    assert any(got == {k: rd(v) for k, v in a.items() if k is not None and v is not None} for a in acc), (got, r["sql"])


@have_shop
@pytest.mark.parametrize("q,acc", WINNERS, ids=[w[0] for w in WINNERS])
def test_winner(answers, q, acc):
    r = answers[q]
    assert r["rows"], r["text"]
    top = r["rows"][0]
    assert any(v in top and n in top for v, n in acc), (top, acc, r["sql"])


@have_shop
def test_distinct_list_is_complete(answers):
    r = answers["list the unique officeID values"]
    assert r["kind"] == "table" and len(r["rows"]) == one("select count(distinct OfficeId) from test_data"), r["sql"]


@have_shop
def test_highest_price_item(answers):
    r = answers["highest price item"]
    assert round(max(c for row in r["rows"] for c in row if isinstance(c, float)), 2) == rd(one("select max(ConstructionPrice) from test_data")), r["sql"]


# ---- Part 2: a different database, to prove nothing is tied to shop.db -------------------------------------------
@pytest.fixture(scope="session")
def library(tmp_path_factory):
    p = tmp_path_factory.mktemp("lib") / "library.sqlite"
    c = sqlite3.connect(p)
    c.executescript("""
        CREATE TABLE authors (author_id INTEGER PRIMARY KEY, name TEXT, country TEXT);
        CREATE TABLE books (book_id INTEGER PRIMARY KEY, title TEXT, author_id INTEGER REFERENCES authors(author_id), genre TEXT, year INTEGER, pages INTEGER);
        CREATE TABLE loans (loan_id INTEGER PRIMARY KEY, book_id INTEGER REFERENCES books(book_id), member TEXT, loaned_on TEXT, returned INTEGER);
        INSERT INTO authors VALUES (1,'Ursula Le Guin','USA'),(2,'Stanislaw Lem','Poland'),(3,'Octavia Butler','USA'),(4,'Haruki Murakami','Japan');
        INSERT INTO books VALUES (1,'The Dispossessed',1,'scifi',1974,387),(2,'Solaris',2,'scifi',1961,204),(3,'Kindred',3,'scifi',1979,264),
          (4,'Norwegian Wood',4,'novel',1987,296),(5,'Kafka on the Shore',4,'novel',2002,505),(6,'A Wizard of Earthsea',1,'fantasy',1968,183),(7,'Parable of the Sower',3,'scifi',1993,345);
        INSERT INTO loans VALUES (1,1,'ana','2025-01-03',1),(2,1,'ben','2025-02-10',1),(3,2,'ana','2025-02-11',0),(4,4,'cho','2025-03-01',1),
          (5,5,'cho','2025-03-02',0),(6,5,'ana','2025-03-09',1),(7,7,'dev','2025-04-01',1),(8,3,'ben','2025-04-04',0),(9,5,'ben','2025-04-20',1);
    """)
    c.commit(); c.close()
    return str(p)


LIBRARY = [  # (question, check on the result rows)
    ("how many books are there", lambda r: r["rows"][0][0] == 7),
    ("how many books were written by Octavia Butler", lambda r: r["rows"][0][0] == 2),
    ("which author has written the most pages in total", lambda r: "Murakami" in str(r["rows"][0])),
    ("how many loans per member", lambda r: {row[0]: row[-1] for row in r["rows"]} == {"ana": 3, "ben": 3, "cho": 2, "dev": 1}),
    ("average number of pages of scifi books", lambda r: abs(r["rows"][0][0] - (387 + 204 + 264 + 345) / 4) < 0.01),
    ("how many different countries are the authors from", lambda r: r["rows"][0][0] == 3),
    ("how many loans are still not returned", lambda r: r["rows"][0][0] == 3),
    ("which book was loaned the most times", lambda r: "Kafka on the Shore" in str(r["rows"][0])),
]


@pytest.mark.parametrize("q,check", LIBRARY, ids=[x[0] for x in LIBRARY])
def test_other_database(library, q, check):
    r = ask(q, library)
    assert r["kind"] in ("scalar", "table"), (r["kind"], r["text"], r["sql"])
    assert check(r), (r["rows"], r["sql"])


def test_unanswerable_is_reported_not_invented(library):
    r = ask("what is the total revenue from book sales", library)
    assert r["kind"] in ("empty", "error") or not r["rows"], r


def test_writes_are_impossible(library):
    for bad in ("DELETE FROM books", "DROP TABLE loans", "UPDATE books SET pages=0", "SELECT 1; DROP TABLE books"):
        with pytest.raises((ValueError, sqlite3.Error)):
            nlsql.run_sql(library, bad)
    assert sqlite3.connect(library).execute("select count(*) from books").fetchone()[0] == 7
