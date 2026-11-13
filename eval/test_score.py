"""Smoke test for score.py on a synthetic workbook. Run: python -m pytest eval/test_score.py -q"""
import sys
from pathlib import Path

import openpyxl
from openpyxl.styles import PatternFill

sys.path.insert(0, str(Path(__file__).parent))
import score


def test_score(tmp_path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Datasheet Data"
    ws.append(["Source File", "Valve", "Value", "Process\nInlet pressure\n(bar)", "Process\nTemperature\n(degC)", "Process\nFluid"])
    ws.append(["a.pdf", "FV-1", "Normal", 10, 99, "Steam"])      # pressure ok, temp wrong+flagged, fluid ok
    ws.append(["a.pdf", "FV-1", "Maximum", 12.04, 100, "Spur"])  # 12.04 vs 12 = within 0.5% only; temp wrong silent; spurious fluid
    ws["E2"].fill = PatternFill("solid", fgColor="FFC7CE")
    wb.save(tmp_path / "a.pdf.xlsx")
    truth = tmp_path / "t.csv"
    truth.write_text("file,valve_tag,section,parameter,case,unit,value,prefilled\n"
                     "a.pdf,FV-1,Process,Inlet pressure,Normal,bar,10,0\n"
                     "a.pdf,FV-1,Process,Temperature,Normal,degC,98,0\n"
                     "a.pdf,FV-1,Process,Fluid,Normal,,Steam,0\n"
                     "a.pdf,FV-1,Process,Inlet pressure,Maximum,bar,12,0\n"
                     "a.pdf,FV-1,Process,Temperature,Maximum,degC,UNREADABLE,0\n")
    r = score.score_dir(score.load_truth(truth), tmp_path)
    o, c = r["overall"], r["counts"]
    assert c["gt"] == 5 and c["ok"] == 2 and c["ok_tol"] == 3 and c["unreadable"] == 1
    assert c["wrong"] == 3 and c["wrong_flagged"] == 1 and c["spurious"] == 1
    assert o["silent_error_rate"] == 2 / 5 and o["error_recall"] == 1 / 3
