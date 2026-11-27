"""Capacity test (spec 容量测试): where the open-source stack stops.

Four tiers of a synthetic design (roughly 500 / 2 000 / 10 000 / 75 000 RTL lines). For each:
how long slang and Yosys need to read it (and the peak memory this process saw), how deep a
shallow BMC still finishes within the time budget, and whether k-induction proves the counter
invariant. The output is a capability statement — a module size the stack is known to take, with
the countermeasures to use above it (COI cropping, black boxes, splitting modules). Until this
has run, the report does not promise any size.

The design is generated, not sampled from customers: it has many small instantiations of one
block, which is what costs Yosys read time, but no deep hierarchy or memories.
"""
from __future__ import annotations

import json
import resource
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

TIERS = {1: 500, 2: 2000, 3: 10000, 4: 75000}
BLOCK_LINES = 15

_BLOCK = """\
module blk_{i} (input logic clk, input logic rst_n, input logic en, input logic [15:0] din,
               output logic [15:0] dout, output logic [7:0] cnt);
  logic [15:0] acc;
  logic [7:0] c;
  always_ff @(posedge clk or negedge rst_n)
    if (!rst_n) begin acc <= 16'd0; c <= 8'd0; end
    else begin
      c <= c + 8'd1;
      acc <= en ? (acc + din) : (acc ^ {{8'd0, c}});
    end
  assign dout = acc;
  assign cnt = c;
endmodule
"""


