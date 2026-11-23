"""Tests for N5: SVA generation (delivery and verification versions, antecedent covers, names,
the capability matrix and degradations, the three-way differential test), storage and CLI."""
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
from verimine import engines
from verimine.n2b.engine import find_tools
from verimine.n2b.pipeline import _check_delivery
from verimine.n2b.props import AND, EQ, NOT, PAST, const, ref
from verimine.n3.stim import available
from verimine.n4.ir import Cand
from verimine.n5 import render as R
from verimine.n5 import run_n5
from verimine.n5.render import props_of

GOLDEN = Path(__file__).parent / "golden" / "n4"
EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "fifo"
needs_sim = pytest.mark.skipif(not available("icarus"), reason="Icarus Verilog not installed")
needs_diff = pytest.mark.skipif(not (find_tools()[0] and available("verilator")),
                                reason="differential test needs Verilator and sby / yosys / an SMT solver")
FAST = {"n5_diff_cases": 2, "n5_diff_cycles": 30}
CLK = {"name": "clk", "edge": "posedge"}
RST = {"rst_n": "low"}


def _src(root: Path, **params) -> Path:
    (root / "rtl").mkdir(parents=True)
    (root / "tb").mkdir()
    shutil.copy(GOLDEN / "src.sv", root / "rtl" / "src.sv")
    shutil.copy(GOLDEN / "tb_src.sv", root / "tb" / "tb_src.sv")
    cfg = {"schema_version": 1, "status": "confirmed", "design": {"top": "src", "files": ["rtl/src.sv"]},
           "clocks": [CLK], "resets": [{"name": "rst_n", "active": "low", "type": "async"}],
           "testbench": {"files": ["tb/tb_src.sv"], "top": "tb_src"}, "run": {"params": FAST | params}}
    p = root / "verimine.yaml"
    with p.open("w") as fh:
        YAML(typ="safe").dump(cfg, fh)
    return p


def _run(cfg: Path, **kw):
    kw.setdefault("tools", FAKE_TOOLS)
    kw.setdefault("env", {})
    kw.setdefault("workspace", cfg.parent / ".verimine")
    return run_n5(cfg, **kw)


def _cand(ante, cons, nxt=False, t="t", resets=RST) -> Cand:
    return Cand(t, "top", "top", ante, cons, nxt, ["A"], clock=CLK, resets=dict(resets))


# --------------------------------------------------------------------------- #
# Steps 1-3, 5-6: rendering
# --------------------------------------------------------------------------- #

def test_render_two_versions_names_covers_and_degradations():
    cs = [_cand(AND(ref("v"), NOT(ref("r"))), EQ(ref("d"), PAST(ref("d"))), True, "handshake_data"),
          _cand(None, NOT(AND(ref("a"), ref("b"))), t="mutex"),
          _cand(ref("c"), NOT(AND(ref("a"), ref("b"))), t="mutex"),     # same name base: made unique
          _cand(ref("x"), EQ(ref("q"), const(0)), True, "reset_value", resets={})]
    rs = props_of(cs)
    assert [r.name for r in rs] == ["handshake_data_d", "handshake_data_d_c", "mutex_a_b", "mutex_a_b_2",
                                    "mutex_a_b_2_c", "reset_value_q", "reset_value_q_c"]
    hd, hd_c = rs[0], rs[1]
    assert hd.delivery == ("handshake_data_d: assert property (@(posedge clk) disable iff (!rst_n) "
                           "(v && !(r)) |=> (d == $past(d)));")
    # Yosys: monitor, `disable iff` as a gate, |=> through $past; both cycles out of reset
    assert hd.verification == ("always @(posedge clk) if (rst_n && vm_n2b_pv_clk && $past(rst_n)) "
                               "handshake_data_d: assert(!($past((v && !(r)))) || ((d == $past(d))));")
    assert hd.degraded == ["monitor", "gate", "past"]
    assert hd.window == 2 and hd.fragment == "bounded_implication"
    # cover of the antecedent, for vacuity in N7; invariants get none
    assert hd_c.kind == "cover" and hd_c.delivery.startswith("handshake_data_d_c: cover property") \
        and "(v && !(r))" in hd_c.delivery
    assert not any(r.name == "mutex_a_b_c" for r in rs)
    # no reset on reset_value: the first tick is checked (no $initstate gate)
    assert rs[6].verification == "always @(posedge clk) reset_value_q_c: cover(x);"
    # the matrix for other engines: delivery as is on the commercial ones, unknown on pending ones
    assert hd.engines["jaspergold"] == {"degraded": [], "unsupported": []}
    assert hd.engines["galaxfv"]["unsupported"]
    # the simulation copy prints the bench's cycle counter on failure
    assert hd.sim.endswith(f'else $display("VM_FAIL handshake_data_d %0d", {R.CYCLE});')


def test_capability_matrix_decides_what_is_rendered():
    used = engines.features(None, EQ(ref("g[0].x"), PAST(ref("y"), )), True, RST)
    assert used == {"concurrent", "disable_iff", "next", "past", "hier_ref"}
    deg, bad = engines.plan("yosys-sby", used)
    assert deg == ["monitor", "gate", "past"] and bad == ["hier_ref"]
    (r,) = props_of([_cand(None, EQ(ref("g[0].x"), const(1)))])
    assert r.delivery and not r.verification and "生成块内信号" in r.skip
    assert engines.plan("verilator", used) == ([], [])
    for name, caps in engines.CAPS.items():
        assert set(caps) == set(engines.FEATURES) | {"basis", "role"}, name


