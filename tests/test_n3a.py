"""Tests for N3a: assumption mining (candidates, trace checks, over-strength, assume-guarantee,
confirmation into the config, env_missing attribution)."""
from __future__ import annotations

import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from ruamel.yaml import YAML

from conftest import FAKE_TOOLS
from verimine.issues import StageError
from verimine.n0 import run_n0
from verimine.n2b.engine import find_tools
from verimine.n3.stim import available
from verimine.n3a import apply_decisions, run_n3a
from verimine.sva_text import desugar, past_calls

GOLDEN = Path(__file__).parent / "golden" / "n3a"
EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "fifo"
needs_sim = pytest.mark.skipif(not available("icarus"), reason="Icarus Verilog not installed")
needs_engine = pytest.mark.skipif(not find_tools()[0], reason="sby / yosys / SMT solver not installed")
RESET = [{"name": "rst_n", "active": "low", "type": "async"}]


def _project(root: Path, rtl: str, top: str, **extra) -> Path:
    (root / "rtl").mkdir(parents=True, exist_ok=True)
    shutil.copy(GOLDEN / rtl, root / "rtl" / rtl)
    cfg = {"schema_version": 1, "status": "confirmed", "design": {"top": top, "files": [f"rtl/{rtl}"]},
           "clocks": [{"name": "clk", "edge": "posedge"}], "resets": RESET, **extra}
    p = root / "verimine.yaml"
    with p.open("w") as fh:
        YAML(typ="safe").dump(cfg, fh)
    return p


def _ctl(root: Path) -> Path:
    (root / "tb").mkdir(parents=True)
    shutil.copy(GOLDEN / "tb_ctl.sv", root / "tb" / "tb_ctl.sv")
    return _project(root, "ctl.sv", "ctl", testbench={"files": ["tb/tb_ctl.sv"], "top": "tb_ctl"})


def _run(cfg: Path, **kw):
    kw.setdefault("tools", FAKE_TOOLS)
    kw.setdefault("env", {})
    kw.setdefault("workspace", cfg.parent / ".verimine")
    return run_n3a(cfg, **kw)


def _by_type(res, t):
    return [c for c in res.cands if c.type == t]


def _codes(res):
    return {w["code"] for w in res.warnings}


# --------------------------------------------------------------------------- #
# Acceptance 1: env_missing counterexamples go down once the assumption is in
# --------------------------------------------------------------------------- #

@needs_sim
@needs_engine
def test_handshake_env_missing_drops_after_accepting(tmp_path):
    cfg = _project(tmp_path, "hs_rx.sv", "hs_rx")
    r1 = _run(cfg)
    (hold,) = _by_type(r1, "protocol_hold")
    assert hold.text == "in_valid && !in_ready |=> in_valid"
    assert (hold.status, hold.confidence, hold.recommend) == ("PROPOSED", "高", "accept")
    assert hold.strength["lost_key"] == [] and hold.strength["lost_branch"] == []
    env1 = [e for e in r1.env_missing if e["class"] == "env_missing"]
    assert env1 and all(hold.id in e["violated"] for e in env1)
    assert any(e["check"] == "counter.overflow" for e in env1)

    applied = apply_decisions(cfg, r1, accept_high=True)
    assert applied["accepted"] == [hold.id]
    assumes = YAML(typ="safe").load(cfg.read_text())["assumes"]
    assert assumes == [{"name": hold.name, "expr": hold.text}]

    r2 = _run(cfg)
    assert r2.n3.n2.n1.n0.hashes["build_hash"] != r1.n3.n2.n1.n0.hashes["build_hash"]   # assumes are build inputs
    (hold2,) = _by_type(r2, "protocol_hold")
    assert hold2.status == "ACCEPTED" and "config" in hold2.source
    assert [e for e in r2.env_missing if e["class"] == "env_missing"] == []
    assert len(env1) > 0 == len(r2.env_missing)
    # N3 now generates random stimulus under the hold assumption
    rnd = [c for c in r2.n3.cases if c.kind == "random"]
    assert rnd
    for c in rnd:
        v, r = c.signals["hs_rx.in_valid"], c.signals["hs_rx.in_ready"]
        assert all(v[i + 1] for i in range(c.cycles - 1) if v[i] and not r[i])


