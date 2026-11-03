"""Tests for N2: behavioral modeling (VDG, structures, roles, PageRank, COI)."""
from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import FAKE_TOOLS
from verimine.n2 import run_n2

GOLDEN = Path(__file__).parent / "golden" / "n2"
EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "fifo"
ACCEPT = 0.90  # spec: state registers, counters, handshakes recognized >= 90%


def _n2(project, **kw):
    kw.setdefault("tools", FAKE_TOOLS)
    kw.setdefault("env", {})
    kw.setdefault("write", False)
    return run_n2(project.yaml(), workspace=project.root / ".verimine", **kw)


def _design(project, text: str, top: str = "top", **cfg):
    project.write("rtl/top.sv", text)
    project.design.update(files=["rtl/top.sv"], top=top)
    project.cfg.update(cfg)
    return _n2(project)


def _recognized(res) -> dict[str, set[str]]:
    """Roles backed by structure with confidence 高 or 中: what the acceptance metric counts."""
    return {sr.node: {r.role for r in sr.roles if r.evidence_kind != "naming" and r.confidence in ("高", "中")}
            for sr in res.roles}


def _score(res, labels, prefix: str) -> tuple[float, list]:
    got = _recognized(res)
    misses = [l for l in labels if (l[2] in got.get(f"{prefix}.{l[0]}.{l[1]}", set())) != l[3]]
    return 1 - len(misses) / len(labels), misses


# --------------------------------------------------------------------------- #
# Acceptance: golden set
# --------------------------------------------------------------------------- #

def test_golden_set_recognition_rate(project):
    files = []
    for f in sorted(GOLDEN.glob("*.sv")):
        project.write(f"rtl/{f.name}", f.read_text())
        files.append(f"rtl/{f.name}")
    project.design.update(files=files, top="golden_top")
    project.cfg["resets"] = [{"name": "rst_n", "active": "low", "type": "async"},
                             {"name": "rst", "active": "high", "type": "sync"}]
    res = _n2(project)
    labels = json.loads((GOLDEN / "expected.json").read_text())["labels"]
    rate, misses = _score(res, labels, "golden_top")
    assert rate >= ACCEPT, misses
    assert misses == []  # currently 100%; a regression should be looked at, not absorbed


def test_fifo_example_recognition(tmp_path):
    shutil.copytree(EXAMPLE, tmp_path / "fifo", ignore=shutil.ignore_patterns(".verimine"))
    res = run_n2(tmp_path / "fifo" / "verimine.yaml", tools=FAKE_TOOLS, env={}, write=False)
    labels = json.loads((GOLDEN / "expected.json").read_text())["fifo_labels"]
    rate, misses = _score(res, labels, "fifo_top")
    assert misses == [], misses
    mem = next(p for p in res.patterns if p.kind == "memory")
    assert mem.detail["write_addr"] == ["waddr"] and mem.detail["read_addr"] == ["raddr"]


# --------------------------------------------------------------------------- #
# VDG
# --------------------------------------------------------------------------- #

PIPE = """\
module top(input logic clk, input logic rst_n, input logic en, input logic [3:0] d, output logic [3:0] q);
  logic [3:0] r;
  logic [3:0] w;
  assign w = d + 4'd1;
  always_ff @(posedge clk or negedge rst_n)
    if (!rst_n) r <= '0;
    else if (en) r <= w;
  assign q = r;
endmodule
"""


def test_vdg_edge_kinds_and_delays(project):
    res = _design(project, PIPE)
    e = {(x.src, x.dst): x for x in res.vdg.edges}
    assert e[("top.d", "top.w")].kind == "data" and e[("top.d", "top.w")].delay == 0
    assert e[("top.w", "top.r")].kind == "data" and e[("top.w", "top.r")].delay == 1
    assert e[("top.en", "top.r")].kind == "control" and e[("top.en", "top.r")].delay == 1
    assert e[("top.rst_n", "top.r")].reset
    assert e[("top.r", "top.q")].delay == 0
    assert res.vdg.nodes["top.r"].kind == "reg"
    assert res.vdg.nodes["top.w"].kind == "wire"


def test_vdg_crosses_port_connections(project):
    res = _design(project, """\
module leaf(input logic clk, input logic a, output logic b); always_ff @(posedge clk) b <= a; endmodule
module top(input logic clk, input logic x, output logic y);
  logic m;
  leaf u0(.clk, .a(x), .b(m));
  leaf u1(.clk, .a(m), .b(y));
endmodule
""")
    pairs = {(e.src, e.dst) for e in res.vdg.edges}
    assert {("top.x", "top.u0.a"), ("top.u0.b", "top.m"), ("top.m", "top.u1.a"), ("top.u1.b", "top.y")} <= pairs
    coi = next(c for c in res.cois if c.target == "top.y")
    assert "top.x" in coi.nodes and coi.sequential_depth == 2
    assert res.vdg.nodes["top.u0.clk"].clock  # clock followed through the port


