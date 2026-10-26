"""Content hashing. Every hash in VeriMine goes through these two functions."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def normalize_text(data: bytes) -> bytes:
    """CRLF/CR -> LF, so a checkout on Windows hashes the same as on Linux."""
    return data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def file_hash(path: Path) -> tuple[str, bytes]:
    """Returns (sha256 hex of normalized content, normalized content)."""
    data = normalize_text(path.read_bytes())
    return hashlib.sha256(data).hexdigest(), data


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def tagged_hash(tag: str, obj: Any) -> str:
    """Domain-separated hash: the same payload under different layers never collides."""
    h = hashlib.sha256()
    h.update(f"verimine/{tag}/v1\0".encode())
    h.update(canonical_json(obj).encode())
    return h.hexdigest()
