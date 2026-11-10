"""Optional LLM label mapping (claude.md section 3, Phase 3).

The model sees ONLY row labels and units, never values, and may only answer with a name from the
canonical list. Code rejects anything else. So it can re-tag a row ("Upstream P" -> inlet_pressure)
but it cannot touch, invent or "fix" a number: provenance is trivially preserved.
"""
from ..constants import CANON
from ..consistency import issue
from ..textutil import norm
from .mdparse import kind_of

TOOL = {
    "name": "map_labels",
    "description": "Map each printed datasheet row label to the closest canonical parameter, or 'other'.",
    "input_schema": {"type": "object", "properties": {"mapping": {"type": "array", "items": {
        "type": "object", "properties": {"id": {"type": "integer"},
                                         "canonical": {"type": "string", "enum": CANON}},
        "required": ["id", "canonical"]}}}, "required": ["mapping"]},
}
SYSTEM = ("You classify row labels of industrial datasheets. Use only the given canonical names. "
          "Choose 'other' when nothing fits; never force a fit. You are given labels and units only.")


def claude_proposer(model, client=None):
    def propose(items):  # items: [(id, label, unit)]
        import anthropic
        c = client or anthropic.Anthropic()
        text = "\n".join(f'{i}: "{label}" unit={unit or "-"}' for i, label, unit in items)
        resp = c.messages.create(model=model, max_tokens=4000, system=SYSTEM, tools=[TOOL],
                                 tool_choice={"type": "tool", "name": "map_labels"},
                                 messages=[{"role": "user", "content": text}])
        for b in resp.content:
            if b.type == "tool_use":
                return {m["id"]: m["canonical"] for m in b.input.get("mapping", [])}
        return {}
    return propose


def apply_label_map(records, propose, fname, issues):
    """Re-tag entries whose canonical is 'other'. Values and units are never read or written."""
    rows = {}
    for v in records:
        for e in v["entries"]:
            if e.get("canonical") == "other" and e.get("kind") != "calculated":
                rows.setdefault(norm(e["parameter"]), []).append((v, e))
    if not rows:
        return
    items = [(i, es[0][1]["parameter"], es[0][1].get("unit")) for i, es in enumerate(rows.values())]
    answers = propose(items)
    for i, es in enumerate(rows.values()):
        canon = answers.get(i)
        if canon in CANON and canon != "other":  # anything outside the enum is ignored
            for v, e in es:
                e["canonical"] = canon
                if e.get("kind") != "calculated":
                    e["kind"] = kind_of(e["parameter"], canon)
                issues.append(issue(fname, v["_name"], e.get("section"), e["parameter"], e["case"],
                                    "info", "label_mapped", f"Label tagged {canon} by the label mapper"))
