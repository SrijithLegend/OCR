"""No-AI extractor: reads the PDF text layer (real characters, so no misread digits) and parses it with
the same header-mapping rules as mdparse. Instant, deterministic, needs no GPU / API key.
Pages without a text layer (scans) go through a CPU OCR (RapidOCR) if installed; OCR'd pages are
flagged `ocr_page` (check) because OCR can misread digits. Without RapidOCR they get an error note."""
from ..consistency import issue, normalize
from ..ingest import words_to_text
from . import mdparse as mp

_OCR = None


def ocr_text(img):
    """-> layout text from RapidOCR, or None when it is not installed."""
    global _OCR
    if _OCR is None:
        try:
            from rapidocr_onnxruntime import RapidOCR
        except ImportError:
            return None
        _OCR = RapidOCR()
    import numpy as np
    res, _ = _OCR(np.asarray(img.convert("RGB")))
    return words_to_text([(b[0][0], b[0][1], b[2][0], b[2][1], t) for b, t, _ in res or []], every_gap=True)

MIN_CHARS = 40


def parse_text(text, page):
    rows = [[c.strip() for c in ln.split(" | ")] for ln in text.splitlines() if ln.strip()]
    ident = mp.identity([rows[0][0]] if rows else [], [("", rows)])
    # skip title / key-value lines above the first column-header row (they are not table rows)
    start = next((i for i, r in enumerate(rows) if sum(1 for c in r if mp.case_of(c)) >= 2), 0)
    return ident, mp.parse_table(rows[start:], "", page)


class TextLayerExtractor:
    name = "text"

    def extract(self, pages, plan, fname):
        parsed, ocr_pages = [], set()
        for n, (img, t) in enumerate(pages, 1):
            if len(t.strip()) < MIN_CHARS:
                t = ocr_text(img) or ""
                ocr_pages.add(n)
            parsed.append(parse_text(t, n) if len(t.strip()) >= MIN_CHARS else ({}, None))
        records = normalize(mp.build_records([p[0] for p in parsed], [p[1] or [] for p in parsed]))
        issues = []
        for n, (_, ents) in enumerate(parsed, 1):
            for v in records:
                if n not in v["source_pages"]:
                    continue
                if ents is None:
                    issues.append(issue(fname, v["_name"], "", "", "", "error", "no_text_layer",
                                        f"Page {n} has no text layer and OCR is unavailable "
                                        "(pip install rapidocr-onnxruntime)"))
                elif n in ocr_pages:
                    issues.append(issue(fname, v["_name"], "", "", "", "warning", "ocr_page",
                                        f"Page {n} was read by OCR (scan): verify every value"))
        return records, issues
