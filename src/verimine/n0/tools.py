"""N0 step 6: record the version of every tool that can influence results."""
from __future__ import annotations

import platform
import shutil
import subprocess
from importlib import metadata

import pyslang as ps

import verimine

# Engines used by later stages. Missing ones are simply absent from the record.
EXTERNAL_TOOLS = {
    "yosys": ["-V"],
    "sby": ["--version"],
    "verilator": ["--version"],
    "iverilog": ["-V"],
}


def _first_line(cmd: list[str]) -> str | None:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    for line in (p.stdout + "\n" + p.stderr).splitlines():
        if line.strip():
            return line.strip()
    return None


def collect_tools() -> dict[str, str]:
    v = ps.VersionInfo
    tools = {
        "verimine": verimine.__version__,
        "python": platform.python_version(),
        "pyslang": f"{metadata.version('pyslang')} (slang {v.getMajor()}.{v.getMinor()}.{v.getPatch()}+{v.getHash()})",
        "pydantic": metadata.version("pydantic"),
    }
    for name, args in EXTERNAL_TOOLS.items():
        exe = shutil.which(name)
        if exe and (ver := _first_line([exe, *args])):
            tools[name] = ver
    return dict(sorted(tools.items()))
