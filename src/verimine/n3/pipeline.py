"""N3: trace preparation. Traces for N4 (mining) and N7 (offline checks): from the user's
waveforms, the user's testbench, or generated stimulus (random simulation under the confirmed
assumes, formal exploration of coverage gaps; conservative stimulus when there are no assumes).
Read with wellen, mapped onto N1 paths, sampled per cycle, trimmed, cut into test cases, split by
case into S_mine / S_val, with coverage, a gap list and a quality verdict.

Entry point: run_n3(config_path, workspace, n2_result, ...)
"""
from __future__ import annotations

import gzip
import json
from dataclasses import dataclass, field
from pathlib import Path

from verimine.hashing import file_hash, tagged_hash
from verimine.issues import IssueLog, StageError, Issue
from verimine.n2 import N2Result, run_n2
from verimine.n2.patterns import InstView
from verimine.n2b.checks import Ctx
from verimine.n2b.scan import scan as n2b_scan
from verimine.n3 import cover, explore, stim
from verimine.n3.sample import Case, Design, find_scope, make_cases, sample, wave_names
from verimine.n3.store import write_run, load_run
from verimine.n3.wave import open_wave, reader_name

# Bump when sampling, trimming, splitting or stimulus generation changes: part of the input hash.
N3_VERSION = 3

DEFAULTS = {
    "n3_random_runs": 8, "n3_random_cycles": 200,
    "n3_idle_cycles": 32, "n3_max_toggle_cases": 32,
    "n3_val_percent": 30, "n3_min_state_coverage": 0.8, "n3_min_case_cycles": 2,
    "n3_explore_depth": 20, "n3_explore_timeout": 120, "n3_explore_max": 64,
    "n3_simulator": "auto",
}
LEGALITY = ("user", "constrained", "unknown")


@dataclass
class N3Result:
    n2: N2Result
    cases: list[Case]
    coverage: cover.Coverage
    sources: list[dict]
    explore: dict
    quality: dict
    warnings: list[dict]
    input_hash: str
    traces_hash: str
    db: dict = field(default_factory=dict)

    def sets(self) -> dict:
        out = {}
        for s in ("mine", "val"):
            cs = [c for c in self.cases if c.set == s]
            out[s] = {"cases": len(cs), "cycles": sum(c.cycles for c in cs)}
        return out

    def legality(self) -> dict:
        return {k: sum(1 for c in self.cases if c.legality == k) for k in LEGALITY}

    def to_dict(self) -> dict:
        return {
            "stage": "N3",
            "hashes": self.n2.n1.n0.hashes,
            "n2_input_hash": self.n2.input_hash,
            "input_hash": self.input_hash,
            "traces_hash": self.traces_hash,
            "sources": self.sources,
            "cases": [c.meta() for c in self.cases],
            "sets": self.sets(),
            "legality": self.legality(),
            "coverage": self.coverage.summary(),
            "gaps": self.coverage.gaps(),
            "fsm_coverage": self.coverage.states,
            "explore": self.explore,
            "quality": self.quality,
            "warnings": self.warnings,
            "db": self.db,
        }


# --------------------------------------------------------------------------- #
# Design facts
# --------------------------------------------------------------------------- #

