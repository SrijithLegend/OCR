"""Web UI for main.py: upload PDFs/images -> review and edit the extracted table -> export Excel.
Run: uvicorn web:app --host 127.0.0.1 --port 8000   (needs GENAI_API_KEY, e.g. in .env)"""
import atexit
import collections
import os
import re
import shutil
import sqlite3
import tempfile
import uuid
from argparse import Namespace
from pathlib import Path

from fastapi import Body, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from google import genai
from google.genai import types
from starlette.background import BackgroundTask

import main as ocr
import openpyxl
import estimate
import idfix
import nlsql
import standard_valve as sv

ALLOWED = {".pdf", ".png", ".jpg", ".jpeg"}
MAX_MB = 50
ARGS = Namespace(passes=int(os.environ.get("PASSES", "1")), dpi=int(os.environ.get("DPI", "300")),
                 bands=int(os.environ.get("BANDS", "0")), cols=int(os.environ.get("COLS", "1")))
PAGE = Path(__file__).with_name("web.html").read_text(encoding="utf-8")
app = FastAPI(docs_url=None, redoc_url=None)

# Databases uploaded in the Search tab live in a private temp folder for this server run (newest 5 kept, never written to).
DB_DIR = Path(tempfile.mkdtemp(prefix="nlsql_"))
atexit.register(shutil.rmtree, DB_DIR, ignore_errors=True)
DBS = collections.OrderedDict()  # id -> (path, original file name)
MAX_DBS, MAX_DB_MB = 5, 500


HERE = Path(__file__).parent


def local_dbs():
    return sorted(p.name for p in HERE.iterdir() if p.is_file() and p.suffix.lower() in {".db", ".sqlite", ".sqlite3", ".db3"})


def db_path(db_id):
    if db_id.startswith("local:"):  # a database file sitting next to the app
        name = db_id[6:]
        if name not in local_dbs():
            raise HTTPException(404, f"{name} is not in the app folder.")
        return str(HERE / name), name
    if not db_id:
        return nlsql.DEFAULT_DB, Path(nlsql.DEFAULT_DB).name
    if db_id not in DBS:
        raise HTTPException(404, "That database is no longer loaded. Upload it again.")
    return DBS[db_id]


@app.get("/", response_class=HTMLResponse)
def index():
    return PAGE


WHATIF = re.compile(r"\b(if|would|suppose|estimate|hypothetical|may|might|approx\w*|roughly|how much)\b", re.I)


def _estimate(q, path):
    try:
        return estimate.what_if(q, path)
    except Exception:  # the estimate is a bonus; the normal answer still stands
        return None


@app.post("/search")
def search(payload: dict = Body(...)):
    """Plain-English question about a database -> {kind, text, sql, columns, rows, truncated}. Read-only."""
    q = str(payload.get("query", "")).strip()
    if not q:
        raise HTTPException(422, "Type a question first")
    path, _ = db_path(payload.get("db"))
    what_if = bool(WHATIF.search(q))
    r = _estimate(q, path) if what_if else None  # "if there were yellow ...": try an estimate first
    if r:
        return r
    r = nlsql.ask(q, path)
    if r["kind"] == "empty" and not what_if:  # nothing stored: it may still be a what-if without the usual cue words
        r = _estimate(q, path) or r
    return r


@app.get("/search/local")
def search_local():
    """Database files found in the app folder, and which one is used when none is chosen."""
    return {"files": local_dbs(), "default": Path(nlsql.DEFAULT_DB).name}


@app.post("/search/fix")
def search_fix(payload: dict = Body(...)):
    """Catalogue table (found automatically): rows whose id is wrong -> fixes from the database or, failing that, a web price. Reads only."""
    path, name = db_path(payload.get("db"))
    try:
        return idfix.check_table(path)
    except sqlite3.Error as e:
        raise HTTPException(422, f"{name} cannot be checked: {e}")
    except ValueError as e:
        raise HTTPException(422, str(e))
    except RuntimeError as e:
        raise HTTPException(503, str(e))


@app.get("/search/info")
def search_info(db: str = ""):
    """Which database is in use and what it contains (tables, row and column counts)."""
    path, name = db_path(db)
    try:
        return {"db": db, "name": name, "tables": nlsql.overview(path)}
    except (sqlite3.Error, OSError) as e:
        raise HTTPException(422, f"Could not read {name}: {e}")


@app.post("/search/db")
def search_upload_db(file: UploadFile = File(...)):
    """Upload any SQLite file to ask questions of it. Checked, size-capped, opened read-only."""
    name = Path(file.filename or "database").name[:120]
    if Path(name).suffix.lower() not in {".db", ".sqlite", ".sqlite3", ".db3"}:
        raise HTTPException(415, f"{name}: unsupported type (.db, .sqlite, .sqlite3, .db3 only)")
    db_id = uuid.uuid4().hex
    dest, size = DB_DIR / f"{db_id}.sqlite", 0
    with dest.open("wb") as f:
        while chunk := file.file.read(1 << 20):
            size += len(chunk)
            if size > MAX_DB_MB << 20:
                f.close(); dest.unlink(missing_ok=True)
                raise HTTPException(413, f"{name}: larger than {MAX_DB_MB} MB")
            f.write(chunk)
    if dest.read_bytes()[:15] != b"SQLite format 3":
        dest.unlink(missing_ok=True)
        raise HTTPException(422, f"{name}: not a SQLite database file")
    try:
        tables = nlsql.overview(str(dest))
    except sqlite3.Error as e:
        dest.unlink(missing_ok=True)
        raise HTTPException(422, f"{name}: could not read ({e})")
    if not tables:
        dest.unlink(missing_ok=True)
        raise HTTPException(422, f"{name}: no tables found")
    DBS[db_id] = (str(dest), name)
    while len(DBS) > MAX_DBS:
        Path(DBS.popitem(last=False)[1][0]).unlink(missing_ok=True)
    return {"db": db_id, "name": name, "tables": tables}


