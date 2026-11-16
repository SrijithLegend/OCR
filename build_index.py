import sqlite3
from sentence_transformers import SentenceTransformer
import chromadb

conn = sqlite3.connect("shop.db")
conn.row_factory = sqlite3.Row
rows = conn.execute("SELECT * FROM test_data").fetchall()

model = SentenceTransformer("all-MiniLM-L6-v2")        # 384-dim vectors
client = chromadb.PersistentClient(path="./vector_store")
col = client.get_or_create_collection("test_data", metadata={"hnsw:space": "cosine"})

TEXT = ["WorkspaceName", "ItemName", "ConstructionType", "WorldArea", "WorkspaceCreatedBy"]  # searchable text
BATCH = 256
for i in range(0, len(rows), BATCH):
    chunk = rows[i:i + BATCH]
    texts = [". ".join(str(r[c]) for c in TEXT if r[c] is not None) for r in chunk]
    embs = model.encode(texts, normalize_embeddings=True).tolist()
    col.upsert(
        ids=[str(r["row_id"]) for r in chunk],
        embeddings=embs,
        documents=texts,
        # everything else as filterable metadata (Chroma rejects None)
        metadatas=[{k: r[k] for k in r.keys() if k != "row_id" and r[k] is not None} for r in chunk],
    )

print("indexed:", col.count(), "of", len(rows))
