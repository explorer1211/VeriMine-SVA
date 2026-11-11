"""N3a steps 4 and 6, and the env_missing view of N2b's counterexamples.

Over-strength (step 4): N2b's covers (FSM states, FIFO full/empty, branches) are checked once
without any assumption and once per assumption. An assumption that turns a reached cover into an
unreached one restricts the design's behavior:
  key cover lost (a state machine state, FIFO full / empty)  -> rejected ("过强")
  only branch covers lost                                    -> warning, needs confirmation
The config's own assumes go through the same check (they are the user's: flagged, not removed),
and so does the set that would be in force together.

Assume-guarantee (step 6): an assumption on a submodule's inputs is turned into an assertion on
that instance and proved in the whole design under the top-level assumes: PROVEN -> DISCHARGED
(the parent already guarantees it), otherwise OPEN.

env_missing: N2b assertions that FAILED are replayed on their counterexample; if the trace
violates a candidate (an input breaking a protocol template), the failure is attributed to a
missing environment assumption, the N8 env_missing rule.
"""
from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from verimine.n2b import engine
from verimine.n2b.props import Prop
from verimine.n3a.cands import Cand, check

KEY_COVERS = {"fsm.unreachable_state", "fifo.reachable"}
STRENGTH_COVERS = KEY_COVERS | {"dead.branch"}


def _cover_id(p: Prop) -> tuple:
    return (p.check, p.inst, p.key)


def strength(cands: list[Cand], combined: list[Cand], covers: list[Prop], ctx: dict, workdir: Path,
             depth: int, timeout: int) -> dict:
    """Fills c.strength for every candidate; returns the baseline and combined results."""
    def run(name: str, assumes: list[dict]) -> dict[tuple, str]:
        ps = copy.deepcopy(covers)
        for p in ps:
            p.status, p.verification, p.skip = "", "", ""
        res = engine.run(ps, assumes=assumes, workdir=workdir / name, depth=depth, timeout=timeout,
                         prove=False, **ctx)
        out = {_cover_id(p): p.status for p in ps}
        if res.errors:
            out["__error__"] = "；".join(res.errors)[:300]
        return out

    jobs = {"baseline": []}
    for i, c in enumerate(cands):
        jobs[f"a{i}"] = [{"name": c.name, "expr": c.text}]
    if combined:
        jobs["combined"] = [{"name": c.name, "expr": c.text} for c in combined]
    with ThreadPoolExecutor(max_workers=4) as pool:
        res = dict(zip(jobs, pool.map(lambda kv: run(*kv), jobs.items())))
    base = res["baseline"]
    by_id = {_cover_id(p): p for p in covers}
    reached = {k for k, s in base.items() if s == "REACHED"}

    def lost(r: dict) -> dict:
        if "__error__" in r:
            return {"error": r["__error__"], "lost_key": [], "lost_branch": []}
        gone = sorted(k for k in reached if r.get(k) != "REACHED")
        return {"error": None,
                "lost_key": [f"{by_id[k].inst}: {by_id[k].message}" for k in gone if k[0] in KEY_COVERS],
                "lost_branch": [f"{by_id[k].inst}: {by_id[k].message}" for k in gone if k[0] not in KEY_COVERS]}

    for i, c in enumerate(cands):
        c.strength = lost(res[f"a{i}"]) | {"checked": len(reached)}
    out = {"covers": len(covers), "baseline_reached": len(reached),
           "baseline_error": base.get("__error__")}
    if combined:
        out["combined"] = lost(res["combined"]) | {"assumes": [c.name for c in combined]}
    return out


def discharge(cands: list[Cand], cx, ctx: dict, assumes: list[dict], workdir: Path, depth: int, timeout: int) -> None:
    props = []
    for c in cands:
        inst = cx.by_path[c.scope]
        p = Prop("assert", "n3a.guarantee", c.scope, c.module, c.cons, guard=c.ante, next=c.next,
                 clock=cx.inst_clock(inst), resets=dict(cx.g.resets.get(c.scope, {})),
                 message=f"{c.scope} 的假设 {c.text} 由上层保证")
        p.id = f"N3AG_{len(props) + 1:04d}"
        props.append(p)
    if not props:
        return
    engine.run(props, assumes=assumes, workdir=workdir, depth=depth, timeout=timeout, prove=True, **ctx)
    for c, p in zip(cands, props):
        c.discharge = {"status": p.status, "step": p.step, "trace": p.trace, "skip": p.skip}
        if p.status == "PROVEN":
            c.status, c.reason = "DISCHARGED", "在上层实例化后作为断言被证明"
        else:
            c.status = "OPEN"
            c.reason = {"FAILED": f"上层没有约束它：第 {p.step} 拍出现反例",
                        "BOUNDED": f"{depth} 拍内成立，但未能完整证明"}.get(
                p.status, f"未能验证（{p.status}{'：' + p.skip if p.skip else ''}）")


def env_missing(n2b_res, cands: list[Cand], design, from_wave, widths_of) -> list[dict]:
    """For every FAILED N2b assertion: which candidates its counterexample violates."""
    out = []
    usable = [c for c in cands if c.status in ("PROPOSED", "OPEN")]
    for p in n2b_res.props:
        if p.kind != "assert" or p.status != "FAILED" or not p.trace or not Path(p.trace).exists():
            continue
        cases, _info = from_wave(Path(p.trace), design, kind="formal", legality="unknown", prefix=p.id,
                                 source=p.id, min_cycles=1, final_sample=True)
        bad = [c.id for c in usable if check(c, cases, widths_of(c.scope))["violations"]]
        out.append({"prop": p.id, "check": p.check, "inst": p.inst, "message": p.message, "step": p.step,
                    "class": "env_missing" if bad else "other", "violated": bad})
    return out
