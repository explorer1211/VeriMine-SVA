"""
N1 SQLite storage and JSON export.

Schema (separate from N0's verimine.db for clean separation):
  - n1_index: one row per N0 build_hash — stores the index hash and status
  - n1_instance: one row per instance path
  - n1_port: ports per instance
  - n1_signal: signals per instance
  - n1_proc: procedural blocks per instance
  - n1_driver: driver relationships per instance
  - n1_module_status: module status judgments
  - n1_fingerprint: instance path -> fingerprint
  - n1_assign: assignments (lhs, rhs expression tree, control-condition stack, blocking)
  - n1_connection: how each instance's ports are connected in its parent

All writes are idempotent (INSERT OR IGNORE on primary keys). The index is derived data, so
when the stored schema version differs from SCHEMA_VERSION the n1_* tables are dropped and
rebuilt instead of migrated.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .elaborate import InstanceInfo, ElaborationResult
from .status import ModuleStatus


SCHEMA_VERSION = 2

_TABLES = ("n1_index", "n1_instance", "n1_port", "n1_signal", "n1_proc", "n1_driver",
           "n1_module_status", "n1_fingerprint", "n1_assign", "n1_connection", "n1_meta")

_DDL = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS n1_meta (
    schema_version  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS n1_index (
    build_hash      TEXT PRIMARY KEY,
    schema_version  INTEGER NOT NULL,
    index_hash      TEXT NOT NULL,
    top_path        TEXT NOT NULL,
    is_incomplete   INTEGER NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS n1_instance (
    build_hash  TEXT NOT NULL,
    path        TEXT NOT NULL,
    module      TEXT NOT NULL,
    parent      TEXT,
    params_json TEXT NOT NULL,
    clocks_json TEXT NOT NULL,
    is_incomplete INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (build_hash, path)
);

CREATE TABLE IF NOT EXISTS n1_port (
    build_hash  TEXT NOT NULL,
    inst_path   TEXT NOT NULL,
    name        TEXT NOT NULL,
    direction   TEXT NOT NULL,
    type_str    TEXT NOT NULL,
    is_interface INTEGER NOT NULL DEFAULT 0,
    width       INTEGER NOT NULL,
    signed      INTEGER NOT NULL,
    PRIMARY KEY (build_hash, inst_path, name)
);

CREATE TABLE IF NOT EXISTS n1_signal (
    build_hash  TEXT NOT NULL,
    inst_path   TEXT NOT NULL,
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL,
    type_str    TEXT NOT NULL,
    location    TEXT NOT NULL,
    width       INTEGER NOT NULL,
    signed      INTEGER NOT NULL,
    array_dims  TEXT NOT NULL,
    scope       TEXT NOT NULL,
    PRIMARY KEY (build_hash, inst_path, name)
);

CREATE TABLE IF NOT EXISTS n1_proc (
    build_hash   TEXT NOT NULL,
    inst_path    TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    kind         TEXT NOT NULL,
    sensitivity  TEXT NOT NULL,
    location     TEXT NOT NULL,
    scope        TEXT NOT NULL,
    PRIMARY KEY (build_hash, inst_path, seq)
);

CREATE TABLE IF NOT EXISTS n1_driver (
    build_hash   TEXT NOT NULL,
    inst_path    TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    signal       TEXT NOT NULL,
    driver_kind  TEXT NOT NULL,
    proc_kind    TEXT,
    location     TEXT NOT NULL,
    PRIMARY KEY (build_hash, inst_path, seq)
);

CREATE TABLE IF NOT EXISTS n1_module_status (
    build_hash      TEXT NOT NULL,
    path            TEXT NOT NULL,
    module          TEXT NOT NULL,
    status          TEXT NOT NULL,
    reason          TEXT NOT NULL,
    blocked_tools   TEXT NOT NULL,
    blocking_codes  TEXT NOT NULL,
    PRIMARY KEY (build_hash, path)
);

CREATE TABLE IF NOT EXISTS n1_assign (
    build_hash   TEXT NOT NULL,
    inst_path    TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    proc         INTEGER,
    lhs          TEXT NOT NULL,
    lhs_tree     TEXT NOT NULL,
    rhs_tree     TEXT NOT NULL,
    rhs_refs     TEXT NOT NULL,
    cond_stack   TEXT NOT NULL,
    blocking     INTEGER NOT NULL,
    location     TEXT NOT NULL,
    PRIMARY KEY (build_hash, inst_path, seq)
);

CREATE TABLE IF NOT EXISTS n1_connection (
    build_hash   TEXT NOT NULL,
    inst_path    TEXT NOT NULL,
    port         TEXT NOT NULL,
    direction    TEXT NOT NULL,
    expr         TEXT,
    PRIMARY KEY (build_hash, inst_path, port)
);

CREATE TABLE IF NOT EXISTS n1_fingerprint (
    build_hash  TEXT NOT NULL,
    inst_path   TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    PRIMARY KEY (build_hash, inst_path)
);
"""


