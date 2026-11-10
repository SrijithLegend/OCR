"""Upload page for main.py: drop PDFs/images, get process_conditions.xlsx back.
Run: uvicorn web:app --host 127.0.0.1 --port 8000   (needs GENAI_API_KEY, e.g. in .env)"""
import os
import tempfile
from argparse import Namespace
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from starlette.background import BackgroundTask
from google import genai

import main as ocr

ALLOWED = {".pdf", ".png", ".jpg", ".jpeg"}
MAX_MB = 50
ARGS = Namespace(passes=int(os.environ.get("PASSES", "1")), dpi=int(os.environ.get("DPI", "300")),
                 bands=int(os.environ.get("BANDS", "0")), cols=int(os.environ.get("COLS", "1")))
app = FastAPI(docs_url=None, redoc_url=None)

PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Process conditions extractor</title>
<style>body{font:15px system-ui;max-width:640px;margin:40px auto;padding:0 16px}
#drop{border:2px dashed #888;border-radius:12px;padding:48px 16px;text-align:center;cursor:pointer}
#drop.over{background:#e8f0fe}#msg{margin-top:16px;white-space:pre-wrap}</style>
<h1>Process conditions extractor</h1>
<div id=drop>Drop PDFs or images here, or click to choose<input id=pick type=file multiple hidden
 accept=".pdf,.png,.jpg,.jpeg"></div><div id=msg></div>
<script>
const drop=document.getElementById('drop'),pick=document.getElementById('pick'),msg=document.getElementById('msg');
async function send(files){
  if(!files.length)return;
  const fd=new FormData();[...files].forEach(f=>fd.append('files',f));
  msg.textContent='Extracting '+files.length+' file(s)... this can take a few minutes.';
  try{
    const r=await fetch('/extract',{method:'POST',body:fd});
    if(!r.ok){let d=r.statusText;try{d=(await r.json()).detail}catch{}throw new Error(d)}
    const a=document.createElement('a');a.href=URL.createObjectURL(await r.blob());
    a.download='process_conditions.xlsx';a.click();
    msg.textContent='Done. process_conditions.xlsx downloaded - open the "Review" sheet first.';
  }catch(e){msg.textContent='Failed: '+e.message}
}
drop.onclick=()=>pick.click();pick.onchange=()=>{send(pick.files);pick.value=''};
['dragenter','dragover'].forEach(t=>drop.addEventListener(t,e=>{e.preventDefault();drop.classList.add('over')}));
['dragleave','drop'].forEach(t=>drop.addEventListener(t,e=>{e.preventDefault();drop.classList.remove('over')}));
drop.addEventListener('drop',e=>send(e.dataTransfer.files));
['dragover','drop'].forEach(t=>window.addEventListener(t,e=>e.preventDefault()));
</script>"""


@app.get("/", response_class=HTMLResponse)
def index():
    return PAGE


@app.post("/extract")
def extract(files: list[UploadFile] = File(...)):
    tmp = tempfile.TemporaryDirectory()
    try:
        paths = []
        for i, up in enumerate(files):
            name = Path(up.filename or "file").name[:120]  # basename only, never a path
            if Path(name).suffix.lower() not in ALLOWED:
                raise HTTPException(415, f"{name}: unsupported type (PDF, PNG, JPG only)")
            data = up.file.read(MAX_MB * 1024 * 1024 + 1)
            if not data or len(data) > MAX_MB * 1024 * 1024:
                raise HTTPException(413 if data else 422, f"{name}: empty or larger than {MAX_MB} MB")
            p = Path(tmp.name) / (name if not (Path(tmp.name) / name).exists() else f"{i}_{name}")
            p.write_bytes(data)
            paths.append(p)
        client = genai.Client(api_key=os.environ["GENAI_API_KEY"])
        results, issues = [], []
        for p in paths:
            valves, iss = ocr.process_file(client, ocr.MODEL, p, ARGS)
            results.append((p.name, valves))
            issues += iss
        out = Path(tmp.name) / "process_conditions.xlsx"
        ocr.build_workbook(results, issues, str(out))
    except Exception:
        tmp.cleanup()
        raise
    return FileResponse(out, filename="process_conditions.xlsx", background=BackgroundTask(tmp.cleanup),
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
