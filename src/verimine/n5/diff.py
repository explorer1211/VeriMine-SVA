"""N5 step 4: the three-way differential test. The same input vector goes three ways:

  IR        the IR is evaluated offline (Python, sampled-value semantics) on the trace
  sim       the delivery version (concurrent SVA) runs in Verilator; failures and cover hits
            are printed by action blocks
  formal    the verification version (Yosys immediate assertions) runs in SymbiYosys with every
            input pinned to the vector by an assumption: BMC gives each assertion's first
            failing step, cover mode each cover's first step

The IR is evaluated twice, on the simulation trace and on the formal model's own trace (a cover
on the last step of the vector yields it). That separates the two questions:

  IR(sim trace)    == sim       the delivery rendering means what the IR means
  IR(formal trace) == formal    the verification rendering means what the IR means
  sim trace        == formal trace   Verilator and Yosys agree on the design itself

A disagreement in one of the first two is a rendering error (spec: 不一致说明渲染有语义错误).
The third is reported separately: it is the design model, not the rendering.

The vector: up to `n5_diff_cases` N3 test cases, each cut to `n5_diff_cycles`, each preceded by
reset cycles (so `disable iff` is exercised in the middle of the vector, not only at the start).

The checker modules are instantiated in the test bench with hierarchical port connections:
Verilator does not take `bind` to an instance path, so the bind statements themselves are left
to the slang compile (N6). The checker text is the delivery text plus action blocks.
"""
from __future__ import annotations

import copy
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from verimine.n1.assign import tree_refs
from verimine.n2b import engine
from verimine.n2b.props import EQ, Prop, const, ref
from verimine.n2b.render import _port_name, checker_module, refs
from verimine.n5 import render as R
from verimine.n3.sample import Design, find_scope, sample
from verimine.n3.wave import open_wave
from verimine.n3a.cands import series
from verimine.n4.pool import _active_rows

PERIOD = 10
END = "vm_n5_end"


@dataclass
class Vector:
    clock: dict                       # {"name", "edge"}
    ports: list[tuple[str, int]]      # driven top inputs (name, width), resets first
    rows: list[list[int]]
    segments: list[dict] = field(default_factory=list)
    n_resets: int = 0

    @property
    def n(self) -> int:
        return len(self.rows)


def build_vector(cases, d: Design, params: dict) -> Vector | None:
    top = d.top
    resets = {n[len(top) + 1:]: lv for n, lv in sorted(d.resets.items())}
    data = [(x[len(top) + 1:], d.nodes[x][2]) for x in d.inputs]
    ports = [(r, 1) for r in resets] + data
    order = {"user": 0, "constrained": 1, "unknown": 2}
    pick = sorted(cases, key=lambda c: (order.get(c.legality, 3), c.set != "val", c.id))
    pick = [c for c in pick if all(f"{top}.{x}" in c.signals for x, _ in data)][: int(params["n5_diff_cases"])]
    if not pick:
        return None
    cyc, rcyc = int(params["n5_diff_cycles"]), int(params["n5_diff_reset_cycles"])
    rows, segs = [], []
    for c in pick:
        start = len(rows)
        for _ in range(rcyc):
            rows.append([0 if lv == "low" else 1 for lv in resets.values()] + [0] * len(data))
        m = min(cyc, c.cycles)
        for i in range(m):
            vals = [c.signals[f"{top}.{x}"][i] for x, _ in data]
            rows.append([1 if lv == "low" else 0 for lv in resets.values()] + [v or 0 for v in vals])
        segs.append({"case": c.id, "start": start, "reset_rows": rcyc, "rows": m})
    return Vector({"name": d.clock, "edge": d.edge}, ports, rows, segs, len(resets))


# --------------------------------------------------------------------------- #
# IR offline
# --------------------------------------------------------------------------- #

