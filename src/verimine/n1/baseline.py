"""
Baseline checks: run the design through slang, Verilator, and Yosys without
any assertions, and cross-check the top-level port lists.

Each tool returns a list of diagnostics with severity, message, and location.
A "BLOCKED" verdict for a tool means that tool couldn't read the design at all
(non-zero exit and no useful structured output).
"""

from __future__ import annotations

import dataclasses
import json
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any


@dataclasses.dataclass
class ToolReport:
    tool: str
    ok: bool                  # True = tool ran and produced output we can use
    blocked: bool             # True = tool couldn't read the design
    errors: list[dict]
    warnings: list[dict]
    raw_log: str              # trimmed stdout+stderr for debugging


def _parse_verilator(text: str) -> tuple[list[dict], list[dict]]:
    """
    Parse Verilator output. Lines look like:
      %Warning-LATCH: file:3:3: message
      %Error: file:5:2: message
    """
    errors: list[dict] = []
    warnings: list[dict] = []
    pat = re.compile(
        r"^(%Warning|%(Error|Fatal))-(\w+):\s*([^:]+):(\d+):(\d+):\s*(.*)$"
    )
    for line in text.splitlines():
        m = pat.match(line.strip())
        if not m:
            continue
        sev, code, _name, path, line_no, col, msg = m.groups()
        entry = {
            "code": code,
            "message": msg.strip(),
            "where": f"{path}:{line_no}:{col}",
        }
        if sev == "%Warning":
            warnings.append(entry)
        else:
            errors.append(entry)
    return errors, warnings


def _parse_yosys(text: str) -> tuple[list[dict], list[dict]]:
    """
    Parse Yosys output. Errors start with 'ERROR:', warnings with 'Warning:'.
    """
    errors: list[dict] = []
    warnings: list[dict] = []
    err_pat = re.compile(r"^ERROR:\s*(.*)$")
    warn_pat = re.compile(r"^Warning:\s*(.*)$")
    loc_pat = re.compile(r"([^:]+):(\d+)")
    for line in text.splitlines():
        line = line.strip()
        em = err_pat.match(line)
        wm = warn_pat.match(line)
        if em:
            msg = em.group(1).strip()
            where = ""
            lm = loc_pat.search(msg)
            if lm:
                where = f"{lm.group(1)}:{lm.group(2)}"
            errors.append({"code": "YOSYS_ERROR", "message": msg, "where": where})
        elif wm:
            msg = wm.group(1).strip()
            where = ""
            lm = loc_pat.search(msg)
            if lm:
                where = f"{lm.group(1)}:{lm.group(2)}"
            warnings.append({"code": "YOSYS_WARNING", "message": msg, "where": where})
    return errors, warnings


def _slang_top_ports(comp) -> list[dict]:
    """Extract top-level port list from a compiled slang instance."""
    from pyslang.ast import VisitAction
    tops = comp.getRoot().topInstances
    if not tops:
        return []
    top = tops[0]
    ports: list[dict] = []

    def visitor(s):
        if str(s.kind) == "SymbolKind.Port":
            try:
                ports.append({
                    "name": s.name,
                    "direction": str(s.direction).split(".")[-1].lower(),
                    "type": str(s.type),
                })
            except Exception:
                pass
            return VisitAction.Skip
        if str(s.kind) == "SymbolKind.InstanceBody":
            if s.hierarchicalPath != top.hierarchicalPath:
                return VisitAction.Skip
            return VisitAction.Advance
        return VisitAction.Skip

    top.body.visit(visitor)
    return ports


def _yosys_top_ports(script_out: str, top: str) -> list[dict]:
    """Parse Yosys 'select -list' or hierarchy output for port names."""
    # Yosys prints "module top (\n  input clk,\n  output [3:0] q\n)" during read.
    ports: list[dict] = []
    pat = re.compile(
        r"^\s*(input|output|inout)\s+(\[[^\]]+\]\s+)?(\w+)"
    )
    in_top = False
    for line in script_out.splitlines():
        if line.strip().startswith(f"module {top}"):
            in_top = True
            continue
        if in_top:
            if line.strip().startswith("endmodule") or (
                line.strip().startswith("module ") and top not in line
            ):
                in_top = False
                continue
            m = pat.match(line)
            if m:
                direction, _width, name = m.groups()
                ports.append({"name": name, "direction": direction, "type": ""})
    return ports


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