@app.post("/extract")
def extract(files: list[UploadFile] = File(...)):
    """Files -> {results: [{file, valves}], issues}. Same order as the upload. Nothing is stored."""
    with tempfile.TemporaryDirectory() as tmp:
        paths = []
        for i, up in enumerate(files):
            name = Path(up.filename or "file").name[:120]  # basename only, never a path
            if Path(name).suffix.lower() not in ALLOWED:
                raise HTTPException(415, f"{name}: unsupported type (PDF, PNG, JPG only)")
            data = up.file.read(MAX_MB * 1024 * 1024 + 1)
            if not data or len(data) > MAX_MB * 1024 * 1024:
                raise HTTPException(413 if data else 422, f"{name}: empty or larger than {MAX_MB} MB")
            p = Path(tmp) / (name if not (Path(tmp) / name).exists() else f"{i}_{name}")
            p.write_bytes(data)
            paths.append((name, p))
        client = genai.Client(api_key=os.environ["GENAI_API_KEY"], http_options=types.HttpOptions(timeout=300_000))
        results, issues = [], []
        for name, p in paths:
            valves, iss = ocr.process_file(client, ocr.MODEL, p, ARGS)
            for i in iss:
                i["file"] = name
            results.append({"file": name, "valves": valves})
            issues += iss
    return {"results": results, "issues": issues}


@app.post("/export")
def export(payload: dict = Body(...)):
    """The (edited) table from the page -> process_conditions.xlsx."""
    tmp = tempfile.TemporaryDirectory()
    out = Path(tmp.name) / "process_conditions.xlsx"
    try:
        results = [(r["file"], r["valves"]) for r in payload["results"]]
        ocr.build_workbook(results, payload.get("issues", []), str(out))
    except (KeyError, TypeError, AttributeError, ValueError) as e:
        tmp.cleanup()
        raise HTTPException(422, f"Bad table data: {e}")
    return FileResponse(out, filename="process_conditions.xlsx", background=BackgroundTask(tmp.cleanup),
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.post("/standardize/export")
def standardize_export(payload: dict = Body(...)):
    """The (edited) table from the page -> xlsx."""
    tmp = tempfile.TemporaryDirectory()
    out = Path(tmp.name) / "standardized.xlsx"
    try:
        rows = [[(int(c) if i == 2 and str(c).strip().isdigit() else (c or None)) for i, c in enumerate(r)]
                for r in payload["rows"][1:]]
        sv.write_summary(out, rows)
    except (KeyError, TypeError, ValueError) as e:
        tmp.cleanup()
        raise HTTPException(422, f"Bad table data: {e}")
    return FileResponse(out, filename="standardized.xlsx", background=BackgroundTask(tmp.cleanup),
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.post("/standardize")
def standardize(file: UploadFile = File(...)):
    """Excel/CSV of valve descriptions -> {sheets, marks, summary}. Local only, nothing stored."""
    name = Path(file.filename or "file").name[:120]
    ext = Path(name).suffix.lower()
    if ext not in {".xlsx", ".xlsm", ".csv"}:
        raise HTTPException(415, f"{name}: unsupported type (XLSX, XLSM, CSV only)")
    data = file.file.read(MAX_MB * 1024 * 1024 + 1)
    if not data or len(data) > MAX_MB * 1024 * 1024:
        raise HTTPException(413 if data else 422, f"{name}: empty or larger than {MAX_MB} MB")
    with tempfile.TemporaryDirectory() as tmp:
        src, out = Path(tmp) / ("in" + ext), Path(tmp) / "out.xlsx"
        src.write_bytes(data)
        try:
            rows = sv.load_table(src)
            if not rows:
                raise HTTPException(422, f"{name}: empty")
            nrows, master_rows, excluded = sv.run(rows)
            sv.write_summary(out, master_rows, excluded)
        except HTTPException:
            raise
        except SystemExit as e:  # standard_valve reports user errors this way
            raise HTTPException(422, str(e))
        except Exception as e:
            raise HTTPException(422, f"{name}: could not read ({e})")
        wb = openpyxl.load_workbook(out)
        sheets = [{"name": ws.title, "rows": [list(r) for r in ws.iter_rows(values_only=True)]} for ws in wb]
    marks = ["bad" if not r[1] else "changed" if r[3] else "" for r in master_rows]
    summary = f"{nrows} rows, {len(master_rows)} part numbers · excluded {len(excluded)} · no majority {marks.count('bad')} · disagreements {marks.count('changed')}"
    return {"sheets": sheets, "marks": marks, "summary": summary}
