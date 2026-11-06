"""Adaptive Document Controller, stage 1 (AMDP section 13.4): rule-based routing, no learning.

It decides how a file is read, and it logs why. The one hard constraint is the model API's
limit of 100 images per request.
"""
from dataclasses import dataclass, field

MAX_IMAGES_PER_REQUEST = 100
DENSE_TEXT_CHARS = 2500  # a text layer this long on one page means a dense, often two-column sheet


@dataclass
class Plan:
    passes: int
    bands: int          # vertical zoom tiles per page (0 = none)
    cols: int           # horizontal zoom tiles per page
    adjudicate: bool = True
    notes: list = field(default_factory=list)

    def images_per_page(self):
        tiles = (max(self.bands, 1) * max(self.cols, 1)) if (self.bands > 0 or self.cols > 1) else 0
        return 1 + tiles


def plan_for(pages, default_passes: int) -> Plan:
    n = len(pages)
    dense = any(len(t) > DENSE_TEXT_CHARS for _, t in pages)
    plan = Plan(passes=default_passes, bands=3, cols=2 if dense else 1)
    if dense:
        plan.notes.append("dense text layer: 2 tile columns")
    # shrink tiling until the request fits the image limit
    while n * plan.images_per_page() > MAX_IMAGES_PER_REQUEST and (plan.cols > 1 or plan.bands > 0):
        if plan.cols > 1:
            plan.cols = 1
        else:
            plan.bands -= 1
        plan.notes.append(f"{n} pages: reduced tiling to {plan.bands}x{plan.cols}")
    if n * plan.images_per_page() > MAX_IMAGES_PER_REQUEST:
        raise ValueError(f"{n} pages cannot fit one request; split the file.")
    return plan
