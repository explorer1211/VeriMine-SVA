"""N2b storage: SQLite tables next to N0-N2 in verimine.db, keyed by the N2b input hash (N2
input, build hash, tool environment, register map, N2b version and engine options). Derived
data like n1_* / n2_*: a schema version change drops and rebuilds the n2b_* tables.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1

_TABLES = ("n2b_meta", "n2b_run", "n2b_finding", "n2b_property")

_DDL = """
CREATE TABLE IF NOT EXISTS n2b_meta (schema_version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS n2b_run (
    input_hash TEXT PRIMARY KEY, schema_version INTEGER NOT NULL, build_hash TEXT NOT NULL,
    n2_input_hash TEXT NOT NULL, n2b_hash TEXT NOT NULL, engine TEXT NOT NULL, summary TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS n2b_finding (
    input_hash TEXT NOT NULL, seq INTEGER NOT NULL, check_id TEXT NOT NULL, klass TEXT NOT NULL,
    inst TEXT NOT NULL, severity TEXT NOT NULL, evidence TEXT NOT NULL, status TEXT NOT NULL,
    candidate_defect INTEGER NOT NULL, message TEXT NOT NULL, detail TEXT NOT NULL,
    PRIMARY KEY (input_hash, seq)
);
CREATE TABLE IF NOT EXISTS n2b_property (
    input_hash TEXT NOT NULL, id TEXT NOT NULL, kind TEXT NOT NULL, check_id TEXT NOT NULL,
    inst TEXT NOT NULL, status TEXT NOT NULL, step INTEGER, delivery TEXT NOT NULL,
    verification TEXT NOT NULL, skip TEXT NOT NULL, PRIMARY KEY (input_hash, id)
);
"""


def _j(x) -> str:
    return json.dumps(x, sort_keys=True, ensure_ascii=False)


def _ensure_schema(con: sqlite3.Connection) -> None:
    have = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    version = None
    if "n2b_meta" in have:
        row = con.execute("SELECT schema_version FROM n2b_meta").fetchone()
        version = row[0] if row else None
    if have & set(_TABLES) and version != SCHEMA_VERSION:
        for t in _TABLES:
            con.execute(f"DROP TABLE IF EXISTS {t}")
    con.executescript(_DDL)
    if con.execute("SELECT count(*) FROM n2b_meta").fetchone()[0] == 0:
        con.execute("INSERT INTO n2b_meta VALUES (?)", (SCHEMA_VERSION,))


def write_result(db_path: Path, input_hash: str, build_hash: str, n2_input_hash: str, n2b_hash: str,
                 res) -> dict:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as con:
        _ensure_schema(con)
        if con.execute("SELECT 1 FROM n2b_run WHERE input_hash=?", (input_hash,)).fetchone():
            return {"new_result": False}
        e = res.engine
        con.execute("INSERT INTO n2b_run VALUES (?,?,?,?,?,?,?,?)", (
            input_hash, SCHEMA_VERSION, build_hash, n2_input_hash, n2b_hash,
            _j({"available": e.available, "solver": e.solver, "depth": e.depth, "runs": e.runs}),
            _j(res.summary()), datetime.now(timezone.utc).isoformat()))
        con.executemany("INSERT INTO n2b_finding VALUES (?,?,?,?,?,?,?,?,?,?,?)", [
            (input_hash, i, f.check, f.klass, f.inst, f.severity, f.evidence, f.status, int(f.candidate_defect),
             f.message, _j({"signals": f.signals, "location": f.location, "props": f.props, **f.detail}))
            for i, f in enumerate(res.findings)
        ])
        con.executemany("INSERT INTO n2b_property VALUES (?,?,?,?,?,?,?,?,?,?)", [
            (input_hash, p.id, p.kind, p.check, p.inst, p.status, p.step, p.delivery, p.verification, p.skip)
            for p in res.props
        ])
    return {"new_result": True}
