# Incremental sync SQLite -> Chroma. Run after test_data changes.
import os, sqlite3
from sentence_transformers import SentenceTransformer
import chromadb

STATE = "last_sync.txt"
TEXT = ["WorkspaceName", "ItemName", "ConstructionType", "WorldArea", "WorkspaceCreatedBy"]  # keep same as build_index.py
conn = sqlite3.connect("shop.db"); conn.row_factory = sqlite3.Row
model = SentenceTransformer("all-MiniLM-L6-v2")
col = chromadb.PersistentClient(path="./vector_store").get_or_create_collection(
    "test_data", metadata={"hnsw:space": "cosine"})

last = open(STATE).read().strip() if os.path.exists(STATE) else "1970-01-01"

# >= not >: WorkspaceLastModified is date-only, so same-day edits would be missed; upsert is idempotent
rows = conn.execute("SELECT * FROM test_data WHERE WorkspaceLastModified >= ?", (last,)).fetchall()
for i in range(0, len(rows), 256):
    chunk = rows[i:i + 256]
    texts = [". ".join(str(r[c]) for c in TEXT if r[c] is not None) for r in chunk]
    col.upsert(
        ids=[str(r["row_id"]) for r in chunk],
        embeddings=model.encode(texts, normalize_embeddings=True).tolist(),
        documents=texts,
        metadatas=[{k: r[k] for k in r.keys() if k != "row_id" and r[k] is not None} for r in chunk],
    )
if rows:
    open(STATE, "w").write(max(r["WorkspaceLastModified"] or last for r in rows))

# deleted rows
sql_ids = {str(r[0]) for r in conn.execute("SELECT row_id FROM test_data")}
gone = list(set(col.get(include=[])["ids"]) - sql_ids)
if gone:
    col.delete(ids=gone)

print(f"upserted {len(rows)}, deleted {len(gone)}, total {col.count()}")
