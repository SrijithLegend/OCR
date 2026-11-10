import pymupdf
import pytest
from PIL import Image

from app.consistency import validate
from app.controller import plan_for
from app.extractors.textlayer import TextLayerExtractor
from app.ingest import layout_text

ROWS = [  # note Max/Normal/Min order: columns must follow the printed header
    (40, ["PROCESS DATA"]),
    (60, ["No", "Parameter", "Unit", "Max", "Normal", "Min"]),
    (80, ["1", "Inlet Pressure (P1)", "bar(g)", "56.7", "56.7", "55.0"]),
    (100, ["2", "Outlet Pressure (P2)", "bar(g)", "52.7", "52.9", "40.0"]),
    (120, ["3", "Pressure Drop (dP)", "bar", "4.0", "3.8", "15.0"]),
    (140, ["4", "Flow Rate Mass", "kg/h", "45300.0", "41000.0", "20000.0"]),
]
X = [30, 60, 230, 300, 370, 440]


def pdf_page():
    doc = pymupdf.open()
    p = doc.new_page()
    p.insert_text((30, 20), "Tag: FV-101", fontsize=9)
    for y, cells in ROWS:
        for x, c in zip(X, cells):
            p.insert_text((x, y + 20), c, fontsize=9)
    return p


def test_text_layer_extraction():
    text = layout_text(pdf_page())
    pages = [(Image.new("RGB", (10, 10)), text)]
    records, issues = TextLayerExtractor().extract(pages, plan_for(pages, 1), "f.pdf")
    assert [v["tag"] for v in records] == ["FV-101"]
    got = {(e["canonical"], e["case"]): (e["value"], e["unit"]) for e in records[0]["entries"]}
    assert got[("inlet_pressure", "maximum")] == ("56.7", "bar(g)")
    assert got[("inlet_pressure", "minimum")] == ("55.0", "bar(g)")      # header order, not position
    assert got[("flow_rate_mass", "normal")] == ("41000.0", "kg/h")
    assert not issues
    assert "p_balance" not in {i["code"] for i in validate(records, "f.pdf")}  # all three columns balance


def test_scan_page_without_ocr_gets_error_note(monkeypatch):
    monkeypatch.setattr("app.extractors.textlayer.ocr_text", lambda img: None)
    pages = [(Image.new("RGB", (10, 10)), "")]
    records, issues = TextLayerExtractor().extract(pages, plan_for(pages, 1), "s.pdf")
    assert issues and issues[0]["code"] == "no_text_layer" and issues[0]["severity"] == "error"


def test_scan_page_read_by_ocr():
    from app.ingest import _render
    pytest.importorskip("rapidocr_onnxruntime")
    pages = [(_render(pdf_page(), 200), "")]            # image only: no text layer
    records, issues = TextLayerExtractor().extract(pages, plan_for(pages, 1), "s.pdf")
    got = {(e["canonical"], e["case"]): e["value"] for e in records[0]["entries"]}
    assert got[("inlet_pressure", "minimum")] == "55.0" and got[("flow_rate_mass", "maximum")] == "45300.0"
    assert [i["code"] for i in issues] == ["ocr_page"]


def test_auto_routes_by_text_layer(monkeypatch):
    from app.extractors import textlayer as tl
    calls = []
    monkeypatch.setattr(tl.TextLayerExtractor, "extract", lambda self, *a: calls.append("text") or ([], []))
    import app.extractors.gemini_vision as gv
    monkeypatch.setattr(gv.GeminiVisionExtractor, "__init__", lambda self: None)
    monkeypatch.setattr(gv.GeminiVisionExtractor, "extract", lambda self, *a: calls.append("gemini") or ([], []))
    img = Image.new("RGB", (10, 10))
    tl.AutoExtractor().extract([(img, "x" * 100)], None, "f")
    tl.AutoExtractor().extract([(img, "x" * 100), (img, "")], None, "f")
    assert calls == ["text", "gemini"]
