"""Tests for N2b: structural checks (templates -> assertions/covers -> engine -> report)."""
from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from ruamel.yaml import YAML

from conftest import FAKE_TOOLS
from verimine.n2b import run_n2b
from verimine.n2b.engine import find_tools
from verimine.n2b.props import CLASSES, Prop, const, op, ref
from verimine.n2b.regmap import parse_ipxact
from verimine.n2b.render import delivery, verification, wildcard_mask

GOLDEN = Path(__file__).parent / "golden" / "n2b"
EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "fifo"
HAVE_ENGINE = find_tools()[0]
needs_engine = pytest.mark.skipif(not HAVE_ENGINE, reason="sby / yosys / SMT solver not installed")
FORMAL_FILES = ["dead_code.sv", "fsm.sv", "array_arith.sv", "x_drive.sv", "control.sv", "structure.sv",
                "regs.sv", "top.sv"]


def _write_project(root: Path, files: list[str], top: str, **extra) -> Path:
    (root / "rtl").mkdir(parents=True, exist_ok=True)
    for f in files:
        shutil.copy(GOLDEN / f, root / "rtl" / f)
    cfg = {"schema_version": 1, "status": "confirmed",
           "design": {"top": top, "files": [f"rtl/{f}" for f in files if f.endswith(".sv")]},
           "clocks": [{"name": "clk", "edge": "posedge"}], **extra}
    p = root / "verimine.yaml"
    with p.open("w") as fh:
        YAML(typ="safe").dump(cfg, fh)
    return p


def _run(cfg: Path, **kw):
    kw.setdefault("tools", FAKE_TOOLS)
    kw.setdefault("env", {})
    kw.setdefault("write", False)
    return run_n2b(cfg, **kw)


@pytest.fixture(scope="module")
def golden(tmp_path_factory):
    root = tmp_path_factory.mktemp("n2b_golden")
    cfg = _write_project(root / "formal", FORMAL_FILES + ["regs.csv"], "golden_top",
                         resets=[{"name": "rst_n", "active": "low", "type": "async"}],
                         registers={"file": "rtl/regs.csv", "scope": "golden_top.u_regs", "addr": "addr",
                                    "write": "we"})
    static = _write_project(root / "static", ["static_only.sv"], "static_top")
    return _run(cfg), _run(static)


def _matches(f, e) -> bool:
    return f.check == e["check"] and f.inst == e["inst"] and e["match"] in f.message


# --------------------------------------------------------------------------- #
# Acceptance: every class detects its known defect; false positives per class
# --------------------------------------------------------------------------- #

def test_golden_every_class_detected_and_no_false_positives(golden):
    exp = json.loads((GOLDEN / "expected.json").read_text())
    findings = [f for r in golden for f in r.findings]
    wanted = [e for e in exp["detections"] if HAVE_ENGINE or not e.get("formal")]
    missed = [e for e in wanted if not any(_matches(f, e) for f in findings)]
    assert missed == [], missed
    found = {e["klass"] for e in wanted}
    assert found == set(CLASSES), set(CLASSES) - found          # spec: every class at least one
    explained = exp["detections"] + exp["also_true"]
    fp = [f for f in findings if f.status != "REFUTED" and not any(_matches(f, e) for e in explained)]
    per_class = {k: sum(1 for f in fp if f.klass == k) for k in CLASSES}
    assert fp == [], (per_class, [(f.check, f.inst, f.message) for f in fp])


@needs_engine
def test_golden_failed_assertions_are_candidate_defects(golden):
    res = golden[0]
    defects = res.defects
    assert defects and all(f.status == "FAILED" and f.evidence == "formal" for f in defects)
    assert res.findings[: len(defects)] == defects                 # shown first
    for f in defects:
        p = next(p for p in res.props if p.id == f.props[0])
        assert p.trace.endswith(".vcd") and Path(p.trace).exists()
    # clean modules: nothing fails, the key invariants are proved
    st = {(p.inst, p.check): p.status for p in res.props if p.inst.endswith("_ok")}
    assert "FAILED" not in st.values()
    assert st[("golden_top.u_fifo_ok", "fifo.bound")] == "PROVEN"
    assert st[("golden_top.u_xd_ok", "x.bus_conflict")] == "PROVEN"


@needs_engine
def test_static_and_formal_evidence_merge(golden):
    res = golden[0]
    lost = next(f for f in res.findings if f.check == "fsm.unreachable_state" and "LOST" in f.message)
    assert lost.evidence == "static+formal" and lost.status == "UNREACHABLE"
    hang = next(f for f in res.findings if f.check == "fsm.deadlock" and "HANG" in f.message)
    assert hang.evidence == "static+formal"


