"""Cross-model consistency engine (AMDP section 10), deterministic parts only.

* reconcile two independent reads (find_disputes -> adjudicate -> apply_adjudication)
* engineering checks (validate): P1 - dP = P2, ordering, units, ranges, duplicates
* to_sheets: turn raw dicts + issues into Sheet/Cell models with per-cell flags

An issue is a dict: file, record, section, parameter, case, severity, code, issue.
"""
import copy
import re

from .constants import CASES, FLOW, KINDS, NON_NUMERIC, P_BASES, PRESSURE, T_UNITS, TEMP
from .schemas import Cell, Flag, Sheet
from .textutil import norm, same_value, to_num, unit_norm

P_UNIT = re.compile(r"(bar|psi|kpa|mpa|pa|kg/?cm|kgf|atm|torr|mmhg|mmwc|inh|ksi)")


def normalize(records):
    for i, v in enumerate(records, 1):
        tag = (v.get("tag") or "").strip()
        title = (v.get("sheet_title") or "").strip()
        pages = v.get("source_pages") or []
        v["_name"] = tag or (f"{title} (p{min(pages)})" if title and pages else f"(untitled sheet {i})")
        v["_id"] = norm(v["_name"])
        v["entries"] = v.get("entries") or []
    return records


def vkey(v):
    return v["_id"]


def issue(fname, record, section, parameter, case, severity, code, msg):
    return {"file": fname, "record": record, "section": section or "", "parameter": parameter or "",
            "case": case or "", "severity": severity, "code": code, "issue": msg}


# ----------------------------------------------------------- reconcile two reads
def keyed(v):
    out, seen = {}, {}
    for e in v["entries"]:
        k = (norm(e.get("section")), norm(e["parameter"]), e["case"])
        n = seen.get(k, 0)
        seen[k] = n + 1
        out[k + (n,)] = e
    return out


def find_disputes(a_records, b_records, fname, issues):
    disputes = []
    idx_b = {v["_id"]: v for v in b_records}
    for va in a_records:
        vb = idx_b.pop(va["_id"], None)
        if vb is None:
            issues.append(issue(fname, va["_name"], "", "", "", "error", "not_in_pass2",
                                "Sheet not found in the second read"))
            continue
        ka, kb = keyed(va), keyed(vb)
        for k in sorted(set(ka) | set(kb), key=str):
            a, b = ka.get(k), kb.get(k)
            if a and b and same_value(a.get("value"), b.get("value")) \
                    and unit_norm(a.get("unit")) == unit_norm(b.get("unit")):
                continue
            disputes.append({"id": len(disputes), "record": va, "a": a, "b": b})
    for vb in idx_b.values():
        issues.append(issue(fname, vb["_name"], "", "", "", "error", "only_pass2",
                            "Sheet only found in the second read"))
    return disputes


def dispute_questions(disputes):
    lines = []
    for d in disputes:
        ref = d["a"] or d["b"]
        lines.append(f'Q{d["id"]}: sheet "{d["record"]["_name"]}", page {ref.get("page")}, '
                     f'section "{ref.get("section") or "-"}", row {ref.get("row_no") or "-"} '
                     f'"{ref["parameter"]}", column "{ref.get("case_label") or ref["case"]}": '
                     f'value and unit as printed?')
    return lines


