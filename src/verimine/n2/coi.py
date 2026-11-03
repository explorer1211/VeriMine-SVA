"""N2 step 9: cone of influence (COI) and COI fingerprint for every assertion target.

Targets are every output port and every register of a modeled instance: the places assertions
get written. The COI of a target is everything it transitively reads in the VDG (data and
control edges, across clock cycles).

COI fingerprint = tagged hash of the cone's nodes (with kind and width), the edges inside it,
and the logic driving every node in it (assignment trees with their condition stacks, port
connection expressions). It changes when something that can influence the target changes and
stays put otherwise, which is what incremental runs (N10) need: an assertion whose COI
fingerprint is unchanged does not have to be re-proved.

N1's module fingerprints are not used here: they include every child's fingerprint, so a change
anywhere below a parent would leak into cones that never touch it.
"""
from __future__ import annotations

from dataclasses import dataclass

from verimine.hashing import tagged_hash
from verimine.n2.vdg import Vdg


@dataclass
class Coi:
    target: str
    inst: str
    nodes: list[str]
    inputs: list[str]          # top-level input ports in the cone
    sequential_depth: int      # most clocked edges on any shortest path to the target
    fingerprint: str


def targets(g: Vdg) -> list[str]:
    return sorted(n.id for n in g.nodes.values() if n.kind in ("output", "reg", "memory") and not n.clock)


def compute(g: Vdg) -> list[Coi]:
    preds = g.preds()
    out = []
    for t in targets(g):
        depth = {t: 0}
        order = [t]
        i = 0
        while i < len(order):
            cur = order[i]
            i += 1
            for e in preds[cur]:
                if e.src not in depth:
                    depth[e.src] = depth[cur] + e.delay
                    order.append(e.src)
        nodes = sorted(depth)
        inside = set(nodes)
        edges = sorted([e.src, e.dst, e.kind, e.delay] for e in g.edges if e.src in inside and e.dst in inside)
        fp = tagged_hash("coi", {
            "target": t,
            "nodes": [[x, g.nodes[x].kind, g.nodes[x].width] for x in nodes],
            "edges": edges,
            "logic": {x: g.logic.get(x, []) for x in nodes},
        })
        inputs = sorted(x for x in nodes if g.nodes[x].top_port and g.nodes[x].kind == "input")
        out.append(Coi(t, g.nodes[t].inst, nodes, inputs, max(depth.values()), fp))
    return out
