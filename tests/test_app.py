import io
import time

import openpyxl
import pytest

from app.consistency import apply_adjudication, find_disputes, normalize, to_sheets, validate
from app.controller import MAX_IMAGES_PER_REQUEST, plan_for
from app.textutil import same_value, to_num, unit_norm


def upload(client, files):
    r = client.post("/api/jobs", files=[("files", f) for f in files])
    return r


def wait_done(client, job_id, timeout=20):
    t = time.time()
    while time.time() - t < timeout:
        j = client.get(f"/api/jobs/{job_id}").json()
        if j["status"] in ("done", "error"):
            return j
        time.sleep(0.1)
    raise AssertionError("job did not finish")


@pytest.fixture
def done_job(client, pdf_bytes, png_bytes):
    r = upload(client, [("a.pdf", pdf_bytes, "application/pdf"), ("b.png", png_bytes, "image/png")])
    assert r.status_code == 202, r.text
    j = wait_done(client, r.json()["id"])
    assert j["status"] == "done", j
    return j


def find(job, parameter, case, tag="DEMO-101"):
    for s in job["sheets"]:
        if s["tag"] == tag:
            for c in s["cells"]:
                if c["parameter"] == parameter and c["case"] == case:
                    return s, c
    raise AssertionError((parameter, case))


# ------------------------------------------------------------------ upload
def test_upload_pipeline_done(done_job):
    assert [f["name"] for f in done_job["files"]] == ["a.pdf", "b.png"]
    assert [f["pages"] for f in done_job["files"]] == [2, 1]
    assert len(done_job["sheets"]) == 4  # stub returns 2 sheets per file


def test_flags_attached_to_cells(done_job):
    _, c = find(done_job, "Flow Rate Mass", "minimum")
    assert any(f["code"] == "unreadable" and f["severity"] == "error" for f in c["flags"])
    _, c = find(done_job, "Outlet Pressure (P2)", "minimum")
    assert any(f["code"] == "p_balance" for f in c["flags"])  # 56.7 - 6.7 != 40.0 -> flagged
    _, c = find(done_job, "Flow Rate Mass", "maximum")
    assert any(f["code"] == "pass_disagree" for f in c["flags"])


@pytest.mark.parametrize("name,data,ctype,code", [
    ("x.exe", b"MZ....", "application/octet-stream", 415),
    ("x.pdf", b"not a pdf at all", "application/pdf", 415),       # spoofed extension
    ("x.png", b"%PDF-1.4 ...", "image/png", 415),                  # wrong family
    ("x.pdf", b"", "application/pdf", 422),
])
def test_upload_rejections(client, name, data, ctype, code):
    r = upload(client, [(name, data, ctype)])
    assert r.status_code == code, r.text


def test_rejected_upload_leaves_no_job(client):
    from app.store import jobs_dir
    before = set(p.name for p in jobs_dir().iterdir())
    upload(client, [("x.pdf", b"nope", "application/pdf")])
    assert set(p.name for p in jobs_dir().iterdir()) == before


def test_filename_never_used_as_path(client, png_bytes):
    r = upload(client, [("../../evil.png", png_bytes, "image/png")])
    assert r.status_code == 202
    j = wait_done(client, r.json()["id"])
    assert j["files"][0]["name"] == "evil.png"


def test_password_pdf_gives_error_not_crash(client):
    import pymupdf
    doc = pymupdf.open()
    doc.new_page()
    data = doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="o", user_pw="u")
    r = upload(client, [("locked.pdf", data, "application/pdf")])
    j = wait_done(client, r.json()["id"])
    assert j["status"] == "error" and "password" in j["error"].lower()


def test_invalid_job_id(client):
    for bad in ("nope", "../etc", "0" * 31, "g" * 32):
        assert client.get(f"/api/jobs/{bad}").status_code == 404
    assert client.get("/api/jobs/" + "0" * 32).status_code == 404


# ----------------------------------------------------------------- editing
def test_patch_marks_edited_and_keeps_original(client, done_job):
    s, c = find(done_job, "Inlet Pressure (P1)", "normal")
    r = client.patch(f"/api/jobs/{done_job['id']}/cells/{c['id']}", json={"value": "57.1"})
    assert r.status_code == 200
    n = r.json()
    assert n["value"] == "57.1" and n["edited"] and n["reviewed"] and n["original_value"] == "56.7"
    # a second edit keeps the ORIGINAL model value
    n = client.patch(f"/api/jobs/{done_job['id']}/cells/{c['id']}", json={"value": "57.2"}).json()
    assert n["original_value"] == "56.7"
    # persisted
    j = client.get(f"/api/jobs/{done_job['id']}").json()
    assert find(j, "Inlet Pressure (P1)", "normal")[1]["value"] == "57.2"


def test_review_without_change_is_not_an_edit(client, done_job):
    _, c = find(done_job, "Flow Rate Mass", "maximum")
    n = client.patch(f"/api/jobs/{done_job['id']}/cells/{c['id']}", json={"reviewed": True}).json()
    assert n["reviewed"] and not n["edited"]