def apply_adjudication(disputes, answers, fname, issues):
    """Majority vote of read 1, read 2 and the re-read. An unmatched re-read stays an error."""
    for d in disputes:
        a, b, v = d["a"], d["b"], d["record"]
        ref = a or b
        sec, par, case, name = ref.get("section"), ref["parameter"], ref["case"], v["_name"]
        ans = answers.get(d["id"]) or {}
        av, bv = (a or {}).get("value"), (b or {}).get("value")
        ua, ub = (a or {}).get("unit"), (b or {}).get("unit")
        rv = ans.get("value")
        readable = rv is not None and "UNREADABLE" not in str(rv).upper()
        win = None
        if readable and a and av is not None and same_value(rv, av):
            win = "a"
        elif readable and b and bv is not None and same_value(rv, bv):
            win = "b"
        if win is None:
            if a is None and b is not None:
                v["entries"].append(copy.deepcopy(b))
            issues.append(issue(fname, name, sec, par, case, "error", "unresolved",
                                f"Unresolved: read 1 {av!r}, read 2 {bv!r}, re-read {rv!r}"))
            continue
        t = a if a else copy.deepcopy(b)
        if not a:
            v["entries"].append(t)
        if win == "b":
            t["value"], t["unit"] = bv, ub
        msg = f"Reads disagreed (1: {av!r}, 2: {bv!r}); re-read decided {t['value']!r}"
        if a and b and unit_norm(ua) != unit_norm(ub):
            t["unit"] = ub if unit_norm(ans.get("unit")) == unit_norm(ub) else ua
            msg += f"; unit differed ({ua!r} vs {ub!r}) -> {t['unit']!r}"
        issues.append(issue(fname, name, sec, par, case, "warning", "pass_disagree", msg))


# ------------------------------------------------------------------ validation
def _pressure_base(un):
    if un in P_BASES:
        return un
    if un[:-1] in P_BASES and un[-1:] in ("g", "a"):
        return un[:-1]
    return un


def validate(records, fname):
    issues = []

    def add(v, e, sev, code, msg):
        issues.append(issue(fname, v["_name"], e.get("section") if e else "",
                            e["parameter"] if e else "", e["case"] if e else "", sev, code, msg))

    seen_names = {}
    for v in records:
        ents = v["entries"]
        seen_names[v["_id"]] = seen_names.get(v["_id"], 0) + 1
        if not v.get("has_values") and not ents:
            add(v, None, "info", "no_values", "No filled process-condition values (blank template or no such block)")
        elif not v.get("has_values") and ents:
            add(v, None, "warning", "blank_with_entries", "Sheet marked blank but entries were returned")
        if v.get("has_values") and not (v.get("tag") or "").strip():
            add(v, None, "warning", "no_tag", "Tag missing")

        seen = set()
        for e in ents:
            key = (norm(e.get("section")), norm(e["parameter"]), e["case"])
            if key in seen:
                add(v, e, "warning", "duplicate_row", "Duplicate entry for the same row and column")
            seen.add(key)
            val = e.get("value")
            if val is None or str(val).strip() == "":
                add(v, e, "warning", "no_value", "Entry has no value")
                continue
            if "UNREADABLE" in str(val).upper():
                add(v, e, "error", "unreadable", "Model could not read this cell")
                continue
            canon, un, num = e.get("canonical", "other"), unit_norm(e.get("unit")), to_num(val)
            if canon not in NON_NUMERIC and num is None:
                add(v, e, "warning", "non_numeric", f"Non-numeric value {val!r}; confirm against the sheet")
            if num is None:
                continue
            if canon in PRESSURE | TEMP | FLOW and not un:
                add(v, e, "warning", "no_unit", "No unit captured")
            if canon in PRESSURE and un and not P_UNIT.search(un):
                add(v, e, "warning", "bad_pressure_unit", f"Unit {e.get('unit')!r} is not a pressure unit")
            if canon in TEMP and un and un not in T_UNITS:
                add(v, e, "warning", "bad_temp_unit", f"Unit {e.get('unit')!r} is not a temperature unit")
            if canon in TEMP and un == "c" and num < -273.15:
                add(v, e, "error", "below_abs_zero", "Temperature below absolute zero")
            if canon in FLOW and num < 0:
                add(v, e, "error", "negative_flow", "Negative flow rate")
            if canon == "specific_gravity" and not (0.05 <= num <= 20):
                add(v, e, "warning", "sg_range", "Specific gravity outside 0.05-20")

        # P1 - dP = P2, per column, only when the three rows are unambiguous and units compatible
        for case in CASES:
            d, dup = {}, set()
            for e in ents:
                if e["case"] == case and e.get("kind", "condition") == "condition" and \
                        e.get("canonical") in ("inlet_pressure", "outlet_pressure", "pressure_drop"):
                    if e["canonical"] in d:
                        dup.add(e["canonical"])
                    d[e["canonical"]] = e
            if len(d) == 3 and not dup:
                p1, p2, dp = d["inlet_pressure"], d["outlet_pressure"], d["pressure_drop"]
                n1, n2, nd = (to_num(x["value"]) for x in (p1, p2, dp))
                u1, u2, ud = (unit_norm(x.get("unit")) for x in (p1, p2, dp))
                if None not in (n1, n2, nd) and u1 == u2 and \
                        _pressure_base(u1) == _pressure_base(ud) and \
                        abs((n1 - nd) - n2) > max(0.02 * abs(n1), 0.05):
                    for x in (p1, p2, dp):  # flag all three: the check cannot tell which one is wrong
                        add(v, x, "error", "p_balance",
                            f"P1 {n1} - dP {nd} != P2 {n2} ({case}); one of the three is misread")

        # min <= normal <= max for flow and temperature
        for canon in FLOW | TEMP:
            by = {e["case"]: e for e in ents if e.get("canonical") == canon
                  and e.get("kind", "condition") == "condition" and to_num(e.get("value")) is not None}
            if {"minimum", "normal", "maximum"} <= set(by) and \
                    len({unit_norm(by[c].get("unit")) for c in by}) == 1:
                lo, no, hi = (to_num(by[c]["value"]) for c in ("minimum", "normal", "maximum"))
                if not (lo <= no <= hi):
                    add(v, by["normal"], "warning", "order", f"Min/Normal/Max not ordered: {lo}, {no}, {hi}")

        rows = {}
        for e in ents:
            rows.setdefault((norm(e.get("section")), norm(e["parameter"])), []).append(e)
        for es in rows.values():
            if len({unit_norm(e.get("unit")) for e in es if e.get("unit")}) > 1:
                add(v, es[0], "warning", "units_differ", "Different units across columns of the same row")

    for k, n in seen_names.items():
        if n > 1:
            issues.append(issue(fname, k.upper(), "", "", "", "warning", "dup_tag",
                                f"Tag appears {n} times in one file - possible split or duplicate"))
    return issues


