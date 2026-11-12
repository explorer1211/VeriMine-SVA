"""N4 storage: the candidate pool and the LLM audit log, keyed by the N4 input hash.

n4_run / n4_cand are derived data (dropped on a schema change). n4_llm_audit is not: it is the
record of what left the machine and what came back (spec "LLM 合规模式" 3), kept for export.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1
_DERIVED = ("n4_meta", "n4_run", "n4_cand")
_DDL = """
CREATE TABLE IF NOT EXISTS n4_meta (schema_version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS n4_run (
    input_hash TEXT PRIMARY KEY, schema_version INTEGER NOT NULL, build_hash TEXT NOT NULL,
    n3_input_hash TEXT NOT NULL, n4_hash TEXT NOT NULL, counts TEXT NOT NULL, paths TEXT NOT NULL,
    budget TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS n4_cand (
    input_hash TEXT NOT NULL, id TEXT NOT NULL, scope TEXT NOT NULL, type TEXT NOT NULL, text TEXT NOT NULL,
    sources TEXT NOT NULL, legality TEXT NOT NULL, assumes TEXT NOT NULL, level TEXT NOT NULL,
    status TEXT NOT NULL, ir TEXT NOT NULL, detail TEXT NOT NULL, PRIMARY KEY (input_hash, id)
);
CREATE TABLE IF NOT EXISTS n4_llm_audit (
    input_hash TEXT NOT NULL, run_input_hash TEXT NOT NULL, module TEXT NOT NULL, provider TEXT NOT NULL,
    model TEXT NOT NULL, level TEXT NOT NULL, output TEXT NOT NULL, error TEXT NOT NULL,
    adopted TEXT NOT NULL, created_at TEXT NOT NULL
);
"""


def _j(x) -> str:
    return json.dumps(x, sort_keys=True, ensure_ascii=False)


def _ensure(con: sqlite3.Connection) -> None:
    have = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    version = None
    if "n4_meta" in have:
        row = con.execute("SELECT schema_version FROM n4_meta").fetchone()
        version = row[0] if row else None
    if have & set(_DERIVED) and version != SCHEMA_VERSION:
        for t in _DERIVED:
            con.execute(f"DROP TABLE IF EXISTS {t}")
    con.executescript(_DDL)
    if con.execute("SELECT count(*) FROM n4_meta").fetchone()[0] == 0:
        con.execute("INSERT INTO n4_meta VALUES (?)", (SCHEMA_VERSION,))


def write_run(db: Path, res) -> dict:
    from verimine.n4.pipeline import cand_dict

    db.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(db) as con:
        _ensure(con)
        adopted = {c.key: c.id for c in res.cands if c.status == "POOL"}
        con.executemany("INSERT INTO n4_llm_audit VALUES (?,?,?,?,?,?,?,?,?,?)", [
            (a["input_hash"], res.input_hash, a["module"], a["provider"], a["model"], a["level"], a["output"],
             a["error"], _j(sorted(adopted[k] for k in a["candidates"] if k in adopted)), now) for a in res.audit])
        if con.execute("SELECT 1 FROM n4_run WHERE input_hash=?", (res.input_hash,)).fetchone():
            return {"new_run": False}
        n0 = res.n3.n2.n1.n0
        con.execute("INSERT INTO n4_run VALUES (?,?,?,?,?,?,?,?,?)", (
            res.input_hash, SCHEMA_VERSION, n0.hashes["build_hash"], res.n3.input_hash, res.n4_hash,
            _j(res.counts()), _j(res.paths), _j(res.budget), now))
        con.executemany("INSERT INTO n4_cand VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", [
            (res.input_hash, c.id, c.scope, c.type, c.text, _j(c.sources), c.legality, _j(c.assumes), c.level,
             c.status, _j(c.ir()), _j(cand_dict(c))) for c in res.cands])
    return {"new_run": True}


def export_audit(db: Path) -> list[dict]:
    if not db.exists():
        return []
    with sqlite3.connect(db) as con:
        _ensure(con)
        cols = [d[1] for d in con.execute("PRAGMA table_info(n4_llm_audit)")]
        return [dict(zip(cols, r)) for r in con.execute("SELECT * FROM n4_llm_audit ORDER BY created_at")]
