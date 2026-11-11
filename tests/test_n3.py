"""Tests for N3: trace preparation (sources, sampling, trimming, cases, split, coverage, storage)."""
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
from verimine.n2b.engine import find_tools
from verimine.n3 import run_n3
from verimine.n3 import wave as wave_mod
from verimine.n3.cover import _Unknown, evaluate
from verimine.n3.stim import assume_constraints, available

GOLDEN = Path(__file__).parent / "golden" / "n3"
EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "fifo"
needs_sim = pytest.mark.skipif(not available("icarus"), reason="Icarus Verilog not installed")
needs_engine = pytest.mark.skipif(not find_tools()[0], reason="sby / yosys / SMT solver not installed")
RESET = [{"name": "rst_n", "active": "low", "type": "async"}]


def _fifo(tmp_path: Path, **extra) -> Path:
    shutil.copytree(EXAMPLE, tmp_path / "fifo", ignore=shutil.ignore_patterns(".verimine"))
    cfg = tmp_path / "fifo" / "verimine.yaml"
    if extra:
        y = YAML()
        data = y.load(cfg.read_text())
        data.update(extra)
        with cfg.open("w") as fh:
            y.dump(data, fh)
    return cfg


def _lock(tmp_path: Path, assumes: bool = True) -> Path:
    (tmp_path / "rtl").mkdir(parents=True)
    shutil.copy(GOLDEN / "lock.sv", tmp_path / "rtl" / "top.sv")
    cfg = {"schema_version": 1, "status": "confirmed", "design": {"top": "top", "files": ["rtl/top.sv"]},
           "clocks": [{"name": "clk", "edge": "posedge"}], "resets": RESET}
    if assumes:
        cfg["assumes"] = [{"name": "one_req", "expr": "!(req_a && req_b)"}]
    p = tmp_path / "verimine.yaml"
    with p.open("w") as fh:
        YAML(typ="safe").dump(cfg, fh)
    return p


def _run(cfg: Path, **kw):
    kw.setdefault("tools", FAKE_TOOLS)
    kw.setdefault("env", {})
    kw.setdefault("write", False)
    return run_n3(cfg, **kw)


def _codes(res) -> set[str]:
    return {w["code"] for w in res.warnings}


# --------------------------------------------------------------------------- #
# Generated stimulus
# --------------------------------------------------------------------------- #

@needs_sim
def test_random_stimulus_respects_assumes_and_sampling(tmp_path):
    res = _run(_fifo(tmp_path))
    assert {c.kind for c in res.cases} == {"random"}
    assert res.legality() == {"user": 0, "constrained": len(res.cases), "unknown": 0}
    full_seen = 0
    for c in res.cases:
        s = c.signals
        push, pop, full, empty, cnt = (s[f"fifo_top.{x}"] for x in ("push", "pop", "full", "empty", "u_ctrl.count"))
        for i in range(c.cycles):
            assert not (full[i] and push[i])             # assume full |-> !push held in every cycle
            full_seen += full[i]
            if i + 1 < c.cycles:                          # sampled values: the row is what the edge consumed
                assert cnt[i + 1] == cnt[i] + (push[i] and not full[i]) - (pop[i] and not empty[i])
        assert c.detail["stuck_at"] is None
    assert full_seen > 0
    assert res.quality["verdict"] == "ok"


@needs_sim
def test_conservative_stimulus_without_assumes(tmp_path):
    res = _run(_lock(tmp_path, assumes=False))
    assert {c.kind for c in res.cases} == {"idle", "toggle"}
    assert {c.legality for c in res.cases} == {"unknown"}
    assert {"N3_NO_ASSUMES", "N3_LOW_COVERAGE"} <= _codes(res)
    assert not res.explore["ran"]                        # no assumes: no formal exploration either
    toggles = [c for c in res.cases if c.kind == "toggle"]
    assert len(toggles) == 19                            # start + key[15:0] + req_a + req_b
    assert res.quality["verdict"] == "low"


def test_assumes_enforceable_in_a_testbench():
    cons, prev, bad, hist = assume_constraints([
        {"name": "a", "expr": "full |-> !push"},
        {"name": "b", "expr": "req |=> u_arb.busy"},
        {"name": "c", "expr": "valid ##1 ready"},
        {"name": "d", "expr": "valid && !ready |=> $stable(data)"},
        {"name": "e", "expr": "!x && $past(x) |-> $past(x, 2)"},
    ])
    assert cons == ["(!(full) || (!push))", "(!vm_prev_0 || (dut.u_arb.busy))",
                    "(!vm_prev_1 || (((data) == vm_h0_1)))", "(!(!x && vm_h1_1) || (vm_h1_2))"]
    assert prev == ["req", "valid && !ready"] and bad == ["c"]
    assert hist == [("data", 1), ("x", 2)]


