"""DeepSeek-OCR-2 extractor (local GPU, 4-bit by default).

Per page: two independent OCR reads (tiled-crop view and global view) -> markdown -> mdparse.
Accuracy rules, all deterministic code:
* two reads must agree; a disagreement is settled only by EVIDENCE (the PDF text layer, or for scans
  a third OCR of overlapping page bands) - the candidate that appears in the evidence wins;
* if neither (or both) appear, the cell stays flagged `unresolved` (error) for a human, never guessed;
* every numeric value must appear in the evidence; a value that does not is corrected only when exactly
  one evidence number is one character away, and always flagged.
Requires an NVIDIA GPU, torch (CUDA), transformers==4.46.3, bitsandbytes (for 4-bit).
"""
import logging
import os
import tempfile
import threading
from pathlib import Path

from ..consistency import apply_adjudication, find_disputes, issue, normalize
from ..textutil import same_value, unit_norm
from . import mdparse as mp
from .labelmap import apply_label_map

log = logging.getLogger("amdp")

MODEL_NAME = os.environ.get("DEEPSEEK_MODEL", "deepseek-ai/DeepSeek-OCR-2")
REVISION = os.environ.get("DEEPSEEK_REVISION") or None   # pin a commit: the repo ships code we execute
QUANT_4BIT = os.environ.get("DEEPSEEK_4BIT", "1") != "0"  # 0 = bf16 (needs ~8 GB VRAM)
PROMPT = "<image>\n<|grounding|>Convert the document to markdown. "
TEXT_LAYER_MIN = 80
BANDS = 3  # overlapping horizontal bands used as OCR evidence for scans
TABLE_CROP = os.environ.get("TABLE_CROP", "0") == "1"  # Table Transformer crops instead of bands

_LOCK = threading.Lock()  # one GPU, one inference at a time
_MODEL = None


def _load():
    global _MODEL
    if _MODEL is None:
        try:
            import torch
            from transformers import AutoModel, AutoTokenizer, BitsAndBytesConfig
        except ImportError as e:
            raise RuntimeError(f"DeepSeek-OCR-2 needs torch + transformers==4.46.3 ({e}). "
                               "See requirements.txt, or set EXTRACTOR=stub.") from e
        if not torch.cuda.is_available():
            raise RuntimeError("DeepSeek-OCR-2 needs an NVIDIA GPU with a CUDA build of torch "
                               "(torch.cuda.is_available() is False).")
        kw = {"trust_remote_code": True, "revision": REVISION}
        tok = AutoTokenizer.from_pretrained(MODEL_NAME, **kw)
        extra = {}
        if QUANT_4BIT:  # LLM in 4-bit so the 3B model fits 6 GB; the vision tower stays bf16
            extra["quantization_config"] = BitsAndBytesConfig(
                load_in_4bit=True, bnb_4bit_quant_type="nf4", bnb_4bit_compute_dtype=torch.bfloat16,
                llm_int8_skip_modules=["sam_model", "qwen2_model", "projector", "lm_head"])
            extra["device_map"] = {"": 0}
        model = AutoModel.from_pretrained(MODEL_NAME, use_safetensors=True, torch_dtype=torch.bfloat16,
                                          attn_implementation="eager", **extra, **kw)
        if not QUANT_4BIT:
            model = model.cuda()
        _MODEL = (model.eval(), tok)
    return _MODEL


def ocr(img, crop_mode=True):
    """One OCR read of a PIL image -> markdown text."""
    import torch
    model, tok = _load()
    with _LOCK, tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "page.png"
        img.save(p)
        try:
            return model.infer(tok, prompt=PROMPT, image_file=str(p), output_path=tmp, base_size=1024,
                               image_size=768, crop_mode=crop_mode, eval_mode=True) or ""
        finally:
            torch.cuda.empty_cache()


def bands(img, n=BANDS, overlap=0.15):
    w, h = img.size
    size = h / (n - (n - 1) * overlap)
    step = size * (1 - overlap)
    return [img.crop((0, int(k * step), w, min(h, int(k * step + size)))) for k in range(n)]


