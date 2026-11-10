"""Rendering properties to SystemVerilog, twice (spec N5/N6: 交付版 and 验证版):

  delivery      standard concurrent SVA, one checker module per instance, attached with
                `bind <instance path>`. This is what the user takes to any tool.
  verification  immediate assertions inside the design modules, the subset the open-source
                Yosys front end reads (it rejects `assert property (@(posedge clk) ...)`):
                    always @(posedge clk) if (<gate>) LABEL: assert(<!guard || expr>);
                The gate leaves out the initial step and cycles in reset; `|=>` becomes
                `!$past(guard) || expr` gated by a past-valid register.

Both come from the same expression trees, so they check the same thing. Constants are
rendered as plain integers, never as parameter or enum names: the checker module does not see
the design's names.
"""
from __future__ import annotations

import re
from typing import Callable

from verimine.n2b.props import Prop

_SV_LIT = re.compile(r"^\d*'[sS]?[bBoOdDhH][0-9a-fA-FxXzZ?_]+$|^'[01xXzZ]$")
_SYS_FUNCS = {"$signed", "$unsigned", "$countones", "$onehot", "$onehot0", "$bits", "$clog2"}
_UNARY = {"!": "!", "~": "~", "u-": "-", "&r": "&", "|r": "|", "^r": "^", "~&r": "~&",
          "~|r": "~|", "~^r": "~^"}
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


class Unrenderable(Exception):
    pass


_WILD = re.compile(r"^(\d*)'[sS]?([bBoOhH])([0-9a-fA-FxXzZ?_]+)$")


def wildcard_mask(text: str) -> tuple[int, int, int] | None:
    """`4'b1?0z` -> (width, mask, value): mask has 1 where the bit must match."""
    m = _WILD.match(text)
    if not m:
        return None
    per = {"b": 1, "o": 3, "h": 4}[m.group(2).lower()]
    bits = ""
    for ch in m.group(3).replace("_", "").lower():
        bits += ch * per if ch in "xz?" else format(int(ch, 16), f"0{per}b")
    width = int(m.group(1)) if m.group(1) else len(bits)
    bits = bits[-width:].rjust(width, "0")
    mask = int("".join("0" if b in "xz?" else "1" for b in bits), 2)
    value = int("".join("0" if b in "xz?" else b for b in bits), 2)
    return width, mask, value


def _int(v: int) -> str:
    if v < 0:
        return f"-{_int(-v)}"
    if v < 2 ** 31:
        return str(v)
    return f"{v.bit_length()}'d{v}"


def sv(t: dict, name: Callable[[str], str] = lambda n: n, yosys: bool = False) -> str:
    r = lambda x: sv(x, name, yosys)  # noqa: E731
    if "ref" in t:
        n = t["ref"]
        if yosys and not _IDENT.match(n):
            raise Unrenderable(f"信号 {n} 在生成块内，验证版不支持层次引用")
        return name(n)
    if "const" in t:
        return _int(t["const"])
    o, args = t.get("op"), t.get("args", [])
    if o == "xz":
        txt = t.get("text", "")
        if not _SV_LIT.match(txt):
            raise Unrenderable(f"无法渲染 x/z 常量 {txt}")
        return txt
    if o == "opaque" or o is None:
        raise Unrenderable("表达式含无法还原的部分（函数调用、复杂选择等）")
    if o == "$past":
        return f"$past({r(args[0])})"
    if o in _UNARY:
        return f"{_UNARY[o]}({r(args[0])})"
    if o == "u+":
        return r(args[0])
    if o == "?:":
        return f"(({r(args[0])}) ? ({r(args[1])}) : ({r(args[2])}))"
    if o == "{}":
        return "{" + ", ".join(r(a) for a in args) + "}"
    if o == "{n{}}":
        inner = args[1]
        body = ", ".join(r(a) for a in inner["args"]) if inner.get("op") == "{}" else r(inner)
        return "{" + r(args[0]) + "{" + body + "}}"
    if o == "[]":
        return f"{r(args[0])}[{r(args[1])}]"
    if o == "[:]":
        return f"{r(args[0])}[{r(args[1])}:{r(args[2])}]"
    if o == "inside":
        return "(" + " || ".join(f"(({r(args[0])}) == ({r(x)}))" for x in args[1:]) + ")"
    if o == "call":
        fn = t.get("name", "")
        if fn not in _SYS_FUNCS:
            raise Unrenderable(f"调用 {fn or '函数'}")
        return f"{fn}(" + ", ".join(r(a) for a in args) + ")"
    if o == "->":
        return f"(!({r(args[0])}) || ({r(args[1])}))"
    if o == "<->":
        return f"(!({r(args[0])}) == !({r(args[1])}))"
    if o in ("==?", "!=?") and yosys:  # Yosys has no wildcard equality: compare under a mask
        w = wildcard_mask(args[1].get("text", "")) if args[1].get("op") == "xz" else None
        if w is None:
            raise Unrenderable("通配比较的右侧不是字面量")
        width, mask, value = w
        return f"(({r(args[0])} & {width}'d{mask}) {o[:2]} {width}'d{value})"
    if o in ("++", "--"):
        raise Unrenderable("自增/自减")
    if len(args) == 2:
        return f"({r(args[0])} {o} {r(args[1])})"
    raise Unrenderable(f"运算 {o}")