@needs_sim
@needs_engine
def test_formal_exploration_closes_gaps_and_proves_unreachable(tmp_path):
    res = _run(_lock(tmp_path))
    assert res.explore["ran"] and res.explore["reached"] >= 1
    formal = [c for c in res.cases if c.kind == "formal"]
    assert formal and all(c.legality == "constrained" for c in formal)
    assert any("16'hBEEF" in t for c in formal for t in c.detail["targets"])
    s = res.coverage.summary()
    assert s["branch"]["ratio"] == 1.0 and s["state"]["ratio"] == 1.0 and s["state"]["transition_ratio"] == 1.0
    g = res.coverage.gaps()
    assert [x["name"] for x in g["unreachable_states"]] == ["ORPHAN"]   # forbidden by the assume
    assert g["states"] == [] and g["branches"] == [] and g["transitions"] == []
    # without exploration the BEEF branch stays a gap
    plain = _run(tmp_path / "verimine.yaml", explore_gaps=False)
    assert any("16'hBEEF" in b["branch"] for b in plain.coverage.gaps()["branches"])


# --------------------------------------------------------------------------- #
# User sources
# --------------------------------------------------------------------------- #

@needs_sim
def test_user_testbench_cut_by_reset_and_deduped(tmp_path):
    cfg = _fifo(tmp_path, testbench={"files": ["tb/tb_fifo.sv"], "top": "tb_fifo",
                                      "runs": [{"name": "t0", "args": []}, {"name": "t1", "args": ["+test=1"]}]})
    (tmp_path / "fifo" / "tb").mkdir()
    shutil.copy(GOLDEN / "tb_fifo.sv", tmp_path / "fifo" / "tb" / "tb_fifo.sv")
    res = _run(cfg)
    assert [s["scope"] for s in res.sources] == ["vm_n3_root.tb_fifo.u_dut"] * 2
    # t1 runs test 1 again (same trace as t0: dropped as a duplicate) and then test 2
    assert [(c.id, c.kind, c.legality) for c in res.cases] == [("tb:t0", "testbench", "user"),
                                                               ("tb:t1#1", "testbench", "user")]
    assert res.sources[1]["trim"]["reset"] == 4
    assert max(res.cases[1].signals["fifo_top.u_ctrl.count"]) == 16
    assert {c.set for c in res.cases} == {"mine", "val"}


def test_user_waveform_mapping_trimming_and_cases(tmp_path):
    (tmp_path / "rtl").mkdir()
    shutil.copy(GOLDEN / "wave_top.sv", tmp_path / "rtl" / "wave_top.sv")
    shutil.copy(GOLDEN / "wave.vcd", tmp_path / "wave.vcd")
    cfg = tmp_path / "verimine.yaml"
    with cfg.open("w") as fh:
        YAML(typ="safe").dump({"schema_version": 1, "status": "confirmed",
                               "design": {"top": "wave_top", "files": ["rtl/wave_top.sv"]},
                               "clocks": [{"name": "clk", "edge": "posedge"}], "resets": RESET,
                               "waveforms": ["wave.vcd"]}, fh)
    res = _run(cfg)
    (src,) = res.sources
    assert src["scope"] == "bench.u_dut"                  # escaped identifier normalized
    assert src["mapped"] == src["nodes"] == 5             # incl. generate-block signal g(0).t -> g[0].t
    assert src["trim"] == {"rows": 11, "reset": 3, "x": 1, "init": 0, "short": 0, "before_reset": 0}
    q = [c.signals["wave_top.q"] for c in res.cases]
    assert q == [[0, 1], [3, 4], [0, 1, 2]]               # cut at the X input and at the second reset
    assert res.cases[0].signals["wave_top.g[0].t"] == [0, 1]
    assert {c.legality for c in res.cases} == {"user"}
    assert not any(c.kind != "waveform" for c in res.cases)   # user waveforms: no generated stimulus


@pytest.mark.skipif(wave_mod._wellen is None, reason="pywellen not installed")
def test_builtin_vcd_reader_agrees_with_wellen():
    a = wave_mod._WellenWave(GOLDEN / "wave.vcd")
    b = wave_mod._VcdWave(GOLDEN / "wave.vcd")
    assert a.vars == b.vars
    for n in a.vars:
        assert a.changes(n) == b.changes(n), n


