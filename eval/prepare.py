"""Step 0/1 helper.  python eval/prepare.py [--tender TENDER.pdf]
 - optional: extract pages 15-20 of the public tender PDF into eval/sheets/tender_pNN.pdf
 - seeded 30/70 dev/test split of eval/sheets/* -> eval/split.json (re-run only when sheets change: re-splitting after tuning breaks rule 4)
 - results/run_meta.json: model, dpi, bands, cols, passes, date, git commit
"""
import argparse
import json
import os
import random
import subprocess
import time
from pathlib import Path

import pymupdf as fitz

ROOT = Path(__file__).resolve().parent.parent
SHEETS = ROOT / "eval/sheets"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tender", type=Path)
    a = ap.parse_args()
    SHEETS.mkdir(parents=True, exist_ok=True)
    if a.tender:
        src = fitz.open(a.tender)
        for p in range(15, 21):
            out = fitz.open()
            out.insert_pdf(src, from_page=p - 1, to_page=p - 1)
            out.save(SHEETS / f"tender_p{p}.pdf")
    files = sorted(p.name for p in SHEETS.iterdir() if p.suffix.lower() in (".pdf", ".png", ".jpg", ".jpeg"))
    random.seed(0)
    random.shuffle(files)
    k = round(0.3 * len(files))
    split = {"seed": 0, "dev": sorted(files[:k]), "test": sorted(files[k:])}
    (ROOT / "eval/split.json").write_text(json.dumps(split, indent=1))
    git = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    (ROOT / "results").mkdir(exist_ok=True)
    (ROOT / "results/run_meta.json").write_text(json.dumps({
        "model": os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite"), "dpi": 300, "bands": 3, "cols": 1,
        "passes": [1, 2], "date": time.strftime("%Y-%m-%d"), "git_commit": git,
        "n_dev": len(split["dev"]), "n_test": len(split["test"])}, indent=1))
    print(f"{len(files)} sheets: {len(split['dev'])} dev / {len(split['test'])} test")


if __name__ == "__main__":
    main()
