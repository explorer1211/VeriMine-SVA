"""N3a storage: the assumption table (spec step 7) and the user's decisions.

n3a_run / n3a_assume are derived data keyed by the N3a input hash. n3a_decision is not: it holds
what the user decided (accept / reject) per design and candidate key, survives schema changes
of the other tables, and feeds the next run (a rejected candidate stays rejected).
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1
_DERIVED = ("n3a_meta", "n3a_run", "n3a_assume")
_DDL = """
CREATE TABLE IF NOT EXISTS n3a_meta (schema_version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS n3a_run (
    input_hash TEXT PRIMARY KEY, schema_version INTEGER NOT NULL, build_hash TEXT NOT NULL,
    n3_input_hash TEXT NOT NULL, n3a_hash TEXT NOT NULL, counts TEXT NOT NULL, strength TEXT NOT NULL,
    env_missing TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS n3a_assume (
    input_hash TEXT NOT NULL, id TEXT NOT NULL, scope TEXT NOT NULL, type TEXT NOT NULL, name TEXT NOT NULL,
    expr TEXT NOT NULL, source TEXT NOT NULL, confidence TEXT NOT NULL, status TEXT NOT NULL,
    reason TEXT NOT NULL, detail TEXT NOT NULL, PRIMARY KEY (input_hash, id)
);
CREATE TABLE IF NOT EXISTS n3a_decision (
    design TEXT NOT NULL, key TEXT NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL,
    created_at TEXT NOT NULL, PRIMARY KEY (design, key)
);
"""


def _j(x) -> str:
    return json.dumps(x, sort_keys=True, ensure_ascii=False)


def _ensure(con: sqlite3.Connection) -> None:
    have = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    version = None
    if "n3a_meta" in have:
        row = con.execute("SELECT schema_version FROM n3a_meta").fetchone()
        version = row[0] if row else None
    if have & set(_DERIVED) and version != SCHEMA_VERSION:
        for t in _DERIVED:
            con.execute(f"DROP TABLE IF EXISTS {t}")
    con.executescript(_DDL)
    if con.execute("SELECT count(*) FROM n3a_meta").fetchone()[0] == 0:
        con.execute("INSERT INTO n3a_meta VALUES (?)", (SCHEMA_VERSION,))


def load_decisions(db: Path, design: str) -> dict[str, dict]:
    if not db.exists():
        return {}
    with sqlite3.connect(db) as con:
        _ensure(con)
        rows = con.execute("SELECT key, status, reason FROM n3a_decision WHERE design=?", (design,)).fetchall()
    return {k: {"status": s, "reason": r} for k, s, r in rows}


def record_decisions(db: Path, design: str, items: list[tuple[str, str, str]]) -> None:
    if not items:
        return
    db.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(db) as con:
        _ensure(con)
        con.executemany("INSERT OR REPLACE INTO n3a_decision VALUES (?,?,?,?,?)",
                        [(design, k, s, r, now) for k, s, r in items])


def write_run(db: Path, res) -> dict:
    from verimine.n3a.pipeline import _cand_dict

    db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db) as con:
        _ensure(con)
        if con.execute("SELECT 1 FROM n3a_run WHERE input_hash=?", (res.input_hash,)).fetchone():
            return {"new_run": False}
        n0 = res.n3.n2.n1.n0
        con.execute("INSERT INTO n3a_run VALUES (?,?,?,?,?,?,?,?,?)", (
            res.input_hash, SCHEMA_VERSION, n0.hashes["build_hash"], res.n3.input_hash, res.n3a_hash,
            _j(res.counts()), _j(res.strength), _j(res.env_missing), datetime.now(timezone.utc).isoformat()))
        con.executemany("INSERT INTO n3a_assume VALUES (?,?,?,?,?,?,?,?,?,?,?)", [
            (res.input_hash, c.id, c.scope, c.type, c.name, c.text, c.source, c.confidence, c.status, c.reason,
             _j(_cand_dict(c))) for c in res.cands])
    return {"new_run": True}