# --------------------------------------------------------------------------- #
# Split, coverage evaluator
# --------------------------------------------------------------------------- #

@needs_sim
def test_split_by_whole_cases(tmp_path):
    cfg = _fifo(tmp_path)
    res = _run(cfg)
    mine = {c.hash for c in res.cases if c.set == "mine"}
    val = {c.hash for c in res.cases if c.set == "val"}
    assert mine and val and not (mine & val)
    assert sum(c.cycles for c in res.cases) == res.sets()["mine"]["cycles"] + res.sets()["val"]["cycles"]
    again = _run(cfg)
    assert [(c.hash, c.set) for c in again.cases] == [(c.hash, c.set) for c in res.cases]


def test_evaluator():
    w = {"a": 4, "b": 4, "s": 2}
    ev = lambda t, env: evaluate(t, env, w)  # noqa: E731
    ref = lambda n: {"ref": n}  # noqa: E731
    c = lambda v: {"const": v, "text": str(v)}  # noqa: E731
    wild = {"op": "==?", "args": [ref("s"), {"op": "xz", "args": [], "x": False, "z": True, "text": "2'b1?"}]}
    assert ev(wild, {"s": 3}) == 1 and ev(wild, {"s": 1}) == 0
    wrap = {"op": "==", "args": [{"op": "+", "args": [ref("a"), c(1)]}, c(0)]}
    assert ev(wrap, {"a": 15}) == 1                      # 4-bit context: 15 + 1 wraps to 0
    cat = {"op": "{}", "args": [ref("a"), ref("b")]}
    assert ev(cat, {"a": 1, "b": 2}) == 0x12
    assert ev({"op": "&&", "args": [ref("a"), ref("b")]}, {"a": 0, "b": None}) == 0
    with pytest.raises(_Unknown):
        ev({"op": "||", "args": [ref("a"), ref("b")]}, {"a": 0, "b": None})


# --------------------------------------------------------------------------- #
# Storage, reuse, CLI
# --------------------------------------------------------------------------- #

@needs_sim
def test_storage_reuse_and_determinism(tmp_path):
    cfg = _fifo(tmp_path)
    ws = tmp_path / "ws"
    r1 = run_n3(cfg, workspace=ws, tools=FAKE_TOOLS, env={}, write=True)
    assert r1.db == {"new_run": True, "cached": False}
    with sqlite3.connect(ws / "verimine.db") as con:
        assert con.execute("SELECT traces_hash FROM n3_run WHERE input_hash=?", (r1.input_hash,)).fetchone() == (
            r1.traces_hash,)
        assert con.execute("SELECT count(*) FROM n3_case").fetchone()[0] == len(r1.cases)
    assert all((ws / "traces" / f"{c.hash}.json.gz").exists() for c in r1.cases)
    run = ws / "runs" / r1.n2.n1.n0.hashes["run_hash"]
    out = json.loads((run / "n3.json").read_text())
    assert out["traces_hash"] == r1.traces_hash and "gaps" in out
    assert "# 轨迹准备报告" in (run / "n3_report.md").read_text()
    r2 = run_n3(cfg, workspace=ws, tools=FAKE_TOOLS, env={}, write=True)
    assert r2.db["cached"] and r2.traces_hash == r1.traces_hash
    assert r2.coverage.summary() == r1.coverage.summary()
    r3 = run_n3(cfg, workspace=ws, tools=FAKE_TOOLS, env={}, write=True, reuse=False)   # simulate again
    assert r3.traces_hash == r1.traces_hash


@needs_sim
def test_seed_and_assumes_are_inputs(tmp_path):
    cfg = _fifo(tmp_path)
    r1 = _run(cfg)
    y = YAML()
    data = y.load(cfg.read_text())
    data["run"]["seed"] = 2
    with cfg.open("w") as fh:
        y.dump(data, fh)
    r2 = _run(cfg)
    assert r2.input_hash != r1.input_hash and r2.traces_hash != r1.traces_hash


@needs_sim
def test_cli_n3_dry_run(tmp_path):
    cfg = _fifo(tmp_path)
    r = subprocess.run([sys.executable, "-m", "verimine.cli", "n3", str(cfg), "--dry-run", "--no-formal"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "N3 完成" in r.stdout and "S_mine" in r.stdout