def test_patch_validation(client, done_job):
    _, c = find(done_job, "Inlet Pressure (P1)", "normal")
    url = f"/api/jobs/{done_job['id']}/cells/{c['id']}"
    assert client.patch(url, json={"value": "x" * 301}).status_code == 422
    assert client.patch(url, json={"parameter": "  "}).status_code == 422
    assert client.patch(f"/api/jobs/{done_job['id']}/cells/deadbeef", json={"value": "1"}).status_code == 404


def test_add_and_delete_cell(client, done_job):
    s, _ = find(done_job, "Inlet Pressure (P1)", "normal")
    r = client.post(f"/api/jobs/{done_job['id']}/sheets/{s['id']}/cells",
                    json={"parameter": "Viscosity", "case": "normal", "value": "1.2", "unit": "cP"})
    assert r.status_code == 201
    cid = r.json()["id"]
    assert r.json()["source"] == "human" and r.json()["edited"]
    assert client.delete(f"/api/jobs/{done_job['id']}/cells/{cid}").status_code == 204
    j = client.get(f"/api/jobs/{done_job['id']}").json()
    assert all(c["id"] != cid for sh in j["sheets"] for c in sh["cells"])


# ------------------------------------------------------------------ export
def sheet_rows(wb, name):
    ws = wb[name]
    head = [c.value for c in ws[1]]
    return [dict(zip(head, [c.value for c in row])) for row in ws.iter_rows(min_row=2)]


def export(client, job_id):
    r = client.get(f"/api/jobs/{job_id}/export.xlsx")
    assert r.status_code == 200
    assert "attachment" in r.headers["content-disposition"]
    return openpyxl.load_workbook(io.BytesIO(r.content))


def test_export_reflects_edits(client, done_job):
    _, c = find(done_job, "Inlet Temperature", "normal")
    client.patch(f"/api/jobs/{done_job['id']}/cells/{c['id']}", json={"value": "99.9"})
    # the P1 row carries an unresolved p_balance error -> must still read ERROR in the export
    wb = export(client, done_job["id"])
    assert wb.sheetnames == ["Process Conditions", "Sheet Info", "Long Format", "Review & Edits"]
    rows = {r["Parameter"]: r for r in sheet_rows(wb, "Process Conditions") if r["Tag"] == "DEMO-101" and r["Source File"] == "a.pdf"}
    t = rows["Inlet Temperature"]
    assert t["Normal"] == 99.9 and t["Maximum"] == 150 and t["Status"] == "EDITED"  # numbers stay numeric
    assert rows["Inlet Pressure (P1)"]["Status"] == "ERROR"
    edits = [r for r in sheet_rows(wb, "Review & Edits") if r["Type"] == "Human edit"]
    assert any("150" in r["Detail"] and "99.9" in r["Detail"] for r in edits)


def test_export_blocks_formula_injection(client, done_job):
    _, c = find(done_job, "Inlet State", "normal")
    client.patch(f"/api/jobs/{done_job['id']}/cells/{c['id']}", json={"value": '=HYPERLINK("http://x","y")'})
    wb = export(client, done_job["id"])
    cells = [cell for ws in wb for row in ws.iter_rows() for cell in row if isinstance(cell.value, str) and cell.value.startswith("=")]
    assert cells, "expected the payload to be present as text"
    assert all(cell.data_type == "s" for cell in cells)


def test_export_unfinished_job_rejected(client):
    from app import store
    job = store.create()
    assert client.get(f"/api/jobs/{job.id}/export.xlsx").status_code == 409


# -------------------------------------------------------------- misc routes
def test_page_image_and_delete(client, done_job):
    jid = done_job["id"]
    r = client.get(f"/api/jobs/{jid}/pages/0/2.png")
    assert r.status_code == 200 and r.content[:4] == b"\x89PNG"
    assert client.get(f"/api/jobs/{jid}/pages/0/9.png").status_code == 404
    assert client.get(f"/api/jobs/{jid}/pages/../../x/1.png").status_code in (404, 422)
    assert client.delete(f"/api/jobs/{jid}").status_code == 204
    assert client.get(f"/api/jobs/{jid}").status_code == 404


def test_security_headers_and_index(client):
    r = client.get("/")
    assert r.status_code == 200 and "Process conditions extractor" in r.text
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"
    assert client.get("/static/app.js").status_code == 200


# --------------------------------------------------- consistency + controller
def E(par, canon, case, val, unit="bar", **kw):
    return dict(section="S", row_no=None, parameter=par, canonical=canon, kind="condition", case=case,
                case_label=case, value=val, unit=unit, page=1, **kw)


def V(entries, tag="T1"):
    return normalize([dict(tag=tag, source_pages=[1], has_values=True, entries=entries)])[0:1]


def codes(issues):
    return {i["code"] for i in issues}


