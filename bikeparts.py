"""Only used by gen_bikes.py (the sample dataset): id format and category codes. The repair code (idfix.py) knows none of this.
Id = CL350-<CAT>-<NNN>-<CC>.  CC is 2 check characters computed from the part's name + attributes, so an id that was
copied from a different part fails the check (like a check digit on a part number)."""
from idfix import check, sig  # noqa: F401  (the check characters are generic; only the categories below are bike specific)

# category code -> words that put a part name in it (first hit wins, order matters)
CATS = {"SEA": ["seat"], "BRK": ["brake", "caliper", "disc"], "FLT": ["filter"],
        "ENG": ["piston", "gasket", "spark plug", "valve", "cylinder", "clutch plate"],
        "ELE": ["bulb", "headlight", "battery", "indicator", "horn", "tail lamp", "regulator"],
        "TYR": ["tyre", "tube"], "CHN": ["chain", "sprocket"], "SUS": ["fork", "shock"],
        "CBL": ["cable", "lever"], "EXH": ["silencer", "exhaust"],
        "BDY": ["mirror", "mudguard", "tank", "handlebar", "grip", "footrest", "stand", "guard", "badge"]}


def cat_of(name):
    n = name.lower()
    return next((c for c, ws in CATS.items() if any(w in n for w in ws)), "GEN")


def make_id(name, attrs, n):
    return f"CL350-{cat_of(name)}-{n:03d}-{check(name, attrs)}"
