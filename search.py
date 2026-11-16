"""Plain-English search over shop.db + vector_store. CLI: python search.py [query]   UI: web.py POST /search."""
import re, sqlite3, sys, threading

COLS = ["WorkspaceName", "ItemName", "ConstructionType", "WorldArea", "ConstructionPrice", "Week_Label"]
_lock, _model, _col = threading.Lock(), None, None


def _vectors():  # heavy imports + model load only on first semantic query
    global _model, _col
    with _lock:
        if _model is None:
            from sentence_transformers import SentenceTransformer
            import chromadb
            _model = SentenceTransformer("all-MiniLM-L6-v2")        # MUST match build_index.py
            _col = chromadb.PersistentClient(path="./vector_store").get_collection("test_data")
    return _model, _col


def search(query):
    """-> {"kind": "count"|"exact"|"semantic", "text": str, "columns": [...], "rows": [[...]]}"""
    conn = sqlite3.connect("shop.db")
    conn.row_factory = sqlite3.Row
    try:
        out = lambda kind, text, rows=(): {"kind": kind, "text": text, "columns": COLS,
                                           "rows": [[r[c] for c in COLS] for r in rows]}
        # 1) Exact ID lookup: any token equal to an ItemName / ItemId / WorkspaceId wins outright.
        tokens = query.replace(",", " ").split()
        if tokens:
            m = ",".join("?" * len(tokens))
            exact = conn.execute(f"SELECT * FROM test_data WHERE upper(ItemName) IN ({m}) OR CAST(ItemId AS TEXT) IN ({m}) "
                                 f"OR CAST(WorkspaceId AS TEXT) IN ({m}) LIMIT 50",
                                 [t.upper() for t in tokens] + tokens + tokens).fetchall()
            if exact:
                return out("exact", f"Exact match: {len(exact)} row(s)" + (" (showing first 50)" if len(exact) == 50 else ""), exact)

        # 2) Filters spoken in the query are hard conditions, not left to embeddings.
        where, rest = [], query
        if (w := re.search(r"\bweek\s*(\d+)\b", query, re.I)):
            where.append({"Week_Label": f"Week {int(w.group(1))}"}); rest = rest.replace(w.group(0), " ")
        if (a := re.search(r"\b(AP|MEA|EUR)\b", query)):
            where.append({"WorldArea": a.group(1)}); rest = rest.replace(a.group(0), " ")
        if (u := re.search(r"\b(?:created by|from user|by)\s+(\w+)", query, re.I)):
            where.append({"WorkspaceCreatedBy": u.group(1).lower()}); rest = rest.replace(u.group(0), " ")
        flt = None if not where else where[0] if len(where) == 1 else {"$and": where}
        rest = rest.strip() or query
        shown = ", ".join(f"{k} = {v}" for d in where for k, v in d.items())

        # Counting / listing questions are answered with SQL over the whole table, never from the top-5 sample.
        cond = " AND ".join(f"{k} = ?" for d in where for k in d) or "1=1"
        args = [v for d in where for v in d.values()]
        # A column named in the question ("unique values in officeID column") beats the workspace/item shortcuts.
        squashed = re.sub(r"[\s_]+", "", query.lower())
        named = [c[1] for c in conn.execute("PRAGMA table_info(test_data)") if c[1] != "row_id" and c[1].lower().replace("_", "") in squashed]
        colname = max(named, key=len) if named else None  # 'OfficeId' over 'Office'
        distinct = colname and re.search(r"unique|distinct|different", query, re.I)
        if distinct or re.search(r"how many|number of|count|total", query, re.I):
            if colname and distinct:
                unit, expr = f"distinct {colname} values", f"COUNT(DISTINCT {colname})"
            elif colname:
                unit, expr = f"{colname} values (non-empty)", f"COUNT({colname})"
            elif re.search(r"\bwor\w*ces?\b", query, re.I): unit, expr = "workspaces", "COUNT(DISTINCT WorkspaceId)"  # tolerates typos like 'worksapces'
            elif re.search(r"\bitems?\b", query, re.I): unit, expr = "items", "COUNT(DISTINCT ItemId)"
            else: unit, expr = "rows", "COUNT(*)"
            n = conn.execute(f"SELECT {expr} FROM test_data WHERE {cond}", args).fetchone()[0]
            res = out("count", f"{n} {unit}" + (f"  ({shown})" if shown else "  (no filters)"))
            if distinct:  # also list every value with how many rows carry it
                vals = conn.execute(f"SELECT {colname}, COUNT(*) FROM test_data WHERE {cond} AND {colname} IS NOT NULL "
                                    f"GROUP BY {colname} ORDER BY 2 DESC, 1 LIMIT 500", args).fetchall()
                res["columns"], res["rows"] = [colname, "Rows"], [list(v) for v in vals]
            return res

        # 3) Semantic ranking on what's left, inside the filter.
        model, col = _vectors()
        res = col.query(query_embeddings=model.encode([rest], normalize_embeddings=True).tolist(), n_results=5, where=flt)
        ids = res["ids"][0]
        by_id = {str(r["row_id"]): r for r in conn.execute(
            f"SELECT * FROM test_data WHERE row_id IN ({','.join('?' * len(ids))})", ids)} if ids else {}
        return out("semantic", "Closest matches" + (f"  ({shown})" if shown else ""), [by_id[i] for i in ids])
    finally:
        conn.close()


if __name__ == "__main__":
    def show(r):
        print(r["text"])
        for row in r["rows"]: print("  ", " | ".join(str(c) for c in row))
    if len(sys.argv) > 1:
        show(search(" ".join(sys.argv[1:])))
    else:
        print("Ask in plain English (blank line to quit). e.g.  pertamina items week 33 AP by dilla")
        while (q := input("> ").strip()):
            show(search(q)); print()
