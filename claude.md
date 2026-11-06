# Valve datasheet process-conditions extractor: implementation guide

Stack: Python 3.11+, FastAPI. Upload or drag-drop PDFs and images, get an editable table of the service/process conditions, export to Excel.

This file is self-contained. The **Appendix** holds the full source of a working MVP; section 2 unpacks it with one command.

---

## 0. Read this first

| Status | What |
|---|---|
| **Tested** | Upload (picker + drag-drop), PDF/image ingest, job lifecycle, flags, inline edit, add/delete rows, row review, Excel export with edits, CSV export for scoring, security checks. 28 pytest tests plus a headless-Chromium run (upload, drag-drop, edit, XSS probe, reload-resume, download, delete). |
| **Tested only with a stub** | Everything above ran with `EXTRACTOR=stub`, a canned reader that returns fixed fake sheets. It proves the app, not the extraction. |
| **NOT tested** | The live Claude reader (`claude_vision.py`) has never been run against the real API. Accuracy on real datasheets is **unmeasured**. Table Transformer, SAM and DeepSeek-OCR 2 are **not implemented**, and could not be run in the build environment (no GPU, no model downloads). |

Do not trust any accuracy claim, including your own 99% target, until section 11's evaluation says so on files you did not tune against.

---

## 1. What it does

1. Drop up to 20 files (PDF, PNG, JPG, TIFF; 50 MB each, 40 pages each by default).
2. A background job renders each page, reads the PDF text layer when there is one, and sends page + zoom tiles to Claude twice, independently. Cells where the two reads disagree get a third targeted re-read; majority decides.
3. Code (not the model) checks the result: P1 - dP = P2, Min <= Normal <= Max, unit sanity, plausible ranges, duplicates, unreadable cells.
4. The UI shows one table per valve sheet: Min / Normal / Max (plus Design / Single / Other when present). Cells are colour-coded: red = error, yellow = check, blue = you edited it, green = you reviewed it. Hover for the reason. Page link opens the source page.
5. You edit any cell, add or delete rows, press **OK** on rows you verified.
6. **Export to Excel** writes the current state, including your edits.

---

## 2. Quick start

```bash
# unpack the Appendix of this file into ./amdp
python - <<'EOF'
import re, pathlib, sys
src = pathlib.Path("IMPLEMENTATION.md").read_text(encoding="utf-8")   # this file's name
for path, body in re.findall(r"^### `([^`]+)`\n\n````[a-z]*\n(.*?)\n````", src, re.S | re.M):
    p = pathlib.Path("amdp") / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body + "\n", encoding="utf-8")
    print("wrote", p)
EOF

cd amdp
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

pytest -q                                   # 28 tests, no network, no API key

# demo UI with fake data (no API key needed)
EXTRACTOR=stub uvicorn app.main:app --host 127.0.0.1 --port 8000

# real extraction
# .env.example lists the variables. The app does NOT read .env itself: export them (or use `set -a; source .env; set +a`)
export ANTHROPIC_API_KEY=sk-ant-...
EXTRACTOR=claude PASSES=1 uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000.

**First live run checklist** (the Claude path has never run):
1. Use **one** short PDF and `PASSES=1`. Check cost in your Anthropic usage page before scaling.
2. Compare every extracted cell with the page. Use the page link in the table.
3. Only then raise `PASSES=2`.
4. Run **one uvicorn worker** (no `--workers`): jobs live in process memory plus JSON files.

Cost grows with `pages x (1 + tiles) x passes` images per file, plus the re-read. I have no measured number; check the usage page.

---

## 3. Deviations from the AMDP spec

| AMDP component | MVP decision | Why | When to add it |
|---|---|---|---|
| Table Transformer | Not in MVP | Claude reads layout directly; TATR adds a model to host and a new failure mode (bad cell boxes) before there is any evidence it helps. | Phase 2, as an ablation: keep only if it lowers the silent-error rate. |
| SAM refinement | **Off. I recommend never**, unless an ablation proves it. | SAM segments objects, not text. Hi-SAM / Char-SAM need extra machinery to do text at all. Valve sheets are clean tables. | Only if Phase 4 ablation shows a gain on your worst scans. |
| DeepSeek-OCR 2 | Not in MVP | Needs a GPU, flash-attn, a pinned environment. Useful as an independent second reader. | Phase 3. |
| LLM "reconstruction / verification" | LLM only transcribes and maps labels; code verifies | An LLM allowed to "fix" a value will confidently invent a plausible one. Checks that matter (P1 - dP = P2) are arithmetic. | Phase 3: LLM may propose a label mapping, but a provenance check in code rejects any value not present in an OCR/text-layer read. |
| Adaptive Document Controller (RL) | Rules (`controller.py`): dense text layer -> 2 tile columns; shrink tiling to fit the 100-image limit | Bandits/RL need thousands of labelled outcomes you do not have. | Phase 5, only if data exists and rules plateau. |
| Cross-Model Consistency Engine | Deterministic code (`consistency.py`): two reads, majority on a third, engineering checks | Voting and arithmetic need no model. | Extend to N engines in Phase 3. |
| Correction loop | Human review loop in the UI; edited cells keep the model's original value; CSV export feeds `score.py` | Human corrections are the only trustworthy training/eval signal. | Phase 5. |

**One independent second read is the single biggest accuracy lever here.** Two reads from the same model are less independent than two different engines. That is the real argument for Phase 3.

---

## 4. Architecture

```text
Browser (static HTML/JS)
   |  POST /api/jobs (multipart)         GET /api/jobs/{id} (poll 1.5 s)
   v
FastAPI main.py --- validates (suffix + magic bytes + size), saves as {idx}.{ext}, starts background task
   |
pipeline.run_job
   |-- ingest.prepare_views     -> pages/{file}_{page}.png   (UI viewer, 110 dpi)
   |-- ingest.load_model_pages  -> [(PIL image @300 dpi, text layer)]
   |-- controller.plan_for      -> Plan(passes, bands, cols)   rules, logged in plan.notes
   |-- extractor.extract        -> raw sheet dicts + reconciliation issues
   |      claude_vision: read x passes -> find_disputes -> targeted re-read -> majority vote
   |-- consistency.validate     -> engineering-check issues
   |-- consistency.to_sheets    -> Sheet/Cell models, flags attached per cell
   v
store (memory + data/jobs/{id}/job.json)  <--  PATCH/POST/DELETE cell edits
   v
excel.build_xlsx(job)   GET /export.xlsx      model-output.csv / truth.csv -> score.py
```

Extractor contract (every future engine implements this):

```python
class Extractor:
    name: str
    def extract(self, pages, plan, fname) -> tuple[list[dict], list[dict]]:
        """pages: [(PIL.Image, text_layer_str)]. Returns (valves, issues).
        valve = {sheet_title, tag, fluid, service, source_pages, has_values,
                 entries: [{section, row_no, parameter, canonical, kind, case,
                            case_label, value, unit, page}]}
        issue = {file, valve, section, parameter, case, severity, code, issue}"""
```

Rules that carry the accuracy and must not be "improved" casually:
- Columns are mapped by the **printed header text**, never by position (Max/Norm/Min order differs per vendor).
- Values are copied exactly as printed. Illegible means `UNREADABLE`, never a guess. Empty cell means no entry.
- A blank template yields `has_values=false`, no invented numbers.
- The model returns data only through a forced tool call, so there is no free-text parsing.

---

## 5. Repository layout

```text
amdp/
  requirements.txt  .env.example  .gitignore
  app/
    main.py            routes, upload hardening, security headers
    config.py          env-driven limits
    constants.py       cases, kinds, canonical parameter names, unit sets
    schemas.py         Pydantic models: Job, Sheet, Cell, Flag, patches
    store.py           job store (memory + JSON), job-id validation, restart recovery
    ingest.py          PDF/image -> view PNGs and model-resolution pages + text layer
    controller.py      rule-based routing plan
    pipeline.py        background job
    consistency.py     dispute detection, majority vote, engineering checks, flag attachment
    textutil.py        number/unit normalisation and comparison
    excel.py           workbook builder (edited state)
    extractors/        __init__ (registry), claude_vision.py, prompts.py, stub.py
    static/            index.html, app.js, app.css
  tests/               conftest.py, test_app.py
```

---

## 6. Data model

- `Cell`: `id, section, row_no, parameter (as printed), canonical, kind (condition|design|calculated), case (minimum|normal|maximum|design|single|other), value (string, as printed), unit, page, bbox (reserved for Phase 2+), flags[], source (model|human), original_value/original_unit (before the first human edit), edited, reviewed`.
- `Sheet`: one per valve datasheet found: `tag, title, fluid, service, pages, has_values, info, cells[], notes[]` (sheet-level flags).
- `Job`: `status (queued|processing|done|error), progress, files[], sheets[], error`.
- Values stay **strings** end to end so `1.4E+06`, `<75`, `N/A` survive. They become numbers only in the Excel export.

Flag codes: `unreadable, p_balance, order, no_unit, bad_pressure_unit, bad_temp_unit, units_differ, non_numeric, sg_range, below_abs_zero, negative_flow, duplicate_row, no_value, no_tag, no_values, pass_disagree, unresolved, not_in_pass2, only_pass2, dup_tag`.

`p_balance` flags all three cells (P1, P2, dP): the arithmetic cannot say which one is wrong.

---

## 7. API

| Method and path | Purpose |
|---|---|
| `GET /` | UI |
| `POST /api/jobs` | multipart `files`; returns `{id}` (202). 415 wrong type or spoofed extension, 413 too big, 422 empty or too many |
| `GET /api/jobs/{id}` | full job; poll until `done` or `error` |
| `DELETE /api/jobs/{id}` | delete uploads, page images and results |
| `GET /api/jobs/{id}/pages/{file_idx}/{page}.png` | page image for the viewer |
| `PATCH /api/jobs/{id}/cells/{cell_id}` | body `{value?, unit?, parameter?, reviewed?}`; a real change sets `edited` and `reviewed` and keeps the original |
| `POST /api/jobs/{id}/sheets/{sheet_id}/cells` | add a cell (`source=human`) |
| `DELETE /api/jobs/{id}/cells/{cell_id}` | delete a cell |
| `GET /api/jobs/{id}/export.xlsx` | Excel of current state (409 until done) |
| `GET /api/jobs/{id}/model-output.csv` | what the model said, human edits undone, with its flags; candidate input for `score.py` |
| `GET /api/jobs/{id}/truth.csv` | reviewed/edited cells as ground truth for `score.py` |

---

## 8. UI behaviour (acceptance checklist)

- [ ] Click-to-choose and drag-drop both upload; a file dropped outside the zone does not navigate the browser away.
- [ ] Progress text shows per-file stage; an error shows a trimmed message and returns to the drop zone.
- [ ] One card per sheet; header shows tag, title, fluid, service, source file, pages.
- [ ] Columns Min/Normal/Max always; Design/Single/Other only when that sheet has them; values land in the column named by the printed header, whatever the vendor's order.
- [ ] Inline edit on parameter, unit and every value cell; saved on blur; failure shows an error and does not silently drop the edit.
- [ ] Editing an empty cell in an existing row creates the cell.
- [ ] Colour states: error, check, edited, reviewed; tooltip shows flag text and the model's original value for edited cells.
- [ ] **OK** marks a row reviewed; **x** deletes a row (confirm); **Add row** at the table foot.
- [ ] "Show only rows needing review" filter; summary line counts open flags and edits.
- [ ] Page link opens the source page in a modal; Esc closes.
- [ ] Calculated values (Cv, % open, SPL...) sit in a collapsed section, not mixed with the process conditions.
- [ ] **Export to Excel** waits for in-flight saves before downloading.
- [ ] Reload resumes the last job (localStorage); **Delete my data** needs two clicks.
- [ ] All text rendered via `textContent`/`.value`; the e2e run injected `<img onerror>` and it stayed text.

---

## 9. Excel layout

| Sheet | Content |
|---|---|
| Process Conditions | one row per (sheet, section, parameter): Source File, Tag, Section, Row #, Parameter, Kind, Unit, Minimum..Other, Page, Status (`ERROR`, `CHECK`, `EDITED`, blank). Numbers are real numbers; fills: blue edited, green reviewed, red error, yellow check |
| Sheet Info | per sheet: tag, title, item, service, fluid, line, project, template, pages, has values, cell count |
| Long Format | one row per cell (good for pivots) |
| Review & Edits | every flag, plus every human edit as `old -> new`; flags on reviewed cells are marked resolved |

Strings starting with `= + - @` are written as text so nothing typed in the UI or read from a hostile PDF can run as a formula (tested).

---

## 10. Security controls (all in the MVP)

- Upload: suffix allow-list **and** magic-byte match (a renamed `.exe` or HTML is rejected), streamed size limit, file count limit, page limit, empty-file rejection, password-protected PDFs rejected with a clear message.
- Stored under generated names (`{idx}.{ext}`); the client filename is display-only, truncated, never a path.
- Job ids are 128-bit random, regex-validated before touching the filesystem (path traversal tested).
- CSP `default-src 'self'`, no inline script or style; `nosniff`; `no-referrer`; `no-store`; frame-ancestors none.
- Rendering with `textContent` only; Excel formula-injection guard; generic 500 body (no stack traces to the client).
- Delete endpoint and UI button remove all data for a job.
- **Not included, on purpose:** authentication and multi-user isolation. The server binds to `127.0.0.1`. If you expose it, put it behind your reverse proxy with auth first; anyone with a job id can read that job.
- **Data leaves your machine.** With `EXTRACTOR=claude` every page image goes to the Anthropic API. Decide whether the customer data allows that before the first real file. For a confidential-data policy, check Anthropic's current data-handling terms for your account type; I have not verified them for your plan.

---

## 11. Evaluation: the only way to know the accuracy

1. Pick 5 to 10 real sheets across different vendor formats, **original PDFs or 300 dpi scans** (screenshots of 250 to 500 px cap accuracy; no model recovers pixels that are not there).
2. Run them. Review every cell in the UI against the page, press OK, correct errors.
3. Download `truth.csv` (your corrected values) and `model-output.csv` (what the model said).
4. Score:

```bash
python score.py truth.csv model-output.csv
```

`score.py` (delivered earlier in this conversation) reports accuracy, **silent errors** (wrong or missing and not flagged), flag recall and precision, and unflagged accuracy.

Targets:
- **Silent errors = 0** is the real goal. A wrong digit nobody flagged is the failure that costs money.
- 99% accuracy on a sample of 200 cells means roughly 2 errors; one more error moves it a full point. Small samples cannot prove 99%.

Caveats:
- `truth.csv` comes from reviewing the model's own output, so reviewers anchor on it and miss errors. For the final test, **fill the truth by hand from the page first**, then score.
- Tune on one set, report on another. A prompt tweaked against 5 sheets will score well on those 5.
- Every ablation in Phase 2+ is judged by this same score, same files.

---

## 12. Roadmap

| Phase | Do | Exit criterion |
|---|---|---|
| 0 | This MVP | 28 tests green, e2e green (done) |
| 1 | Live Claude run on your 5 to 10 sheets; baseline score | Baseline silent-error rate recorded. If it is already 0 on a fresh set, stop here |
| 2 | Table Transformer: crop each table, send the crop as an extra zoom image | Silent-error rate lower than baseline on the **same** files, else remove it |
| 3 | DeepSeek-OCR 2 as an independent second reader on a GPU box; consistency engine votes across engines; LLM label mapping with provenance check | Disagreements flagged, never silently resolved; silent errors lower |
| 4 | SAM refinement | Keep only if the ablation shows a gain. Expect none |
| 5 | Learned controller | Only with >1000 reviewed outcomes and rules plateaued |
| 6 | Production: auth, Postgres, task queue (Celery/Redis), object storage, MLflow | Needed only when more than one user or a restart-proof queue is required |

A change that does not improve the score on held-out files does not ship.

---

## 13. Integration notes for later phases

### Adding an engine
Implement the extractor contract (section 4), register it in `extractors/__init__.py`, set `EXTRACTOR=name`. For voting across engines, reuse `find_disputes` / `apply_adjudication` in `consistency.py`; a disagreement the vote cannot settle stays an `unresolved` error.

### Table Transformer (Phase 2). Snippet from the Hugging Face docs, not run here
```python
from transformers import AutoImageProcessor, TableTransformerForObjectDetection
import torch
from PIL import Image

image = Image.open("page.png").convert("RGB")
image_processor = AutoImageProcessor.from_pretrained("microsoft/table-transformer-detection")
model = TableTransformerForObjectDetection.from_pretrained("microsoft/table-transformer-detection")

inputs = image_processor(images=image, return_tensors="pt")
outputs = model(**inputs)

target_sizes = torch.tensor([image.size[::-1]])        # (height, width)
results = image_processor.post_process_object_detection(outputs, threshold=0.9, target_sizes=target_sizes)[0]
for score, label, box in zip(results["scores"], results["labels"], results["boxes"]):
    print(model.config.id2label[label.item()], round(score.item(), 3), [round(i, 2) for i in box.tolist()])
```
- Boxes come back as `(xmin, ymin, xmax, ymax)` pixels. Crop with a small margin and feed the crop to the existing reader as an extra tile.
- Structure recognition uses the same class with `microsoft/table-transformer-structure-recognition`. The docs did not list its label names: **read them from `model.config.id2label`**, do not assume.
- The docs note that padding the image matters for detection. If loading fails, check whether `timm` is required; the docs did not say.
- Store the crop box in `Cell.bbox` so the UI can later highlight the cell on the page.

### DeepSeek-OCR 2 (Phase 3). From the project README, not run here
```python
from transformers import AutoModel, AutoTokenizer
import torch, os
os.environ["CUDA_VISIBLE_DEVICES"] = "0"
model_name = "deepseek-ai/DeepSeek-OCR-2"

tokenizer = AutoTokenizer.from_pretrained(model_name, trust_remote_code=True)
model = AutoModel.from_pretrained(model_name, _attn_implementation="flash_attention_2",
                                  trust_remote_code=True, use_safetensors=True)
model = model.eval().cuda().to(torch.bfloat16)

prompt = "<image>\n<|grounding|>Convert the document to markdown. "
res = model.infer(tokenizer, prompt=prompt, image_file="page.png", output_path="out/",
                  base_size=1024, image_size=768, crop_mode=True, save_results=True)
```
- Needs an NVIDIA GPU, CUDA, and `flash-attn` (the README pins `flash-attn==2.7.3`). `trust_remote_code=True` runs code from the model repo: pin a revision and read it first.
- The README describes the output as markdown with grounding. Parse the table, map cells to your schema in code, and feed the values to the consistency engine as a second reader. Check the model licence for your use.

### SAM (Phase 4)
I have **no verified API snippet** for it and recommend not building it. If an ablation ever justifies it, take the loading code from the current official docs of whichever SAM variant you choose, and give it one job: refining a cell crop before the reader sees it. It must never produce a value.

---

## 14. Known limits

- Single process: jobs are in memory plus `data/jobs/*/job.json`. A restart marks unfinished jobs as failed (tested); re-upload.
- No auth, one trust domain (section 10).
- A row that continues on another page links to the first cell's page.
- Output is capped by the model's max tokens; a very large document fails with a message to split it. Image limit is 100 per request (the controller shrinks tiling, then rejects).
- `UNREADABLE` appears in the table as text and is red until you correct it.
- The model can still misread a digit **both** times in the same way. Flags catch inconsistent reads and broken arithmetic, not consistent misreads. Only the Phase 1 score tells you how often that happens.
- Vendor-reported benchmarks for any OCR model are a shortlist, not evidence for your sheets.

---

# Appendix: source files

### `requirements.txt`

````
fastapi>=0.110
uvicorn[standard]>=0.29
python-multipart>=0.0.9
pydantic>=2.6
pymupdf>=1.24
pillow>=10.0
openpyxl>=3.1
anthropic>=0.40

# dev / tests
pytest>=8.0
httpx>=0.27
````

### `.env.example`

````
# Which extractor runs: "claude" (needs ANTHROPIC_API_KEY) or "stub" (canned demo data, no network)
EXTRACTOR=claude
ANTHROPIC_API_KEY=sk-ant-...
CLAUDE_MODEL=claude-sonnet-5-5

# 1 = one read, 2 = two independent reads + a third read on disputed cells
PASSES=2

DATA_DIR=data
MAX_UPLOAD_MB=50
MAX_FILES=20
MAX_PAGES=40
MODEL_DPI=300
VIEW_DPI=110
````

### `.gitignore`

````
data/
.env
__pycache__/
.pytest_cache/
````

### `app/__init__.py`

````python

````

### `app/config.py`

````python
import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "data")).resolve()
EXTRACTOR = os.environ.get("EXTRACTOR", "claude")  # claude | stub
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")
PASSES = max(1, min(2, int(os.environ.get("PASSES", "2"))))

MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "50"))
MAX_FILES = int(os.environ.get("MAX_FILES", "20"))
MAX_PAGES = int(os.environ.get("MAX_PAGES", "40"))  # per file; bounds cost and request size
MODEL_DPI = int(os.environ.get("MODEL_DPI", "300"))  # resolution sent to the model
VIEW_DPI = int(os.environ.get("VIEW_DPI", "110"))    # resolution of the page images in the UI

ALLOWED_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff"}
````

### `app/constants.py`

````python
CASES = ["minimum", "normal", "maximum", "design", "single", "other"]
CASE_TITLES = {"minimum": "Minimum", "normal": "Normal", "maximum": "Maximum",
               "design": "Design", "single": "Single", "other": "Other"}
KINDS = ["condition", "design", "calculated"]

# Canonical parameter names. Printed labels vary per vendor; these make sheets comparable.
CANON = [
    "fluid_name", "fluid_state", "flow_rate_mass", "flow_rate_volume", "flow_rate_gas_std",
    "inlet_pressure", "outlet_pressure", "pressure_drop", "shutoff_pressure", "design_pressure",
    "vapor_pressure", "critical_pressure", "inlet_temperature", "outlet_temperature",
    "design_temperature", "critical_temperature", "specific_gravity", "density",
    "molecular_weight", "viscosity", "compressibility_factor", "specific_heat_ratio",
    "specific_volume", "cavitation_factor", "percent_vapour", "percent_solids",
    "corrosion_allowance", "other",
]
PRESSURE = {"inlet_pressure", "outlet_pressure", "pressure_drop", "shutoff_pressure",
            "design_pressure", "vapor_pressure", "critical_pressure"}
TEMP = {"inlet_temperature", "outlet_temperature", "design_temperature", "critical_temperature"}
FLOW = {"flow_rate_mass", "flow_rate_volume", "flow_rate_gas_std"}
NON_NUMERIC = {"fluid_name", "fluid_state", "other", "corrosion_allowance"}
P_BASES = {"bar", "mbar", "kpa", "mpa", "pa", "psi", "kg/cm2", "kgf/cm2", "atm"}
T_UNITS = {"c", "f", "k", "r"}
````

### `app/schemas.py`

````python
from __future__ import annotations

import time
import uuid
from typing import Literal, Optional

from pydantic import BaseModel, Field

Severity = Literal["error", "warning", "info"]
Case = Literal["minimum", "normal", "maximum", "design", "single", "other"]
Kind = Literal["condition", "design", "calculated"]


def new_id() -> str:
    return uuid.uuid4().hex[:8]


class Flag(BaseModel):
    severity: Severity
    code: str
    message: str


class Cell(BaseModel):
    id: str = Field(default_factory=new_id)
    section: Optional[str] = None
    row_no: Optional[int] = None
    parameter: str
    canonical: str = "other"
    kind: Kind = "condition"
    case: Case
    value: Optional[str] = None
    unit: Optional[str] = None
    page: int = 1
    bbox: Optional[list[float]] = None      # [x0, y0, x1, y1] in original page pixels (Phase 2+)
    flags: list[Flag] = []
    source: str = "model"
    original_value: Optional[str] = None    # value before the first human edit
    original_unit: Optional[str] = None
    edited: bool = False
    reviewed: bool = False                  # a human accepted or corrected this cell


class Sheet(BaseModel):
    id: str = Field(default_factory=new_id)
    file_idx: int
    file_name: str
    title: Optional[str] = None
    tag: Optional[str] = None
    fluid: Optional[str] = None
    service: Optional[str] = None
    pages: list[int] = []
    has_values: bool = True
    info: dict[str, str] = {}
    cells: list[Cell] = []
    notes: list[Flag] = []                  # sheet-level flags


class FileInfo(BaseModel):
    idx: int
    name: str
    pages: int = 0


class Job(BaseModel):
    id: str
    status: Literal["queued", "processing", "done", "error"] = "queued"
    progress: str = "Queued"
    files: list[FileInfo] = []
    sheets: list[Sheet] = []
    error: Optional[str] = None
    created: float = Field(default_factory=time.time)


class CellPatch(BaseModel):
    value: Optional[str] = None
    unit: Optional[str] = None
    parameter: Optional[str] = None
    reviewed: Optional[bool] = None


class NewCell(BaseModel):
    parameter: str
    case: Case
    value: Optional[str] = None
    unit: Optional[str] = None
    section: Optional[str] = None
    kind: Kind = "condition"
````

### `app/textutil.py`

````python
import math
import re


def norm(s) -> str:
    return re.sub(r"\s+", " ", str(s if s is not None else "")).strip().lower()


