"""N0a step 7: infer include directories from `include "a/b.vh" references.

For `include "common/defs.vh" we look for scanned files whose path ends with common/defs.vh.
If X/common/defs.vh is found, X goes into include_dirs (not the header's own directory). With
several matches the one nearest to the including file wins; the rest are alternatives.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath


@dataclass
class IncludeMatch:
    includer: str
    name: str
    chosen: str | None
    incdir: str | None
    alternatives: list[str] = field(default_factory=list)


def _parts(p: str) -> tuple[str, ...]:
    return tuple(x for x in PurePosixPath(p).parts if x not in (".", ""))


def _distance(a: tuple[str, ...], b: tuple[str, ...]) -> int:
    common = 0
    for x, y in zip(a, b):
        if x != y:
            break
        common += 1
    return (len(a) - common) + (len(b) - common)


def match_include(includer: str, name: str, paths: list[str]) -> IncludeMatch:
    want = _parts(name)
    if not want or ".." in want:
        return IncludeMatch(includer, name, None, None)
    home = _parts(includer)[:-1]
    cands = [p for p in paths if _parts(p)[-len(want):] == want and p != includer]
    cands.sort(key=lambda p: (_distance(home, _parts(p)[:-1]), p))
    if not cands:
        return IncludeMatch(includer, name, None, None)
    chosen = cands[0]
    base = _parts(chosen)[: -len(want)]
    return IncludeMatch(includer, name, chosen, "/".join(base) or ".", cands[1:])


def resolve_all(facts: dict, paths: list[str]) -> dict[tuple[str, str], IncludeMatch]:
    """(includer, include name) -> match, for every include in every scanned file."""
    return {
        (f.path, inc): match_include(f.path, inc, paths)
        for f in facts.values()
        for inc in f.includes
    }
