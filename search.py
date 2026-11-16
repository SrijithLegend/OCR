"""Kept so `python search.py ...` still works: the engine is nlsql.py (any SQLite file, LLM-written read-only SQL)."""
from nlsql import ask as search  # noqa: F401  (web.py and older callers import search.search)

if __name__ == "__main__":
    import runpy
    runpy.run_module("nlsql", run_name="__main__")
