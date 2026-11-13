"""Run: python -m pytest eval/test_standard_valve.py -q"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import standard_valve as sv

LIST = Path(__file__).parent / "valve_body_list.xlsx"


def test_rows_without_part_number_are_listed():
    rows = [["Part No.", "Description"], ["A1", "valve x"], ["A1", "Valve  X"], [None, "orphan"], ["B2", "valve y"]]
    n, out, excluded = sv.run(rows)
    assert n == 4 and len(out) == 2
    assert excluded == [[4, "no part number", "orphan"]]


@pytest.mark.skipif(not LIST.exists(), reason="eval/valve_body_list.xlsx not provided yet")
def test_valve_body_list():
    n, out, excluded = sv.run(sv.load_table(LIST))
    assert n == 27 and len(out) == 6 and len(excluded) == 1
    row = next(r for r in out if r[0] == "59A3655")
    assert row[3] and row[3].count("(x") == 3  # "3 disagreeing values": adjust if the file counts them differently
