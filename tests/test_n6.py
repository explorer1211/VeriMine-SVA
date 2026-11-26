"""Tests for N6: compile check (slang on the delivery version, Yosys on the verification
version), pinning diagnostics on properties, the re-render rounds, ENV_ERROR, the re-run of the
differential test on changed verification texts, storage and CLI."""
from __future__ import annotations

import copy
import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from ruamel.yaml import YAML

from conftest import FAKE_TOOLS
from verimine.n2b.engine import find_tools
from verimine.n2b.props import EQ, Prop, ref
from verimine.n3.stim import available
from verimine.n5 import diff as diff_mod
from verimine.n5 import run_n5
from verimine.n5.pipeline import diff_setup
from verimine.n6 import run_n6
from verimine.n6 import variants as V
from verimine.n6.compile import Diag, bisect, pin, slang_diags, yosys_available
from verimine.n6.pipeline import _n5_params

GOLDEN = Path(__file__).parent / "golden" / "n4"
needs_yosys = pytest.mark.skipif(not yosys_available(), reason="Yosys not installed")
needs_diff = pytest.mark.skipif(not (find_tools()[0] and available("verilator")),
                                reason="differential test needs Verilator and sby / yosys / an SMT solver")
CLK = {"name": "clk", "edge": "posedge"}


def _src(root: Path, clash: bool = False, **params) -> Path:
    (root / "rtl").mkdir(parents=True)
    (root / "tb").mkdir()
    text = (GOLDEN / "src.sv").read_text()
    if clash:   # a signal named like the assertion N5 will call `reset_value_st`
        text = text.replace("output logic five);", "output logic five, output logic reset_value_st);")
        text = text.replace("assign five = hits == 4'd5;", "assign five = hits == 4'd5;\n  assign reset_value_st = st == WAIT;")
    (root / "rtl" / "src.sv").write_text(text)
    shutil.copy(GOLDEN / "tb_src.sv", root / "tb" / "tb_src.sv")
    cfg = {"schema_version": 1, "status": "confirmed", "design": {"top": "src", "files": ["rtl/src.sv"]},
           "clocks": [CLK], "resets": [{"name": "rst_n", "active": "low", "type": "async"}],
           "testbench": {"files": ["tb/tb_src.sv"], "top": "tb_src"},
           "run": {"params": {"n5_diff_cases": 2, "n5_diff_cycles": 30} | params}}
    p = root / "verimine.yaml"
    with p.open("w") as fh:
        YAML(typ="safe").dump(cfg, fh)
    return p


def _run(cfg: Path, **kw):
    kw.setdefault("tools", FAKE_TOOLS)
    kw.setdefault("env", {})
    kw.setdefault("workspace", cfg.parent / ".verimine")
    return run_n6(cfg, **kw)


def _by(res, name):
    return next(p for p in res.props if p.name == name)


def _final_slang(res, root: Path, tmp: Path) -> list[Diag]:
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / "a.sv").write_text(res.delivery)
    (tmp / "b.sv").write_text(res.bind)
    return slang_diags([root / "rtl" / "src.sv"], [], {}, "src", [tmp / "a.sv", tmp / "b.sv"])


# --------------------------------------------------------------------------- #
# Re-renderings
# --------------------------------------------------------------------------- #

def test_variants_keep_the_meaning_and_change_the_form():
    p = Prop("assert", "t", "top", "top", EQ(ref("d"), {"op": "$past", "k": 2, "args": [ref("d")]}),
             guard=ref("v"), next=True, clock=CLK, resets={"rst_n": "low"})
    p.id = "t_d"
    v0 = V.verification(p, "L", 0)
    assert "$past(d, 2)" in v0
    v1 = V.verification(p, "L", 1)
    assert "$past($past(d))" in v1 and v1.replace("$past($past(d))", "$past(d, 2)") == v0
    v2 = V.verification(p, "L", 2)
    assert "$past" not in v2 and V.label_of(v2) == "L"
    decl, check = V.split_verification(v2)
    assert len(decl) == 4    # d, its past, rst_n, the guard
    assert all(x.startswith("reg [$bits(") for x in decl) and check.startswith("always @(posedge clk) if (rst_n && ")
    # $onehot as $countones
    q = Prop("assert", "t", "top", "top", {"op": "call", "name": "$onehot0", "args": [ref("s")]}, clock=CLK)
    assert "$countones(s) <= 1" in V.verification(q, "L", 1)
    # delivery: port names and upward references
    p.delivery = ""
    assert V.delivery_text(p, 1).count("p_d") == 2 and "p_rst_n" in V.delivery_text(p, 1)
    assert "top.d" in V.delivery_text(p, 2) and "@(posedge top.clk)" in V.delivery_text(p, 2)
    sva, bind, smap, bmap = V.assemble([("t_d", p, 2)], lambda i: {})
    assert "module vm_n6_t_d;" in sva and "bind top vm_n6_t_d u_vm_n6_t_d ();" in bind
    assert smap[next(i for i, x in enumerate(sva.split("\n"), 1) if x.strip().startswith("t_d:"))] == ["t_d"]


