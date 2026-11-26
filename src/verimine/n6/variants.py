"""N6 step 3: the re-renderings a property goes back to N5 for when it does not compile.

Each round is a more conservative rendering of the same IR; the meaning does not change, only
the form. Delivery (what slang compiles) and verification (what Yosys reads) are separate legs,
and each leg of a property moves on only if that leg failed.

  delivery
    0  N5: one checker module per instance, ports named after the signals
    1  isolated: the property alone in its own checker module, ports renamed `p_<signal>`, so a
       label that equals a signal name, or a port another property breaks, no longer matters
    2  portless: no ports at all; the checker is bound into the instance and reads the signals
       by upward hierarchical name `<module>.<signal>` (no port types or widths to get wrong)
  verification
    0  N5 (monitor with $past)
    1  expanded: `$past(e, k)` as k nested `$past`, `$onehot` / `$onehot0` as `$countones`
    2  explicit registers: no `$past` at all; every look-back is a history register
       `vm_h_<hash>_<i>` sized with `$bits`, clocked like the property

The label inside a verification text is the property name, as in N5; engines substitute their
own (n2b engine: `LABEL:` -> `vm_<n>:`), see label_of().
"""
from __future__ import annotations

import copy
import hashlib
import re

from verimine.n2b.props import Prop
from verimine.n2b.render import Unrenderable, _inactive, _port_name, delivery, pv_name, sv, uses_past
from verimine.n5.render import n5_verification

D_NAMES = {0: "N5 原样", 1: "独立检查器（端口改名）", 2: "无端口检查器（向上层次引用）"}
V_NAMES = {0: "N5 原样", 1: "展开（$past 嵌套、$onehot 改 $countones）", 2: "显式历史寄存器（不用 $past）"}
MAX_ROUND = 2

_LABEL = re.compile(r"(\w+):\s*(?:assert|assume|cover)\b")


def label_of(text: str) -> str:
    """The label of the (first) check in a verification text."""
    m = _LABEL.search(text)
    return m.group(1) if m else ""


def _ident(s: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"\W", "_", s)).strip("_")


# --------------------------------------------------------------------------- #
# Tree rewrites
# --------------------------------------------------------------------------- #

def expand(t: dict | None) -> dict | None:
    """$past(e, k) -> $past(...$past(e)); $onehot(x) -> $countones(x) == 1; $onehot0 -> <= 1."""
    if t is None:
        return None
    t = dict(t)
    if "args" in t:
        t["args"] = [expand(a) for a in t["args"]]
    if t.get("op") == "$past" and t.get("k", 1) > 1:
        inner = t["args"][0]
        for _ in range(t["k"]):
            inner = {"op": "$past", "k": 1, "args": [inner]}
        return inner
    if t.get("op") == "call" and t.get("name") in ("$onehot", "$onehot0"):
        cnt = {"op": "call", "name": "$countones", "args": t["args"]}
        return {"op": "==" if t["name"] == "$onehot" else "<=", "args": [cnt, {"const": 1, "text": "1"}]}
    return t


def _expanded(p: Prop) -> Prop:
    q = copy.copy(p)
    q.expr, q.guard = expand(p.expr), expand(p.guard)
    return q


# --------------------------------------------------------------------------- #
# Verification
# --------------------------------------------------------------------------- #

def verification(p: Prop, label: str, variant: int) -> str:
    if variant == 0:
        return n5_verification(p, label)
    if variant == 1:
        return n5_verification(_expanded(p), label)
    return _explicit(p, label)


