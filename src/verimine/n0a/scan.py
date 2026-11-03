"""N0a step 1: find every HDL file under root, skipping VCS and build directories.

Files with identical content (after CRLF normalization, same as N0's file_hash) are kept once:
the first path in sorted order wins, the rest are reported as copies.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from verimine.hashing import file_hash

HDL_SUFFIXES = {".v", ".sv", ".vh", ".svh"}
HEADER_SUFFIXES = {".vh", ".svh"}
SKIP_DIRS = {
    ".git", ".svn", ".hg", ".verimine", ".venv", "venv", "node_modules", "__pycache__",
    "build", "obj_dir", "sim_build", "work", "out", "xsim.dir", "simv.daidir", "csrc",
}


@dataclass
class ScannedFile:
    path: str  # relative to root, posix
    sha: str


@dataclass
class ScanResult:
    files: list[ScannedFile] = field(default_factory=list)
    copies: dict[str, list[str]] = field(default_factory=dict)  # kept path -> identical copies


def scan(root: Path) -> ScanResult:
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
        for name in filenames:
            if Path(name).suffix in HDL_SUFFIXES:
                found.append((Path(dirpath) / name).relative_to(root).as_posix())
    res = ScanResult()
    by_hash: dict[str, str] = {}
    for rel in sorted(found):
        sha, _ = file_hash(root / rel)
        if sha in by_hash:
            res.copies.setdefault(by_hash[sha], []).append(rel)
            continue
        by_hash[sha] = rel
        res.files.append(ScannedFile(rel, sha))
    return res