def ir_hits(p: Prop, sig: dict[str, list], n: int, widths: dict[str, int], ante: bool = False) -> list[int]:
    """Rows where an assertion fails / a cover holds, under the IR's semantics: sampled values,
    `disable iff` over the whole attempt (both cycles of `|=>`), unknown values never count.
    ante=True: rows where the assertion's attempt starts with a true antecedent instead."""
    pre = p.inst + "."
    cols = {k[len(pre):]: v for k, v in sig.items() if k.startswith(pre)}
    need = tree_refs(p.expr) | (tree_refs(p.guard) if p.guard else set())
    if not need <= set(cols):
        return []
    act = _active_rows(type("C", (), {"resets": p.resets})(), cols, n)
    e = series(p.expr, cols, n, widths)
    if p.kind == "cover":
        return [i for i in range(n) if act[i] and e[i]]
    g = series(p.guard, cols, n, widths) if p.guard else [1] * n
    out = []
    for i in range(n):
        j = i + 1 if p.next else i
        if j < n and act[i] and act[j] and g[i] and (ante or (e[j] is not None and not e[j])):
            out.append(i if ante else j)
    return out


# --------------------------------------------------------------------------- #
# Simulation (delivery version)
# --------------------------------------------------------------------------- #

def bench_text(top_inst, params: dict, vec: Vector, checkers: list[tuple[str, str, list[str]]]) -> str:
    """checkers: (checker module, instance path, signal names relative to it)."""
    clk = vec.clock
    act, inact = ("1", "0") if clk["edge"] == "posedge" else ("0", "1")
    L = ["`timescale 1ns/1ps", "module vm_n5_tb;", f"  reg {clk['name']} = 1'b{inact};", "  integer vm_n5_cyc = 0;"]
    resets = {n for n, _ in vec.ports[: vec.n_resets]}
    for (name, w), v in zip(vec.ports, vec.rows[0]):
        # resets start inactive so the first row's reset is an edge (async resets fire at time 0)
        init = (1 - v) if name in resets else 0
        L.append(f"  reg {f'[{w - 1}:0] ' if w > 1 else ''}{name} = {init};")
    driven = {clk["name"]} | {n for n, _ in vec.ports}
    for p in top_inst.ports:
        if p.name in driven:
            continue
        w = max(1, p.width or 1)
        kind = "reg" if p.direction == "in" else "wire"
        L.append(f"  {kind}{' signed' if p.signed else ''}{f' [{w - 1}:0]' if w > 1 else ''} {p.name};")
    pars = ", ".join(f".{k}({v})" for k, v in sorted(params.items()))
    conns = ", ".join(f".{p.name}({p.name})" for p in top_inst.ports)
    L.append(f"  {top_inst.module} {'#(' + pars + ') ' if pars else ''}dut ({conns});")
    top = top_inst.path
    for k, (mod, inst, names) in enumerate(checkers):
        rel = inst[len(top):].lstrip(".")
        base = "vm_n5_tb.dut" + (f".{rel}" if rel else "")
        cs = ", ".join(f".{_port_name(n)}({base}.{n})" for n in names)
        L.append(f"  {mod} vm_n5_c{k} ({cs});")
    L += ["  initial begin", '    $dumpfile("trace.vcd");', "    $dumpvars(0, dut);"]
    for i, row in enumerate(vec.rows):
        sets = " ".join(f"{n} = {w}'d{v};" for (n, w), v in zip(vec.ports, row))
        L.append(f"    vm_n5_cyc = {i}; {sets} #{PERIOD // 2} {clk['name']} = 1'b{act}; #{PERIOD // 2} {clk['name']} = 1'b{inact};"
                 + (f"  // {i}" if i % 10 == 0 else ""))
    L += ['    $display("VM_N5_DONE");', "    $finish;", "  end", "endmodule", ""]
    return "\n".join(L)


_LINE = re.compile(r"VM_(FAIL|COVER) (\S+) (\d+)")


def run_sim(files, include_dirs, defines, checkers_text: str, tb_text: str, workdir: Path, timeout: int) -> dict:
    if not shutil.which("verilator"):
        return {"error": "没有 Verilator：交付版（并发 SVA）需要它仿真"}
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "vm_n5_checkers.sv").write_text(checkers_text, encoding="utf-8")
    (workdir / "vm_n5_tb.sv").write_text(tb_text, encoding="utf-8")
    obj = workdir / "obj"
    cmd = ["verilator", "--binary", "--timing", "--trace", "--assert", "--coverage-user", "-Wno-fatal", "-Wno-lint",
           "-Wno-style", "-Wno-MULTITOP", "--top-module", "vm_n5_tb", "-Mdir", str(obj),
           *[f"-I{d}" for d in include_dirs],
           *[f"-D{k}={v}" if v is not None else f"-D{k}" for k, v in sorted(defines.items())],
           *[str(f) for f in files], str(workdir / "vm_n5_checkers.sv"), str(workdir / "vm_n5_tb.sv")]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=workdir)
        if r.returncode != 0:
            return {"error": "Verilator 编译失败：" + " ".join((r.stdout + r.stderr).strip().splitlines()[:4])}
        r = subprocess.run([str(obj / "Vvm_n5_tb")], capture_output=True, text=True, timeout=timeout, cwd=workdir)
    except subprocess.TimeoutExpired:
        return {"error": "Verilator 超时"}
    out = r.stdout + r.stderr
    if "VM_N5_DONE" not in out:
        return {"error": "仿真没有跑完：" + out[-300:]}
    hits: dict[str, list[int]] = {}
    for kind, name, t in _LINE.findall(out):
        hits.setdefault(name, []).append(int(t))
    return {"hits": {k: sorted(set(v)) for k, v in hits.items()}, "vcd": workdir / "trace.vcd"}


