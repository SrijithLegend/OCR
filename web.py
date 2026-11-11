"""Web UI for main.py: upload PDFs/images -> review and edit the extracted table -> export Excel.
Run: uvicorn web:app --host 127.0.0.1 --port 8000   (needs GENAI_API_KEY, e.g. in .env)"""
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