def test_blocked_design_still_gets_static_checks(golden):
    res = golden[1]
    assert res.n2.vdg.modeled == []                              # Yosys cannot read it: all BLOCKED
    assert any(w["code"] == "N2B_NO_FORMAL" for w in res.warnings)
    assert res.props == []
    assert {f.check for f in res.findings} == {"timing.comb_loop", "timing.latch", "x.multi_driver"}


def test_delivery_version_compiles(golden):
    res = golden[0]
    assert res.delivery_check == {"ok": True, "errors": []}
    assert "bind golden_top.u_fifo_bug" in res.bind_file
    assert "assert property (@(posedge clk) disable iff (!rst_n)" in res.bind_file


# --------------------------------------------------------------------------- #
# The fifo example
# --------------------------------------------------------------------------- #

@needs_engine
def test_fifo_example_clean_and_proved(tmp_path):
    shutil.copytree(EXAMPLE, tmp_path / "fifo", ignore=shutil.ignore_patterns(".verimine"))
    res = run_n2b(tmp_path / "fifo" / "verimine.yaml", tools=FAKE_TOOLS, env={}, write=False)
    assert res.findings == []
    kinds = {p.check for p in res.props}
    assert {"fifo.overflow", "fifo.underflow", "fifo.bound", "counter.overflow"} <= kinds
    assert all(p.status == "PROVEN" for p in res.props if p.kind == "assert")
    assert all(p.status == "REACHED" for p in res.props if p.kind == "cover")
    # the user's assume (full |-> !push) is part of the verification environment
    assert res.engine.warnings == []


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

def test_two_renderings_of_one_property():
    p = Prop("assert", "fifo.overflow", "top.u", "m", op("==", ref("cnt"), op("$past", ref("cnt"))),
             guard=op("&&", ref("full"), ref("push")), next=True, clock={"name": "clk", "edge": "posedge"},
             resets={"rst_n": "low"}, id="N2B_0001")
    assert delivery(p) == ("N2B_0001: assert property (@(posedge clk) disable iff (!rst_n) "
                           "(full && push) |=> (cnt == $past(cnt)));")
    v = verification(p, "vm_1")
    assert v.startswith("always @(posedge clk) if (!$initstate && rst_n && vm_n2b_pv_clk && $past(rst_n))")
    assert v.endswith("vm_1: assert(!($past((full && push))) || ((cnt == $past(cnt))));")
    c = Prop("cover", "dead.branch", "top", "top", op("==", ref("s"), const(3)), id="N2B_0002")
    assert delivery(c) == "always_comb if (1'b1) N2B_0002: cover ((s == 3));"


def test_wildcard_equality_for_yosys():
    assert wildcard_mask("2'b1?") == (2, 0b10, 0b10)
    assert wildcard_mask("4'b1z0?") == (4, 0b1010, 0b1000)
    t = op("==?", ref("req"), {"op": "xz", "args": [], "x": False, "z": True, "text": "2'b1?"})
    p = Prop("assert", "control.case_parallel", "top", "top", t, id="N2B_0003")
    assert "((req & 2'd2) == 2'd2)" in verification(p, "vm_0")
    assert "(req ==? 2'b1?)" in delivery(p)


# --------------------------------------------------------------------------- #
# Without an engine, storage, CLI, inputs
# --------------------------------------------------------------------------- #

def test_formal_off_keeps_static_results(tmp_path):
    cfg = _write_project(tmp_path, FORMAL_FILES, "golden_top",
                         resets=[{"name": "rst_n", "active": "low", "type": "async"}])
    res = _run(cfg, formal=False)
    assert {p.status for p in res.props} == {"NOT_RUN"}
    assert not res.defects
    assert any(f.check == "fsm.unreachable_state" for f in res.findings)
    assert any(f.check == "dead.const_cond" for f in res.findings)


