# Datasheet OCR

Reads valve datasheets (PDF, PNG, JPG) with the Gemini API and writes the **process / service conditions** (flow, pressures, temperatures, fluid properties; min / normal / max) to an Excel file. Files are sent to the Gemini API.

## How it works

1. Each page is rendered (300 dpi) with zoom bands and sent to Gemini with the PDF text layer, if there is one.
2. With `--passes 2` (default) the file is read twice. Cells where the reads disagree get a targeted re-read, and the majority wins.
3. Plain code checks the result: P1 - dP = P2, min <= normal <= max, units, plausible ranges, duplicates, unreadable cells.
4. Values are copied as printed, never computed or guessed. Unclear cells are `UNREADABLE`.
5. Excel output colours cells by severity (red = error, yellow = check). Review those first, then spot-check the rest.

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows;  source .venv/bin/activate on Linux/macOS
pip install -r requirements.txt
copy .env.example .env          # then put your key in GENAI_API_KEY
```

## Run

Web UI (upload, edit the table, export):

```bash
uvicorn web:app --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000. Nothing is stored on the server. There is no login, so keep it on 127.0.0.1.

Command line:

```bash
python main.py sheet1.pdf sheet2.png -o process_conditions.xlsx
```

With no files it processes every PDF/image next to `main.py`.

| Option (CLI) | Default | Meaning |
|---|---|---|
| `--passes {1,2}` | 2 | 1 = one read (cheaper) |
| `--dpi` | 300 | render resolution |
| `--bands` | 3 | vertical zoom tiles per page |
| `--cols` | 1 | horizontal zoom tiles per page |
| `--model` | `GEMINI_MODEL` | Gemini model id |

The web UI takes the same settings from the env vars `PASSES` (default 1), `DPI`, `BANDS` (default 0) and `COLS`.

Try one short PDF with `--passes 1` first and compare every cell with the page. Accuracy on your sheets has not been measured.

## Files

- `main.py`: prompts, extraction, checks, Excel writer, CLI
- `web.py`, `web.html`: web UI (`/extract`, `/export`)
