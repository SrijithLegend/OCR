"""Background job: ingest -> plan -> extract -> validate -> sheets."""
import logging

from . import store
from .config import EXTRACTOR, MAX_PAGES, MODEL_DPI, PASSES, VIEW_DPI
from .consistency import to_sheets, validate
from .controller import plan_for
from .extractors import get_extractor
from .ingest import load_model_pages, prepare_views

log = logging.getLogger("amdp")


def run_job(job_id: str):
    job = store.get(job_id)
    try:
        job.status = "processing"
        store.save(job)
        extractor = get_extractor(EXTRACTOR)
        updir, pagedir = store.job_dir(job_id) / "uploads", store.job_dir(job_id) / "pages"
        sheets = []
        for f in job.files:
            job.progress = f"[{f.idx + 1}/{len(job.files)}] {f.name}: preparing pages"
            store.save(job)
            path = next(updir.glob(f"{f.idx}.*"))
            f.pages = prepare_views(path, f.idx, pagedir, VIEW_DPI, MAX_PAGES)
            pages = load_model_pages(path, MODEL_DPI, MAX_PAGES)
            plan = plan_for(pages, PASSES)
            job.progress = f"[{f.idx + 1}/{len(job.files)}] {f.name}: reading ({plan.passes} pass(es))"
            store.save(job)
            records, issues = extractor.extract(pages, plan, f.name)
            issues = issues + validate(records, f.name)
            sheets += to_sheets(f.idx, f.name, records, issues)
        job.sheets, job.status, job.progress = sheets, "done", "Done"
    except Exception as e:  # surfaced to the UI, trimmed; full trace in the log
        log.exception("job %s failed", job_id)
        job.status, job.error = "error", f"{type(e).__name__}: {str(e)[:300]}"
        job.progress = "Failed"
    store.save(job)