def _explicit(p: Prop, label: str) -> str:
    """N5's verification text with every look-back as a history register. Same gating as
    n5_verification (past_reset only for |=>, the first tick checked)."""
    p = _expanded(p)
    past = p.next or uses_past(p.expr) or uses_past(p.guard)
    if past and not p.clock:
        raise Unrenderable("跨周期属性需要时钟")
    regs: list[tuple[str, str]] = []            # (name, value it samples)
    seed = hashlib.sha1(repr((p.expr, p.guard, p.next, sorted(p.resets.items()), p.clock)).encode()).hexdigest()[:8]

    def hist(text: str) -> str:
        for n, v in regs:
            if v == text:
                return n
        n = f"vm_h_{seed}_{len(regs)}"
        regs.append((n, text))
        return n

    def lower(t: dict | None) -> dict | None:
        if t is None:
            return None
        if t.get("op") == "$past":
            return {"ref": hist(sv(lower(t["args"][0]), yosys=True))}
        if "args" in t:
            return {**t, "args": [lower(a) for a in t["args"]]}
        return t

    e = sv(lower(p.expr), yosys=True)
    g = sv(lower(p.guard), yosys=True) if p.guard else None
    gate = list(_inactive(p.resets))
    if past:
        gate.append(pv_name(p.clock))
        ok = " && ".join(_inactive(p.resets))
        if ok and p.next:
            gate.append(hist(ok))
    if g:
        gg = hist(g) if p.next else g
        e = f"!({gg}) || ({e})" if p.kind == "assert" else f"({gg}) && ({e})"
    ev = f"{p.clock['edge']} {p.clock['name']}" if p.clock else "*"
    cond = f"if ({' && '.join(gate)}) " if gate else ""
    decl = [f"reg [$bits({v})-1:0] {n}; always @({ev}) {n} <= {v};" for n, v in regs]
    # declarations first (Yosys would declare a name used before its `reg` implicitly); the
    # label is found by label_of(), not by position
    return "\n".join([*decl, f"always @({ev}) {cond}{label}: {p.kind}({e});"])


def split_verification(text: str) -> tuple[list[str], str]:
    """(declaration lines, check line) of a verification text."""
    lines = text.split("\n")
    return lines[:-1], lines[-1]


# --------------------------------------------------------------------------- #
# Delivery
# --------------------------------------------------------------------------- #

def _refs(p: Prop) -> list[str]:
    from verimine.n2b.render import refs
    return sorted(refs(p))


def delivery_text(p: Prop, variant: int) -> str:
    """The property line as it appears inside its checker module."""
    if variant == 0:
        return delivery(p)
    if variant == 1:
        return delivery(p, name=lambda n: "p_" + _ident(n))
    return delivery(p, name=lambda n: f"{p.module}.{n}")


def assemble(items: list[tuple[str, Prop, int]], widths_of) -> tuple[str, str, dict, dict]:
    """Checker modules and bind statements for (name, prop, delivery variant) items.

    Returns (sva text, bind text, sva line -> [names], bind line -> [names]); lines are 1-based.
    Variant 0 items share a checker per instance (as N5 writes them); variants 1 and 2 get one
    checker each, named after the property."""
    head = ["// VeriMine N6：交付版断言与 cover（标准 SVA），已通过 slang 编译检查",
            "// 由 verimine n6 生成，请勿手工修改", ""]
    sva, bind = list(head), list(head)
    sva_map: dict[int, list[str]] = {}
    bind_map: dict[int, list[str]] = {}

    def module(mod: str, inst: str, group: list[tuple[str, Prop, int]], ports: list[tuple[str, str]]):
        names = [n for n, _, _ in group]
        start = len(sva) + 1
        sva.append(f"// {inst}")
        if ports:
            sva.append(f"module {mod} (")
            for i, (pn, sig) in enumerate(ports):
                w, signed = widths_of(inst).get(sig, (1, False))
                rng = f" [{w - 1}:0]" if w > 1 else ""
                sva.append(f"  input logic{' signed' if signed else ''}{rng} {pn}" + ("," if i < len(ports) - 1 else ""))
            sva.append(");")
        else:
            sva.append(f"module {mod};")
        for ln in range(start, len(sva) + 1):
            sva_map[ln] = names
        for n, p, v in group:
            sva.append(f"  // [{p.check}] {p.message}")
            sva_map[len(sva)] = names
            sva.append("  " + delivery_text(p, v))
            sva_map[len(sva)] = [n]
        sva.append("endmodule")
        sva_map[len(sva)] = names
        sva.append("")
        conns = ", ".join(f".{pn}({sig})" for pn, sig in ports)
        bind.append(f"bind {inst} {mod} u_{mod} ({conns});")
        bind_map[len(bind)] = names

    shared: dict[str, list] = {}
    own = []
    for it in items:
        (shared.setdefault(it[1].inst, []) if it[2] == 0 else own).append(it)
    for k, (inst, group) in enumerate(sorted(shared.items())):
        sigs = sorted(set().union(*(set(_refs(p)) for _, p, _ in group)))
        module(f"vm_n6_chk_{k}", inst, group, [(_port_name(s), s) for s in sigs])
    for n, p, v in sorted(own, key=lambda x: x[0]):
        ports = [("p_" + _ident(s), s) for s in _refs(p)] if v == 1 else []
        module(f"vm_n6_{_ident(n)}", p.inst, [(n, p, v)], ports)
    return "\n".join(sva) + "\n", "\n".join(bind) + "\n", sva_map, bind_map
