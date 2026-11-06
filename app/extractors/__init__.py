"""Extractor registry. An extractor turns page images (+ text layer) into raw sheet dicts.

    extract(pages, plan, fname) -> (valves, issues)

`pages` is a list of (PIL.Image, text_layer_str). `valves` use the dict shape in
`claude_vision.RECORD_TOOL`. `issues` are reconciliation problems found while reading.
"""


def get_extractor(name: str):
    if name == "stub":
        from .stub import StubExtractor
        return StubExtractor()
    if name == "claude":
        from .claude_vision import ClaudeVisionExtractor
        return ClaudeVisionExtractor()
    if name == "deepseek":
        from .deepseek_ocr import DeepSeekExtractor
        return DeepSeekExtractor()
    raise ValueError(f"Unknown EXTRACTOR {name!r} (use 'deepseek', 'claude' or 'stub')")
