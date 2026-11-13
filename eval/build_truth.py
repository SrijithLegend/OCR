"""python eval/build_truth.py -> eval/truth.csv

Values below were read by Claude (the annotator) from page images of eval/sheets/*.pdf, NOT copied from tool output
(see eval/TRUTH_NOTES.md). Template rows with no printed cell are written with an empty value, so any tool cell landing
there scores as spurious. Rows the tool never produced but the sheet prints are appended.
"""
import csv
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SC, VD = "Service Conditions", "Valve Data"
M3, KG, KGG, C = "m3/hr", "Kg/cm2", "Kg/cm2g", "°C"


def common_star(case):  # "*" cells and the printed "max 85"; no unit checked on non-numeric text
    return [(SC, "Cv Min.", case, "*", ""), (SC, "Cv Max.", case, "*", ""), (VD, "Cv Nor.", case, "*", ""),
            (VD, "Selected Cv", case, "*", ""), (VD, "Inlet", case, "*", ""), (VD, "Outlet", case, "*", "")]


def wash_water(state):  # p15 / p16 share every printed value
    return [(SC, "Fluid", "Single", "Sour water", ""), (SC, "State", "Single", state, ""),
            (SC, "Flow Liq-Min", "Minimum", "4.5", M3), (SC, "Flow Liq-Min", "Normal", "12", M3), (SC, "Flow Liq-Min", "Maximum", "14.4", M3),
            (SC, "Inlet Pr. - Normal", "Normal", "10.3", KG),
            (SC, "dP @FlowRateMin", "Minimum", "1.9", KGG), (SC, "dP @FlowRateMin", "Normal", "0.7", KGG), (SC, "dP @FlowRateMin", "Maximum", "0.5", KGG),
            (SC, "dPressure Shut Off", "Single", "16.7", KGG),
            (SC, "Temp. Oper.", "Normal", "96.7", C), (SC, "Temp. Oper.", "Maximum", "150", C),
            (SC, "Specific gravity", "Single", "0.959", ""), (SC, "Viscosity (cp)", "Single", "0.7", "cp"),
            (SC, "Vapour Pr.kg/cm2A", "Single", "1.9", "Kg/cm2a"),
            (VD, "Predicted Sound Level dBA", "Single", "max 85", "")] + common_star("Single")


def p17():
    out = []
    data = {  # case no -> (name, specific gravity, viscosity, vapour, critical)
        1: ("CASE 1 (BH CRUDE)", "0.8151", "2.8", "0.66", "30.8"),
        2: ("CASE 2 (LS CRUDE)", "0.8225", "3.0 at 25 Deg C", "0.97", ""),
        3: ("CASE 3 (HS CRUDE)", "0.872", "9.5", "0.58", "28.89")}
    for n, (c, sg, vis, vp, cp) in data.items():
        out += [(SC, "Fluid", c, "HC", ""), (SC, "State", c, "LIQUID" if n == 1 else "Liquid", ""),
                (SC, "Flow Liq-Min", f"{c} - Normal", "108", M3), (SC, "Flow Liq-Min", f"{c} - Maximum", "140", M3),
                (SC, "Inlet Pr. - Normal", f"{c} - Normal", "9.4", KGG), (SC, "Inlet Pr. - Normal", f"{c} - Maximum", "11", KGG),
                (SC, "dP @FlowRateMin", f"{c} - Normal", "7.7", KGG), (SC, "dP @FlowRateMin", f"{c} - Maximum", "9.3", KGG),
                (SC, "dPressure Shut Off", c, "7.7", KGG),
                (SC, "Temp. Oper.", f"{c} - Normal", "35-40", C), (SC, "Temp. Oper.", f"{c} - Maximum", "65", C),
                (SC, "Specific gravity", c, sg, ""), (SC, "Viscosity (cp)", c, vis, "cp"),
                (SC, "Vapour Pr.kg/cm2A", c, vp, "Kg/cm2a"), (SC, "Critical Pr.", c, cp, "Kg/cm2a"),
                (VD, "Predicted Sound Level dBA", c, "max 85", "")] + common_star(c)
    return out


def p18():
    return [(SC, "Fluid", "Single", "Steam", ""), (SC, "State", "Single", "Vapor", ""),
            (SC, "Flow Steam-Min", "Minimum", "14", "MT/D"), (SC, "Flow Steam-Min", "Normal", "50", "MT/D"), (SC, "Flow Steam-Min", "Maximum", "62", "MT/D"),
            (SC, "Inlet Pr. - Normal (kg/cm2A)", "Normal", "3.9", "kg/cm2A"), (SC, "Inlet Pr. - Normal (kg/cm2A)", "Maximum", "4.5", "kg/cm2A"),
            (SC, "dP @FlowRateMin", "Maximum", "0.4", KGG),
            (SC, "dPressure Shut Off (KG/CM2)", "Single", "6.5", "KG/CM2"),
            (SC, "Temp. Oper.", "Normal", "142", C), (SC, "Temp. Oper.", "Maximum", "170", C),
            (SC, "Mol. Wt.", "Single", "18", ""), (SC, "Cp/Cv", "Single", "1.3", ""), (SC, "Compress Factor", "Single", "1", ""),
            (VD, "Predicted Sound Level dBA", "Single", "max 85", "")] + common_star("Single")


