# Datasheet OCR

Reads valve datasheets (PDF, PNG, JPG) with the Gemini API and writes everything printed on them to one Excel sheet: process / service conditions (min / normal / max), body, trim, actuator, materials, dimensions, notes, revision tables and header fields. Parameters are columns (header: section, parameter, unit); each row is one valve's Min / Normal / Max / Design / Single value set.

It also has a **Search** tab: ask any SQLite database a question in plain English and get an exact answer plus the SQL that produced it (see [Ask the database](#ask-the-database-search-tab)).

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
2. **Review.** Parameters are columns and Min / Normal / Max are rows; every value is an editable cell. Red = error, yellow = check, blue = edited by you. Use *Next issue* to jump between flagged cells, *Show only columns needing review* to filter, and the `p1` link to open the source page.
3. **Export to Excel.** Your edits are included; flags on cells you fixed are dropped.

**Standardize descriptions** (second tab): upload an .xlsx/.csv of valve descriptions (`ITEM ~ SIZE, CLASS ... PARTNO`). Each part number gets one standard description (`ITEM, MODIFIER, SIZE UNIT, CLASS, END, VARIANT`) by majority vote; disagreements are flagged, never guessed. Runs locally (no Gemini call); *Export to Excel* downloads the result. Also a CLI: `python standard_valve.py in.xlsx -o out.xlsx`.

**Search** (third tab): ask a question about a database. See the next section.

OCR uploads are not stored. Databases uploaded in the Search tab are kept in a private temp folder (newest 5) until the server stops. There is no login, so keep it bound to 127.0.0.1.

## Ask the database (Search tab)

Ask any SQLite file a question ("how many rows per world area?", "average price of Primary constructions", "which user created the most workspaces?"). Nothing about a particular database is hardcoded.

1. **Schema.** `nlsql.py` reads the tables, columns, types and distinct counts from the file itself, plus the value list of columns with few distinct values (25 or fewer).
2. **Value hints.** Words in your question that are real values in the data (an office name, a workspace name) are found locally and passed along, so the model filters on the exact spelling.
3. **SQL.** Gemini writes one `SELECT`.
4. **Safe run.** It executes on a read-only connection with a SELECT-only authorizer, a 15 s timeout and a 500-row cap. `DROP`, `DELETE`, `UPDATE`, `PRAGMA`, `ATTACH` and chained statements are refused. A query that errors is retried once with the error.
5. **Answer.** A number or table, a one-line note saying what was counted, and a *SQL used* panel. Read the note: "items" can mean rows or distinct ids, and the note says which.

**Use it**
- Web: the **Search** tab. *Use another database...* uploads a `.db`, `.sqlite`, `.sqlite3` or `.db3` file (up to 500 MB, checked to be SQLite).
- CLI: `python nlsql.py --db other.db "your question"`. With no question it opens a prompt. `python search.py` is a shortcut to the same thing.
- Default database: `shop.db`, or the path in `SHOP_DB`.

**What is sent to the Gemini API:** your question, table and column names and types, the short value lists above, and any value you typed that exists in the data. Never row data or query results.

**Limits**
- The model can write wrong SQL. Check the note and the SQL on anything that matters.
- Free-tier Gemini allows about 15 requests per minute. If it is hit, the page says how long to wait.
- If the database cannot answer the question, it says so instead of guessing.

| Setting (env) | Default | Meaning |
|---|---|---|
| `SHOP_DB` | `shop.db` | database used when none is uploaded |
| `GEMINI_MODEL` | `gemini-3.5-flash-lite` | model id (shared with the OCR) |
| `NLSQL_MAX_ROWS` | 500 | rows returned |
| `NLSQL_MAX_VALUES` | 25 | list values for columns with at most this many distinct values |
| `NLSQL_RATE_WAIT` | 70 | longest wait (seconds) for a rate limit before reporting it |

**Tests:** `python -m pytest eval/test_search.py -q` asks 52 questions (42 on `shop.db`, 8 on a small library database built on the fly, plus a refusal and a safety check) and compares each with independent SQL. It needs `GENAI_API_KEY` and takes about 5 minutes because it paces itself to the rate limit.

### Check ids (Search tab, any database)

Any `.db` / `.sqlite` file in the app folder appears in a dropdown at the top of the Search tab (or upload one). *Check ids* finds the catalogue-style table by itself: one with an id column, a name/title column and a price column (description and attribute/spec columns are used if present). No table or column names are built in.

1. **Which ids are wrong?** If the ids end in two check characters computed from name + attributes (the sample dataset does), a row that fails the check is wrong. Otherwise the table's own redundancy decides: a part (name + attributes) should have one id and an id one part; the minority loses, a tie is shown as *Ambiguous* and never changed. If most rows disagree, the table is not a catalogue and it says so.
2. **From the database:** the same part elsewhere with a good id gives the id and price.
3. **From the web:** otherwise Gemini with Google Search finds the price (in the currency of other items), and the row gets a new id shaped like its nearest neighbour's (next free number). If that fails the row shows *Unresolved*; nothing is guessed. At most 200 web lookups per run.

The page only shows the fixes. `python idfix.py [db] --apply` writes them. Sent to Gemini in step 3: that part's name, description and attributes plus about six other items of the table (name, attributes, price). Needs a key with Google Search grounding quota ("Google Search quota exhausted" means enable billing or set `GROUND_MODEL`). Tests: `python -m pytest eval/test_idfix.py -q` (offline).

`python gen_bikes.py` builds a sample, `bike_parts.db` (133 Royal Enfield Classic 350 parts, 30% with another part's id and price; answers in `bike_parts_truth.csv`).

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

A measured run on public tender sheets is in `results/RESULTS.md` (small sample, and the ground truth was keyed with AI help rather than by hand, so read its limitations before quoting a number). Re-score with `python eval/score.py`. It says nothing about your sheets: try one short PDF with `--passes 1` and compare every cell with the page before trusting a batch. Review red and yellow cells first, then spot-check the rest.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `KeyError: 'GENAI_API_KEY'` | Put the key in `.env` next to `main.py`. |
| 413 / 415 error in the UI | File over 50 MB, or not PDF/PNG/JPG. |
| Many `UNREADABLE` cells | Use the original PDF, or raise `--dpi` / `--bands`. |
| Slow | Use `--passes 1`. |
| Search says "rate limit reached" | Free-tier Gemini: wait the number of seconds shown, then ask again. |
| Search says "GENAI_API_KEY is not set" | Put the key in `.env` next to `nlsql.py`. |
| Search says the database is no longer loaded | Uploads last until the server restarts; upload the file again. |

## Files

- `main.py`: prompts, extraction, checks, Excel writer, CLI
- `web.py`, `web.html`: web UI (`/extract`, `/export`, `/standardize`, `/search`, `/search/db`, `/search/info`)
- `standard_valve.py`: description standardizer (used by the web tab and the CLI)
- `nlsql.py`: plain-English questions over any SQLite file (schema reader, SQL generation, sandboxed run); `search.py` is a shim that runs it
- `eval/`: accuracy harness for the OCR (`score.py`, ground truth, tests) and `test_search.py` for the database search
- `build_index.py`, `sync.py`: leftovers from an earlier embedding-based search; nothing uses them now
