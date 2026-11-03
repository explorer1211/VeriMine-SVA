"""N2 step 6: interface events — valid/ready, req/ack, push/pop, full/empty.

Names only propose a pairing (weak evidence). Structure decides the confidence:
  高  the two signals are used in the same expression or condition (valid && ready,
      push && !full), i.e. the design actually gates one with the other
  中  ports of opposite direction (one input, one output), the usual handshake shape
  低  naming only
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from verimine.n1.elaborate import InstanceInfo
from verimine.n2 import exprs as X
from verimine.n2.patterns import HIGH, LOW, MEDIUM

# role -> suffix pattern. The prefix (what comes before) must match for a pair.
ROLE_RE = {
    "valid": r"valid|vld",
    "ready": r"ready|rdy",
    "req": r"req|request",
    "ack": r"ack|gnt|grant",
    "push": r"push|wr_en|wren|write_en|wr_req",
    "pop": r"pop|rd_en|rden|read_en|rd_req",
    "full": r"full|wr_full|afull",
    "empty": r"empty|rd_empty|aempty",
}
PAIRS = [
    ("valid_ready", "valid", "ready"),
    ("req_ack", "req", "ack"),
    ("push_full", "push", "full"),
    ("pop_empty", "pop", "empty"),
    ("push_pop", "push", "pop"),
    ("full_empty", "full", "empty"),
]
_COMPILED = {r: re.compile(rf"^(?P<prefix>.*?)(?:_)?(?:{p})(?:_[io]|_n)?$", re.I) for r, p in ROLE_RE.items()}


@dataclass
class Event:
    kind: str
    inst: str
    signals: dict[str, str]          # role -> signal name
    confidence: str
    evidence: list[str] = field(default_factory=list)


def _role_of(name: str) -> list[tuple[str, str]]:
    leaf = name.rsplit(".", 1)[-1]
    out = []
    for role, rx in _COMPILED.items():
        m = rx.match(leaf)
        if m:
            out.append((role, m.group("prefix").rstrip("_").lower()))
    return out


def find_events(inst: InstanceInfo, child_connections: list[dict] = ()) -> list[Event]:
    """child_connections: expression trees the children's ports are connected to."""
    dirs = {p.name: p.direction for p in inst.ports}
    names = sorted(set(dirs) | {s.name for s in inst.signals if not s.array_dims})
    by_role: dict[str, dict[str, list[str]]] = {}
    for n in names:
        for role, prefix in _role_of(n):
            by_role.setdefault(role, {}).setdefault(prefix, []).append(n)

    groups: list[set[str]] = []
    for a in inst.assigns:
        groups.append(X.tree_refs(a.rhs_tree))
        for e in X.cond_exprs(a.cond_stack):
            groups.append(X.tree_refs(e))
        # A signal assigned under a condition on another: `if (pop) ... else if (push && !full)`
        groups.append(set(X.tree_refs({"op": "x", "args": X.cond_exprs(a.cond_stack)})))
    # Expressions on child port connections live in this instance too: .we(push && !full)
    for e in child_connections:
        groups.append(X.tree_refs(e))

    def together(a: str, b: str) -> bool:
        return any(a in g and b in g for g in groups)

    out: list[Event] = []
    for kind, ra, rb in PAIRS:
        for prefix in sorted(set(by_role.get(ra, {})) & set(by_role.get(rb, {}))):
            for a in by_role[ra][prefix]:
                for b in by_role[rb][prefix]:
                    if a == b:
                        continue
                    ev = [f"命名：{a} / {b}"]
                    conf = LOW
                    da, db = dirs.get(a), dirs.get(b)
                    if da and db and {da, db} == {"in", "out"}:
                        conf = MEDIUM
                        ev.append(f"端口方向相反（{a}:{da}，{b}:{db}）")
                    if together(a, b):
                        conf = HIGH
                        ev.append("在同一表达式或条件中一起使用")
                    out.append(Event(kind, inst.path, {ra: a, rb: b}, conf, ev))
    return out
