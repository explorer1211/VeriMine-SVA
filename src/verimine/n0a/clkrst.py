"""N0a step 10: clock and reset candidates of the top module.

  - clock: an edge in an always_ff / always sensitivity list (anywhere in the hierarchy) on a
    signal that comes straight from a top input port
  - async reset: the edge signal also tested by the block's first `if`
  - sync reset: the first `if` of a single-clock block, on an input port named like a reset
  - polarity: `!x` / `~x` / negedge means active low, plain `x` active high

These are drafts; N1 confirms them against the elaborated structure.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from pyslang.ast import VisitAction

RESET_NAME_RE = re.compile(r"(rst|reset|clr|clear)", re.I)


@dataclass
class ClockCand:
    name: str
    edge: str
    blocks: int = 0


@dataclass
class ResetCand:
    name: str
    active: str  # low | high
    type: str  # async | sync
    blocks: int = 0


@dataclass
class ClkRst:
    clocks: list[ClockCand] = field(default_factory=list)
    resets: list[ResetCand] = field(default_factory=list)
    found_top: bool = False


def _edge(e) -> str | None:
    s = str(e)
    return "posedge" if "PosEdge" in s else "negedge" if "NegEdge" in s else None


def _events(timing) -> list[tuple[str, str | None]]:
    k = str(timing.kind)
    evs = list(timing.events) if k == "TimingControlKind.EventList" else [timing] if k == "TimingControlKind.SignalEvent" else []
    out = []
    for ev in evs:
        try:
            out.append((ev.expr.symbol.name, _edge(ev.edge)))
        except Exception:
            continue
    return out


def _first_if(stmt):
    """The first statement of a block, if it is an `if`."""
    for _ in range(8):
        k = str(stmt.kind)
        if k == "StatementKind.Block":
            stmt = stmt.body
        elif k == "StatementKind.List":
            if not len(stmt.list):
                return None
            stmt = stmt.list[0]
        elif k == "StatementKind.Conditional":
            return stmt
        else:
            return None
    return None


def _cond_signal(stmt) -> tuple[str, str] | None:
    """(signal, active) for `if (x)`, `if (!x)`, `if (~x)`, `if (x == 0)`."""
    try:
        e = stmt.conditions[0].expr
    except Exception:
        return None
    k = str(e.kind)
    try:
        if k == "ExpressionKind.NamedValue":
            return e.symbol.name, "high"
        if k == "ExpressionKind.UnaryOp" and str(e.op) in ("UnaryOperator.LogicalNot", "UnaryOperator.BitwiseNot"):
            return e.operand.symbol.name, "low"
        if k == "ExpressionKind.BinaryOp" and str(e.op) in ("BinaryOperator.Equality", "BinaryOperator.CaseEquality"):
            digits = str(e.right.constant).split("'")[-1].lstrip("sSbBdDhHoO")
            return e.left.symbol.name, "low" if digits and set(digits) <= {"0"} else "high"
    except Exception:
        return None
    return None


def _named(expr) -> str | None:
    """Signal name behind a port connection, looking through implicit conversions."""
    for _ in range(4):
        if expr is None:
            return None
        k = str(expr.kind)
        if k == "ExpressionKind.NamedValue":
            return expr.symbol.name
        if k == "ExpressionKind.Conversion":
            expr = expr.operand
            continue
        return None
    return None


def infer(compilation, top: str) -> ClkRst:
    """Looks at every instance under top. A signal counts only if it traces back, through plain
    port connections, to an input port of top; it is reported under top's port name."""
    res = ClkRst()
    if compilation is None:
        return res
    inst = next((i for i in compilation.getRoot().topInstances if i.name == top), None)
    if inst is None:
        return res
    res.found_top = True
    clocks: dict[str, ClockCand] = {}
    resets: dict[str, ResetCand] = {}

    def scan(body, to_top: dict[str, str]) -> None:
        def visit(s):
            k = str(s.kind)
            if k == "SymbolKind.Instance":
                child = {}
                for pc in s.portConnections:
                    name = _named(pc.expression)
                    if name in to_top and str(pc.port.direction) == "ArgumentDirection.In":
                        child[pc.port.name] = to_top[name]
                if child:
                    scan(s.body, child)
                return VisitAction.Skip
            if k == "SymbolKind.InstanceArray":
                return VisitAction.Skip
            if k != "SymbolKind.ProceduralBlock":
                return VisitAction.Advance
            if str(s.procedureKind) not in ("ProceduralBlockKind.AlwaysFF", "ProceduralBlockKind.Always"):
                return VisitAction.Skip
            stmt = s.body
            if str(stmt.kind) != "StatementKind.Timed":
                return VisitAction.Skip
            edges = [(to_top[n], e) for n, e in _events(stmt.timing) if e and n in to_top]
            if not edges:
                return VisitAction.Skip
            first = _first_if(stmt.stmt)
            cond = _cond_signal(first) if first is not None else None
            if cond and cond[0] in to_top:
                cond = (to_top[cond[0]], cond[1])
            elif cond:
                cond = None
            edge_names = {n for n, _ in edges}
            for n, e in edges:
                if cond and cond[0] == n and len(edges) > 1:
                    active = "low" if e == "negedge" else cond[1]
                    resets.setdefault(n, ResetCand(n, active, "async")).blocks += 1
                else:
                    clocks.setdefault(n, ClockCand(n, e)).blocks += 1
            if cond and cond[0] not in edge_names and len(edges) == 1 and RESET_NAME_RE.search(cond[0]):
                resets.setdefault(cond[0], ResetCand(cond[0], cond[1], "sync")).blocks += 1
            return VisitAction.Skip

        body.visit(visit)

    inputs = {p.name: p.name for p in inst.body.portList if str(p.direction) == "ArgumentDirection.In"}
    scan(inst.body, inputs)
    for n in list(clocks):
        if n in resets:  # a reset that is also a clock elsewhere: keep it as reset only
            del clocks[n]
    res.clocks = sorted(clocks.values(), key=lambda c: c.name)
    res.resets = sorted(resets.values(), key=lambda r: r.name)
    return res
