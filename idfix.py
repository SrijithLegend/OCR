"""Find rows of ANY catalogue-style table whose id does not belong to the row, and work out the right id and price.
CLI: python idfix.py [db] [--apply]

The table and its columns are found from the database itself: a table with an id column, a name/title column and a price
column (description and attribute/spec columns are used when present). Nothing about the data is built in.

1. Which ids are wrong?  If the ids end in 2 check characters computed from name + attributes (see check()), a row whose
   id fails the check is wrong. Otherwise the table's own redundancy is used: a part (name + attributes) should always
   carry one id and an id should belong to one part; the minority loses. A tie is reported as "ambiguous", never guessed.
2. Fix from the database: the same part (name + attributes) elsewhere with a good id -> its id and price.
3. Not there -> Gemini with Google Search finds the price (in the currency of similar items), and the row gets a new id
   that copies the shape of the most similar row's id (next free number, check characters recomputed if the table uses them).
Nothing is written unless you pass --apply. Sent to Gemini for step 3: the part's name/description/attributes plus a few
other items of the table (name, attributes, price) so it can match currency and scale."""
import collections, hashlib, os, re, sqlite3, sys, time
from pathlib import Path

import nlsql  # loads .env, shares the Gemini client and rate-limit settings

BATCH = 8
MAX_WEB = 200  # most parts looked up on the web in one run
GROUND_MODEL = os.environ.get("GROUND_MODEL", nlsql.MODEL)  # model used with Google Search; needs a grounding quota on your key
ROLES = {"id": r"(^|_)(id|code|sku|no|num|number)$|^(id|sku)", "name": r"name|title", "desc": r"desc", "attrs": r"attr|spec|variant|propert",
         "price": r"price|cost|amount|rate|mrp"}


def sig(name, attrs):
    return " ".join(sorted(re.findall(r"[a-z0-9]+", f"{name} {attrs}".lower())))


def check(name, attrs):
    """Two check characters for a part, for datasets whose ids end with them."""
    return hashlib.sha1(sig(name, attrs).encode()).hexdigest()[:2].upper()


def _q(n):
    return '"' + str(n).replace('"', '""') + '"'


def find_table(conn):
    """-> (table, {role: column}) for the table that best looks like a catalogue; raises ValueError if none does."""
    best = None
    for (t,) in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%'"):
        cols, used, found = [r[1] for r in conn.execute(f"PRAGMA table_info({_q(t)})")], set(), {}
        for role, rx in ROLES.items():
            for c in cols:
                if c not in used and re.search(rx, c, re.I):
                    found[role] = c
                    used.add(c)
                    break
        found["more"] = [c for c in cols if c not in used and re.search(ROLES["price"], c, re.I)]  # further price columns (e.g. one per country)
        if {"id", "name", "price"} <= found.keys() and (best is None or len(found) > len(best[1])):
            best = (t, found)
    if not best:
        raise ValueError("No table with an id column, a name/title column and a price column was found.")
    return best


def _words(s):
    return set(s.split())


def _load(path):
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        table, cols = find_table(conn)
        sel = ", ".join(_q(cols[r]) if r in cols else "NULL" for r in ("id", "name", "desc", "attrs", "price"))
        more = "".join(", " + _q(c) for c in cols["more"])
        rows = [(rid, pid, str(n or ""), str(d or ""), str(a or ""), p, tuple(x)) for rid, pid, n, d, a, p, *x in conn.execute(f"SELECT rowid, {sel}{more} FROM {_q(table)}")]
    finally:
        conn.close()
    return table, cols, rows


def _key(r):
    """What identifies a part: name + attributes (+ description when the table has no attribute column)."""
    return sig(r[2], r[4] or r[3])


def _chk_ok(r):
    return bool(re.search(r"[0-9A-F]{2}$", str(r[1] or ""))) and str(r[1])[-2:].upper() == check(r[2], r[4] or r[3])