def unit_norm(u) -> str:
    s = norm(u)
    for a, b in (("²", "2"), ("³", "3"), ("°", ""), ("º", ""), ("deg", "")):
        s = s.replace(a, b)
    return re.sub(r"[\s_()\[\].]", "", s)


_NUM = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")
_THOU = re.compile(r"^[+-]?\d{1,3}(,\d{3})+(\.\d+)?$")


def to_num(s):
    """float for plain numbers (also '1,500' and '1.4E+06'); None for anything else."""
    if s is None:
        return None
    t = str(s).strip()
    if _THOU.match(t):
        t = t.replace(",", "")
    return float(t) if _NUM.match(t) else None


def same_value(a, b) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    na, nb = to_num(a), to_num(b)
    if na is not None and nb is not None:
        return math.isclose(na, nb, rel_tol=1e-9, abs_tol=1e-12)
    return norm(a) == norm(b)
````

### `app/store.py`

````python
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
````

### `app/ingest.py`

````python
"""Turn an uploaded PDF or image into (a) page PNGs for the UI and (b) model-resolution pages.

The original upload is never modified. Everything downstream refers to page numbers (1-based).
"""
import math
from pathlib import Path

from PIL import Image, ImageOps, ImageSequence

try:
    import pymupdf as fitz  # current name
except ImportError:  # older PyMuPDF
    import fitz


def layout_text(page) -> str:
    """Rebuild text rows from word boxes; ' | ' marks a column gap. Empty for scanned pages."""
    words = page.get_text("words")
    if not words:
        return ""
    words = sorted(words, key=lambda w: ((w[1] + w[3]) / 2, w[0]))
    rows = []
    for w in words:
        yc, h = (w[1] + w[3]) / 2, max(w[3] - w[1], 1.0)
        if rows and abs(yc - rows[-1]["yc"]) <= 0.5 * max(h, rows[-1]["h"]):
            r = rows[-1]
            r["w"].append(w)
            r["yc"] = (r["yc"] * (len(r["w"]) - 1) + yc) / len(r["w"])
            r["h"] = max(r["h"], h)
        else:
            rows.append({"yc": yc, "h": h, "w": [w]})
    lines = []
    for r in rows:
        ws = sorted(r["w"], key=lambda w: w[0])
        parts = [ws[0][4]]
        for a, b in zip(ws, ws[1:]):
            parts.append(" | " if b[0] - a[2] > 1.2 * r["h"] else " ")
            parts.append(b[4])
        lines.append("".join(parts))
    return "\n".join(lines)


def _render(page, dpi) -> Image.Image:
    r = page.rect
    scale = dpi / 72
    if r.width * r.height * scale * scale > 40e6:  # cap at ~40 megapixels
        scale = math.sqrt(40e6 / (r.width * r.height))
    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), colorspace=fitz.csRGB, alpha=False)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


def _open_pdf(path: Path, max_pages: int):
    doc = fitz.open(path)
    if doc.needs_pass:
        raise ValueError("The PDF is password-protected.")
    if doc.page_count > max_pages:
        raise ValueError(f"{doc.page_count} pages exceeds the limit of {max_pages} per file.")
    return doc


def _image_pages(path: Path, max_pages: int):
    img = Image.open(path)
    frames = [ImageOps.exif_transpose(f.copy()).convert("RGB") for f in ImageSequence.Iterator(img)]
    if len(frames) > max_pages:
        raise ValueError(f"{len(frames)} pages exceeds the limit of {max_pages} per file.")
    return frames


def load_model_pages(path: Path, dpi: int, max_pages: int):
    """-> list of (PIL image at model resolution, text layer or '')"""
    if path.suffix.lower() == ".pdf":
        doc = _open_pdf(path, max_pages)
        return [(_render(p, dpi), layout_text(p)) for p in doc]
    return [(f, "") for f in _image_pages(path, max_pages)]


def prepare_views(path: Path, file_idx: int, pages_dir: Path, view_dpi: int, max_pages: int) -> int:
    """Write view PNGs `{file_idx}_{page}.png` for the UI. Returns the page count."""
    pages_dir.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() == ".pdf":
        doc = _open_pdf(path, max_pages)
        for n, p in enumerate(doc, 1):
            _render(p, view_dpi).save(pages_dir / f"{file_idx}_{n}.png")
        return doc.page_count
    frames = _image_pages(path, max_pages)
    for n, f in enumerate(frames, 1):
        f = f.copy()
        f.thumbnail((1800, 1800))
        f.save(pages_dir / f"{file_idx}_{n}.png")
    return len(frames)
````

### `app/controller.py`

````python
"""Adaptive Document Controller, stage 1 (AMDP section 13.4): rule-based routing, no learning.

It decides how a file is read, and it logs why. The one hard constraint is the model API's
limit of 100 images per request.
"""
from dataclasses import dataclass, field

MAX_IMAGES_PER_REQUEST = 100
DENSE_TEXT_CHARS = 2500  # a text layer this long on one page means a dense, often two-column sheet


@dataclass
class Plan:
    passes: int
    bands: int          # vertical zoom tiles per page (0 = none)
    cols: int           # horizontal zoom tiles per page
    adjudicate: bool = True
    notes: list = field(default_factory=list)

    def images_per_page(self):
        tiles = (max(self.bands, 1) * max(self.cols, 1)) if (self.bands > 0 or self.cols > 1) else 0
        return 1 + tiles


def plan_for(pages, default_passes: int) -> Plan:
    n = len(pages)
    dense = any(len(t) > DENSE_TEXT_CHARS for _, t in pages)
    plan = Plan(passes=default_passes, bands=3, cols=2 if dense else 1)
    if dense:
        plan.notes.append("dense text layer: 2 tile columns")
    # shrink tiling until the request fits the image limit
    while n * plan.images_per_page() > MAX_IMAGES_PER_REQUEST and (plan.cols > 1 or plan.bands > 0):
        if plan.cols > 1:
            plan.cols = 1
        else:
            plan.bands -= 1
        plan.notes.append(f"{n} pages: reduced tiling to {plan.bands}x{plan.cols}")
    if n * plan.images_per_page() > MAX_IMAGES_PER_REQUEST:
        raise ValueError(f"{n} pages cannot fit one request; split the file.")
    return plan
````

### `app/consistency.py`

````python
"""Cross-model consistency engine (AMDP section 10), deterministic parts only.

* reconcile two independent reads (find_disputes -> adjudicate -> apply_adjudication)
* engineering checks (validate): P1 - dP = P2, ordering, units, ranges, duplicates
* to_sheets: turn raw dicts + issues into Sheet/Cell models with per-cell flags

An issue is a dict: file, valve, section, parameter, case, severity, code, issue.
"""
import copy
import re

from .constants import CASES, FLOW, KINDS, NON_NUMERIC, P_BASES, PRESSURE, T_UNITS, TEMP
from .schemas import Cell, Flag, Sheet
from .textutil import norm, same_value, to_num, unit_norm

P_UNIT = re.compile(r"(bar|psi|kpa|mpa|pa|kg/?cm|kgf|atm|torr|mmhg|mmwc|inh|ksi)")


def normalize(valves):
    for i, v in enumerate(valves, 1):
        tag = (v.get("tag") or "").strip()
        title = (v.get("sheet_title") or "").strip()
        pages = v.get("source_pages") or []
        v["_name"] = tag or (f"{title} (p{min(pages)})" if title and pages else f"(untitled sheet {i})")
        v["_id"] = norm(v["_name"])
        v["entries"] = v.get("entries") or []
    return valves


def vkey(v):
    return v["_id"]


def issue(fname, valve, section, parameter, case, severity, code, msg):
    return {"file": fname, "valve": valve, "section": section or "", "parameter": parameter or "",
            "case": case or "", "severity": severity, "code": code, "issue": msg}


# ----------------------------------------------------------- reconcile two reads
def keyed(v):
    out, seen = {}, {}
    for e in v["entries"]:
        k = (norm(e.get("section")), norm(e["parameter"]), e["case"])
        n = seen.get(k, 0)
        seen[k] = n + 1
        out[k + (n,)] = e
    return out


def find_disputes(a_valves, b_valves, fname, issues):
    disputes = []
    idx_b = {v["_id"]: v for v in b_valves}
    for va in a_valves:
        vb = idx_b.pop(va["_id"], None)
        if vb is None:
            issues.append(issue(fname, va["_name"], "", "", "", "error", "not_in_pass2",
                                "Sheet not found in the second read"))
            continue
        ka, kb = keyed(va), keyed(vb)
        for k in sorted(set(ka) | set(kb), key=str):
            a, b = ka.get(k), kb.get(k)
            if a and b and same_value(a.get("value"), b.get("value")) \
                    and unit_norm(a.get("unit")) == unit_norm(b.get("unit")):
                continue
            disputes.append({"id": len(disputes), "valve": va, "a": a, "b": b})
    for vb in idx_b.values():
        issues.append(issue(fname, vb["_name"], "", "", "", "error", "only_pass2",
                            "Sheet only found in the second read"))
    return disputes


def dispute_questions(disputes):
    lines = []
    for d in disputes:
        ref = d["a"] or d["b"]
        lines.append(f'Q{d["id"]}: sheet "{d["valve"]["_name"]}", page {ref.get("page")}, '
                     f'section "{ref.get("section") or "-"}", row {ref.get("row_no") or "-"} '
                     f'"{ref["parameter"]}", column "{ref.get("case_label") or ref["case"]}": '
                     f'value and unit as printed?')
    return lines


def apply_adjudication(disputes, answers, fname, issues):
    """Majority vote of read 1, read 2 and the re-read. An unmatched re-read stays an error."""
    for d in disputes:
        a, b, v = d["a"], d["b"], d["valve"]
        ref = a or b
        sec, par, case, name = ref.get("section"), ref["parameter"], ref["case"], v["_name"]
        ans = answers.get(d["id"]) or {}
        av, bv = (a or {}).get("value"), (b or {}).get("value")
        ua, ub = (a or {}).get("unit"), (b or {}).get("unit")
        rv = ans.get("value")
        readable = rv is not None and "UNREADABLE" not in str(rv).upper()
        win = None
        if readable and a and av is not None and same_value(rv, av):
            win = "a"
        elif readable and b and bv is not None and same_value(rv, bv):
            win = "b"
        if win is None:
            if a is None and b is not None:
                v["entries"].append(copy.deepcopy(b))
            issues.append(issue(fname, name, sec, par, case, "error", "unresolved",
                                f"Unresolved: read 1 {av!r}, read 2 {bv!r}, re-read {rv!r}"))
            continue
        t = a if a else copy.deepcopy(b)
        if not a:
            v["entries"].append(t)
        if win == "b":
            t["value"], t["unit"] = bv, ub
        msg = f"Reads disagreed (1: {av!r}, 2: {bv!r}); re-read decided {t['value']!r}"
        if a and b and unit_norm(ua) != unit_norm(ub):
            t["unit"] = ub if unit_norm(ans.get("unit")) == unit_norm(ub) else ua
            msg += f"; unit differed ({ua!r} vs {ub!r}) -> {t['unit']!r}"
        issues.append(issue(fname, name, sec, par, case, "warning", "pass_disagree", msg))


# ------------------------------------------------------------------ validation
def _pressure_base(un):
    if un in P_BASES:
        return un
    if un[:-1] in P_BASES and un[-1:] in ("g", "a"):
        return un[:-1]
    return un