def _design(n2: N2Result, norm: dict, log: IssueLog) -> tuple[Design, dict[str, dict[str, int]], dict[str, int]]:
    elab = n2.n1.elaboration
    top = elab.top_path
    by_path = {i.path: i for i in elab.instances}
    ti = by_path[top]
    nodes: dict[str, tuple[str, str, int]] = {}
    widths: dict[str, dict[str, int]] = {}
    for i in elab.instances:
        w = widths.setdefault(i.path, {})
        for p in i.ports:
            if not p.is_interface:
                nodes[f"{i.path}.{p.name}"] = (i.path, p.name, p.width or 1)
                w[p.name] = p.width or 1
        for s in i.signals:
            w[s.name] = s.width or 1
            if not s.array_dims:
                nodes[f"{i.path}.{s.name}"] = (i.path, s.name, s.width or 1)
    clocks = norm.get("clocks", [])
    if clocks:
        clock, edge = clocks[0]["name"], clocks[0].get("edge", "posedge")
        if len(clocks) > 1:
            log.warn("N3_MULTICLOCK", f"配置了 {len(clocks)} 个时钟，按 {clock} 采样（其余时钟域的采样是近似）")
    else:
        cands = sorted(n2.vdg.clocks.get(top, set()))
        if len(cands) != 1:
            raise StageError([Issue("N3_NO_CLOCK", "没有可用于采样的时钟：请在配置的 clocks 中给出", "clocks")])
        clock, edge = cands[0], "posedge"
        log.warn("N3_CLOCK_INFERRED", f"配置中没有时钟，按敏感列表推断的 {clock} 采样")
    resets = {f"{top}.{r['name']}": r["active"] for r in norm.get("resets", [])}
    special = {clock} | {r["name"] for r in norm.get("resets", [])}
    inputs = [f"{top}.{p.name}" for p in ti.ports if p.direction == "in" and p.name not in special
              and not p.is_interface]
    init_regs = []
    for path in n2.vdg.modeled:
        inst = by_path[path]
        v = InstView(inst, n2.vdg.resets.get(path, {}))
        init_regs += [f"{path}.{r}" for r in v.registers if r not in v.arrays and any(v.in_reset(a) for a in v.writes[r])]
    children = sorted({i.path[len(top) + 1:].split(".")[0] for i in elab.instances if i.parent == top})
    d = Design(top, [p.name for p in ti.ports], children, nodes, clock, edge, resets, inputs, sorted(init_regs))
    special_nodes = {n.id for n in n2.vdg.nodes.values() if n.clock or n.reset}
    toggles = {n: w for n, (inst, _name, w) in nodes.items() if inst in set(n2.vdg.modeled) and n not in special_nodes}
    return d, widths, toggles


# --------------------------------------------------------------------------- #
# Sources
# --------------------------------------------------------------------------- #

def _from_wave(path: Path, d: Design, *, kind, legality, prefix, source, min_cycles, final_sample=False,
               detail=None) -> tuple[list[Case], dict]:
    w = open_wave(path)
    scope, score = find_scope(w, d)
    info = {"source": source, "kind": kind, "legality": legality, "file": str(path), "scope": scope}
    if scope is None or score == 0:
        info["error"] = "波形里找不到设计顶层（没有与顶层端口、子实例同名的作用域）"
        return [], info
    names = wave_names(scope, d)
    present = [n for n, f in names.items() if f in w.vars]
    info["mapped"] = len(present)
    info["nodes"] = len(names)
    if f"{scope}.{d.clock}" not in w.vars:
        info["error"] = f"波形里没有时钟 {scope}.{d.clock}"
        return [], info
    times, sig = sample(w, scope, d, final_sample)
    cases, stats = make_cases(times, sig, d, source=source, kind=kind, legality=legality, prefix=prefix,
                              min_cycles=min_cycles, detail=detail)
    info["trim"] = stats
    info["cases"] = len(cases)
    return cases, info


def _bench(n2: N2Result, norm: dict) -> stim.Bench:
    elab = n2.n1.elaboration
    ti = next(i for i in elab.instances if i.path == elab.top_path)
    ports = [stim.Port(p.name, p.direction, p.width or 1, p.signed, "$" in p.type_str, p.is_interface)
             for p in ti.ports]
    clocks = norm.get("clocks") or [{"name": sorted(n2.vdg.clocks.get(elab.top_path, set()))[0], "edge": "posedge"}]
    return stim.Bench(ti.module, dict(norm["design"].get("parameters", {})), ports,
                      [{"name": c["name"], "edge": c.get("edge", "posedge")} for c in clocks],
                      [{"name": r["name"], "active": r["active"]} for r in norm.get("resets", [])])


