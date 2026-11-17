"""Shared rules for the Classic 350 parts dataset: id format and its check characters.
Id = CL350-<CAT>-<NNN>-<CC>.  CC is 2 check characters computed from the part's name + attributes, so an id that was
copied from a different part fails the check (like a check digit on a part number)."""
import hashlib
import re

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


def sig(name, attrs):
    return " ".join(sorted(re.findall(r"[a-z0-9]+", f"{name} {attrs}".lower())))


def check(name, attrs):
    return hashlib.sha1(sig(name, attrs).encode()).hexdigest()[:2].upper()


def make_id(name, attrs, n):
    return f"CL350-{cat_of(name)}-{n:03d}-{check(name, attrs)}"


def id_ok(pid, name, attrs):
    """True if the id has the right shape, category and check characters for this part."""
    m = re.fullmatch(r"CL350-([A-Z]{3})-(\d{3})-([0-9A-F]{2})", str(pid or ""))
    return bool(m) and m[1] == cat_of(name) and m[3] == check(name, attrs)
