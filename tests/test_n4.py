"""Tests for N4: candidate mining (path A templates, path B decision trees, path C LLM),
the unified pool (IR, normalization, dedupe, S_mine check, tags, gap-driven budget), storage."""
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
from verimine.n2b import engine
from verimine.n2b.engine import find_tools
from verimine.n2b.props import AND, NOT, op, ref
from verimine.n2b.scan import scan
from verimine.n3.stim import available
from verimine.n4 import run_n4
from verimine.n4.ir import Cand, canon, to_prop
from verimine.n4.store import export_audit

GOLDEN = Path(__file__).parent / "golden" / "n4"
EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "fifo"
needs_sim = pytest.mark.skipif(not available("icarus"), reason="Icarus Verilog not installed")
needs_engine = pytest.mark.skipif(not find_tools()[0], reason="sby / yosys / SMT solver not installed")


def _src(root: Path, **extra) -> Path:
    (root / "rtl").mkdir(parents=True)
    (root / "tb").mkdir()
    shutil.copy(GOLDEN / "src.sv", root / "rtl" / "src.sv")
    shutil.copy(GOLDEN / "tb_src.sv", root / "tb" / "tb_src.sv")
    cfg = {"schema_version": 1, "status": "confirmed", "design": {"top": "src", "files": ["rtl/src.sv"]},
           "clocks": [{"name": "clk", "edge": "posedge"}], "resets": [{"name": "rst_n", "active": "low", "type": "async"}],
           "testbench": {"files": ["tb/tb_src.sv"], "top": "tb_src"}, **extra}
    p = root / "verimine.yaml"
    with p.open("w") as fh:
        YAML(typ="safe").dump(cfg, fh)
    return p


def _run(cfg: Path, **kw):
    kw.setdefault("tools", FAKE_TOOLS)
    kw.setdefault("env", {})
    kw.setdefault("workspace", cfg.parent / ".verimine")
    return run_n4(cfg, **kw)


def _texts(res, status="POOL"):
    return {c.text: c for c in res.cands if c.status == status}


def _prove(res, cands):
    n0 = res.n3.n2.n1.n0
    root, d = n0.root, n0.normalized["design"]
    files = [root / f for f in d["files"]]
    props = [to_prop(c) for c in cands]
    engine.run(props, instances=res.n3.n2.n1.elaboration.instances, files=files, include_dirs=[], defines={},
               top=d["top"], root=root, scan=scan(files, d["top"], [], {}, root), top_resets=n0.normalized["resets"],
               assumes=n0.normalized["assumes"], top_clock=n0.normalized["clocks"][0],
               workdir=n0.workspace / "prove", depth=12, timeout=60)
    return {c.text: p.status for c, p in zip(cands, props)}


# --------------------------------------------------------------------------- #
# Path A
# --------------------------------------------------------------------------- #

@needs_sim
@needs_engine
def test_path_a_templates_hold_and_wrong_one_dropped(tmp_path):
    res = _run(_src(tmp_path))
    pool = _texts(res)
    want = {
        "(st & (st - 1)) == 0 && st != 0": "onehot",
        "st == 1 |=> st == 1 || st == 2": "fsm_transition",
        "st == 4 |=> st == 1": "fsm_transition",
        "out_valid && !out_ready |=> out_valid": "handshake_hold",
        "out_valid && !out_ready |=> out_data == $past(out_data)": "handshake_data",
        "!(out_valid && waiting)": "mutex",
        "beats <= 9": "counter_range",
        "!rst_n |=> st == 1": "reset_value",
        "!rst_n |=> out_data == 0": "reset_value",
    }
    for text, t in want.items():
        assert pool[text].type == t and pool[text].sources == ["A"]
    assert all(c.legality == "none" and c.assumes == [] for c in pool.values())
    assert pool["out_valid && !out_ready |=> out_valid"].level == "port"
    assert pool["beats <= 9"].level == "internal"
    # hits is compared with 5 but counts on: the template is wrong, the user trace says so
    (bad,) = [c for c in res.cands if c.status == "DROPPED"]
    assert bad.text == "hits <= 5" and "S_mine" in bad.reason and bad.trace["violations"]["user"] > 0
    # everything left in the pool holds (path A needs no traces to be right)
    st = _prove(res, res.pool)
    assert set(st.values()) == {"PROVEN"}, st


