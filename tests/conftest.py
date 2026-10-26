from __future__ import annotations

from pathlib import Path

import pytest
from ruamel.yaml import YAML

FAKE_TOOLS = {"verimine": "test", "pyslang": "test"}


class Project:
    def __init__(self, root: Path):
        self.root = root
        self.cfg = {
            "schema_version": 1,
            "status": "confirmed",
            "unresolved": [],
            "design": {"name": "top", "root": ".", "top": "top", "files": ["rtl/top.sv"]},
            "clocks": [{"name": "clk", "edge": "posedge"}],
            "resets": [{"name": "rst_n", "active": "low", "type": "async"}],
        }
        self.write("rtl/top.sv", "module top(input clk, input rst_n); endmodule\n")

    def write(self, rel: str, text: str | bytes) -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(text, bytes):
            p.write_bytes(text)
        else:
            p.write_text(text)
        return p

    @property
    def design(self) -> dict:
        return self.cfg["design"]

    def yaml(self, name: str = "verimine.yaml") -> Path:
        p = self.root / name
        with p.open("w") as f:
            YAML(typ="safe").dump(self.cfg, f)
        return p

    def run(self, **kw):
        from verimine.n0 import run_n0

        kw.setdefault("tools", FAKE_TOOLS)
        kw.setdefault("env", {})
        kw.setdefault("workspace", self.root / ".verimine")
        return run_n0(self.yaml(), **kw)


@pytest.fixture
def project(tmp_path: Path) -> Project:
    return Project(tmp_path / "proj")


def codes(excinfo) -> list[str]:
    return sorted(i.code for i in excinfo.value.issues)
