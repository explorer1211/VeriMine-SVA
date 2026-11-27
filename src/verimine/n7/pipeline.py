"""N7: verification, coverage and the closed loop. N6's compiled properties in; proof results,
coverage and the gap list out.

  1  the IR is checked offline on S_val; trace-derived candidates refuted there are eliminated as
     trace_overfit (offline.py)
  2  every antecedent cover is proved reachable or not; unreachable -> VACUOUS
  3  layered proof L0 / L1 / L2 (prove.py)
  4  sim_status + formal_status + cover_status -> one of the seven statuses (status.py)
  5  cost order by COI size and importance; timeouts -> resource_limit
  6  jobs run in threads, subprocesses or on Slurm (sched.py, jobs.py)
  7  COI coverage, proof-core coverage (absent), vacuity, reachability (coverage.py)
  8  gap list: logic no PROVEN assertion touches
  9  closed loop: gaps -> N4 (extra gap signals) -> N5 -> N6 -> N7, at most `n7_loop_rounds`
 10  the report says COI coverage is touch, not sufficiency, and gives mutation kill beside it

Entry point: run_n7(config_path, workspace, n6_result, ...)
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from verimine import engines
from verimine.hashing import tagged_hash
from verimine.issues import IssueLog
from verimine.n1.assign import tree_refs
from verimine.n2b.props import Prop
from verimine.n2b.scan import scan as n2b_scan
from verimine.n3 import cover as n3cover
from verimine.n3.pipeline import _design
from verimine.n4 import run_n4
from verimine.n5 import run_n5
from verimine.n6 import N6Result, run_n6
from verimine.n7 import coi, coverage as n7coverage, mutation, offline, prove, reach
from verimine.n7.status import merge

N7_VERSION = 1
DEFAULTS = {
    "n7_l0_depth": 12, "n7_timeout": 120, "n7_l1_k": 8, "n7_l1_depth": 20,
    "n7_l2_depth": 64, "n7_l2_timeout": 300, "n7_l2_top": 8, "n7_use_pagerank": False,
    "n7_backend": "thread", "n7_jobs": 1, "n7_reach": True, "n7_reach_max": 64,
    "n7_mutants": 8, "n7_mut_timeout": 120, "n7_vec_cases": 2, "n7_vec_cycles": 30,
    "n7_loop_rounds": 0, "n7_loop_target": 0.9, "n7_loop_epsilon": 0.01,
}


@dataclass
class N7Prop:
    name: str
    kind: str                    # assert | cover
    prop: Prop
    variant: int
    cand: object | None = None   # N4 Cand
    n6: object | None = None     # N6Prop
    is_reach: bool = False
    sim: dict = field(default_factory=dict)
    formal: dict = field(default_factory=dict)
    cover: str = "NOT_RUN"
    status: str = ""
    eliminated: str = ""
    coi: dict = field(default_factory=dict)
    importance: float = 0.0

    @property
    def reason(self) -> str:
        return self.eliminated or self.formal.get("skip", "")

    def to_dict(self, full: bool = True) -> dict:
        d = {"name": self.name, "kind": self.kind, "is_reach": self.is_reach,
             "status": self.status, "cover": self.cover, "eliminated": self.eliminated,
             "sim_status": self.sim.get("status", "not_run"), "formal_status": self.formal.get("status", "NOT_RUN"),
             "formal_layer": self.formal.get("layer"), "step": self.formal.get("step"),
             "coi": self.coi, "importance": round(self.importance, 6)}
        if self.cand is not None:
            d |= {"cand": self.cand.id, "scope": self.cand.scope, "type": self.cand.type,
                  "sources": self.cand.sources, "legality": self.cand.legality, "text": self.cand.text}
        else:
            d |= {"scope": self.prop.inst, "text": self.prop.message}
        if full:
            d |= {"sim": self.sim, "formal": self.formal}
        return d


@dataclass
class N7Result:
    n6: N6Result
    props: list[N7Prop]
    coverage: dict
    gaps: dict
    layers: dict
    rounds: list[dict]
    warnings: list[dict]
    input_hash: str
    n7_hash: str
    db: dict = field(default_factory=dict)

    @property
    def asserts(self) -> list[N7Prop]:
        return [p for p in self.props if p.kind == "assert"]

    def counts(self) -> dict:
        out = {s: 0 for s in ("ENV_ERROR", "FAILED", "VACUOUS", "PROVEN", "BOUNDED", "WEAK_EVIDENCE",
                              "REVIEW_REQUIRED")}
        for p in self.asserts:
            out[p.status] = out.get(p.status, 0) + 1
        sim = {}
        for p in self.asserts:
            k = p.sim.get("status", "not_run")
            sim[k] = sim.get(k, 0) + 1
        return {"asserts": len(self.asserts), "proven_ratio": _ratio(out["PROVEN"], len(self.asserts)),
                "sim": dict(sorted(sim.items())), **out,
                "eliminated": sum(1 for p in self.asserts if p.eliminated)}

    def result_dict(self) -> dict:
        return {"props": [p.to_dict(full=False) for p in self.props], "coverage": self.coverage,
                "gaps": self.gaps}

    def to_dict(self) -> dict:
        n6 = self.n6
        return {"stage": "N7", "hashes": n6.n5.n4.n3.n2.n1.n0.hashes, "n6_input_hash": n6.input_hash,
                "n6_hash": n6.n6_hash, "input_hash": self.input_hash, "n7_hash": self.n7_hash,
                "counts": self.counts(), "coverage": self.coverage, "gaps": self.gaps,
                "layers": self.layers, "rounds": self.rounds, "props": [p.to_dict() for p in self.props],
                "warnings": self.warnings, "capabilities": engines.CAPS, "db": self.db}


def _ratio(a: int, b: int) -> float | None:
    return round(a / b, 4) if b else None


# --------------------------------------------------------------------------- #
# One pass
# --------------------------------------------------------------------------- #

def _units(n6: N6Result, n2, norm: dict, params: dict) -> list[N7Prop]:
    by_name = {p.name: p for p in n6.props}
    out: list[N7Prop] = []
    for p in n6.props:
        if p.kind != "assert" or not p.to_n7:
            continue
        u = N7Prop(p.name, "assert", p.rendered.prop, p.v_variant, cand=p.rendered.cand, n6=p)
        if not p.rendered.skip:
            u.prop = Prop("assert", p.rendered.prop.check, p.rendered.prop.inst, p.rendered.prop.module,
                          p.rendered.prop.expr, guard=p.rendered.prop.guard, next=p.rendered.prop.next,
                          clock=p.rendered.prop.clock, resets=dict(p.rendered.prop.resets),
                          message=p.rendered.prop.message)
            u.prop.id = p.name
        out.append(u)
        cv = by_name.get(p.name + "_c")
        if cv is not None and cv.kind == "cover" and cv.to_n7:
            cu = N7Prop(cv.name, "cover", cv.rendered.prop, cv.v_variant, cand=cv.rendered.cand, n6=cv)
            cu.prop = Prop("cover", cu.prop.check, cu.prop.inst, cu.prop.module, cu.prop.expr,
                           clock=cu.prop.clock, resets=dict(cu.prop.resets), message=cu.prop.message)
            cu.prop.id = cv.name
            out.append(cu)
    for t in reach.targets(n2, norm, params):
        out.append(N7Prop(t.name, "cover", t.prop, t.variant, is_reach=True))
    return out


def _once(n6: N6Result, *, params: dict, workspace: Path, backend: str, write: bool,
          log: IssueLog, round_no: int = 0) -> N7Result:
    n5, n4, n3, n2, n1, n0 = n6.n5, n6.n5.n4, n6.n5.n4.n3, n6.n5.n4.n3.n2, n6.n5.n4.n3.n2.n1, n6.n5.n4.n3.n2.n1.n0
    norm = n0.normalized
    root, design = n0.root, norm["design"]
    files = [root / f for f in design.get("files", [])]
    incdirs = [root / x for x in design.get("include_dirs", [])]
    defines = {k: (str(v) if v is not None else None) for k, v in design.get("defines", {}).items()}
    d, widths, toggle_nodes = _design(n2, norm, log)
    by_path = {i.path: i for i in n1.elaboration.instances}
    val = [c for c in n3.cases if c.set == "val"]

    units = _units(n6, n2, norm, params)

    # Steps 1-2: S_val offline; overfit eliminated
    sims = offline.check([u.cand for u in units if u.cand is not None], val, lambda s: widths.get(s, {}))
    for u in units:
        if u.cand is not None:
            u.sim = sims.get(u.cand.key, {"status": "no_data"})
    for u in units:
        if u.kind != "assert" or not u.sim:
            continue
        if u.sim.get("status") == "fail":
            if offline.overfit(u.cand, u.sim):
                u.eliminated = "trace_overfit"
                log.warn("N7_TRACE_OVERFIT", f"{u.name}：在合法 S_val 轨迹上被违反且来自挖掘（路径 B），"
                                              "判为过拟合训练轨迹，不证明")
            else:
                log.warn("N7_SIM_REFUTED", f"{u.name}：在合法 S_val 轨迹上被违反（{u.sim.get('first')}），"
                                            "作为反例报告，不做形式证明")

    # Step 5: COI, importance, order
    preds = coi.graph(n1.elaboration.instances)
    all_blocks = [b for b in coi.blocks(n1.elaboration.instances) if b.inst in set(n2.vdg.modeled)]
    for u in units:
        if u.kind != "assert":
            continue
        c = coi.cone(preds, coi.prop_starts(u.prop.inst, u.prop))
        feats = tree_refs(u.prop.expr) | (tree_refs(u.prop.guard) if u.prop.guard else set())
        imp = max((n2.pagerank.get(f"{u.prop.inst}.{f}", 0.0) for f in feats), default=0.0) \
            if params["n7_use_pagerank"] else 0.0
        u.importance = imp
        u.coi = {"signals": len(c), "blocks": sum(1 for b in all_blocks if b.node in c)}
    rank = {u.name: (bool(u.cand and u.cand.review_required), u.coi.get("signals", 0), -u.importance, u.name)
            for u in units if u.kind == "assert"}

    # Steps 3-5: proof layers (skip what is already decided by S_val)
    to_prove = [u for u in units if not u.eliminated and not (u.kind == "assert" and u.sim.get("status") == "fail")]
    spec_common = dict(files=files, include_dirs=incdirs, defines=defines, top=design["top"], root=root,
                       instances=n1.elaboration.instances, top_resets=norm.get("resets", []),
                       assumes=norm.get("assumes", []), top_clock=(norm.get("clocks") or [None])[0])
    work = workspace / "n7" / tagged_hash("n7_work", {"n6": n6.input_hash, "round": round_no})[:16]
    wl: list[str] = []
    formal = prove.prove(to_prove, spec_common=spec_common, params=params, rank=rank,
                         backend=backend, workers=int(params["n7_jobs"]), workdir=work / "proof",
                         warnings=wl) if to_prove else {}
    for m in wl:
        log.warn("N7_ENGINE", m)
    for u in units:
        u.formal = formal.get(u.name, {"status": "NOT_RUN", "layer": None, "step": None})

    # Step 4: the cover leg, then the merged status
    covers = {u.name: u for u in units if u.kind == "cover" and not u.is_reach}
    for u in units:
        if u.kind != "assert":
            continue
        cv = covers.get(u.name + "_c")
        u.cover = (cv.formal.get("status") if cv else "NOT_RUN") or "NOT_RUN"
        if u.eliminated:
            u.status = "REVIEW_REQUIRED"
        else:
            u.status = merge(sim=u.sim.get("status", "not_run"), formal=u.formal.get("status", "NOT_RUN"),
                             cover=u.cover, review_required=bool(u.cand and u.cand.review_required))

    # Step 7-8: coverage and gaps
    proven = {u.name for u in units if u.kind == "assert" and u.status == "PROVEN"}
    cones = {u.name: coi.cone(preds, coi.prop_starts(u.prop.inst, u.prop)) for u in units if u.kind == "assert"}
    branches = n3cover.branch_targets(n1.elaboration.instances, set(n2.vdg.modeled), n2.vdg.resets)
    val_cov = n3cover.compute(val, branches, n2.fsms, widths, toggle_nodes)
    reach_units = [u for u in units if u.is_reach]
    n7cov = n7coverage.compute(
        None, units, proven=proven, cones=cones, blocks=all_blocks, n3_cov=val_cov,
        covers={u.name: u.cover for u in units if u.kind == "assert"},
        reach=[{"name": u.name, "message": u.prop.message, "status": u.formal.get("status"),
                "step": u.formal.get("step")} for u in reach_units], params=params)
    ml: list[str] = []
    prior = n7cov["proof_core"]
    n7cov["proof_core"] = mutation.run(units, spec_common=spec_common | {
        "n1": n1, "modeled": set(n2.vdg.modeled), "parameters": dict(design.get("parameters", {})),
        "scan": n2b_scan(files, design["top"], incdirs, defines, root)},
        n3=n3, d=d, by_path=by_path, params=params, workdir=work / "mutation", warnings=ml)
    if n7cov["proof_core"].get("ratio") is None:
        n7cov["proof_core"]["note"] = prior["note"]
    for m in ml:
        log.warn("N7_MUTATION", m)
    gap = n7coverage.gaps(
        all_blocks, proven, cones, val_cov.gaps(), [
            {"name": u.name, "message": u.prop.message, "status": u.formal.get("status")} for u in reach_units])

    asserts = [u for u in units if u.kind == "assert"]
    layers = {k: sum(1 for u in asserts if u.formal.get("layer") == k) for k in ("L0", "L1", "L2")}
    layers["covers"] = sum(1 for u in units if u.kind == "cover")
    layers["resource_limit"] = sum(1 for u in units if u.formal.get("status") == "RESOURCE_LIMIT")
    rows = [r for r in n5.props if r.diff.get("status") in ("consistent", "mismatch")]
    agree = sum(1 for r in rows if r.diff.get("status") == "consistent")
    layers["agreement"] = {"basis": "交付版（Verilator）× 验证版（Yosys），N5 差分测试",
                           "compared": len(rows), "agree": agree,
                           "ratio": round(agree / len(rows), 4) if rows else None,
                           "note": "两个开源实现的同一批属性一致率；商用引擎仍未在本机验证"}
    res = N7Result(n6, units, n7cov, gap, layers, [], [w.to_dict() for w in log.warnings], "", "")
    res.n7_hash = tagged_hash("n7_result", res.result_dict())
    return res


def _gap_signals(gaps: dict) -> dict[str, set[str]]:
    return {b["inst"]: {s["signal"] for s in b["signals"]} for b in gaps.get("blocks", [])}


def run_n7(
    config_path: Path,
    workspace: Path | None = None,
    n6_result: N6Result | None = None,
    env: dict | None = None,
    tools: dict | None = None,
    write: bool = True,
    loop_rounds: int | None = None,
    backend: str | None = None,
    mutants: int | None = None,
) -> N7Result:
    if n6_result is None:
        n6_result = run_n6(config_path, workspace=workspace, env=env, tools=tools, write=write)
    n6 = n6_result
    n0 = n6.n5.n4.n3.n2.n1.n0
    workspace = Path(workspace) if workspace else n0.workspace
    norm = n0.normalized
    params = DEFAULTS | {k: v for k, v in norm.get("run", {}).get("params", {}).items() if k in DEFAULTS}
    if loop_rounds is not None:
        params["n7_loop_rounds"] = int(loop_rounds)
    if backend:
        params["n7_backend"] = backend
    if mutants is not None:
        params["n7_mutants"] = int(mutants)
    backend = str(params["n7_backend"])
    if backend not in ("thread", "process", "slurm"):
        backend = "thread"
    log = IssueLog()

    ih = tagged_hash("n7_input", {"n6_input_hash": n6.input_hash, "n6_hash": n6.n6_hash, "params": params,
                                  "n7_version": N7_VERSION})
    res = _once(n6, params=params, workspace=workspace, backend=backend, write=write, log=log)

    # Step 9: the closed loop
    rounds = []
    rounds_max = max(0, int(params["n7_loop_rounds"]))
    target, eps = float(params["n7_loop_target"]), float(params["n7_loop_epsilon"])
    prev = res.coverage["coi"]["ratio"]
    stable = 0
    while rounds_max and len(rounds) < rounds_max:
        ratio = res.coverage["coi"]["ratio"] or 0.0
        if ratio >= target:
            rounds.append({"round": len(rounds), "stop": f"COI 覆盖 {ratio} 已达阈值 {target}"})
            break
        if prev is not None and ratio - prev < eps:
            stable += 1
            if stable >= 2:
                rounds.append({"round": len(rounds), "stop": f"连续两轮提升低于 {eps}"})
                break
        prev = ratio
        gaps = _gap_signals(res.gaps)
        if not gaps:
            rounds.append({"round": len(rounds), "stop": "没有剩余缺口"})
            break
        log.warn("N7_LOOP", f"第 {len(rounds) + 1} 轮：按 {sum(len(v) for v in gaps.values())} 个缺口信号"
                             f"（{len(gaps)} 个实例）定向重新生成候选")
        n4 = run_n4(config_path, workspace=workspace, n3_result=n6.n5.n4.n3, env=env, tools=tools,
                    write=write, extra_gaps=gaps)
        n5r = run_n5(config_path, workspace=workspace, n4_result=n4, env=env, tools=tools, write=write)
        n6 = run_n6(config_path, workspace=workspace, n5_result=n5r, env=env, tools=tools, write=write)
        res = _once(n6, params=params, workspace=workspace, backend=backend, write=write, log=log,
                    round_no=len(rounds) + 1)
        rounds.append({"round": len(rounds) + 1, "coi": res.coverage["coi"]["ratio"],
                       "proven": res.counts()["PROVEN"], "candidates": len(n4.pool),
                       "gap_signals": sum(len(v) for v in gaps.values())})
    res.rounds = rounds
    res.warnings = [w.to_dict() for w in log.warnings]
    res.input_hash = ih
    res.n7_hash = tagged_hash("n7_result", res.result_dict())

    if write:
        from verimine.n7.report import markdown
        from verimine.n7.store import write_run

        res.db = write_run(workspace / "verimine.db", res)
        run_dir = workspace / "runs" / n0.hashes["run_hash"]
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "n7.json").write_text(json.dumps(res.to_dict(), indent=2, ensure_ascii=False,
                                                    sort_keys=True, default=str) + "\n", encoding="utf-8")
        (run_dir / "n7_report.md").write_text(markdown(res), encoding="utf-8")
    return res
