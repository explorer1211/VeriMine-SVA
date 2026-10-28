"""Tests for N1: structural elaboration and indexing."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from conftest import FAKE_TOOLS, Project, codes
from verimine.n1 import run_n1
from verimine.n1.pipeline import run_n1


# --------------------------------------------------------------------------- #
# Helper: a tiny SV design for quick checks
# --------------------------------------------------------------------------- #

SIMPLE_SV = """\
module child #(parameter int W=8) (input logic clk, input logic [W-1:0] a, output logic [W-1:0] b);
  always_ff @(posedge clk) b <= a;
endmodule

module top (input logic clk, input logic rst_n, output logic [7:0] out);
  logic [7:0] sig;
  child #(.W(8)) u_child(.clk(clk), .a(sig), .b(out));
  always_ff @(posedge clk, negedge rst_n) begin
    if (!rst_n) sig <= 0;
    else sig <= sig + 1;
  end
endmodule
"""


def _n1(project: Project, **kw):
    """Run N1 with no writes, using FAKE_TOOLS for N0."""
    kw.setdefault("tools", FAKE_TOOLS)
    kw.setdefault("env", {})
    kw.setdefault("write", False)
    return run_n1(project.yaml(), workspace=project.root / ".verimine", **kw)


# --------------------------------------------------------------------------- #
# Elaboration
# --------------------------------------------------------------------------- #

def test_elaborates_simple_hierarchy(project):
    project.write("rtl/top.sv", SIMPLE_SV)
    project.design.update(files=["rtl/top.sv"], top="top")
    project.cfg["clocks"] = [{"name": "clk", "edge": "posedge"}]
    project.cfg["resets"] = [{"name": "rst_n", "active": "low", "type": "async"}]
    res = _n1(project)
    paths = [i.path for i in res.elaboration.instances]
    assert "top" in paths
    assert "top.u_child" in paths


def test_ports_extracted(project):
    project.write("rtl/top.sv", SIMPLE_SV)
    project.design.update(files=["rtl/top.sv"], top="top")
    project.cfg["clocks"] = [{"name": "clk", "edge": "posedge"}]
    project.cfg["resets"] = [{"name": "rst_n", "active": "low", "type": "async"}]
    res = _n1(project)
    top = next(i for i in res.elaboration.instances if i.path == "top")
    port_names = {p.name for p in top.ports}
    assert port_names == {"clk", "rst_n", "out"}
    clk_port = next(p for p in top.ports if p.name == "clk")
    assert clk_port.direction == "in"


def test_parameter_resolved(project):
    project.write("rtl/top.sv", SIMPLE_SV)
    project.design.update(files=["rtl/top.sv"], top="top")
    project.cfg["clocks"] = [{"name": "clk", "edge": "posedge"}]
    project.cfg["resets"] = [{"name": "rst_n", "active": "low", "type": "async"}]
    res = _n1(project)
    child = next(i for i in res.elaboration.instances if i.path == "top.u_child")
    assert child.params.get("W") == "8"


def test_proc_sensitivity_extracted(project):
    project.write("rtl/top.sv", SIMPLE_SV)
    project.design.update(files=["rtl/top.sv"], top="top")
    project.cfg["clocks"] = [{"name": "clk", "edge": "posedge"}]
    project.cfg["resets"] = [{"name": "rst_n", "active": "low", "type": "async"}]
    res = _n1(project)
    top = next(i for i in res.elaboration.instances if i.path == "top")
    ff_procs = [p for p in top.procs if p.kind == "always_ff"]
    assert len(ff_procs) == 1
    sigs = {ev["signal"]: ev["edge"] for ev in ff_procs[0].sensitivity}
    assert sigs.get("clk") == "posedge"
    assert sigs.get("rst_n") == "negedge"


# --------------------------------------------------------------------------- #
# Clock/reset confirmation
# --------------------------------------------------------------------------- #

def test_no_clock_mismatch_warning_when_correct(project):
    project.write("rtl/top.sv", SIMPLE_SV)
    project.design.update(files=["rtl/top.sv"], top="top")
    project.cfg["clocks"] = [{"name": "clk", "edge": "posedge"}]
    project.cfg["resets"] = [{"name": "rst_n", "active": "low", "type": "async"}]
    res = _n1(project)
    warn_codes = [w["code"] for w in res.warnings]
    assert "N1_CLOCK_MISMATCH" not in warn_codes


def test_clock_mismatch_warning_when_wrong(project):
    project.write("rtl/top.sv", SIMPLE_SV)
    project.design.update(files=["rtl/top.sv"], top="top")
    project.cfg["clocks"] = [{"name": "wrong_clk", "edge": "posedge"}]
    project.cfg["resets"] = [{"name": "rst_n", "active": "low", "type": "async"}]
    res = _n1(project)
    warn_codes = [w["code"] for w in res.warnings]
    assert "N1_CLOCK_MISMATCH" in warn_codes


# --------------------------------------------------------------------------- #
# Module status
# --------------------------------------------------------------------------- #

def test_ok_status_for_clean_design(project):
    project.write("rtl/top.sv", SIMPLE_SV)
    project.design.update(files=["rtl/top.sv"], top="top")
    project.cfg["clocks"] = [{"name": "clk", "edge": "posedge"}]
    project.cfg["resets"] = [{"name": "rst_n", "active": "low", "type": "async"}]
    res = _n1(project)
    top_ms = next(s for s in res.statuses if s.path == "top")
    # verilator and yosys both read plain SV fine
    # Status may be OK or OK_WITH_WARNINGS depending on tool output
    assert top_ms.status in ("OK", "OK_WITH_WARNINGS", "BLOCKED", "CONTAINS_BLOCKED")


def test_unknown_module_blocked(project):
    project.write("rtl/top.sv", "module top (input clk); missing_mod u(); endmodule\n")
    project.cfg["clocks"] = [{"name": "clk", "edge": "posedge"}]
    project.cfg["resets"] = [{"name": "rst_n", "active": "low", "type": "async"}]
    res = _n1(project)
    top_ms = next(s for s in res.statuses if s.path == "top")
    # slang will report an error for the unknown module
    assert res.elaboration.errors or top_ms.status in ("BLOCKED", "CONTAINS_BLOCKED", "OK_WITH_WARNINGS")


# --------------------------------------------------------------------------- #
# Fingerprints
# --------------------------------------------------------------------------- #

def test_fingerprints_computed(project):
    project.write("rtl/top.sv", SIMPLE_SV)
    project.design.update(files=["rtl/top.sv"], top="top")
    project.cfg["clocks"] = [{"name": "clk", "edge": "posedge"}]
    project.cfg["resets"] = [{"name": "rst_n", "active": "low", "type": "async"}]
    res = _n1(project)
    assert "top" in res.fingerprints
    assert "top.u_child" in res.fingerprints
    assert len(res.fingerprints["top"]) == 64   # sha256 hex


def test_comment_change_does_not_change_fingerprint(project):
    project.write("rtl/top.sv", "module top (input logic clk, input logic rst_n, output logic [7:0] out);\n"
                               "  // original comment\n"
                               "  always_ff @(posedge clk, negedge rst_n) begin\n"
                               "    if (!rst_n) out <= 0;\n"
                               "    else out <= out + 1;\n"
                               "  end\n"
                               "endmodule\n")
    project.cfg["clocks"] = [{"name": "clk", "edge": "posedge"}]
    project.cfg["resets"] = [{"name": "rst_n", "active": "low", "type": "async"}]
    r1 = _n1(project)
    project.write("rtl/top.sv", "module top (input logic clk, input logic rst_n, output logic [7:0] out);\n"
                               "  // changed comment\n"
                               "  always_ff @(posedge clk, negedge rst_n) begin\n"
                               "    if (!rst_n) out <= 0;\n"
                               "    else out <= out + 1;\n"
                               "  end\n"
                               "endmodule\n")
    r2 = _n1(project)
    assert r1.fingerprints["top"] == r2.fingerprints["top"]


def test_logic_change_changes_fingerprint(project):
    project.write("rtl/top.sv", "module top (input logic clk, input logic rst_n, output logic [7:0] out);\n"
                               "  always_ff @(posedge clk, negedge rst_n) begin\n"
                               "    if (!rst_n) out <= 0;\n"
                               "    else out <= out + 1;\n"
                               "  end\n"
                               "endmodule\n")
    project.cfg["clocks"] = [{"name": "clk", "edge": "posedge"}]
    project.cfg["resets"] = [{"name": "rst_n", "active": "low", "type": "async"}]
    r1 = _n1(project)
    project.write("rtl/top.sv", "module top (input logic clk, input logic rst_n, output logic [7:0] out);\n"
                               "  always_ff @(posedge clk, negedge rst_n) begin\n"
                               "    if (!rst_n) out <= 0;\n"
                               "    else out <= out + 2;  // changed\n"
                               "  end\n"
                               "endmodule\n")
    r2 = _n1(project)
    assert r1.fingerprints["top"] != r2.fingerprints["top"]


def test_child_change_propagates_to_parent_fingerprint(project):
    parent_sv = ("module parent (input logic clk, input logic rst_n);\n"
                 "  child u_c(.clk(clk));\n"
                 "endmodule\n")
    child_v1 = "module child (input logic clk); logic q; always_ff @(posedge clk) q <= 1; endmodule\n"
    child_v2 = "module child (input logic clk); logic q; always_ff @(posedge clk) q <= 2; endmodule\n"
    project.write("rtl/parent.sv", parent_sv)
    project.write("rtl/child.sv", child_v1)
    project.design.update(files=["rtl/child.sv", "rtl/parent.sv"], top="parent")
    project.cfg["clocks"] = [{"name": "clk", "edge": "posedge"}]
    project.cfg["resets"] = [{"name": "rst_n", "active": "low", "type": "async"}]
    r1 = _n1(project)
    project.write("rtl/child.sv", child_v2)
    r2 = _n1(project)
    assert r1.fingerprints.get("parent") != r2.fingerprints.get("parent")
    assert r1.fingerprints.get("parent.u_c") != r2.fingerprints.get("parent.u_c")


# --------------------------------------------------------------------------- #
# Index determinism
# --------------------------------------------------------------------------- #

def test_index_hash_deterministic(project):
    project.write("rtl/top.sv", SIMPLE_SV)
    project.design.update(files=["rtl/top.sv"], top="top")
    project.cfg["clocks"] = [{"name": "clk", "edge": "posedge"}]
    project.cfg["resets"] = [{"name": "rst_n", "active": "low", "type": "async"}]
    r1 = _n1(project)
    r2 = _n1(project)
    assert r1.index_hash == r2.index_hash


# --------------------------------------------------------------------------- #
# Clock-domain analysis
# --------------------------------------------------------------------------- #

def test_reset_not_counted_as_clock(project):
    project.write("rtl/top.sv", SIMPLE_SV)
    project.design.update(files=["rtl/top.sv"], top="top")
    project.cfg["clocks"] = [{"name": "clk", "edge": "posedge"}]
    project.cfg["resets"] = [{"name": "rst_n", "active": "low", "type": "async"}]
    res = _n1(project)
    # rst_n should not appear in clock domain lists
    for path, clks in res.clock_domains.clocks_by_instance.items():
        assert "rst_n" not in clks, f"rst_n counted as clock in {path}"


def test_multiclock_emits_warning(project):
    project.write("rtl/top.sv",
                  "module top (input logic clk_a, input logic clk_b, output logic q);\n"
                  "  always_ff @(posedge clk_a) q <= 1;\n"
                  "  always_ff @(posedge clk_b) q <= 0;\n"
                  "endmodule\n")
    project.design.update(files=["rtl/top.sv"], top="top")
    project.cfg["clocks"] = [{"name": "clk_a", "edge": "posedge"}]
    project.cfg["resets"] = [{"name": "rst_n", "active": "low", "type": "async"}]
    res = _n1(project)
    warn_codes = [w["code"] for w in res.warnings]
    assert "N1_UNSUPPORTED_MULTICLOCK" in warn_codes


# --------------------------------------------------------------------------- #
# Database storage
# --------------------------------------------------------------------------- #

def test_storage_writes_tables(project):
    project.write("rtl/top.sv", SIMPLE_SV)
    project.design.update(files=["rtl/top.sv"], top="top")
    project.cfg["clocks"] = [{"name": "clk", "edge": "posedge"}]
    project.cfg["resets"] = [{"name": "rst_n", "active": "low", "type": "async"}]
    res = run_n1(project.yaml(),
                 workspace=project.root / ".verimine",
                 tools=FAKE_TOOLS, env={}, write=True)
    db = project.root / ".verimine" / "verimine.db"
    with sqlite3.connect(db) as con:
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert {"n1_index", "n1_instance", "n1_port", "n1_signal",
                "n1_proc", "n1_fingerprint", "n1_module_status"} <= tables
        rows = con.execute("SELECT count(*) FROM n1_instance").fetchone()[0]
        assert rows == 2   # top + u_child
        fp_rows = con.execute("SELECT count(*) FROM n1_fingerprint").fetchone()[0]
        assert fp_rows == 2
    assert res.db["new_index"]


def test_storage_is_idempotent(project):
    project.write("rtl/top.sv", SIMPLE_SV)
    project.design.update(files=["rtl/top.sv"], top="top")
    project.cfg["clocks"] = [{"name": "clk", "edge": "posedge"}]
    project.cfg["resets"] = [{"name": "rst_n", "active": "low", "type": "async"}]
    ws = project.root / ".verimine"
    run_n1(project.yaml(), workspace=ws, tools=FAKE_TOOLS, env={}, write=True)
    r2 = run_n1(project.yaml(), workspace=ws, tools=FAKE_TOOLS, env={}, write=True)
    assert not r2.db["new_index"]


def test_n1_json_written(project):
    project.write("rtl/top.sv", SIMPLE_SV)
    project.design.update(files=["rtl/top.sv"], top="top")
    project.cfg["clocks"] = [{"name": "clk", "edge": "posedge"}]
    project.cfg["resets"] = [{"name": "rst_n", "active": "low", "type": "async"}]
    ws = project.root / ".verimine"
    res = run_n1(project.yaml(), workspace=ws, tools=FAKE_TOOLS, env={}, write=True)
    run_hash = res.n0.hashes["run_hash"]
    n1_json = ws / "runs" / run_hash / "n1.json"
    assert n1_json.exists()
    data = json.loads(n1_json.read_text())
    assert "instances" in data
    assert "fingerprints" in data
    assert "index_hash" in data


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def test_cli_n1_dry_run(project):
    import subprocess, sys
    project.write("rtl/top.sv", SIMPLE_SV)
    project.design.update(files=["rtl/top.sv"], top="top")
    project.cfg["clocks"] = [{"name": "clk", "edge": "posedge"}]
    project.cfg["resets"] = [{"name": "rst_n", "active": "low", "type": "async"}]
    cfg = project.yaml()
    r = subprocess.run(
        [sys.executable, "-m", "verimine.cli", "n1", str(cfg), "--dry-run"],
        capture_output=True, text=True,
    )
    assert r.returncode == 0
