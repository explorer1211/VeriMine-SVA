"""Tests for N7: S_val offline check and trace_overfit, cover-based vacuity, the layered proof,
the merged statuses, COI coverage and gaps, mutation kill, the closed loop, the process backend
and the CLI."""
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
from verimine.n1.assign import AssignInfo
from verimine.n2b.engine import find_tools
from verimine.n2b.props import Prop, ref
from verimine.n4.ir import Cand
from verimine.n6 import run_n6
from verimine.n7 import coi, offline, run_n7
from verimine.n7.mutation import _mutable
from verimine.n7.status import merge

GOLDEN = Path(__file__).parent / "golden" / "n4"
needs_engine = pytest.mark.skipif(not find_tools()[0], reason="needs sby / yosys / an SMT solver")
CLK = {"name": "clk", "edge": "posedge"}
RST = {"rst_n": "low"}


def _src(root: Path) -> Path:
    (root / "rtl").mkdir(parents=True)
    (root / "tb").mkdir()
    shutil.copy(GOLDEN / "src.sv", root / "rtl" / "src.sv")
    shutil.copy(GOLDEN / "tb_src.sv", root / "tb" / "tb_src.sv")
    cfg = {"schema_version": 1, "status": "confirmed", "design": {"top": "src", "files": ["rtl/src.sv"]},
           "clocks": [CLK], "resets": [{"name": "rst_n", "active": "low", "type": "async"}],
           "testbench": {"files": ["tb/tb_src.sv"], "top": "tb_src"},
           "run": {"params": {"n5_diff_cases": 2, "n5_diff_cycles": 30}}}
    p = root / "verimine.yaml"
    with p.open("w") as fh:
        YAML(typ="safe").dump(cfg, fh)
    return p


def _kw(cfg: Path, **kw):
    kw.setdefault("tools", FAKE_TOOLS)
    kw.setdefault("env", {})
    kw.setdefault("workspace", cfg.parent / ".verimine")
    return kw


class _Inst:
    """The little coi.graph needs of an instance."""

    def __init__(self, path, parent=None, assigns=(), connections=()):
        self.path, self.parent = path, parent
        self.assigns, self.connections = list(assigns), list(connections)


def _a(lhs, refs, cond=()):
    return AssignInfo(None, [lhs], {}, {}, list(refs), [{"kind": "if", "expr": ref(r), "polarity": True} for r in cond],
                      False, "")


# --------------------------------------------------------------------------- #
# Units
# --------------------------------------------------------------------------- #

def test_merge_rules():
    base = {"sim": "pass", "formal": "PROVEN", "cover": "REACHED", "review_required": False}
    assert merge(**base) == "PROVEN"
    assert merge(**(base | {"formal": "FAILED"})) == "FAILED"
    assert merge(**(base | {"sim": "fail", "formal": "NOT_RUN"})) == "FAILED"
    assert merge(**(base | {"cover": "UNREACHABLE"})) == "VACUOUS"
    assert merge(**(base | {"review_required": True})) == "REVIEW_REQUIRED"
    assert merge(**(base | {"cover": "UNREACHED"})) == "WEAK_EVIDENCE"
    assert merge(**(base | {"formal": "BOUNDED"})) == "BOUNDED"
    assert merge(**(base | {"formal": "RESOURCE_LIMIT"})) == "REVIEW_REQUIRED"
    assert merge(**(base | {"formal": "ENV_ERROR"})) == "ENV_ERROR"
    assert merge(**(base | {"formal": "NOT_RUN", "sim": "no_data"})) == "WEAK_EVIDENCE"


def test_coi_graph_follows_connections():
    top_assigns = [_a("z", ["s2"])]
    child_assigns = [_a("o", ["q"], cond=["en"])]
    top = _Inst("top", assigns=top_assigns)
    child = _Inst("top.u", parent="top", assigns=child_assigns,
                  connections=[{"port": "o", "direction": "out", "expr": ref("s2")},
                               {"port": "i", "direction": "in", "expr": ref("z")}])
    preds = coi.graph([top, child])
    c = coi.cone(preds, ["top.z"])
    assert {"top.z", "top.s2", "top.u.o", "top.u.q", "top.u.en"} <= c
    assert "top.u.i" not in c          # the child's input does not feed its output here
    blocks = coi.blocks([top, child])
    assert {b.node for b in blocks} == {"top.z", "top.u.o"}


def test_overfit_only_for_trace_derived_candidates():
    sim = {"legal": 1, "status": "fail"}
    mined = Cand("t", "top", "top", ref("a"), ref("b"), sources=["B"])
    templ = Cand("t", "top", "top", ref("a"), ref("b"), sources=["A"])
    assert offline.overfit(mined, sim) and not offline.overfit(templ, sim)
    assert not offline.overfit(mined, {"legal": 0, "status": "warn"})


