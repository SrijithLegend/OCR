import math
import re


def norm(s) -> str:
    return re.sub(r"\s+", " ", str(s if s is not None else "")).strip().lower()


def unit_norm(u) -> str:
    s = norm(u)
    for a, b in (("²", "2"), ("³", "3"), ("°", ""), ("º", ""), ("deg", "")):
        s = s.replace(a, b)
    return re.sub(r"[\s_()\[\].]", "", s)


_NUM = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")
_THOU = re.compile(r"^[+-]?\d{1,3}(,\d{3})+(\.\d+)?$")


def to_num(s):
    """float for plain numbers (also '1,500' and '1.4E+06'); None for anything else."""
    if s is None:
        return None
    t = str(s).strip()
    if _THOU.match(t):
        t = t.replace(",", "")
    return float(t) if _NUM.match(t) else None


def same_value(a, b) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    na, nb = to_num(a), to_num(b)
    if na is not None and nb is not None:
        return math.isclose(na, nb, rel_tol=1e-9, abs_tol=1e-12)
    return norm(a) == norm(b)
