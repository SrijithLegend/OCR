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
