# Datasheet OCR & Data Assistant

A local web app built around the Gemini API with three tools:

| Tool | What it does |
|---|---|
| **OCR** | Reads valve datasheets (PDF, PNG, JPG) and writes everything printed on them to one Excel sheet: process / service conditions (min / normal / max), body, trim, actuator, materials, dimensions, notes, revision tables and header fields. |
| **Standardize descriptions** | Turns messy valve descriptions into one standard description per part number, locally, by majority vote. |
| **Search** | Ask any SQLite database a question in plain English and get an exact answer plus the SQL behind it. Also **Check ids**: finds rows whose id belongs to a different part and repairs the id and price. |

> Files and questions are sent to the Gemini API. Do not upload anything you are not allowed to share with it.

## Contents

[Quick start](#quick-start) · [OCR](#ocr) · [Standardize descriptions](#standardize-descriptions) · [Search](#search-ask-the-database) · [Check ids](#check-ids-repair-wrong-part-ids) · [Sample data](#sample-data) · [Command line](#command-line) · [Configuration](#configuration) · [Tests](#tests) · [Tech stack](#tech-stack) · [Project layout](#project-layout) · [Troubleshooting](#troubleshooting)

## Quick start

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows;  source .venv/bin/activate on Linux/macOS
pip install -r requirements.txt
copy .env.example .env          # Windows (cp on Linux/macOS), then set GENAI_API_KEY in .env
uvicorn web:app --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000. There is no login, so keep the server bound to `127.0.0.1`. OCR uploads are not stored. Databases uploaded in the Search tab live in a private temp folder (newest 5) until the server stops.

## OCR

1. **Upload** PDFs or images (up to 50 MB each). Original PDFs read far better than screenshots. A few minutes per file is normal; keep the tab open.
2. **Review.** Parameters are columns and Min / Normal / Max are rows; every value is an editable cell. Red = error, yellow = check, blue = edited by you. *Next issue* jumps between flagged cells, *Show only columns needing review* filters, and the `p1` link opens the source page.
3. **Export to Excel.** Your edits are included; flags on cells you fixed are dropped.

**How it reads a sheet**
1. Each page is rendered at 300 dpi with zoom bands and sent to Gemini together with the PDF text layer, if there is one.
2. With `--passes 2` the file is read twice. Cells where the reads disagree get a targeted re-read and the majority wins.
3. Plain code (not the model) checks the result: P1 - dP = P2, min <= normal <= max, units, plausible ranges, duplicates, unreadable cells.
4. Values are copied as printed, never computed or guessed. Unclear cells are `UNREADABLE`.

**Accuracy.** A measured run on public tender sheets is in `results/RESULTS.md` (small sample; the ground truth was keyed with AI help, so read its limitations before quoting a number). Re-score with `python eval/score.py`. It says nothing about your sheets: try one short PDF with `--passes 1` and compare every cell with the page before trusting a batch.

## Standardize descriptions

Upload an `.xlsx` or `.csv` of valve descriptions (`ITEM ~ SIZE, CLASS ... PARTNO`). Each part number gets one standard description (`ITEM, MODIFIER, SIZE UNIT, CLASS, END, VARIANT`) by majority vote; disagreements are flagged, never guessed. Runs locally (no Gemini call). *Export to Excel* downloads the result. CLI: `python standard_valve.py in.xlsx -o out.xlsx`.

## Search: ask the database

Ask any SQLite file a question ("how many rows per world area?", "which user created the most workspaces?"). Nothing about a particular database is hardcoded.

1. **Schema.** `nlsql.py` reads tables, columns, types and distinct counts from the file itself, plus the value list of columns with few distinct values (25 or fewer).
2. **Value hints.** Words in the question that are real values in the data are found locally and passed along, so the model filters on the exact spelling.
3. **SQL.** Gemini writes one `SELECT`.
4. **Safe run.** It executes on a read-only connection with a SELECT-only authorizer, a 15 s timeout and a 500-row cap. `DROP`, `DELETE`, `UPDATE`, `PRAGMA`, `ATTACH` and chained statements are refused. A query that errors is retried once with the error.
5. **Answer.** A number or table, a one-line note saying what was counted, and a *SQL used* panel.

**Choosing a database.** Every `.db` / `.sqlite` / `.sqlite3` / `.db3` file in the app folder appears in a dropdown at the top of the tab. *Use another database...* uploads one (up to 500 MB, checked to be SQLite). The default is `shop.db`, or the path in `SHOP_DB`.

**CLI:** `python nlsql.py --db other.db "your question"` (no question opens a prompt; `python search.py` is a shortcut).

**Sent to Gemini:** your question, table and column names and types, the short value lists above, and any value you typed that exists in the data. Never row data or query results.

**Limits.** The model can write wrong SQL, so check the note and the SQL on anything that matters. Free-tier Gemini allows about 15 requests per minute; the page says how long to wait. If the database cannot answer, it says so instead of guessing.

## Check ids: repair wrong part ids

For catalogue-style data where some rows carry another part's id (and that part's price), for example a white seat listed with the yellow seat's id and price. Click **Check ids** in the Search tab. The table and columns are found automatically: any table with an id column, a name/title column and a price column (description and attribute/spec columns are used when present). No table or column names are built in.