def test_pin_and_bisect():
    maps = {"a.sv": {3: ["x", "y"], 4: ["x"], 5: ["y"]}}
    ds = [Diag("slang", "a.sv", 4, "E", "bad"), Diag("slang", "a.sv", 3, "Redefinition", "redefinition of 'y'"),
          Diag("slang", "d.sv", 9, "E", "elsewhere"), Diag("slang", "d.sv", 1, "E", "old")]
    pinned, loose = pin(ds, maps, {("E", "d.sv", 1, "old")})
    assert sorted(pinned) == ["x", "y"] and len(pinned["y"]) == 1 and [d.message for d in loose] == ["elsewhere"]
    calls = []
    budget = [20]
    assert bisect(list("abcdefgh"), lambda ns: calls.append(ns) or bool({"c", "g"} & set(ns)), budget) == ["c", "g"]
    assert budget[0] == 20 - len(calls) and len(calls) < 15


# --------------------------------------------------------------------------- #
# Pipeline
# --------------------------------------------------------------------------- #

@needs_yosys
def test_clean_design_passes_round_zero(tmp_path):
    cfg = _src(tmp_path)
    res = _run(cfg, rediff=False)
    c = res.counts()
    assert c["env_error"] == 0 and c["rerendered"] == 0 and c["ok"] == c["props"] == len(res.n5.props)
    assert len(res.rounds) == 1 and res.legs["delivery"]["ran"] and res.legs["verification"]["ran"]
    assert not _final_slang(res, tmp_path, tmp_path / "chk")
    run_dir = tmp_path / ".verimine" / "runs" / res.n5.n4.n3.n2.n1.n0.hashes["run_hash"]
    assert (run_dir / "n6_sva.sv").read_text() == res.delivery
    assert (run_dir / "n6_verification" / "vm_n6_src.svh").is_file() and (run_dir / "n6_report.md").is_file()
    with sqlite3.connect(tmp_path / ".verimine" / "verimine.db") as con:
        assert con.execute("SELECT count(*) FROM n6_prop WHERE status='OK' AND to_n7=1").fetchone()[0] == c["props"]
    # same inputs: same hash, no new run
    again = _run(cfg, rediff=False, n5_result=res.n5)
    assert again.n6_hash == res.n6_hash and again.db == {"new_run": False}


@needs_yosys
def test_label_equal_to_a_signal_is_fixed_by_isolation(tmp_path):
    """Measured: N5 names the reset-value assertion of st `reset_value_st`; with a signal of that
    name in the same checker, slang warns (Redefinition) and the property reading the signal
    no longer resolves; Verilator rejects the file. Round 1 renames the ports."""
    res = _run(_src(tmp_path, clash=True), rediff=False)
    rr = {p.name for p in res.props if p.rerendered}
    assert "reset_value_st" in rr and all(_by(res, n).d_variant == 1 for n in rr)
    assert _by(res, "reset_value_st").history[0]["diags"][0].endswith("redefinition of 'reset_value_st'")
    assert res.counts()["env_error"] == 0 and len(res.rounds) == 2
    assert not _final_slang(res, tmp_path, tmp_path / "chk")
    assert "module vm_n6_reset_value_st (" in res.delivery and ".p_st(st)" in res.bind


