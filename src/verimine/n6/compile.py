"""N6 steps 1-2: compile the delivery version with slang, read the verification version with Yosys.

Both report diagnostics with file and line; a diagnostic is pinned to the property whose line it
is on (or to every property of a checker module for a module-level line). What is not pinned
(a design file only breaks with our files added, a Yosys error with no location) is narrowed down
by bisection: compile halves of the properties until single culprits remain.

Some warnings are errors here, because the tool would otherwise go on with a wrong reading of
what N5 meant (measured, see docs/N6.md):
  slang  Redefinition (an assertion label equal to a port: other tools reject it),
         IndexOOB / RangeOOB (a select outside the signal)
  Yosys  "Identifier ... is implicitly declared" (an unknown name becomes a free wire and the
         proof goes on against it), "Range select out of bounds" (bits read as undef)
Only diagnostics in our files count; the design alone is compiled once as the baseline.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

SLANG_PROMOTE = {"Redefinition", "IndexOOB", "RangeOOB"}
_Y_LOC = re.compile(r"^(\S+?):(\d+)(?:\.\d+-\d+\.\d+)?: (ERROR|Warning): ?(.*)$")
_Y_PROMOTE = re.compile(r"is implicitly declared|[Rr]ange select .*out of bounds|out of bounds on signal")


@dataclass(frozen=True)
class Diag:
    tool: str          # slang | yosys
    file: str          # basename for our files, path as reported otherwise
    line: int
    code: str
    message: str

    def to_dict(self) -> dict:
        return {"tool": self.tool, "file": self.file, "line": self.line, "code": self.code, "message": self.message}

    def __str__(self) -> str:
        where = f"{self.file}:{self.line}" if self.line else self.file
        return f"{where}: {self.message}" if where else self.message


# --------------------------------------------------------------------------- #
# slang
# --------------------------------------------------------------------------- #

def slang_diags(files, include_dirs, defines, top: str, extra: list[Path]) -> list[Diag]:
    """Errors (and promoted warnings) of design + extra files."""
    from pyslang.ast import Compilation, CompilationOptions
    from pyslang.parsing import PreprocessorOptions
    from pyslang.pyslang import Bag, DiagnosticEngine, SourceManager
    from pyslang.syntax import SyntaxTree

    from verimine.n0a.check import _code

    sm = SourceManager()
    po = PreprocessorOptions()
    po.additionalIncludePaths = [str(d) for d in include_dirs]
    po.predefines = [f"{k}={v}" if v is not None else k for k, v in defines.items()]
    opts = CompilationOptions()
    opts.topModules = {top}
    comp = Compilation(Bag([opts]))
    trees = SyntaxTree.fromFiles([str(f) for f in [*files, *extra]], sm, Bag([po]))
    for t in trees if isinstance(trees, (list, tuple)) else [trees]:
        comp.addSyntaxTree(t)
    eng = DiagnosticEngine(sm)
    ours = {str(Path(p).resolve()): Path(p).name for p in extra}
    out = []
    for d in comp.getAllDiagnostics():
        code = _code(d)
        if not d.isError() and code not in SLANG_PROMOTE:
            continue
        try:
            full = str(Path(str(sm.getFullPath(d.location.buffer))).resolve())
            line = sm.getLineNumber(d.location)
        except Exception:
            full, line = "", 0
        out.append(Diag("slang", ours.get(full, full), line, code, eng.formatMessage(d)))
    return sorted(set(out), key=lambda x: (x.file, x.line, x.code, x.message))


# --------------------------------------------------------------------------- #
# Yosys
# --------------------------------------------------------------------------- #

def yosys_available() -> bool:
    return bool(shutil.which("yosys"))


def yosys_diags(design, incs: dict[str, str], workdir: Path, timeout: int) -> list[Diag]:
    """Read the design with the verification includes the way the n2b engine's sby script does
    (`design` is an n2b engine _Design, sources already hooked). Yosys stops at the first error,
    so at most one ERROR comes back; promoted warnings come back all at once."""
    if workdir.exists():
        shutil.rmtree(workdir)
    inc = workdir / "inc"
    inc.mkdir(parents=True)
    for name, text in incs.items():
        (inc / f"vm_n2b_{name}.svh").write_text(text, encoding="utf-8")
    sources = sorted((design.workdir / "src").glob("*"))
    dirs = [inc] + sorted({Path(f).resolve().parent for f in design.files}) + [Path(d) for d in design.include_dirs]
    defs = ["-DVERIMINE_N2B"] + [f"-D{k}={v}" if v is not None else f"-D{k}" for k, v in sorted(design.defines.items())]
    script = workdir / "read.ys"
    script.write_text("\n".join([
        f"verilog_defaults -add {' '.join(defs)} {' '.join(f'-I{d}' for d in dirs)}",
        f"read_verilog -formal -sv {' '.join(str(s) for s in sources)}",
        f"hierarchy -top {design.top}", "proc", "tribuf -formal", "delete */*tribuf_conflict*",
        f"prep -top {design.top}", ""]), encoding="utf-8")
    try:
        r = subprocess.run(["yosys", "-q", "-s", str(script)], capture_output=True, text=True, timeout=timeout,
                           cwd=workdir)
        log = r.stdout + r.stderr
    except subprocess.TimeoutExpired:
        return [Diag("yosys", "", 0, "timeout", f"Yosys 读入超时（{timeout} 秒）")]
    ours = {f"vm_n2b_{n}.svh" for n in incs}
    out = []
    for line in log.splitlines():
        m = _Y_LOC.match(line.strip())
        if m:
            f, ln, kind, msg = m.groups()
            base = Path(f).name
            if kind == "Warning" and not (base in ours and _Y_PROMOTE.search(msg)):
                continue
            out.append(Diag("yosys", base if base in ours else f, int(ln), kind.lower(), msg.strip()))
        elif "ERROR:" in line:
            out.append(Diag("yosys", "", 0, "error", line.split("ERROR:", 1)[1].strip()))
    if r.returncode != 0 and not any(d.code in ("error", "timeout") for d in out):
        out.append(Diag("yosys", "", 0, "error", f"Yosys 退出码 {r.returncode}：" + log.strip()[-300:]))
    return sorted(set(out), key=lambda x: (x.file, x.line, x.code, x.message))


# --------------------------------------------------------------------------- #
# Pinning diagnostics on properties
# --------------------------------------------------------------------------- #

def pin(diags: list[Diag], line_maps: dict[str, dict[int, list[str]]], baseline: set) -> tuple[dict[str, list[Diag]], list[Diag]]:
    """(property name -> its diagnostics, diagnostics pinned on nothing). line_maps: our file ->
    line -> names. A diagnostic on a module-level line that quotes one of that module's
    property names is pinned on that property only (slang: "redefinition of 'x'")."""
    pinned: dict[str, list[Diag]] = {}
    loose = []
    for d in diags:
        if (d.code, d.file, d.line, d.message) in baseline:
            continue
        names = line_maps.get(d.file, {}).get(d.line)
        if not names:
            loose.append(d)
            continue
        quoted = [n for n in names if f"'{n}'" in d.message or f"`\\{n}'" in d.message]
        for n in quoted or names:
            pinned.setdefault(n, []).append(d)
    return pinned, loose


def bisect(names: list[str], fails: Callable[[list[str]], bool], budget: list[int]) -> list[str]:
    """Names that make `fails` true on their own (group testing by halving). budget[0] is the
    number of compiles left; when it runs out, the rest of the group is returned as is."""
    if not names:
        return []
    if budget[0] <= 0:
        return list(names)
    budget[0] -= 1
    if not fails(names):
        return []
    if len(names) == 1:
        return list(names)
    mid = len(names) // 2
    return bisect(names[:mid], fails, budget) + bisect(names[mid:], fails, budget)
