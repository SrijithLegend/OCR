"""Find the rows of a parts table whose part_id is wrong and work out the right id and price.  CLI: python bikefix.py [db] [--apply]

For each row (table `parts`: part_id, part_name, part_description, part_attribute, part_price):
 1. Check the id against the row's own name + attributes (bikeparts.id_ok). Fine -> leave it alone.
 2. Wrong -> look the part up in the database by name + attributes among the rows whose id checks out. Found -> its id and price.
 3. Not in the database (the white seat when only a yellow one is listed) -> ask Gemini with Google Search for the going price
    and give the part a new id in the database's scheme (next free number, correct category and check characters).
Nothing is written unless you pass --apply. Sent to Gemini for step 3: only that part's name, description and attributes."""
import re, sqlite3, sys, time
from pathlib import Path

import nlsql  # loads .env, shares the Gemini client and the rate-limit settings
from bikeparts import CATS, id_ok, make_id, sig

BATCH = 8


def _rows(path):
    c = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        return c.execute("SELECT rowid, part_id, part_name, part_description, part_attribute, part_price FROM parts").fetchall()
    finally:
        c.close()


def web_prices(parts):
    """[(name, description, attributes)] -> [(price or None, source text)] from Google Search via Gemini. Never raises: a failure is returned as the source text."""
    from google.genai import types
    out = [(None, "")] * len(parts)
    for i in range(0, len(parts), BATCH):
        chunk = parts[i:i + BATCH]
        lines = "\n".join(f"{n}. {name} | {desc} | {attrs}" for n, (name, desc, attrs) in enumerate(chunk, 1))
        prompt = ("Royal Enfield Classic 350 motorcycle parts. For each part below, search the web for its typical current retail price in Indian rupees "
                  "(INR) for exactly that variant (colour, size, material as given). Reply with one line per part, nothing else, in the form\n"
                  "<number>|<price as a plain number>|<website name>\nIf you cannot find a price write <number>|none|none.\n\n" + lines)
        text = ""
        for attempt in range(3):
            try:
                r = nlsql._get_client().models.generate_content(model=nlsql.MODEL, contents=prompt, config=types.GenerateContentConfig(
                    temperature=0, tools=[types.Tool(google_search=types.GoogleSearch())]))
                text = r.text or ""
                break
            except Exception as e:
                msg = f"{type(e).__name__} {e}"
                m = re.search(r"retry in ([0-9.]+)s", msg)
                if ("429" in msg or "RESOURCE_EXHAUSTED" in msg) and attempt < 2 and (float(m[1]) if m else 20) <= nlsql.RATE_WAIT_MAX:
                    time.sleep((float(m[1]) if m else 20) + 1)
                    continue
                text = "ERROR " + msg[:200]
                break
        for n in range(1, len(chunk) + 1):
            m = re.search(rf"(?m)^\s*{n}\s*\|\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*\|\s*(.*)$", text)
            out[i + n - 1] = (float(m[1].replace(",", "")), m[2].strip() or "web") if m else (None, text[:120] if text.startswith("ERROR") else "no price found on the web")
    return out


def check(path, web=True):
    """-> {"total": n, "bad": n, "fixes": [{rowid, name, attrs, old_id, old_price, new_id, new_price, how, source}]}"""
    rows = _rows(path)
    good = {}
    for rid, pid, name, desc, attrs, price in rows:
        if id_ok(pid, name, attrs):
            good.setdefault(sig(name, attrs), (pid, price))
    used = [int(m[1]) for r in rows if (m := re.fullmatch(r"CL350-[A-Z]{3}-(\d{3})-[0-9A-F]{2}", str(r[1] or "")))]
    nxt, fixes, need, minted = max(used or [0]) + 1, [], [], {}
    for rid, pid, name, desc, attrs, price in rows:
        if id_ok(pid, name, attrs):
            continue
        s = sig(name, attrs)
        f = {"rowid": rid, "name": name, "attrs": attrs, "old_id": pid, "old_price": price, "new_id": None, "new_price": None, "how": "", "source": ""}
        if s in good:
            f.update(new_id=good[s][0], new_price=good[s][1], how="database", source="same part elsewhere in the database")
        elif s in minted:  # a second wrong row for a part that is not in the database: share the first one's new id and price
            f["twin"] = minted[s]
            f["new_id"] = minted[s]["new_id"]
        else:
            f["new_id"] = make_id(name, attrs, nxt)
            nxt += 1
            minted[s] = f
            need.append((f, (name, desc, attrs)))
        fixes.append(f)
    prices = web_prices([p for _, p in need]) if web and need else [(None, "web lookup skipped")] * len(need)
    for (f, _), (price, src) in zip(need, prices):
        f.update(new_price=price, how="web" if price is not None else "unresolved", source=src)
    for f in fixes:
        if t := f.pop("twin", None):
            f.update(new_price=t["new_price"], how=t["how"], source=t["source"])
    return {"total": len(rows), "bad": len(fixes), "fixes": fixes}


def apply(path, fixes):
    c = sqlite3.connect(path)
    with c:
        for f in fixes:
            if f["new_id"] and f["new_price"] is not None:
                c.execute("UPDATE parts SET part_id=?, part_price=? WHERE rowid=?", (f["new_id"], f["new_price"], f["rowid"]))
    c.close()


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    db = args[0] if args else "bike_parts.db"
    res = check(db)
    for f in res["fixes"]:
        print(f"row {f['rowid']:>3} {f['name']} [{f['attrs']}]  {f['old_id']} {f['old_price']} -> {f['new_id']} {f['new_price']}  ({f['how']}: {f['source']})")
    print(f"{res['bad']} of {res['total']} ids wrong; by database {sum(f['how'] == 'database' for f in res['fixes'])}, "
          f"by web {sum(f['how'] == 'web' for f in res['fixes'])}, unresolved {sum(f['how'] == 'unresolved' for f in res['fixes'])}")
    if "--apply" in sys.argv:
        apply(db, res["fixes"]); print("applied")