@needs_yosys
def test_failures_rerendered_or_env_error(tmp_path, monkeypatch):
    cfg = _src(tmp_path)
    n5 = run_n5(cfg, tools=FAKE_TOOLS, env={}, workspace=tmp_path / ".verimine")
    d0, v0 = V.delivery_text, V.verification

    def delivery_text(p, variant):
        if p.id == "onehot_st" and variant == 0:
            return "onehot_st: assert property (@(posedge clk) nosuch_d);"
        if p.id == "counter_range_beats":
            return "counter_range_beats: assert property (@(posedge clk) beats <= );"
        return d0(p, variant)

    def verification(p, label, variant):
        if p.id == "mutex_out_valid_waiting" and variant == 0:   # Yosys only warns about this one
            return f"always @(posedge clk) if (rst_n) {label}: assert(!nosuch_v);"
        if p.id == "counter_range_beats":                        # and stops reading at this one
            return f"always @(posedge clk) {label}: assert(beats <= );"
        return v0(p, label, variant)

    monkeypatch.setattr(V, "delivery_text", delivery_text)
    monkeypatch.setattr(V, "verification", verification)
    res = _run(cfg, n5_result=n5)

    one = _by(res, "onehot_st")
    assert one.status == "OK" and one.d_variant == 1 and one.v_variant == 0
    assert "undeclared identifier 'nosuch_d'" in one.history[0]["diags"][0]
    mx = _by(res, "mutex_out_valid_waiting")
    assert mx.status == "OK" and mx.v_variant == 1 and "implicitly declared" in mx.history[0]["diags"][0]
    bad = _by(res, "counter_range_beats")
    assert bad.status == "ENV_ERROR" and not bad.to_n7 and bad.d_ok is False and bad.v_ok is False
    assert [(h["leg"], h["round"]) for h in bad.history] == [
        ("delivery", 0), ("verification", 0), ("delivery", 1), ("verification", 1), ("delivery", 2), ("verification", 2)]
    assert "counter_range_beats" not in res.delivery and "counter_range_beats" not in res.verification["src"]
    assert "nosuch" not in res.delivery and "nosuch" not in res.verification["src"]
    assert not _final_slang(res, tmp_path, tmp_path / "chk")
    assert res.counts()["env_error"] == 1 and any(w["code"] == "N6_ENV_ERROR" for w in res.warnings)
    report = (tmp_path / ".verimine" / "runs" / n5.n4.n3.n2.n1.n0.hashes["run_hash"] / "n6_report.md").read_text()
    assert "### counter_range_beats" in report and "独立检查器" in report
    if n5.diff.get("ran"):       # the changed verification text was tested again
        assert mx.rediff.get("status") == "consistent" and res.legs["rediff"]["props"] == 1


@needs_diff
def test_verification_fallbacks_pass_the_differential_test(tmp_path):
    """Variants 1 (expanded) and 2 (explicit history registers) mean what N5's text means:
    the three-way differential test agrees on every property."""
    n5 = run_n5(_src(tmp_path), tools=FAKE_TOOLS, env={}, workspace=tmp_path / ".verimine", differential=False)
    setup = diff_setup(n5.n4, _n5_params(n5))
    assert not isinstance(setup, str)
    for var in (1, 2):
        rs = [copy.copy(r) for r in n5.props if r.verification]
        info = diff_mod.run(rs, workdir=tmp_path / f"rd{var}", params=_n5_params(n5), **setup,
                            render=lambda q, lab: V.verification(q, lab, var))
        assert not info.get("formal_error")
        assert {r.diff.get("status") for r in rs} == {"consistent"}, [(r.name, r.diff) for r in rs]


@needs_yosys
def test_cli(tmp_path):
    cfg = _src(tmp_path)
    r = subprocess.run([sys.executable, "-m", "verimine.cli", "n6", str(cfg), "--dry-run", "--no-diff"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "N6 完成" in r.stdout and "ENV_ERROR 0" in r.stdout
    r = subprocess.run([sys.executable, "-m", "verimine.cli", "n6", str(cfg), "--json", "--dry-run", "--no-diff"],
                       capture_output=True, text=True)
    d = json.loads(r.stdout)
    assert d["stage"] == "N6" and d["counts"]["env_error"] == 0 and d["props"][0]["status"] == "OK"
