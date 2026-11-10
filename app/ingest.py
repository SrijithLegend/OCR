"""Turn an uploaded PDF or image into (a) page PNGs for the UI and (b) model-resolution pages.

The original upload is never modified. Everything downstream refers to page numbers (1-based).
"""
import math
from pathlib import Path

from PIL import Image, ImageOps, ImageSequence

try:
    import pymupdf as fitz  # current name
except ImportError:  # older PyMuPDF
    import fitz


def layout_text(page) -> str:
    """Rebuild text rows from word boxes; ' | ' marks a column gap. Empty for scanned pages."""
    return words_to_text(page.get_text("words"))


def words_to_text(words, every_gap=False) -> str:
    """words: (x0, y0, x1, y1, text). every_gap=True: each box is a cell (OCR boxes are cell-level)."""
    if not words:
        return ""
    words = sorted(words, key=lambda w: ((w[1] + w[3]) / 2, w[0]))
    rows = []
    for w in words:
        yc, h = (w[1] + w[3]) / 2, max(w[3] - w[1], 1.0)
        if rows and abs(yc - rows[-1]["yc"]) <= 0.5 * max(h, rows[-1]["h"]):
            r = rows[-1]
            r["w"].append(w)
            r["yc"] = (r["yc"] * (len(r["w"]) - 1) + yc) / len(r["w"])
            r["h"] = max(r["h"], h)
        else:
            rows.append({"yc": yc, "h": h, "w": [w]})
    lines = []
    for r in rows:
        ws = sorted(r["w"], key=lambda w: w[0])
        parts = [ws[0][4]]
        for a, b in zip(ws, ws[1:]):
            parts.append(" | " if every_gap or b[0] - a[2] > 1.2 * r["h"] else " ")
            parts.append(b[4])
        lines.append("".join(parts))
    return "\n".join(lines)


def _render(page, dpi) -> Image.Image:
    r = page.rect
    scale = dpi / 72
    if r.width * r.height * scale * scale > 40e6:  # cap at ~40 megapixels
        scale = math.sqrt(40e6 / (r.width * r.height))
    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), colorspace=fitz.csRGB, alpha=False)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


def _open_pdf(path: Path, max_pages: int):
    doc = fitz.open(path)
    if doc.needs_pass:
        raise ValueError("The PDF is password-protected.")
    if doc.page_count > max_pages:
        raise ValueError(f"{doc.page_count} pages exceeds the limit of {max_pages} per file.")
    return doc


def _image_pages(path: Path, max_pages: int):
    img = Image.open(path)
    frames = [ImageOps.exif_transpose(f.copy()).convert("RGB") for f in ImageSequence.Iterator(img)]
    if len(frames) > max_pages:
        raise ValueError(f"{len(frames)} pages exceeds the limit of {max_pages} per file.")
    return frames


def load_model_pages(path: Path, dpi: int, max_pages: int):
    """-> list of (PIL image at model resolution, text layer or '')"""
    if path.suffix.lower() == ".pdf":
        doc = _open_pdf(path, max_pages)
        return [(_render(p, dpi), layout_text(p)) for p in doc]
    return [(f, "") for f in _image_pages(path, max_pages)]


def prepare_views(path: Path, file_idx: int, pages_dir: Path, view_dpi: int, max_pages: int) -> int:
    """Write view PNGs `{file_idx}_{page}.png` for the UI. Returns the page count."""
    pages_dir.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() == ".pdf":
        doc = _open_pdf(path, max_pages)
        for n, p in enumerate(doc, 1):
            _render(p, view_dpi).save(pages_dir / f"{file_idx}_{n}.png")
        return doc.page_count
    frames = _image_pages(path, max_pages)
    for n, f in enumerate(frames, 1):
        f = f.copy()
        f.thumbnail((1800, 1800))
        f.save(pages_dir / f"{file_idx}_{n}.png")
    return len(frames)
