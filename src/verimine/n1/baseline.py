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


# `module m import pkg::*; (` is legal SV-2009 but Yosys' Verilog front end rejects it, while it
# accepts the same import at file scope. Hoisting it onto the same line keeps line numbers.
_HEADER_IMPORT_RE = re.compile(r"\b(module|interface)(\s+)(\w+)(\s+)(import\s+[\w:*,\s]+?;)")


def _yosys_compatible(files: list[Path], tmp: Path) -> tuple[list[Path], dict[str, str]]:
    """Copies files that need the import rewrite into tmp. Returns (files to read,
    {temp path: original path}) so diagnostics can be mapped back."""
    out, back = [], {}
    for i, f in enumerate(files):
        try:
            text = Path(f).read_text(encoding="utf-8", errors="replace")
        except OSError:
            out.append(Path(f))
            continue
        new = _HEADER_IMPORT_RE.sub(lambda m: f"{' '.join(m.group(5).split())} {m.group(1)}{m.group(2)}{m.group(3)}{m.group(4)}", text)
        if new == text:
            out.append(Path(f))
            continue
        dst = tmp / f"{i:04d}_{Path(f).name}"
        dst.write_text(new, encoding="utf-8")
        out.append(dst)
        back[str(dst)] = str(f)
    return out, back


def run_yosys(
    files: list[Path],
    top: str,
    include_dirs: list[Path],
    defines: dict[str, str | None],
) -> ToolReport:
    """Run Yosys read + hierarchy -check + proc + check on the design."""
    with tempfile.TemporaryDirectory(prefix="verimine-yosys-") as tmp:
        return _run_yosys(files, top, include_dirs, defines, Path(tmp))


def _run_yosys(files, top, include_dirs, defines, tmp: Path) -> ToolReport:
    read_files, back = _yosys_compatible(files, tmp)
    read_args = []
    # Rewritten copies live in tmp, so `include "x.vh" relative to the original file needs
    # the original directory on the search path.
    extra_dirs = sorted({str(Path(o).parent) for o in back.values()})
    for d in [*include_dirs, *extra_dirs]:
        read_args.append(f"-I{d}")
    for k, v in defines.items():
        read_args.append(f"-D{k}={v}" if v is not None else f"-D{k}")

    file_list = " ".join(str(f) for f in read_files)
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

    log = r.stdout + r.stderr
    for tmp_path, orig in back.items():
        log = log.replace(tmp_path, orig)
    errors, warnings = _parse_yosys(log)
    blocked = "ERROR" in log and r.returncode != 0
    return ToolReport(
        tool="yosys",
        ok=r.returncode == 0 and not blocked,
        blocked=blocked,
        errors=errors,
        warnings=warnings,
        raw_log=log[-4000:],
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