def run_n3(
    config_path: Path,
    workspace: Path | None = None,
    n2_result: N2Result | None = None,
    env: dict | None = None,
    tools: dict | None = None,
    write: bool = True,
    auto: bool = False,
    explore_gaps: bool | None = None,
    formal: bool = True,
    reuse: bool = True,
) -> N3Result:
    """auto: add generated stimulus even when the user gave waveforms or a testbench.
    explore_gaps: formal exploration of coverage gaps (default: on for generated stimulus).
    reuse: with the same input hash, load the stored traces instead of simulating again."""
    if n2_result is None:
        n2_result = run_n2(config_path, workspace=workspace, env=env, tools=tools, write=write)
    n2 = n2_result
    n1, n0 = n2.n1, n2.n1.n0
    norm = n0.normalized
    design = norm["design"]
    root = n0.root
    workspace = Path(workspace) if workspace else n0.workspace
    params = DEFAULTS | {k: v for k, v in norm.get("run", {}).get("params", {}).items() if k in DEFAULTS}
    seed = int(norm.get("run", {}).get("seed", 1))
    log = IssueLog()
    d, widths, toggle_nodes = _design(n2, norm, log)

    files = [root / f for f in design.get("files", [])]
    incdirs = [root / x for x in design.get("include_dirs", [])]
    defines = {k: (str(v) if v is not None else None) for k, v in design.get("defines", {}).items()}
    waves = [root / w for w in norm.get("waveforms", [])]
    tb = norm.get("testbench")
    assumes = norm.get("assumes", [])
    cons, prev, bad, hist = stim.assume_constraints(assumes)
    have_assumes = bool(assumes) and not bad

    ih = tagged_hash("n3_input", {
        "n2_input_hash": n2.input_hash, "build_hash": n0.hashes["build_hash"], "env_hash": n0.hashes["env_hash"],
        "assumes": assumes, "waveforms": [[w.name, file_hash(w)[0]] for w in waves],
        "testbench": tb and (tb | {"files": [[f, file_hash(root / f)[0]] for f in tb["files"]]}),
        "params": params, "seed": seed, "auto": auto, "explore": explore_gaps, "formal": formal,
        "n3_version": N3_VERSION,
    })
    work = workspace / "n3" / ih[:16]
    min_cycles = int(params["n3_min_case_cycles"])

    branches = cover.branch_targets(n1.elaboration.instances, set(n2.vdg.modeled), n2.vdg.resets)
    cached = load_run(workspace / "verimine.db", workspace / "traces", ih) if (reuse and write) else None
    if cached is not None:
        cases, sources, explore_info = cached
        cov = cover.compute(cases, branches, n2.fsms, widths, toggle_nodes)
        _mark_unreachable(cov, explore_info)
        return _finish(n2, cases, cov, sources, explore_info, params, log, ih, workspace, write, cached=True)

    cases: list[Case] = []
    sources: list[dict] = []

    # Step 1: user waveforms first, then the user's testbench
    for k, w in enumerate(waves):
        cs, info = _from_wave(w, d, kind="waveform", legality="user", prefix=f"wave{k}:{w.name}",
                              source=w.relative_to(root).as_posix(), min_cycles=min_cycles)
        cases += cs
        sources.append(info)
        if "error" in info:
            log.warn("N3_WAVE_UNUSABLE", f"{w.name}：{info['error']}")
    if not waves and tb:
        try:
            dut = tb.get("dut")
            (work / "tb").mkdir(parents=True, exist_ok=True)
            root_v = work / "tb" / "vm_n3_root.sv"
            root_v.write_text(stim.root_text(tb["top"], dut), encoding="utf-8")
            comp = stim.compile_sim([*files, *[root / f for f in tb["files"]], root_v], "vm_n3_root", incdirs,
                                    defines, work / "tb", tb.get("simulator", "auto"), tb.get("timeout", 300))
            for r in tb["runs"]:
                run = stim.run_sim(comp, r["name"], r["args"], tb.get("timeout", 300))
                if run.vcd is None:
                    sources.append({"source": f"testbench:{r['name']}", "kind": "testbench", "error": run.message})
                    log.warn("N3_TB_FAILED", f"testbench 运行 {r['name']} 没有产生波形：{run.message[-200:]}")
                    continue
                cs, info = _from_wave(run.vcd, d, kind="testbench", legality="user", prefix=f"tb:{r['name']}",
                                      source=f"testbench:{r['name']}", min_cycles=min_cycles,
                                      detail={"simulator": comp.simulator, "args": r["args"]})
                cases += cs
                sources.append(info)
        except stim.StimError as e:
            log.warn("N3_TB_FAILED", str(e))

    # Step 2, 9, 10: generated stimulus
    generated = auto or not (waves or tb)
    explore_info: dict = {"ran": False}
    if generated:
        b = _bench(n2, norm)
        bad_ports = stim.unsupported_ports(b)
        if bad_ports:
            log.warn("N3_AUTO_UNSUPPORTED", f"顶层端口 {', '.join(bad_ports)} 是接口或非压缩数组，无法自动激励")
        else:
            if bad:
                log.warn("N3_ASSUME_NOT_ENFORCEABLE",
                         f"假设 {', '.join(bad)} 含序列或采样函数，随机激励无法保证满足：只生成保守激励")
            elif not assumes:
                log.warn("N3_NO_ASSUMES", "没有已确认的假设：只生成保守激励（复位序列、单点翻转），"
                                           "trace_legality = unknown；由此挖出的候选默认降权并进入 REVIEW_REQUIRED")
            b.constraints, b.prev, b.hist = (cons, prev, hist) if have_assumes else ([], [], [])
            (work / "gen").mkdir(parents=True, exist_ok=True)
            tbv = work / "gen" / "vm_n3_tb.sv"
            tbv.write_text(stim.bench_text(b), encoding="utf-8")
            try:
                comp = stim.compile_sim([*files, tbv], "vm_n3_tb", incdirs, defines, work / "gen",
                                        str(params["n3_simulator"]))
                runs = []
                if have_assumes:
                    for k in range(int(params["n3_random_runs"])):
                        runs.append(("random", f"random_{k}", ["+mode=0", f"+seed={seed * 1000 + k}",
                                                               f"+cycles={params['n3_random_cycles']}"]))
                else:
                    runs.append(("idle", "idle", ["+mode=1", f"+cycles={params['n3_idle_cycles']}"]))
                    nbits = sum(max(1, p.width) for p in b.data_inputs)
                    for k in range(min(nbits, int(params["n3_max_toggle_cases"]))):
                        runs.append(("toggle", f"toggle_{k}", ["+mode=2", f"+bit={k}",
                                                               f"+cycles={params['n3_idle_cycles']}"]))
                    if nbits > int(params["n3_max_toggle_cases"]):
                        log.warn("N3_TOGGLE_CAPPED", f"输入共 {nbits} 位，单点翻转只做前 {params['n3_max_toggle_cases']} 位")
                legality = "constrained" if have_assumes else "unknown"
                stuck = 0
                for kind, name, args in runs:
                    run = stim.run_sim(comp, name, args)
                    if run.stuck_at is not None:
                        stuck += 1
                    if run.vcd is None:
                        log.warn("N3_SIM_FAILED", f"{name}：{run.message[-200:]}")
                        continue
                    cs, info = _from_wave(run.vcd, d, kind=kind, legality=legality, prefix=name, source=name,
                                          min_cycles=min_cycles,
                                          detail={"simulator": comp.simulator, "args": args,
                                                  "stuck_at": run.stuck_at})
                    cases += cs
                    sources.append(info)
                if stuck:
                    log.warn("N3_REJECTION_STUCK", f"{stuck} 次随机仿真在某一拍找不到满足假设的输入（拒绝采样 {stim.MAX_TRIES} 次），"
                                                   "仿真在那一拍提前结束")
            except stim.StimError as e:
                log.warn("N3_SIM_FAILED", str(e))

    cases = _dedupe(cases)
    cov = cover.compute(cases, branches, n2.fsms, widths, toggle_nodes)

    # Step 2: formal exploration of the gaps
    want_explore = (generated and have_assumes) if explore_gaps is None else explore_gaps
    if want_explore and formal:
        explore_info = _explore(n2, norm, d, cov, branches, files, incdirs, defines, root, work, params,
                                have_assumes, min_cycles, log)
        if explore_info.get("cases"):
            cases = _dedupe(cases + explore_info.pop("cases"))
            cov = cover.compute(cases, branches, n2.fsms, widths, toggle_nodes)
        else:
            explore_info.pop("cases", None)
    _mark_unreachable(cov, explore_info)

    # Step 5: split by case
    _split(cases, seed, int(params["n3_val_percent"]), log)
    return _finish(n2, cases, cov, sources, explore_info, params, log, ih, workspace, write)


