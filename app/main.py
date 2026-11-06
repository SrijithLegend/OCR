"""FastAPI app: upload -> background extraction -> editable table -> Excel export."""
import csv
import io
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import store
from .config import ALLOWED_SUFFIXES, MAX_FILES, MAX_UPLOAD_MB
from .excel import build_xlsx
from .pipeline import run_job
from .schemas import Cell, CellPatch, FileInfo, Job, NewCell

STATIC = Path(__file__).parent / "static"
app = FastAPI(title="Valve datasheet process-conditions extractor", docs_url=None, redoc_url=None)

MAX_FIELD = 300  # longest accepted value / unit / parameter text


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Cache-Control"] = "no-store"
    return resp


def magic_family(head: bytes):
    if head.startswith(b"%PDF-"):
        return "pdf"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"
    return None


SUFFIX_FAMILY = {".pdf": "pdf", ".png": "png", ".jpg": "jpeg", ".jpeg": "jpeg", ".tif": "tiff", ".tiff": "tiff"}


def get_job(job_id: str) -> Job:
    try:
        return store.get(job_id)
    except KeyError:
        raise HTTPException(404, "Job not found")


def find_cell(job: Job, cell_id: str):
    for sh in job.sheets:
        for c in sh.cells:
            if c.id == cell_id:
                return sh, c
    raise HTTPException(404, "Cell not found")


def clean(s):
    if s is None:
        return None
    s = s.strip()
    if len(s) > MAX_FIELD:
        raise HTTPException(422, f"Text longer than {MAX_FIELD} characters")
    return s or None


# --------------------------------------------------------------------- upload
@app.post("/api/jobs", status_code=202)
async def create_job(background: BackgroundTasks, files: list[UploadFile] = File(...)):
    if not files or len(files) > MAX_FILES:
        raise HTTPException(422, f"Upload 1 to {MAX_FILES} files")
    limit = MAX_UPLOAD_MB * 1024 * 1024
    job = store.create()
    updir = store.job_dir(job.id) / "uploads"
    try:
        for idx, up in enumerate(files):
            name = Path(up.filename or "file").name[:120]  # display only; never used as a path
            suffix = Path(name).suffix.lower()
            if suffix not in ALLOWED_SUFFIXES:
                raise HTTPException(415, f"{name}: unsupported type (allowed: PDF, PNG, JPG, TIFF)")
            dest, size, head = updir / f"{idx}{suffix}", 0, b""
            with dest.open("wb") as out:
                while chunk := await up.read(1024 * 1024):
                    if not head:
                        head = chunk[:16]
                    size += len(chunk)
                    if size > limit:
                        raise HTTPException(413, f"{name}: larger than {MAX_UPLOAD_MB} MB")
                    out.write(chunk)
            if size == 0:
                raise HTTPException(422, f"{name}: empty file")
            if magic_family(head) != SUFFIX_FAMILY[suffix]:
                raise HTTPException(415, f"{name}: content does not match its extension")
            job.files.append(FileInfo(idx=idx, name=name))
    except HTTPException:
        store.delete(job.id)
        raise
    store.save(job)
    background.add_task(run_job, job.id)
    return {"id": job.id}


@app.get("/api/jobs/{job_id}")
def read_job(job_id: str):
    return get_job(job_id)


@app.delete("/api/jobs/{job_id}", status_code=204)
def delete_job(job_id: str):
    get_job(job_id)
    store.delete(job_id)


@app.get("/api/jobs/{job_id}/pages/{file_idx}/{page}.png")
def page_image(job_id: str, file_idx: int, page: int):
    get_job(job_id)
    p = store.job_dir(job_id) / "pages" / f"{file_idx}_{page}.png"
    if not p.is_file():
        raise HTTPException(404, "Page not found")
    return FileResponse(p, media_type="image/png")


