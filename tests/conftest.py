import io
import os
import sys
import tempfile
from pathlib import Path

os.environ["EXTRACTOR"] = "stub"
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="amdp-test-")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pymupdf
import pytest
from fastapi.testclient import TestClient
from PIL import Image


@pytest.fixture(scope="session")
def client():
    from app.main import app
    return TestClient(app)


def make_pdf(pages=2) -> bytes:
    doc = pymupdf.open()
    for i in range(pages):
        p = doc.new_page()
        p.insert_text((72, 100), f"Process data sheet page {i + 1}", fontsize=14)
    return doc.tobytes()


def make_png(size=(600, 800)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, "white").save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture
def pdf_bytes():
    return make_pdf()


@pytest.fixture
def png_bytes():
    return make_png()
