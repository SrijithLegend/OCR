"""Canned extractor for tests and UI demos. No network, no model. NOT a real reader."""
from ..consistency import issue, normalize


def _t(param, canon, vals, unit, kind="condition", sec="PROCESS DATA", row=None):
    return [dict(section=sec, row_no=row, parameter=param, canonical=canon, kind=kind, case=c,
                 case_label=c[:3].title(), value=v, unit=unit, page=1)
            for c, v in zip(("maximum", "normal", "minimum"), vals)]


class StubExtractor:
    name = "stub"

    def extract(self, pages, plan, fname):
        v1 = dict(sheet_title="Control valve data sheet", format_name="Demo", tag="DEMO-101",
                  item_no="1", fluid="Water", service="Demo service", source_pages=[1], has_values=True,
                  entries=_t("Inlet Pressure (P1)", "inlet_pressure", ["56.7", "56.7", "56.7"], "bar(g)", row=3)
                  + _t("Outlet Pressure (P2)", "outlet_pressure", ["52.7", "52.9", "40.0"], "bar(g)", row=4)
                  + _t("Pressure Drop (dP)", "pressure_drop", ["4.0", "3.9", "6.7"], "bar", row=5)
                  + _t("Flow Rate Mass", "flow_rate_mass", ["45300.0", "41000.0", "UNREADABLE"], "kg/h", row=2)
                  + _t("Inlet Temperature", "inlet_temperature", ["150", "150", "127"], "C", row=6)
                  + _t("Inlet State", "fluid_state", ["Liquid", "Liquid", "Liquid"], None, row=1)
                  + _t("SPL at 1 metre", "other", ["<75", "<75", "<75"], "dBA", kind="calculated", row=12))
        v2 = dict(sheet_title="Spec sheet", tag="DEMO-102", source_pages=[min(2, len(pages))], has_values=True,
                  entries=_t("Inlet Pressure (P1)", "inlet_pressure", ["10.0", "10.0", "10.0"], "kg/cm2g", sec="OPERATING DATA")
                  + _t("Flow Rate", "flow_rate_gas_std", ["3600", "3000", "1500"], "scfh", sec="OPERATING DATA"))
        valves = normalize([v1, v2])
        issues = [issue(fname, "DEMO-101", "PROCESS DATA", "Flow Rate Mass", "maximum", "warning",
                        "pass_disagree", "Reads disagreed (1: '43300.9', 2: '45300.0'); re-read decided '45300.0'")]
        return valves, issues