def refs(p: Prop) -> set[str]:
    from verimine.n1.assign import tree_refs
    out = tree_refs(p.expr)
    if p.guard:
        tree_refs(p.guard, out)
    if p.clock:
        out.add(p.clock["name"])
    out.update(p.resets)
    return out


def uses_past(t: dict | None) -> bool:
    if not t:
        return False
    return t.get("op") == "$past" or any(uses_past(a) for a in t.get("args", []))


def _active(resets: dict[str, str], name=lambda n: n) -> str:
    return " || ".join(f"!{name(r)}" if lv == "low" else name(r) for r, lv in sorted(resets.items()))


def _inactive(resets: dict[str, str], name=lambda n: n) -> list[str]:
    return [name(r) if lv == "low" else f"!{name(r)}" for r, lv in sorted(resets.items())]


# --------------------------------------------------------------------------- #
# Delivery version
# --------------------------------------------------------------------------- #

def delivery(p: Prop, name=lambda n: n) -> str:
    e = sv(p.expr, name)
    g = sv(p.guard, name) if p.guard else None
    if p.clock:
        body = f"{g} {'|=>' if p.next else '|->'} {e}" if g else e
        dis = f" disable iff ({_active(p.resets, name)})" if p.resets else ""
        return f"{p.id}: {p.kind} property (@({p.clock['edge']} {name(p.clock['name'])}){dis} {body});"
    if p.next or uses_past(p.expr):
        raise Unrenderable("跨周期属性需要时钟")
    if g:
        e = f"!({g}) || ({e})" if p.kind == "assert" else f"({g}) && ({e})"
    ok = " && ".join(_inactive(p.resets, name)) or "1'b1"
    return f"always_comb if ({ok}) {p.id}: {p.kind} ({e});"


def _port_name(n: str) -> str:
    return n if _IDENT.match(n) else "vm_" + re.sub(r"\W", "_", n)


def checker_module(mod_name: str, inst: str, props: list[Prop], widths: dict[str, tuple[int, bool]]) -> tuple[str, str]:
    """(checker module text, bind statement) for one instance. widths: signal -> (width, signed)."""
    names = sorted(set().union(*(refs(p) for p in props)))
    ports = []
    for n in names:
        w, signed = widths.get(n, (1, False))
        rng = f" [{w - 1}:0]" if w > 1 else ""
        ports.append(f"  input logic{' signed' if signed else ''}{rng} {_port_name(n)}")
    body = "\n".join(f"  // [{p.check}] {p.message}\n  {p.delivery}" for p in props)
    text = f"// {inst}\nmodule {mod_name} (\n" + ",\n".join(ports) + "\n);\n" + body + "\nendmodule\n"
    conns = ", ".join(f".{_port_name(n)}({n})" for n in names)
    return text, f"bind {inst} {mod_name} u_{mod_name} ({conns});"


# --------------------------------------------------------------------------- #
# Verification version (Yosys)
# --------------------------------------------------------------------------- #

def pv_name(clock: dict) -> str:
    return f"vm_n2b_pv_{re.sub(r'\W', '_', clock['name'])}"


def verification(p: Prop, label: str) -> str:
    """One `always` block. Raises Unrenderable if Yosys cannot take it."""
    e = sv(p.expr, yosys=True)
    g = sv(p.guard, yosys=True) if p.guard else None
    past = p.next or uses_past(p.expr) or uses_past(p.guard)
    gate = ["!$initstate"] + _inactive(p.resets)
    if past:
        if not p.clock:
            raise Unrenderable("跨周期属性需要时钟")
        gate.append(pv_name(p.clock))
        ok = " && ".join(_inactive(p.resets))
        if ok:
            gate.append(f"$past({ok})")
    if g:
        gg = f"$past({g})" if p.next else g
        e = f"!({gg}) || ({e})" if p.kind == "assert" else f"({gg}) && ({e})"
    ev = f"{p.clock['edge']} {p.clock['name']}" if p.clock else "*"
    return f"always @({ev}) if ({' && '.join(gate)}) {label}: {p.kind}({e});"


def negated_cover(p: Prop, label: str) -> str:
    """The cover's target as an assertion that it never happens: proving it shows the cover
    is unreachable, not just unreached within the bound."""
    q = Prop(**{**p.__dict__, "kind": "assert", "expr": {"op": "!", "args": [
        p.expr if not p.guard else {"op": "&&", "args": [p.guard, p.expr]}]}, "guard": None})
    if p.next:
        raise Unrenderable("跨周期 cover 不做不可达证明")
    return verification(q, label)