# ------------------------------------------------------------------- editing
@app.patch("/api/jobs/{job_id}/cells/{cell_id}")
def patch_cell(job_id: str, cell_id: str, patch: CellPatch):
    job = get_job(job_id)
    with store.lock():
        _, c = find_cell(job, cell_id)
        fields = patch.model_fields_set
        new_val = clean(patch.value) if "value" in fields else c.value
        new_unit = clean(patch.unit) if "unit" in fields else c.unit
        new_par = clean(patch.parameter) if "parameter" in fields else c.parameter
        if not new_par:
            raise HTTPException(422, "Parameter cannot be empty")
        if (new_val, new_unit, new_par) != (c.value, c.unit, c.parameter):
            if not c.edited:
                c.original_value, c.original_unit = c.value, c.unit
            c.value, c.unit, c.parameter = new_val, new_unit, new_par
            c.edited = c.reviewed = True
        if "reviewed" in fields and patch.reviewed is not None:
            c.reviewed = patch.reviewed or c.edited  # an edited cell stays reviewed
        store.save(job)
        return c


@app.post("/api/jobs/{job_id}/sheets/{sheet_id}/cells", status_code=201)
def add_cell(job_id: str, sheet_id: str, new: NewCell):
    job = get_job(job_id)
    with store.lock():
        sh = next((s for s in job.sheets if s.id == sheet_id), None)
        if sh is None:
            raise HTTPException(404, "Sheet not found")
        par = clean(new.parameter)
        if not par:
            raise HTTPException(422, "Parameter cannot be empty")
        c = Cell(parameter=par, case=new.case, value=clean(new.value), unit=clean(new.unit),
                 section=clean(new.section), kind=new.kind, source="human",
                 page=sh.pages[0] if sh.pages else 1, edited=True, reviewed=True)
        sh.cells.append(c)
        store.save(job)
        return c


@app.delete("/api/jobs/{job_id}/cells/{cell_id}", status_code=204)
def delete_cell(job_id: str, cell_id: str):
    job = get_job(job_id)
    with store.lock():
        sh, c = find_cell(job, cell_id)
        sh.cells.remove(c)
        store.save(job)


# -------------------------------------------------------------------- export
@app.get("/api/jobs/{job_id}/export.xlsx")
def export(job_id: str):
    job = get_job(job_id)
    if job.status != "done":
        raise HTTPException(409, "Job is not finished")
    with store.lock():
        data = build_xlsx(job)
    return Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": 'attachment; filename="process_conditions.xlsx"'})


# ------------------------------------------------- evaluation helpers (score.py)
def _csv(rows, head):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(head)
    w.writerows(rows)
    return Response(buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="data.csv"'})


@app.get("/api/jobs/{job_id}/model-output.csv")
def model_output(job_id: str):
    """What the MODEL said (human edits undone), with its own flags. Input for score.py as the candidate.
    Plain CSV for scripts: do not open in Excel, values are untrusted text."""
    job = get_job(job_id)
    rows = []
    for sh in job.sheets:
        for c in sh.cells:
            if c.source != "model":
                continue
            v, u = (c.original_value, c.original_unit) if c.edited else (c.value, c.unit)
            rows.append([sh.tag or sh.title or "(untitled)", c.parameter, c.case, v or "", u or "",
                         "; ".join(f.message for f in c.flags if f.severity != "info")])
    return _csv(rows, ["valve", "parameter", "case", "value", "unit", "flag"])


@app.get("/api/jobs/{job_id}/truth.csv")
def truth(job_id: str):
    """Cells a human reviewed or corrected, as ground truth for score.py. Reviewer anchoring bias applies."""
    job = get_job(job_id)
    rows = [[sh.tag or sh.title or "(untitled)", c.parameter, c.case, c.value or "", c.unit or ""]
            for sh in job.sheets for c in sh.cells if c.reviewed or c.edited]
    return _csv(rows, ["valve", "parameter", "case", "value", "unit"])


@app.exception_handler(Exception)
async def unhandled(_request: Request, exc: Exception):
    return JSONResponse({"detail": "Internal error"}, status_code=500)


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
