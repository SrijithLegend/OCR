# Datasheet OCR

Reads valve datasheets (PDF, PNG, JPG) with the Gemini API and writes everything printed on them to one Excel sheet: process / service conditions (min / normal / max), body, trim, actuator, materials, dimensions, notes, revision tables and header fields. Each row has a `Kind` (condition, design, calculated, spec) for filtering.

> Files are sent to the Gemini API. Do not upload anything you are not allowed to share with it.

## Quick start

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows;  source .venv/bin/activate on Linux/macOS
pip install -r requirements.txt
copy .env.example .env          # Windows (cp on Linux/macOS), then set GENAI_API_KEY in .env
uvicorn web:app --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000 and drop in your files.

## Using the web UI

1. **Upload** PDFs or images (up to 50 MB each). Original PDFs read far better than screenshots. A few minutes per file is normal; keep the tab open.
2. **Review.** Every value is an editable cell. Red = error, yellow = check, blue = edited by you. Use *Next issue* to jump between flagged cells, *Show only rows needing review* to filter, and the `p1` link to open the source page.
3. **Export to Excel.** Your edits are included; flags on cells you fixed are dropped.

Nothing is stored on the server. There is no login, so keep it bound to 127.0.0.1.

## Command line

```bash
python main.py sheet1.pdf sheet2.png -o process_conditions.xlsx
```

With no files it processes every PDF/image next to `main.py`.

| Option | Default | Meaning |
|---|---|---|
| `-o`, `--out` | `process_conditions.xlsx` | output file |
| `--passes {1,2}` | 2 | 1 = one read (cheaper, faster) |
| `--dpi` | 300 | render resolution |
| `--bands` | 3 | vertical zoom tiles per page |
| `--cols` | 1 | horizontal zoom tiles per page |
| `--model` | `GEMINI_MODEL` | Gemini model id |

The web UI reads the same settings from env vars: `PASSES` (default 1), `DPI` (300), `BANDS` (default 0), `COLS` (1).

## How it works

1. Each page is rendered at 300 dpi with zoom bands and sent to Gemini together with the PDF text layer, if there is one.
2. With `--passes 2` the file is read twice. Cells where the reads disagree get a targeted re-read and the majority wins.
3. Plain code (not the model) checks the result: P1 - dP = P2, min <= normal <= max, units, plausible ranges, duplicates, unreadable cells.
4. Values are copied as printed, never computed or guessed. Unclear cells are `UNREADABLE`.

## Accuracy

Not measured on your sheets. Try one short PDF with `--passes 1` and compare every cell with the page before trusting a batch. Review red and yellow cells first, then spot-check the rest.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `KeyError: 'GENAI_API_KEY'` | Put the key in `.env` next to `main.py`. |
| 413 / 415 error in the UI | File over 50 MB, or not PDF/PNG/JPG. |
| Many `UNREADABLE` cells | Use the original PDF, or raise `--dpi` / `--bands`. |
| Slow | Use `--passes 1`. |

## Files

- `main.py`: prompts, extraction, checks, Excel writer, CLI
- `web.py`, `web.html`: web UI (`/extract`, `/export`)