def test_validate_p_balance_and_ok_case():
    bad = V([E("P1", "inlet_pressure", "normal", "10"), E("P2", "outlet_pressure", "normal", "5"),
             E("dP", "pressure_drop", "normal", "2")])
    assert "p_balance" in codes(validate(bad, "f"))
    ok = V([E("P1", "inlet_pressure", "normal", "10"), E("P2", "outlet_pressure", "normal", "8"),
            E("dP", "pressure_drop", "normal", "2")])
    assert "p_balance" not in codes(validate(ok, "f"))


def test_validate_p_balance_skipped_for_incompatible_units():
    v = V([E("P1", "inlet_pressure", "normal", "10", "bar"), E("P2", "outlet_pressure", "normal", "100", "psi"),
           E("dP", "pressure_drop", "normal", "2", "bar")])
    assert "p_balance" not in codes(validate(v, "f"))


def test_validate_order_units_unreadable():
    v = V([E("Q", "flow_rate_mass", "minimum", "50", "kg/h"), E("Q", "flow_rate_mass", "normal", "10", "kg/h"),
           E("Q", "flow_rate_mass", "maximum", "100", "kg/h"),
           E("T", "inlet_temperature", "normal", "80", "bar"),
           E("X", "viscosity", "normal", "UNREADABLE", "cP")])
    c = codes(validate(v, "f"))
    assert {"order", "bad_temp_unit", "unreadable"} <= c


def test_reconcile_majority_vote():
    a = normalize([dict(tag="T", source_pages=[1], has_values=True, entries=[E("P1", "inlet_pressure", "normal", "10.5")])])
    b = normalize([dict(tag="T", source_pages=[1], has_values=True, entries=[E("P1", "inlet_pressure", "normal", "10.6")])])
    issues = []
    d = find_disputes(a, b, "f", issues)
    assert len(d) == 1
    apply_adjudication(d, {0: {"value": "10.6", "unit": "bar"}}, "f", issues)
    assert a[0]["entries"][0]["value"] == "10.6" and "pass_disagree" in codes(issues)
    # re-read matches neither -> keep read 1 but mark unresolved error
    a = normalize([dict(tag="T", source_pages=[1], has_values=True, entries=[E("P1", "inlet_pressure", "normal", "10.5")])])
    issues = []
    d = find_disputes(a, b, "f", issues)
    apply_adjudication(d, {0: {"value": "10.9", "unit": "bar"}}, "f", issues)
    assert a[0]["entries"][0]["value"] == "10.5"
    assert [i for i in issues if i["code"] == "unresolved" and i["severity"] == "error"]


def test_to_sheets_unmatched_issue_becomes_sheet_note():
    v = V([E("P1", "inlet_pressure", "normal", "10")])
    issues = [dict(file="f", record="T1", section="", parameter="", case="", severity="error", code="x", issue="boom")]
    sh = to_sheets(0, "f", v, issues)[0]
    assert sh.notes and not sh.cells[0].flags


def test_textutil():
    assert to_num("1,500") == 1500 and to_num("1.4E+06") == 1.4e6 and to_num("<75") is None
    assert same_value("10.0", "10") and not same_value("10.5", "10.6")
    assert unit_norm("kg/cm²g") == unit_norm("KG/CM2G")


def test_controller_respects_image_limit():
    from PIL import Image
    img = Image.new("RGB", (10, 10))
    for n in (1, 10, 24, 40):
        p = plan_for([(img, "")] * n, 2)
        assert n * p.images_per_page() <= MAX_IMAGES_PER_REQUEST
    dense = plan_for([(img, "x" * 3000)], 2)
    assert dense.cols == 2
    with pytest.raises(ValueError):
        plan_for([(img, "")] * 101, 2)


def test_eval_csv_roundtrip(client, done_job):
    import csv as _csv
    jid = done_job["id"]
    _, c = find(done_job, "Inlet Temperature", "normal")
    client.patch(f"/api/jobs/{jid}/cells/{c['id']}", json={"value": "99.9"})
    out = list(_csv.DictReader(io.StringIO(client.get(f"/api/jobs/{jid}/model-output.csv").text)))
    r = [x for x in out if x["parameter"] == "Inlet Temperature" and x["case"] == "normal" and x["record"] == "DEMO-101"][0]
    assert r["value"] == "150"          # model value, not the human edit
    flagged = [x for x in out if x["parameter"] == "Flow Rate Mass" and x["case"] == "minimum"][0]
    assert "read this cell" in flagged["flag"]
    truth = list(_csv.DictReader(io.StringIO(client.get(f"/api/jobs/{jid}/truth.csv").text)))
    assert [x["value"] for x in truth if x["parameter"] == "Inlet Temperature" and x["case"] == "normal"] == ["99.9"]


def test_job_interrupted_by_restart_becomes_error(client):
    from app import store
    job = store.create()
    job.status = "processing"
    store.save(job)
    store._jobs.pop(job.id)               # simulate a fresh process reading from disk
    j = client.get(f"/api/jobs/{job.id}").json()
    assert j["status"] == "error" and "restart" in j["error"]