def validate(valves, fname):
    issues = []

    def add(v, e, sev, code, msg):
        issues.append(issue(fname, v["_name"], e.get("section") if e else "",
                            e["parameter"] if e else "", e["case"] if e else "", sev, code, msg))

    seen_names = {}
    for v in valves:
        ents = v["entries"]
        seen_names[v["_id"]] = seen_names.get(v["_id"], 0) + 1
        if not v.get("has_values") and not ents:
            add(v, None, "info", "no_values", "No filled process-condition values (blank template or no such block)")
        elif not v.get("has_values") and ents:
            add(v, None, "warning", "blank_with_entries", "Sheet marked blank but entries were returned")
        if v.get("has_values") and not (v.get("tag") or "").strip():
            add(v, None, "warning", "no_tag", "Tag missing")

        seen = set()
        for e in ents:
            key = (norm(e.get("section")), norm(e["parameter"]), e["case"])
            if key in seen:
                add(v, e, "warning", "duplicate_row", "Duplicate entry for the same row and column")
            seen.add(key)
            val = e.get("value")
            if val is None or str(val).strip() == "":
                add(v, e, "warning", "no_value", "Entry has no value")
                continue
            if "UNREADABLE" in str(val).upper():
                add(v, e, "error", "unreadable", "Model could not read this cell")
                continue
            canon, un, num = e.get("canonical", "other"), unit_norm(e.get("unit")), to_num(val)
            if canon not in NON_NUMERIC and num is None:
                add(v, e, "warning", "non_numeric", f"Non-numeric value {val!r}; confirm against the sheet")
            if num is None:
                continue
            if canon in PRESSURE | TEMP | FLOW and not un:
                add(v, e, "warning", "no_unit", "No unit captured")
            if canon in PRESSURE and un and not P_UNIT.search(un):
                add(v, e, "warning", "bad_pressure_unit", f"Unit {e.get('unit')!r} is not a pressure unit")
            if canon in TEMP and un and un not in T_UNITS:
                add(v, e, "warning", "bad_temp_unit", f"Unit {e.get('unit')!r} is not a temperature unit")
            if canon in TEMP and un == "c" and num < -273.15:
                add(v, e, "error", "below_abs_zero", "Temperature below absolute zero")
            if canon in FLOW and num < 0:
                add(v, e, "error", "negative_flow", "Negative flow rate")
            if canon == "specific_gravity" and not (0.05 <= num <= 20):
                add(v, e, "warning", "sg_range", "Specific gravity outside 0.05-20")

        # P1 - dP = P2, per column, only when the three rows are unambiguous and units compatible
        for case in CASES:
            d, dup = {}, set()
            for e in ents:
                if e["case"] == case and e.get("kind", "condition") == "condition" and \
                        e.get("canonical") in ("inlet_pressure", "outlet_pressure", "pressure_drop"):
                    if e["canonical"] in d:
                        dup.add(e["canonical"])
                    d[e["canonical"]] = e
            if len(d) == 3 and not dup:
                p1, p2, dp = d["inlet_pressure"], d["outlet_pressure"], d["pressure_drop"]
                n1, n2, nd = (to_num(x["value"]) for x in (p1, p2, dp))
                u1, u2, ud = (unit_norm(x.get("unit")) for x in (p1, p2, dp))
                if None not in (n1, n2, nd) and u1 == u2 and \
                        _pressure_base(u1) == _pressure_base(ud) and \
                        abs((n1 - nd) - n2) > max(0.02 * abs(n1), 0.05):
                    for x in (p1, p2, dp):  # flag all three: the check cannot tell which one is wrong
                        add(v, x, "error", "p_balance",
                            f"P1 {n1} - dP {nd} != P2 {n2} ({case}); one of the three is misread")

        # min <= normal <= max for flow and temperature
        for canon in FLOW | TEMP:
            by = {e["case"]: e for e in ents if e.get("canonical") == canon
                  and e.get("kind", "condition") == "condition" and to_num(e.get("value")) is not None}
            if {"minimum", "normal", "maximum"} <= set(by) and \
                    len({unit_norm(by[c].get("unit")) for c in by}) == 1:
                lo, no, hi = (to_num(by[c]["value"]) for c in ("minimum", "normal", "maximum"))
                if not (lo <= no <= hi):
                    add(v, by["normal"], "warning", "order", f"Min/Normal/Max not ordered: {lo}, {no}, {hi}")

        rows = {}
        for e in ents:
            rows.setdefault((norm(e.get("section")), norm(e["parameter"])), []).append(e)
        for es in rows.values():
            if len({unit_norm(e.get("unit")) for e in es if e.get("unit")}) > 1:
                add(v, es[0], "warning", "units_differ", "Different units across columns of the same row")

    for k, n in seen_names.items():
        if n > 1:
            issues.append(issue(fname, k.upper(), "", "", "", "warning", "dup_tag",
                                f"Tag appears {n} times in one file - possible split or duplicate"))
    return issues


# ------------------------------------------------------------ raw dicts -> models
def _enum(value, allowed, default):
    return value if value in allowed else default


def to_sheets(file_idx, fname, valves, issues, source="model"):
    sheets = []
    for v in valves:
        cells = [Cell(section=e.get("section"), row_no=e.get("row_no"), parameter=e["parameter"],
                      canonical=e.get("canonical") or "other",
                      kind=_enum(e.get("kind"), KINDS, "condition"),
                      case=_enum(e.get("case"), CASES, "other"),
                      value=None if e.get("value") is None else str(e["value"]),
                      unit=e.get("unit"), page=e.get("page") or 1, source=source)
                 for e in v["entries"]]
        info = {k: str(v[k]) for k in ("item_no", "line_no", "project", "format_name") if v.get(k)}
        sheet = Sheet(file_idx=file_idx, file_name=fname, title=v.get("sheet_title"),
                      tag=v.get("tag"), fluid=v.get("fluid"), service=v.get("service"),
                      pages=v.get("source_pages") or [], has_values=bool(v.get("has_values")),
                      info=info, cells=cells)
        by = {}
        for c in cells:
            by.setdefault((norm(c.section), norm(c.parameter)), []).append(c)
        for i in (x for x in issues if x["valve"] == v["_name"]):
            flag = Flag(severity=i["severity"], code=i.get("code", "check"), message=i["issue"])
            if not i.get("parameter"):
                sheet.notes.append(flag)
                continue
            targets = [c for c in by.get((norm(i.get("section")), norm(i["parameter"])), [])
                       if not i.get("case") or c.case == i["case"]]
            if not targets:
                sheet.notes.append(flag)
            for c in targets:
                c.flags.append(flag)
        sheets.append(sheet)
    return sheets
````

### `app/pipeline.py`

````python
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
            valves, issues = extractor.extract(pages, plan, f.name)
            issues = issues + validate(valves, f.name)
            sheets += to_sheets(f.idx, f.name, valves, issues)
        job.sheets, job.status, job.progress = sheets, "done", "Done"
    except Exception as e:  # surfaced to the UI, trimmed; full trace in the log
        log.exception("job %s failed", job_id)
        job.status, job.error = "error", f"{type(e).__name__}: {str(e)[:300]}"
        job.progress = "Failed"
    store.save(job)
````

### `app/excel.py`

````python
"""Excel export built from the CURRENT (edited) job state. Returns bytes."""
import io

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from .constants import CASE_TITLES, CASES
from .schemas import Job
from .textutil import norm, to_num

HDR_FILL = PatternFill("solid", fgColor="1F3864")
HDR_FONT = Font(bold=True, color="FFFFFF")
RED, YELLOW = PatternFill("solid", fgColor="FFC7CE"), PatternFill("solid", fgColor="FFEB9C")
BLUE, GREEN = PatternFill("solid", fgColor="BDD7EE"), PatternFill("solid", fgColor="C6EFCE")
THIN = Side(style="thin", color="BFBFBF")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
SEV = {"error": 2, "warning": 1, "info": 0}


def put(ws, row, col, value):
    """Write a cell. Numbers become numbers; any other string is forced to text so a value
    like =HYPERLINK(...) typed into the UI can never execute as a formula."""
    c = ws.cell(row, col)
    if isinstance(value, str):
        n = to_num(value)
        if n is not None:
            c.value = n
        else:
            c.value = value
            if value[:1] in ("=", "+", "-", "@"):
                c.data_type = "s"
    else:
        c.value = value
    return c


def _style(ws, widths):
    for c in ws[1]:
        c.fill, c.font = HDR_FILL, HDR_FONT
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    for row in ws.iter_rows(min_row=1):
        for c in row:
            c.border = BORDER
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions


def cell_fill(c):
    """Human state wins over model flags: edited (blue) > reviewed (green) > error > warning."""
    if c.edited:
        return BLUE
    if c.reviewed:
        return GREEN
    s = max((SEV[f.severity] for f in c.flags), default=-1)
    return RED if s == 2 else YELLOW if s == 1 else None


def build_xlsx(job: Job) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Process Conditions"
    head = ["Source File", "Tag", "Section", "Row #", "Parameter", "Kind", "Unit"] \
        + [CASE_TITLES[c] for c in CASES] + ["Page", "Status"]
    for j, h in enumerate(head, 1):
        ws.cell(1, j, h)
    r = 1
    for sh in job.sheets:
        groups = {}
        for c in sh.cells:
            groups.setdefault((norm(c.section), norm(c.parameter), c.kind), []).append(c)
        for cells in groups.values():
            r += 1
            first = cells[0]
            units = []
            for c in cells:
                if c.unit and c.unit not in units:
                    units.append(c.unit)
            for j, v in enumerate([sh.file_name, sh.tag or sh.title or "", first.section or "", first.row_no,
                                   first.parameter, first.kind, " / ".join(units) or None], 1):
                put(ws, r, j, v)
            status = ""
            for c in cells:
                j = 8 + CASES.index(c.case)
                put(ws, r, j, c.value)
                f = cell_fill(c)
                if f:
                    ws.cell(r, j).fill = f
                if not c.reviewed and not c.edited and any(x.severity == "error" for x in c.flags):
                    status = "ERROR"
                elif not c.reviewed and not c.edited and any(x.severity == "warning" for x in c.flags) \
                        and status != "ERROR":
                    status = "CHECK"
            if not status and any(c.edited for c in cells):
                status = "EDITED"
            put(ws, r, 8 + len(CASES), first.page)
            put(ws, r, 9 + len(CASES), status)
            if status in ("ERROR", "CHECK"):
                ws.cell(r, 9 + len(CASES)).fill = RED if status == "ERROR" else YELLOW
    _style(ws, [20, 16, 22, 7, 38, 11, 14] + [14] * len(CASES) + [6, 9])

    w2 = wb.create_sheet("Sheet Info")
    for j, h in enumerate(["Source File", "Tag", "Title", "Item", "Service", "Fluid", "Line No",
                           "Project", "Template / Vendor", "Pages", "Has Values", "Cells"], 1):
        w2.cell(1, j, h)
    for n, sh in enumerate(job.sheets, 2):
        row = [sh.file_name, sh.tag, sh.title, sh.info.get("item_no"), sh.service, sh.fluid,
               sh.info.get("line_no"), sh.info.get("project"), sh.info.get("format_name"),
               ", ".join(map(str, sh.pages)), "yes" if sh.has_values else "no", len(sh.cells)]
        for j, v in enumerate(row, 1):
            put(w2, n, j, v)
    _style(w2, [20, 16, 28, 8, 24, 16, 14, 18, 22, 10, 10, 8])

    w3 = wb.create_sheet("Long Format")
    for j, h in enumerate(["Source File", "Tag", "Section", "Row #", "Parameter", "Canonical", "Kind",
                           "Case", "Value", "Unit", "Page"], 1):
        w3.cell(1, j, h)
    n = 1
    for sh in job.sheets:
        for c in sh.cells:
            n += 1
            for j, v in enumerate([sh.file_name, sh.tag or sh.title, c.section, c.row_no, c.parameter,
                                   c.canonical, c.kind, c.case, c.value, c.unit, c.page], 1):
                put(w3, n, j, v)
    _style(w3, [20, 16, 22, 7, 38, 20, 11, 10, 16, 12, 6])

    w4 = wb.create_sheet("Review & Edits")
    for j, h in enumerate(["Type", "Severity", "Source File", "Tag", "Parameter", "Column", "Detail"], 1):
        w4.cell(1, j, h)
    n = 1
    for sh in job.sheets:
        for f in sh.notes:
            n += 1
            for j, v in enumerate(["Sheet flag", f.severity.upper(), sh.file_name, sh.tag or sh.title,
                                   "", "", f.message], 1):
                put(w4, n, j, v)
        for c in sh.cells:
            if c.edited:
                n += 1
                for j, v in enumerate(["Human edit", "", sh.file_name, sh.tag or sh.title, c.parameter,
                                       c.case, f"{c.original_value!r} {c.original_unit or ''} -> "
                                       f"{c.value!r} {c.unit or ''}"], 1):
                    put(w4, n, j, v)
            for f in c.flags:
                n += 1
                state = " [resolved by reviewer]" if (c.reviewed or c.edited) else ""
                for j, v in enumerate(["Cell flag", f.severity.upper(), sh.file_name, sh.tag or sh.title,
                                       c.parameter, c.case, f.message + state], 1):
                    put(w4, n, j, v)
    if n == 1:
        w4.append(["", "", "", "", "", "", "Nothing flagged (still spot-check against the source)."])
    _style(w4, [12, 10, 20, 16, 34, 10, 90])

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
````

### `app/main.py`

````python
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
````

### `app/extractors/__init__.py`

````python
"""Extractor registry. An extractor turns page images (+ text layer) into raw sheet dicts.

    extract(pages, plan, fname) -> (valves, issues)

`pages` is a list of (PIL.Image, text_layer_str). `valves` use the dict shape in
`claude_vision.RECORD_TOOL`. `issues` are reconciliation problems found while reading.
"""


def get_extractor(name: str):
    if name == "stub":
        from .stub import StubExtractor
        return StubExtractor()
    if name == "claude":
        from .claude_vision import ClaudeVisionExtractor
        return ClaudeVisionExtractor()
    raise ValueError(f"Unknown EXTRACTOR {name!r} (use 'claude' or 'stub')")
