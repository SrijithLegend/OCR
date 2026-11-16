# Datasheet OCR: measured accuracy

All numbers come from `results/scores.json` (regenerate: `python eval/score.py`) and `results/p{1,2}/*.json`.
Nothing here is estimated. Anything not measured says "not measured".

## 1. Setup

| | |
|---|---|
| Model | `gemini-3.5-flash-lite` (default in `main.py`) |
| Render | 300 dpi, 3 bands x 1 col, PDF text-layer fusion (defaults) |
| Sheets | 6 public pages (pp. 15-20, BPCL control-valve tender spec), single-page PDFs |
| Split | seed 0, `eval/split.json`: **2 dev** (p17, p19), **4 test** (p15, p16, p18, p20) |
| Headline | held-out test only. Dev pages were not used to tune anything either, since `main.py` extraction logic and validators were not changed (only timing/token logging was added) |
| Scoring | normalisation: strip, uppercase, collapse spaces, drop thousands separators; numeric compare by float; 95 % CI = bootstrap over **files**, 10,000 resamples, seed 0 |
| Customer data | not run. The three `.jpeg` sheets were left out pending approval (CLAUDE.md rule 3) |

## 2. Results (test set, N = 4 files, 127 ground-truth cells)

| Metric | `--passes 1` | `--passes 2` |
|---|---|---|
| Cell accuracy, strict (value **and** unit) | 51.2 % [38.5, 64.5] | **69.3 %** [58.2, 86.9] |
| Cell accuracy, value only (unit ignored) | 70.1 % [46.2, 82.7] | **91.3 %** [70.4, 100.0] |
| Cell accuracy, within 0.5 % | 51.2 % [38.5, 64.5] | 69.3 % [58.2, 86.9] |
| UNREADABLE rate | 0.0 % | 0.0 % |
| Extraction recall (truth cells the tool produced) | 72.4 % [46.2, 84.1] | 91.3 % [70.4, 100.0] |
| Extraction precision (tool cells that match a truth cell) | 72.4 % [46.2, 86.1] | 78.4 % [55.6, 91.5] |
| Error recall (wrong cells flagged red/yellow) | 37.1 % [0.0, 56.1] (23/62) | 71.8 % [0.0, 100.0] (28/39) |
| Flag precision (flagged cells that were wrong) | 100 % [100, 100] (23/23) | 69.0 % [57.0, 76.6] (49/71) |
| Silent-error rate (wrong and unflagged, / all cells) | 30.7 % [17.3, 53.8] (39/127) | 8.7 % [0.0, 29.6] (11/127) |

Per file, strict accuracy, pass 1 -> pass 2: p15 68.2 -> 68.2 %, p16 38.6 -> 63.6 %, p18 38.1 -> 100 %, p20 55.6 -> 50.0 %.
By type (pass 2, strict): text/other 80.3 % (76 cells), flow 63.6 % (33), temperature 33.3 % (12), pressure 33.3 % (6).
Dev pages (N = 2, reported for transparency, not tuned on): strict 80.9 % (p1) / 75.5 % (p2). Pass 2 was *not* better on dev, so the gain is not consistent across the 6 pages.

Cost on the 4 test files: pass 1 = 158 s total (39.5 s/file mean), 4 API calls, 27,908 prompt + 41,822 output tokens;
pass 2 = 258 s total (64.5 s/file mean), 12 calls, 81,243 prompt + 85,671 output tokens.
Manual entry took about 2 min per sheet (reported by the repo owner, stopwatch; per-file times not recorded), while the tool ran unattended in 40-65 s per sheet (pass 1 / pass 2, run time only). Review/correction time: **not measured**, so no net time-saved claim is made (`eval/manual_times.csv` not filled).

## 3. Error analysis (pass 2, test)

All 39 strict errors fall into three causes (cell references are `file / valve / parameter`):

1. **Unit not inferred from the sheet header: 28 cells, value correct, all flagged.** The unit is printed only in the header
   (`Pressure: Kg/cm2g`, `Temperature: °C`) or row label, and the tool left it blank.
   p15 and p16, all four valves: `Temp. Oper.` Normal/Max (8), `dP @FlowRateMin` Min/Normal/Max (12), `dPressure Shut Off` (4);
   p16 C/D: `Vapour Pr.` and `Viscosity (cp)` (4).
2. **Case label wrong, so the cell is "missing" and a spurious one appears elsewhere: 8 cells (p20, ULV5151).**
   `Fluid` (WATER), `State` (LIQUID), `Cv Min/Max/Nor`, `Selected Cv`, `Inlet`, `Outlet` velocity were filed under
   Minimum/Normal instead of a single value. This follows my keying choice (see Limitations).
3. **Cell not produced: 3 cells.** p15 DFV-158A and DFV-158B `Selected Cv` ("*"); p20 `Inlet Pr. - Normal` Maximum = 4.
   I did not investigate why (merged/split cell is a guess).

Wrong digits / misread values: **0 cells.** Every value the tool produced that matched a truth cell was correct (value-only accuracy
equals extraction recall, 91.3 %). All 11 unflagged errors are missing cells, which a cell-level flag cannot mark.
Page crops were not produced, and I did not check for watermark effects.

Pass 1 -> 2 gain came from fewer missing cells (35 -> 11). Unit-only errors did not improve (24 -> 28).
Spurious tool cells were 35 -> 32; pass 2 also produced more flags (71 vs 23).

## 4. Limitations

- **Ground truth was keyed by Claude, not a human** (`eval/TRUTH_NOTES.md`), unverified by anyone else.
  Treat these numbers as provisional until you spot-check a sample of cells.
- **Small N and one template.** 4 test files, and p15/p16 are near-identical, so about 3 distinct layouts. CIs are wide
  (error recall 0-100 %). One vendor (BPCL tender), one model, one run per config (no variance across reruns measured).
- Strict accuracy depends on my unit and case-label conventions (see notes). The value-only row is the less convention-sensitive one.
- The truth row list started from tool output keys (plus 15 added rows), so cells the tool never produced and I did not add are not counted.
- Truth units and the extra `ignoring_unit` metric were chosen after seeing aggregate test results once.
- No customer datasheets, no scanned/photographed sheets, no handwriting.
- Wall-clock times are from this machine, which had low free RAM during the first attempt (those crashed runs were discarded and re-run).

## 5. Resume bullets (filled only from the numbers above)

> - Built and evaluated a multimodal (Gemini API) pipeline extracting service/process conditions from valve datasheets into
>   structured Excel: on **4 held-out sheets** (127 cells, one tender template, AI-keyed ground truth) it reached **91.3 %
>   value accuracy** (95 % CI 70-100 %) and 69.3 % strict value+unit accuracy; **N is small**.
> - Raised strict cell accuracy from 51.2 % to 69.3 % (passes 1 -> 2) via two-pass reads with majority-vote adjudication; rule-based
>   validators flagged 71.8 % of the remaining wrong cells (CI 0-100 %), leaving 8.7 % of cells as silent errors.

Interview notes: N = 4 test files from one public template; ground truth read by an AI annotator and not human-verified;
time-vs-manual comparison not measured. Don't claim more than that.
