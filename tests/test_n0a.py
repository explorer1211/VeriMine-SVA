"""Tests for N0a: configuration completion (YAML draft + inference report) and confirmation."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from ruamel.yaml import YAML

from conftest import FAKE_TOOLS
from verimine.issues import StageError
from verimine.n0 import run_n0
from verimine.n0a import confirm, infer

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "fifo"


class Rtl:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def write(self, rel: str, text: str) -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return p

    def infer(self, **kw):
        kw.setdefault("force", True)
        return infer(self.root, **kw)

    def one(self, **kw):
        res = self.infer(**kw)
        assert len(res) == 1, [r.top for r in res]
        return res[0]


@pytest.fixture
def rtl(tmp_path: Path) -> Rtl:
    return Rtl(tmp_path / "proj")


def _yaml(path: Path) -> dict:
    return YAML(typ="safe").load(path.read_text())


CHILD = "module child(input logic clk, input logic d, output logic q); always_ff @(posedge clk) q <= d; endmodule\n"
TOP = ("module top(input logic clk, input logic rst_n, input logic d, output logic q);\n"
       "  logic m;\n"
       "  always_ff @(posedge clk or negedge rst_n) if (!rst_n) m <= 0; else m <= d;\n"
       "  child u_c(.clk(clk), .d(m), .q(q));\n"
       "endmodule\n")


# --------------------------------------------------------------------------- #
# RTL only
# --------------------------------------------------------------------------- #

def test_fifo_example_matches_handwritten_config(tmp_path):
    root = tmp_path / "fifo"
    shutil.copytree(EXAMPLE / "rtl", root / "rtl")
    (r,) = infer(root)
    cfg = _yaml(r.config_path)
    assert cfg["status"] == "draft"
    assert cfg["unresolved"] == []
    d = cfg["design"]
    assert d["top"] == "fifo_top"
    assert d["include_dirs"] == ["rtl/include"]
    assert set(d["files"]) == {"rtl/pkg/fifo_pkg.sv", "rtl/fifo_mem.sv", "rtl/fifo_ctrl.sv", "rtl/fifo_top.sv"}
    files = d["files"]
    assert files.index("rtl/pkg/fifo_pkg.sv") < files.index("rtl/fifo_mem.sv")
    assert files.index("rtl/pkg/fifo_pkg.sv") < files.index("rtl/fifo_top.sv")
    assert cfg["clocks"] == [{"name": "clk", "edge": "posedge"}]
    assert cfg["resets"] == [{"name": "rst_n", "active": "low", "type": "async"}]
    report = json.loads(r.report_path.read_text())
    assert report["fields"]["design.top"]["confidence"] == "高"
    assert report["check"]["clean"]


def test_confirmed_draft_runs_through_n0(tmp_path):
    root = tmp_path / "fifo"
    shutil.copytree(EXAMPLE / "rtl", root / "rtl")
    (r,) = infer(root)
    assert confirm(r.config_path, resolve_all=True) == []
    assert _yaml(r.config_path)["status"] == "confirmed"
    res = run_n0(r.config_path, tools=FAKE_TOOLS, env={}, write=False)
    assert res.normalized["design"]["top"] == "fifo_top"
    assert "rtl/include/fifo_defs.vh" in [s["path"] for s in res.sources]


def test_deterministic_byte_identical(rtl):
    rtl.write("rtl/top.sv", TOP)
    rtl.write("rtl/child.sv", CHILD)
    r1 = rtl.one()
    y1, j1 = r1.config_path.read_bytes(), r1.report_path.read_bytes()
    r2 = rtl.one()
    assert r2.config_path.read_bytes() == y1
    assert r2.report_path.read_bytes() == j1


def test_unrelated_files_not_in_closure(rtl):
    rtl.write("rtl/top.sv", TOP)
    rtl.write("rtl/child.sv", CHILD)
    rtl.write("rtl/unused_pkg.sv", "package unused_pkg; endpackage\n")
    r = rtl.one()
    assert r.document["design"]["files"] == ["rtl/child.sv", "rtl/top.sv"]


def test_skips_build_dirs_and_dedupes_copies(rtl):
    rtl.write("rtl/top.sv", TOP)
    rtl.write("rtl/child.sv", CHILD)
    rtl.write("obj_dir/Vtop.sv", "module junk; endmodule\n")
    rtl.write("rtl/copy/child.sv", CHILD)
    r = rtl.one()
    assert r.document["design"]["files"] == ["rtl/child.sv", "rtl/top.sv"]
    assert r.report["copies"] == {"rtl/child.sv": ["rtl/copy/child.sv"]}


def test_ifdef_branch_instances_collected(rtl):
    rtl.write("rtl/top.sv", "module top(input logic clk);\n"
                            "`ifdef USE_FAST\n  fast u(.clk);\n`else\n  slow u(.clk);\n`endif\nendmodule\n")
    rtl.write("rtl/fast.sv", "module fast(input logic clk); endmodule\n")
    rtl.write("rtl/slow.sv", "module slow(input logic clk); endmodule\n")
    r = rtl.one()
    assert r.document["design"]["files"] == ["rtl/fast.sv", "rtl/slow.sv", "rtl/top.sv"]
    assert "USE_FAST" in r.report["macros"]["cond_missing"]
    assert not any("USE_FAST" in u for u in r.unresolved)  # conditional macros do not block


# --------------------------------------------------------------------------- #
# Spec unit tests
# --------------------------------------------------------------------------- #

def test_dependency_cycle_reported(rtl):
    rtl.write("rtl/pa.sv", "package pa; parameter int A = pb::B; endpackage\n")
    rtl.write("rtl/pb.sv", "package pb; parameter int B = pa::A; endpackage\n")
    rtl.write("rtl/top.sv", "module top(input logic clk); import pa::*; endmodule\n")
    r = rtl.one()
    assert r.report["cycles"] == [["rtl/pa.sv", "rtl/pb.sv"]]
    assert any(u.startswith("文件依赖出现环") for u in r.unresolved)
    assert set(r.document["design"]["files"]) == {"rtl/pa.sv", "rtl/pb.sv", "rtl/top.sv"}


def test_multiple_tops_one_draft_each(rtl):
    rtl.write("ip_a/a.sv", "module ip_a(input logic clk, output logic q); always_ff @(posedge clk) q <= ~q; endmodule\n")
    rtl.write("ip_b/b.sv", "module ip_b(input logic clk, output logic q); always_ff @(posedge clk) q <= ~q; endmodule\n")
    res = rtl.infer()
    assert sorted(r.top for r in res) == ["ip_a", "ip_b"]
    for r in res:
        assert r.config_path.name == f"verimine.{r.top}.yaml"
        assert r.report_path.name == f"n0a_report.{r.top}.json"
        assert len(r.document["design"]["files"]) == 1
        assert r.unresolved[0].startswith("发现 2 个 top 候选")


def test_user_top_recomputes_closure(rtl):
    rtl.write("rtl/top.sv", TOP)
    rtl.write("rtl/child.sv", CHILD)
    r = rtl.one(top="child")
    assert r.document["design"]["files"] == ["rtl/child.sv"]
    assert r.report["fields"]["design.top"]["basis"] == "用户指定"


def test_testbench_not_a_top_candidate(rtl):
    rtl.write("rtl/top.sv", TOP)
    rtl.write("rtl/child.sv", CHILD)
    rtl.write("tb/tb_top.sv", "module tb_top; logic clk; always #5 clk = ~clk;\n"
                              "  top dut(.clk(clk), .rst_n(1'b1), .d(1'b0), .q());\n"
                              "  initial begin #100 $finish; end endmodule\n")
    r = rtl.one()
    assert r.top == "top"
    assert "tb_top" in r.report["testbenches"]


def test_include_missing(rtl):
    rtl.write("rtl/top.sv", '`include "nowhere/defs.vh"\nmodule top(input logic clk); endmodule\n')
    r = rtl.one()
    assert any("include 文件 nowhere/defs.vh 找不到" in u for u in r.unresolved)


def test_include_dir_is_prefix_not_header_dir(rtl):
    rtl.write("rtl/top.sv", '`include "common/defs.vh"\nmodule top(input logic clk); logic [`W-1:0] x; endmodule\n')
    rtl.write("inc/common/defs.vh", "`define W 4\n")
    r = rtl.one()
    assert r.document["design"]["include_dirs"] == ["inc"]
    assert r.unresolved == ["未能推断出时钟：top 的输入端口没有出现在任何 always_ff/always 的边沿敏感列表里"]


def test_include_multiple_matches_nearest_wins(rtl):
    rtl.write("core/rtl/top.sv", '`include "defs.vh"\nmodule top(input logic clk); logic [`W-1:0] x; endmodule\n')
    rtl.write("core/include/defs.vh", "`define W 4\n")
    rtl.write("other/ip/include/defs.vh", "`define W 8\n")
    r = rtl.one()
    assert r.document["design"]["include_dirs"] == ["core/include"]
    f = r.report["fields"]["design.include_dirs"]
    assert f["alternatives"] == ["other/ip/include/defs.vh"]
    assert f["confidence"] == "需确认"


def test_duplicate_module_prefers_non_stale_path(rtl):
    rtl.write("rtl/top.sv", "module top(input logic clk); fifo u(.clk); endmodule\n")
    rtl.write("rtl/fifo.sv", "module fifo(input logic clk); endmodule\n")
    rtl.write("rtl/fifo_old.sv", "module fifo(input logic clk); logic x; endmodule\n")
    r = rtl.one()
    assert r.document["design"]["files"] == ["rtl/fifo.sv", "rtl/top.sv"]
    assert r.report["duplicates"]["fifo"] == {"chosen": "rtl/fifo.sv", "others": ["rtl/fifo_old.sv"]}
    assert any(u.startswith("模块 fifo 在多个文件中定义") for u in r.unresolved)


def test_vendor_primitive_becomes_blackbox_candidate(rtl):
    rtl.write("rtl/top.sv", "module top(input logic clk, input logic d, output logic q);\n"
                            "  FDRE ff(.C(clk), .D(d), .Q(q), .CE(1'b1), .R(1'b0));\n"
                            "  sram_1rw_256x32 mem();\nendmodule\n")
    r = rtl.one()
    assert r.document["design"]["blackbox"] == ["FDRE", "sram_1rw_256x32"]
    assert "sram_1rw_256x32：是黑盒还是漏了文件" in r.unresolved
    assert not any(u.startswith("slang elaboration") for u in r.unresolved)


def test_unknown_module_unresolved(rtl):
    rtl.write("rtl/top.sv", "module top(input logic clk); mystery_block u(); endmodule\n")
    r = rtl.one()
    assert any(u.startswith("模块 mystery_block 被引用但找不到定义") for u in r.unresolved)


def test_value_macro_undefined_blocks(rtl):
    rtl.write("rtl/top.sv", "module top(input logic clk); logic [`WIDTH-1:0] x;\n"
                            "`ifdef SYNTHESIS\n`endif\nendmodule\n")
    r = rtl.one()
    assert any(u.startswith("宏 WIDTH 需要取值") for u in r.unresolved)
    assert "SYNTHESIS" in r.report["macros"]["cond_missing"]
    assert any(n.startswith("SYNTHESIS") for n in r.report["macros"]["flavour"])
    left = confirm(r.config_path, defines={"WIDTH": "8"}, resolve=[
        i for i, u in enumerate(r.unresolved, 1) if not u.startswith("宏 WIDTH")
    ])
    assert left == []
    cfg = _yaml(r.config_path)
    assert cfg["status"] == "confirmed"
    assert cfg["design"]["defines"] == {"WIDTH": "8"}


def test_macro_defined_in_other_unit_ordered_first(rtl):
    rtl.write("rtl/a_top.sv", "module top(input logic clk); logic [`BUS_W-1:0] x; endmodule\n")
    rtl.write("rtl/z_defs.sv", "`define BUS_W 16\n")
    r = rtl.one()
    assert r.document["design"]["files"] == ["rtl/z_defs.sv", "rtl/a_top.sv"]
    assert not any("BUS_W" in u for u in r.unresolved)


def test_verilog2005_file_flagged(rtl):
    rtl.write("rtl/top.v", "module top(input clk, input logic, output reg q);\n"
                           "  always @(posedge clk) q <= logic;\nendmodule\n")
    r = rtl.one()
    assert r.report["parse"]["verilog2005"] == ["rtl/top.v"]


# --------------------------------------------------------------------------- #
# Clocks and resets
# --------------------------------------------------------------------------- #

def test_sync_reset_and_high_polarity(rtl):
    rtl.write("rtl/top.sv", "module top(input logic clk, input logic srst, input logic d, output logic q);\n"
                            "  always_ff @(posedge clk) begin if (srst) q <= 0; else q <= d; end\nendmodule\n")
    r = rtl.one()
    assert r.document["clocks"] == [{"name": "clk", "edge": "posedge"}]
    assert r.document["resets"] == [{"name": "srst", "active": "high", "type": "sync"}]


def test_clock_traced_through_submodule_ports(rtl):
    rtl.write("rtl/top.sv", "module top(input logic sys_clk, input logic d, output logic q);\n"
                            "  child u(.clk(sys_clk), .d, .q);\nendmodule\n")
    rtl.write("rtl/child.sv", CHILD)
    r = rtl.one()
    assert r.document["clocks"] == [{"name": "sys_clk", "edge": "posedge"}]


def test_multiple_clocks_unresolved(rtl):
    rtl.write("rtl/top.sv", "module top(input logic clk_a, input logic clk_b, output logic qa, output logic qb);\n"
                            "  always_ff @(posedge clk_a) qa <= ~qa;\n  always_ff @(posedge clk_b) qb <= ~qb;\nendmodule\n")
    r = rtl.one()
    assert [c["name"] for c in r.document["clocks"]] == ["clk_a", "clk_b"]
    assert any("UNSUPPORTED_MULTICLOCK" in u for u in r.unresolved)


# --------------------------------------------------------------------------- #
# Step 0: what the user gave
# --------------------------------------------------------------------------- #

def test_filelist_without_yaml_references_it(rtl):
    rtl.write("rtl/top.sv", TOP)
    rtl.write("rtl/child.sv", CHILD)
    rtl.write("rtl/spare.sv", "module spare(input logic clk); endmodule\n")
    f = rtl.write("design.f", "rtl/child.sv\nrtl/top.sv\n")
    r = rtl.one(filelist=f)
    d = r.document["design"]
    assert r.mode == "filelist"
    assert d["filelist"] == ["design.f"]
    assert "files" not in d and "include_dirs" not in d
    assert d["top"] == "top"
    assert r.document["clocks"] == [{"name": "clk", "edge": "posedge"}]


def test_filelist_missing_file_reported(rtl):
    rtl.write("rtl/top.sv", TOP)
    rtl.write("rtl/child.sv", CHILD)
    f = rtl.write("design.f", "rtl/top.sv\n")
    r = rtl.one(filelist=f)
    assert any("模块 child 不在 .f 中" in u for u in r.unresolved)


def test_existing_yaml_fills_only_missing(rtl):
    rtl.write("rtl/top.sv", TOP)
    rtl.write("rtl/child.sv", CHILD)
    cfg = rtl.root / "verimine.yaml"
    cfg.write_text(
        "# 用户的注释要保留\n"
        "schema_version: 1\n"
        "status: confirmed\n"
        "design:\n"
        "  root: .\n"
        "  files: [rtl/child.sv, rtl/top.sv]\n"
        "  include_dirs: [rtl]\n"
        "resets: [{name: rst_n, active: high, type: async}]\n"
    )
    (r,) = infer(config=cfg)
    out = _yaml(cfg)
    assert r.mode == "fill_missing"
    assert out["status"] == "draft"
    assert out["design"]["top"] == "top"
    assert out["design"]["files"] == ["rtl/child.sv", "rtl/top.sv"]
    assert out["design"]["include_dirs"] == ["rtl"]  # user value kept
    assert out["clocks"] == [{"name": "clk", "edge": "posedge"}]
    assert out["resets"] == [{"name": "rst_n", "active": "high", "type": "async"}]  # kept
    assert {"design.top", "clocks"} <= set(r.report["filled"])
    fields = {c["field"] for c in r.report["conflicts"]}
    assert {"resets", "design.include_dirs"} <= fields
    assert cfg.read_text().startswith("# 用户的注释要保留")


def test_complete_yaml_skips(tmp_path):
    shutil.copytree(EXAMPLE, tmp_path / "fifo", ignore=shutil.ignore_patterns(".verimine"))
    cfg = tmp_path / "fifo" / "verimine.yaml"
    before = cfg.read_bytes()
    (r,) = infer(config=cfg)
    assert r.mode == "skip"
    assert cfg.read_bytes() == before


def test_refuses_to_overwrite_without_force(rtl):
    rtl.write("rtl/top.sv", TOP)
    rtl.write("rtl/child.sv", CHILD)
    rtl.one()
    with pytest.raises(StageError) as e:
        infer(rtl.root)
    assert e.value.issues[0].code == "N0A_EXISTS"


# --------------------------------------------------------------------------- #
# Confirm, export, CLI
# --------------------------------------------------------------------------- #

def test_confirm_keeps_draft_while_items_remain(rtl):
    rtl.write("rtl/top.sv", "module top(input logic clk); mystery u(); logic [`W-1:0] x; endmodule\n")
    r = rtl.one()
    assert len(r.unresolved) >= 2
    left = confirm(r.config_path, resolve=[1])
    assert left == r.unresolved[1:]
    cfg = _yaml(r.config_path)
    assert cfg["status"] == "draft"
    with pytest.raises(StageError) as e:
        run_n0(r.config_path, tools=FAKE_TOOLS, env={}, write=False)
    assert e.value.exit_code == 2


def test_export_filelist(rtl):
    rtl.write("rtl/top.sv", TOP)
    rtl.write("rtl/child.sv", CHILD)
    r = rtl.one(export_f=True)
    text = r.filelist_path.read_text()
    assert text.splitlines()[0].startswith("// 由 verimine.yaml 生成，以 YAML 为准")
    assert text.splitlines()[1:] == ["rtl/child.sv", "rtl/top.sv"]


def test_facts_cache_reused(rtl):
    rtl.write("rtl/top.sv", TOP)
    rtl.write("rtl/child.sv", CHILD)
    rtl.one()
    cache = list((rtl.root / ".verimine" / "n0a_cache").iterdir())
    assert len(cache) == 2
    r = rtl.one()
    assert r.document["design"]["files"] == ["rtl/child.sv", "rtl/top.sv"]


def test_cli_infer_and_confirm(rtl):
    rtl.write("rtl/top.sv", TOP)
    rtl.write("rtl/child.sv", CHILD)
    run = lambda *a: subprocess.run([sys.executable, "-m", "verimine.cli", *a],  # noqa: E731
                                    capture_output=True, text=True, stdin=subprocess.DEVNULL)
    p = run("infer", str(rtl.root))
    assert p.returncode == 0, p.stderr
    assert "top=top" in p.stdout
    p = run("confirm", str(rtl.root / "verimine.yaml"))
    assert p.returncode == 0, p.stderr
    assert _yaml(rtl.root / "verimine.yaml")["status"] == "confirmed"