def _dedupe(cases: list[Case]) -> list[Case]:
    seen, out = set(), []
    for c in cases:
        if c.hash not in seen:
            seen.add(c.hash)
            out.append(c)
    return out


def _explore(n2, norm, d, cov, branches, files, incdirs, defines, root, work, params, have_assumes,
             min_cycles, log) -> dict:
    from verimine.n2b.engine import find_tools

    info: dict = {"ran": False}
    if not find_tools()[0]:
        log.warn("N3_NO_ENGINE", "没有 sby / yosys / SMT 求解器，跳过形式探索")
        return info
    if n2.n1.yosys_report.blocked:
        log.warn("N3_NO_ENGINE", "Yosys 读不了这个设计（见 N1 基线），跳过形式探索")
        return info
    top_clock = {"name": d.clock, "edge": d.edge}
    dropped = explore.assumes_usable(norm.get("assumes", []), top_clock)
    if dropped:
        log.warn("N3_EXPLORE_SKIPPED", f"假设 {', '.join(dropped)} 无法加入形式模型，跳过形式探索")
        return info
    gaps = cov.gaps()
    gap_branches = [b for b in branches if b.status == "gap"]
    sc = n2b_scan(files, norm["design"]["top"], incdirs, defines, root)
    cx = Ctx(n2, sc, {})
    unreachable_now = {(x["inst"], x["state"], x["value"]) for x in gaps["unreachable_states"]}
    trans = [t for t in gaps["transitions"]
             if (t["inst"], t["state"], t["from"]) not in unreachable_now
             and (t["inst"], t["state"], t["to"]) not in unreachable_now]
    ts = explore.targets(cx, gap_branches, gaps["states"], trans, int(params["n3_explore_max"]))
    info = {"ran": True, "targets": len(ts), "reached": 0, "unreachable": [], "unreached": [], "cases": []}
    if not ts:
        return info
    res = explore.run(ts, n1=n2.n1, scan=sc, files=files, include_dirs=incdirs, defines=defines,
                      top=norm["design"]["top"], root=root, norm=norm, top_clock=top_clock,
                      workdir=work / "formal", depth=int(params["n3_explore_depth"]),
                      timeout=int(params["n3_explore_timeout"]), prove=True)
    info["engine"] = {"solver": res.solver, "depth": res.depth, "runs": res.runs, "errors": res.errors}
    legality = "constrained" if have_assumes else "unknown"
    traces: dict[str, list] = {}
    for t in ts:
        p = t.prop
        entry = {"kind": t.kind, "inst": t.inst, "target": t.text, "status": p.status}
        if p.status == "REACHED" and p.trace:
            traces.setdefault(p.trace, []).append(entry)
            info["reached"] += 1
        elif p.status == "UNREACHABLE":
            info["unreachable"].append(entry)
        else:
            info["unreached"].append(entry)
    for k, (trace, entries) in enumerate(sorted(traces.items())):
        cs, _src = _from_wave(Path(trace), d, kind="formal", legality=legality, prefix=f"formal_{k}",
                              source=f"formal:{entries[0]['target']}", min_cycles=1, final_sample=True,
                              detail={"targets": [e["target"] for e in entries]})
        info["cases"] += cs
    return info


