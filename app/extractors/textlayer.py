"""No-AI extractor: reads the PDF text layer (real characters, so no misread digits) and parses it with
the same header-mapping rules as mdparse. Instant, deterministic, needs no GPU / API key.
Pages without a text layer (scans) are not read; they get an error note on the sheet."""
from ..consistency import issue, normalize
from . import mdparse as mp

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
        parsed = [parse_text(t, n) if len(t.strip()) >= MIN_CHARS else ({}, None)
                  for n, (_, t) in enumerate(pages, 1)]
        records = normalize(mp.build_records([p[0] for p in parsed], [p[1] or [] for p in parsed]))
        issues = []
        for n, (_, ents) in enumerate(parsed, 1):
            if ents is None:
                for v in records:
                    if n in v["source_pages"]:
                        issues.append(issue(fname, v["_name"], "", "", "", "error", "no_text_layer",
                                            f"Page {n} has no text layer (scan?); not read by EXTRACTOR=text"))
        return records, issues
