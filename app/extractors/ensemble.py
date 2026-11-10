"""Two different engines, one result (claude.md section 3: "extend to N engines in Phase 3").

DeepSeek-OCR-2 is the primary reader, a second engine (Claude vision by default) is the witness.
Cells where the engines disagree are settled the same way as inside one engine: the candidate that
appears in the evidence (PDF text layer, or OCR of page bands/table crops for scans) wins; if the
evidence cannot decide, the cell stays an `unresolved` error. The witness never overwrites a value
on its own.
"""
from .deepseek_ocr import DeepSeekExtractor
from ..consistency import find_disputes


class EnsembleExtractor:
    name = "ensemble"

    def __init__(self, primary=None, secondary=None):
        self.primary = primary or DeepSeekExtractor()
        if secondary is None:
            from .claude_vision import ClaudeVisionExtractor
            secondary = ClaudeVisionExtractor()
        self.secondary = secondary

    def extract(self, pages, plan, fname):
        records, issues = self.primary.extract(pages, plan, fname)
        witness, _ = self.secondary.extract(pages, plan, fname)  # its own issues describe its own reads
        disputes = find_disputes(records, witness, fname, issues)
        self.primary._settle(disputes, pages, {}, fname, issues)
        return records, issues
