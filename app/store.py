"""Job store: in-memory dict + JSON on disk. Single process only (one uvicorn worker)."""
import re
import shutil
import threading
import uuid

from .config import DATA_DIR
from .schemas import Job

JOB_ID = re.compile(r"^[0-9a-f]{32}$")
_lock = threading.RLock()
_jobs: dict[str, Job] = {}


def jobs_dir():
    d = DATA_DIR / "jobs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def job_dir(job_id: str):
    if not JOB_ID.match(job_id):
        raise KeyError(job_id)
    return jobs_dir() / job_id


def create() -> Job:
    job = Job(id=uuid.uuid4().hex)
    (job_dir(job.id) / "uploads").mkdir(parents=True)
    (job_dir(job.id) / "pages").mkdir()
    with _lock:
        _jobs[job.id] = job
    save(job)
    return job


def get(job_id: str) -> Job:
    if not JOB_ID.match(job_id):
        raise KeyError(job_id)
    with _lock:
        if job_id in _jobs:
            return _jobs[job_id]
        p = job_dir(job_id) / "job.json"
        if not p.exists():
            raise KeyError(job_id)
        job = Job.model_validate_json(p.read_text("utf-8"))
        if job.status in ("queued", "processing"):  # the worker died with the previous process
            job.status, job.progress = "error", "Failed"
            job.error = "Interrupted by a server restart. Upload the files again."
            save(job)
        _jobs[job_id] = job
        return job


def save(job: Job):
    with _lock:
        tmp = job_dir(job.id) / "job.json.tmp"
        tmp.write_text(job.model_dump_json(), "utf-8")
        tmp.replace(job_dir(job.id) / "job.json")


def delete(job_id: str):
    with _lock:
        d = job_dir(job_id)
        _jobs.pop(job_id, None)
        if d.exists():
            shutil.rmtree(d)


def lock():
    return _lock
