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

All writes are idempotent (INSERT OR IGNORE on primary keys).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .elaborate import InstanceInfo, ElaborationResult
from .status import ModuleStatus


SCHEMA_VERSION = 1

_DDL = """
PRAGMA journal_mode=WAL;

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
    PRIMARY KEY (build_hash, inst_path, name)
);

CREATE TABLE IF NOT EXISTS n1_signal (
    build_hash  TEXT NOT NULL,
    inst_path   TEXT NOT NULL,
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL,
    type_str    TEXT NOT NULL,
    location    TEXT NOT NULL,
    PRIMARY KEY (build_hash, inst_path, name)
);

CREATE TABLE IF NOT EXISTS n1_proc (
    build_hash   TEXT NOT NULL,
    inst_path    TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    kind         TEXT NOT NULL,
    sensitivity  TEXT NOT NULL,
    location     TEXT NOT NULL,
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
        con.executescript(_DDL)

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
                    "INSERT OR IGNORE INTO n1_port VALUES (?,?,?,?,?,?)",
                    (build_hash, inst.path, p.name, p.direction,
                     p.type_str, int(p.is_interface)),
                )
            for s in inst.signals:
                con.execute(
                    "INSERT OR IGNORE INTO n1_signal VALUES (?,?,?,?,?,?)",
                    (build_hash, inst.path, s.name, s.kind,
                     s.type_str, s.location),
                )
            for seq, proc in enumerate(inst.procs):
                con.execute(
                    "INSERT OR IGNORE INTO n1_proc VALUES (?,?,?,?,?,?)",
                    (build_hash, inst.path, seq, proc.kind,
                     json.dumps(proc.sensitivity), proc.location),
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
