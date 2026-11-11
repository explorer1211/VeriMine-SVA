"""N3 steps 1-2, 9-10: running simulations.

Two things are simulated here:

  user testbench   wrapped in `vm_n3_root`, which instantiates the testbench top and adds the
                   VCD dump; each configured run (plusargs) is one simulation
  generated bench  `vm_n3_tb` instantiates the design top, drives clocks and resets, and
                   in one of three modes drives the data inputs:
                     random  uniform random values from a seeded xorshift generator; each draw
                             is checked against the confirmed assumes and redrawn if it
                             violates one (rejection sampling)
                     idle    reset, then all inputs 0                     (conservative)
                     toggle  reset, then a single input bit high for 4 cycles  (conservative)

Inputs change after the inactive clock edge and settle before the active one, so the sampled
values seen at the active edge are the ones the design consumed. The generator is ours, not
$urandom, so a seed gives the same stimulus in every simulator.

Simulators: Icarus Verilog (fast to compile) first, Verilator as the fallback, or as chosen.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from verimine.sva_text import desugar, past_calls

RESET_CYCLES = 3
MAX_TRIES = 64
_SAMPLED = re.compile(r"\$sampled\b")
_SEQ = re.compile(r"##|\[\*|\[->|\[=|\bs_eventually\b|\beventually\b|\buntil\b|\bthroughout\b|\bwithin\b")
_DOTTED = re.compile(r"(?<![\w.$'])([A-Za-z_][A-Za-z0-9_$]*(?:\.[A-Za-z_][A-Za-z0-9_$]*)+)")


class StimError(Exception):
    pass


@dataclass
class Port:
    name: str
    direction: str
    width: int
    signed: bool
    unpacked: bool
    interface: bool


@dataclass
class Bench:
    top: str                     # design top module
    params: dict
    ports: list[Port]
    clocks: list[dict]           # [{"name", "edge"}]
    resets: list[dict]           # [{"name", "active"}]
    constraints: list[str] = field(default_factory=list)   # Verilog boolean expressions
    prev: list[str] = field(default_factory=list)          # antecedents of |=> assumes
    hist: list[tuple[str, int]] = field(default_factory=list)  # $past(e, k): (e, k) -> vm_h<i>_<k>

    @property
    def data_inputs(self) -> list[Port]:
        special = {c["name"] for c in self.clocks} | {r["name"] for r in self.resets}
        return [p for p in self.ports if p.direction == "in" and p.name not in special]


def assume_constraints(assumes: list[dict]) -> tuple[list[str], list[str], list[str], list[tuple[str, int]]]:
    """(constraints, |=> antecedents, names that cannot be enforced in a testbench, $past
    histories). `$past(e, k)` becomes a register chain sampled at the active edge, so at the
    time inputs are drawn it holds e from k edges ago, as in SVA."""
    cons, prev, bad, hist = [], [], [], []

    def fix(e: str) -> str:
        e = desugar(e.strip())
        for start, end, arg, k in reversed(past_calls(e)):
            arg = fix(arg)
            names = [h[0] for h in hist]
            if arg in names:
                i = names.index(arg)
                hist[i] = (arg, max(hist[i][1], k))
            else:
                i = len(hist)
                hist.append((arg, k))
            e = e[:start] + f"vm_h{i}_{k}" + e[end:]
        return _DOTTED.sub(lambda m: f"dut.{m.group(1)}", e)

    for a in assumes:
        text = a["expr"].strip()
        if _SEQ.search(text) or _SAMPLED.search(text):
            bad.append(a["name"])
            continue
        try:
            if "|=>" in text:
                ante, cons_ = text.split("|=>", 1)
                prev.append(fix(ante))
                cons.append(f"(!vm_prev_{len(prev) - 1} || ({fix(cons_)}))")
            elif "|->" in text:
                ante, cons_ = text.split("|->", 1)
                cons.append(f"(!({fix(ante)}) || ({fix(cons_)}))")
            else:
                cons.append(f"({fix(text)})")
        except ValueError:
            bad.append(a["name"])
    return cons, prev, bad, hist


def unsupported_ports(b: Bench) -> list[str]:
    return [p.name for p in b.ports if p.unpacked or p.interface]


def bench_text(b: Bench) -> str:
    ins = b.data_inputs
    total = max(1, sum(max(1, p.width) for p in ins))
    L = ["`timescale 1ns/1ps", "module vm_n3_tb;"]
    for c in b.clocks:
        L.append(f"  reg {c['name']} = 1'b{0 if c['edge'] == 'posedge' else 1};")
    for r in b.resets:
        L.append(f"  reg {r['name']} = 1'b{0 if r['active'] == 'low' else 1};")
    L.append(f"  reg [{total - 1}:0] vm_in = {total}'d0;")
    off = 0
    for p in ins:
        w = max(1, p.width)
        rng = f" [{w - 1}:0]" if w > 1 else ""
        L.append(f"  wire{' signed' if p.signed else ''}{rng} {p.name} = vm_in[{off + w - 1}:{off}];")
        off += w
    for p in b.ports:
        if p.direction != "in":
            w = max(1, p.width)
            L.append(f"  wire{' signed' if p.signed else ''}{f' [{w - 1}:0]' if w > 1 else ''} {p.name};")
    params = ", ".join(f".{k}({v})" for k, v in sorted(b.params.items()))
    conns = ", ".join(f".{p.name}({p.name})" for p in b.ports)
    L.append(f"  {b.top} {'#(' + params + ') ' if params else ''}dut ({conns});")
    L += ["  reg [63:0] vm_s, vm_r;", "  reg vm_rst;", "  integer vm_mode, vm_cycles, vm_bit, vm_i, vm_k, vm_tries;",
          "  reg [8*16:1] vm_mode_s;"]
    act = " ".join(f"{c['name']} = 1'b{1 if c['edge'] == 'posedge' else 0};" for c in b.clocks)
    inact = " ".join(f"{c['name']} = 1'b{0 if c['edge'] == 'posedge' else 1};" for c in b.clocks)
    rst_on = " ".join(f"{r['name']} = 1'b{0 if r['active'] == 'low' else 1};" for r in b.resets)
    rst_off = " ".join(f"{r['name']} = 1'b{1 if r['active'] == 'low' else 0};" for r in b.resets)
    main_clk = b.clocks[0]
    for i, a in enumerate(b.prev):
        L.append(f"  reg vm_prev_{i} = 1'b0;")
        L.append(f"  always @({main_clk['edge']} {main_clk['name']}) vm_prev_{i} <= vm_rst ? 1'b0 : ({a});")
    for i, (e, k) in enumerate(b.hist):
        regs = " ".join(f"vm_h{i}_{j}" for j in range(1, k + 1))
        L.append(f"  reg [63:0] {', '.join(regs.split())};")
        shift = " ".join(f"vm_h{i}_{j} <= vm_h{i}_{j - 1};" for j in range(k, 1, -1))
        L.append(f"  always @({main_clk['edge']} {main_clk['name']}) begin {shift} vm_h{i}_1 <= ({e}); end")
    ok = " && ".join(b.constraints) or "1'b1"
    chunks = (total + 63) // 64
    draw = []
    for k in range(chunks):
        hi = min(total, 64 * k + 64) - 1
        draw.append(f"      vm_next; vm_in[{hi}:{64 * k}] = vm_r[{hi - 64 * k}:0];")
    L += [
        "  task vm_next;",
        "    begin",
        "      vm_s = vm_s ^ (vm_s << 13); vm_s = vm_s ^ (vm_s >> 7); vm_s = vm_s ^ (vm_s << 17);",
        "      vm_r = vm_s;",
        "    end",
        "  endtask",
        "  task vm_draw;",
        "    begin",
        *draw,
        "    end",
        "  endtask",
        "  task vm_cycle;  // active edge, then inactive edge",
        f"    begin #4 {act} #5 {inact} end",
        "  endtask",
        "  initial begin",
        "    if (!$value$plusargs(\"seed=%d\", vm_s)) vm_s = 1;",
        "    vm_s = vm_s * 64'h9E3779B97F4A7C15 + 64'd1;",
        "    if (!$value$plusargs(\"cycles=%d\", vm_cycles)) vm_cycles = 100;",
        "    if (!$value$plusargs(\"bit=%d\", vm_bit)) vm_bit = 0;",
        "    if (!$value$plusargs(\"mode=%d\", vm_mode)) vm_mode = 0;  // 0 random, 1 idle, 2 toggle",
        "    $dumpfile(\"trace.vcd\");",
        "    $dumpvars(0, dut);",
        f"    vm_rst = 1'b1; {rst_on}",
        f"    for (vm_i = 0; vm_i < {RESET_CYCLES}; vm_i = vm_i + 1) begin #1 vm_cycle; end",
        f"    #1 {rst_off} vm_rst = 1'b0;",
        "    for (vm_i = 0; vm_i < vm_cycles; vm_i = vm_i + 1) begin",
        "      if (vm_mode == 0) begin",
        "        vm_tries = 0;",
        "        vm_draw; #1 vm_tries = vm_tries + 1;",
        f"        while (!({ok}) && vm_tries < {MAX_TRIES}) begin vm_draw; #1 vm_tries = vm_tries + 1; end",
        f"        if (!({ok})) begin $display(\"VM_N3_STUCK %0d\", vm_i); $finish; end",
        "      end else begin",
        "        vm_in = 0;",
        "        if (vm_mode == 2 && vm_i >= 2 && vm_i < 6) vm_in[vm_bit] = 1'b1;",
        "        #1;",
        "      end",
        "      vm_cycle;",
        "    end",
        "    $display(\"VM_N3_DONE %0d\", vm_cycles);",
        "    $finish;",
        "  end",
        "endmodule",
        "",
    ]
    return "\n".join(L)


def root_text(tb_top: str, dut: str | None) -> str:
    """Wrapper for a user testbench: adds the dump without touching the testbench."""
    target = f"vm_n3_root.{dut}" if dut else f"vm_n3_root.{tb_top}"
    return (f"`timescale 1ns/1ps\nmodule vm_n3_root;\n  {tb_top} {tb_top} ();\n"
            f"  initial begin $dumpfile(\"trace.vcd\"); $dumpvars(0, {target}); end\nendmodule\n")


# --------------------------------------------------------------------------- #
# Simulators
# --------------------------------------------------------------------------- #

def available(sim: str) -> bool:
    return bool(shutil.which("iverilog") and shutil.which("vvp")) if sim == "icarus" else bool(shutil.which("verilator"))


@dataclass
class Compiled:
    simulator: str
    exe: list[str]
    workdir: Path
    log: str = ""


def compile_sim(files: list[Path], top: str, include_dirs: list[Path], defines: dict, workdir: Path,
                simulator: str = "auto", timeout: int = 300) -> Compiled:
    workdir.mkdir(parents=True, exist_ok=True)
    order = ["icarus", "verilator"] if simulator == "auto" else [simulator]
    errors = []
    for sim in order:
        if not available(sim):
            errors.append(f"{sim}: 未安装")
            continue
        incs = [f"-I{d}" for d in include_dirs]
        defs = [f"-D{k}={v}" if v is not None else f"-D{k}" for k, v in sorted(defines.items())]
        if sim == "icarus":
            cmd = ["iverilog", "-g2012", "-o", str(workdir / "sim.vvp"), "-s", top, *incs, *defs,
                   *[str(f) for f in files]]
            exe = ["vvp", "-n", str(workdir / "sim.vvp")]
        else:
            obj = workdir / "obj"
            cmd = ["verilator", "--binary", "--timing", "--trace", "-Wno-fatal", "-Wno-lint", "-Wno-style",
                   "-Wno-MULTITOP", "--top-module", top, "-Mdir", str(obj), *incs, *defs, *[str(f) for f in files]]
            exe = [str(obj / f"V{top}")]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=workdir)
        except subprocess.TimeoutExpired:
            errors.append(f"{sim}: 编译超时")
            continue
        if r.returncode == 0:
            return Compiled(sim, exe, workdir, (r.stdout + r.stderr)[-2000:])
        errors.append(f"{sim}: " + " ".join((r.stdout + r.stderr).strip().splitlines()[:3]))
    raise StimError("仿真器编译失败：" + "；".join(errors))


@dataclass
class SimRun:
    name: str
    vcd: Path | None
    ok: bool
    stuck_at: int | None = None
    message: str = ""


def run_sim(c: Compiled, name: str, args: list[str], timeout: int = 300) -> SimRun:
    d = c.workdir / "runs" / re.sub(r"[^\w.-]", "_", name)
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)
    try:
        r = subprocess.run([*c.exe, *args], capture_output=True, text=True, timeout=timeout, cwd=d)
        out = r.stdout + r.stderr
        ok = r.returncode == 0
    except subprocess.TimeoutExpired:
        out, ok = "仿真超时", False
    vcd = d / "trace.vcd"
    m = re.search(r"VM_N3_STUCK (\d+)", out)
    return SimRun(name, vcd if vcd.exists() and vcd.stat().st_size else None, ok or vcd.exists(),
                  int(m.group(1)) if m else None, out[-1000:])
