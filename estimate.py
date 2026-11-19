"""What-if prices: "prices of grips if there were yellow in india" when no yellow grip exists -> an ESTIMATE.

The model only reads the question (part, attribute, value, price columns). The arithmetic is plain Python on the stored rows:
1. Ratio: other parts that have the wanted value AND one of this part's values give a price ratio (Yellow / Black ...);
   this part's own price for that value x the ratio = the estimate (averaged over the values it has).
2. Fallback: nobody else has the value -> the average of this part's existing variants (low confidence, said so).
Reads only; nothing is written. Works on any table idfix.find_table recognises that has an attribute column written 'Key: Value; Key: Value'."""
import collections
import re
import statistics

import idfix
import nlsql

SYSTEM = """You read a question about a parts catalogue. Reply as JSON.
- is_what_if: true only if it asks the price of a variant of a part that may not exist ("if there were yellow", "what would X cost in Y",
  "estimate the price of ..."). Plain lookups are false.
- part: the product words only, without the variant value and without words like price. Correct obvious spelling mistakes ("hand break" -> "hand brake").
- attribute_key: EXACTLY one of the ATTRIBUTE KEYS that the wanted value belongs to ("" if none fits).
- value: the wanted attribute value, as the user wrote it.
- price_columns: the PRICE COLUMNS the question asks for (for example "in india" -> the India column); [] if it names none.
- market: the country or region the question wants a price for when that is NOT covered by the PRICE COLUMNS (for example "korea"); ""
  otherwise. A price asked for a market that is not stored is a what-if (is_what_if true), even with no variant (value "")."""
SCHEMA = {"type": "object", "required": ["is_what_if", "part", "attribute_key", "value", "price_columns", "market"],
          "properties": {"is_what_if": {"type": "boolean"}, "part": {"type": "string"}, "attribute_key": {"type": "string"},
                         "value": {"type": "string"}, "price_columns": {"type": "array", "items": {"type": "string"}}, "market": {"type": "string"}}}


def _attrs(s):
    """'Colour: Black; Size: L' -> [(key, value)]"""
    return [(k.strip(), v.strip()) for k, sep, v in (p.partition(":") for p in str(s or "").split(";")) if sep and k.strip() and v.strip()]


def _num(x, whole):
    return float(round(x)) if whole else round(x, 2)


def _load(path):
    """-> (name col, attr col, price cols, {(name, attrs): ([prices per column], text, description)}) with duplicate rows collapsed to their median price."""
    conn = nlsql._connect(path, sandbox=False)
    try:
        table, cols = idfix.find_table(conn)
        if "attrs" not in cols:
            return None
        pcols = [cols["price"], *cols["more"]]
        sel = ", ".join(idfix._q(cols[r]) if r in cols else "NULL" for r in ("name", "attrs", "desc")) + "".join(", " + idfix._q(c) for c in pcols)
        raw = collections.defaultdict(list)
        text, descs = {}, {}
        for name, attrs, desc, *prices in conn.execute(f"SELECT {sel} FROM {idfix._q(table)}"):
            raw[(str(name or ""), str(attrs or ""))].append(prices)
            text[(str(name or ""), str(attrs or ""))] = f"{name} {desc} {attrs}".lower()
            descs[(str(name or ""), str(attrs or ""))] = str(desc or "")
    finally:
        conn.close()
    med = lambda col: statistics.median(v) if (v := [float(p) for p in col if isinstance(p, (int, float))]) else None
    return cols["name"], cols["attrs"], pcols, {k: ([med(c) for c in zip(*v)], text[k], descs[k]) for k, v in raw.items()}


def _web(p, ncol, acol, pcols, variants):
    """A price for a market the table has no column for: Gemini + Google Search (idfix.web_prices) -> an Estimate row. Never raises."""
    market = p["market"].strip()
    words = [w[:-1] if w.endswith("s") and len(w) > 3 else w for w in re.findall(r"[a-z0-9]+", p["part"].lower())]
    mine = {k: v for k, v in variants.items() if words and all(w in v[1] for w in words)}
    names = sorted({n for n, _ in mine})[:3] or [p["part"].strip()]  # not in the table at all: look the part up by what the user called it
    desc = {n: next((v[2] for (m, _), v in mine.items() if m == n and v[2]), "") for n in names}
    attr = f"{p['attribute_key']}: {p['value'].strip()}" if p["attribute_key"].strip() and p["value"].strip() else ""
    found = idfix.web_prices([(n, desc[n], attr) for n in names], market=market)
    columns = [ncol, acol, *pcols, f"price_in_{market.lower().replace(' ', '_')}", "currency", "Source"]
    rows = [[n, a, *pr, None, None, "Stored"] for (n, a), (pr, _, _) in sorted(mine.items())][:6]
    basis, ok = [], False
    for n, (price, src) in zip(names, found):
        cur, _, site = src.partition(",")
        if price is None:
            basis.append(f"{n}: no price found for {market} ({src}).")
            continue
        ok = True
        rows.append([n, attr or "(any variant)", *[None] * len(pcols), price, cur.strip() if site else "", "Estimate"])
        basis.append(f"{n}: typical retail price in {market} from a web search ({(site or cur).strip() or 'no site named'}), in {cur.strip() if site else 'the local currency'}. "
                     "Not stored in the database; prices vary by seller, so treat it as a guide.")
    if not ok:
        return {"kind": "empty", "text": f"'{p['part']}' in {market} is not stored and the web lookup found nothing: " + " ".join(basis),
                "sql": "", "columns": [], "rows": [], "truncated": False}
    return {"kind": "table", "text": f"'{p['part']}' in {market} is not stored, so the {market} price is a web estimate.",
            "sql": "", "columns": columns, "rows": rows, "truncated": False, "estimated": True, "basis": basis}


