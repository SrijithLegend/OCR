"""Ask any SQLite database a question in plain English.  CLI: python nlsql.py [--db file.db] ["question"]

Flow: read the schema from the file itself -> find words in the question that are real values in the data (locally)
-> an LLM (Gemini) writes ONE SELECT -> it runs sandboxed (read-only, SELECT-only authorizer, timeout, row cap)
-> you get the answer plus the SQL that produced it. Nothing about a particular database is hardcoded.

Sent to the Gemini API: your question, table/column names and types, short value lists for columns with few distinct
values, and values you typed that exist in the data. Never row data, and never the query results."""
import json, os, re, sqlite3, sys, threading, time
from pathlib import Path

_env = Path(__file__).with_name(".env")
if _env.exists():
    for _l in _env.read_text(encoding="utf-8").splitlines():
        if "=" in _l and not _l.lstrip().startswith("#"):
            _k, _v = _l.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip().strip("\"'"))

DEFAULT_DB = os.environ.get("SHOP_DB", "shop.db")
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")
MAX_ROWS = int(os.environ.get("NLSQL_MAX_ROWS", "500"))
MAX_VALUES = int(os.environ.get("NLSQL_MAX_VALUES", "25"))   # list the values of columns with at most this many distinct values
INDEX_LIMIT = 50_000                                         # text columns with more distinct values aren't value-matched
TIMEOUT_S = 15
RATE_WAIT_MAX = float(os.environ.get("NLSQL_RATE_WAIT", "70"))  # longest we will wait out a rate limit before telling the user

SYSTEM = """You write ONE SQLite SELECT query that answers the user's question about the database described below.
Reply as JSON: {"sql": "...", "note": "..."}.
- note: one short plain sentence saying exactly what the result means and any assumption you made (for example
  "Counts distinct WorkspaceId" or "Counts rows where OfficeId equals that office").
- One statement only: SELECT, or WITH ... SELECT. SQLite syntax. Double-quote identifiers that need it.
- Use only the tables and columns listed. Never invent a column or table.
- 'VALUE HINTS' lists values from the question that really exist in the data and the column they belong to: filter on
  them exactly as written there.
- 'How many <things>' where the things have an id/key column: COUNT(DISTINCT that key). If the question asks for rows,
  records or entries, or how many times a specific value occurs: COUNT(*).
- 'per', 'by', 'each' -> GROUP BY with one row per group and NO LIMIT. Words like highest/lowest/total/average then apply
  inside each group (MAX / MIN / SUM / AVG per group). Order the groups by that measure DESC.
- 'Which X has the most/least/highest/lowest ...' -> return the group AND its measure value, ORDER BY the measure,
  LIMIT 5 (the first row is the answer, the rest give context). Never let a NULL/empty group be the winner.
- Sums, averages, min and max ignore NULLs; say in the note if many rows have no value.
- Listing questions: include identifying columns, and LIMIT 200 unless the user asks for all.
- Match text case-insensitively (lower() or LIKE) unless you are using an exact hint value.
- If the database cannot answer the question, return {"sql": "", "note": "<why, and what the data does contain>"}."""

SCHEMA_JSON = {"type": "object", "properties": {"sql": {"type": "string"}, "note": {"type": "string"}}, "required": ["sql", "note"]}
_client, _lock, _cache = None, threading.Lock(), {}


def _q(name):  # quote an identifier
    return '"' + str(name).replace('"', '""') + '"'


def _connect(path, sandbox=True):
    """Read-only connection. With sandbox=True (generated SQL) it can only run SELECTs and gives up after TIMEOUT_S seconds."""
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    conn.execute("PRAGMA trusted_schema=OFF")  # the file may be untrusted: don't run schema-defined functions
    if not sandbox:
        t1 = time.time()
        conn.set_progress_handler(lambda: 1 if time.time() - t1 > 60 else 0, 20_000)
        return conn
    ok = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}
    conn.set_authorizer(lambda action, *_: sqlite3.SQLITE_OK if action in ok else sqlite3.SQLITE_DENY)
    t0 = time.time()
    conn.set_progress_handler(lambda: 1 if time.time() - t0 > TIMEOUT_S else 0, 20_000)
    return conn


def _signature(path):
    p = Path(path)
    return (str(p.resolve()), p.stat().st_mtime_ns, p.stat().st_size)