# --------------------------------------------------------------------------- #
# Formal (verification version)
# --------------------------------------------------------------------------- #

def env_lines(vec: Vector) -> list[str]:
    clk = vec.clock
    bits = max(1, (vec.n).bit_length())
    W = sum(w for _, w in vec.ports)
    L = [f"  reg [{bits}:0] vm_n5_step = 0;",
         f"  always @({clk['edge']} {clk['name']}) if (vm_n5_step < {vec.n}) vm_n5_step <= vm_n5_step + 1;",
         f"  reg [{W - 1}:0] vm_n5_vec;",
         "  always @* case (vm_n5_step)"]
    for i, row in enumerate(vec.rows):
        v = 0
        for (_n, w), x in zip(vec.ports, row):
            v = (v << w) | (x & ((1 << w) - 1))
        L.append(f"    {i}: vm_n5_vec = {W}'d{v};")
    L += [f"    default: vm_n5_vec = {W}'d0;", "  endcase",
          "  always @* assume({" + ", ".join(n for n, _ in vec.ports) + "} == vm_n5_vec);"]
    return L


def run_formal(props: list[Prop], vec: Vector, ctx: dict, workdir: Path, timeout: int) -> dict:
    ps = [copy.deepcopy(p) for p in props]
    for p in ps:
        p.status, p.verification, p.skip, p.step, p.trace = "", "", "", None, ""
    end = Prop("cover", "n5.end", ctx["top_path"], ctx["top"], EQ(ref("vm_n5_step"), const(vec.n - 1)),
               clock=vec.clock, message="向量最后一拍")
    end.id = END
    # Yosys registers clocked checks: a check on the values of step k is reported at step k + 1.
    res = engine.run(ps + [end], assumes=[], workdir=workdir, depth=vec.n + 2, timeout=timeout, prove=False,
                     extra_env=env_lines(vec), zero_init=True,
                     render=lambda p, lab: R.n5_verification(p, lab),
                     **{k: v for k, v in ctx.items() if k not in ("top_path", "parameters")})
    if not res.available:
        return {"error": "没有形式引擎（sby / yosys / SMT 求解器）"}
    if res.errors:
        return {"error": "；".join(res.errors)[:400]}
    if end.status != "REACHED" or not end.trace:
        return {"error": f"形式模型走不完整个向量（{end.status}），输入钉住后可能与配置的复位假设冲突"}
    shift = lambda p: (p.status, None if p.step is None else p.step - 1, p.skip)  # noqa: E731
    return {"props": {p.id: shift(p) for p in ps}, "vcd": Path(end.trace)}


# --------------------------------------------------------------------------- #

def _wave_rows(path: Path, d: Design, scope_hint: str | None, final: bool) -> tuple[dict[str, list], int]:
    w = open_wave(path)
    scope = None
    if scope_hint:
        cands = sorted({n[: -len(f".{d.clock}")] for n in w.vars if n.endswith(f"{scope_hint}.{d.clock}")}, key=len)
        scope = cands[0] if cands else None
    if scope is None:
        scope, _ = find_scope(w, d)
    times, sig = sample(w, scope, d, final_sample=final)
    return sig, len(times)


def _cut(sig: dict[str, list], n: int) -> dict[str, list]:
    return {k: (v[:n] + [None] * (n - len(v)))[:n] for k, v in sig.items()}


