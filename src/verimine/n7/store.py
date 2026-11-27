"""N7 storage: proof and coverage results keyed by the N7 input hash (derived data)."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1
_TABLES = ("n7_meta", "n7_run", "n7_prop")
_DDL = """
CREATE TABLE IF NOT EXISTS n7_meta (schema_version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS n7_run (
    input_hash TEXT PRIMARY KEY, schema_version INTEGER NOT NULL, build_hash TEXT NOT NULL,
    n6_input_hash TEXT NOT NULL, n7_hash TEXT NOT NULL, counts TEXT NOT NULL, coverage TEXT NOT NULL,
    gaps TEXT NOT NULL, rounds TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS n7_prop (
    input_hash TEXT NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL,
    sim_status TEXT NOT NULL, formal_status TEXT NOT NULL, cover TEXT NOT NULL, detail TEXT NOT NULL,
    PRIMARY KEY (input_hash, name)
);
"""


def _j(x) -> str:
    return json.dumps(x, sort_keys=True, ensure_ascii=False, default=str)


def _ensure(con: sqlite3.Connection) -> None:
    have = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    version = None
    if "n7_meta" in have:
        row = con.execute("SELECT schema_version FROM n7_meta").fetchone()
        version = row[0] if row else None
    if have & set(_TABLES) and version != SCHEMA_VERSION:
        for t in _TABLES:
            con.execute(f"DROP TABLE IF EXISTS {t}")
    con.executescript(_DDL)
    if con.execute("SELECT count(*) FROM n7_meta").fetchone()[0] == 0:
        con.execute("INSERT INTO n7_meta VALUES (?)", (SCHEMA_VERSION,))


def write_run(db: Path, res) -> dict:
    db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db) as con:
        _ensure(con)
        if con.execute("SELECT 1 FROM n7_run WHERE input_hash=?", (res.input_hash,)).fetchone():
            return {"new_run": False}
        con.execute("INSERT INTO n7_run VALUES (?,?,?,?,?,?,?,?,?,?)", (
            res.input_hash, SCHEMA_VERSION, res.n6.n5.n4.n3.n2.n1.n0.hashes["build_hash"], res.n6.input_hash,
            res.n7_hash, _j(res.counts()), _j(res.coverage), _j(res.gaps), _j(res.rounds),
            datetime.now(timezone.utc).isoformat()))
        con.executemany("INSERT INTO n7_prop VALUES (?,?,?,?,?,?,?,?)", [
            (res.input_hash, p.name, p.kind, p.status or "-", p.sim.get("status", "-"),
             p.formal.get("status", "-"), p.cover, _j(p.to_dict()))
            for p in res.props])
    return {"new_run": True}