def generate(lines: int) -> str:
    n = max(1, lines // BLOCK_LINES)
    blocks = [_BLOCK.format(i=i) for i in range(n)]
    wires = [f"  logic [15:0] w{i};" for i in range(n)]
    inst = []
    for i in range(n):
        din = "din" if i == 0 else f"w{i - 1}"
        cnt = "c0" if i == 0 else ""
        conns = f".clk(clk), .rst_n(rst_n), .en(en), .din({din}), .dout(w{i})" + (f", .cnt({cnt})" if cnt else "")
        inst.append(f"  blk_{i} u{i} ({conns});")
    top = "\n".join([
        "module cap_top (input logic clk, input logic rst_n, input logic en, input logic [15:0] din,",
        "                output logic [15:0] dout);",
        "  logic [7:0] c0;",
        "  reg vm_pv = 1'b0;",
        "  always @(posedge clk) vm_pv <= 1'b1;",
        *wires, *inst,
        f"  assign dout = w{n - 1};",
        "  always @(posedge clk) if (rst_n && vm_pv) cap_cnt:",
        "    assert(!$past(rst_n) || $past(c0) == 8'hFF || c0 == $past(c0) + 8'd1);",
        "endmodule", ""])
    return "\n".join(blocks) + "\n" + top


def _peak_mb() -> float:
    v = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(v / (1024 * 1024 if sys.platform == "darwin" else 1024), 1)


def _slang_read(path: Path, top: str) -> dict:
    t = time.perf_counter()
    try:
        from pyslang.ast import Compilation, CompilationOptions
        from pyslang.parsing import PreprocessorOptions
        from pyslang.pyslang import Bag, DiagnosticEngine, SourceManager
        from pyslang.syntax import SyntaxTree

        sm = SourceManager()
        po = PreprocessorOptions()
        opts = CompilationOptions()
        opts.topModules = {top}
        comp = Compilation(Bag([opts]))
        trees = SyntaxTree.fromFiles([str(path)], sm, Bag([po]))
        for x in trees if isinstance(trees, (list, tuple)) else [trees]:
            comp.addSyntaxTree(x)
        eng = DiagnosticEngine(sm)
        errs = [eng.formatMessage(d) for d in comp.getAllDiagnostics() if d.isError()]
        return {"ok": not errs, "seconds": round(time.perf_counter() - t, 2), "errors": errs[:3]}
    except Exception as e:
        return {"ok": False, "seconds": round(time.perf_counter() - t, 2), "errors": [str(e)[:200]]}


def _yosys_read(path: Path, top: str, timeout: int) -> dict:
    if not shutil.which("yosys"):
        return {"ok": False, "reason": "没有 Yosys"}
    t = time.perf_counter()
    try:
        r = subprocess.run(["yosys", "-q", "-p", f"read_verilog -sv {path}; hierarchy -top {top}; proc; prep -top {top}"],
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "seconds": timeout, "reason": f"超时（{timeout} 秒）"}
    err = [x for x in (r.stdout + r.stderr).splitlines() if "ERROR" in x]
    return {"ok": r.returncode == 0 and not err, "seconds": round(time.perf_counter() - t, 2),
            "reason": (err or [""])[0][:200]}


def _sby(work: Path, path: Path, top: str, mode: str, depth: int, timeout: int,
         solver: str = "bitwuzla") -> dict:
    if not shutil.which("sby"):
        return {"ok": False, "reason": "没有 sby"}
    work.mkdir(parents=True, exist_ok=True)
    d = work / f"{mode}_{depth}"
    if d.exists():
        shutil.rmtree(d)
    s = work / f"{mode}_{depth}.sby"
    s.write_text(f"""[options]
mode {mode}
depth {depth}
timeout {timeout}

[engines]
smtbmc {solver}

[script]
read_verilog -formal -sv {Path(path).resolve()}
prep -top {top}
""", encoding="utf-8")
    t = time.perf_counter()
    try:
        subprocess.run(["sby", "-f", "-d", str(d), str(s)], capture_output=True, text=True, timeout=timeout + 30)
    except subprocess.TimeoutExpired:
        return {"ok": False, "seconds": timeout, "status": "TIMEOUT"}
    st = (d / "status").read_text().split()[0] if (d / "status").exists() else "ERROR"
    return {"ok": st == "PASS", "status": st, "seconds": round(time.perf_counter() - t, 2)}


def run(tiers: list[int] | None = None, timeout: int = 120, workspace: Path | None = None,
        budget: int = 120) -> dict:
    """tiers: 1-4; timeout per tool call; budget: total seconds a tier may take before it is
    recorded as the failure point."""
    tiers = tiers or [1, 2]
    work = (workspace or Path(".verimine")) / "capacity"
    work.mkdir(parents=True, exist_ok=True)
    out = {"tiers": [], "capability": None}
    for t in tiers:
        lines = TIERS[t]
        d = work / f"tier{t}"
        d.mkdir(parents=True, exist_ok=True)
        src = d / "cap.sv"
        src.write_text(generate(lines), encoding="utf-8")
        row = {"tier": t, "target_lines": lines, "lines": len(src.read_text().splitlines()),
               "bytes": src.stat().st_size}
        start = time.perf_counter()
        row["slang"] = _slang_read(src, "cap_top")
        row["yosys"] = _yosys_read(src, "cap_top", timeout)
        row["peak_mb"] = _peak_mb()
        if row["yosys"]["ok"]:
            reached, tries = 0, []
            for depth in (5, 10, 20, 40):
                if time.perf_counter() - start > budget:
                    tries.append({"depth": depth, "status": "budget"})
                    break
                r = _sby(d / "l0", src, "cap_top", "bmc", depth, timeout)
                tries.append({"depth": depth, "status": r.get("status", "ERROR"), "seconds": r.get("seconds")})
                if r["ok"]:
                    reached = depth
                else:
                    break
            row["l0"] = {"max_depth": reached, "tries": tries}
            r = _sby(d / "l1", src, "cap_top", "prove", 20, timeout)
            row["l1"] = {"ok": r["ok"], "status": r.get("status"), "seconds": r.get("seconds")}
        else:
            row["l0"] = {"max_depth": None, "tries": []}
            row["l1"] = {"ok": False, "status": "not_run", "reason": "Yosys 读不进去"}
        row["seconds"] = round(time.perf_counter() - start, 2)
        out["tiers"].append(row)
    out["capability"] = statement(out)
    return out


def statement(res: dict) -> dict:
    ok = [r for r in res["tiers"] if r["yosys"].get("ok") and (r.get("l1") or {}).get("ok")]
    read = [r for r in res["tiers"] if r["yosys"].get("ok") and r["slang"].get("ok")]
    top = max((r["lines"] for r in ok), default=None)
    if top is None:
        return {"lines": None, "text": "没有一档完成 L1 证明：不对外承诺规模",
                "countermeasures": ["COI 裁剪", "黑盒不相关模块", "拆模块"]}
    return {"lines": top, "text": f"本机开源栈（slang + Yosys + sby）在本测试设计上验证到约 {top} 行；"
                                  "超过时先做 COI 裁剪、黑盒和拆模块，仍不行则降级为有界结论",
            "max_read_lines": max((r["lines"] for r in read), default=None),
            "countermeasures": ["COI 裁剪", "黑盒不相关模块", "拆模块"]}


def markdown(res: dict) -> str:
    L = ["# 容量测试与能力声明", "",
         "| 档 | 目标行数 | 实际行数 | slang | Yosys 读入 | 峰值内存 | L0 最大深度 | L1 | 用时 |",
         "|---|---|---|---|---|---|---|---|---|"]
    for r in res["tiers"]:
        s = r["slang"]
        y = r["yosys"]
        L.append(f"| {r['tier']} | {r['target_lines']} | {r['lines']} | "
                 f"{'通过' if s['ok'] else '失败'} {s['seconds']}s | "
                 f"{'通过' if y.get('ok') else '失败'} {y.get('seconds', '-')}s | {r['peak_mb']} MB | "
                 f"{r['l0'].get('max_depth') if r['l0'].get('max_depth') is not None else '-'} | "
                 f"{'通过' if r['l1'].get('ok') else r['l1'].get('status', '-')} | {r['seconds']}s |")
    cap = res["capability"]
    L += ["", "## 能力声明", "", cap["text"], ""]
    if cap.get("max_read_lines"):
        L.append(f"读入可到约 {cap['max_read_lines']} 行。超出时的对策：{'、'.join(cap['countermeasures'])}。")
    L += ["", "说明：测试设计是生成的（同一小模块的大量实例，无深层次、无存储器），"
              "读入耗时主要来自实例数量；真实设计的结果可能不同。容量测试出来前不对外承诺规模。"]
    return "\n".join(L) + "\n"


def write(res: dict, out_dir: Path | None) -> dict:
    out_dir = out_dir or Path(".verimine")
    (out_dir / "capacity.json").write_text(json.dumps(res, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (out_dir / "capacity_report.md").write_text(markdown(res), encoding="utf-8")
    return {"json": str(out_dir / "capacity.json"), "markdown": str(out_dir / "capacity_report.md")}