def web_prices(parts, context=""):
    """[(name, description, attributes)] -> [(price or None, source text)] from Google Search via Gemini. Never raises: a failure is returned as the source text."""
    from google.genai import types
    out = [(None, "")] * len(parts)
    for i in range(0, len(parts), BATCH):
        chunk = parts[i:i + BATCH]
        lines = "\n".join(f"{n}. {name} | {desc} | {attrs}" for n, (name, desc, attrs) in enumerate(chunk, 1))
        prompt = ("These items come from a product catalogue database. " + context + "For each item below, search the web for its typical current retail "
                  "price for exactly that variant (colour, size, material, model as given), in the same currency as the example prices. "
                  "Reply with one line per item, nothing else, in the form\n<number>|<price as a plain number>|<website name>\n"
                  "If you cannot find a price write <number>|none|none.\n\n" + lines)
        text = ""
        for attempt in range(3):
            try:
                r = nlsql._get_client().models.generate_content(model=GROUND_MODEL, contents=prompt, config=types.GenerateContentConfig(
                    temperature=0, tools=[types.Tool(google_search=types.GoogleSearch())]))
                text = r.text or ""
                break
            except Exception as e:
                msg = f"{type(e).__name__} {e}"
                m = re.search(r"retry in ([0-9.]+)s", msg)
                if m and attempt < 2 and float(m[1]) <= nlsql.RATE_WAIT_MAX:  # a per-minute limit says how long to wait; a quota does not
                    time.sleep(float(m[1]) + 1)
                    continue
                if attempt < 2 and "429" not in msg:
                    time.sleep(3)  # dropped connection: try again
                    continue
                text = "ERROR " + ("Google Search quota exhausted for this key/model (set GROUND_MODEL or enable billing)" if "429" in msg else msg[:150])
                break
        if text.startswith("ERROR"):  # same cause for the rest: stop instead of hammering the API
            return [(None, text[6:])] * len(parts)
        for n in range(1, len(chunk) + 1):
            m = re.search(rf"(?m)^\s*{n}\s*\|\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*\|\s*(.*)$", text)
            out[i + n - 1] = (float(m[1].replace(",", "")), m[2].strip() or "web") if m else (None, "no price found on the web")
    return out


def _wrong_rows(rows):
    """-> (set of row ids that are wrong, set that are ambiguous, how the verdict was reached)."""
    if sum(map(_chk_ok, rows)) >= 0.4 * len(rows):
        return {r[0] for r in rows if not _chk_ok(r)}, set(), "check characters"
    cnt, per_sig, per_id = collections.Counter(), collections.defaultdict(collections.Counter), collections.defaultdict(set)
    for r in rows:
        cnt[(r[1], _key(r))] += 1
        per_sig[_key(r)][r[1]] += 1
        per_id[r[1]].add(_key(r))
    top, tied = {}, set()
    for s, ids in per_sig.items():  # each part's own id: most used, then one that belongs to no other part
        ranked = sorted(ids, key=lambda i: (ids[i], len(per_id[i]) == 1), reverse=True)
        if len(ranked) > 1 and (ids[ranked[0]], len(per_id[ranked[0]]) == 1) == (ids[ranked[1]], len(per_id[ranked[1]]) == 1):
            tied.add(s)
        top[s] = ranked[0]
    claim = collections.defaultdict(list)
    for s, i in top.items():
        claim[i].append(s)
    lose, amb = set(), set()
    for r in rows:
        s, i = _key(r), r[1]
        if s in tied:
            amb.add(r[0])
        elif top[s] != i:
            lose.add(r[0])
        elif len(claim[i]) > 1:  # two parts both claim this id: the one that uses it more wins, a tie is ambiguous
            best = max(cnt[(i, c)] for c in claim[i])
            if cnt[(i, s)] < best:
                lose.add(r[0])
            elif sum(cnt[(i, c)] == best for c in claim[i]) > 1:
                amb.add(r[0])
    return lose, amb - lose, "duplicate ids"


def _mint(template, n, checked, name, attrs):
    base = template[:-2] if checked else template
    runs = list(re.finditer(r"\d{2,}", base))
    if not runs:
        return None
    m = runs[-1]
    new = base[:m.start()] + str(n).zfill(len(m[0])) + base[m.end():]
    return new + check(name, attrs) if checked else new


