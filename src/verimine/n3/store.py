"""N3 storage: SQLite tables next to N0-N2b in verimine.db, keyed by the N3 input hash; the
sampled tables themselves are content-addressed files, traces/<case hash>.json.gz (a case that
shows up in two runs is stored once). Derived data: a schema change drops the n3_* tables.
"""
from __future__ import annotations

import gzip
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from verimine.n3.sample import Case

SCHEMA_VERSION = 1
_TABLES = ("n3_meta", "n3_run", "n3_case")
_DDL = """
CREATE TABLE IF NOT EXISTS n3_meta (schema_version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS n3_run (
    input_hash TEXT PRIMARY KEY, schema_version INTEGER NOT NULL, build_hash TEXT NOT NULL,
    n2_input_hash TEXT NOT NULL, traces_hash TEXT NOT NULL, sources TEXT NOT NULL, explore TEXT NOT NULL,
    coverage TEXT NOT NULL, quality TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS n3_case (
    input_hash TEXT NOT NULL, seq INTEGER NOT NULL, case_id TEXT NOT NULL, case_hash TEXT NOT NULL,
    kind TEXT NOT NULL, source TEXT NOT NULL, legality TEXT NOT NULL, split TEXT NOT NULL,
    cycles INTEGER NOT NULL, detail TEXT NOT NULL, PRIMARY KEY (input_hash, seq)
);
CREATE INDEX IF NOT EXISTS n3_case_split ON n3_case (input_hash, split);
"""


def _j(x) -> str:
    return json.dumps(x, sort_keys=True, ensure_ascii=False, default=list)


def _ensure_schema(con: sqlite3.Connection) -> None:
    have = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    version = None
    if "n3_meta" in have:
        row = con.execute("SELECT schema_version FROM n3_meta").fetchone()
        version = row[0] if row else None
    if have & set(_TABLES) and version != SCHEMA_VERSION:
        for t in _TABLES:
            con.execute(f"DROP TABLE IF EXISTS {t}")
    con.executescript(_DDL)
    if con.execute("SELECT count(*) FROM n3_meta").fetchone()[0] == 0:
        con.execute("INSERT INTO n3_meta VALUES (?)", (SCHEMA_VERSION,))


def write_run(db_path: Path, res) -> dict:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    n0 = res.n2.n1.n0
    with sqlite3.connect(db_path) as con:
        _ensure_schema(con)
        if con.execute("SELECT 1 FROM n3_run WHERE input_hash=?", (res.input_hash,)).fetchone():
            return {"new_run": False}
        con.execute("INSERT INTO n3_run VALUES (?,?,?,?,?,?,?,?,?,?)", (
            res.input_hash, SCHEMA_VERSION, n0.hashes["build_hash"], res.n2.input_hash, res.traces_hash,
            _j(res.sources), _j(res.explore), _j(res.coverage.summary()), _j(res.quality),
            datetime.now(timezone.utc).isoformat()))
        con.executemany("INSERT INTO n3_case VALUES (?,?,?,?,?,?,?,?,?,?)", [
            (res.input_hash, i, c.id, c.hash, c.kind, c.source, c.legality, c.set, c.cycles, _j(c.detail))
            for i, c in enumerate(res.cases)
        ])
    return {"new_run": True}


def load_case(tdir: Path, case_hash: str) -> dict | None:
    f = tdir / f"{case_hash}.json.gz"
    if not f.exists():
        return None
    return json.loads(gzip.decompress(f.read_bytes()))


def load_run(db_path: Path, tdir: Path, input_hash: str):
    """(cases, sources, explore) of a stored run, or None if it is not there in full."""
    if not db_path.exists():
        return None
    with sqlite3.connect(db_path) as con:
        have = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "n3_run" not in have:
            return None
        meta = con.execute("SELECT schema_version FROM n3_meta").fetchone()
        if not meta or meta[0] != SCHEMA_VERSION:
            return None
        row = con.execute("SELECT sources, explore FROM n3_run WHERE input_hash=?", (input_hash,)).fetchone()
        if row is None:
            return None
        rows = con.execute("SELECT case_id, case_hash, kind, source, legality, split, cycles, detail FROM n3_case "
                           "WHERE input_hash=? ORDER BY seq", (input_hash,)).fetchall()
    cases = []
    for cid, h, kind, source, legality, split, cycles, detail in rows:
        data = load_case(tdir, h)
        if data is None:
            return None
        cases.append(Case(cid, source, kind, legality, cycles, data["signals"], h, split, json.loads(detail)))
    return cases, json.loads(row[0]), json.loads(row[1])
