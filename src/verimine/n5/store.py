"""N5 storage: rendered properties keyed by the N5 input hash (derived data)."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1
_TABLES = ("n5_meta", "n5_run", "n5_prop")
_DDL = """
CREATE TABLE IF NOT EXISTS n5_meta (schema_version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS n5_run (
    input_hash TEXT PRIMARY KEY, schema_version INTEGER NOT NULL, build_hash TEXT NOT NULL,
    n4_input_hash TEXT NOT NULL, n5_hash TEXT NOT NULL, counts TEXT NOT NULL, degradations TEXT NOT NULL,
    diff TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS n5_prop (
    input_hash TEXT NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL, cand TEXT NOT NULL, scope TEXT NOT NULL,
    delivery TEXT NOT NULL, verification TEXT NOT NULL, degraded TEXT NOT NULL, diff_status TEXT NOT NULL,
    detail TEXT NOT NULL, PRIMARY KEY (input_hash, name)
);
"""


def _j(x) -> str:
    return json.dumps(x, sort_keys=True, ensure_ascii=False, default=str)


def _ensure(con: sqlite3.Connection) -> None:
    have = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    version = None
    if "n5_meta" in have:
        row = con.execute("SELECT schema_version FROM n5_meta").fetchone()
        version = row[0] if row else None
    if have & set(_TABLES) and version != SCHEMA_VERSION:
        for t in _TABLES:
            con.execute(f"DROP TABLE IF EXISTS {t}")
    con.executescript(_DDL)
    if con.execute("SELECT count(*) FROM n5_meta").fetchone()[0] == 0:
        con.execute("INSERT INTO n5_meta VALUES (?)", (SCHEMA_VERSION,))


def write_run(db: Path, res) -> dict:
    from verimine.n5.pipeline import prop_dict

    db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db) as con:
        _ensure(con)
        if con.execute("SELECT 1 FROM n5_run WHERE input_hash=?", (res.input_hash,)).fetchone():
            return {"new_run": False}
        con.execute("INSERT INTO n5_run VALUES (?,?,?,?,?,?,?,?,?)", (
            res.input_hash, SCHEMA_VERSION, res.n4.n3.n2.n1.n0.hashes["build_hash"], res.n4.input_hash,
            res.n5_hash, _j(res.counts()), _j(res.degradations()), _j(res.diff),
            datetime.now(timezone.utc).isoformat()))
        con.executemany("INSERT INTO n5_prop VALUES (?,?,?,?,?,?,?,?,?,?)", [
            (res.input_hash, r.name, r.kind, r.cand.id, r.cand.scope, r.delivery, r.verification, _j(r.degraded),
             r.diff.get("status", "not_run"), _j(prop_dict(r))) for r in res.props])
    return {"new_run": True}
