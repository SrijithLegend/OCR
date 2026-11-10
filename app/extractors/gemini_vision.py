"""Gemini vision extractor (free-tier friendly). Same flow as claude_vision: page + zoom tiles + text layer,
JSON out, optional second read, targeted re-read of disputed cells, majority vote.
Needs GEMINI_API_KEY (https://aistudio.google.com/apikey). NOT exercised against the live API."""
import json
import time

from ..config import GEMINI_MODEL
from ..consistency import apply_adjudication, dispute_questions, find_disputes, normalize
from .claude_vision import fit, tiles
from .prompts import ADJ_SYSTEM, ANSWER_TOOL, RECORD_TOOL, SYSTEM


def build_parts(pages, plan):
    parts = []
    for i, (img, text) in enumerate(pages, 1):
        parts += [f"=== PAGE {i} of {len(pages)} ===", fit(img)]
        if len(text) >= 80:
            parts.append(f"TEXT LAYER of page {i} (real characters; ' | ' = column gap):\n{text}")
        if plan.bands > 0 or plan.cols > 1:
            for ri, ci, t in tiles(img, max(plan.bands, 1), max(plan.cols, 1)):
                parts += [f"ZOOM tile row {ri} col {ci} of page {i} (overlaps neighbours)", fit(t)]
    return parts


def call_json(client, system, schema, parts):
    from google.genai import types
    cfg = types.GenerateContentConfig(
        system_instruction=system + "\n\nReturn ONLY JSON matching this schema:\n" + json.dumps(schema),
        response_mime_type="application/json", temperature=0)
    for attempt in range(5):
        try:
            r = client.models.generate_content(model=GEMINI_MODEL, contents=parts, config=cfg)
            return json.loads(r.text)
        except Exception as e:
            # ponytail: free tier is rate limited (429); fixed 30 s backoff, no jitter
            if attempt == 4 or not any(s in str(e) for s in ("429", "503", "RESOURCE_EXHAUSTED", "UNAVAILABLE")):
                raise
            time.sleep(30)


class GeminiVisionExtractor:
    name = "gemini"

    def __init__(self):
        from google import genai  # reads GEMINI_API_KEY / GOOGLE_API_KEY
        self.client = genai.Client()

    def extract(self, pages, plan, fname):
        shared = build_parts(pages, plan)
        ask = "Extract the service/process conditions of every sheet in this document."
        runs = [normalize(call_json(self.client, SYSTEM, RECORD_TOOL["input_schema"], shared + [ask])
                          .get("records", [])) for _ in range(plan.passes)]
        records, issues = runs[0], []
        if len(runs) == 2 and plan.adjudicate:
            disputes = find_disputes(runs[0], runs[1], fname, issues)
            if disputes:
                out = call_json(self.client, ADJ_SYSTEM, ANSWER_TOOL["input_schema"], shared + [
                    "Re-read these cells. Use the question number as id.\n"
                    + "\n".join(dispute_questions(disputes))])
                apply_adjudication(disputes, {a["id"]: a for a in out.get("answers", [])}, fname, issues)
        return records, issues
