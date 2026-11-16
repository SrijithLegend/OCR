import re, sqlite3, sys
from sentence_transformers import SentenceTransformer
import chromadb

conn = sqlite3.connect("shop.db")
conn.row_factory = sqlite3.Row
COLS = "WorkspaceName | ItemName | ConstructionType | WorldArea | ConstructionPrice | Week_Label"
show = lambda r: f"{r['WorkspaceName']} | {r['ItemName']} | {r['ConstructionType']} | {r['WorldArea']} | {r['ConstructionPrice']} | {r['Week_Label']}"

model = SentenceTransformer("all-MiniLM-L6-v2")        # MUST match build_index.py
col = chromadb.PersistentClient(path="./vector_store").get_collection("test_data")


def search(query):
    # 1) Exact ID lookup: any token equal to an ItemName / ItemId / WorkspaceId wins outright.
    tokens = query.replace(",", " ").split()
    if tokens:
        m = ",".join("?" * len(tokens))
        exact = conn.execute(f"SELECT * FROM test_data WHERE upper(ItemName) IN ({m}) OR CAST(ItemId AS TEXT) IN ({m}) "
                             f"OR CAST(WorkspaceId AS TEXT) IN ({m}) LIMIT 50", [t.upper() for t in tokens] + tokens + tokens).fetchall()
        if exact:
            print(f"exact match ({len(exact)}):  {COLS}")
            for r in exact: print("  ", show(r))
            return

    # 2) Filters spoken in the query ("week 33", "AP/MEA/EUR") are applied as hard conditions, not left to embeddings.
    where, rest = [], query
    if (w := re.search(r"\bweek\s*(\d+)\b", query, re.I)):
        where.append({"Week_Label": f"Week {int(w.group(1))}"}); rest = rest.replace(w.group(0), " ")
    if (a := re.search(r"\b(AP|MEA|EUR)\b", query)):
        where.append({"WorldArea": a.group(1)}); rest = rest.replace(a.group(0), " ")
    if (u := re.search(r"\b(?:created by|from user|by)\s+(\w+)", query, re.I)):
        where.append({"WorkspaceCreatedBy": u.group(1).lower()}); rest = rest.replace(u.group(0), " ")
    flt = None if not where else where[0] if len(where) == 1 else {"$and": where}
    rest = rest.strip() or query

    # Counting questions are answered with SQL over the whole table, never from the top-5 sample.
    if re.search(r"how many|number of|count|total", query, re.I):
        cond = " AND ".join(f"{k} = ?" for d in where for k in d) or "1=1"
        args = [v for d in where for v in d.values()]
        if re.search(r"\bwor\w*ces?\b", query, re.I): unit, expr = "workspaces", "COUNT(DISTINCT WorkspaceId)"  # tolerates typos like 'worksapces'
        elif re.search(r"\bitems?\b", query, re.I): unit, expr = "items", "COUNT(DISTINCT ItemId)"
        else: unit, expr = "rows", "COUNT(*)"
        n = conn.execute(f"SELECT {expr} FROM test_data WHERE {cond}", args).fetchone()[0]
        print(f"{n} {unit}" + (f"   (filters: {where})" if where else "   (no filters)"))
        return

    # 3) Semantic ranking on what's left, inside the filter.
    res = col.query(query_embeddings=model.encode([rest], normalize_embeddings=True).tolist(), n_results=5, where=flt)
    ids = res["ids"][0]
    by_id = {str(r["row_id"]): r for r in conn.execute(f"SELECT * FROM test_data WHERE row_id IN ({','.join('?' * len(ids))})", ids)} if ids else {}
    print(f"semantic{' + filter ' + str(flt) if flt else ''}:  {COLS}")
    for i, d in zip(ids, res["distances"][0]): print(f"  {d:.3f}", show(by_id[i]))


if len(sys.argv) > 1:
    search(" ".join(sys.argv[1:]))
else:
    print("Ask in plain English (blank line to quit). e.g.  pertamina items week 33 AP by dilla")
    while (q := input("> ").strip()):
        search(q)
        print()