def _iso_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _ensure_schema(con: sqlite3.Connection) -> None:
    have = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    version = None
    if "n1_meta" in have:
        row = con.execute("SELECT schema_version FROM n1_meta").fetchone()
        version = row[0] if row else None
    if have & set(_TABLES) and version != SCHEMA_VERSION:
        for t in _TABLES:
            con.execute(f"DROP TABLE IF EXISTS {t}")
    con.executescript(_DDL)
    if con.execute("SELECT count(*) FROM n1_meta").fetchone()[0] == 0:
        con.execute("INSERT INTO n1_meta VALUES (?)", (SCHEMA_VERSION,))


def _j(x) -> str:
    return json.dumps(x, sort_keys=True, ensure_ascii=False)


def write_index(
    db_path: Path,
    build_hash: str,
    elab: ElaborationResult,
    statuses: list[ModuleStatus],
    fingerprints: dict[str, str],
    index_hash: str,
) -> dict:
    """
    Write the full N1 index to SQLite. Returns {"new_index": bool}.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as con:
        _ensure_schema(con)

        # n1_index — skip if already written
        existing = con.execute(
            "SELECT index_hash FROM n1_index WHERE build_hash=?", (build_hash,)
        ).fetchone()
        if existing:
            return {"new_index": False}

        is_incomplete = int(any(i.is_incomplete for i in elab.instances))
        con.execute(
            "INSERT OR IGNORE INTO n1_index VALUES (?,?,?,?,?,?)",
            (build_hash, SCHEMA_VERSION, index_hash,
             elab.top_path, is_incomplete, _iso_now()),
        )

        for inst in elab.instances:
            con.execute(
                "INSERT OR IGNORE INTO n1_instance VALUES (?,?,?,?,?,?,?)",
                (build_hash, inst.path, inst.module, inst.parent,
                 json.dumps(inst.params),
                 json.dumps(sorted(inst.clocks)),
                 int(inst.is_incomplete)),
            )
            for p in inst.ports:
                con.execute(
                    "INSERT OR IGNORE INTO n1_port VALUES (?,?,?,?,?,?,?,?)",
                    (build_hash, inst.path, p.name, p.direction,
                     p.type_str, int(p.is_interface), p.width, int(p.signed)),
                )
            for s in inst.signals:
                con.execute(
                    "INSERT OR IGNORE INTO n1_signal VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (build_hash, inst.path, s.name, s.kind,
                     s.type_str, s.location, s.width, int(s.signed), s.array_dims, s.scope),
                )
            for seq, proc in enumerate(inst.procs):
                con.execute(
                    "INSERT OR IGNORE INTO n1_proc VALUES (?,?,?,?,?,?,?)",
                    (build_hash, inst.path, seq, proc.kind,
                     json.dumps(proc.sensitivity), proc.location, proc.scope),
                )
            for seq, a in enumerate(inst.assigns):
                con.execute(
                    "INSERT OR IGNORE INTO n1_assign VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (build_hash, inst.path, seq, a.proc, _j(a.lhs), _j(a.lhs_tree),
                     _j(a.rhs_tree), _j(a.rhs_refs), _j(a.cond_stack), int(a.blocking), a.location),
                )
            for c in inst.connections:
                con.execute(
                    "INSERT OR IGNORE INTO n1_connection VALUES (?,?,?,?,?)",
                    (build_hash, inst.path, c["port"], c["direction"],
                     None if c["expr"] is None else _j(c["expr"])),
                )
            for seq, drv in enumerate(inst.drivers):
                con.execute(
                    "INSERT OR IGNORE INTO n1_driver VALUES (?,?,?,?,?,?,?)",
                    (build_hash, inst.path, seq, drv.signal,
                     drv.driver_kind, drv.proc_kind, drv.location),
                )

        for ms in statuses:
            con.execute(
                "INSERT OR IGNORE INTO n1_module_status VALUES (?,?,?,?,?,?,?)",
                (build_hash, ms.path, ms.module, ms.status, ms.reason,
                 json.dumps(ms.blocked_tools), json.dumps(ms.blocking_codes)),
            )

        for path, fp in fingerprints.items():
            con.execute(
                "INSERT OR IGNORE INTO n1_fingerprint VALUES (?,?,?)",
                (build_hash, path, fp),
            )

    return {"new_index": True}