def run_verilator(
    files: list[Path],
    top: str,
    include_dirs: list[Path],
    defines: dict[str, str | None],
) -> ToolReport:
    """Run Verilator --lint-only -Wall on the design."""
    args = ["verilator", "--lint-only", "--no-timing",
            "--top-module", top, "-Wall", "-Wno-DECLFILENAME"]
    for d in include_dirs:
        args.append(f"+incdir+{d}")
    for k, v in defines.items():
        args.append(f"+define+{k}={v}" if v is not None else f"+define+{k}")
    args += [str(f) for f in files]

    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=120)
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return ToolReport(
            tool="verilator", ok=False, blocked=True,
            errors=[{"code": "VERILATOR_MISSING",
                     "message": str(e), "where": ""}],
            warnings=[], raw_log="",
        )

    errors, warnings = _parse_verilator(r.stderr + r.stdout)
    blocked = r.returncode != 0 and not errors and not warnings and "Exiting due to" in (r.stderr + r.stdout)
    # If only warnings and rc=1 due to -Wno-fatal, treat as ok_with_warnings
    has_error = bool(errors) or ("Exiting due to" in (r.stderr + r.stdout) and any("%Error" in l for l in (r.stderr + r.stdout).splitlines()))
    ok = r.returncode == 0 or (not has_error)
    return ToolReport(
        tool="verilator",
        ok=ok and not blocked,
        blocked=blocked,
        errors=errors,
        warnings=warnings,
        raw_log=(r.stderr + r.stdout)[-4000:],
    )


def run_yosys(
    files: list[Path],
    top: str,
    include_dirs: list[Path],
    defines: dict[str, str | None],
) -> ToolReport:
    """Run Yosys read + hierarchy -check + proc + check on the design."""
    # Write a yosys script file
    read_args = []
    for d in include_dirs:
        read_args.append(f"-I{d}")
    for k, v in defines.items():
        read_args.append(f"-D{k}={v}" if v is not None else f"-D{k}")

    file_list = " ".join(str(f) for f in files)
    read_cmd = " ".join(["read_verilog", "-sv", "-formal"] + read_args + [file_list])
    script = f"""
{read_cmd}
hierarchy -check -top {top}
proc
check
select -list {top}
"""

    try:
        r = subprocess.run(
            ["yosys", "-p", script],
            capture_output=True, text=True, timeout=180,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return ToolReport(
            tool="yosys", ok=False, blocked=True,
            errors=[{"code": "YOSYS_MISSING",
                     "message": str(e), "where": ""}],
            warnings=[], raw_log="",
        )

    errors, warnings = _parse_yosys(r.stdout + r.stderr)
    blocked = "ERROR" in (r.stdout + r.stderr) and r.returncode != 0
    return ToolReport(
        tool="yosys",
        ok=r.returncode == 0 and not blocked,
        blocked=blocked,
        errors=errors,
        warnings=warnings,
        raw_log=(r.stdout + r.stderr)[-4000:],
    )


def run_slang_baseline(comp) -> ToolReport:
    """Use the already-built Compilation to report slang's view of the design."""
    diags = comp.getAllDiagnostics()
    errors: list[dict] = []
    warnings: list[dict] = []
    from pyslang.pyslang import DiagnosticEngine, SourceManager
    sm = comp.getSourceManager() if hasattr(comp, "getSourceManager") else None
    engine = DiagnosticEngine(sm) if sm is not None else None
    for d in diags:
        try:
            msg = engine.formatMessage(d) if engine else str(d.code)
        except Exception:
            msg = str(d.code)
        loc = ""
        try:
            if sm:
                buf = sm.getBufferFromLocation(d.location)
                path = sm.getFileName(d.location)
                line = sm.getLineNumber(d.location)
                col = sm.getColumnNumber(d.location)
                loc = f"{path}:{line}:{col}"
        except Exception:
            pass
        entry = {"code": str(d.code), "message": msg, "where": loc}
        if d.isError():
            errors.append(entry)
        else:
            warnings.append(entry)
    return ToolReport(
        tool="slang",
        ok=len(errors) == 0,
        blocked=False,
        errors=errors,
        warnings=warnings,
        raw_log="",
    )


def cross_check_ports(slang_ports: list[dict], yosys_ports: list[dict]) -> list[dict]:
    """
    Compare slang and yosys top-level port lists. Return mismatch entries.
    """
    s = {p["name"]: p for p in slang_ports}
    y = {p["name"]: p for p in yosys_ports}
    diffs: list[dict] = []
    for name in sorted(set(s) | set(y)):
        if name not in s:
            diffs.append({
                "code": "N1_PORT_MISMATCH",
                "message": f"port '{name}' present in yosys but not slang",
                "where": "",
            })
        elif name not in y:
            diffs.append({
                "code": "N1_PORT_MISMATCH",
                "message": f"port '{name}' present in slang but not yosys",
                "where": "",
            })
        else:
            sd = (s[name].get("direction") or "").lower()
            yd = (y[name].get("direction") or "").lower()
            # normalize: yosys uses 'input'/'output'/'inout'; slang uses 'in'/'out'/'inout'
            smap = {"in": "input", "out": "output", "inout": "inout"}
            if sd in smap:
                sd = smap[sd]
            if sd != yd:
                diffs.append({
                    "code": "N1_PORT_MISMATCH",
                    "message": f"port '{name}' direction differs: slang={sd} yosys={yd}",
                    "where": "",
                })
    return diffs