@needs_sim
def test_fifo_templates_and_assume_tags(tmp_path):
    shutil.copytree(EXAMPLE, tmp_path / "fifo", ignore=shutil.ignore_patterns(".verimine"))
    res = _run(tmp_path / "fifo" / "verimine.yaml")
    pool = _texts(res)
    assert {"!(full && empty)", "count <= 16", "full && push && !pop |=> count == $past(count)",
            "empty && pop && !push |=> count == $past(count)", "!rst_n |=> count == 0"} <= set(pool)
    # push is a top input in the cone of the count: depends on the config's assume on push
    assert pool["count <= 16"].assumes == ["no_push_when_full"]
    assert pool["!rst_n |=> count == 0"].assumes == []          # under reset inputs do not matter
    assert {c.scope for c in pool.values()} == {"fifo_top", "fifo_top.u_ctrl"}


# --------------------------------------------------------------------------- #
# Path B
# --------------------------------------------------------------------------- #

@needs_sim
def test_path_b_off_by_default_fto(tmp_path):
    res = _run(_src(tmp_path))
    assert res.paths["B"]["ran"] is False and "FTO" in res.paths["B"]["reason"]
    assert res.by_source()["B"] == 0


@needs_sim
@needs_engine
def test_path_b_decision_tree(tmp_path):
    res = _run(_src(tmp_path, run={"params": {"n4_path_b": True}}))
    assert "N4_PATH_B_EXPERIMENTAL" in {w["code"] for w in res.warnings}
    b = [c for c in res.pool if "B" in c.sources]
    assert b and all(c.legality == "user" for c in b if c.sources == ["B"])
    texts = {c.text for c in b}
    # out_valid is st == SEND, waiting is st == WAIT: found from the state predicates
    assert {"st == 2 |-> out_valid", "st == 4 |-> waiting"} <= texts, texts
    # a register target is decided by the previous cycle: IDLE with go -> SEND
    assert "out_ready && st == 1 && go |=> st == 2" in texts
    per_target: dict = {}
    for c in b:
        per_target[c.detail["target"]] = per_target.get(c.detail["target"], 0) + 1
    assert max(per_target.values()) <= 8
    st = _prove(res, b)
    assert st["st == 2 |-> out_valid"] == st["out_ready && st == 1 && go |=> st == 2"] == "PROVEN"
    # trained on S_mine, so S_mine cannot refute it: some are wrong and only N7 can tell
    assert "FAILED" in st.values() or "BOUNDED" in st.values()


# --------------------------------------------------------------------------- #
# Path C
# --------------------------------------------------------------------------- #

def _replay(tmp_path: Path, answer) -> dict:
    f = tmp_path / "answers.json"
    f.write_text(json.dumps({"src": answer}))
    return {"VERIMINE_LLM_REPLAY": str(f)}


@needs_sim
def test_path_c_llm_masked_schema_checked_and_audited(tmp_path, monkeypatch):
    cfg = _src(tmp_path, llm={"mode": "local", "provider": "replay", "model": "m"})
    sent = []
    from verimine.n4 import llm as L
    orig = L.call

    def spy(llm, params, module, messages):
        sent.append(messages)
        return orig(llm, params, module, messages)

    monkeypatch.setattr(L, "call", spy)
    # p3 = out_ready ... codes follow port order: clk rst_n go out_ready out_valid out_data waiting five
    answer = {"candidates": [
        {"kind": "template", "template": "handshake_hold", "signals": ["p4", "p3"], "why": "valid 保持"},
        {"kind": "prop", "ante": {"sig": "p6"}, "cons": {"op": "!", "args": [{"sig": "p4"}]}, "next": False},
        {"kind": "prop", "cons": {"sig": "nope"}},                        # not in the summary
        {"kind": "sva", "text": "assert property (a);"},                  # free SVA: refused
    ]}
    for k, v in _replay(tmp_path, answer).items():
        monkeypatch.setenv(k, v)
    res = _run(cfg)
    assert res.paths["C"]["ran"] and res.paths["C"]["calls"] == 1
    # L1: no real signal name leaves; L2: no source text
    blob = json.dumps(sent, ensure_ascii=False)
    for real in ("out_valid", "out_ready", "beats", "always_ff", "module src"):
        assert real not in blob
    pool = _texts(res)
    hold = pool["out_valid && !out_ready |=> out_valid"]
    assert sorted(hold.sources) == ["A", "C"]                         # same IR from A and C: one candidate
    assert pool["waiting |-> !out_valid"].sources == ["C"]
    assert "N4_LLM_REJECTED" in {w["code"] for w in res.warnings}
    audit = export_audit(tmp_path / ".verimine" / "verimine.db")
    assert len(audit) == 1 and audit[0]["module"] == "src" and len(json.loads(audit[0]["adopted"])) == 2
    assert audit[0]["level"] == "L1+L2"