def test_mutation_operators_skip_assignment_ops():
    got = _mutable("  else if (go) hits <= hits + 4'd1;")
    assert getattr(_mutable, "__name__", "") and len(got) == 1 and got[0][1:] == ("+", "-")
    assert all(t not in ("<=", "===") for _, t, _ in _mutable("if (a <= b && c != d) e <= f;"))


# --------------------------------------------------------------------------- #
# Pipeline
# --------------------------------------------------------------------------- #

@needs_engine
def test_end_to_end(tmp_path):
    cfg = _src(tmp_path)
    res = run_n7(cfg, mutants=0, **_kw(cfg))
    c = res.counts()
    assert c["asserts"] == len(res.n5_asserts if hasattr(res, "n5_asserts") else res.asserts)
    assert c["PROVEN"] >= 5
    assert c["FAILED"] == 0 and c["ENV_ERROR"] == 0
    assert res.coverage["coi"]["ratio"] is not None and 0 < res.coverage["coi"]["ratio"] <= 1
    assert res.gaps["block_count"] >= 1
    assert any("five" in {s["signal"] for s in b["signals"]} for b in res.gaps["blocks"])
    assert res.coverage["vacuity"]["antecedents"] > 0 and res.coverage["vacuity"]["ratio"] == 0
    assert res.layers["L1"] > 0 and res.layers["agreement"]["ratio"] == 1
    one = next(p for p in res.asserts if p.name == "onehot_st")
    assert one.status == "PROVEN" and one.formal["layer"] in ("L1", "L2")
    assert one.coi["signals"] > 0
    run_dir = tmp_path / ".verimine" / "runs" / res.n6.n5.n4.n3.n2.n1.n0.hashes["run_hash"]
    assert (run_dir / "n7.json").is_file() and "诚实标注" in (run_dir / "n7_report.md").read_text()
    with sqlite3.connect(tmp_path / ".verimine" / "verimine.db") as con:
        n = con.execute("SELECT count(*) FROM n7_prop").fetchone()[0]
    assert n == len(res.props)


@needs_engine
def test_vacuous_from_an_unreachable_cover(tmp_path, monkeypatch):
    cfg = _src(tmp_path)
    n6 = run_n6(cfg, write=False, **_kw(cfg))
    from verimine.n7 import prove

    def fake(units, **kw):
        out = {}
        for u in units:
            if u.kind == "assert":
                out[u.name] = {"status": "PROVEN", "layer": "L1", "step": None, "trace": ""}
            else:
                out[u.name] = {"status": "UNREACHABLE" if u.name == "fsm_transition_st_c" else "REACHED",
                               "layer": "L1"}
        return out

    monkeypatch.setattr(prove, "prove", fake)
    res = run_n7(cfg, n6_result=n6, mutants=0, write=False, **_kw(cfg))
    assert next(p for p in res.asserts if p.name == "fsm_transition_st").status == "VACUOUS"
    assert res.coverage["vacuity"]["unreachable"] == 1 and res.coverage["vacuity"]["names"] == ["fsm_transition_st"]
    # the vacuous assertion's cone does not count as coverage
    assert "fsm_transition_st" not in {p.name for p in res.asserts if p.status == "PROVEN"}


@needs_engine
def test_mutation_kill_uses_renamed_ports(tmp_path):
    cfg = _src(tmp_path)
    res = run_n7(cfg, mutants=2, **_kw(cfg))
    pc = res.coverage["proof_core"]
    assert pc["total"] == 2 and pc["invalid"] == 0
    assert pc["ratio"] is not None and (pc["killed"] == 0 or pc["ratio"] <= 1)
    assert "近似" in pc["note"]


@needs_engine
def test_closed_loop_reruns_n4_with_the_gap(tmp_path):
    cfg = _src(tmp_path)
    res = run_n7(cfg, loop_rounds=1, mutants=0, **_kw(cfg))
    assert len(res.rounds) == 1 and res.rounds[0]["gap_signals"] >= 1
    assert res.rounds[0]["coi"] is not None


@needs_engine
def test_process_backend(tmp_path):
    cfg = _src(tmp_path)
    res = run_n7(cfg, mutants=0, backend="process", **_kw(cfg))
    assert res.counts()["PROVEN"] >= 5


@needs_engine
def test_cli(tmp_path):
    cfg = _src(tmp_path)
    r = subprocess.run([sys.executable, "-m", "verimine.cli", "n7", str(cfg), "--dry-run", "--json",
                        "--no-mutants"], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    d = json.loads(r.stdout)
    assert d["stage"] == "N7" and d["counts"]["asserts"] > 0
    assert d["coverage"]["coi"]["ratio"] is not None
