"""Plain-English questions over shop.db. CLI: python search.py [query]   UI: web.py POST /search.

Answered exactly with SQL (never from a sample): counts, distinct counts/lists, sum/avg/min/max of a measure,
group-by ("per area", "by week"), "which X has the most Y", filters (week, area, type, revision, year, creator,
workspace name/id, office, price range) and exact ID lookup. Anything else falls back to semantic (embedding) search."""
import re, sqlite3, sys, threading

COLS = ["WorkspaceName", "ItemName", "ConstructionType", "WorldArea", "ConstructionPrice", "Week_Label"]
_lock, _model, _col = threading.Lock(), None, None

# (regex, label, id column, extra column shown beside the id when listing)
ENTITIES = [
    (r"\bwor\w*ces?\b", "Workspaces", "WorkspaceId", "WorkspaceName"),  # tolerates typos like 'worksapces'
    (r"\bitems?\b", "Items", "ItemId", "ItemName"),
    (r"\boffices?\b", "Offices", "OfficeId", None),
    (r"\b(?:users?|creators?|authors?)\b", "Users", "WorkspaceCreatedBy", None),
    (r"\bconstructions?\b", "Constructions", "ConstructionId", None),
]
ENTITY_OF_COL = {"WorkspaceName": 0, "WorkspaceId": 0, "ItemName": 1, "ItemId": 1, "OfficeId": 2, "Office": 2,
                 "WorkspaceCreatedBy": 3, "ConstructionId": 4}
ALIAS = {"WorldArea": r"areas?|regions?", "Week_Label": r"weeks?", "WorkspaceCreatedBy": r"users?|creators?|authors?",
         "WorkspaceName": r"workspaces?", "ItemName": r"items?", "OfficeId": r"offices?",
         "ConstructionType": r"types?", "HighestRevision": r"revisions?|revs?"}
GROUPBY = {"WorkspaceName": ["WorkspaceId", "WorkspaceName"], "WorkspaceId": ["WorkspaceId", "WorkspaceName"],
           "ItemName": ["ItemId", "ItemName"], "ItemId": ["ItemId", "ItemName"]}
NUMERIC = ["TotalConstructions", "NumOfCompleteCons", "NumOfInCompleteCons", "NumOfAlternates",
           "NumOfConsHavingSpecialConfig", "NumOfConsHavingSpecialPrice", "NumOfTags", "NumOfParts"]
CATEGORY = ["WorldArea", "ConstructionType", "HighestRevision", "ProdYear"]  # small value sets, matched by the value itself
PRICE = r"\b(?:prices?|pricing|costs?|revenue|worth)\b"
DISTINCT = r"\b(?:unique|distinct|different)\b"
COUNT = r"\b(?:how many|number of|count|total|sum)\b"
AGG = r"\b(?:sum|total|overall|average|avg|mean|max|maximum|highest|largest|biggest|most expensive|min|minimum|lowest|smallest|cheapest)\b"
HI, LO = r"\b(?:most|highest|largest|biggest|greatest|top)\b", r"\b(?:least|lowest|fewest|smallest|bottom)\b"


def _vectors():  # heavy imports + model load only on first semantic query
    global _model, _col
    with _lock:
        if _model is None:
            from sentence_transformers import SentenceTransformer
            import chromadb
            _model = SentenceTransformer("all-MiniLM-L6-v2")        # MUST match build_index.py
            _col = chromadb.PersistentClient(path="./vector_store").get_collection("test_data")
    return _model, _col


def _phrase(c):
    """Regex for a column as people write it: WorldArea -> world[\\s_]*areas? (+ aliases like 'area')."""
    words = [w for w in re.split(r"_|(?<=[a-z0-9])(?=[A-Z])", c) if w]
    core = r"[\s_]*".join(re.escape(w.lower()) for w in words) + "s?"
    return f"(?:{core}|{ALIAS[c]})" if c in ALIAS else core


def _cut(query, m):
    return query[:m.start()] + " " + query[m.end():]