````

### `app/extractors/prompts.py`

````python
"""Prompts and tool schemas for the Claude vision extractor (verbatim from the tested CLI script)."""
from ..constants import CANON, CASES, KINDS

SYSTEM = """You transcribe the SERVICE / PROCESS CONDITIONS of industrial valve datasheets (control valves, relief valves, ball/on-off valves) from many customers and vendors. Layouts differ; the job does not.

SCOPE - extract only:
- Sheet identity: title, tag / item / KKS number, service, fluid, line number, project, template or vendor name.
- The block(s) describing the duty of the valve, under headings such as PROCESS DATA, OPERATING DATA, OPERATING CONDITIONS, SERVICE CONDITIONS, UPSTREAM / DOWNSTREAM CONDITIONS, VALVE DESIGN CONDITIONS, or "Process Data" on relief-valve sheets. Include the fluid name / state and critical or shut-off pressure when printed with that block.
- IGNORE: pipe line, body, bonnet, trim, actuator, positioner, accessories, testing, notes, revision/approval tables, logos, charts.

OUTPUT: one entry per FILLED cell. Fields:
- section: block heading as printed (or null).
- row_no: printed line number at the left of the row, else null.
- parameter: row label EXACTLY as printed, symbols included, e.g. "Inlet Pressure (P1)".
- canonical: closest value from the enum. Use "other" if nothing fits; never force a fit. Flow: mass units (kg/h, lb/h) -> flow_rate_mass; volumetric liquid (m3/h, gpm) -> flow_rate_volume; gas at standard conditions (scfh, Nm3/h) -> flow_rate_gas_std. Upstream/inlet temperature T1 -> inlet_temperature; T2 -> outlet_temperature. dP / pressure differential / pressure drop -> pressure_drop. Cp/Cv, gamma, isentropic exponent -> specific_heat_ratio.
- kind: "condition" = specified duty value (flow, pressures, temperatures, fluid properties); "design" = design limits (design pressure/temperature, shut-off or max allowable differential pressure, corrosion allowance); "calculated" = results of sizing sitting in the same table (Cv, % open / travel, capacity required, noise / SPL, velocities, Reynolds number).
- case: which column the value is under. Map by the printed HEADER TEXT, never by position (some sheets print Max/Norm/Min, others Min/Normal/Max): max/maximum -> maximum; norm/normal/operating -> normal; min/minimum -> minimum; design -> design; "Others"/"Other"/"Notes" -> other; a single value column with no case header -> single.
- case_label: the column header exactly as printed (or null).
- value: the cell text EXACTLY as printed.
- unit: unit exactly as printed, taken from the Units column or from inside the label (e.g. "Flow Rate Mass kg/h"). Keep gauge/absolute markers: "bar(g)", "kPaa", "kg/cm2g". Write superscripts as plain digits. If one unit applies to the whole row, repeat it on every entry of the row. null if none.
- page: page number the cell is on.

TRANSCRIPTION RULES (accuracy matters more than completeness):
1. Copy values exactly: digits, decimals, exponents (1.4E+06), signs, "<", ">". Never round, convert units, compute, reorder or normalise.
2. Never infer, interpolate or copy a value from another cell or another sheet. Empty cell -> no entry.
3. If any character is not clearly legible, set value to "UNREADABLE". Do not guess digits.
4. Sub-headers such as "Liquid / Liquid / Liquid" under Max / Norm / Min are VALUES of a row (e.g. Inlet State), not case labels.
5. Watermarks, stamps, logos or revision clouds overlapping text are not data. Read through them; if a character stays unclear use "UNREADABLE". Black bars / redactions = empty cell.
6. Placeholder text such as "XXXX" or "xxxxxxxx" = empty. "N/A", "NA", "-", "NIL" are real answers: copy them.
7. A blank template (no values filled in) -> has_values=false and entries=[]. Do NOT invent example values.
8. A sheet's title or tag can be on a different page from its table. Attach each table to the correct sheet using the tag/title, not page order; list the pages used in source_pages. One record per sheet.
9. Sheets without a process/service-conditions block (or with nothing filled) still get a record with has_values=false.
10. TEXT LAYER (when supplied) is real text: use it for characters; the images decide which row/column a value belongs to. If they disagree on a digit, use "UNREADABLE". ZOOM tiles are overlapping crops of the same page for legibility; emit each cell once.

Return the result only through the record_service_conditions tool."""

ADJ_SYSTEM = """You re-read specific cells of valve datasheets. For each numbered question, locate the cell using the page, section, row label and column header given, and report the value and unit EXACTLY as printed.
- Do not infer from neighbouring cells, other columns or physics.
- If the cell is empty, return value null. If any character is not clearly legible, return "UNREADABLE".
- A TEXT LAYER, when supplied, holds real characters; the images decide the row/column.
Return answers only through the answer_cells tool."""

STR = {"type": ["string", "null"]}
ENTRY = {
    "type": "object",
    "properties": {
        "section": STR,
        "row_no": {"type": ["integer", "null"]},
        "parameter": {"type": "string"},
        "canonical": {"type": "string", "enum": CANON},
        "kind": {"type": "string", "enum": KINDS},
        "case": {"type": "string", "enum": CASES},
        "case_label": STR,
        "value": STR,
        "unit": STR,
        "page": {"type": "integer"},
    },
    "required": ["parameter", "canonical", "kind", "case", "value", "unit", "page"],
}
RECORD_TOOL = {
    "name": "record_service_conditions",
    "description": "Record the service/process conditions of every valve sheet found.",
    "input_schema": {
        "type": "object",
        "properties": {
            "valves": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "sheet_title": STR, "format_name": STR, "tag": STR, "item_no": STR,
                        "service": STR, "fluid": STR, "line_no": STR, "project": STR,
                        "source_pages": {"type": "array", "items": {"type": "integer"}},
                        "has_values": {"type": "boolean"},
                        "entries": {"type": "array", "items": ENTRY},
                    },
                    "required": ["tag", "source_pages", "has_values", "entries"],
                },
            }
        },
        "required": ["valves"],
    },
}
ANSWER_TOOL = {
    "name": "answer_cells",
    "description": "Report the re-read value of each requested cell.",
    "input_schema": {
        "type": "object",
        "properties": {
            "answers": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"id": {"type": "integer"}, "value": STR, "unit": STR},
                    "required": ["id", "value", "unit"],
                },
            }
        },
        "required": ["answers"],
    },
}
````

### `app/extractors/claude_vision.py`

````python
"""Claude vision extractor: text layer + full page + zoom tiles -> forced tool call.

Two independent reads; disputed cells get a third targeted read; majority decides.
Requires ANTHROPIC_API_KEY. NOT exercised against the live API in the reference build.
"""
import base64
import io

from ..config import CLAUDE_MODEL
from ..consistency import (apply_adjudication, dispute_questions, find_disputes, normalize)
from .prompts import ADJ_SYSTEM, ANSWER_TOOL, RECORD_TOOL, SYSTEM
from PIL import Image


def fit(img, lo=1400, hi=1568):
    """Long edge into [lo, hi] px so small images get enough tokens and big ones aren't silently downscaled."""
    w, h = img.size
    long_edge = max(w, h)
    if lo <= long_edge <= hi:
        return img
    s = (lo if long_edge < lo else hi) / long_edge
    return img.resize((max(1, round(w * s)), max(1, round(h * s))), Image.LANCZOS)


def img_block(img):
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                        "data": base64.b64encode(buf.getvalue()).decode()}}


def tiles(img, rows, cols, overlap=0.12):
    w, h = img.size

    def spans(total, n):
        if n <= 1:
            return [(0, total)]
        size = total / (n - (n - 1) * overlap)
        step = size * (1 - overlap)
        return [(int(k * step), min(total, int(k * step + size))) for k in range(n)]

    for ri, (t, b) in enumerate(spans(h, rows), 1):
        for ci, (l, r) in enumerate(spans(w, cols), 1):
            yield ri, ci, img.crop((l, t, r, b))


def build_content(pages, plan):
    blocks = []
    for i, (img, text) in enumerate(pages, 1):
        blocks.append({"type": "text", "text": f"=== PAGE {i} of {len(pages)} ==="})
        blocks.append(img_block(fit(img)))
        if len(text) >= 80:
            blocks.append({"type": "text",
                           "text": f"TEXT LAYER of page {i} (real characters; ' | ' = column gap):\n{text}"})
        if plan.bands > 0 or plan.cols > 1:
            for ri, ci, t in tiles(img, max(plan.bands, 1), max(plan.cols, 1)):
                blocks.append({"type": "text",
                               "text": f"ZOOM tile row {ri} col {ci} of page {i} (overlaps neighbours)"})
                blocks.append(img_block(fit(t)))
    blocks[-1] = {**blocks[-1], "cache_control": {"type": "ephemeral"}}
    return blocks


def call_tool(client, model, system, tool, content, max_tokens=32000):
    with client.messages.stream(
        model=model, max_tokens=max_tokens, system=system, tools=[tool],
        tool_choice={"type": "tool", "name": tool["name"]},
        messages=[{"role": "user", "content": content}],
    ) as stream:
        resp = stream.get_final_message()
    if resp.stop_reason == "max_tokens":
        raise RuntimeError("Model output was truncated; split the file into fewer sheets.")
    for b in resp.content:
        if b.type == "tool_use":
            return b.input
    raise RuntimeError("Model returned no tool call.")


class ClaudeVisionExtractor:
    name = "claude"

    def __init__(self):
        import anthropic  # imported late so the stub works without the SDK configured
        self.client = anthropic.Anthropic()

    def extract(self, pages, plan, fname):
        shared = build_content(pages, plan)
        ask = {"type": "text",
               "text": "Extract the service/process conditions of every valve sheet in this document."}
        runs = [normalize(call_tool(self.client, CLAUDE_MODEL, SYSTEM, RECORD_TOOL, shared + [ask])
                          .get("valves", [])) for _ in range(plan.passes)]
        valves, issues = runs[0], []
        if len(runs) == 2 and plan.adjudicate:
            disputes = find_disputes(runs[0], runs[1], fname, issues)
            if disputes:
                out = call_tool(self.client, CLAUDE_MODEL, ADJ_SYSTEM, ANSWER_TOOL, shared + [
                    {"type": "text", "text": "Re-read these cells. Use the question number as id.\n"
                     + "\n".join(dispute_questions(disputes))}], max_tokens=8000)
                apply_adjudication(disputes, {a["id"]: a for a in out.get("answers", [])}, fname, issues)
        return valves, issues
````

### `app/extractors/stub.py`

````python
"""Canned extractor for tests and UI demos. No network, no model. NOT a real reader."""
from ..consistency import issue, normalize


def _t(param, canon, vals, unit, kind="condition", sec="PROCESS DATA", row=None):
    return [dict(section=sec, row_no=row, parameter=param, canonical=canon, kind=kind, case=c,
                 case_label=c[:3].title(), value=v, unit=unit, page=1)
            for c, v in zip(("maximum", "normal", "minimum"), vals)]


class StubExtractor:
    name = "stub"

    def extract(self, pages, plan, fname):
        v1 = dict(sheet_title="Control valve data sheet", format_name="Demo", tag="DEMO-101",
                  item_no="1", fluid="Water", service="Demo service", source_pages=[1], has_values=True,
                  entries=_t("Inlet Pressure (P1)", "inlet_pressure", ["56.7", "56.7", "56.7"], "bar(g)", row=3)
                  + _t("Outlet Pressure (P2)", "outlet_pressure", ["52.7", "52.9", "40.0"], "bar(g)", row=4)
                  + _t("Pressure Drop (dP)", "pressure_drop", ["4.0", "3.9", "6.7"], "bar", row=5)
                  + _t("Flow Rate Mass", "flow_rate_mass", ["45300.0", "41000.0", "UNREADABLE"], "kg/h", row=2)
                  + _t("Inlet Temperature", "inlet_temperature", ["150", "150", "127"], "C", row=6)
                  + _t("Inlet State", "fluid_state", ["Liquid", "Liquid", "Liquid"], None, row=1)
                  + _t("SPL at 1 metre", "other", ["<75", "<75", "<75"], "dBA", kind="calculated", row=12))
        v2 = dict(sheet_title="Spec sheet", tag="DEMO-102", source_pages=[min(2, len(pages))], has_values=True,
                  entries=_t("Inlet Pressure (P1)", "inlet_pressure", ["10.0", "10.0", "10.0"], "kg/cm2g", sec="OPERATING DATA")
                  + _t("Flow Rate", "flow_rate_gas_std", ["3600", "3000", "1500"], "scfh", sec="OPERATING DATA"))
        valves = normalize([v1, v2])
        issues = [issue(fname, "DEMO-101", "PROCESS DATA", "Flow Rate Mass", "maximum", "warning",
                        "pass_disagree", "Reads disagreed (1: '43300.9', 2: '45300.0'); re-read decided '45300.0'")]
        return valves, issues
