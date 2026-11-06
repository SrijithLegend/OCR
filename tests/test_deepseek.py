"""Parser + DeepSeek extractor logic. No GPU: the OCR reader is faked."""
from PIL import Image

from app.consistency import validate
from app.controller import Plan
from app.extractors import mdparse as mp
from app.extractors.deepseek_ocr import DeepSeekExtractor

HTML = """# Process Data Sheet
Tag No.: FV-101
<table><tr><td colspan="6">PROCESS DATA</td></tr>
<tr><td>No</td><td>Parameter</td><td>Unit</td><td>Max</td><td>Norm</td><td>Min</td></tr>
<tr><td>1</td><td>Fluid</td><td></td><td>Water</td><td>Water</td><td>Water</td></tr>
<tr><td>2</td><td>Flow Rate Mass</td><td>kg/h</td><td>12000</td><td>10000</td><td>5000</td></tr>
<tr><td>3</td><td>Inlet Pressure (P1)</td><td>bar(g)</td><td>20.5</td><td>18</td><td>15</td></tr>
<tr><td>4</td><td>Outlet Pressure (P2)</td><td>bar(g)</td><td>10.5</td><td>9</td><td>8</td></tr>
<tr><td>5</td><td>Pressure Drop</td><td>bar</td><td>10</td><td>9</td><td>7</td></tr>
<tr><td>6</td><td>Cv required</td><td></td><td>80</td><td>60</td><td>30</td></tr>
<tr><td colspan="6">BODY</td></tr>
<tr><td>7</td><td>Inlet Pressure rating</td><td>bar</td><td>99</td><td>99</td><td>99</td></tr></table>"""

# vendor with Min / Normal / Max order, unit after the values, pipe table, unit inside the label
PIPE = """Data Sheet
Item: PSV-7
| Parameter | Minimum | Normal | Maximum | Units |
|---|---|---|---|---|
| Flow Rate Mass kg/h | 1,000 | 2,000 | 3,000 | kg/h |
| Inlet Temperature | 40 | 50 | 60 | °C |
| Inlet Pressure | 5 | 6 | 7 | barg |
| Specific Gravity | 0.9 | 0.9 | 0.9 | - |"""


def by(ents, canon):
    return {e["case"]: e["value"] for e in ents if e["canonical"] == canon}


def test_html_table_maps_columns_by_header_text():
    pg = mp.parse_page(HTML, 1)
    assert pg["tag"] == "FV-101" and pg["sheet_title"] == "Process Data Sheet"
    e = pg["entries"]
    assert by(e, "inlet_pressure") == {"maximum": "20.5", "normal": "18", "minimum": "15"}
    assert by(e, "flow_rate_mass") == {"maximum": "12000", "normal": "10000", "minimum": "5000"}
    assert {x["unit"] for x in e if x["canonical"] == "inlet_pressure"} == {"bar(g)"}
    assert all(x["case_label"] for x in e)


def test_out_of_scope_section_and_calculated_kind():
    e = mp.parse_page(HTML, 1)["entries"]
    assert not any("rating" in x["parameter"] for x in e)  # rows under BODY are skipped
    cv = [x for x in e if x["parameter"] == "Cv required"]
    assert cv and all(x["kind"] == "calculated" for x in cv)


def test_pipe_table_other_column_order_and_trailing_unit():
    pg = mp.parse_page(PIPE, 1)
    e = pg["entries"]
    assert pg["tag"] == "PSV-7"
    assert by(e, "flow_rate_mass") == {"minimum": "1,000", "normal": "2,000", "maximum": "3,000"}
    assert by(e, "inlet_temperature")["maximum"] == "60"
    assert {x["unit"] for x in e if x["canonical"] == "flow_rate_mass"} == {"kg/h"}


def test_values_copied_exactly_and_placeholders_dropped():
    md = HTML.replace("<td>20.5</td>", "<td>xxxx</td>").replace("<td>18</td>", "<td>1.8E+01</td>")
    d = by(mp.parse_page(md, 1)["entries"], "inlet_pressure")
    assert "maximum" not in d and d["normal"] == "1.8E+01"