def p19():
    out = []
    s = {1: dict(flow=("700", "2300", "3000"), inlet=("10.04", "11.8"), dp=("2.8", "1.67", "0.75"), shut="16.1", t=("77", "100")),
         2: dict(flow=("1400", "3444", "5800"), inlet=("12.2", "13.2"), dp=("4.2", "3.2", "1.8"), shut="31.6", t=("40", "65"))}
    for n, d in s.items():
        c = f"STREAM-{n}"
        out += [(SC, "State", c, "LIQUID", "")]
        out += [(SC, "Flow Liq-Min", f"{c} - {k}", v, "MT/D") for k, v in zip(("Minimum", "Normal", "Maximum"), d["flow"])]
        out += [(SC, "Inlet Pr. - Normal (kg/cm2A)", f"{c} - {k}", v, "kg/cm2A") for k, v in zip(("Normal", "Maximum"), d["inlet"])]
        out += [(SC, "dP @FlowRateMin", f"{c} - {k}", v, "kg/cm2g") for k, v in zip(("Minimum", "Normal", "Maximum"), d["dp"])]
        out += [(SC, "dPressure Shut Off (KG/CM2A)", c, d["shut"], "KG/CM2A")]
        out += [(SC, "Temp. Oper.", f"{c} - {k}", v, C) for k, v in zip(("Normal", "Maximum"), d["t"])]
        out += [(SC, "Specific gravity @ 20.c", c, "1.038", ""), (SC, "Mol. Wt.", c, "27.52", ""),
                (SC, "Viscosity (cp)@ 20.c", c, "1.01", "cp"), (SC, "Vapour Pr. (pa@ 20.c)", c, "1", "Kg/cm2a")]
        out += common_star(c)
    return out + [(VD, "Predicted Sound Level dBA", "Single", "max 85", "")]


def p20():
    return [(SC, "Fluid", "Single", "WATER", ""), (SC, "State", "Single", "LIQUID", ""),
            (SC, "Flow Liq-Min", "Minimum", "300", "MT/D"), (SC, "Flow Liq-Min", "Normal", "600", "MT/D"), (SC, "Flow Liq-Min", "Maximum", "1400", "MT/D"),
            (SC, "Inlet Pr. - Normal", "Normal", "3", KGG), (SC, "Inlet Pr. - Normal", "Maximum", "4", KGG),
            (SC, "dP @FlowRateMin", "Minimum", "4", KGG), (SC, "dP @FlowRateMin", "Normal", "3", KGG),
            (SC, "Temp. Oper.", "Normal", "35", C), (SC, "Temp. Oper.", "Maximum", "65", C),
            (VD, "Predicted Sound Level dBA", "Single", "max 85", "")] + common_star("Single")


SPEC = {"tender_p15.pdf": ("DFV-158A", "DFV-158B"), "tender_p16.pdf": ("DFV-158C", "DFV-158D")}
BUILD = {"tender_p15.pdf": lambda t: wash_water("LIQUID" if t.endswith("A") else "Liquid"),
         "tender_p16.pdf": lambda t: wash_water("LIQUID" if t.endswith("C") else "Liquid"),
         "tender_p17.pdf": lambda t: p17(), "tender_p18.pdf": lambda t: p18(),
         "tender_p19.pdf": lambda t: p19(), "tender_p20.pdf": lambda t: p20()}

tpl = list(csv.DictReader(open(ROOT / "eval/truth_template.csv", encoding="utf-8")))
tags = {}
for r in tpl:
    tags.setdefault(r["file"], [])
    if r["valve_tag"] not in tags[r["file"]]:
        tags[r["file"]].append(r["valve_tag"])

out, n_added = [], 0
for f, build in BUILD.items():
    for tag in tags[f]:
        want = {(s, p, c): (v, u) for s, p, c, v, u in build(tag)}
        have = set()
        for r in (x for x in tpl if x["file"] == f and x["valve_tag"] == tag):
            k = (r["section"], r["parameter"], r["case"])
            have.add(k)
            v, u = want.get(k, ("", r["unit"]))
            out.append([f, tag, *k, u if v else r["unit"], v, 0])
        for k, (v, u) in want.items():
            if k not in have:
                out.append([f, tag, *k, u, v, 0])
                n_added += 1
with open(ROOT / "eval/truth.csv", "w", newline="", encoding="utf-8") as fh:
    w = csv.writer(fh)
    w.writerow(["file", "valve_tag", "section", "parameter", "case", "unit", "value", "prefilled"])
    w.writerows(out)
keyed = [r for r in out if r[6]]
print(f"{len(out)} rows, {len(keyed)} keyed values ({n_added} added beyond template), {len(out) - len(keyed)} blank (no printed cell)")
