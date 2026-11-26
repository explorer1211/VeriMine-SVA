"""N5 steps 1-3, 5-6: rendering N4's IR.

  delivery      standard SVA: one checker module per instance, `bind <instance path>` (what the
                user takes to any tool)
  verification  what the open-source engine reads (Yosys): immediate assertions in `always`
                blocks, i.e. a monitor; constructs Yosys lacks are degraded by the capability
                matrix (verimine/engines.py), and each degradation is recorded
  covers        every assertion with an antecedent also gets a cover of its antecedent, so N7
                can tell a vacuous proof from a real one

Clock, `disable iff` (the instance's resets) and names are generated in one place (step 2):
names are `<template>_<consequent signals>`, made unique; the cover of an assertion is
`<name>_c`. Both versions come from the same trees (N2b's renderer), so they check the same
thing; the differential test (diff.py) is there to catch where they do not.

v5 scope: the IR is the bounded-delay implication fragment (invariant, next-cycle implication,
$past data transfer); every property says so (`fragment`) with its window.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from verimine import engines
from verimine.n1.assign import tree_refs
from verimine.n2b.props import Prop
from verimine.n2b.render import Unrenderable, checker_module, delivery, verification

VERIFY_ENGINE = "yosys-sby"        # the open-source engine the verification version is for
CYCLE = "vm_n5_tb.vm_n5_cyc"    # simulation action blocks print the bench's cycle counter, not $time


def n5_verification(p: Prop, label: str) -> str:
    """The verification version as N5 renders it. Two differences from N2b's, both to keep SVA's
    meaning (found by the differential test): `disable iff` spans the attempt, so only `|=>`
    also needs the previous cycle out of reset; and the first clock tick is checked."""
    return verification(p, label, past_reset=p.next, initstate=False)


@dataclass
class Rendered:
    cand: object                    # N4 Cand
    name: str
    kind: str                       # assert | cover
    prop: Prop
    delivery: str = ""
    verification: str = ""
    sim: str = ""                   # delivery text with an action block, for the differential test
    degraded: list[str] = field(default_factory=list)   # rules applied for the verification engine
    skip: str = ""                  # why the verification version is missing
    engines: dict = field(default_factory=dict)         # engine -> {"degraded", "unsupported"}
    fragment: str = ""
    window: int = 0
    diff: dict = field(default_factory=dict)


def _ident(s: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"\W", "_", s)).strip("_")


def past_depth(t: dict | None) -> int:
    if not t:
        return 0
    k = t.get("k", 1) if t.get("op") == "$past" else 0
    return k + max([past_depth(a) for a in t.get("args", [])] or [0])


def names(cands) -> dict[str, str]:
    """candidate key -> unique assertion name."""
    out, used = {}, set()
    for c in cands:
        sig = sorted(tree_refs(c.cons))[:3]
        base = _ident(f"{c.type}_{'_'.join(sig)}")[:48] or "prop"
        name, k = base, 2
        while name in used:
            name, k = f"{base}_{k}", k + 1
        used.add(name)
        out[c.key] = name
    return out


def props_of(cands, engine_names: list[str] = tuple(engines.CAPS)) -> list[Rendered]:
    from verimine.n4.ir import to_prop

    out = []
    nm = names(cands)
    for c in cands:
        name = nm[c.key]
        a = to_prop(c, name)
        a.check = f"n5.{c.type}"
        items = [(name, "assert", a)]
        if c.ante is not None:
            cv = Prop("cover", f"n5.{c.type}.ante", c.scope, c.module, c.ante, clock=c.clock, resets=dict(c.resets),
                      message=f"{name} 的前件可达", key=c.key + "|ante")
            cv.id = name + "_c"
            items.append((cv.id, "cover", cv))
        for n, kind, p in items:
            r = Rendered(c, n, kind, p)
            r.window = max(past_depth(p.expr), past_depth(p.guard)) + (1 if p.next else 0)
            r.fragment = "bounded_implication"   # every N4 IR form is in it (no sequences)
            used = engines.features(p.guard, p.expr, p.next, p.resets)
            for e in engine_names:
                deg, bad = engines.plan(e, used)
                r.engines[e] = {"degraded": deg, "unsupported": bad}
            try:
                r.delivery = delivery(p)
            except Unrenderable as e:
                r.skip = f"交付版：{e}"
            bad = r.engines.get(VERIFY_ENGINE, {}).get("unsupported") or engines.plan(VERIFY_ENGINE, used)[1]
            if bad:
                r.skip = r.skip or f"验证版：引擎 {VERIFY_ENGINE} 不支持 " + "、".join(engines.FEATURES[f] for f in bad)
            else:
                try:
                    r.verification = n5_verification(p, n)
                    r.degraded = engines.plan(VERIFY_ENGINE, used)[0]
                except Unrenderable as e:
                    r.skip = r.skip or f"验证版：{e}"
            if r.delivery and r.delivery.endswith(");"):
                act = (f' else $display("VM_FAIL {n} %0d", {CYCLE});' if kind == "assert"
                       else f' $display("VM_COVER {n} %0d", {CYCLE});')
                r.sim = r.delivery[:-1] + act
            out.append(r)
    return out


def widths_of(inst) -> dict[str, tuple[int, bool]]:
    return {x.name: (x.width or 1, x.signed) for x in [*inst.ports, *inst.signals]}


def delivery_files(rs: list[Rendered], by_path: dict, mems: set) -> tuple[str, str, dict[str, str]]:
    """(checker modules, bind statements, inst -> checker module name)."""
    by_inst: dict[str, list[Prop]] = {}
    for r in rs:
        if not r.delivery:
            continue
        if any((r.prop.inst, x) in mems for x in tree_refs(r.prop.expr) | (tree_refs(r.prop.guard) if r.prop.guard else set())):
            r.delivery, r.skip = "", r.skip or "交付版：引用了存储器（非压缩数组），不能作为检查器端口"
            continue
        r.prop.delivery = r.delivery
        by_inst.setdefault(r.prop.inst, []).append(r.prop)
    mods, binds, chk = [], [], {}
    for n, (inst, ps) in enumerate(sorted(by_inst.items())):
        text, bind = checker_module(f"vm_n5_chk_{n}", inst, ps, widths_of(by_path[inst]))
        mods.append(text)
        binds.append(bind)
        chk[inst] = f"vm_n5_chk_{n}"
    head = "// VeriMine N5：交付版断言与 cover（标准 SVA）\n// 由 verimine n5 生成，请勿手工修改\n\n"
    return head + "\n".join(mods), head + "\n".join(binds) + "\n", chk


def verification_files(rs: list[Rendered]) -> dict[str, str]:
    """module -> text of the verification version, to be included before `endmodule` (one copy
    per module, as the engine inserts it), with the past-valid registers it reads."""
    from verimine.n2b.render import pv_name, uses_past

    out: dict[str, set[str]] = {}
    clocks: dict[str, dict[str, dict]] = {}
    for r in rs:
        if r.verification:
            out.setdefault(r.prop.module, set()).add("  " + r.verification)
            p = r.prop
            if p.clock and (p.next or uses_past(p.expr) or uses_past(p.guard)):
                clocks.setdefault(p.module, {})[p.clock["name"]] = p.clock
    texts = {}
    for m, v in sorted(out.items()):
        pv = [f"  reg {pv_name(c)} = 1'b0;\n  always @({c['edge']} {c['name']}) {pv_name(c)} <= 1'b1;"
              for _, c in sorted(clocks.get(m, {}).items())]
        texts[m] = ("// VeriMine N5 验证版（Yosys 即时断言），插在模块末尾，读入时加 -formal\n"
                    + "\n".join(pv + sorted(v)) + "\n")
    return texts
