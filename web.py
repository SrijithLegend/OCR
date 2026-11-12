"""Web UI for main.py: upload PDFs/images -> review and edit the extracted table -> export Excel.
Run: uvicorn web:app --host 127.0.0.1 --port 8000   (needs GENAI_API_KEY, e.g. in .env)"""
import base64
import collections
import os
import tempfile
from argparse import Namespace
from pathlib import Path

from fastapi import Body, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from google import genai
from google.genai import types
from starlette.background import BackgroundTask

import main as ocr
import openpyxl
import standard_valve as sv

ALLOWED = {".pdf", ".png", ".jpg", ".jpeg"}
MAX_MB = 50
ARGS = Namespace(passes=int(os.environ.get("PASSES", "1")), dpi=int(os.environ.get("DPI", "300")),
                 bands=int(os.environ.get("BANDS", "0")), cols=int(os.environ.get("COLS", "1")))
PAGE = Path(__file__).with_name("web.html").read_text(encoding="utf-8")
app = FastAPI(docs_url=None, redoc_url=None)


@app.get("/", response_class=HTMLResponse)
def index():
    return PAGE


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


@app.post("/standardize")
def standardize(file: UploadFile = File(...)):
    """Excel/CSV of valve descriptions -> {sheets, desc_col, marks, summary, xlsx (base64)}. Local only, nothing stored."""
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
            header, desc_i, recs, master_rows, has_header = sv.run(rows)
            sv.write(out, header, desc_i, recs, master_rows, like_input=True, header_row=has_header)
        except HTTPException:
            raise
        except SystemExit as e:  # standard_valve reports user errors this way
            raise HTTPException(422, str(e))
        except Exception as e:
            raise HTTPException(422, f"{name}: could not read ({e})")
        wb = openpyxl.load_workbook(out)
        sheets = [{"name": ws.title, "rows": [list(r) for r in ws.iter_rows(values_only=True)]} for ws in wb]
        xlsx = base64.b64encode(out.read_bytes()).decode()
    marks = ["bad" if not x["std"] else "changed" if str(x["std"]) != str(x["row"][desc_i]) else "" for x in recs]
    counts = collections.Counter(x["status"] for x in recs)
    summary = f"{len(recs)} rows, {len(master_rows)} part numbers · " + " · ".join(
        f"{s} {counts[s]}" for s in sv.SEVERITY if counts[s])
    return {"sheets": sheets, "desc_col": desc_i, "has_header": has_header, "marks": marks, "summary": summary, "xlsx": xlsx}
