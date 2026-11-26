"""N6 storage: compile-check results keyed by the N6 input hash (derived data)."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1
_TABLES = ("n6_meta", "n6_run", "n6_prop")
_DDL = """
CREATE TABLE IF NOT EXISTS n6_meta (schema_version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS n6_run (
    input_hash TEXT PRIMARY KEY, schema_version INTEGER NOT NULL, build_hash TEXT NOT NULL,
    n5_input_hash TEXT NOT NULL, n6_hash TEXT NOT NULL, counts TEXT NOT NULL, legs TEXT NOT NULL,
    rounds TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS n6_prop (
    input_hash TEXT NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL,
    to_n7 INTEGER NOT NULL, delivery_variant INTEGER NOT NULL, verification_variant INTEGER NOT NULL,
    detail TEXT NOT NULL, PRIMARY KEY (input_hash, name)
);
"""


def _j(x) -> str:
    return json.dumps(x, sort_keys=True, ensure_ascii=False, default=str)


def _ensure(con: sqlite3.Connection) -> None:
    have = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    version = None
    if "n6_meta" in have:
        row = con.execute("SELECT schema_version FROM n6_meta").fetchone()
        version = row[0] if row else None
    if have & set(_TABLES) and version != SCHEMA_VERSION:
        for t in _TABLES:
            con.execute(f"DROP TABLE IF EXISTS {t}")
    con.executescript(_DDL)
    if con.execute("SELECT count(*) FROM n6_meta").fetchone()[0] == 0:
        con.execute("INSERT INTO n6_meta VALUES (?)", (SCHEMA_VERSION,))


def write_run(db: Path, res) -> dict:
    from verimine.n6.pipeline import prop_dict

    db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db) as con:
        _ensure(con)
        if con.execute("SELECT 1 FROM n6_run WHERE input_hash=?", (res.input_hash,)).fetchone():
            return {"new_run": False}
        con.execute("INSERT INTO n6_run VALUES (?,?,?,?,?,?,?,?,?)", (
            res.input_hash, SCHEMA_VERSION, res.n5.n4.n3.n2.n1.n0.hashes["build_hash"], res.n5.input_hash,
            res.n6_hash, _j(res.counts()), _j(res.legs), _j(res.rounds), datetime.now(timezone.utc).isoformat()))
        con.executemany("INSERT INTO n6_prop VALUES (?,?,?,?,?,?,?,?)", [
            (res.input_hash, p.name, p.kind, p.status, int(p.to_n7), p.d_variant, p.v_variant, _j(prop_dict(p)))
            for p in res.props])
    return {"new_run": True}
