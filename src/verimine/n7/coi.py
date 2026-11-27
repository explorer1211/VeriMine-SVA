"""N7 step 5 and 7: cone of influence (COI) and the logic blocks it is counted over.

The graph is over nodes `<instance path>.<signal>`. Edges are N1's assignments (right-hand side
and the conditions around it -> each left-hand side), plus the parent/child connections
(`in`: the parent's expression drives the child's input port; `out`: the child's output port
drives the parent's signal). A property's COI is the backward closure from the signals it reads.

A logic block is one assignment in a modeled instance (one left-hand side signal). COI coverage
is the share of blocks inside the COI of at least one PROVEN assertion. It says which logic an
assertion *touches*; it does not say the proof depended on it — the report says so, and the
mutation-kill figure is the supplement (spec N7 item 10).

The graph is also what orders the proof: a property's COI size is its cost, so the cheapest and
most important go to the deeper layers first (N7 item 5).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from verimine.n1.assign import cond_refs, tree_refs


def node(inst: str, signal: str) -> str:
    return f"{inst}.{signal}"


def graph(instances) -> dict[str, set[str]]:
    """node -> the nodes that feed it (empty for an instance's input port with no parent)."""
    preds: dict[str, set[str]] = {}

    def add(dst: str, src: str) -> None:
        preds.setdefault(dst, set()).add(src)

    for i in instances:
        for a in i.assigns:
            refs = set(a.rhs_refs) | set(cond_refs(a.cond_stack))
            for lhs in a.lhs:
                for r in refs:
                    add(node(i.path, lhs), node(i.path, r))
    for i in instances:
        if i.parent is None:
            continue
        for c in i.connections:
            e = c.get("expr")
            if not e:
                continue
            refs = tree_refs(e)
            if c.get("direction") == "in":
                for r in refs:
                    add(node(i.path, c["port"]), node(i.parent, r))
            elif c.get("direction") == "out":
                for r in refs:
                    add(node(i.parent, r), node(i.path, c["port"]))
    return preds


def cone(preds: dict[str, set[str]], starts) -> set[str]:
    seen: set[str] = set()
    todo = [n for n in starts if n in preds or True]
    while todo:
        n = todo.pop()
        if n in seen:
            continue
        seen.add(n)
        todo.extend(preds.get(n, ()))
    return seen


@dataclass
class Block:
    inst: str
    signal: str
    location: str
    proc: int | None
    cond: bool          # written under a condition (a branch)

    @property
    def node(self) -> str:
        return node(self.inst, self.signal)

    def to_dict(self) -> dict:
        return {"inst": self.inst, "signal": self.signal, "location": self.location}


def blocks(instances) -> list[Block]:
    """One block per (instance, left-hand side signal); the first assignment writes it."""
    out, seen = [], set()
    for i in instances:
        for a in i.assigns:
            for lhs in a.lhs:
                k = node(i.path, lhs)
                if k in seen:
                    continue
                seen.add(k)
                out.append(Block(i.path, lhs, a.location, a.proc, bool(a.cond_stack)))
    return sorted(out, key=lambda b: (b.inst, b.signal))


def prop_starts(inst: str, prop) -> list[str]:
    """Nodes an N2b/N5 property reads (its own instance's names)."""
    refs = tree_refs(prop.expr)
    if prop.guard:
        tree_refs(prop.guard, refs)
    return [node(inst, r) for r in sorted(refs)]


def block_coverage(bs: list[Block], cones: list[set[str]]) -> dict:
    """COI coverage over blocks given the COIs of the proven assertions."""
    union: set[str] = set()
    for c in cones:
        union |= c
    gap = [b for b in bs if b.node not in union]
    return {"blocks": len(bs), "touched": len(bs) - len(gap),
            "ratio": round((len(bs) - len(gap)) / len(bs), 4) if bs else None,
            "gaps": [b.to_dict() for b in gap]}
