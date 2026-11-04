"""
Assignment extraction (N1 index table "赋值"): left-hand side, the signals the right-hand side
reads, the control-condition stack (enclosing if / case conditions), blocking or non-blocking,
and location.

Expressions are kept as small JSON trees so N2 can pattern-match them (counters, shift
registers, state transitions) without going back to slang:

    {"ref": "cnt"}                         signal, named relative to its instance
    {"const": 1, "text": "4'd1"}           anything that folds to a constant (literals,
                                           parameters, enum values, constant expressions)
    {"op": "+", "args": [...]}             operators, selects, concatenation, ?:, calls
    {"op": "opaque", "refs": [...]}        anything else; only the signals it reads are kept

Every name is relative to the instance body, e.g. "g_lane[1].tmp" for a signal inside a
generate block, so two generate copies never collide.
"""

from __future__ import annotations

import dataclasses
import re
from typing import Any

from pyslang import ast

MAX_DEPTH = 12
_TEXT_MAX = 60

_BIN = {
    "Add": "+", "Subtract": "-", "Multiply": "*", "Divide": "/", "Mod": "%", "Power": "**",
    "BinaryAnd": "&", "BinaryOr": "|", "BinaryXor": "^", "BinaryXnor": "~^",
    "Equality": "==", "Inequality": "!=", "CaseEquality": "===", "CaseInequality": "!==",
    "WildcardEquality": "==?", "WildcardInequality": "!=?",
    "GreaterThan": ">", "GreaterThanEqual": ">=", "LessThan": "<", "LessThanEqual": "<=",
    "LogicalAnd": "&&", "LogicalOr": "||", "LogicalImplication": "->", "LogicalEquivalence": "<->",
    "LogicalShiftLeft": "<<", "LogicalShiftRight": ">>",
    "ArithmeticShiftLeft": "<<<", "ArithmeticShiftRight": ">>>",
}
_UN = {
    "Plus": "u+", "Minus": "u-", "BitwiseNot": "~", "LogicalNot": "!",
    "BitwiseAnd": "&r", "BitwiseOr": "|r", "BitwiseXor": "^r",
    "BitwiseNand": "~&r", "BitwiseNor": "~|r", "BitwiseXnor": "~^r",
    "Preincrement": "++", "Postincrement": "++", "Predecrement": "--", "Postdecrement": "--",
}
_SIGNAL_KINDS = {"SymbolKind.Variable", "SymbolKind.Net", "SymbolKind.Port"}
_COMMENT_RE = re.compile(r"//[^\n]*|/\*.*?\*/", re.S)
_LIT_RE = re.compile(r"^(?:\d+)?'s?([bodh])([0-9a-fA-F_]+)$")
_BASES = {"b": 2, "o": 8, "d": 10, "h": 16}


@dataclasses.dataclass
class AssignInfo:
    proc: int | None            # index into InstanceInfo.procs; None for a continuous assign
    lhs: list[str]              # base signals written
    lhs_tree: dict
    rhs_tree: dict
    rhs_refs: list[str]         # signals the right-hand side reads (sorted)
    cond_stack: list[dict]      # outermost first
    blocking: bool
    location: str


def const_int(cv) -> int | None:
    """ConstantValue -> int, or None when unset, unknown (x/z) or not an integer."""
    try:
        if not cv or cv.hasUnknown():
            return None
    except Exception:
        return None
    s = str(cv).replace("_", "")
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    m = _LIT_RE.match(s)
    if m:
        return int(m.group(2), _BASES[m.group(1).lower()])
    return None


def _text(node) -> str:
    try:
        # Implicit conversions have no syntax of their own; use what they convert.
        for _ in range(4):
            if node.syntax is None and str(node.kind) == "ExpressionKind.Conversion":
                node = node.operand
        syn = node.syntax
        if syn is None:
            return ""
        # Syntax text carries trivia; drop comments so a comment edit never changes the index.
        t = " ".join(_COMMENT_RE.sub(" ", str(syn)).split())
    except Exception:
        return ""
    return t if len(t) <= _TEXT_MAX else t[: _TEXT_MAX - 3] + "..."


