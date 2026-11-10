import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "data")).resolve()
EXTRACTOR = os.environ.get("EXTRACTOR", "deepseek")  # deepseek | ensemble | claude | gemini | stub
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")  # free tier
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")
LABEL_MAP = os.environ.get("LABEL_MAP", "0") == "1"  # LLM re-tags unknown row labels (needs ANTHROPIC_API_KEY)
PASSES = max(1, min(2, int(os.environ.get("PASSES", "2"))))

MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "50"))
MAX_FILES = int(os.environ.get("MAX_FILES", "20"))
MAX_PAGES = int(os.environ.get("MAX_PAGES", "40"))  # per file; bounds cost and request size
MODEL_DPI = int(os.environ.get("MODEL_DPI", "300"))  # resolution sent to the model
VIEW_DPI = int(os.environ.get("VIEW_DPI", "110"))    # resolution of the page images in the UI

ALLOWED_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff"}