def _mark_unreachable(cov: cover.Coverage, info: dict) -> None:
    """Targets proved unreachable are not gaps: no stimulus can reach them."""
    proven = {(e["kind"], e["inst"], e["target"]) for e in info.get("unreachable", [])}
    for b in cov.branches:
        if b.status == "gap" and ("branch", b.inst, b.text) in proven:
            b.status = "unreachable"
    for f in cov.states:
        f["unreachable"] = sorted(v for v in f["states"] if v not in f["visited"]
                                  and ("state", f["inst"], f"{f['state']} == {f['names'].get(v, str(v))}") in proven)
        # a transition into or out of an unreachable state cannot happen either
        f["unreachable_transitions"] = [t for t in f["transitions"] if t not in f["transitions_seen"]
                                        and (t[0] in f["unreachable"] or t[1] in f["unreachable"])]


def _split(cases: list[Case], seed: int, val_percent: int, log: IssueLog) -> None:
    keys = {c.hash: int(tagged_hash("n3_split", {"seed": seed, "case": c.hash}), 16) for c in cases}
    for c in cases:
        c.set = "val" if keys[c.hash] % 100 < val_percent else "mine"
    if len(cases) >= 2:
        for want in ("val", "mine"):
            if not any(c.set == want for c in cases):
                pick = min(cases, key=lambda c: keys[c.hash]) if want == "val" else max(cases, key=lambda c: keys[c.hash])
                pick.set = want
    elif cases:
        log.warn("N3_SINGLE_CASE", "只有一个测试用例：全部进入挖掘集 S_mine，没有验证集 S_val")


