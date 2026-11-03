"""N2 steps 7 and 11: the signal role table and abstraction-level labels.

Every role carries a confidence and where it came from:
  structural  derived from the RTL structure (strong)
  trace       from simulation traces; filled in from N3 onward (strong)
  naming      only the signal name suggests it (weak: never above 低)

Abstraction level: a port of its own instance is "port", everything else (registers,
intermediate nets) is "internal". The v5 pilot found port-level assertions survive equivalent
refactoring, internal ones often break because the signal name disappears; N9 uses this label.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from verimine.n2.events import Event
from verimine.n2.patterns import HIGH, LOW, MEDIUM, Fsm, Pattern
from verimine.n2.vdg import Vdg

NAMING_HINTS = [
    ("state_register", re.compile(r"(^|_)(state|st|fsm|cs)(_|$)|state", re.I)),
    ("counter", re.compile(r"(cnt|count|counter|ctr)", re.I)),
    ("shift_register", re.compile(r"(shift|shreg|sreg)", re.I)),
    ("memory", re.compile(r"(mem|ram|rom|regfile|rf)(_|$)", re.I)),
]


@dataclass
class Role:
    role: str
    confidence: str
    evidence_kind: str          # structural | trace | naming
    evidence: list[str] = field(default_factory=list)


@dataclass
class SignalRoles:
    node: str
    inst: str
    name: str
    kind: str
    width: int
    level: str
    roles: list[Role] = field(default_factory=list)


def build(g: Vdg, patterns: list[Pattern], fsms: list[Fsm], events: list[Event]) -> list[SignalRoles]:
    table = {
        n.id: SignalRoles(n.id, n.inst, n.name, n.kind, n.width, n.level)
        for n in g.nodes.values()
    }

    def add(inst: str, name: str, role: Role) -> None:
        sr = table.get(f"{inst}.{name}")
        if sr is None:
            return
        same = [r for r in sr.roles if r.role == role.role]
        if role.evidence_kind == "naming" and same:
            return  # structure already says so; the name adds nothing
        if role.evidence_kind != "naming":
            sr.roles = [r for r in sr.roles if not (r.role == role.role and r.evidence_kind == "naming")]
            for r in same:
                if r.evidence_kind == role.evidence_kind:
                    rank = {HIGH: 0, MEDIUM: 1, LOW: 2}
                    if rank[role.confidence] < rank[r.confidence]:
                        r.confidence, r.evidence = role.confidence, role.evidence
                    else:
                        r.evidence = sorted(set(r.evidence) | set(role.evidence))
                    return
        sr.roles.append(role)

    for n in g.nodes.values():
        if n.clock:
            add(n.inst, n.name, Role("clock", HIGH, "structural", ["敏感列表边沿信号或配置中的时钟"]))
        if n.reset:
            add(n.inst, n.name, Role("reset", HIGH, "structural", ["配置中的复位或敏感列表中的异步复位"]))
    for f in fsms:
        add(f.inst, f.state, Role("state_register", f.confidence, "structural", f.evidence))
        if f.next_state:
            add(f.inst, f.next_state, Role("next_state", f.confidence, "structural", [f"{f.state} 的下一状态"]))
    for p in patterns:
        add(p.inst, p.signal, Role(p.kind, p.confidence, "structural", p.evidence))
    for e in events:
        for role, sig in e.signals.items():
            kind = "naming" if e.confidence == LOW else "structural"
            add(e.inst, sig, Role(role, e.confidence, kind, [f"{e.kind}: " + "；".join(e.evidence)]))

    # Naming only as weak evidence, and only where structure found nothing of that kind.
    for sr in table.values():
        have = {r.role for r in sr.roles}
        leaf = sr.name.rsplit(".", 1)[-1]
        for role, rx in NAMING_HINTS:
            if role not in have and rx.search(leaf):
                if role == "memory" and sr.kind != "memory":
                    continue
                sr.roles.append(Role(role, LOW, "naming", [f"名字 {leaf} 像{role}，但结构上不符合"]))

    for sr in table.values():
        sr.roles.sort(key=lambda r: ({HIGH: 0, MEDIUM: 1, LOW: 2}[r.confidence], r.role))
    return sorted(table.values(), key=lambda s: s.node)