def describe(path):
    """-> (schema text for the prompt, {lowercased value: (table, column, value)} for local value matching). Cached per file version."""
    sig = _signature(path)
    with _lock:
        if sig in _cache:
            return _cache[sig]
    conn = _connect(path, sandbox=False)  # our own introspection queries (PRAGMA etc.), never model-written SQL
    lines, index = [], {}
    try:
        objs = conn.execute("SELECT name, type FROM sqlite_master WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
        for tname, ttype in objs:
            n = conn.execute(f"SELECT COUNT(*) FROM {_q(tname)}").fetchone()[0]
            lines.append(f"{ttype.upper()} {_q(tname)} ({n:,} rows)")
            for _, col, ctype, notnull, _, pk in conn.execute(f"PRAGMA table_info({_q(tname)})").fetchall():
                ctype = (ctype or "").upper()
                try:
                    nd, nn, lo, hi = conn.execute(
                        f"SELECT COUNT(DISTINCT {_q(col)}), COUNT({_q(col)}), MIN({_q(col)}), MAX({_q(col)}) "
                        f"FROM (SELECT {_q(col)} FROM {_q(tname)} LIMIT 200000)").fetchone()
                except sqlite3.Error:
                    lines.append(f"  {_q(col)} {ctype}"); continue
                if isinstance(lo, bytes):
                    lines.append(f"  {_q(col)} {ctype} [binary]"); continue
                desc = f"  {_q(col)} {ctype}{' PRIMARY KEY' if pk else ''} [{nd:,} distinct, {n - nn:,} empty]"
                if nd and isinstance(lo, (int, float)) or (nd and isinstance(lo, str) and nd > MAX_VALUES):
                    desc += f" range {str(lo)[:40]!r}..{str(hi)[:40]!r}"
                if 0 < nd <= MAX_VALUES:
                    vals = [r[0] for r in conn.execute(f"SELECT {_q(col)} FROM {_q(tname)} WHERE {_q(col)} IS NOT NULL GROUP BY 1 ORDER BY COUNT(*) DESC LIMIT {MAX_VALUES}")]
                    desc += " values: " + ", ".join(repr(str(v)[:50]) if isinstance(v, str) else str(v) for v in vals)
                lines.append(desc)
                if isinstance(lo, str) and 0 < nd <= INDEX_LIMIT:  # text values you might type: matched locally against the question
                    for (v,) in conn.execute(f"SELECT DISTINCT {_q(col)} FROM {_q(tname)} WHERE {_q(col)} IS NOT NULL LIMIT {INDEX_LIMIT}"):
                        if isinstance(v, str) and len(v.strip()) >= 3:
                            index.setdefault(v.strip().lower(), (tname, col, v))
            for fk in conn.execute(f"PRAGMA foreign_key_list({_q(tname)})").fetchall():
                lines.append(f"  FOREIGN KEY {_q(fk[3])} -> {_q(fk[2])}.{_q(fk[4])}")
    finally:
        conn.close()
    out = ("\n".join(lines), index)
    with _lock:
        _cache.clear(); _cache[sig] = out
    return out


def overview(path):
    """-> [{"table", "rows", "columns"}] for the UI. Raises sqlite3.Error if the file isn't a readable SQLite database."""
    conn = _connect(path, sandbox=False)
    try:
        out = []
        for (t,) in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall():
            out.append({"table": t, "rows": conn.execute(f"SELECT COUNT(*) FROM {_q(t)}").fetchone()[0],
                        "columns": len(conn.execute(f"PRAGMA table_info({_q(t)})").fetchall())})
        return out
    finally:
        conn.close()


def hints(question, index):
    """Values the user typed that exist in the data (longest first, no overlaps) -> lines for the prompt. Local only."""
    q, taken, found = question.lower(), [], []
    for key in sorted(index, key=len, reverse=True):
        if len(key) > len(q) or any(key in t for t in taken):
            continue
        if re.search(r"(?<!\w)" + re.escape(key) + r"(?!\w)", q):
            taken.append(key); found.append(index[key])
            if len(found) >= 15:
                break
    return "\n".join(f"{_q(t)}.{_q(c)} has the value {v!r}" for t, c, v in found)


def _get_client():
    """One shared client, created exactly once (two threads each making one and dropping the other closed it mid-request)."""
    global _client
    with _lock:
        if _client is None:
            from google import genai
            from google.genai import types
            if not os.environ.get("GENAI_API_KEY"):
                raise RuntimeError("GENAI_API_KEY is not set (put it in .env)")
            _client = genai.Client(api_key=os.environ["GENAI_API_KEY"], http_options=types.HttpOptions(timeout=60_000))
        return _client


def _model(prompt):
    from google.genai import types
    client = _get_client()
    for attempt in range(4):
        try:
            r = client.models.generate_content(model=MODEL, contents=prompt, config=types.GenerateContentConfig(
                system_instruction=SYSTEM, temperature=0, response_mime_type="application/json", response_json_schema=SCHEMA_JSON))
            if not r.text:
                raise RuntimeError("The model returned nothing (blocked or truncated).")
            return json.loads(r.text)
        except Exception as e:
            msg = f"{type(e).__name__} {e}"
            if "429" in msg or "RESOURCE_EXHAUSTED" in msg:  # rate limit: wait as long as the API asks, if that is reasonable
                m = re.search(r"retry in ([0-9.]+)s", msg) or re.search(r"retryDelay.: .([0-9.]+)s", msg)
                wait = float(m.group(1)) + 1 if m else 20
                if wait > RATE_WAIT_MAX or attempt == 3:
                    raise RuntimeError(f"Gemini rate limit reached (free tier allows about 15 requests per minute). Try again in {int(wait)} seconds.")
                time.sleep(wait)
                continue
            if attempt == 3 or not any(c in msg for c in ("503", "500", "disconnected", "timed out", "SSL", "EOF", "ConnectError", "Timeout", "reset")):
                raise
            time.sleep(3 * (attempt + 1))


def run_sql(path, sql):
    """-> (columns, rows, truncated). Raises sqlite3.Error / ValueError on anything that isn't a plain read-only SELECT."""
    sql = sql.strip().rstrip(";").strip()
    if not re.match(r"(?is)^\s*(select|with)\b", sql):
        raise ValueError("Only SELECT queries are allowed")
    conn = _connect(path)
    try:
        cur = conn.execute(sql)  # a second statement raises; the authorizer blocks every write, PRAGMA and ATTACH
        rows = cur.fetchmany(MAX_ROWS + 1)
        cols = [d[0] for d in cur.description or []]
    finally:
        conn.close()
    clean = lambda v: "<binary>" if isinstance(v, bytes) else v
    return cols, [[clean(v) for v in r] for r in rows[:MAX_ROWS]], len(rows) > MAX_ROWS


def ask(question, db=None):
    """-> {"kind": "scalar"|"table"|"empty"|"error", "text": note, "sql": str, "columns": [...], "rows": [[...]], "truncated": bool}"""
    question, path = (question or "").strip()[:500], db or DEFAULT_DB
    err = lambda m, sql="": {"kind": "error", "text": m, "sql": sql, "columns": [], "rows": [], "truncated": False}
    if not question:
        return err("Type a question first")
    if not Path(path).is_file():
        return err(f"Database file not found: {path}")
    try:
        schema, index = describe(path)
    except sqlite3.Error as e:
        return err(f"Could not read that database: {e}")
    prompt = f"DATABASE SCHEMA\n{schema}\n\n" + (f"VALUE HINTS\n{hints(question, index)}\n\n" if hints(question, index) else "") + f"QUESTION\n{question}"
    sql, note, last = "", "", None
    try:
        for attempt in range(2):  # one retry, feeding the database error back to the model
            out = _model(prompt if not last else prompt + f"\n\nYour previous SQL failed.\nSQL: {sql}\nError: {last}\nReturn a corrected query.")
            sql, note = (out.get("sql") or "").strip(), (out.get("note") or "").strip()
            if not sql:
                return {"kind": "empty", "text": note or "That can't be answered from this database.", "sql": "", "columns": [], "rows": [], "truncated": False}
            try:
                cols, rows, trunc = run_sql(path, sql)
                break
            except (sqlite3.Error, ValueError) as e:
                last = str(e)
        else:
            return err(f"The generated query failed: {last}", sql)
    except Exception as e:  # network / key / quota
        return err(f"Could not get an answer: {e}")
    kind = "scalar" if len(rows) == 1 and len(cols) == 1 else "table" if rows else "empty"
    return {"kind": kind, "text": note if rows else (note + " No rows matched.").strip(), "sql": sql, "columns": cols, "rows": rows, "truncated": trunc}


if __name__ == "__main__":
    args = sys.argv[1:]
    db = None
    if args[:1] == ["--db"]:
        db, args = args[1], args[2:]

    def show(r):
        print(r["text"])
        if r["sql"]: print("SQL:", r["sql"])
        if r["columns"]: print(" | ".join(r["columns"]))
        for row in r["rows"][:30]: print("  ", " | ".join(str(c) for c in row))
        if len(r["rows"]) > 30: print(f"   ... {len(r['rows'])} rows")
    if args:
        show(ask(" ".join(args), db))
    else:
        print("Ask a question about the database (blank line to quit).")
        while (q := input("> ").strip()):
            show(ask(q, db)); print()