# --------------------------------------------------------------------------- #
# Acceptance 2: over-strength check catches a deliberately over-strong assumption
# --------------------------------------------------------------------------- #

@needs_sim
@needs_engine
def test_overstrong_assumption_intercepted(tmp_path):
    cfg = _project(tmp_path, "hs_rx.sv", "hs_rx", assumes=[{"name": "never_valid", "expr": "!in_valid"}])
    res = _run(cfg, classify=False)
    (row,) = [c for c in res.cands if c.name == "never_valid"]
    assert row.type == "config" and row.status == "ACCEPTED"
    assert any("SEEN" in x for x in row.strength["lost_key"])
    assert "N3A_OVERSTRONG" in _codes(res)
    assert res.strength["combined"]["lost_key"]
    assert "N3A_OVERSTRONG_COMBINED" in _codes(res)


@needs_sim
@needs_engine
def test_trace_mining_and_overstrong_range(tmp_path):
    res = _run(_ctl(tmp_path), classify=False)
    assert [c.kind for c in res.n3.cases] == ["testbench"]
    texts = {c.type: [x.text for x in _by_type(res, c.type)] for c in res.cands}
    assert "!(req_a && req_b)" in texts["mutex"]
    assert texts["onehot"] == ["(mode & (mode - 1)) == 0"]
    assert texts["min_hold"] == ["!start && $past(start) |-> $past(start, 2) && $past(start, 3)"]
    assert "!rst_n |-> cfg == 1" in texts["reset_value"]
    rng = {c.text: c for c in _by_type(res, "range")}
    # LONG needs len > 12: the observed range is over-strong and rejected
    lenr = rng["len >= 2 && len <= 10"]
    assert lenr.status == "REJECTED" and "LONG" in lenr.reason
    assert all(c.recommend == "reject" for c in rng.values())
    # one-hot mode only cuts a branch: a warning, still needs confirmation
    (oh,) = _by_type(res, "onehot")
    assert oh.status == "PROPOSED" and oh.recommend == "confirm" and len(oh.strength["lost_branch"]) == 1
    assert oh.strength["lost_key"] == [] and "N3A_OVERSTRONG_BRANCH" in _codes(res)
    (mx,) = _by_type(res, "mutex")
    assert mx.trace["support"] > 0 and mx.trace["violations"] == 0
    assert {c.confidence for c in _by_type(res, "reset_value")} == {"高"}


@needs_sim
def test_user_traces_reject_a_violated_template(tmp_path):
    cfg = _project(tmp_path, "hs_rx.sv", "hs_rx",
                   testbench={"files": ["tb/tb.sv"], "top": "tb"})
    (tmp_path / "tb").mkdir()
    (tmp_path / "tb" / "tb.sv").write_text("""\
`timescale 1ns/1ps
module tb;
  reg clk = 0, rst_n = 0, in_valid = 0; reg [7:0] in_data = 0; wire in_ready; wire [7:0] last;
  hs_rx u (.clk(clk), .rst_n(rst_n), .in_valid(in_valid), .in_data(in_data), .in_ready(in_ready), .last(last));
  always #5 clk = ~clk;
  initial begin
    repeat (2) @(negedge clk); rst_n = 1;
    repeat (6) begin @(negedge clk); in_valid = 1; @(negedge clk); in_valid = 0; @(negedge clk); end
    $finish;
  end
endmodule
""")
    res = _run(cfg, formal_checks=False, write=False)
    (hold,) = _by_type(res, "protocol_hold")
    assert hold.status == "REJECTED" and "用户轨迹违反" in hold.reason
    assert hold.trace["violations"] > 0