def tree_refs(tree: dict, out: set[str] | None = None) -> set[str]:
    out = set() if out is None else out
    if "ref" in tree:
        out.add(tree["ref"])
    out.update(tree.get("refs", ()))
    for a in tree.get("args", ()):
        tree_refs(a, out)
    return out


class Extractor:
    """Builds expression trees and assignment records for one instance."""

    def __init__(self, inst_path: str, body, location):
        self.prefix = inst_path + "."
        self.location = location
        try:
            self.ctx = ast.EvalContext(ast.ASTContext(body, ast.LookupLocation.max))
        except Exception:
            self.ctx = None

    # ---- names -----------------------------------------------------------
    def rel(self, sym) -> str | None:
        try:
            path = sym.hierarchicalPath
        except Exception:
            return None
        if path.startswith(self.prefix):
            return path[len(self.prefix):]
        return None

    def _signal(self, expr) -> str | None:
        try:
            sym = expr.symbol
        except Exception:
            return None
        if str(sym.kind) not in _SIGNAL_KINDS:
            return None
        return self.rel(sym)

    # ---- expressions -----------------------------------------------------
    def _eval(self, expr) -> int | None:
        if self.ctx is None:
            return None
        try:
            return const_int(expr.eval(self.ctx))
        except Exception:
            return None

    def _flat_refs(self, expr) -> list[str]:
        refs: set[str] = set()

        def visit(x):
            k = str(x.kind)
            if k in ("ExpressionKind.NamedValue", "ExpressionKind.HierarchicalValue"):
                n = self._signal(x)
                if n:
                    refs.add(n)
            return True

        try:
            expr.visit(visit)
        except Exception:
            pass
        return sorted(refs)

    def tree(self, expr, depth: int = 0) -> dict:
        if expr is None:
            return {"op": "opaque", "refs": []}
        k = str(expr.kind).removeprefix("ExpressionKind.")
        if k in ("NamedValue", "HierarchicalValue"):
            n = self._signal(expr)
            if n:
                return {"ref": n}
        refs = self._flat_refs(expr)
        if not refs and k not in ("Call", "Assignment"):
            v = self._eval(expr)
            if v is not None:
                return {"const": v, "text": _text(expr) or str(v)}
        if depth >= MAX_DEPTH:
            return {"op": "opaque", "refs": refs}
        sub = lambda e: self.tree(e, depth + 1)  # noqa: E731
        try:
            if k == "Assignment":  # output port connections bind as `parent_lvalue = port`
                return sub(expr.left)
            if k == "Conversion":
                return sub(expr.operand)
            if k == "UnaryOp":
                op = str(expr.op).split(".")[-1]
                return {"op": _UN.get(op, op), "args": [sub(expr.operand)]}
            if k == "BinaryOp":
                op = str(expr.op).split(".")[-1]
                return {"op": _BIN.get(op, op), "args": [sub(expr.left), sub(expr.right)]}
            if k == "ConditionalOp":
                return {"op": "?:", "args": [sub(expr.conditions[0].expr), sub(expr.left), sub(expr.right)]}
            if k == "Concatenation":
                return {"op": "{}", "args": [sub(x) for x in expr.operands]}
            if k == "Replication":
                return {"op": "{n{}}", "args": [sub(expr.count), sub(expr.concat)]}
            if k == "ElementSelect":
                return {"op": "[]", "args": [sub(expr.value), sub(expr.selector)]}
            if k == "RangeSelect":
                return {"op": "[:]", "args": [sub(expr.value), sub(expr.left), sub(expr.right)]}
            if k == "Inside":
                return {"op": "inside", "args": [sub(expr.left)] + [sub(x) for x in expr.rangeList]}
            if k == "Call":
                return {"op": "call", "name": str(expr.subroutineName), "args": [sub(a) for a in expr.arguments]}
        except Exception:
            pass
        return {"op": "opaque", "refs": refs}

    # ---- statements ------------------------------------------------------
    def _lhs_bases(self, tree: dict) -> list[str]:
        """Signals written by an lvalue: the base of selects, every part of a concatenation."""
        if "ref" in tree:
            return [tree["ref"]]
        op = tree.get("op")
        if op in ("[]", "[:]"):
            return self._lhs_bases(tree["args"][0])
        if op == "{}":
            return sorted({b for a in tree["args"] for b in self._lhs_bases(a)})
        return sorted(tree_refs(tree))

    def _assignment(self, expr, proc, stack, out):
        left = self.tree(expr.left)
        right = self.tree(expr.right)
        # Index expressions on the left (mem[addr] <= d) are reads too.
        idx_refs = tree_refs(left) - set(self._lhs_bases(left))
        out.append(AssignInfo(
            proc=proc,
            lhs=self._lhs_bases(left),
            lhs_tree=left,
            rhs_tree=right,
            rhs_refs=sorted(tree_refs(right) | idx_refs),
            cond_stack=[dict(c) for c in stack],
            blocking=not bool(getattr(expr, "isNonBlocking", False)),
            location=self.location(expr.sourceRange.start) if hasattr(expr, "sourceRange") else "",
        ))

    def statement(self, stmt, proc: int, stack: list[dict], out: list[AssignInfo]) -> None:
        if stmt is None:
            return
        k = str(stmt.kind).removeprefix("StatementKind.")
        if k == "List":
            for s in stmt.list:
                self.statement(s, proc, stack, out)
        elif k == "Block":
            self.statement(stmt.body, proc, stack, out)
        elif k == "Timed":
            self.statement(stmt.stmt, proc, stack, out)
        elif k == "Conditional":
            conds = [self.tree(c.expr) for c in stmt.conditions]
            cond = conds[0] if len(conds) == 1 else {"op": "&&", "args": conds}
            entry = {"kind": "if", "expr": cond, "text": " && ".join(_text(c.expr) for c in stmt.conditions)}
            self.statement(stmt.ifTrue, proc, stack + [{**entry, "polarity": True}], out)
            if stmt.ifFalse is not None:
                self.statement(stmt.ifFalse, proc, stack + [{**entry, "polarity": False}], out)
        elif k == "Case":
            subject = self.tree(stmt.expr)
            items = []
            for it in stmt.items:
                items.append([self.tree(e) for e in it.expressions])
            base = {"kind": "case", "subject": subject, "text": _text(stmt.expr)}
            for i, it in enumerate(stmt.items):
                self.statement(it.stmt, proc, stack + [{**base, "items": items[i],
                                                         "item_text": ", ".join(_text(e) for e in it.expressions),
                                                         "default": False}], out)
            if stmt.defaultCase is not None:
                others = [x for xs in items for x in xs]
                self.statement(stmt.defaultCase, proc, stack + [{**base, "items": [], "others": others,
                                                                  "item_text": "default", "default": True}], out)
        elif k in ("ForLoop", "RepeatLoop", "WhileLoop", "DoWhileLoop", "ForeverLoop", "ForeachLoop"):
            self.statement(stmt.body, proc, stack + [{"kind": "loop", "text": k}], out)
        elif k == "ExpressionStatement":
            e = stmt.expr
            if str(e.kind) == "ExpressionKind.Assignment":
                self._assignment(e, proc, stack, out)
        elif k == "ProceduralAssign":
            try:
                self._assignment(stmt.assignment, proc, stack, out)
            except Exception:
                pass

    def continuous(self, sym) -> list[AssignInfo]:
        out: list[AssignInfo] = []
        try:
            a = sym.assignment
        except Exception:
            return out
        if str(a.kind) == "ExpressionKind.Assignment":
            self._assignment(a, None, [], out)
        return out


def cond_refs(stack: list[dict]) -> list[str]:
    refs: set[str] = set()
    for c in stack:
        if c["kind"] == "if":
            tree_refs(c["expr"], refs)
        elif c["kind"] == "case":
            tree_refs(c["subject"], refs)
            for x in c.get("items", []):
                tree_refs(x, refs)
    return sorted(refs)


def to_dict(a: AssignInfo) -> dict[str, Any]:
    return dataclasses.asdict(a)