def test_abstraction_levels(project):
    res = _design(project, PIPE)
    lv = {sr.node: sr.level for sr in res.roles}
    assert lv["top.q"] == "port" and lv["top.d"] == "port"
    assert lv["top.r"] == "internal" and lv["top.w"] == "internal"


# --------------------------------------------------------------------------- #
# Structures
# --------------------------------------------------------------------------- #

def test_fsm_transitions_and_reset_state(project):
    project.write("rtl/fsm.sv", (GOLDEN / "fsm_case.sv").read_text())
    project.design.update(files=["rtl/fsm.sv"], top="fsm_case")
    res = _n2(project)
    (f,) = res.fsms
    names = {s["value"]: s["name"] for s in f.states}
    assert names == {0: "IDLE", 1: "REQ", 2: "WAIT", 3: "DONE"}
    assert f.next_state == "state_nxt" and f.reset_state == 0
    edges = {(names[t["from"]], names[t["to"]]) for t in f.transitions}
    assert edges == {("IDLE", "IDLE"), ("IDLE", "REQ"), ("REQ", "WAIT"), ("WAIT", "IDLE"),
                     ("WAIT", "WAIT"), ("WAIT", "DONE"), ("DONE", "IDLE")}
    # REQ always moves on: the default `state_nxt = state` is overridden, so no REQ -> REQ
    assert ("REQ", "REQ") not in edges
    wait_done = next(t for t in f.transitions if (t["from"], t["to"]) == (2, 3))
    assert wait_done["cond"] == ["!(abort)", "ack"]
    assert f.reachable == [0, 1, 2, 3]


def test_fsm_unreachable_state(project):
    res = _design(project, """\
module top(input logic clk, input logic rst_n, input logic go, output logic busy);
  localparam [1:0] A = 0, B = 1, C = 2, ORPHAN = 3;
  logic [1:0] st;
  always_ff @(posedge clk or negedge rst_n)
    if (!rst_n) st <= A;
    else case (st)
      A: if (go) st <= B;
      B: st <= C;
      C: st <= A;
      ORPHAN: st <= A;
    endcase
  assign busy = st != A;
endmodule
""")
    (f,) = res.fsms
    assert f.next_state is None
    assert f.reachable == [0, 1, 2]
    assert {s["name"] for s in f.states} == {"A", "B", "C", "ORPHAN"}


def test_counter_shift_memory_details(project):
    res = _design(project, """\
module top(input logic clk, input logic sin, input logic [3:0] a, input logic we,
           output logic [7:0] q, output logic [3:0] c, output logic [7:0] s);
  logic [7:0] mem [16];
  always_ff @(posedge clk) c <= c - 4'd1;
  always_ff @(posedge clk) s <= {sin, s[7:1]};
  always_ff @(posedge clk) if (we) mem[a] <= s;
  assign q = mem[c];
endmodule
""")
    p = {(x.kind, x.signal): x for x in res.patterns}
    assert p[("counter", "c")].detail["direction"] == "down"
    assert p[("shift_register", "s")].detail == {"direction": "right", "serial_inputs": ["sin"], "width": 8}
    m = p[("memory", "mem")].detail
    assert m["write_addr"] == ["a"] and m["write_data"] == ["s"] and m["read_addr"] == ["c"]
    assert m["dims"] == "[0:15]" and m["width"] == 8


def test_naming_is_weak_evidence_only(project):
    res = _design(project, """\
module top(input logic clk, input logic [7:0] d, output logic [7:0] state_q, output logic [7:0] my_count);
  always_ff @(posedge clk) state_q <= d;
  always_ff @(posedge clk) my_count <= d;
endmodule
""")
    roles = {sr.name: sr.roles for sr in res.roles}
    for name, role in (("state_q", "state_register"), ("my_count", "counter")):
        (r,) = [x for x in roles[name] if x.role == role]
        assert r.evidence_kind == "naming" and r.confidence == "低"
    assert res.fsms == [] and [p for p in res.patterns if p.kind == "counter"] == []


def test_handshake_confidence_levels(project):
    res = _design(project, """\
module top(input logic clk, input logic a_valid, output logic a_ready,
           input logic b_valid, output logic b_ready, output logic [1:0] q);
  assign a_ready = 1'b1;
  assign b_ready = 1'b1;
  always_ff @(posedge clk) if (a_valid && a_ready) q <= q + 1'b1;
endmodule
""")
    conf = {e.signals["valid"]: e.confidence for e in res.events if e.kind == "valid_ready"}
    assert conf == {"a_valid": "高", "b_valid": "中"}


# --------------------------------------------------------------------------- #
# PageRank, COI, hashes
# --------------------------------------------------------------------------- #

