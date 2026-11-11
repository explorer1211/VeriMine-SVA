"""N3 step 2 (formal exploration): coverage gaps become reachability targets (covers), the model
checker finds a trace to each one it can reach, and each trace is a new test case.

Targets: branches the traces never took, state-machine states they never visited, and N2
transitions they never showed. The confirmed assumes are part of the model (step 9), together
with the reset in the first cycle.
A target whose negation is proved (UNREACHABLE) is not a gap at all; the gap list says so.

The engine is N2b's (Yosys + SymbiYosys); see verimine/n2b/engine.py.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from verimine.n2b import engine
from verimine.n2b.checks import Ctx
from verimine.n2b.props import AND, EQ, PAST, const, ref


@dataclass
class Target:
    kind: str           # branch | state | transition
    inst: str
    text: str
    prop: object        # n2b Prop


def assumes_usable(assumes: list[dict], clock: dict | None) -> list[str]:
    """Names of assumes the verification version cannot express (they would be dropped)."""
    warn: list[str] = []
    engine._assume_lines(assumes, clock, warn)
    return [w.split(" ", 2)[1] for w in warn]


def _fsm_clock(cx: Ctx, f):
    inst = cx.by_path[f.inst]
    view = cx.views[f.inst]
    procs = [a.proc for a in view.writes.get(f.state, []) if view.clocked(a)]
    return cx.proc_clock(inst, procs[0]) if procs else cx.inst_clock(inst)


def targets(cx: Ctx, gap_branches, gap_states, gap_transitions, limit: int) -> list[Target]:
    out: list[Target] = []
    for b in gap_branches:
        inst = cx.by_path[b.inst]
        p = cx.prop(inst, "cover", "n3.branch", b.guard, clock=cx.clock_for(inst, b.proc),
                    message=f"分支 {b.text}", location=b.location)
        if p is not None:
            out.append(Target("branch", b.inst, b.text, p))
    for s in gap_states:
        inst = cx.by_path[s["inst"]]
        f = next(f for f in cx.n2.fsms if f.inst == s["inst"] and f.state == s["state"])
        p = cx.prop(inst, "cover", "n3.state", EQ(ref(f.state), const(s["value"])), clock=_fsm_clock(cx, f),
                    message=f"{f.state} == {s['name']}")
        if p is not None:
            out.append(Target("state", s["inst"], f"{f.state} == {s['name']}", p))
    for t in gap_transitions:
        inst = cx.by_path[t["inst"]]
        f = next(f for f in cx.n2.fsms if f.inst == t["inst"] and f.state == t["state"])
        clock = _fsm_clock(cx, f)
        if clock is None:
            continue
        names = {x["value"]: x["name"] for x in f.states}
        text = f"{f.state}: {names.get(t['from'], t['from'])} → {names.get(t['to'], t['to'])}"
        p = cx.prop(inst, "cover", "n3.transition",
                    AND(EQ(PAST(ref(f.state)), const(t["from"])), EQ(ref(f.state), const(t["to"]))),
                    clock=clock, message=text)
        if p is not None:
            out.append(Target("transition", t["inst"], text, p))
    return out[:limit]


def run(ts: list[Target], *, n1, scan, files, include_dirs, defines, top, root, norm, top_clock,
        workdir: Path, depth: int, timeout: int, prove: bool) -> engine.EngineResult:
    props = [t.prop for t in ts]
    for i, p in enumerate(props, 1):
        p.id = f"N3X_{i:04d}"
    return engine.run(props, instances=n1.elaboration.instances, files=files, include_dirs=include_dirs,
                      defines=defines, top=top, root=root, scan=scan, top_resets=norm.get("resets", []),
                      assumes=norm.get("assumes", []), top_clock=top_clock, workdir=workdir, depth=depth,
                      timeout=timeout, prove=prove)