def search(query):
    """-> {"kind": "count"|"group"|"exact"|"semantic", "text": str, "columns": [...], "rows": [[...]]}"""
    conn = sqlite3.connect("shop.db")
    conn.row_factory = sqlite3.Row
    try:
        return _search(conn, query)
    finally:
        conn.close()


def _search(conn, query):
    schema = [r[1] for r in conn.execute("PRAGMA table_info(test_data)") if r[1] != "row_id"]
    out = lambda kind, text, rows=(): {"kind": kind, "text": text, "columns": COLS, "rows": [[r[c] for c in COLS] for r in rows]}
    table = lambda kind, text, cols, rows: {"kind": kind, "text": text, "columns": cols, "rows": [list(r) for r in rows]}
    asks = re.search(COUNT + "|" + DISTINCT, query, re.I)
    orig = query

    # 1) Exact ID lookup: a token equal to an ItemName / ItemId / WorkspaceId wins outright (unless it's an analytic question).
    tokens = [t for t in query.replace(",", " ").split() if any(ch.isdigit() for ch in t)]  # IDs contain a digit; plain words don't count
    if tokens and not asks and not re.search(r"\b(?:per|each|average|avg|mean|max|maximum|min|minimum)\b|" + HI + "|" + LO, query, re.I):
        m = ",".join("?" * len(tokens))
        exact = conn.execute(f"SELECT * FROM test_data WHERE upper(ItemName) IN ({m}) OR CAST(ItemId AS TEXT) IN ({m}) "
                             f"OR CAST(WorkspaceId AS TEXT) IN ({m}) LIMIT 50",
                             [t.upper() for t in tokens] + tokens + tokens).fetchall()
        if exact:
            return out("exact", f"Exact match: {len(exact)} row(s)" + (" (showing first 50)" if len(exact) == 50 else ""), exact)

    # 2) "per / each / by / which <column>" = group the answer by that column. The longest column phrase wins.
    group, best = None, None
    for c in schema:
        if c == "Office":
            continue
        for g in re.finditer(r"\b(?:per|each|by|which|what|group(?:ed)?\s+by)\s+(?:the\s+)?" + _phrase(c) + r"\b", query, re.I):
            if best is None or len(g.group(0)) > len(best.group(0)):
                group, best = c, g
    if best:
        query = _cut(query, best)
    gcols = GROUPBY.get(group, [group]) if group else None

    # 3) Filters spoken in the query are hard conditions, not left to embeddings.
    where = []  # (column, operator, value)
    if (w := re.search(r"\bweek\s*(\d+)\b", query, re.I)):
        where.append(("Week_Label", "=", f"Week {int(w.group(1))}")); query = _cut(query, w)
    if (w := re.search(r"\bwork\w*space\s*(?:id)?\s*#?(\d+)\b", query, re.I)):
        where.append(("WorkspaceId", "=", int(w.group(1)))); query = _cut(query, w)
    if (w := re.search(r"\boffice\s*(?:id)?\s*#?(\d+)\b", query, re.I)):
        where.append(("Office", "=", int(w.group(1)))); query = _cut(query, w)
    for c in CATEGORY:  # whole-word value match: "AP", "Primary", "Rev A", 2026 (short values are case-sensitive)
        for (v,) in conn.execute(f"SELECT DISTINCT {c} FROM test_data WHERE {c} IS NOT NULL").fetchall():
            fl = 0 if len(str(v)) <= 3 else re.I
            if (w := re.search(rf"\b{re.escape(str(v))}\b", query, fl)):
                where.append((c, "=", v)); query = _cut(query, w)
                break
    if (u := re.search(r"\b(?:created by|from user|by)\s+(\w+)", query, re.I)) and conn.execute(
            "SELECT 1 FROM test_data WHERE WorkspaceCreatedBy = ? LIMIT 1", (u.group(1).lower(),)).fetchone():
        where.append(("WorkspaceCreatedBy", "=", u.group(1).lower())); query = _cut(query, u)
    names = sorted((r[0] for r in conn.execute("SELECT DISTINCT WorkspaceName FROM test_data")), key=len, reverse=True)
    for n in names:  # "items in pertamina pa": a known workspace name (5+ chars) is a filter
        if len(n.strip()) >= 5 and (w := re.search(rf"(?<!\w){re.escape(n.strip())}(?!\w)", query, re.I)):
            where.append(("WorkspaceName", "=", n)); query = _cut(query, w)
            break
    if re.search(PRICE, query, re.I) and (p := re.search(
            r"\b(above|over|greater than|more than|higher than|exceeding|below|under|less than|lower than|cheaper than)\s*\$?(\d[\d,]*\.?\d*)", query, re.I)):
        where.append(("ConstructionPrice", ">" if p.group(1).lower() in ("above", "over", "greater than", "more than", "higher than", "exceeding") else "<",
                      float(p.group(2).replace(",", "")))); query = _cut(query, p)
    cond = " AND ".join(f"{c} {op} ?" for c, op, _ in where) or "1=1"
    args = [v for _, _, v in where]
    tail = f"  ({', '.join(f'{c} {op} {v}' for c, op, v in where)})" if where else ""

    # 4) What to compute: a measure (sum/avg/min/max), else an entity / named column (count or distinct), else rows.
    distinct = re.search(DISTINCT, query, re.I)
    squashed = re.sub(r"[\s_]+", "", query.lower())
    named = [c for c in schema if c.lower().replace("_", "") in squashed]
    named_c = max(named, key=len) if named else None  # 'OfficeId' over 'Office'
    ent = min(((m.start(), e) for e in ENTITIES if (m := re.search(e[0], query, re.I))), default=(0, None))[1]
    agg = re.search(AGG, query, re.I)
    measure = next((c for c in NUMERIC if c in named and not distinct and agg), None) or (
        "ConstructionPrice" if re.search(PRICE, query, re.I) or "ConstructionPrice" in named or re.search("expensive|cheap", query, re.I) else None)
    if measure and re.search(r"\b(?:how many|number of|count)\b", orig, re.I) and not re.search(
            r"\b(?:average|avg|mean|max|maximum|highest|largest|biggest|most expensive|min|minimum|lowest|smallest|cheapest)\b", orig, re.I):
        measure = None  # "how many rows have price above 10000" counts rows; price is only a filter
    if not (asks or group or (measure and agg)):
        return _semantic(conn, query, where, out)

    fn = None
    if measure:
        fn = ("AVG" if re.search(r"\b(?:average|avg|mean)\b", query, re.I) else "SUM" if re.search(r"\b(?:sum|total|overall)\b", query, re.I)
              else "MAX" if re.search(r"\b(?:max|maximum|highest|largest|biggest|most expensive)\b", query, re.I)
              else "MIN" if re.search(r"\b(?:min|minimum|lowest|smallest|cheapest)\b", query, re.I) else "SUM")
        unit = {"SUM": "Total", "AVG": "Average", "MAX": "Highest", "MIN": "Lowest"}[fn] + " " + measure
        expr, idc = f"{fn}({measure})", None
    else:
        e = ent or (ENTITIES[ENTITY_OF_COL[named_c]] if named_c in ENTITY_OF_COL else None)
        if e:
            unit, expr, idc = e[1], f"COUNT(DISTINCT {e[2]})", e
        elif named_c and distinct:
            unit, expr, idc = f"Distinct {named_c}", f"COUNT(DISTINCT {named_c})", (None, None, named_c, None)
        elif named_c:
            unit, expr, idc = f"{named_c} (non-empty)", f"COUNT({named_c})", None
        else:
            unit, expr, idc = "Rows", "COUNT(*)", None
    fmt = (lambda v: "none" if v is None else f"{v:.2f}") if measure else str

    # 5a) Grouped: "per area", "which week has the most items".
    if group:
        asc = bool(re.search(LO, query, re.I))
        rows = conn.execute(f"SELECT {', '.join(gcols)}, {expr} FROM test_data WHERE {cond} GROUP BY {', '.join(gcols)} "
                            f"ORDER BY {len(gcols) + 1} {'ASC' if asc else 'DESC'} LIMIT 500", args).fetchall()
        if measure:
            rows = [[*r[:-1], round(r[-1], 2) if isinstance(r[-1], float) else r[-1]] for r in rows]
        head = f"{unit} per {group}"
        if fn in (None, "SUM"):
            total = conn.execute(f"SELECT {expr} FROM test_data WHERE {cond}", args).fetchone()[0]
            head += f": {total:,.2f} in total" if measure and total is not None else f": {total:,} in total" if total is not None else ""
        if rows and re.search(HI + "|" + LO, query, re.I):
            top = next((r for r in rows if r[len(gcols) - 1] is not None), rows[0])  # an empty group can't "be" the winner
            gv = top[len(gcols) - 1]
            head = f"{'(empty)' if gv is None else gv} has the {'least' if asc else 'most'}: {fmt(top[-1])} {unit}"
        if len(rows) == 500:
            head += " — top 500 shown"
        return table("group", head + tail, gcols + [unit], rows)

    # 5b) Distinct list: "unique officeID", "list unique workspaces" -> the count plus every value with its row count.
    if distinct and idc and idc[2]:
        cols = [idc[2]] + ([idc[3]] if idc[3] else [])
        n = conn.execute(f"SELECT {expr} FROM test_data WHERE {cond}", args).fetchone()[0]
        vals = conn.execute(f"SELECT {', '.join(cols)}, COUNT(*) FROM test_data WHERE {cond} AND {idc[2]} IS NOT NULL "
                            f"GROUP BY {', '.join(cols)} ORDER BY {len(cols) + 1} DESC, 1 LIMIT 500", args).fetchall()
        label = f"distinct {idc[2]} values" if idc[0] is None else f"unique {idc[1].lower()}"
        return table("count", f"{n} {label}" + (" — top 500 shown" if len(vals) == 500 else "") + (tail or "  (no filters)"), cols + ["Rows"], vals)

    # 5c) One number (plus the extreme rows for highest/lowest price).
    v = conn.execute(f"SELECT {expr} FROM test_data WHERE {cond}", args).fetchone()[0]
    text = f"{fmt(v)} {unit if measure else unit.lower()}" + (tail or "  (no filters)")
    if measure:
        k = conn.execute(f"SELECT COUNT({measure}), COUNT(*) FROM test_data WHERE {cond}", args).fetchone()
        text += f"  [{k[0]:,} of {k[1]:,} rows have a value]"
        if fn in ("MAX", "MIN") and measure == "ConstructionPrice":
            top = conn.execute(f"SELECT * FROM test_data WHERE {cond} AND {measure} IS NOT NULL ORDER BY {measure} "
                               f"{'DESC' if fn == 'MAX' else 'ASC'} LIMIT 5", args).fetchall()
            return out("count", text, top)
    return out("count", text)


def _semantic(conn, query, where, out):
    flt = [{c: v} for c, op, v in where if op == "="]
    flt = None if not flt else flt[0] if len(flt) == 1 else {"$and": flt}
    model, col = _vectors()
    res = col.query(query_embeddings=model.encode([query.strip() or "x"], normalize_embeddings=True).tolist(), n_results=5, where=flt)
    ids = res["ids"][0]
    by_id = {str(r["row_id"]): r for r in conn.execute(
        f"SELECT * FROM test_data WHERE row_id IN ({','.join('?' * len(ids))})", ids)} if ids else {}
    shown = ", ".join(f"{c} = {v}" for c, _, v in where)
    return out("semantic", "Closest matches" + (f"  ({shown})" if shown else ""), [by_id[i] for i in ids])


if __name__ == "__main__":
    def show(r):
        print(r["text"])
        for row in r["rows"]: print("  ", " | ".join(str(c) for c in row))
    if len(sys.argv) > 1:
        show(search(" ".join(sys.argv[1:])))
    else:
        print("Ask in plain English (blank line to quit). e.g.  workspaces per world area")
        while (q := input("> ").strip()):
            show(search(q)); print()