def check_table(path, web=True):
    """-> {"table", "columns", "mode", "total", "bad", "fixes": [{rowid, name, attrs, old_id, old_price, new_id, new_price, how, source}]}"""
    table, cols, rows = _load(path)
    lose, amb, mode = _wrong_rows(rows)
    if mode == "duplicate ids" and len(lose | amb) > 0.4 * len(rows):
        raise ValueError(f"Table {table} does not look like a catalogue (an id per part): {len(lose | amb):,} of {len(rows):,} rows disagree about their id.")
    checked = mode == "check characters"
    good = {}
    for r in rows:
        if r[0] not in lose and r[0] not in amb:
            good.setdefault(_key(r), (r[1], r[5], r[6]))
    goodrows = [r for r in rows if r[0] not in lose and r[0] not in amb]
    run = lambda i: [int(m[0]) for m in re.finditer(r"\d{2,}", str(i)[:-2] if checked else str(i))][-1:]
    nxt = max([n for r in goodrows for n in run(r[1])] or [0]) + 1
    examples = [(r[2], r[4] or r[3], r[5]) for r in goodrows[:: max(1, len(goodrows) // 6)][:6]]
    context = ("Examples of items already in it with their prices: " + "; ".join(f"{n} ({a}) = {p}" for n, a, p in examples) + ". ") if examples else ""
    fixes, need, minted = [], [], {}
    for r in rows:
        if r[0] not in lose and r[0] not in amb:
            continue
        rid, pid, name, desc, attrs, price, _ = r
        s = _key(r)
        f = {"rowid": rid, "name": name, "attrs": attrs or desc, "old_id": pid, "old_price": price, "new_id": None, "new_price": None, "new_more": None, "how": "", "source": ""}
        if rid in amb and s not in good:
            f.update(how="ambiguous", source="two different parts share this id and nothing says which one is right")
        elif s in good:
            f.update(new_id=good[s][0], new_price=good[s][1], new_more=good[s][2], how="database", source="same part elsewhere in the database")
        elif s in minted:  # a second wrong row for a part that is not in the database: share the first one's new id and price
            f["twin"] = minted[s]
            f["new_id"] = minted[s]["new_id"]
        else:
            near = max(goodrows, key=lambda g: len(_words(s) & _words(_key(g))) / (len(_words(s) | _words(_key(g))) or 1), default=None)
            f["new_id"] = _mint(str(near[1]), nxt, checked, name, attrs or desc) if near else None
            if f["new_id"]:
                nxt += 1
            minted[s] = f
            need.append((f, (name, desc, attrs)))
        fixes.append(f)
    need, skipped = need[:MAX_WEB], need[MAX_WEB:]
    for f, _ in skipped:
        f.update(how="unresolved", source=f"over the {MAX_WEB} web lookups allowed per run")
    prices = web_prices([p for _, p in need], context) if web and need else [(None, "web lookup skipped")] * len(need)
    for (f, _), (price, src) in zip(need, prices):
        f.update(new_price=price, how="web" if price is not None else "unresolved", source=src)
    for f in fixes:
        if t := f.pop("twin", None):
            f.update(new_price=t["new_price"], new_more=t["new_more"], how=t["how"], source=t["source"])
    return {"table": table, "columns": cols, "mode": mode, "total": len(rows), "bad": len(fixes), "fixes": fixes}


def apply(path, fixes):
    table, cols, _ = _load(path)
    c = sqlite3.connect(path)
    with c:
        for f in fixes:
            if f["new_id"] and f["new_price"] is not None:
                more = f.get("new_more") or [None] * len(cols["more"])  # other price columns: from the database, else emptied (they were another part's)
                sets = "".join(f", {_q(m)}=?" for m in cols["more"])
                c.execute(f"UPDATE {_q(table)} SET {_q(cols['id'])}=?, {_q(cols['price'])}=?{sets} WHERE rowid=?", (f["new_id"], f["new_price"], *more, f["rowid"]))
    c.close()


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    db = args[0] if args else nlsql.DEFAULT_DB
    res = check_table(db)
    print(f"table {res['table']} {res['columns']}  (ids judged by {res['mode']})")
    for f in res["fixes"]:
        print(f"row {f['rowid']:>3} {f['name']} [{f['attrs']}]  {f['old_id']} {f['old_price']} -> {f['new_id']} {f['new_price']}  ({f['how']}: {f['source']})")
    n = lambda k: sum(f["how"] == k for f in res["fixes"])
    print(f"{res['bad']} of {res['total']} ids wrong; by database {n('database')}, by web {n('web')}, unresolved {n('unresolved')}, ambiguous {n('ambiguous')}")
    if "--apply" in sys.argv:
        apply(db, res["fixes"]); print("applied")
