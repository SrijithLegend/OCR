"""Extractor registry. An extractor turns page images (+ text layer) into raw sheet dicts.

    extract(pages, plan, fname) -> (records, issues)

`pages` is a list of (PIL.Image, text_layer_str). `records` use the dict shape in
`claude_vision.RECORD_TOOL`. `issues` are reconciliation problems found while reading.
"""


def get_extractor(name: str):
    if name == "stub":
        from .stub import StubExtractor
        return StubExtractor()
    if name == "gemini":
        from .gemini_vision import GeminiVisionExtractor
        return GeminiVisionExtractor()
    if name == "claude":
        from .claude_vision import ClaudeVisionExtractor
        return ClaudeVisionExtractor()
    if name in ("deepseek", "ensemble"):
        from ..config import CLAUDE_MODEL, LABEL_MAP
        from .deepseek_ocr import DeepSeekExtractor
        from .labelmap import claude_proposer
        ds = DeepSeekExtractor(label_proposer=claude_proposer(CLAUDE_MODEL) if LABEL_MAP else None)
        if name == "deepseek":
            return ds
        from .ensemble import EnsembleExtractor
        return EnsembleExtractor(primary=ds)
    raise ValueError(f"Unknown EXTRACTOR {name!r} (use 'deepseek', 'ensemble', 'claude', 'gemini' or 'stub')")