def _finish(n2, cases, cov, sources, explore_info, params, log, ih, workspace, write, cached=False) -> N3Result:
    summ = cov.summary()
    st, br = summ["state"], summ["branch"]
    basis, ratio = ("state", st["ratio"]) if st["total"] else ("branch", br["ratio"])
    thr = float(params["n3_min_state_coverage"])
    low = ratio is None or ratio < thr
    quality = {"basis": basis, "ratio": ratio, "threshold": thr, "verdict": "low" if low else "ok",
               "cases": len(cases), "cycles": sum(c.cycles for c in cases)}
    if not cases:
        log.warn("N3_NO_TRACES", "没有得到任何可用的轨迹")
    elif low:
        hint = "启用形式探索（--explore）" if not explore_info.get("ran") else "补充定向激励或 assume（形式探索后仍有缺口）"
        log.warn("N3_LOW_COVERAGE", f"{'状态' if basis == 'state' else '分支'}覆盖 {ratio:.0%} 低于阈值 {thr:.0%}："
                                    f"建议补充激励或{hint}")
    if any(c.legality == "unknown" for c in cases) and not any(w.code == "N3_NO_ASSUMES" for w in log.warnings):
        log.warn("N3_LEGALITY_UNKNOWN", "部分轨迹 trace_legality = unknown，由此挖出的候选默认降权并进入 REVIEW_REQUIRED")
    traces_hash = tagged_hash("n3_traces", sorted([c.id, c.hash, c.set, c.legality] for c in cases))
    res = N3Result(n2, cases, cov, sources, explore_info, quality, [w.to_dict() for w in log.warnings], ih,
                   traces_hash, {"new_run": False, "cached": cached})
    if write and not cached:
        tdir = workspace / "traces"
        tdir.mkdir(parents=True, exist_ok=True)
        for c in cases:
            f = tdir / f"{c.hash}.json.gz"
            if not f.exists():
                f.write_bytes(gzip.compress(json.dumps({"hash": c.hash, "cycles": c.cycles, "signals": c.signals},
                                                       sort_keys=True).encode(), mtime=0))
        res.db = write_run(workspace / "verimine.db", res) | {"cached": False}
    if write:
        run_dir = workspace / "runs" / n2.n1.n0.hashes["run_hash"]
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "n3.json").write_text(json.dumps(res.to_dict(), indent=2, ensure_ascii=False, sort_keys=True,
                                                    default=list) + "\n", encoding="utf-8")
        from verimine.n3.report import markdown
        (run_dir / "n3_report.md").write_text(markdown(res, reader_name()), encoding="utf-8")
    return res