@needs_sim
def test_delivery_compiles_with_slang(tmp_path):
    res = _run(_src(tmp_path), differential=False)
    n0 = res.n4.n3.n2.n1.n0
    run = tmp_path / ".verimine" / "runs" / n0.hashes["run_hash"]
    text = (run / "n5_sva.sv").read_text() + "\n" + (run / "n5_bind.sv").read_text()
    assert _check_delivery([tmp_path / "rtl" / "src.sv"], [], {}, "src", text, tmp_path / "chk") == \
        {"ok": True, "errors": []}
    assert "N5_NO_DIFF" in {w["code"] for w in res.warnings}
    svh = (run / "n5_verification" / "vm_n5_src.svh").read_text()
    assert "reg vm_n2b_pv_clk" in svh and "handshake_hold_out_valid:" in svh


# --------------------------------------------------------------------------- #
# Step 4: the differential test
# --------------------------------------------------------------------------- #

@needs_diff
def test_differential_consistent(tmp_path):
    # path B adds wrong candidates: their failures must show up identically in all three
    res = _run(_src(tmp_path, n4_path_b=True))
    d = res.diff
    assert d["ran"] and not d["sim_error"] and not d["formal_error"]
    assert d["model"]["compared"] > 500 and d["model"]["mismatches"] == []
    assert {r.diff["status"] for r in res.props} == {"consistent"}
    failing = [r for r in res.props if r.kind == "assert" and r.diff["sim"]]
    assert failing, "the vector should catch at least one wrong path-B candidate"
    for r in failing:
        assert r.diff["ir_sim"] == r.diff["sim"] and r.diff["formal"]["status"] == "FAILED"
        assert r.diff["formal"]["step"] == r.diff["ir_formal"] == r.diff["sim"][0]
    reached = [r for r in res.props if r.kind == "cover" and r.diff["sim"]]
    assert reached and all(r.diff["formal"]["step"] == r.diff["sim"][0] for r in reached)
    # the vector restarts reset between cases: disable iff in the middle is exercised
    assert len(d["segments"]) == 2 and d["segments"][1]["start"] > 0


@needs_diff
def test_differential_fifo_submodule(tmp_path):
    shutil.copytree(EXAMPLE, tmp_path / "fifo", ignore=shutil.ignore_patterns(".verimine"))
    cfg = tmp_path / "fifo" / "verimine.yaml"
    res = _run(cfg)
    assert {r.diff["status"] for r in res.props} == {"consistent"}
    assert {r.cand.scope for r in res.props} == {"fifo_top", "fifo_top.u_ctrl"}
    assert res.diff["model"]["mismatches"] == []


@needs_diff
def test_differential_catches_rendering_errors(tmp_path, monkeypatch):
    real_delivery, real_verif = R.delivery, R.n5_verification

    def bad_delivery(p, name=lambda n: n):               # |=> rendered as |->
        return real_delivery(p, name).replace("|=>", "|->")

    def bad_verification(p, label):                       # `disable iff` dropped
        q = type(p)(**{**p.__dict__, "resets": {}})
        return real_verif(q, label)

    monkeypatch.setattr(R, "delivery", bad_delivery)
    monkeypatch.setattr(R, "n5_verification", bad_verification)
    res = _run(_src(tmp_path))
    st = {r.name: r.diff for r in res.props}
    kind = {r.name: r.kind for r in res.props}
    # |=> rendered as |->: `st == 4 |=> st == 1` and the data hold now fail in simulation, the IR
    # says they hold; only the delivery leg disagrees
    bad_d = {n for n, r in st.items() if r.get("delivery_ok") is False}
    assert {"fsm_transition_st_2", "handshake_data_out_data"} <= bad_d
    assert all(kind[n] == "assert" and st[n]["ir_sim"] == [] and st[n]["sim"] for n in bad_d)
    # without disable iff the formal side checks during reset: a cover is reached in the reset
    # cycles, before the IR's first hit; only the verification leg disagrees
    bad_v = {n for n, r in st.items() if r.get("verification_ok") is False}
    assert bad_v and all(st[n]["formal"]["step"] < st[n]["ir_formal"] for n in bad_v if kind[n] == "cover")
    assert all(st[n]["status"] == "mismatch" for n in bad_d | bad_v)
    assert {n for n, r in st.items() if r["status"] == "mismatch"} == bad_d | bad_v
    assert "N5_RENDER_MISMATCH" in {w["code"] for w in res.warnings}


# --------------------------------------------------------------------------- #
# Storage, determinism, CLI
# --------------------------------------------------------------------------- #

@needs_sim
def test_storage_and_determinism(tmp_path):
    cfg = _src(tmp_path)
    r1 = _run(cfg, differential=False)
    r2 = _run(cfg, differential=False)
    assert r1.input_hash == r2.input_hash and r1.n5_hash == r2.n5_hash
    assert r1.db == {"new_run": True} and r2.db == {"new_run": False}
    with sqlite3.connect(tmp_path / ".verimine" / "verimine.db") as con:
        rows = con.execute("SELECT name, kind, degraded FROM n5_prop WHERE input_hash=?", (r1.input_hash,)).fetchall()
    assert len(rows) == len(r1.props) and all(json.loads(d) for _n, _k, d in rows)
    md = (tmp_path / ".verimine" / "runs" / r1.n4.n3.n2.n1.n0.hashes["run_hash"] / "n5_report.md").read_text()
    assert "## 降级记录" in md and "## 三方差分测试" in md and "jaspergold" in md


@needs_sim
def test_cli_n5(tmp_path):
    cfg = _src(tmp_path)
    r = subprocess.run([sys.executable, "-m", "verimine.cli", "n5", str(cfg), "--dry-run", "--no-diff"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "N5 完成" in r.stdout and "验证版降级" in r.stdout and "未运行" in r.stdout