class DeepSeekExtractor:
    name = "deepseek"

    def __init__(self, read=ocr, splitter=None, label_proposer=None):
        self.read = read  # injectable for tests
        self.splitter = splitter or (self._table_crops if TABLE_CROP else bands)
        self.label_proposer = label_proposer  # labelmap.claude_proposer(...) or None

    @staticmethod
    def _table_crops(img):
        from .tablecrop import detect_tables
        try:
            return detect_tables(img) or bands(img)
        except Exception as e:  # detector unavailable: fall back, never fail the job
            log.warning("table detection failed (%s); using bands", e)
            return bands(img)

    def evidence(self, img, text, cache, page):
        if page not in cache:
            if len(text) >= TEXT_LAYER_MIN:
                cache[page] = mp.numbers_in(text)
            else:
                cache[page] = mp.numbers_in(" ".join(self.read(b, True) for b in self.splitter(img)))
        return cache[page]

    def extract(self, pages, plan, fname):
        issues, cache = [], {}
        mds_a = [self.read(img, True) for img, _ in pages]
        mds_b = [self.read(img, False) for img, _ in pages] if plan.passes == 2 else None
        pa = [mp.parse_page(md, n) for n, md in enumerate(mds_a, 1)]
        pb = [mp.parse_page(md, n) for n, md in enumerate(mds_b, 1)] if mds_b else None
        idents = [self._merge_ident(a, pb[i] if pb else None, i + 1, fname, issues)
                  for i, a in enumerate(pa)]
        records = normalize(mp.build_records(idents, [p["entries"] for p in pa]))
        if pb:
            records_b = normalize(mp.build_records(idents, [p["entries"] for p in pb]))
            disputes = find_disputes(records, records_b, fname, issues)
            self._settle(disputes, pages, cache, fname, issues)
        self._provenance(records, pages, cache, fname, issues)
        return records, issues

    @staticmethod
    def _merge_ident(a, b, page, fname, issues):
        out = dict(a)
        if b:
            for k, v in b.items():
                if k != "entries" and not out.get(k):
                    out[k] = v
            ta, tb = (a.get("tag") or ""), (b.get("tag") or "")
            if ta and tb and ta.strip().lower() != tb.strip().lower():
                issues.append(issue(fname, ta, "", "", "", "warning", "tag_disagree",
                                    f"Page {page}: tag read as {ta!r} and {tb!r}; using {ta!r}"))
        return out

    def _settle(self, disputes, pages, cache, fname, issues):
        """Decide each disputed cell from evidence; otherwise leave it as an unresolved error."""
        answers = {}
        for d in disputes:
            ref = d["a"] or d["b"]
            img, text = pages[min(max(ref.get("page", 1), 1), len(pages)) - 1]
            ev = self.evidence(img, text, cache, ref.get("page", 1))
            pick = None
            a, b = d["a"], d["b"]
            ina = a is not None and a.get("value") is not None and mp.in_evidence(a["value"], ev)
            inb = b is not None and b.get("value") is not None and mp.in_evidence(b["value"], ev)
            if a and b:
                if ina and not inb:
                    pick = a
                elif inb and not ina:
                    pick = b
                elif ina and inb and unit_norm(a.get("unit")) != unit_norm(b.get("unit")) \
                        and same_value(a.get("value"), b.get("value")):
                    pick = a  # same number, unit spelling differs: keep read 1, apply_adjudication flags it
            if pick is not None:
                answers[d["id"]] = {"value": pick.get("value"), "unit": pick.get("unit")}
        apply_adjudication(disputes, answers, fname, issues)

    def _provenance(self, records, pages, cache, fname, issues):
        # numbers the OCR itself read per page: a neighbour already used by another cell is never a fix
        used = {}
        for v in records:
            for e in v["entries"]:
                c = mp._core(e.get("value"))
                if c is not None:
                    used.setdefault(e.get("page", 1), set()).add(c)
        for v in records:
            for e in v["entries"]:
                core = mp._core(e.get("value"))
                if core is None:
                    continue
                img, text = pages[min(max(e.get("page", 1), 1), len(pages)) - 1]
                if len(text) < TEXT_LAYER_MIN and e.get("page", 1) not in cache:
                    continue  # scan: bands OCR only runs for disputes, to bound GPU time
                ev = self.evidence(img, text, cache, e.get("page", 1))
                if core in ev:
                    continue
                near = [n for n in mp.near_numbers(core, ev) if n not in used.get(e.get("page", 1), ())]
                code, sev = "not_in_evidence", "warning"
                msg = f"Value {e['value']!r} not found in the page's text evidence; verify against the sheet"
                if len(near) == 1:
                    msg = f"OCR read {e['value']!r}; text evidence has {near[0]!r} one character away - corrected"
                    e["value"] = str(e["value"]).replace(core, near[0])
                    code = "evidence_fix"
                issues.append(issue(fname, v["_name"], e.get("section"), e["parameter"], e["case"],
                                    sev, code, msg))