@needs_sim
def test_llm_off_path_a_complete_and_foreign_blocked(tmp_path, monkeypatch):
    off = _run(_src(tmp_path / "a"), write=False)
    assert off.paths["C"] == {"ran": False, "reason": "llm.mode = off"}
    monkeypatch.setenv("VERIMINE_LLM_URL", "https://api.openai.com/v1")
    on = _run(_src(tmp_path / "b", llm={"mode": "cloud", "provider": "openai", "model": "x"}), write=False)
    assert "N4_LLM_FAILED" in {w["code"] for w in on.warnings}
    assert "境外" in next(w["message"] for w in on.warnings if w["code"] == "N4_LLM_FAILED")
    # a failing LLM never costs path A anything
    assert {c.text for c in on.pool} == {c.text for c in off.pool}


def test_llm_parse_rejects_bad_answers():
    from verimine.n4.llm import LlmError, parse

    class Sc:
        def cand(self, t, a, c, n=False, evidence=()):
            return Cand(t, "top", "m", a, c, n, ["C"], evidence=list(evidence))

    names, widths = {"p0": "a", "p1": "b"}, {"a": 1, "b": 1}
    with pytest.raises(LlmError):
        parse("not json", Sc(), names, widths)
    with pytest.raises(LlmError):
        parse('{"candidates": [], "sva": "x"}', Sc(), names, widths)
    cs, bad = parse(json.dumps({"candidates": [
        {"kind": "prop", "cons": {"op": "call", "args": [{"sig": "p0"}]}},
        {"kind": "prop", "cons": {"op": "!", "args": [{"sig": "p0"}, {"sig": "p1"}]}},
        {"kind": "template", "template": "mutex", "signals": ["p0", "p1"]},
    ]}), Sc(), names, widths)
    assert len(bad) == 2 and [c.text for c in cs] == ["!(a && b)"]


# --------------------------------------------------------------------------- #
# Pool
# --------------------------------------------------------------------------- #

def test_canonical_ir_dedupes_syntactic_variants():
    a, b, c = ref("a"), ref("b"), ref("c")
    assert canon(AND(a, AND(b, c))) == canon(AND(AND(c, b), a))
    assert canon(op("==", a, b)) == canon(op("==", b, a))
    assert canon(NOT(NOT(a))) == canon(a)
    assert canon(op("<", a, b)) != canon(op("<", b, a))
    x = Cand("t", "top", "m", AND(a, b), c, True)
    y = Cand("t", "top", "m", AND(b, a), c, True)
    z = Cand("t", "top", "m", AND(b, a), c, False)
    assert x.key == y.key != z.key


@needs_sim
def test_budget_gap_regions_get_more(tmp_path):
    res = _run(_src(tmp_path, run={"params": {"n4_path_b": True, "n4_quota": 1, "n4_gap_boost": 2}}), write=False)
    from verimine.n4.pool import region
    regions: dict = {}
    for c in res.pool:
        regions.setdefault(region(c), []).append(c)
    for r, cs in regions.items():
        assert len(cs) <= (2 if any(c.gap for c in cs) else 1), r
    assert res.counts().get("DEFERRED", 0) > 0
    assert any(len(cs) == 2 for cs in regions.values())          # a gap region used its boost
    deferred = [c for c in res.cands if c.status == "DEFERRED"]
    assert all("配额" in c.reason for c in deferred)


@needs_sim
def test_storage_and_determinism(tmp_path):
    cfg = _src(tmp_path)
    r1 = _run(cfg)
    r2 = _run(cfg)
    assert r1.input_hash == r2.input_hash and r1.n4_hash == r2.n4_hash
    assert [c.id for c in r1.cands] == [c.id for c in r2.cands]
    assert r1.db == {"new_run": True} and r2.db == {"new_run": False}
    with sqlite3.connect(tmp_path / ".verimine" / "verimine.db") as con:
        rows = con.execute("SELECT id, sources, legality, assumes, ir FROM n4_cand WHERE input_hash=?",
                           (r1.input_hash,)).fetchall()
    assert len(rows) == len(r1.cands)
    ir = json.loads(rows[0][4])
    assert set(ir) == {"ante", "cons", "delay", "type", "source", "features"}
    md = (tmp_path / ".verimine" / "runs" / r1.n3.n2.n1.n0.hashes["run_hash"] / "n4_report.md").read_text()
    assert "## 候选池" in md and "## 淘汰与暂缓" in md


@needs_sim
def test_cli_n4(tmp_path):
    cfg = _src(tmp_path)
    r = subprocess.run([sys.executable, "-m", "verimine.cli", "n4", str(cfg), "--dry-run"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "N4 完成" in r.stdout and "路径 B" in r.stdout and "FTO" in r.stdout
    out = tmp_path / "audit.json"
    r = subprocess.run([sys.executable, "-m", "verimine.cli", "n4", str(cfg), "--audit-export", str(out)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert json.loads(out.read_text()) == []