# ------------------------------------------------------------ raw dicts -> models
def _enum(value, allowed, default):
    return value if value in allowed else default


def to_sheets(file_idx, fname, records, issues, source="model"):
    sheets = []
    for v in records:
        cells = [Cell(section=e.get("section"), row_no=e.get("row_no"), parameter=e["parameter"],
                      canonical=e.get("canonical") or "other",
                      kind=_enum(e.get("kind"), KINDS, "condition"),
                      case=_enum(e.get("case"), CASES, "other"),
                      value=None if e.get("value") is None else str(e["value"]),
                      unit=e.get("unit"), page=e.get("page") or 1, source=source)
                 for e in v["entries"]]
        info = {k: str(v[k]) for k in ("item_no", "line_no", "project", "format_name") if v.get(k)}
        sheet = Sheet(file_idx=file_idx, file_name=fname, title=v.get("sheet_title"),
                      tag=v.get("tag"), fluid=v.get("fluid"), service=v.get("service"),
                      pages=v.get("source_pages") or [], has_values=bool(v.get("has_values")),
                      info=info, cells=cells)
        by = {}
        for c in cells:
            by.setdefault((norm(c.section), norm(c.parameter)), []).append(c)
        for i in (x for x in issues if x["record"] == v["_name"]):
            flag = Flag(severity=i["severity"], code=i.get("code", "check"), message=i["issue"])
            if not i.get("parameter"):
                sheet.notes.append(flag)
                continue
            targets = [c for c in by.get((norm(i.get("section")), norm(i["parameter"])), [])
                       if not i.get("case") or c.case == i["case"]]
            if not targets:
                sheet.notes.append(flag)
            for c in targets:
                c.flags.append(flag)
        sheets.append(sheet)
    return sheets
