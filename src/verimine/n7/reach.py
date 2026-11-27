"""N7 step 7 (reachability): branch and FSM-state covers handed to the formal engine.

N3 measures how much of the branch/state space the traces reach. N7 asks the same questions of
the engine: each observable branch's guard and each FSM state becomes a cover, so a branch that
no trace takes can still be proved reachable or unreachable. The list is capped
(`n7_reach_max`) and needs a single clock; otherwise only the trace figures are reported.
"""
from __future__ import annotations

from verimine.n3.cover import branch_targets
from verimine.n2b.props import EQ, Prop, const, ref


class ReachTarget:
    """Duck-typed like the proof units: .name, .kind, .prop, .variant."""

    def __init__(self, name: str, prop: Prop):
        self.name, self.kind, self.prop, self.variant = name, "cover", prop, 0
        self.reach = True


def targets(n2, norm, params: dict) -> list[ReachTarget]:
    clocks = norm.get("clocks", [])
    if len(clocks) != 1 or not params.get("n7_reach", True):
        return []
    clock = {"name": clocks[0]["name"], "edge": clocks[0].get("edge", "posedge")}
    by_path = {i.path: i for i in n2.n1.elaboration.instances}
    out: list[ReachTarget] = []
    cap = int(params.get("n7_reach_max", 64))
    for k, b in enumerate(branch_targets(n2.n1.elaboration.instances, set(n2.vdg.modeled), n2.vdg.resets)):
        inst = by_path.get(b.inst)
        if inst is None:
            continue
        p = Prop("cover", "n7.reach.branch", b.inst, inst.module, b.guard, clock=clock,
                 resets=dict(n2.vdg.resets.get(b.inst, {})), message=f"分支 {b.text}")
        p.id = f"n7_reach_b{k}"
        out.append(ReachTarget(p.id, p))
    for fsm in n2.fsms:
        inst = by_path.get(fsm.inst)
        if inst is None:
            continue
        for st in fsm.states:
            p = Prop("cover", "n7.reach.state", fsm.inst, inst.module,
                     EQ(ref(fsm.state), const(st["value"])), clock=clock,
                     resets=dict(n2.vdg.resets.get(fsm.inst, {})),
                     message=f"状态 {fsm.state} = {st['name']}")
            p.id = f"n7_reach_s{len(out)}"
            out.append(ReachTarget(p.id, p))
    return out[:cap]