````

### `app/static/index.html`

````html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Process Conditions Extractor</title>
<link rel="stylesheet" href="/static/app.css">
</head>
<body>
<header class="top">
  <h1>Process conditions extractor</h1>
  <div class="top-actions">
    <button id="btn-new" class="ghost hidden" type="button">New upload</button>
    <button id="btn-delete" class="ghost danger hidden" type="button">Delete my data</button>
  </div>
</header>

<main>
  <section id="drop" class="drop" tabindex="0" role="button" aria-label="Choose or drop files">
    <p class="drop-title">Drop PDFs or images here</p>
    <p class="muted">or click to choose &middot; PDF, PNG, JPG, TIFF &middot; originals work far better than screenshots</p>
    <input id="picker" type="file" multiple accept=".pdf,.png,.jpg,.jpeg,.tif,.tiff,application/pdf,image/*" hidden>
  </section>

  <section id="progress" class="panel hidden" aria-live="polite">
    <div class="spinner" aria-hidden="true"></div>
    <div><strong id="progress-title">Working</strong><div id="progress-text" class="muted"></div></div>
  </section>

  <section id="error" class="panel err hidden" role="alert"></section>

  <section id="results" class="hidden">
    <div class="bar">
      <div id="summary" class="summary"></div>
      <label class="check"><input id="only-flagged" type="checkbox"> Show only rows needing review</label>
      <button id="btn-export" class="primary" type="button">Export to Excel</button>
    </div>
    <p class="legend">
      <span class="chip c-error">error</span>
      <span class="chip c-warning">check</span>
      <span class="chip c-edited">edited by you</span>
      <span class="chip c-reviewed">reviewed</span>
      <span class="muted">Click a cell to edit. Press OK on a row once you have checked it against the page.</span>
    </p>
    <div id="sheets"></div>
  </section>
</main>

<div id="modal" class="modal hidden" role="dialog" aria-modal="true" aria-label="Source page">
  <div class="modal-body">
    <div class="modal-head"><strong id="modal-title"></strong><button id="modal-close" class="ghost" type="button">Close</button></div>
    <div class="modal-img"><img id="modal-img" alt="Source page"></div>
  </div>
</div>
<script src="/static/app.js"></script>
</body>
</html>
````

### `app/static/app.css`

