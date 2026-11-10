"""Ensemble, label mapper and table-crop fallback. Everything faked: no GPU, no network."""
from PIL import Image

from app.controller import Plan
from app.extractors import mdparse as mp
from app.extractors.deepseek_ocr import DeepSeekExtractor
from app.extractors.ensemble import EnsembleExtractor
from app.extractors.labelmap import apply_label_map
from tests.test_deepseek import HTML, codes, layer

TEXT = layer("20.5 18 15 10.5 9 8 10 7 12000 10000 5000 80 60 30")
PAGES = [(Image.new("RGB", (60, 90), "white"), TEXT)]
PLAN = Plan(passes=1, bands=3, cols=1)


class Witness:  # second engine returning fixed records
    def __init__(self, md):
        self.md = md

    def extract(self, pages, plan, fname):
        pg = mp.parse_page(self.md, 1)
        from app.consistency import normalize
        return normalize(mp.build_records([pg], [pg["entries"]])), []


def run(primary_md, witness_md):
    ex = EnsembleExtractor(primary=DeepSeekExtractor(read=lambda i, c: primary_md), secondary=Witness(witness_md))
    return ex.extract(PAGES, PLAN, "f.pdf")


def test_ensemble_agreement_is_clean():
    v, iss = run(HTML, HTML)
    assert not {"pass_disagree", "unresolved"} & codes(iss)


def test_ensemble_witness_right_primary_wrong_is_fixed_by_evidence():
    bad = HTML.replace("<td>12000</td>", "<td>77777</td>")
    v, iss = run(bad, HTML)
    got = next(e["value"] for e in v[0]["entries"] if e["canonical"] == "flow_rate_mass" and e["case"] == "maximum")
    assert got == "12000" and "pass_disagree" in codes(iss)


def test_ensemble_undecidable_stays_error():
    bad = HTML.replace("<td>12000</td>", "<td>77777</td>")
    wit = HTML.replace("<td>12000</td>", "<td>88888</td>")  # neither is in the text layer
    _, iss = run(bad, wit)
    assert "unresolved" in codes(iss)


def test_label_mapper_retags_label_only_and_ignores_junk():
    md = HTML.replace("Inlet Pressure (P1)", "Upstream Press.")
    pg = mp.parse_page(md, 1)
    valves = mp.build_records([pg], [pg["entries"]])
    from app.consistency import normalize
    normalize(valves)
    before = [(e["value"], e["unit"]) for e in valves[0]["entries"]]
    for e in valves[0]["entries"]:  # force the unknown label
        if e["parameter"] == "Upstream Press.":
            e["canonical"] = "other"
    issues = []
    apply_label_map(valves, lambda items: {i: "inlet_pressure" for i, *_ in items}, "f", issues)
    assert [(e["value"], e["unit"]) for e in valves[0]["entries"]] == before  # values untouched
    assert any(e["canonical"] == "inlet_pressure" for e in valves[0]["entries"] if e["parameter"] == "Upstream Press.")
    junk = []
    apply_label_map(valves, lambda items: {i: "made_up_name" for i, *_ in items}, "f", junk)
    assert junk == []


def test_table_crop_failure_falls_back_to_bands():
    import app.extractors.deepseek_ocr as d
    import app.extractors.tablecrop as t
    orig = t.detect_tables
    t.detect_tables = lambda img: (_ for _ in ()).throw(RuntimeError("no model"))
    try:
        crops = d.DeepSeekExtractor._table_crops(Image.new("RGB", (100, 300)))
        assert len(crops) == d.BANDS
    finally:
        t.detect_tables = orig


def test_scan_evidence_comes_from_the_splitter():
    scan = [(Image.new("RGB", (60, 90), "white"), "")]
    bad = HTML.replace("<td>12000</td>", "<td>77777</td>")
    reads = {"n": 0}

    def read(img, crop):
        reads["n"] += 1
        return HTML if reads["n"] == 2 else bad if reads["n"] == 1 else "12000 20.5 18 15 10.5 9 8 10 7 10000 5000 80 60 30"

    ex = DeepSeekExtractor(read=read, splitter=lambda img: [img])  # 1 evidence crop
    v, iss = ex.extract(scan, Plan(passes=2, bands=3, cols=1), "f.pdf")
    got = next(e["value"] for e in v[0]["entries"] if e["canonical"] == "flow_rate_mass" and e["case"] == "maximum")
    assert got == "12000"
