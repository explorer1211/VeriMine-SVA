"""N7 step 7: the four coverage figures, and step 8: the gap list.

  COI coverage       share of logic blocks (one assignment each) inside the COI of at least one
                     PROVEN assertion. Directly computable (coi.py). It says "touched by an
                     assertion", not "verified": the report says so (spec item 10).
  proof-core cover   whether Yosys can give a proof core is unknown for the open-source stack
                     (engine matrix: proof_core None). N7 does not pretend: the figure is
                     absent, with the reason, and mutation kill is reported beside it instead.
  vacuity            share of assertions with an antecedent whose cover was proved unreachable
                     (cover results), and how many were never reached.
  reachability       branches and FSM states reached: by the traces (N3) and, where N7 ran
                     reach.py, by the engine too.

Step 8: gaps are the logic blocks no PROVEN assertion touches, grouped per instance, together
with the N3 branch/state gaps that the formal reach covers did not explain away as unreachable.
The next N4 run takes this list as its gap input (spec item 9).
"""
from __future__ import annotations

from verimine.n7.coi import Block, block_coverage, cone


def _ratio(a: int, b: int) -> float | None:
    return round(a / b, 4) if b else None


def compute(props, units, *, proven: set[str], cones: dict[str, set[str]], blocks: list[Block],
            n3_cov, reach: list[dict], covers: dict[str, str], params: dict) -> dict:
    """props: N7Prop list. covers: assertion name -> its cover status. reach: the engine's
    branch/state covers with their status."""
    out: dict = {}
    cones_ok = [cones[n] for n in sorted(proven) if n in cones]
    out["coi"] = block_coverage(blocks, cones_ok) | {
        "note": "被已证明断言的影响锥触及的逻辑比例；只表示触及，不等于验证充分"}
    reach_taken = [r for r in reach if r["status"] == "REACHED"]
    reach_dead = [r for r in reach if r["status"] == "UNREACHABLE"]
    out["proof_core"] = {"ratio": None, "basis": "unavailable",
                         "note": "Yosys + SymbiYosys 能否给出证明核尚未确认（能力矩阵 proof_core: null）；"
                                 "以变异杀伤作为补充"}
    antar = [u for u in units if u.kind == "assert" and u.prop.guard is not None]
    vac = [u.name for u in antar if covers.get(u.name) == "UNREACHABLE"]
    out["vacuity"] = {
        "antecedents": len(antar), "unreachable": len(vac),
        "ratio": _ratio(len(vac), len(antar)), "names": sorted(vac),
        "note": "前件不可达的比例（cover 结果：证明为不可达的才算）"}
    s = n3_cov.summary()
    out["reachability"] = {
        "branch": {"traces": s["branch"], "engine": {"covers": len(reach),
                                                     "reached": len(reach_taken),
                                                     "unreachable": len(reach_dead)}},
        "state": {"traces": s["state"]},
        "toggle": {"traces": s["toggle"]},
        "note": "traces 列为 N3 轨迹覆盖；engine 列为 N7 交给形式引擎的分支/状态 cover",
    }
    return out


def gaps(blocks: list[Block], proven: set[str], cones: dict[str, set[str]], n3_gaps: dict,
         reach: list[dict]) -> dict:
    """Step 8: blocks no PROVEN assertion touches, and the trace gaps the engine did not explain."""
    union: set[str] = set()
    for n in proven:
        union |= cones.get(n, set())
    by_inst: dict[str, list[dict]] = {}
    for b in blocks:
        if b.node not in union:
            by_inst.setdefault(b.inst, []).append({"signal": b.signal, "location": b.location})
    dead = {r["message"] for r in reach if r["status"] == "UNREACHABLE"}
    live = [r["message"] for r in reach if r["status"] in ("REACHED", "UNREACHED")]
    branch_live = {m.removeprefix("分支 ") for m in live if m.startswith("分支 ")}
    state_live = {m for m in live if m.startswith("状态 ")}
    branches = [b for b in n3_gaps.get("branches", []) if b["branch"] not in branch_live]
    states = [s for s in n3_gaps.get("states", []) if f"状态 {s['state']} " not in " ".join(state_live)] if False else \
        [s for s in n3_gaps.get("states", []) if f"状态 {s['state']}" not in {m.split(" = ")[0] for m in state_live}]
    return {
        "blocks": [{"inst": k, "signals": v} for k, v in sorted(by_inst.items())],
        "block_count": sum(len(v) for v in by_inst.values()),
        "trace_gaps": {"branches": branches, "states": states,
                       "transitions": n3_gaps.get("transitions", [])},
        "explained_unreachable": sorted(dead),
    }
