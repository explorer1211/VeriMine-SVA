"""What N2b needs from slang beyond the N1 index:

  - lint diagnostics of slang's analysis pass (latches, multiple drivers, incomplete or
    overlapping case, truncation, constant out-of-bounds index). N1 keeps only compile
    diagnostics; these come from AnalysisManager.
  - where every module starts and ends in its file, so the verification version can insert
    its assertions right before `endmodule` without moving any line.

Same files, include dirs and defines as N1, so the result is fixed by the build hash.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from pyslang.analysis import AnalysisManager
from pyslang.ast import Compilation, CompilationOptions
from pyslang.parsing import PreprocessorOptions
from pyslang.pyslang import Bag, DiagnosticEngine, SourceManager
from pyslang.syntax import SyntaxTree

# slang code -> N2b check
DIAG_CHECKS = {
    "CaseUnreachable": "dead.case_unreachable",
    "IndexOOB": "array.const_oob",
    "RangeOOB": "array.const_oob",
    "WidthTruncate": "array.width_trunc",
    "PortWidthTruncate": "array.width_trunc",
    "MultipleContAssigns": "x.multi_driver",
    "MixedVarAssigns": "x.multi_driver",
    "MultipleAlwaysAssigns": "x.multi_driver",
    "MultipleUWireDrivers": "x.multi_driver",
    "MultipleUDNTDrivers": "x.multi_driver",
    "UndrivenNet": "x.floating",
    "UndrivenPort": "x.floating",
    "CaseIncomplete": "control.case_incomplete",
    "CaseEnum": "control.case_incomplete",
    "CaseDup": "control.case_parallel",
    "CaseOverlap": "control.case_parallel",
    "InferredLatch": "timing.latch",
}
_QUOTED = re.compile(r"'([^']+)'")
_CASE_KW = re.compile(r"^\s*(?:(unique0?|priority)\s+)?case[zx]?\b")


def _priority_case(path: Path, line: int) -> bool:
    """Overlapping items are what `priority case` means: the first match wins."""
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return False
    for i in range(min(line, len(lines)) - 1, max(-1, line - 60), -1):
        m = _CASE_KW.match(lines[i])
        if m:
            return m.group(1) == "priority"
    return False


@dataclass
class ModuleSpan:
    name: str
    file: str            # root-relative
    start_line: int
    end_line: int
    end_offset: int      # byte offset of the `endmodule` keyword in the file


@dataclass
class ScanResult:
    diags: list[dict] = field(default_factory=list)       # {"code","check","message","file","line","signal","module"}
    modules: dict[str, ModuleSpan] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def module_at(self, file: str, line: int) -> str | None:
        for m in self.modules.values():
            if m.file == file and m.start_line <= line <= m.end_line:
                return m.name
        return None


def _rel(path: str, root: Path) -> str:
    try:
        return Path(path).resolve().relative_to(root.resolve()).as_posix()
    except (ValueError, OSError):
        return path


def scan(files: list[Path], top: str, include_dirs: list[Path], defines: dict[str, str | None],
         root: Path) -> ScanResult:
    out = ScanResult()
    sm = SourceManager()
    po = PreprocessorOptions()
    po.additionalIncludePaths = [str(d) for d in include_dirs]
    po.predefines = [f"{k}={v}" if v is not None else k for k, v in defines.items()]
    comp_opts = CompilationOptions()
    if top:
        comp_opts.topModules = {top}
    comp = Compilation(Bag([comp_opts]))

    # One compilation unit for all files, like N1: macros defined in one file are seen by the next.
    try:
        trees = SyntaxTree.fromFiles([str(f) for f in files], sm, Bag([po]))
    except Exception as e:
        out.errors.append(f"slang: {e}")
        return out
    for tree in trees if isinstance(trees, (list, tuple)) else [trees]:
        comp.addSyntaxTree(tree)
        root_node = tree.root
        members = getattr(root_node, "members", None)
        decls = list(members) if members is not None else [root_node]
        for d in decls:
            if "ModuleDeclaration" not in str(d.kind):
                continue
            try:
                name = d.header.name.valueText
                start, end = d.header.sourceRange.start, d.endmodule.location
                if sm.getFullPath(end.buffer) != sm.getFullPath(start.buffer):
                    continue  # produced by an include or a macro: cannot insert there
                out.modules.setdefault(name, ModuleSpan(
                    name, _rel(str(sm.getFullPath(start.buffer)), root), sm.getLineNumber(start),
                    sm.getLineNumber(end), end.offset))
            except Exception:
                continue

    diags = list(comp.getAllDiagnostics())
    try:
        mgr = AnalysisManager()
        mgr.analyze(comp)
        diags += list(mgr.getDiagnostics())
    except Exception as e:
        out.errors.append(f"slang analysis: {e}")
    engine = DiagnosticEngine(sm)
    seen = set()
    for d in diags:
        code = str(d.code).removeprefix("DiagCode(").removesuffix(")")
        check = DIAG_CHECKS.get(code)
        if check is None:
            continue
        try:
            file = _rel(str(sm.getFullPath(d.location.buffer)), root)
            line = sm.getLineNumber(d.location)
            msg = engine.formatMessage(d)
        except Exception:
            file, line, msg = "", 0, code
        if code in ("CaseOverlap", "CaseDup") and code == "CaseOverlap" and _priority_case(root / file, line):
            continue
        key = (code, file, line, msg)
        if key in seen:
            continue
        seen.add(key)
        m = _QUOTED.search(msg)
        out.diags.append({"code": code, "check": check, "message": msg, "file": file, "line": line,
                          "signal": m.group(1) if m else "", "module": out.module_at(file, line)})
    out.diags.sort(key=lambda x: (x["file"], x["line"], x["code"]))
    return out