def test_pagerank_deterministic_and_normalized(project):
    r1 = _design(project, PIPE)
    r2 = _n2(project)
    assert r1.pagerank == r2.pagerank
    assert abs(sum(r1.pagerank.values()) - 1) < 1e-6
    # resets gate everything; they are kept out of the ranking
    assert r1.pagerank["top.rst_n"] <= min(r1.pagerank["top.en"], r1.pagerank["top.d"])


def _two_cones(c_logic: str, comment: str = "") -> str:
    return f"""\
module a(input logic clk, input logic x, output logic y); always_ff @(posedge clk) y <= x; endmodule
module b(input logic clk, input logic x, output logic y); {comment}always_ff @(posedge clk) y <= {c_logic}; endmodule
module top(input logic clk, input logic i, input logic j, output logic p, output logic q);
  a ua(.clk, .x(i), .y(p));
  b ub(.clk, .x(j), .y(q));
endmodule
"""


def test_coi_fingerprint_tracks_only_its_cone(project):
    r1 = _design(project, _two_cones("x"))
    fp1 = {c.target: c.fingerprint for c in r1.cois}
    r2 = _design(project, _two_cones("!x"))
    fp2 = {c.target: c.fingerprint for c in r2.cois}
    assert fp1["top.q"] != fp2["top.q"]      # logic in its cone changed
    assert fp1["top.p"] == fp2["top.p"]      # other cone untouched
    r3 = _design(project, _two_cones("!x", "/* just a comment */ "))
    assert fp2 == {c.target: c.fingerprint for c in r3.cois}


def test_hashes_deterministic(project):
    r1 = _design(project, PIPE)
    r2 = _n2(project)
    assert r1.input_hash == r2.input_hash
    assert r1.n2_hash == r2.n2_hash
    project.cfg["resets"] = [{"name": "rst_n", "active": "high", "type": "async"}]
    r3 = _n2(project)
    assert r3.input_hash != r1.input_hash  # clock/reset config is an N2 input


# --------------------------------------------------------------------------- #
# Blocked modules, generate, storage, CLI
# --------------------------------------------------------------------------- #

def test_blocked_child_skipped_parent_modeled(project):
    res = _design(project, """\
module bad(input logic clk, input logic a, output logic y);
  always_ff @(posedge clk) y <= a;
  always_ff @(posedge clk) y <= !a;
endmodule
module top(input logic clk, input logic a, output logic y, output logic z);
  bad u(.clk, .a, .y);
  always_ff @(posedge clk) z <= a;
endmodule
""")
    st = {s.path: s.status for s in res.n1.statuses}
    assert st["top.u"] == "BLOCKED"
    assert [s["path"] for s in res.vdg.skipped] == ["top.u"]
    assert "top" in res.vdg.modeled
    assert not any(n.startswith("top.u.") for n in res.vdg.nodes)
    assert any(w["code"] == "N2_SKIPPED_BLOCKED" for w in res.warnings)


def test_generate_copies_are_distinct_nodes(project):
    res = _design(project, """\
module top(input logic clk, input logic [1:0] d, output logic [1:0] q);
  for (genvar i = 0; i < 2; i++) begin : g
    logic t;
    always_ff @(posedge clk) t <= d[i];
    assign q[i] = t;
  end
endmodule
""")
    assert {"top.g[0].t", "top.g[1].t"} <= set(res.vdg.nodes)
    assert res.vdg.nodes["top.g[0].t"].kind == "reg"


def test_storage_and_idempotency(project):
    project.write("rtl/top.sv", PIPE)
    ws = project.root / ".verimine"
    r1 = run_n2(project.yaml(), workspace=ws, tools=FAKE_TOOLS, env={}, write=True)
    assert r1.db["new_model"]
    with sqlite3.connect(ws / "verimine.db") as con:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"n2_model", "n2_node", "n2_edge", "n2_role", "n2_fsm", "n2_pattern", "n2_event", "n2_coi"} <= tables
        row = con.execute("SELECT index_hash, n2_hash FROM n2_model WHERE input_hash=?", (r1.input_hash,)).fetchone()
        assert row == (r1.n1.index_hash, r1.n2_hash)
        assert con.execute("SELECT count(*) FROM n2_edge").fetchone()[0] == len(r1.vdg.edges)
    r2 = run_n2(project.yaml(), workspace=ws, tools=FAKE_TOOLS, env={}, write=True)
    assert not r2.db["new_model"]
    out = json.loads((ws / "runs" / r1.n1.n0.hashes["run_hash"] / "n2.json").read_text())
    assert out["input_hash"] == r1.input_hash and out["n2_hash"] == r1.n2_hash


def test_cli_n2_dry_run(project):
    project.write("rtl/top.sv", PIPE)
    r = subprocess.run([sys.executable, "-m", "verimine.cli", "n2", str(project.yaml()), "--dry-run"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "N2 完成" in r.stdout