# --------------------------------------------------------------------------- #
# Assume-guarantee
# --------------------------------------------------------------------------- #

@needs_sim
@needs_engine
def test_assume_guarantee_on_fifo(tmp_path):
    shutil.copytree(EXAMPLE, tmp_path / "fifo", ignore=shutil.ignore_patterns(".verimine"))
    res = _run(tmp_path / "fifo" / "verimine.yaml", classify=False)
    st = {(c.scope, c.text): c.status for c in res.cands}
    assert st[("fifo_top", "full |-> !push")] == "ACCEPTED"                  # the config's assume, merged
    assert st[("fifo_top", "empty |-> !pop")] == "PROPOSED"
    assert st[("fifo_top.u_ctrl", "full |-> !push")] == "DISCHARGED"         # the top assume guarantees it
    assert st[("fifo_top.u_ctrl", "empty |-> !pop")] == "OPEN"               # nothing above constrains pop
    assert [c.name for c in res.cands if c.type == "config"] == []            # merged, no duplicate row
    assert "N3A_OPEN" in _codes(res)
    md = (res.n3.n2.n1.n0.workspace / "runs" / res.n3.n2.n1.n0.hashes["run_hash"] / "n3a_report.md").read_text()
    assert "## OPEN：未被上层保证的子模块假设" in md and "fifo_top.u_ctrl" in md
    with pytest.raises(StageError):
        sub = next(c for c in res.cands if c.status == "OPEN")
        apply_decisions(tmp_path / "fifo" / "verimine.yaml", res, accept=[sub.id])


# --------------------------------------------------------------------------- #
# Decisions, storage, CLI
# --------------------------------------------------------------------------- #

@needs_sim
def test_decisions_persist_and_storage(tmp_path):
    cfg = _ctl(tmp_path)
    r1 = _run(cfg, formal_checks=False)
    mx = _by_type(r1, "mutex")[0]
    rng = next(c for c in _by_type(r1, "range") if c.status == "PROPOSED")
    out = apply_decisions(cfg, r1, accept=[mx.id], reject=[rng.id], reason="可配置")
    assert out == {"accepted": [mx.id], "rejected": [rng.id], "warnings": []}
    data = YAML(typ="safe").load(cfg.read_text())
    assert data["assumes"] == [{"name": mx.name, "expr": mx.text}]
    run_n0(cfg, tools=FAKE_TOOLS, env={}, write=False)                         # still a valid config
    r2 = _run(cfg, formal_checks=False)
    by_key = {c.key: c for c in r2.cands}
    assert by_key[mx.key].status == "ACCEPTED"
    assert by_key[rng.key].status == "REJECTED" and "可配置" in by_key[rng.key].reason
    with sqlite3.connect(tmp_path / ".verimine" / "verimine.db") as con:
        assert con.execute("SELECT count(*) FROM n3a_assume WHERE input_hash=?", (r2.input_hash,)).fetchone()[0] == \
            len(r2.cands)
        assert con.execute("SELECT status FROM n3a_decision WHERE key=?", (rng.key,)).fetchone() == ("REJECTED",)
    with pytest.raises(StageError):
        apply_decisions(cfg, r2, accept=["N3A_9999"])


def test_assume_text_rewrites():
    assert desugar("v && !r |=> $stable(d[3:0])") == "v && !r |=> ((d[3:0]) == $past(d[3:0]))"
    assert desugar("$rose(a) |-> b") == "((a) && !$past(a)) |-> b"
    assert past_calls("$past(x, 3) && $past(f(a, b))") == [(0, 11, "x", 3), (15, 29, "f(a, b)", 1)]


@needs_sim
def test_cli_n3a(tmp_path):
    cfg = _project(tmp_path, "hs_rx.sv", "hs_rx")
    r = subprocess.run([sys.executable, "-m", "verimine.cli", "n3a", str(cfg), "--dry-run", "--no-formal"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "N3a 完成" in r.stdout and "协议保持" in r.stdout
