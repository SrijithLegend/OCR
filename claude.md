# Task: Measure the real accuracy of Datasheet OCR (for my resume)

Paste this whole file into Claude Code, run from the repo root (`SrijithLegend/OCR`).

## Goal

Produce **measured** numbers I can put in a resume bullet:

- field-level accuracy against hand-checked ground truth
- `--passes 1` vs `--passes 2` ablation
- how well the red/yellow flags catch real errors
- time per sheet vs manual entry

## Hard rules

1. **Never invent or estimate a number.** If something isn't measured, write "not measured".
2. **Never print, log or commit the API key.** It lives only in `.env` (gitignored). My previous key was pasted into a chat, so I have rotated it. Use the new one.
3. **Confidentiality:** the README says files are sent to the Gemini API. Only run Emerson/customer datasheets after I confirm that is approved. Until then, use the public tender PDF only.
4. **Dev/test split.** Tune prompts or validators on a dev set only. Report numbers on a held-out test set that was never used for tuning. If I change the code after seeing test results, re-split.
5. **Don't touch `main.py` extraction logic** except to add logging for timing/token counts, unless I ask. The point is to measure what exists.
6. Ground truth must come from **me reading the document**, not from the tool's output. If you pre-fill anything from tool output, label it `prefilled=1` and I must verify every such cell, and the report must say so.

## Step 0: Setup

- `pip install -r requirements.txt`, create `.env` from `.env.example`, set `GENAI_API_KEY`.
- Record in `results/run_meta.json`: model name (`GEMINI_MODEL` or default in `main.py`), dpi, bands/cols, passes, date, git commit hash.
- Read `main.py` (`pivot`, `write_conditions`, `build_workbook`) to learn the exact output layout before writing any scorer.

## Step 1: Pick the sheets

- Put sheets in `eval/sheets/`. Aim for **20-30 datasheets**, mixed layouts and vendors, original PDFs preferred over screenshots.
- Randomly assign about 30% to `dev/` and about 70% to `test/` (seeded, e.g. `random.seed(0)`). Save the split in `eval/split.json`.
- Public sanity set: pages 15-20 of the control-valve tender spec PDF (the six "CONTROL VALVE DATA SHEET" pages). Extract each page to its own PDF.

## Step 2: Ground truth

- Generate a **blank** template `eval/truth_template.csv` with columns:
  `file, valve_tag, section, parameter, case, unit, value, prefilled`
  One row per cell I need to key. Derive the row list from the PDF structure, not from tool output, where possible. For test files, leave `value` empty.
- I will fill `value` by reading the PDF and save `eval/truth.csv`.
- Rules for me when keying: copy values as printed; unreadable = `UNREADABLE`; empty cell = empty.

## Step 3: Run the tool

For every file in `test/` (and `dev/`):

```bash
python main.py eval/sheets/<file> --passes 1 -o results/p1/<file>.xlsx
python main.py eval/sheets/<file> --passes 2 -o results/p2/<file>.xlsx
```

- Log wall-clock seconds per file, API call count and token usage (add minimal logging; don't change behavior).
- Save the validator issues (red/yellow/blue flags) per cell, e.g. by exporting the `issues` list as JSON.

## Step 4: Write `eval/score.py`

Inputs: `truth.csv` plus an output workbook. Output: per-file and overall metrics as JSON and markdown.

**Normalization before comparing** (document it in the report):
- strip, uppercase, collapse whitespace
- remove thousands separators
- numeric equality via float parse, exact (no tolerance) and also within 0.5% reported separately
- units compared after the tool's own `unit_norm`

**Metrics:**
- **Cell accuracy** = correct cells / ground-truth cells (exact match). Report overall and per section/parameter type (pressure, temperature, flow, text).
- **Extraction recall / precision:** cells the tool produced vs cells in truth (missing / spurious).
- **UNREADABLE rate** and **accuracy on non-UNREADABLE cells**.
- **Flag quality** (the important one):
  - error recall = wrong cells that were flagged red or yellow / all wrong cells
  - flag precision = flagged cells that were actually wrong / all flagged cells
- **Silent error rate** = wrong cells that were NOT flagged / all cells. This is the number that matters for trust.
- **Ablation:** all of the above for `--passes 1` vs `--passes 2`.
- **Uncertainty:** 95% bootstrap CI resampling **files** (not cells), 10,000 resamples, seeded.

## Step 5: Timing (manual baseline)

- I will time myself keying 5 sheets by hand (stopwatch) and record it in `eval/manual_times.csv` as `file, seconds`.
- Compute: tool wall-clock + my review/correction time (I will log that too) vs manual time. Report median and range, N=5.

## Step 6: Fix a known bug in `standard_valve.py`

Rows with no part number are silently dropped. The console says "27 rows" but only 26 are grouped. Change it so excluded rows are counted and listed (new column or second sheet "Excluded rows") and printed in the console summary. Add a tiny test using the 27-row valve-body list in `eval/valve_body_list.xlsx` (expected: 6 part numbers, 1 excluded row, `59A3655` has 3 disagreeing values).

## Step 7: Deliverable `results/RESULTS.md`

Include:

1. Setup (model, dpi, N dev / N test, split seed)
2. Results table: passes 1 vs 2, each metric with 95% CI
3. Error analysis: the 10 worst errors with page crops or cell references, grouped by cause (digit misread, wrong row/column, merged cell, unit, watermark, etc.)
4. Limitations (small N, layouts covered, one model, me as sole annotator)
5. Resume bullet(s), filled **only** from measured values:

```
• Automated extraction of service/process conditions from [N] valve datasheets
  into structured Excel, reaching [XX.X]% cell accuracy ([CI]) vs hand-checked
  ground truth, by building a multimodal pipeline (Gemini API) with 300-dpi
  rendering, zoom tiling and PDF text-layer fusion.
• Cut [metric] from [A]% to [B]% (passes 1 -> 2) via two-pass reads with
  majority-vote adjudication; [XX]% of remaining errors are flagged for review
  by rule-based validators.
```

If N is below 20 files, say so in the bullet or in the interview notes. A small N with an honest CI beats a big claim.

## Done when

- `results/RESULTS.md` exists with all numbers traceable to `results/*.json`
- `eval/score.py` re-runs end to end with one command
- No API key in git history (`git log -p | grep -i genai_api_key` shows nothing)