"""Helpers over N1's JSON expression trees ({"ref"}, {"const"}, {"op", "args"})."""
from __future__ import annotations

import re
from typing import Iterable

from verimine.n1.assign import tree_refs

COMPARE_OPS = {"==", "!=", "===", "!==", "==?", "!=?", "<", "<=", ">", ">="}
BOOL_OPS = COMPARE_OPS | {"&&", "||", "!", "->", "<->", "&r", "|r", "^r", "~&r", "~|r", "~^r"}
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*(::[A-Za-z_][A-Za-z0-9_$]*)?$")

__all__ = ["tree_refs", "is_ref", "const_of", "leaves", "evaluate", "compares", "text", "one_bit",
           "cond_exprs", "COMPARE_OPS", "is_name"]


def is_ref(t: dict | None, name: str | None = None) -> bool:
    return bool(t) and "ref" in t and (name is None or t["ref"] == name)


def const_of(t: dict | None) -> int | None:
    return t.get("const") if t and "const" in t else None


def is_name(text: str) -> bool:
    return bool(_IDENT_RE.match(text or ""))


def leaves(t: dict) -> list[tuple[dict, list[dict]]]:
    """Values a `?:` tree can produce, each with the (condition, polarity) list that selects it."""
    if t.get("op") == "?:":
        c, a, b = t["args"]
        return ([(x, [{"expr": c, "polarity": True}] + cs) for x, cs in leaves(a)]
                + [(x, [{"expr": c, "polarity": False}] + cs) for x, cs in leaves(b)])
    return [(t, [])]


def evaluate(t: dict, env: dict[str, int]) -> int | None:
    """Evaluates a tree when every signal it reads is in env; None otherwise."""
    if "const" in t:
        return t["const"]
    if "ref" in t:
        return env.get(t["ref"])
    op, args = t.get("op"), t.get("args", [])
    vals = [evaluate(a, env) for a in args]
    if op in ("!", "~", "u-", "u+") and vals and vals[0] is not None:
        v = vals[0]
        return {"!": int(not v), "~": int(not v) if v in (0, 1) else None, "u-": -v, "u+": v}[op]
    if op in ("&&", "||"):
        if op == "&&" and 0 in vals:
            return 0
        if op == "||" and any(v not in (None, 0) for v in vals):
            return 1
        if None in vals:
            return None
        return int(all(vals)) if op == "&&" else int(any(vals))
    if None in vals or len(vals) != 2:
        return None
    a, b = vals
    try:
        return {
            "==": int(a == b), "===": int(a == b), "!=": int(a != b), "!==": int(a != b),
            "<": int(a < b), "<=": int(a <= b), ">": int(a > b), ">=": int(a >= b),
            "&": a & b, "|": a | b, "^": a ^ b, "+": a + b, "-": a - b,
        }.get(op)
    except TypeError:
        return None


def compares(t: dict) -> Iterable[tuple[str, str, int]]:
    """(signal, op, constant) for every `signal OP constant` inside the tree."""
    op, args = t.get("op"), t.get("args", [])
    if op in COMPARE_OPS and len(args) == 2:
        a, b = args
        if is_ref(a) and const_of(b) is not None:
            yield a["ref"], op, b["const"]
        elif is_ref(b) and const_of(a) is not None:
            yield b["ref"], op, a["const"]
    for x in args:
        yield from compares(x)


def text(t: dict) -> str:
    """Readable rendering, used for transition conditions in reports."""
    if "ref" in t:
        return t["ref"]
    if "const" in t:
        return t.get("text") or str(t["const"])
    op, args = t.get("op"), t.get("args", [])
    if op == "opaque":
        return "f(" + ", ".join(t.get("refs", [])) + ")"
    if op in ("!", "~", "u-", "u+"):
        inner = text(args[0])
        return f"{op.lstrip('u')}{inner}" if "ref" in args[0] or "const" in args[0] else f"{op.lstrip('u')}({inner})"
    if op == "?:":
        return f"({text(args[0])} ? {text(args[1])} : {text(args[2])})"
    if op == "{}":
        return "{" + ", ".join(text(a) for a in args) + "}"
    if op == "[]":
        return f"{text(args[0])}[{text(args[1])}]"
    if op == "[:]":
        return f"{text(args[0])}[{text(args[1])}:{text(args[2])}]"
    if op == "call":
        return f"{t.get('name', 'call')}(" + ", ".join(text(a) for a in args) + ")"
    if len(args) == 2:
        return f"({text(args[0])} {op} {text(args[1])})"
    return f"{op}(" + ", ".join(text(a) for a in args) + ")"


def one_bit(t: dict, widths: dict[str, int]) -> bool:
    """True when the tree is certainly a 1-bit value (a flag, a comparison, a logic op)."""
    if "ref" in t:
        return widths.get(t["ref"]) == 1
    if "const" in t:
        return t["const"] in (0, 1)
    op = t.get("op")
    if op in BOOL_OPS:
        return True
    if op in ("&", "|", "^", "~") and t.get("args"):
        return all(one_bit(a, widths) for a in t["args"])
    if op == "[]":
        return True  # bit select of a vector (memories are excluded by the caller)
    return False


def cond_exprs(stack: list[dict]) -> list[dict]:
    """Every expression tree in a condition stack (if conditions, case subjects and items)."""
    out = []
    for c in stack:
        if c["kind"] == "if":
            out.append(c["expr"])
        elif c["kind"] == "case":
            out.append(c["subject"])
            out.extend(c.get("items", []))
    return out