````css
:root{--bg:#f6f7f9;--panel:#fff;--text:#1b1f27;--muted:#667085;--line:#d7dce3;--accent:#1f4e9c;--accent-fg:#fff;
--err:#fbd5d5;--err-b:#d64545;--warn:#fff0c2;--warn-b:#d9a400;--edit:#cfe3f8;--edit-b:#3b82c4;--ok:#d3f0dc;--ok-b:#2e9d57}
@media (prefers-color-scheme:dark){:root{--bg:#14171c;--panel:#1c2027;--text:#e7eaf0;--muted:#9aa4b2;--line:#323946;--accent:#6ea0f0;--accent-fg:#0b1220;
--err:#4a2326;--err-b:#e06666;--warn:#4a3d12;--warn-b:#e0b422;--edit:#1f3a56;--edit-b:#5aa0e0;--ok:#173a26;--ok-b:#4cc07a}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}
.hidden{display:none!important}
.muted{color:var(--muted)}
.top{display:flex;justify-content:space-between;align-items:center;padding:14px 24px;border-bottom:1px solid var(--line);background:var(--panel)}
.top h1{font-size:17px;margin:0}
.top-actions{display:flex;gap:8px}
main{max-width:1280px;margin:0 auto;padding:20px 24px 60px}
button{font:inherit;cursor:pointer;border-radius:6px;border:1px solid var(--line);background:var(--panel);color:var(--text);padding:7px 14px}
button:hover{border-color:var(--accent)}
button.primary{background:var(--accent);color:var(--accent-fg);border-color:var(--accent);font-weight:600}
button.ghost{background:transparent}
button.danger{color:var(--err-b);border-color:var(--err-b)}
button.small{padding:3px 9px;font-size:12px}
.drop{border:2px dashed var(--line);border-radius:12px;background:var(--panel);text-align:center;padding:48px 20px;cursor:pointer;transition:border-color .15s,background .15s}
.drop:hover,.drop.over,.drop:focus-visible{border-color:var(--accent);outline:none}
.drop.over{background:var(--edit)}
.drop-title{font-size:18px;font-weight:600;margin:0 0 6px}
.panel{display:flex;gap:14px;align-items:center;margin-top:16px;padding:14px 18px;border:1px solid var(--line);border-radius:10px;background:var(--panel)}
.panel.err{border-color:var(--err-b);background:var(--err)}
.spinner{width:22px;height:22px;border-radius:50%;border:3px solid var(--line);border-top-color:var(--accent);animation:spin .9s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.bar{display:flex;gap:18px;align-items:center;flex-wrap:wrap;margin:18px 0 8px}
.summary{flex:1;min-width:260px;font-weight:600}
.check{display:flex;gap:6px;align-items:center;color:var(--muted)}
.legend{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:6px 0 16px}
.chip{padding:1px 8px;border-radius:10px;font-size:12px;border:1px solid transparent}
.c-error{background:var(--err);border-color:var(--err-b)}.c-warning{background:var(--warn);border-color:var(--warn-b)}
.c-edited{background:var(--edit);border-color:var(--edit-b)}.c-reviewed{background:var(--ok);border-color:var(--ok-b)}
.sheet{background:var(--panel);border:1px solid var(--line);border-radius:10px;margin-bottom:18px;overflow:hidden}
.sheet-head{padding:12px 16px;border-bottom:1px solid var(--line);display:flex;flex-wrap:wrap;gap:6px 18px;align-items:baseline}
.sheet-head h2{font-size:15px;margin:0}
.meta{color:var(--muted);font-size:13px}
.notes{padding:8px 16px;border-bottom:1px solid var(--line)}
.note{font-size:13px;margin:2px 0}
.note.error{color:var(--err-b)}.note.warning{color:var(--warn-b)}
.scroll{overflow-x:auto}
table{width:100%;border-collapse:collapse}
th,td{padding:4px 6px;border-bottom:1px solid var(--line);text-align:left;vertical-align:middle;white-space:nowrap}
th{font-size:12px;text-transform:uppercase;letter-spacing:.03em;color:var(--muted);background:var(--bg)}
tr.section td{background:var(--bg);font-weight:600;font-size:12px;color:var(--muted);text-transform:uppercase}
td.rn{color:var(--muted);width:34px;text-align:right}
td.act{width:1%}
input.cell{width:100%;min-width:92px;font:inherit;color:var(--text);background:transparent;border:1px solid var(--line);border-radius:5px;padding:4px 6px}
input.cell:focus{outline:2px solid var(--accent);outline-offset:0}
input.cell.param{min-width:240px}
input.cell.unit{min-width:80px;width:96px}
input.cell.error{background:var(--err);border-color:var(--err-b)}
input.cell.warning{background:var(--warn);border-color:var(--warn-b)}
input.cell.edited{background:var(--edit);border-color:var(--edit-b)}
input.cell.reviewed{background:var(--ok);border-color:var(--ok-b)}
a.page{color:var(--accent);cursor:pointer;text-decoration:underline;font-size:13px}
.rowflag{font-size:12px;color:var(--muted);max-width:320px;white-space:normal}
tr.addrow td{background:var(--bg)}
details.calc{border-top:1px solid var(--line)}
details.calc summary{padding:8px 16px;cursor:pointer;color:var(--muted)}
.modal{position:fixed;inset:0;background:rgba(0,0,0,.55);display:flex;align-items:center;justify-content:center;z-index:10}
.modal-body{background:var(--panel);border-radius:10px;max-width:96vw;max-height:94vh;display:flex;flex-direction:column;overflow:hidden}
.modal-head{display:flex;justify-content:space-between;align-items:center;gap:20px;padding:10px 14px;border-bottom:1px solid var(--line)}
.modal-img{overflow:auto}
.modal-img img{display:block;max-width:none;width:1100px}
````

### `app/static/app.js`

````javascript
"use strict";
// All user/model text is rendered with textContent / .value only. Never innerHTML.
const $ = (id) => document.getElementById(id);
const CASES = ["minimum", "normal", "maximum", "design", "single", "other"];
const TITLES = {minimum: "Min", normal: "Normal", maximum: "Max", design: "Design", single: "Single", other: "Other"};
const ALWAYS = ["minimum", "normal", "maximum"];
let job = null, pollTimer = null;
const pending = new Set();   // in-flight saves; export waits for them

function h(tag, props, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined && v !== false) e.setAttribute(k, v === true ? "" : v);
  }
  for (const k of kids.flat()) if (k !== null && k !== undefined) e.append(k.nodeType ? k : document.createTextNode(String(k)));
  return e;
}
const store = {
  get() { try { return localStorage.getItem("jobId"); } catch { return null; } },
  set(v) { try { v ? localStorage.setItem("jobId", v) : localStorage.removeItem("jobId"); } catch {} },
};

async function api(method, url, body) {
  const opt = {method, headers: {}};
  if (body instanceof FormData) opt.body = body;
  else if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  const r = await fetch(url, opt);
  if (!r.ok) {
    let msg = r.statusText;
    try { const j = await r.json(); msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail); } catch {}
    throw new Error(msg);
  }
  return r.status === 204 ? null : r.json();
}
function tracked(p) { pending.add(p); const done = () => pending.delete(p); p.then(done, done); return p; }

// ---------------------------------------------------------------- upload
function showError(msg) {
  const e = $("error");
  e.textContent = msg;
  e.classList.toggle("hidden", !msg);
}
async function upload(fileList) {
  const files = [...fileList];
  if (!files.length) return;
  showError("");
  const fd = new FormData();
  files.forEach((f) => fd.append("files", f));
  setView("progress", "Uploading", files.map((f) => f.name).join(", "));
  try {
    const {id} = await api("POST", "/api/jobs", fd);
    store.set(id);
    poll(id);
  } catch (e) {
    setView("drop");
    showError("Upload failed: " + e.message);
  }
}
function setView(view, title, text) {
  $("drop").classList.toggle("hidden", view !== "drop");
  $("progress").classList.toggle("hidden", view !== "progress");
  $("results").classList.toggle("hidden", view !== "results");
  $("btn-new").classList.toggle("hidden", view === "drop");
  $("btn-delete").classList.toggle("hidden", view === "drop");
  if (title) $("progress-title").textContent = title;
  if (text !== undefined) $("progress-text").textContent = text;
}
function poll(id) {
  clearTimeout(pollTimer);
  const tick = async () => {
    try {
      job = await api("GET", "/api/jobs/" + id);
    } catch (e) {
      store.set(null); setView("drop"); return;
    }
    if (job.status === "done") { setView("results"); render(); return; }
    if (job.status === "error") { setView("drop"); showError("Extraction failed: " + (job.error || "unknown error")); return; }
    setView("progress", "Reading datasheets", job.progress);
    pollTimer = setTimeout(tick, 1500);
  };
  tick();
}

// ---------------------------------------------------------------- state helpers
function sev(c) { return c.flags.some((f) => f.severity === "error") ? "error" : c.flags.some((f) => f.severity === "warning") ? "warning" : ""; }
function stateOf(c) { return c.edited ? "edited" : c.reviewed ? "reviewed" : sev(c); }
function needsReview(c) { return !c.edited && !c.reviewed && sev(c) !== ""; }
function groupRows(cells) {
  const m = new Map();
  for (const c of cells) {
    const k = [(c.section || "").toLowerCase().trim(), c.parameter.toLowerCase().trim(), c.kind].join("\u0001");
    if (!m.has(k)) m.set(k, {section: c.section || "", cells: [], kind: c.kind});
    m.get(k).cells.push(c);
  }
  return [...m.values()];
}
function decorate(input, c) {
  input.classList.remove("error", "warning", "edited", "reviewed");
  const s = c ? stateOf(c) : "";
  if (s) input.classList.add(s);
  const tips = [];
  if (c) {
    c.flags.forEach((f) => tips.push(f.severity.toUpperCase() + ": " + f.message));
    if (c.edited) tips.push("You changed this. Model value: " + (c.original_value ?? "(empty)") + (c.original_unit ? " " + c.original_unit : ""));
  }
  input.title = tips.join("\n");
}
function updateSummary() {
  let cells = 0, open = 0, edited = 0;
  for (const s of job.sheets) for (const c of s.cells) { cells++; if (needsReview(c)) open++; if (c.edited) edited++; }
  $("summary").textContent = `${job.sheets.length} sheet(s), ${cells} cells · ${open} still need review · ${edited} edited by you`;
  applyFilter();
}
function applyFilter() {
  const only = $("only-flagged").checked;
  document.querySelectorAll("tr[data-row]").forEach((tr) => {
    const open = tr.querySelector("input.cell.error, input.cell.warning");
    tr.classList.toggle("hidden", only && !open);
  });
}

// ---------------------------------------------------------------- saving
function saveCell(c, patch, input, rowEls) {
  return tracked(api("PATCH", `/api/jobs/${job.id}/cells/${c.id}`, patch).then((nc) => {
    Object.assign(c, nc);
    decorate(input, c);
    rowEls?.refresh();
    updateSummary();
  }).catch((e) => { showError("Save failed: " + e.message); }));
}

// ---------------------------------------------------------------- rendering
function cellInput(sheet, row, rowCtx, caseName) {
  const c = row.cells.find((x) => x.case === caseName);
  const input = h("input", {class: "cell", type: "text", value: c ? c.value ?? "" : "", "aria-label": TITLES[caseName]});
  input.value = c ? c.value ?? "" : "";
  decorate(input, c);
  input.addEventListener("change", async () => {
    const cur = row.cells.find((x) => x.case === caseName);
    if (cur) return saveCell(cur, {value: input.value}, input, rowCtx);
    if (!input.value.trim()) return;
    const rep = row.cells[0];
    tracked(api("POST", `/api/jobs/${job.id}/sheets/${sheet.id}/cells`, {
      parameter: rep.parameter, case: caseName, value: input.value, unit: rep.unit, section: rep.section, kind: rep.kind,
    }).then((nc) => {
      sheet.cells.push(nc); row.cells.push(nc); decorate(input, nc); rowCtx.refresh(); updateSummary();
    }).catch((e) => showError("Save failed: " + e.message)));
  });
  return input;
}
function rowTr(sheet, row, i, cols) {
  const rep = row.cells[0];
  const ctx = {refresh() { flagTxt.textContent = rowFlags(); }};
  const rowFlags = () => row.cells.flatMap((c) => (c.edited || c.reviewed) ? [] : c.flags.map((f) => f.message)).filter((m, j, a) => a.indexOf(m) === j).join(" · ");
  const flagTxt = h("div", {class: "rowflag"}, rowFlags());

  const par = h("input", {class: "cell param", type: "text", "aria-label": "Parameter"});
  par.value = rep.parameter;
  par.addEventListener("change", () => Promise.all(row.cells.map((c) => saveCell(c, {parameter: par.value}, par, ctx))));
  const unitVals = [...new Set(row.cells.map((c) => c.unit).filter(Boolean))];
  const unit = h("input", {class: "cell unit", type: "text", "aria-label": "Unit"});
  unit.value = unitVals.join(" / ");
  unit.addEventListener("change", () => Promise.all(row.cells.map((c) => saveCell(c, {unit: unit.value}, unit, ctx))));

  const tr = h("tr", {"data-row": "1"},
    h("td", {class: "rn"}, rep.row_no ?? ""),
    h("td", {}, par), h("td", {}, unit),
    cols.map((cn) => h("td", {}, cellInput(sheet, row, ctx, cn))),
    h("td", {}, h("a", {class: "page", tabindex: "0", role: "button", onclick: () => openPage(sheet, rep.page),
      onkeydown: (e) => { if (e.key === "Enter") openPage(sheet, rep.page); }}, "p" + rep.page)),
    h("td", {}, flagTxt),
    h("td", {class: "act"},
      h("button", {class: "small", type: "button", title: "Mark this row as checked against the source", onclick: () => {
        Promise.all(row.cells.map((c) => api("PATCH", `/api/jobs/${job.id}/cells/${c.id}`, {reviewed: true}).then((nc) => Object.assign(c, nc))))
          .then(() => { renderSheets(); }).catch((e) => showError("Save failed: " + e.message));
      }}, "OK"),
      " ",
      h("button", {class: "small danger", type: "button", title: "Delete this row", onclick: () => {
        if (!confirm("Delete this row?")) return;
        Promise.all(row.cells.map((c) => api("DELETE", `/api/jobs/${job.id}/cells/${c.id}`)))
          .then(() => { sheet.cells = sheet.cells.filter((c) => !row.cells.includes(c)); renderSheets(); })
          .catch((e) => showError("Delete failed: " + e.message));
      }}, "×")));
  return tr;
}
function addRow(sheet, cols) {
  const par = h("input", {class: "cell param", type: "text", placeholder: "New parameter"});
  const unit = h("input", {class: "cell unit", type: "text", placeholder: "unit"});
  const vals = cols.map((cn) => h("input", {class: "cell", type: "text", placeholder: TITLES[cn]}));
  const add = h("button", {class: "small", type: "button", onclick: async () => {
    if (!par.value.trim()) { par.focus(); return; }
    const todo = cols.map((cn, i) => ({cn, v: vals[i].value})).filter((x) => x.v.trim());
    if (!todo.length) { vals[0].focus(); return; }
    try {
      for (const {cn, v} of todo) {
        sheet.cells.push(await api("POST", `/api/jobs/${job.id}/sheets/${sheet.id}/cells`,
          {parameter: par.value, case: cn, value: v, unit: unit.value}));
      }
      renderSheets();
    } catch (e) { showError("Add failed: " + e.message); }
  }}, "Add row");
  return h("tr", {class: "addrow"}, h("td"), h("td", {}, par), h("td", {}, unit),
    vals.map((v) => h("td", {}, v)), h("td"), h("td"), h("td", {class: "act"}, add));
}
function table(sheet, cells, withAdd) {
  const used = new Set(cells.map((c) => c.case));
  const cols = CASES.filter((cn) => ALWAYS.includes(cn) || used.has(cn));
  const body = h("tbody");
  let lastSection = null;
  groupRows(cells).forEach((row, i) => {
    if (row.section !== lastSection) {
      lastSection = row.section;
      if (row.section) body.append(h("tr", {class: "section"}, h("td", {colspan: 6 + cols.length}, row.section)));
    }
    body.append(rowTr(sheet, row, i, cols));
  });
  if (withAdd) body.append(addRow(sheet, cols));
  return h("div", {class: "scroll"}, h("table", {},
    h("thead", {}, h("tr", {}, h("th", {}, "#"), h("th", {}, "Parameter"), h("th", {}, "Unit"),
      cols.map((cn) => h("th", {}, TITLES[cn])), h("th", {}, "Page"), h("th", {}, "Flags"), h("th", {}))),
    body));
}
function renderSheets() {
  const root = $("sheets");
  root.replaceChildren();
  for (const s of job.sheets) {
    const main = s.cells.filter((c) => c.kind !== "calculated");
    const calc = s.cells.filter((c) => c.kind === "calculated");
    const card = h("div", {class: "sheet"},
      h("div", {class: "sheet-head"},
        h("h2", {}, s.tag || s.title || "(untitled sheet)"),
        h("span", {class: "meta"}, [s.title && s.tag ? s.title : null, s.fluid ? "Fluid: " + s.fluid : null,
          s.service ? "Service: " + s.service : null, s.file_name, "pages " + (s.pages.join(", ") || "-")].filter(Boolean).join(" · "))));
    if (s.notes.length) card.append(h("div", {class: "notes"}, s.notes.map((f) => h("div", {class: "note " + f.severity}, f.severity.toUpperCase() + ": " + f.message))));
    if (!s.has_values && !main.length) card.append(h("div", {class: "notes muted"}, "No filled process-condition values found on this sheet."));
    card.append(table(s, main, true));
    if (calc.length) card.append(h("details", {class: "calc"}, h("summary", {}, `Calculated values printed in the same table (${calc.length} cells; not process conditions)`), table(s, calc, false)));
    root.append(card);
  }
  updateSummary();
}
function render() { renderSheets(); }

// ---------------------------------------------------------------- page viewer
function openPage(sheet, page) {
  const fi = sheet.file_idx;
  $("modal-title").textContent = `${sheet.file_name} · page ${page}`;
  $("modal-img").src = `/api/jobs/${job.id}/pages/${fi}/${page}.png`;
  $("modal").classList.remove("hidden");
  $("modal-close").focus();
}
function closeModal() { $("modal").classList.add("hidden"); $("modal-img").removeAttribute("src"); }

// ---------------------------------------------------------------- wiring
const drop = $("drop"), picker = $("picker");
drop.addEventListener("click", () => picker.click());
drop.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); picker.click(); } });
picker.addEventListener("change", () => { upload(picker.files); picker.value = ""; });
["dragenter", "dragover"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
drop.addEventListener("drop", (e) => upload(e.dataTransfer.files));
// a file dropped outside the zone must not navigate away from the page
["dragover", "drop"].forEach((t) => window.addEventListener(t, (e) => e.preventDefault()));

$("only-flagged").addEventListener("change", applyFilter);
$("modal-close").addEventListener("click", closeModal);
$("modal").addEventListener("click", (e) => { if (e.target === $("modal")) closeModal(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeModal(); });
$("btn-new").addEventListener("click", () => { clearTimeout(pollTimer); job = null; store.set(null); showError(""); setView("drop"); });
$("btn-export").addEventListener("click", async () => {
  await Promise.allSettled([...pending]);
  window.location.href = `/api/jobs/${job.id}/export.xlsx`;
});
let armed = null;
$("btn-delete").addEventListener("click", async () => {
  const b = $("btn-delete");
  if (!armed) {
    b.textContent = "Click again to confirm";
    armed = setTimeout(() => { armed = null; b.textContent = "Delete my data"; }, 4000);
    return;
  }
  clearTimeout(armed); armed = null; b.textContent = "Delete my data";
  clearTimeout(pollTimer);
  try { if (job) await api("DELETE", "/api/jobs/" + job.id); } catch {}
  job = null; store.set(null); setView("drop");
});

(function init() {
  const id = store.get();
  if (id) { setView("progress", "Resuming", ""); poll(id); } else setView("drop");
})();
````

### `tests/__init__.py`

````python

````

### `tests/conftest.py`

````python
import io
import os
import sys
import tempfile
from pathlib import Path

os.environ["EXTRACTOR"] = "stub"
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="amdp-test-")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pymupdf
import pytest
from fastapi.testclient import TestClient
from PIL import Image


@pytest.fixture(scope="session")
def client():
    from app.main import app
    return TestClient(app)


def make_pdf(pages=2) -> bytes:
    doc = pymupdf.open()
    for i in range(pages):
        p = doc.new_page()
        p.insert_text((72, 100), f"Control valve data sheet page {i + 1}", fontsize=14)
    return doc.tobytes()


def make_png(size=(600, 800)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, "white").save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture
def pdf_bytes():
    return make_pdf()


@pytest.fixture
def png_bytes():
    return make_png()
````

### `tests/test_app.py`

````python
import io
import time

import openpyxl
import pytest

from app.consistency import apply_adjudication, find_disputes, normalize, to_sheets, validate
from app.controller import MAX_IMAGES_PER_REQUEST, plan_for
from app.textutil import same_value, to_num, unit_norm


def upload(client, files):
    r = client.post("/api/jobs", files=[("files", f) for f in files])
    return r


def wait_done(client, job_id, timeout=20):
    t = time.time()
    while time.time() - t < timeout:
        j = client.get(f"/api/jobs/{job_id}").json()
        if j["status"] in ("done", "error"):
            return j
        time.sleep(0.1)
    raise AssertionError("job did not finish")


@pytest.fixture
def done_job(client, pdf_bytes, png_bytes):
    r = upload(client, [("a.pdf", pdf_bytes, "application/pdf"), ("b.png", png_bytes, "image/png")])
    assert r.status_code == 202, r.text
    j = wait_done(client, r.json()["id"])
    assert j["status"] == "done", j
    return j


def find(job, parameter, case, tag="DEMO-101"):
    for s in job["sheets"]:
        if s["tag"] == tag:
            for c in s["cells"]:
                if c["parameter"] == parameter and c["case"] == case:
                    return s, c
    raise AssertionError((parameter, case))


# ------------------------------------------------------------------ upload
def test_upload_pipeline_done(done_job):
    assert [f["name"] for f in done_job["files"]] == ["a.pdf", "b.png"]
    assert [f["pages"] for f in done_job["files"]] == [2, 1]
    assert len(done_job["sheets"]) == 4  # stub returns 2 sheets per file


def test_flags_attached_to_cells(done_job):
    _, c = find(done_job, "Flow Rate Mass", "minimum")
    assert any(f["code"] == "unreadable" and f["severity"] == "error" for f in c["flags"])
    _, c = find(done_job, "Outlet Pressure (P2)", "minimum")
    assert any(f["code"] == "p_balance" for f in c["flags"])  # 56.7 - 6.7 != 40.0 -> flagged
    _, c = find(done_job, "Flow Rate Mass", "maximum")
    assert any(f["code"] == "pass_disagree" for f in c["flags"])


@pytest.mark.parametrize("name,data,ctype,code", [
    ("x.exe", b"MZ....", "application/octet-stream", 415),
    ("x.pdf", b"not a pdf at all", "application/pdf", 415),       # spoofed extension
    ("x.png", b"%PDF-1.4 ...", "image/png", 415),                  # wrong family
    ("x.pdf", b"", "application/pdf", 422),
])
def test_upload_rejections(client, name, data, ctype, code):
    r = upload(client, [(name, data, ctype)])
    assert r.status_code == code, r.text


def test_rejected_upload_leaves_no_job(client):
    from app.store import jobs_dir
    before = set(p.name for p in jobs_dir().iterdir())
    upload(client, [("x.pdf", b"nope", "application/pdf")])
    assert set(p.name for p in jobs_dir().iterdir()) == before


def test_filename_never_used_as_path(client, png_bytes):
    r = upload(client, [("../../evil.png", png_bytes, "image/png")])
    assert r.status_code == 202
    j = wait_done(client, r.json()["id"])
    assert j["files"][0]["name"] == "evil.png"


def test_password_pdf_gives_error_not_crash(client):
    import pymupdf
    doc = pymupdf.open()
    doc.new_page()
    data = doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="o", user_pw="u")
    r = upload(client, [("locked.pdf", data, "application/pdf")])
    j = wait_done(client, r.json()["id"])
    assert j["status"] == "error" and "password" in j["error"].lower()


