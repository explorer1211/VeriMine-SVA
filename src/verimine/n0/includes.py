"""N0 step 4: find every `include'd file by running the slang preprocessor.

Headers are never listed in .f files, but they change the design, so they must enter source_hash.
Only includes on the active `ifdef branches (under the configured defines) are collected; the
defines themselves are part of build_hash, so this is exact for the build being described.
"""
from __future__ import annotations

from pathlib import Path

import pyslang as ps
from pyslang import parsing, syntax

from verimine.issues import IssueLog


def _predefine(name: str, value: str | None) -> str:
    return name if value is None else f"{name}={value}"


def collect_includes(
    root: Path,
    units: list[str],
    include_dirs: list[str],
    defines: dict[str, str | None],
    log: IssueLog,
) -> list[str]:
    """Returns included files relative to root, sorted. Missing or out-of-root includes are errors."""
    if not units:
        return []
    po = parsing.PreprocessorOptions()
    po.additionalIncludePaths = [str(root / d) for d in include_dirs]
    po.predefines = [_predefine(k, v) for k, v in defines.items()]
    sm = ps.SourceManager()
    # One tree over all units: macros defined in an earlier file are visible in later ones,
    # which is how simulators treat a single compilation command.
    tree = syntax.SyntaxTree.fromFiles([str(root / u) for u in units], sm, ps.Bag([po]))

    engine = ps.DiagnosticEngine(sm)
    for diag in tree.diagnostics:
        loc = f"{sm.getFileName(diag.location)}:{sm.getLineNumber(diag.location)}"
        msg = engine.formatMessage(diag)
        if diag.code == ps.Diags.CouldNotOpenIncludeFile:
            log.error("N0_INCLUDE_MISSING", f"找不到被 include 的文件：{msg}", loc)
        elif diag.isError():
            # Syntax problems belong to N1; record them but do not block hashing.
            log.warn("N0_PREPROCESS_DIAG", msg, loc)

    found: set[str] = set()
    for inc in tree.getIncludeDirectives():
        if not inc.buffer:
            continue  # already reported via CouldNotOpenIncludeFile
        real = Path(sm.getFullPath(inc.buffer.id)).resolve()
        if not real.is_relative_to(root):
            log.error("N0_INCLUDE_ESCAPES_ROOT", f"被 include 的文件在 root 之外：{real}", inc.path)
            continue
        found.add(real.relative_to(root).as_posix())
    return sorted(found)
