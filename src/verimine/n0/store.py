"""N0 steps 7-8: content-addressed snapshot and the design / revision / run records."""
from __future__ import annotations

import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from verimine.hashing import canonical_json

SCHEMA = """
CREATE TABLE IF NOT EXISTS design (
    id          TEXT PRIMARY KEY,   -- design.name
    top         TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS revision (
    id           TEXT PRIMARY KEY,  -- build_hash
    design_id    TEXT NOT NULL REFERENCES design(id),
    source_hash  TEXT NOT NULL,
    config_json  TEXT NOT NULL,     -- normalized config, canonical JSON
    created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS revision_file (
    revision_id  TEXT NOT NULL REFERENCES revision(id),
    path         TEXT NOT NULL,     -- relative to root
    sha256       TEXT NOT NULL,     -- object key in the snapshot store
    role         TEXT NOT NULL,     -- source | library | include
    PRIMARY KEY (revision_id, path)
);
CREATE TABLE IF NOT EXISTS run (
    id           TEXT PRIMARY KEY,  -- run_hash
    revision_id  TEXT NOT NULL REFERENCES revision(id),
    env_hash     TEXT NOT NULL,
    tools_json   TEXT NOT NULL,
    run_json     TEXT NOT NULL,     -- llm / run / env sections that went into run_hash
    stage        TEXT NOT NULL,     -- last stage completed
    created_at   TEXT NOT NULL
);
"""


def snapshot(store: Path, blobs: dict[str, bytes]) -> int:
    """Writes each blob to objects/<h[:2]>/<h[2:]>. Returns how many were new."""
    new = 0
    for h, data in sorted(blobs.items()):
        dest = store / "objects" / h[:2] / h[2:]
        if dest.exists():
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Atomic: a crash never leaves a truncated object under a valid hash name.
        fd, tmp = tempfile.mkstemp(dir=dest.parent)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, dest)
        new += 1
    return new


def record(
    db_path: Path,
    normalized: dict,
    sources: list[dict],
    hashes: dict[str, str],
    tools: dict[str, str],
    run_inputs: dict,
) -> dict:
    """Idempotent: running N0 twice on the same inputs adds nothing the second time."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    design = normalized["design"]
    with sqlite3.connect(db_path) as con:
        con.executescript(SCHEMA)
        con.execute("INSERT OR IGNORE INTO design VALUES (?,?,?)", (design["name"], design["top"], now))
        cur = con.execute(
            "INSERT OR IGNORE INTO revision VALUES (?,?,?,?,?)",
            (hashes["build_hash"], design["name"], hashes["source_hash"], canonical_json(normalized), now),
        )
        new_revision = cur.rowcount == 1
        con.executemany(
            "INSERT OR IGNORE INTO revision_file VALUES (?,?,?,?)",
            [(hashes["build_hash"], s["path"], s["sha256"], s["role"]) for s in sources],
        )
        cur = con.execute(
            "INSERT OR IGNORE INTO run VALUES (?,?,?,?,?,?,?)",
            (hashes["run_hash"], hashes["build_hash"], hashes["env_hash"],
             canonical_json(tools), canonical_json(run_inputs), "N0", now),
        )
        new_run = cur.rowcount == 1
    return {
        "design_id": design["name"],
        "revision_id": hashes["build_hash"],
        "run_id": hashes["run_hash"],
        "new_revision": new_revision,
        "new_run": new_run,
    }