def test_storage_report_and_idempotency(tmp_path):
    shutil.copytree(EXAMPLE, tmp_path / "fifo", ignore=shutil.ignore_patterns(".verimine"))
    cfg = tmp_path / "fifo" / "verimine.yaml"
    ws = tmp_path / "ws"
    r1 = run_n2b(cfg, workspace=ws, tools=FAKE_TOOLS, env={}, write=True, formal=HAVE_ENGINE)
    assert r1.db["new_result"]
    with sqlite3.connect(ws / "verimine.db") as con:
        row = con.execute("SELECT n2b_hash, n2_input_hash FROM n2b_run WHERE input_hash=?",
                          (r1.input_hash,)).fetchone()
        assert row == (r1.n2b_hash, r1.n2.input_hash)
        assert con.execute("SELECT count(*) FROM n2b_property").fetchone()[0] == len(r1.props)
    run = ws / "runs" / r1.n2.n1.n0.hashes["run_hash"]
    assert json.loads((run / "n2b.json").read_text())["n2b_hash"] == r1.n2b_hash
    assert (run / "n2b_bind.sv").read_text() == r1.bind_file
    md = (run / "n2b_report.md").read_text()
    assert "# 结构性检查报告" in md and "## 候选 RTL 缺陷" in md and "## 按检查类汇总" in md
    r2 = run_n2b(cfg, workspace=ws, tools=FAKE_TOOLS, env={}, write=True, formal=HAVE_ENGINE)
    assert not r2.db["new_result"]
    assert (r2.input_hash, r2.n2b_hash) == (r1.input_hash, r1.n2b_hash)


def test_register_map_is_an_input(tmp_path):
    cfg = _write_project(tmp_path, ["regs.sv", "regs.csv"], "regs",
                         resets=[{"name": "rst_n", "active": "low", "type": "async"}],
                         registers={"file": "rtl/regs.csv", "addr": "addr", "write": "we"})
    r1 = _run(cfg, formal=False)
    assert any(f.check == "reg.reset" for f in r1.findings)
    (tmp_path / "rtl" / "regs.csv").write_text((GOLDEN / "regs.csv").read_text().replace("CTRL,0x00,8,RW,0x01",
                                                                                         "CTRL,0x00,8,RW,0x00"))
    r2 = _run(cfg, formal=False)
    assert r2.input_hash != r1.input_hash
    assert not any(f.check == "reg.reset" and "CTRL" in f.message for f in r2.findings)


def test_ipxact_register_map(tmp_path):
    x = tmp_path / "regs.xml"
    x.write_text("""<?xml version="1.0"?>
<ipxact:component xmlns:ipxact="http://www.accellera.org/XMLSchema/IPXACT/1685-2014">
 <ipxact:memoryMaps><ipxact:memoryMap><ipxact:addressBlock>
  <ipxact:register><ipxact:name>CTRL</ipxact:name><ipxact:addressOffset>0x0</ipxact:addressOffset>
   <ipxact:size>8</ipxact:size><ipxact:access>read-write</ipxact:access>
   <ipxact:field><ipxact:resets><ipxact:reset><ipxact:value>0x1</ipxact:value></ipxact:reset></ipxact:resets></ipxact:field>
  </ipxact:register>
  <ipxact:register><ipxact:name>STATUS</ipxact:name><ipxact:addressOffset>4</ipxact:addressOffset>
   <ipxact:size>8</ipxact:size><ipxact:access>read-only</ipxact:access></ipxact:register>
 </ipxact:addressBlock></ipxact:memoryMap></ipxact:memoryMaps>
</ipxact:component>""")
    regs = parse_ipxact(x)
    assert [(r.name, r.address, r.width, r.access, r.reset) for r in regs] == [
        ("CTRL", 0, 8, "RW", 1), ("STATUS", 4, 8, "RO", None)]


def test_n1_case_entries_and_xz_constants(project):
    from verimine.n1 import run_n1

    project.write("rtl/top.sv", """\
module top(input logic [1:0] s, input logic [3:0] a, output logic [3:0] y);
  always_comb begin
    y = '0;
    unique casez (s)
      2'b1?: y = a;
      2'b01: y = 'x;
    endcase
  end
endmodule
""")
    n1 = run_n1(project.yaml(), tools=FAKE_TOOLS, env={}, write=False)
    (inst,) = n1.elaboration.instances
    a = next(a for a in inst.assigns if a.cond_stack)
    c = a.cond_stack[0]
    assert (c["condition"], c["check"], c["has_default"]) == ("casez", "unique", False)
    assert len(c["all"]) == 2 and c["all"][0]["op"] == "xz" and c["all"][0]["z"]
    x = next(a for a in inst.assigns if a.rhs_tree.get("op") == "xz")
    assert x.rhs_tree["x"] and not x.rhs_tree["z"]


def test_cli_n2b_dry_run(tmp_path):
    shutil.copytree(EXAMPLE, tmp_path / "fifo", ignore=shutil.ignore_patterns(".verimine"))
    r = subprocess.run([sys.executable, "-m", "verimine.cli", "n2b", str(tmp_path / "fifo" / "verimine.yaml"),
                        "--dry-run", "--no-formal"], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "N2b 完成" in r.stdout
