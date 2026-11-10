# Datasheet OCR: notes for Claude

Gemini-based extractor of everything printed on valve datasheets (process conditions and specs) to Excel. See README.md for setup and usage.

- `main.py` holds everything: prompts, Gemini calls, dispute re-read, `validate`, `build_workbook`, CLI. `web.py` (FastAPI) imports it; `web.html` is the single-page UI.
- Rules that carry accuracy, do not "improve" casually: map columns by printed header text, copy values exactly, `UNREADABLE` instead of guessing, code (not the model) verifies, a blank template gives no values.
- No test suite. Verify by running `python main.py <one pdf> --passes 1` and checking cells against the page.
- Needs `GENAI_API_KEY` in `.env` (read by `main.py`). Single process, no auth: bind to 127.0.0.1.