def test_invalid_job_id(client):
    for bad in ("nope", "../etc", "0" * 31, "g" * 32):
        assert client.get(f"/api/jobs/{bad}").status_code == 404
    assert client.get("/api/jobs/" + "0" * 32).status_code == 404


# ----------------------------------------------------------------- editing
def test_patch_marks_edited_and_keeps_original(client, done_job):
    s, c = find(done_job, "Inlet Pressure (P1)", "normal")
    r = client.patch(f"/api/jobs/{done_job['id']}/cells/{c['id']}", json={"value": "57.1"})
    assert r.status_code == 200
    n = r.json()
    assert n["value"] == "57.1" and n["edited"] and n["reviewed"] and n["original_value"] == "56.7"
    # a second edit keeps the ORIGINAL model value
    n = client.patch(f"/api/jobs/{done_job['id']}/cells/{c['id']}", json={"value": "57.2"}).json()
    assert n["original_value"] == "56.7"
    # persisted
    j = client.get(f"/api/jobs/{done_job['id']}").json()
    assert find(j, "Inlet Pressure (P1)", "normal")[1]["value"] == "57.2"


def test_review_without_change_is_not_an_edit(client, done_job):
    _, c = find(done_job, "Flow Rate Mass", "maximum")
    n = client.patch(f"/api/jobs/{done_job['id']}/cells/{c['id']}", json={"reviewed": True}).json()
    assert n["reviewed"] and not n["edited"]


def test_patch_validation(client, done_job):
    _, c = find(done_job, "Inlet Pressure (P1)", "normal")
    url = f"/api/jobs/{done_job['id']}/cells/{c['id']}"
    assert client.patch(url, json={"value": "x" * 301}).status_code == 422
    assert client.patch(url, json={"parameter": "  "}).status_code == 422
    assert client.patch(f"/api/jobs/{done_job['id']}/cells/deadbeef", json={"value": "1"}).status_code == 404


def test_add_and_delete_cell(client, done_job):
    s, _ = find(done_job, "Inlet Pressure (P1)", "normal")
    r = client.post(f"/api/jobs/{done_job['id']}/sheets/{s['id']}/cells",
                    json={"parameter": "Viscosity", "case": "normal", "value": "1.2", "unit": "cP"})
    assert r.status_code == 201
    cid = r.json()["id"]
    assert r.json()["source"] == "human" and r.json()["edited"]
    assert client.delete(f"/api/jobs/{done_job['id']}/cells/{cid}").status_code == 204
    j = client.get(f"/api/jobs/{done_job['id']}").json()
    assert all(c["id"] != cid for sh in j["sheets"] for c in sh["cells"])


# ------------------------------------------------------------------ export
def sheet_rows(wb, name):
    ws = wb[name]
    head = [c.value for c in ws[1]]
    return [dict(zip(head, [c.value for c in row])) for row in ws.iter_rows(min_row=2)]


def export(client, job_id):
    r = client.get(f"/api/jobs/{job_id}/export.xlsx")
    assert r.status_code == 200
    assert "attachment" in r.headers["content-disposition"]
    return openpyxl.load_workbook(io.BytesIO(r.content))


def test_export_reflects_edits(client, done_job):
    _, c = find(done_job, "Inlet Temperature", "normal")
    client.patch(f"/api/jobs/{done_job['id']}/cells/{c['id']}", json={"value": "99.9"})
    # the P1 row carries an unresolved p_balance error -> must still read ERROR in the export
    wb = export(client, done_job["id"])
    assert wb.sheetnames == ["Process Conditions", "Sheet Info", "Long Format", "Review & Edits"]
    rows = {r["Parameter"]: r for r in sheet_rows(wb, "Process Conditions") if r["Tag"] == "DEMO-101" and r["Source File"] == "a.pdf"}
    t = rows["Inlet Temperature"]
    assert t["Normal"] == 99.9 and t["Maximum"] == 150 and t["Status"] == "EDITED"  # numbers stay numeric
    assert rows["Inlet Pressure (P1)"]["Status"] == "ERROR"
    edits = [r for r in sheet_rows(wb, "Review & Edits") if r["Type"] == "Human edit"]
    assert any("150" in r["Detail"] and "99.9" in r["Detail"] for r in edits)


def test_export_blocks_formula_injection(client, done_job):
    _, c = find(done_job, "Inlet State", "normal")
    client.patch(f"/api/jobs/{done_job['id']}/cells/{c['id']}", json={"value": '=HYPERLINK("http://x","y")'})
    wb = export(client, done_job["id"])
    cells = [cell for ws in wb for row in ws.iter_rows() for cell in row if isinstance(cell.value, str) and cell.value.startswith("=")]
    assert cells, "expected the payload to be present as text"
    assert all(cell.data_type == "s" for cell in cells)


def test_export_unfinished_job_rejected(client):
    from app import store
    job = store.create()
    assert client.get(f"/api/jobs/{job.id}/export.xlsx").status_code == 409


# -------------------------------------------------------------- misc routes
def test_page_image_and_delete(client, done_job):
    jid = done_job["id"]
    r = client.get(f"/api/jobs/{jid}/pages/0/2.png")
    assert r.status_code == 200 and r.content[:4] == b"\x89PNG"
    assert client.get(f"/api/jobs/{jid}/pages/0/9.png").status_code == 404
    assert client.get(f"/api/jobs/{jid}/pages/../../x/1.png").status_code in (404, 422)
    assert client.delete(f"/api/jobs/{jid}").status_code == 204
    assert client.get(f"/api/jobs/{jid}").status_code == 404


def test_security_headers_and_index(client):
    r = client.get("/")
    assert r.status_code == 200 and "Process conditions extractor" in r.text
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"
    assert client.get("/static/app.js").status_code == 200


# --------------------------------------------------- consistency + controller
def E(par, canon, case, val, unit="bar", **kw):
    return dict(section="S", row_no=None, parameter=par, canonical=canon, kind="condition", case=case,
                case_label=case, value=val, unit=unit, page=1, **kw)


def V(entries, tag="T1"):
    return normalize([dict(tag=tag, source_pages=[1], has_values=True, entries=entries)])[0:1]


def codes(issues):
    return {i["code"] for i in issues}


def test_validate_p_balance_and_ok_case():
    bad = V([E("P1", "inlet_pressure", "normal", "10"), E("P2", "outlet_pressure", "normal", "5"),
             E("dP", "pressure_drop", "normal", "2")])
    assert "p_balance" in codes(validate(bad, "f"))
    ok = V([E("P1", "inlet_pressure", "normal", "10"), E("P2", "outlet_pressure", "normal", "8"),
            E("dP", "pressure_drop", "normal", "2")])
    assert "p_balance" not in codes(validate(ok, "f"))


def test_validate_p_balance_skipped_for_incompatible_units():
    v = V([E("P1", "inlet_pressure", "normal", "10", "bar"), E("P2", "outlet_pressure", "normal", "100", "psi"),
           E("dP", "pressure_drop", "normal", "2", "bar")])
    assert "p_balance" not in codes(validate(v, "f"))


def test_validate_order_units_unreadable():
    v = V([E("Q", "flow_rate_mass", "minimum", "50", "kg/h"), E("Q", "flow_rate_mass", "normal", "10", "kg/h"),
           E("Q", "flow_rate_mass", "maximum", "100", "kg/h"),
           E("T", "inlet_temperature", "normal", "80", "bar"),
           E("X", "viscosity", "normal", "UNREADABLE", "cP")])
    c = codes(validate(v, "f"))
    assert {"order", "bad_temp_unit", "unreadable"} <= c


def test_reconcile_majority_vote():
    a = normalize([dict(tag="T", source_pages=[1], has_values=True, entries=[E("P1", "inlet_pressure", "normal", "10.5")])])
    b = normalize([dict(tag="T", source_pages=[1], has_values=True, entries=[E("P1", "inlet_pressure", "normal", "10.6")])])
    issues = []
    d = find_disputes(a, b, "f", issues)
    assert len(d) == 1
    apply_adjudication(d, {0: {"value": "10.6", "unit": "bar"}}, "f", issues)
    assert a[0]["entries"][0]["value"] == "10.6" and "pass_disagree" in codes(issues)
    # re-read matches neither -> keep read 1 but mark unresolved error
    a = normalize([dict(tag="T", source_pages=[1], has_values=True, entries=[E("P1", "inlet_pressure", "normal", "10.5")])])
    issues = []
    d = find_disputes(a, b, "f", issues)
    apply_adjudication(d, {0: {"value": "10.9", "unit": "bar"}}, "f", issues)
    assert a[0]["entries"][0]["value"] == "10.5"
    assert [i for i in issues if i["code"] == "unresolved" and i["severity"] == "error"]


def test_to_sheets_unmatched_issue_becomes_sheet_note():
    v = V([E("P1", "inlet_pressure", "normal", "10")])
    issues = [dict(file="f", valve="T1", section="", parameter="", case="", severity="error", code="x", issue="boom")]
    sh = to_sheets(0, "f", v, issues)[0]
    assert sh.notes and not sh.cells[0].flags


def test_textutil():
    assert to_num("1,500") == 1500 and to_num("1.4E+06") == 1.4e6 and to_num("<75") is None
    assert same_value("10.0", "10") and not same_value("10.5", "10.6")
    assert unit_norm("kg/cm²g") == unit_norm("KG/CM2G")


def test_controller_respects_image_limit():
    from PIL import Image
    img = Image.new("RGB", (10, 10))
    for n in (1, 10, 24, 40):
        p = plan_for([(img, "")] * n, 2)
        assert n * p.images_per_page() <= MAX_IMAGES_PER_REQUEST
    dense = plan_for([(img, "x" * 3000)], 2)
    assert dense.cols == 2
    with pytest.raises(ValueError):
        plan_for([(img, "")] * 101, 2)


def test_eval_csv_roundtrip(client, done_job):
    import csv as _csv
    jid = done_job["id"]
    _, c = find(done_job, "Inlet Temperature", "normal")
    client.patch(f"/api/jobs/{jid}/cells/{c['id']}", json={"value": "99.9"})
    out = list(_csv.DictReader(io.StringIO(client.get(f"/api/jobs/{jid}/model-output.csv").text)))
    r = [x for x in out if x["parameter"] == "Inlet Temperature" and x["case"] == "normal" and x["valve"] == "DEMO-101"][0]
    assert r["value"] == "150"          # model value, not the human edit
    flagged = [x for x in out if x["parameter"] == "Flow Rate Mass" and x["case"] == "minimum"][0]
    assert "read this cell" in flagged["flag"]
    truth = list(_csv.DictReader(io.StringIO(client.get(f"/api/jobs/{jid}/truth.csv").text)))
    assert [x["value"] for x in truth if x["parameter"] == "Inlet Temperature" and x["case"] == "normal"] == ["99.9"]


def test_job_interrupted_by_restart_becomes_error(client):
    from app import store
    job = store.create()
    job.status = "processing"
    store.save(job)
    store._jobs.pop(job.id)               # simulate a fresh process reading from disk
    j = client.get(f"/api/jobs/{job.id}").json()
    assert j["status"] == "error" and "restart" in j["error"]
````