def what_if(question, path):
    """-> a result dict shaped like nlsql.ask's (kind 'table', plus estimated=True and basis=[...]), or None when this is not a what-if
    question, the variant already exists, or there is nothing to estimate from."""
    loaded = _load(path)
    if not loaded:
        return None
    ncol, acol, pcols, variants = loaded
    seen = collections.defaultdict(set)  # attribute key -> its values in the table
    for (_, a) in variants:
        for k, v in _attrs(a):
            seen[k].add(v)
    keys = sorted(seen)[:60]
    examples = {k: sorted(seen[k])[:8] for k in keys}
    p = nlsql._model(f"ATTRIBUTE KEYS (with some of their values): {examples}\nPRICE COLUMNS: {pcols}\n\nQUESTION\n{question}", SYSTEM, SCHEMA)
    if not p.get("is_what_if"):
        return None
    if p.get("market", "").strip() and p.get("part", "").strip():
        return _web(p, ncol, acol, pcols, variants)
    if not p.get("value", "").strip():
        return None
    key = next((k for k in keys if k.lower() == p["attribute_key"].strip().lower()), None)
    want = p["value"].strip()
    want = want.title() if want.islower() else want  # stored values are capitalised
    use = [i for i, c in enumerate(pcols) if c in p["price_columns"]] or list(range(len(pcols)))
    words = [w[:-1] if w.endswith("s") and len(w) > 3 else w for w in re.findall(r"[a-z0-9]+", p["part"].lower())]
    holders = [k for k in keys if want.lower() in {v.lower() for v in seen[k]}]  # keys that really hold this value somewhere
    if holders and key not in holders:
        key = holders[0]  # the model picked a key the value never appears under
    if not key or not words:
        return None
    # groups: same name and same other attributes, different value of the wanted attribute
    groups = collections.defaultdict(dict)  # (name, other attrs) -> {value lower: (value, prices, attrs string)}
    for (name, attrs), (prices, text, _) in variants.items():
        at = _attrs(attrs)
        mine = [v for k, v in at if k == key]
        if mine:
            groups[(name, tuple((k, v.lower()) for k, v in at if k != key))][mine[0].lower()] = (mine[0], prices, attrs, text)
    targets = {g: vals for g, vals in groups.items() if all(w in next(iter(vals.values()))[3] for w in words)}
    if not targets or any(want.lower() in vals for vals in targets.values()):
        return None  # no such part, or the variant exists (the ordinary answer already covers it)
    ratios = collections.defaultdict(list)  # (reference value lower, column) -> [price(want) / price(reference)]
    for vals in groups.values():
        if want.lower() in vals:
            for r, (_, pr, _, _) in vals.items():
                for c in use:
                    if r != want.lower() and pr[c] and vals[want.lower()][1][c]:
                        ratios[(r, c)].append(vals[want.lower()][1][c] / pr[c])
    columns, rows, basis = [ncol, acol, *[pcols[c] for c in use], "Source"], [], []
    for (name, _), vals in sorted(targets.items()):
        whole = all(float(x).is_integer() for _, pr, _, _ in vals.values() for x in pr if x is not None)
        for v, pr, attrs, _ in sorted(vals.values(), key=lambda t: t[0]):
            rows.append([name, attrs, *[pr[c] for c in use], "Stored"])
        est, notes = [], []
        for c in use:
            parts = [(r, vals[r][1][c] * statistics.median(ratios[(r, c)]), len(ratios[(r, c)]), statistics.median(ratios[(r, c)]))
                     for r in vals if ratios.get((r, c)) and vals[r][1][c]]
            if parts:
                est.append(_num(statistics.mean(e for _, e, _, _ in parts), whole))
                if c == use[0]:
                    notes = [f"{vals[r][0]}: {vals[r][1][c]:g} x {m:.2f} (average {want} / {vals[r][0]} price on {n} other part{'s' * (n != 1)})" for r, _, n, m in parts]
            else:
                known = [vals[r][1][c] for r in vals if vals[r][1][c] is not None]
                est.append(_num(statistics.mean(known), whole) if known else None)
        first = [vals[r][1][use[0]] for r in vals if vals[r][1][use[0]] is not None]
        if all(e is None for e in est):
            continue
        new_attrs = "; ".join(f"{k}: {want}" if k == key else f"{k}: {v}" for k, v in _attrs(next(iter(vals.values()))[2]))
        rows.append([name, new_attrs, *est, "Estimate"])
        weak = " Only one other part to compare with, so treat it as rough." if notes and all("on 1 other part)" in n for n in notes) else ""
        basis.append(f"{name}: " + ("; ".join(notes) + ", averaged." + weak if notes else
                     f"no other part is priced in {want}, so this is only the average of the existing variants ({min(first):g} to {max(first):g}). Treat it as rough."))
    if not basis:
        return None
    return {"kind": "table", "text": f"No {p['part']} in {want} is stored, so the {want} rows are estimates from the existing prices.",
            "sql": "", "columns": columns, "rows": rows, "truncated": False, "estimated": True, "basis": basis}