def run(rendered, vec: Vector, d: Design, widths_of, ctx: dict, by_path: dict, files, include_dirs, defines,
        workdir: Path, params: dict) -> dict:
    """Fills r.diff for every rendered property; returns the run summary."""
    timeout = int(params["n5_diff_timeout"])
    live = [r for r in rendered if r.delivery]
    # simulation
    by_inst: dict[str, list[Prop]] = {}
    for r in live:
        q = copy.copy(r.prop)
        q.delivery = r.sim
        by_inst.setdefault(r.prop.inst, []).append(q)
    mods, checkers = [], []
    for k, (inst, ps) in enumerate(sorted(by_inst.items())):
        info = by_path[inst]
        text, _bind = checker_module(f"vm_n5_sim_{k}", inst, ps, {x.name: (x.width or 1, x.signed)
                                                                   for x in [*info.ports, *info.signals]})
        mods.append(text)
        checkers.append((f"vm_n5_sim_{k}", inst, sorted(set().union(*(refs(p) for p in ps)))))
    top_inst = by_path[ctx["top_path"]]
    sim = run_sim(files, include_dirs, defines, "\n".join(mods),
                  bench_text(top_inst, ctx.get("parameters", {}), vec, checkers), workdir / "sim", timeout)
    # formal
    vprops = [r.prop for r in rendered if r.verification]
    formal = run_formal(vprops, vec, ctx, workdir / "formal", timeout) if vprops else {"error": "没有验证版属性"}

    sim_sig = form_sig = None
    if "vcd" in sim:
        sim_sig, n_sim = _wave_rows(sim["vcd"], d, "vm_n5_tb.dut", False)
        sim_sig = _cut(sim_sig, vec.n)
    if "vcd" in formal:
        form_sig, n_form = _wave_rows(formal["vcd"], d, None, True)
        # sampled "just before each edge": row k holds step k - 1, row 0 is before step 0
        form_sig = _cut({k: v[1:] for k, v in form_sig.items()}, vec.n)
    n = vec.n
    model = {"compared": 0, "mismatches": []}
    if sim_sig and form_sig:
        for node in sorted(set(sim_sig) & set(form_sig)):
            a, b = sim_sig[node][:n], form_sig[node][:n]
            for i, (x, y) in enumerate(zip(a, b)):
                if x is None or y is None:
                    continue
                model["compared"] += 1
                if x != y:
                    model["mismatches"].append({"signal": node, "row": i, "sim": x, "formal": y})
                    break
    bad_nodes = {m["signal"] for m in model["mismatches"]}

    for r in rendered:
        p = r.prop
        w = widths_of(p.inst)
        out: dict = {"status": "not_run"}
        legs = []
        if r.delivery and "hits" in sim:
            ir_s = ir_hits(p, sim_sig or {}, n, w)
            got = sim["hits"].get(r.name, [])
            # sim reports every failure; compare the whole set of rows
            out["ir_sim"], out["sim"] = ir_s, got
            out["delivery_ok"] = ir_s == got
            legs.append(out["delivery_ok"])
        elif r.delivery:
            out["sim_error"] = sim.get("error")
        if r.verification and "props" in formal:
            st, step, skip = formal["props"].get(p.id, ("", None, ""))
            ir_f = ir_hits(p, form_sig or {}, n, w)
            first = ir_f[0] if ir_f else None
            got = step if st in ("FAILED", "REACHED") else None
            out["ir_formal"], out["formal"] = first, {"status": st, "step": step, "skip": skip}
            out["verification_ok"] = first == got and st not in ("ENV_ERROR", "SKIPPED", "")
            legs.append(out["verification_ok"])
        elif r.verification:
            out["formal_error"] = formal.get("error")
        if legs:
            out["status"] = "consistent" if all(legs) else "mismatch"
            if "ir_sim" in out and "ir_formal" in out:
                a = out["ir_sim"][0] if out["ir_sim"] else None
                if a != out["ir_formal"]:
                    reads = {f"{p.inst}.{x}" for x in tree_refs(p.expr) | (tree_refs(p.guard) if p.guard else set())}
                    out["cross"] = "model_mismatch" if reads & bad_nodes or bad_nodes else "differs"
            if p.kind == "assert" and sim_sig:
                out["attempts"] = len(ir_hits(p, sim_sig, n, w, ante=True))   # how much the vector tested it
        r.diff = out
    return {"ran": True, "cycles": n, "segments": vec.segments, "sim_error": sim.get("error"),
            "formal_error": formal.get("error"), "model": model}
