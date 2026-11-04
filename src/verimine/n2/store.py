"""N2 storage: SQLite tables next to N0/N1 in verimine.db, keyed by the N2 input hash.

The input hash covers everything N2 reads (N1 index_hash, configured clocks/resets, N2 model
version), so a rerun with the same inputs writes nothing new. Like N1, the n2_* tables are
derived data: on a schema version change they are dropped and rebuilt.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from pathlib import Path

SCHEMA_VERSION = 1

_TABLES = ("n2_meta", "n2_model", "n2_node", "n2_edge", "n2_role", "n2_fsm", "n2_pattern",
           "n2_event", "n2_coi")

_DDL = """
CREATE TABLE IF NOT EXISTS n2_meta (schema_version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS n2_model (
    input_hash TEXT PRIMARY KEY, schema_version INTEGER NOT NULL, build_hash TEXT NOT NULL,
    index_hash TEXT NOT NULL, n2_hash TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS n2_node (
    input_hash TEXT NOT NULL, id TEXT NOT NULL, inst TEXT NOT NULL, name TEXT NOT NULL,
    kind TEXT NOT NULL, width INTEGER NOT NULL, level TEXT NOT NULL, top_port INTEGER NOT NULL,
    pagerank REAL NOT NULL, PRIMARY KEY (input_hash, id)
);
CREATE TABLE IF NOT EXISTS n2_edge (
    input_hash TEXT NOT NULL, src TEXT NOT NULL, dst TEXT NOT NULL, kind TEXT NOT NULL,
    delay INTEGER NOT NULL, via TEXT NOT NULL, reset INTEGER NOT NULL,
    PRIMARY KEY (input_hash, src, dst, kind, delay, via)
);
CREATE INDEX IF NOT EXISTS n2_edge_dst ON n2_edge (input_hash, dst);
CREATE TABLE IF NOT EXISTS n2_role (
    input_hash TEXT NOT NULL, node TEXT NOT NULL, role TEXT NOT NULL, evidence_kind TEXT NOT NULL,
    confidence TEXT NOT NULL, evidence TEXT NOT NULL, PRIMARY KEY (input_hash, node, role, evidence_kind)
);
CREATE TABLE IF NOT EXISTS n2_fsm (
    input_hash TEXT NOT NULL, inst TEXT NOT NULL, state TEXT NOT NULL, detail TEXT NOT NULL,
    PRIMARY KEY (input_hash, inst, state)
);
CREATE TABLE IF NOT EXISTS n2_pattern (
    input_hash TEXT NOT NULL, kind TEXT NOT NULL, inst TEXT NOT NULL, signal TEXT NOT NULL,
    confidence TEXT NOT NULL, detail TEXT NOT NULL, PRIMARY KEY (input_hash, kind, inst, signal)
);
CREATE TABLE IF NOT EXISTS n2_event (
    input_hash TEXT NOT NULL, seq INTEGER NOT NULL, kind TEXT NOT NULL, inst TEXT NOT NULL,
    confidence TEXT NOT NULL, detail TEXT NOT NULL, PRIMARY KEY (input_hash, seq)
);
CREATE TABLE IF NOT EXISTS n2_coi (
    input_hash TEXT NOT NULL, target TEXT NOT NULL, size INTEGER NOT NULL,
    sequential_depth INTEGER NOT NULL, fingerprint TEXT NOT NULL, nodes TEXT NOT NULL,
    PRIMARY KEY (input_hash, target)
);
"""


def _j(x) -> str:
    return json.dumps(x, sort_keys=True, ensure_ascii=False)


def _ensure_schema(con: sqlite3.Connection) -> None:
    have = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    version = None
    if "n2_meta" in have:
        row = con.execute("SELECT schema_version FROM n2_meta").fetchone()
        version = row[0] if row else None
    if have & set(_TABLES) and version != SCHEMA_VERSION:
        for t in _TABLES:
            con.execute(f"DROP TABLE IF EXISTS {t}")
    con.executescript(_DDL)
    if con.execute("SELECT count(*) FROM n2_meta").fetchone()[0] == 0:
        con.execute("INSERT INTO n2_meta VALUES (?)", (SCHEMA_VERSION,))


def write_model(db_path: Path, input_hash: str, build_hash: str, index_hash: str, n2_hash: str,
                model) -> dict:
    from datetime import datetime, timezone

    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as con:
        _ensure_schema(con)
        if con.execute("SELECT 1 FROM n2_model WHERE input_hash=?", (input_hash,)).fetchone():
            return {"new_model": False}
        con.execute("INSERT INTO n2_model VALUES (?,?,?,?,?,?)",
                    (input_hash, SCHEMA_VERSION, build_hash, index_hash, n2_hash,
                     datetime.now(timezone.utc).isoformat()))
        g = model.vdg
        con.executemany("INSERT OR IGNORE INTO n2_node VALUES (?,?,?,?,?,?,?,?,?)", [
            (input_hash, n.id, n.inst, n.name, n.kind, n.width, n.level, int(n.top_port),
             model.pagerank.get(n.id, 0.0))
            for n in g.nodes.values()
        ])
        con.executemany("INSERT OR IGNORE INTO n2_edge VALUES (?,?,?,?,?,?,?)", [
            (input_hash, e.src, e.dst, e.kind, e.delay, e.via, int(e.reset)) for e in g.edges
        ])
        con.executemany("INSERT OR IGNORE INTO n2_role VALUES (?,?,?,?,?,?)", [
            (input_hash, sr.node, r.role, r.evidence_kind, r.confidence, _j(r.evidence))
            for sr in model.roles for r in sr.roles
        ])
        con.executemany("INSERT OR IGNORE INTO n2_fsm VALUES (?,?,?,?)", [
            (input_hash, f.inst, f.state, _j(asdict(f))) for f in model.fsms
        ])
        con.executemany("INSERT OR IGNORE INTO n2_pattern VALUES (?,?,?,?,?,?)", [
            (input_hash, p.kind, p.inst, p.signal, p.confidence, _j(asdict(p))) for p in model.patterns
        ])
        con.executemany("INSERT OR IGNORE INTO n2_event VALUES (?,?,?,?,?,?)", [
            (input_hash, i, e.kind, e.inst, e.confidence, _j(asdict(e))) for i, e in enumerate(model.events)
        ])
        con.executemany("INSERT OR IGNORE INTO n2_coi VALUES (?,?,?,?,?,?)", [
            (input_hash, c.target, len(c.nodes), c.sequential_depth, c.fingerprint, _j(c.nodes))
            for c in model.cois
        ])
    return {"new_model": True}