def test_non_process_table_ignored_and_blank_template():
    md = "Tag: X\n| Part | Material |\n|---|---|\n| Body | WCB |\n| Trim | 316 |"
    assert mp.parse_page(md, 1)["entries"] == []
    assert mp.parse_page("", 1)["entries"] == []


def test_grounding_tags_stripped():
    md = "<|ref|>title<|/ref|><|det|>[[1,2,3,4]]<|/det|>\n# Sheet\n" + HTML
    assert mp.parse_page(md, 1)["entries"]


def test_pages_without_tag_continue_previous_sheet():
    a, b = mp.parse_page(HTML, 1), mp.parse_page(PIPE.replace("Item: PSV-7", ""), 2)
    v = mp.build_records([a, b], [a["entries"], b["entries"]])
    assert len(v) == 1 and v[0]["source_pages"] == [1, 2]
    c = mp.parse_page(PIPE, 3)
    assert len(mp.build_records([a, c], [a["entries"], c["entries"]])) == 2


# ------------------------------------------------------------------ extractor logic
def layer(text):  # a text layer long enough to count as evidence
    return text + " " + " ".join(["filler"] * 20)


def run(reads, text, passes=2):
    seq = iter(reads)
    ex = DeepSeekExtractor(read=lambda img, crop: next(seq))
    pages = [(Image.new("RGB", (60, 90), "white"), text)]
    records, issues = ex.extract(pages, Plan(passes=passes, bands=3, cols=1), "f.pdf")
    return records, issues + validate(records, "f.pdf")


def val(records, canon, case):
    return next(e["value"] for e in records[0]["entries"] if e["canonical"] == canon and e["case"] == case)


def codes(issues):
    return {i["code"] for i in issues}


def test_agreeing_reads_with_evidence_are_clean():
    text = layer("20.5 18 15 10.5 9 8 10 7 12000 10000 5000 80 60 30")
    v, iss = run([HTML, HTML], text)
    assert val(v, "inlet_pressure", "maximum") == "20.5"
    assert not {"pass_disagree", "unresolved", "not_in_evidence", "evidence_fix"} & codes(iss)


def test_disagreement_settled_by_text_layer():
    bad = HTML.replace("<td>20.5</td>", "<td>20.6</td>")
    text = layer("20.5 18 15 10.5 9 8 10 7 12000 10000 5000 80 60 30")
    v, iss = run([bad, HTML], text)  # read 1 wrong, read 2 right
    assert val(v, "inlet_pressure", "maximum") == "20.5"
    assert "pass_disagree" in codes(iss) and "unresolved" not in codes(iss)


def test_disagreement_without_evidence_stays_an_error():
    bad = HTML.replace("<td>20.5</td>", "<td>20.6</td>")
    scan_reads = [bad, HTML, "", "", ""]  # 2 page reads, then 3 empty band reads: no evidence at all
    v, iss = run(scan_reads, "")
    assert "unresolved" in codes(iss)
    assert any(i["severity"] == "error" and i["code"] == "unresolved" for i in iss)


def test_single_char_misread_corrected_from_text_layer_and_flagged():
    wrong = HTML.replace("<td>12000</td>", "<td>12008</td>")
    text = layer("20.5 18 15 10.5 9 8 10 7 12000 10000 5000 80 60 30")
    v, iss = run([wrong, wrong], text)  # both reads wrong the same way
    assert val(v, "flow_rate_mass", "maximum") == "12000"
    assert "evidence_fix" in codes(iss)


def test_value_missing_from_evidence_is_flagged_not_changed():
    text = layer("20.5 18 15 10.5 9 8 10 7 10000 5000 80 60 30")  # 12000 absent, no 1-off neighbour
    v, iss = run([HTML, HTML], text)
    assert val(v, "flow_rate_mass", "maximum") == "12000"
    assert "not_in_evidence" in codes(iss)


def test_p_balance_flags_misread_pressure():
    bad = HTML.replace("<td>10.5</td>", "<td>16.5</td>")  # P1 20.5 - dP 10 != P2 16.5
    text = layer("20.5 18 15 16.5 9 8 10 7 12000 10000 5000 80 60 30")
    _, iss = run([bad, bad], text)
    assert "p_balance" in codes(iss)