**1. Which ids are wrong?**
- If ids end in two check characters computed from name + attributes (the [sample data](#sample-data) does), a row that fails the check is wrong.
- Otherwise the table's own redundancy decides: a part (name + attributes) should have one id, and an id should belong to one part. The minority loses. A tie is shown as *Ambiguous* and never changed.
- If most rows disagree, the table is not a catalogue and the tool says so.

**2. Fix from the database.** The same part (name + attributes) elsewhere with a valid id gives the id and price.

**3. Fix from the web.** Otherwise Gemini with Google Search finds the price, in the currency of other items in the table. The row gets a new id shaped like its nearest neighbour's (next free number, check characters recomputed). If the lookup fails the row is *Unresolved*; nothing is guessed. At most 200 web lookups per run.

**Result table.** Summary counts (rows checked, wrong ids, fixed from database, priced from web, need a look), then one line per fixed row: old id and price struck through, new id and price, and a label saying how it was fixed.

The page only shows the fixes. To write them: `python idfix.py [db] --apply`.

**Sent to Gemini (step 3 only):** that part's name, description and attributes, plus about six other items of the table (name, attributes, price) so it can match currency and scale.

**Requirement.** Step 3 needs a key with Google Search grounding quota. "Google Search quota exhausted" means enable billing on the key or set `GROUND_MODEL` to a model that has it.

## Sample data

`python gen_sample_db.py` builds a sample catalogue of 133 motorcycle parts (seeded, identical every run) with 30% of the rows carrying another part's id and price:

| File | Contents |
|---|---|
| `sample_parts.db` | table `parts`: part_id, part_name, part_description, part_attribute, part_price |
| `sample_parts.xlsx` | Excel copy: sheet `parts` and sheet `answer_key` |
| `sample_parts_truth.csv` | the correct id and price of every row, for scoring |

These are generated files and are not committed (`*.db` and `sample_parts*` are git-ignored). Prices are rough figures.

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

## Configuration

Set in `.env` next to the code (see `.env.example`).

| Setting | Default | Meaning |
|---|---|---|
| `GENAI_API_KEY` | none | Gemini API key (required) |
| `GEMINI_MODEL` | `gemini-3.5-flash-lite` | model id, shared by OCR and Search |
| `GROUND_MODEL` | `GEMINI_MODEL` | model used with Google Search in Check ids |
| `SHOP_DB` | `shop.db` | database used when none is chosen |
| `NLSQL_MAX_ROWS` | 500 | rows returned by a search |
| `NLSQL_MAX_VALUES` | 25 | list values for columns with at most this many distinct values |
| `NLSQL_RATE_WAIT` | 70 | longest wait (seconds) for a rate limit before reporting it |

## Tests

| Command | Covers | Needs the API |
|---|---|---|
| `python -m pytest eval/test_idfix.py -q` | Check ids: detection on the sample (exactly the 30% flagged, database fixes equal the answer key), a database with different column names and no check characters, web reply parsing and failure | no |
| `python -m pytest eval/test_search.py -q` | Search: 52 questions (42 on `shop.db`, 8 on a library database built on the fly, a refusal and a safety check) compared with independent SQL; about 5 minutes because it paces itself to the rate limit | yes |
| `python eval/score.py` | OCR accuracy against the ground truth | no |

## Tech stack

| Area | Used |
|---|---|
| Language | Python 3.12 |
| Web server | FastAPI, Uvicorn |
| Front end | One HTML file, vanilla JavaScript and CSS (no framework, no build step) |
| AI | Google Gemini API (`google-genai`): vision for OCR, SQL generation for Search, Google Search grounding for web prices |
| PDF / images | PyMuPDF, Pillow |
| Excel | openpyxl |
| Database | SQLite via the standard library `sqlite3`, opened read-only |
| Checks | `hashlib` (check characters), `re`, `collections` |
| Tests | pytest |

## Project layout

| File | Purpose |
|---|---|
| `main.py` | OCR prompts, extraction, checks, Excel writer, CLI |
| `web.py`, `web.html` | web UI and API: `/extract`, `/export`, `/standardize`, `/search`, `/search/db`, `/search/info`, `/search/local`, `/search/fix` |
| `standard_valve.py` | description standardizer (web tab and CLI) |
| `nlsql.py` | plain-English questions over any SQLite file; `search.py` is a shim that runs it |
| `idfix.py` | wrong-id detection and repair for any catalogue table (CLI: `python idfix.py [db] [--apply]`) |
| `gen_sample_db.py` | builds the sample catalogue, its answer key and Excel copy |
| `eval/` | OCR scoring (`score.py`, ground truth) and the test files |
| `build_index.py`, `sync.py` | leftovers from an earlier embedding-based search; nothing uses them now |

## Troubleshooting

| Symptom | Fix |
|---|---|
| `KeyError: 'GENAI_API_KEY'` | Put the key in `.env` next to `main.py`. |
| 413 / 415 error in the UI | File over 50 MB, or not PDF/PNG/JPG. |
| Many `UNREADABLE` cells | Use the original PDF, or raise `--dpi` / `--bands`. |
| OCR is slow | Use `--passes 1`. |
| Search says "rate limit reached" | Free-tier Gemini: wait the number of seconds shown, then ask again. |
| Search says "GENAI_API_KEY is not set" | Put the key in `.env` next to `nlsql.py`. |
| Search says the database is no longer loaded | Uploads last until the server restarts; upload the file again. |
| Check ids says the table is not a catalogue | Most rows disagree about their id; the table has no per-part ids to repair. |
| Check ids shows *Unresolved: Google Search quota exhausted* | Enable billing on the key or set `GROUND_MODEL`. |
