"""N0a step 9: elaborate the draft with slang and read back what is still wrong.

The pipeline calls this up to three times, adjusting the draft between rounds (add the file that
defines an unknown module, black-box a vendor primitive, ...). Whatever is left after the last
round goes to unresolved.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pyslang as ps
from pyslang import ast, parsing, syntax


@dataclass
class CheckResult:
    unknown_modules: dict[str, list[str]] = field(default_factory=dict)  # name -> where
    unknown_packages: dict[str, list[str]] = field(default_factory=dict)
    undefined_macros: set[str] = field(default_factory=set)
    duplicates: set[str] = field(default_factory=set)
    other_errors: list[dict] = field(default_factory=list)
    compilation: object = None
    _keep: tuple = ()  # SourceManager and trees must outlive the Compilation

    @property
    def clean(self) -> bool:
        return not (self.unknown_modules or self.unknown_packages or self.undefined_macros
                    or self.other_errors)


def _code(d) -> str:
    return str(d.code).removeprefix("DiagCode(").removesuffix(")")


def elaborate_draft(
    root: Path,
    files: list[str],
    include_dirs: list[str],
    defines: dict[str, str | None],
    top: str,
    blackbox: set[str],
) -> CheckResult:
    root = root.resolve()
    res = CheckResult()
    if not files:
        res.other_errors.append({"code": "NoFiles", "message": "没有可编译的文件", "where": ""})
        return res
    sm = ps.SourceManager()
    po = parsing.PreprocessorOptions()
    po.additionalIncludePaths = [str(root / d) for d in include_dirs]
    po.predefines = [k if v is None else f"{k}={v}" for k, v in defines.items()]
    co = ast.CompilationOptions()
    co.topModules = {top}
    tree = syntax.SyntaxTree.fromFiles([str(root / f) for f in files], sm, ps.Bag([po]))
    comp = ast.Compilation(ps.Bag([co]))
    comp.addSyntaxTree(tree)
    comp.getRoot()
    engine = ps.DiagnosticEngine(sm)

    def where(d) -> str:
        try:
            p = Path(sm.getFileName(d.location))
            rel = p.resolve().relative_to(root).as_posix() if p.is_absolute() else p.as_posix()
            return f"{rel}:{sm.getLineNumber(d.location)}"
        except Exception:
            return ""

    seen = set()
    for d in [*tree.diagnostics, *comp.getAllDiagnostics()]:
        code = _code(d)
        arg = str(d.args[0]) if len(d.args) else ""
        if code == "UnknownModule":
            if arg not in blackbox:
                res.unknown_modules.setdefault(arg, []).append(where(d))
        elif code == "UnknownPackage":
            res.unknown_packages.setdefault(arg, []).append(where(d))
        elif code == "UnknownDirective":
            res.undefined_macros.add(arg.lstrip("`"))
        elif code == "DuplicateDefinition":
            res.duplicates.add(arg)
        elif d.isError():
            key = (code, where(d))
            if key not in seen:
                seen.add(key)
                res.other_errors.append({"code": code, "message": engine.formatMessage(d), "where": key[1]})
    res.compilation = comp
    res._keep = (sm, tree)
    return res
