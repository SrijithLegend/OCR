# Valve Datasheet OCR

Turn valve datasheets (PDF or image) into a clean Excel table of their **process / service conditions**: flow, pressures, temperatures and fluid properties for the minimum, normal and maximum cases.

It reads the sheets with **DeepSeek-OCR-2**, a free OCR model that runs on your own computer. Your files never leave your machine.

---

## What you get

1. Upload one or more datasheets in your browser (click or drag and drop).
2. The app reads them and shows one table per valve.
3. Cells that look wrong are coloured, with the reason on hover:
   - **Red**: probably an error. Check it.
   - **Yellow**: worth a second look.
   - **Blue**: you edited it.
   - **Green**: you marked it as reviewed.
4. Fix any cell, press **OK** on rows you checked, then click **Export to Excel**.

---

## How it stays accurate

The OCR model is good but not perfect, so the app double-checks it with plain code:

- **Two reads of every page.** If the two reads disagree on a value, the app decides using the PDF's own text (or a third read for scans). If it still cannot tell, it leaves the cell red for you instead of guessing.
- **Every number must be real.** A number that does not appear in the page's text is flagged. If it is just one character off from a number that does appear, it is corrected and flagged.
- **Sanity checks.** For example, inlet pressure minus pressure drop must equal outlet pressure, and minimum must not be bigger than maximum.
- **Values are copied exactly as printed.** Nothing is rounded, converted or invented.

No tool is 100% accurate. Always review the red and yellow cells, and spot-check the rest.

---

## What you need

| Requirement | Notes |
|---|---|
| Windows, Linux or macOS | The OCR model needs an NVIDIA GPU, so Windows or Linux in practice |
| Python 3.11 or newer | https://www.python.org/downloads/ |
| NVIDIA GPU, 6 GB or more | 4-bit mode is used by default so it fits small GPUs |
| About 10 GB free disk | For Python packages and the model download |
| 8 GB or more free RAM | The model needs room while it loads |

No GPU? You can still try the app with demo data (see below).

---

## Install

Open a terminal in this folder, then:

```bash
# 1. Create and activate a virtual environment
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # Linux / macOS

# 2. Install PyTorch with GPU support (large download, about 2.5 GB)
pip install torch==2.6.0 --index-url https://download.pytorch.org/whl/cu124

# 3. Install everything else
pip install -r requirements.txt
```

Check that your GPU is visible:

```bash
python -c "import torch; print(torch.cuda.is_available())"
```

It should print `True`.

---

## Run

**Try it with demo data** (no GPU, no download):

```bash
# Windows PowerShell
$env:EXTRACTOR="stub"; uvicorn app.main:app

# Linux / macOS
EXTRACTOR=stub uvicorn app.main:app
```

**Real extraction** (uses the GPU):

```bash
# Windows PowerShell
$env:EXTRACTOR="deepseek"; uvicorn app.main:app

# Linux / macOS
EXTRACTOR=deepseek uvicorn app.main:app
```

Then open **http://127.0.0.1:8000** in your browser.

The first real run downloads the model (several GB) and takes a few minutes to start. Later runs are faster.

Tip: start with **one short PDF** and compare every extracted value with the page before processing a big batch.

---

## Settings

Set these as environment variables (see `.env.example` for the full list).

| Variable | Default | What it does |
|---|---|---|
| `EXTRACTOR` | `deepseek` | `deepseek` (local GPU), `stub` (demo data) or `claude` (needs an API key) |
| `PASSES` | `2` | `2` = two reads and cross-checking (more accurate), `1` = one read (faster) |
| `DEEPSEEK_4BIT` | `1` | `0` uses full precision, which needs about 8 GB of GPU memory |
| `MODEL_DPI` | `300` | Resolution used to render PDF pages for the model |
| `MAX_PAGES` | `40` | Page limit per file |

---

## Project layout

```text
app/
  main.py              web server and API
  pipeline.py          the job: read pages -> extract -> check -> results
  consistency.py       accuracy checks (pressure balance, units, disagreements)
  excel.py             Excel export
  extractors/
    deepseek_ocr.py    DeepSeek-OCR-2 reader (two reads + evidence checks)
    mdparse.py         turns OCR tables into structured values
    stub.py            demo data
  static/              the web page
tests/                 automated tests (no GPU needed)
score.py               measure accuracy against hand-checked answers
claude.md              detailed design guide
```

---

## Test

```bash
pytest -q
```

The tests run without a GPU or an API key.

---

## Measure your accuracy

Do this before trusting the results for real work.

1. Process 5 to 10 real datasheets.
2. Check every cell in the page against the original, correct mistakes and press **OK**.
3. Download `truth.csv` and `model-output.csv` from the job.
4. Run:

```bash
python score.py truth.csv model-output.csv
```

The number to watch is **silent errors**: wrong values that were not flagged. The goal is zero.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `No module named 'torch'` | Run step 2 of the install, inside the virtual environment |
| `torch.cuda.is_available()` is `False` | Install the NVIDIA driver, and use the CUDA build of PyTorch from the command above |
| Out of GPU memory | Close other GPU programs, keep `DEEPSEEK_4BIT=1`, lower `MODEL_DPI` to `200` |
| Download times out | Re-run the same `pip install` command; add `--default-timeout=300` |
| A value is wrong or missing | Open the page link in the table and compare. Fix the cell by hand |

---

## Privacy and security

- With `EXTRACTOR=deepseek` everything runs locally.
- The server listens on `127.0.0.1` only and has no login. If you expose it to a network, put it behind your own authentication first.
- The OCR model's code is downloaded from Hugging Face and run on your machine. For stricter control, set `DEEPSEEK_REVISION` to a specific commit you have reviewed.

---

## Limits

- One valve sheet per page tag. Two different sheets on one page are merged.
- Parameter names are matched by keyword rules. Unfamiliar labels are kept but marked as `other`.
- Scanned pages with no text layer get weaker checking than PDFs, because there is less to compare against.
- Accuracy on your own sheets has to be measured. See *Measure your accuracy*.
