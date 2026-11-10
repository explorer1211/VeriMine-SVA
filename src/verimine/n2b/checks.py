"""N2b step 1: one template per kind of problem. Each check produces static findings (decided
from the N1 index, the N2 model or slang's analysis) and/or properties (an assertion that must
hold, or a cover that must be reachable) for the engine to decide.

  class        check                        how
  dead_code    dead.const_cond              static: an if condition folds to a constant
               dead.case_unreachable        static: slang CaseUnreachable
               dead.branch                  cover: every branch that assigns something
  fsm          fsm.unreachable_state        static (N2 transition graph) + cover(state == S)
               fsm.deadlock                 static (no transition out) + cover(left S)
               fsm.illegal_encoding         assert: state is one of the known encodings
               fsm.dangling                 static: transition into an encoding nobody handles
  array_arith  array.const_oob              static: slang IndexOOB / RangeOOB
               array.index_oob              assert: variable index stays inside the array
               array.overflow               assert: a + b / a - b fits the target (no wrap)
               array.width_trunc            static: slang WidthTruncate / PortWidthTruncate
  x_drive      x.assign                     static: x assigned
               x.multi_driver               static: slang + overlapping drivers in the index
               x.bus_conflict               assert: at most one tri-state driver enabled
               x.floating                   static: read or output but never driven, unconnected input
  control      control.case_incomplete      static: slang CaseIncomplete / CaseEnum;
                                            assert for unique/priority case: subject is listed
               control.case_parallel        static: slang CaseDup / CaseOverlap;
                                            assert for unique/unique0 wildcard case: one item matches
               control.set_reset            assert: two async controls of one block never both active
  timing       timing.comb_loop             static: cycle of zero-delay edges in the VDG
               timing.latch                 static: slang InferredLatch, always_latch
  structure    counter.overflow/underflow   assert (N2 counters): next value fits, no wrap
               fifo.overflow/underflow      assert (N2 FIFO): push when full / pop when empty is ignored
               fifo.bound                   assert: occupancy never exceeds the full threshold
               fifo.reachable               cover: full and empty are reachable
  register     reg.*                        see regmap.py

Assertions run with unconstrained inputs except for the reset sequence and the user's assumes
(N3a adds more), so a failure can still be a missing environment constraint: N8 decides.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from verimine.n1.assign import tree_refs
from verimine.n1.elaborate import InstanceInfo
from verimine.n2 import exprs as X
from verimine.n2.patterns import InstView, _sum_terms
from verimine.n2 import vdg as vdg_mod
from verimine.n2.vdg import is_clocked
from verimine.n2b.props import (AND, EQ, ERROR, INFO, NOT, OR, PAST, WARNING, Finding, Prop, const,
                                klass_of, op, ref)
from verimine.n2b.scan import ScanResult

_RANGE_RE = re.compile(r"\[\s*(-?\d+)\s*:\s*(-?\d+)\s*\]")
_COMB_KINDS = {"always_comb", "always", "assign", "always_latch"}


def _key(*xs) -> str:
    return json.dumps(xs, sort_keys=True, ensure_ascii=False)


@dataclass
class Ctx:
    n2: object                      # N2Result
    scan: ScanResult
    params: dict

    def __post_init__(self):
        n1 = self.n2.n1
        norm = n1.n0.normalized
        self.instances = n1.elaboration.instances
        self.by_path = {i.path: i for i in self.instances}
        # Static checks read the N1 index of every instance, BLOCKED ones included: a design a
        # tool cannot read is exactly where they matter. So they use a VDG over all instances;
        # properties go only to instances N2 modeled (the engine could not read the others).
        self.g = vdg_mod.build(self.instances, n1.elaboration.top_path, {}, norm.get("clocks", []),
                               norm.get("resets", []))
        self.modeled_paths = set(self.n2.vdg.modeled)
        child_exprs: dict[str, list[dict]] = {}
        for i in self.instances:
            if i.parent is not None:
                child_exprs.setdefault(i.parent, []).extend(c["expr"] for c in i.connections if c.get("expr"))
        self.child_exprs = child_exprs
        self.views = {i.path: InstView(i, self.g.resets.get(i.path, {}), child_exprs.get(i.path, []))
                      for i in self.instances}
        self.findings: list[Finding] = []
        self.props: list[Prop] = []

    # ---- helpers ---------------------------------------------------------
    def find(self, check: str, inst: InstanceInfo | None, message: str, severity: str = WARNING,
             signals=(), location: str = "", module: str = "", **detail) -> Finding:
        f = Finding(check, klass_of(check), inst.path if inst else "", inst.module if inst else module,
                    message, severity, "static", sorted(signals), location, detail=detail)
        self.findings.append(f)
        return f

    def prop(self, inst: InstanceInfo, kind: str, check: str, expr: dict, *, guard=None, next=False,
             clock=None, resets=None, message="", location="", key="", on_trigger=None) -> Prop | None:
        if inst.path not in self.modeled_paths:
            return None
        p = Prop(kind, check, inst.path, inst.module, expr, guard, next, clock,
                 dict(self.g.resets.get(inst.path, {}) if resets is None else resets),
                 message, location, key or _key(expr, guard, next), on_trigger or {})
        self.props.append(p)
        return p

    def widths(self, inst: InstanceInfo) -> dict[str, int]:
        return self.views[inst.path].widths if inst.path in self.views else {}

    def edge_of(self, inst: InstanceInfo, sig: str) -> str:
        for p in inst.procs:
            for ev in p.sensitivity:
                if ev.get("signal") == sig and ev.get("edge"):
                    return ev["edge"]
        return "posedge"

    def proc_clock(self, inst: InstanceInfo, proc: int | None) -> dict | None:
        """Clock of a clocked block: the edge event that is not a reset."""
        if proc is None or not is_clocked(inst, proc):
            return None
        rst = self.g.resets.get(inst.path, {})
        for ev in inst.procs[proc].sensitivity:
            if ev.get("edge") and ev["signal"] not in rst:
                return {"name": ev["signal"], "edge": ev["edge"]}
        return self.inst_clock(inst)

    def inst_clock(self, inst: InstanceInfo) -> dict | None:
        clks = sorted(self.g.clocks.get(inst.path, set()))
        if len(clks) != 1:
            return None
        return {"name": clks[0], "edge": self.edge_of(inst, clks[0])}

    def clock_for(self, inst: InstanceInfo, proc: int | None) -> dict | None:
        return self.proc_clock(inst, proc) or self.inst_clock(inst)


# --------------------------------------------------------------------------- #
# Condition stacks -> guard trees
# --------------------------------------------------------------------------- #

def _case_match(c: dict, item: dict) -> dict:
    o = "==?" if c.get("condition") in ("casez", "casex") and item.get("op") == "xz" else "=="
    return op(o, c["subject"], item)


def stack_guard(stack: list[dict], resets: dict[str, str]) -> tuple[dict | None, bool]:
    """(guard tree, in_reset). Conditions only on reset signals are dropped: assertions are
    disabled during reset anyway. in_reset = the stack selects the reset-active branch."""
    parts = []
    for c in stack:
        if c["kind"] == "if":
            refs = tree_refs(c["expr"])
            if refs and refs <= set(resets):
                env = {r: (0 if resets[r] == "low" else 1) for r in refs}
                v = X.evaluate(c["expr"], env)
                if v is not None and bool(v) == c["polarity"]:
                    return None, True
                continue
            parts.append(c["expr"] if c["polarity"] else NOT(c["expr"]))
        elif c["kind"] == "case":
            if c.get("default"):
                others = c.get("others") or []
                if others:
                    parts.append(AND(*[NOT(_case_match(c, x)) for x in others]))
            else:
                items = c.get("items") or []
                if not items:
                    parts.append({"op": "opaque", "refs": []})
                else:
                    parts.append(OR(*[_case_match(c, x) for x in items]))
        else:  # loop: the iteration is not expressible as a guard
            parts.append({"op": "opaque", "refs": []})
    return AND(*parts), False


def _const_dead(stack: list[dict]) -> dict | None:
    for c in stack:
        if c["kind"] == "if" and "const" in c["expr"] and bool(c["expr"]["const"]) != c["polarity"]:
            return c
    return None


def path_text(stack: list[dict], resets: dict[str, str] | None = None) -> str:
    parts = []
    for c in stack:
        if c["kind"] == "if" and resets and (refs := tree_refs(c["expr"])) and refs <= set(resets):
            continue  # reset inactive is implied: assertions are off during reset
        if c["kind"] == "if":
            t = c.get("text") or X.text(c["expr"])
            parts.append(t if c["polarity"] else f"!({t})")
        elif c["kind"] == "case":
            parts.append(f"{c.get('text') or X.text(c['subject'])} 为 {c.get('item_text', '')}")
        else:
            parts.append(c.get("text", "loop"))
    return " 且 ".join(parts) or "无条件"


# --------------------------------------------------------------------------- #
# Widths and ranges
# --------------------------------------------------------------------------- #

def width_of(t: dict, widths: dict[str, int]) -> int | None:
    if "ref" in t:
        return widths.get(t["ref"]) or None
    if "const" in t:
        return max(1, int(t["const"]).bit_length()) if t["const"] >= 0 else None
    o, a = t.get("op"), t.get("args", [])
    if o in X.BOOL_OPS:
        return 1
    if o == "[]":
        return 1
    if o == "[:]" and "const" in a[1] and "const" in a[2]:
        return abs(a[1]["const"] - a[2]["const"]) + 1
    if o == "{}":
        ws = [width_of(x, widths) for x in a]
        return None if None in ws else sum(ws)
    if o in ("&", "|", "^", "~^", "+", "-", "~"):
        ws = [width_of(x, widths) for x in a]
        return None if None in ws or not ws else max(ws)
    if o == "?:":
        ws = [width_of(x, widths) for x in a[1:]]
        return None if None in ws else max(ws)
    return None


def dims_range(dims: str) -> tuple[int, int] | None:
    m = _RANGE_RE.findall(dims)
    if len(m) != 1:
        return None
    a, b = int(m[0][0]), int(m[0][1])
    return min(a, b), max(a, b)


def packed_range(type_str: str) -> tuple[int, int] | None:
    if "$" in type_str:
        return None
    m = _RANGE_RE.findall(type_str)
    if len(m) != 1:
        return None
    a, b = int(m[0][0]), int(m[0][1])
    return min(a, b), max(a, b)


def _loc(inst: InstanceInfo, a) -> str:
    return getattr(a, "location", "") or ""


# --------------------------------------------------------------------------- #
# Static findings from slang's analysis
# --------------------------------------------------------------------------- #

def slang_findings(cx: Ctx) -> None:
    by_module: dict[str, list[InstanceInfo]] = {}
    for i in cx.instances:
        by_module.setdefault(i.module, []).append(i)
    sev = {"x.multi_driver": ERROR, "dead.case_unreachable": WARNING, "array.const_oob": ERROR,
           "timing.latch": WARNING, "array.width_trunc": INFO, "control.case_incomplete": WARNING,
           "control.case_parallel": WARNING, "x.floating": WARNING}
    for d in cx.scan.diags:
        insts = by_module.get(d["module"] or "", [])
        inst = insts[0] if insts else None
        f = cx.find(d["check"], inst, f"slang {d['code']}：{d['message']}", sev.get(d["check"], WARNING),
                    [d["signal"]] if d["signal"] else [], f"{d['file']}:{d['line']}",
                    module=d["module"] or "", slang_code=d["code"], instances=[i.path for i in insts])
        f.evidence = "static"


def _slang_has(cx: Ctx, check: str, module: str, signal: str) -> bool:
    base = signal.split("[")[0]
    return any(d["check"] == check and d["module"] == module and d["signal"].split("[")[0] == base
               for d in cx.scan.diags)


# --------------------------------------------------------------------------- #
# Dead code
# --------------------------------------------------------------------------- #

def dead_code(cx: Ctx) -> None:
    limit = int(cx.params.get("n2b_max_branch_covers", 200))
    for inst in cx.instances:
        rst = cx.g.resets.get(inst.path, {})
        seen: set[str] = set()
        n = 0
        for a in inst.assigns:
            if not a.cond_stack:
                continue
            if any(c["kind"] == "case" and c.get("default") for c in a.cond_stack):
                continue  # a defensive default is meant to be unreachable
            dead = _const_dead(a.cond_stack)
            k = _key([{kk: vv for kk, vv in c.items() if kk not in ("text", "item_text")} for c in a.cond_stack])
            if k in seen:
                continue
            seen.add(k)
            if dead:
                cx.find("dead.const_cond", inst, f"条件 {dead.get('text') or X.text(dead['expr'])} 恒为"
                        f"{'真' if dead['expr']['const'] else '假'}，分支 {path_text(a.cond_stack)} 永远不会执行",
                        WARNING, a.lhs, a.location)
                continue
            guard, in_reset = stack_guard(a.cond_stack, rst)
            if in_reset or guard is None or n >= limit:
                continue
            n += 1
            pt = path_text(a.cond_stack, rst)
            cx.prop(inst, "cover", "dead.branch", guard, clock=cx.clock_for(inst, a.proc),
                    message=f"分支可达：{pt}（赋值 {', '.join(a.lhs)}）",
                    location=a.location, key=k,
                    on_trigger={"message": f"分支 {pt} 不可达（赋值 {', '.join(a.lhs)}）",
                                "severity": WARNING, "signals": a.lhs})


# --------------------------------------------------------------------------- #
# State machines
# --------------------------------------------------------------------------- #

def fsm_checks(cx: Ctx) -> None:
    for f in cx.n2.fsms:
        inst = cx.by_path[f.inst]
        view = cx.views[f.inst]
        s = f.state
        name = {x["value"]: x["name"] for x in f.states}
        values = [x["value"] for x in f.states]
        procs = [a.proc for a in view.writes.get(s, []) if view.clocked(a)]
        clock = cx.proc_clock(inst, procs[0]) if procs else cx.inst_clock(inst)
        st = lambda v: f"{name.get(v, v)}（{v}）"  # noqa: E731

        # unreachable states
        unreachable = sorted(set(values) - set(f.reachable)) if f.reset_state is not None else []
        for v in values:
            mk = _key("fsm.unreachable_state", f.inst, s, v)
            if v in unreachable:
                cx.find("fsm.unreachable_state", inst, f"状态机 {s} 的状态 {st(v)} 从复位状态 "
                        f"{st(f.reset_state)} 不可达（按 N2 迁移图）", WARNING, [s], merge=mk)
            cx.prop(inst, "cover", "fsm.unreachable_state", EQ(ref(s), const(v)), clock=clock,
                    message=f"{s} 能到达状态 {st(v)}", key=mk,
                    on_trigger={"message": f"状态机 {s} 的状态 {st(v)} 不可达", "severity": WARNING,
                                "signals": [s], "merge": mk})

        # deadlock: no way out of a reachable state
        outs: dict[int, set[int]] = {v: set() for v in values}
        for t in f.transitions:
            if t["from"] in outs and t["to"] != t["from"]:
                outs[t["from"]].add(t["to"])
        if len(values) > 1:
            for v in values:
                mk = _key("fsm.deadlock", f.inst, s, v)
                if not outs[v] and v in f.reachable:
                    cx.find("fsm.deadlock", inst, f"状态机 {s} 的状态 {st(v)} 没有出口（只有自环），进入后无法离开",
                            WARNING, [s], merge=mk)
                if clock and v not in unreachable:
                    cx.prop(inst, "cover", "fsm.deadlock", AND(EQ(PAST(ref(s)), const(v)), op("!=", ref(s), const(v))),
                            clock=clock, message=f"{s} 能离开状态 {st(v)}", key=mk,
                            on_trigger={"message": f"状态机 {s} 进入状态 {st(v)} 后无法离开（死锁）",
                                        "severity": WARNING, "signals": [s], "merge": mk})

        # illegal encodings
        if f.width and 2 ** f.width > len(values):
            cx.prop(inst, "assert", "fsm.illegal_encoding", OR(*[EQ(ref(s), const(v)) for v in values]),
                    clock=clock, message=f"{s} 只取合法编码 {{{', '.join(str(v) for v in values)}}}",
                    on_trigger={"message": f"状态机 {s} 出现非法编码（不在 {{{', '.join(str(v) for v in values)}}} 中）",
                                "severity": ERROR, "signals": [s]})

        # dangling transitions: into an encoding no branch handles
        handled = view.compared_consts(s) | {t["from"] for t in f.transitions if not t.get("implicit")}
        if f.next_state:
            handled |= view.compared_consts(f.next_state)
        for v in sorted({t["to"] for t in f.transitions} - handled):
            cx.find("fsm.dangling", inst, f"状态机 {s} 会迁移到编码 {st(v)}，但没有任何分支处理这个编码", WARNING, [s])


# --------------------------------------------------------------------------- #
# Arrays and arithmetic
# --------------------------------------------------------------------------- #

def _selects(t: dict, out: list[dict]) -> list[dict]:
    if t.get("op") == "[]":
        out.append(t)
    for a in t.get("args", []):
        _selects(a, out)
    return out


def array_arith(cx: Ctx) -> None:
    counters = {(p.inst, p.signal) for p in cx.n2.patterns if p.kind in ("counter", "shift_register")}
    for inst in cx.instances:
        widths = cx.widths(inst)
        rst = cx.g.resets.get(inst.path, {})
        mems = {s.name: dims_range(s.array_dims) for s in inst.signals if s.array_dims}
        vecs = {p.name: packed_range(p.type_str) for p in inst.ports}
        vecs |= {s.name: packed_range(s.type_str) for s in inst.signals if not s.array_dims}
        sites: list[tuple] = []   # (select, stack, proc, location)
        for a in inst.assigns:
            for t in (a.lhs_tree, a.rhs_tree, *X.cond_exprs(a.cond_stack)):
                for sel in _selects(t, []):
                    sites.append((sel, a.cond_stack, a.proc, a.location))
        for e in cx.child_exprs.get(inst.path, []):
            for sel in _selects(e, []):
                sites.append((sel, [], None, ""))
        seen = set()
        for sel, stack, proc, loc in sites:
            base, idx = sel["args"]
            if not X.is_ref(base) or "const" in idx:
                continue
            b = base["ref"]
            rng = mems.get(b) if b in mems else vecs.get(b)
            if not rng:
                continue
            lo, hi = rng
            w = width_of(idx, widths)
            if w is not None and lo == 0 and 2 ** w - 1 <= hi:
                continue  # every value of the index is in range
            guard, in_reset = stack_guard(stack, rst)
            if in_reset:
                continue
            k = _key(b, idx, guard)
            if k in seen:
                continue
            seen.add(k)
            cond = op("<=", idx, const(hi)) if lo == 0 else AND(op(">=", idx, const(lo)), op("<=", idx, const(hi)))
            what = "数组" if b in mems else "向量"
            cx.prop(inst, "assert", "array.index_oob", cond, guard=guard, clock=cx.clock_for(inst, proc),
                    message=f"{b}[{X.text(idx)}] 下标在 [{lo}:{hi}] 内", location=loc, key=k,
                    on_trigger={"message": f"{what} {b} 的下标 {X.text(idx)} 可能越界（范围 [{lo}:{hi}]）",
                                "severity": ERROR, "signals": [b] + sorted(tree_refs(idx))})

        # arithmetic overflow on plain assignments (counters are checked as structures)
        for a in inst.assigns:
            if len(a.lhs) != 1 or not X.is_ref(a.lhs_tree) or (inst.path, a.lhs[0]) in counters:
                continue
            t = a.rhs_tree
            if t.get("op") not in ("+", "-") or len(t.get("args", [])) != 2:
                continue
            x, y = t["args"]
            if not (X.is_ref(x) and X.is_ref(y)):
                continue  # a +/- constant is modular arithmetic by intent (or a counter)
            W = widths.get(a.lhs[0])
            wx, wy = width_of(x, widths), width_of(y, widths)
            if not W or wx is None or wy is None or W > 31:
                continue
            if any(s.signed for s in inst.signals if s.name in tree_refs(t) | {a.lhs[0]}):
                continue
            if t["op"] == "+" and max(wx, wy) < W:
                continue  # the carry has room
            if t["op"] == "-" and "const" in y and y["const"] == 0:
                continue
            guard, in_reset = stack_guard(a.cond_stack, rst)
            if in_reset:
                continue
            if t["op"] == "+":
                cond, what = op("<=", t, const(2 ** W - 1)), "上溢"
            else:
                cond, what = op(">=", x, y), "下溢"
            cx.prop(inst, "assert", "array.overflow", cond, guard=guard, clock=cx.clock_for(inst, a.proc),
                    message=f"{a.lhs[0]} = {X.text(t)} 不{what}（{W} 位）", location=a.location,
                    on_trigger={"message": f"{a.lhs[0]} = {X.text(t)} 会{what}，结果被截断到 {W} 位",
                                "severity": WARNING, "signals": a.lhs})


# --------------------------------------------------------------------------- #
# X and drivers
# --------------------------------------------------------------------------- #

def _has_x(t: dict) -> bool:
    if t.get("op") == "xz" and t.get("x"):
        return True
    return any(_has_x(a) for a in t.get("args", []))


def _tristate_enable(t: dict) -> dict | None:
    """`en ? d : 'z` -> en; `en ? 'z : d` -> !en."""
    if t.get("op") != "?:":
        return None
    c, a, b = t["args"]
    zb = b.get("op") == "xz" and b.get("z") and not b.get("x")
    za = a.get("op") == "xz" and a.get("z") and not a.get("x")
    if zb and not za:
        return c
    if za and not zb:
        return NOT(c)
    return None


def _bits(lhs: dict, width: int) -> set[int] | None:
    """Bits of the base signal an lvalue writes; None = unknown (variable select)."""
    if "ref" in lhs:
        return set(range(max(width, 1)))
    if lhs.get("op") == "[]" and "const" in lhs["args"][1]:
        return {lhs["args"][1]["const"]}
    if lhs.get("op") == "[:]" and "const" in lhs["args"][1] and "const" in lhs["args"][2]:
        a, b = lhs["args"][1]["const"], lhs["args"][2]["const"]
        return set(range(min(a, b), max(a, b) + 1))
    return None


def _base(t: dict) -> dict:
    while t.get("op") in ("[]", "[:]"):
        t = t["args"][0]
    return t


def x_drive(cx: Ctx) -> None:
    for inst in cx.instances:
        widths = cx.widths(inst)
        # x assignments
        for a in inst.assigns:
            if _has_x(a.rhs_tree):
                cx.find("x.assign", inst, f"{', '.join(a.lhs)} 被赋 X（{path_text(a.cond_stack)}）。"
                        "仿真与综合对 X 的处理不同；若作无关值使用请确认", INFO, a.lhs, a.location)

        # drivers per signal: (driver id, bits, tri-state enable)
        drivers: dict[str, list[tuple]] = {}
        for a in inst.assigns:
            if len(a.lhs) != 1:
                continue
            drv = ("proc", a.proc) if a.proc is not None and inst.procs[a.proc].kind != "assign" else ("assign", id(a))
            bits = _bits(a.lhs_tree, widths.get(a.lhs[0], 1))
            drivers.setdefault(a.lhs[0], []).append((drv, bits, _tristate_enable(a.rhs_tree), a.location))
        for child in (i for i in cx.instances if i.parent == inst.path):
            for c in child.connections:
                e = c.get("expr")
                if c["direction"] == "out" and e:
                    parts = e["args"] if e.get("op") == "{}" else [e]
                    for part in parts:
                        for base in tree_refs(part):
                            bits = _bits(part, widths.get(base, 1)) if X.is_ref(_base(part), base) else None
                            drivers.setdefault(base, []).append(((f"port {c['port']}", child.path), bits, None, ""))

        for sig, ds in sorted(drivers.items()):
            by_drv: dict = {}
            for drv, bits, en, loc in ds:
                d = by_drv.setdefault(drv, [set(), False, [], loc])
                if bits is None:
                    d[1] = True
                else:
                    d[0] |= bits
                d[2].append(en)
            if len(by_drv) < 2:
                continue
            ens = [x for d in by_drv.values() for x in d[2]]
            if all(e is not None for e in ens):
                if len(ens) >= 2:
                    pairs = [AND(ens[i], ens[j]) for i in range(len(ens)) for j in range(i + 1, len(ens))]
                    cx.prop(inst, "assert", "x.bus_conflict", NOT(OR(*pairs)), clock=cx.inst_clock(inst),
                            message=f"三态总线 {sig} 同一时刻至多一个驱动使能",
                            on_trigger={"message": f"三态总线 {sig} 可能同时有多个驱动使能（总线冲突）",
                                        "severity": ERROR, "signals": [sig]})
                continue
            items = list(by_drv.items())
            overlap = False
            for i in range(len(items)):
                for j in range(i + 1, len(items)):
                    (_, (bi, ui, _, _)), (_, (bj, uj, _, _)) = items[i], items[j]
                    if ui or uj or bi & bj:
                        overlap = True
            if overlap and not _slang_has(cx, "x.multi_driver", inst.module, sig):
                where = "；".join(sorted({f"{k[0]} {k[1]}" if k[0].startswith("port") else
                                         ("过程块" if k[0] == "proc" else "连续赋值") for k in by_drv}))
                cx.find("x.multi_driver", inst, f"{sig} 有多个驱动源（{where}），驱动的位有重叠", ERROR, [sig],
                        next((d[3] for d in by_drv.values() if d[3]), ""))

        # floating: output or read signal without driver; unconnected child inputs
        driven = set(drivers) | {p.name for p in inst.ports if p.direction in ("in", "inout")}
        read: set[str] = set()
        for a in inst.assigns:
            read |= set(a.rhs_refs)
            for e in X.cond_exprs(a.cond_stack):
                read |= tree_refs(e)
        for e in cx.child_exprs.get(inst.path, []):
            read |= tree_refs(e)
        for p in inst.ports:
            if p.direction == "out" and not p.is_interface and p.name not in driven:
                if not _slang_has(cx, "x.floating", inst.module, p.name):
                    cx.find("x.floating", inst, f"输出端口 {p.name} 没有驱动（浮空）", WARNING, [p.name])
        for s in inst.signals:
            if s.name in read and s.name not in driven and not s.array_dims and "." not in s.name:
                if not _slang_has(cx, "x.floating", inst.module, s.name):
                    cx.find("x.floating", inst, f"信号 {s.name} 被读取但没有驱动（浮空）", WARNING, [s.name], s.location)
        for child in (i for i in cx.instances if i.parent == inst.path):
            ports = {p.name: p for p in child.ports}
            for c in child.connections:
                port = ports.get(c["port"])
                if c["direction"] == "in" and c.get("expr") is None and port is not None and not port.is_interface:
                    cx.find("x.floating", inst, f"实例 {child.path} 的输入端口 {c['port']} 未连接（浮空）",
                            WARNING, [c["port"]])


# --------------------------------------------------------------------------- #
# Control completeness
# --------------------------------------------------------------------------- #

def control(cx: Ctx) -> None:
    for inst in cx.instances:
        widths = cx.widths(inst)
        rst = cx.g.resets.get(inst.path, {})
        seen = set()
        for a in inst.assigns:
            for i, c in enumerate(a.cond_stack):
                if c["kind"] != "case" or not c.get("check"):
                    continue
                k = _key(a.proc, c["subject"], c.get("all"))
                if k in seen:
                    continue
                seen.add(k)
                items = c.get("all") or []
                guard, in_reset = stack_guard(a.cond_stack[:i], rst)
                if in_reset or not items:
                    continue
                subj = X.text(c["subject"])
                clock = cx.clock_for(inst, a.proc)
                # full: unique / priority
                consts = [x for x in items if "const" in x]
                w = width_of(c["subject"], widths)
                if c["check"] in ("unique", "priority") and not c.get("has_default") \
                        and not (c.get("condition") == "case" and len(consts) == len(items) and w
                                 and 2 ** w <= len(set(x["const"] for x in consts))):
                    cx.prop(inst, "assert", "control.case_incomplete", OR(*[_case_match(c, x) for x in items]),
                            guard=guard, clock=clock, message=f"{c['check']} case({subj}) 的选择值总在分支项中",
                            location=a.location, key=k,
                            on_trigger={"message": f"{c['check']} case({subj}) 出现未列出的取值（case 不全）",
                                        "severity": ERROR, "signals": sorted(tree_refs(c["subject"]))})
                # parallel: unique / unique0 with wildcard items
                if c["check"] in ("unique", "unique0") and c.get("condition") != "case" and len(items) >= 2:
                    ms = [_case_match(c, x) for x in items]
                    pairs = [AND(ms[p], ms[q]) for p in range(len(ms)) for q in range(p + 1, len(ms))]
                    cx.prop(inst, "assert", "control.case_parallel", NOT(OR(*pairs)), guard=guard, clock=clock,
                            message=f"{c['check']} case({subj}) 至多匹配一个分支项", location=a.location, key=k,
                            on_trigger={"message": f"{c['check']} case({subj}) 同时匹配多个分支项（case 不并行）",
                                        "severity": ERROR, "signals": sorted(tree_refs(c["subject"]))})

        # async set and reset of one block active together
        for pi, p in enumerate(inst.procs):
            asyncs = [ev for ev in p.sensitivity[1:] if ev.get("edge")]
            if len(p.sensitivity) < 3 or len(asyncs) < 2:
                continue
            act = [(ev["signal"], NOT(ref(ev["signal"])) if ev["edge"] == "negedge" else ref(ev["signal"]))
                   for ev in asyncs]
            pairs = [AND(act[i][1], act[j][1]) for i in range(len(act)) for j in range(i + 1, len(act))]
            names = [s for s, _ in act]
            cx.prop(inst, "assert", "control.set_reset", NOT(OR(*pairs)), clock=None,
                    resets={k: v for k, v in rst.items() if k not in names},
                    message=f"异步控制 {', '.join(names)} 不同时有效", location=p.location, key=_key(pi),
                    on_trigger={"message": f"异步置位/复位 {', '.join(names)} 可能同时有效（若来自输入端口，需要环境约束）",
                                "severity": WARNING, "signals": names})


# --------------------------------------------------------------------------- #
# Timing risks
# --------------------------------------------------------------------------- #

def _sccs(nodes: list[str], succ: dict[str, list[str]]) -> list[list[str]]:
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack, on, out = [], set(), []
    counter = 0
    for root in nodes:
        if root in index:
            continue
        work = [(root, iter(succ.get(root, [])))]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on.add(root)
        while work:
            v, it = work[-1]
            nxt = next(it, None)
            if nxt is not None:
                if nxt not in index:
                    index[nxt] = low[nxt] = counter
                    counter += 1
                    stack.append(nxt)
                    on.add(nxt)
                    work.append((nxt, iter(succ.get(nxt, []))))
                elif nxt in on:
                    low[v] = min(low[v], index[nxt])
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[v])
            if low[v] == index[v]:
                comp = []
                while True:
                    w = stack.pop()
                    on.discard(w)
                    comp.append(w)
                    if w == v:
                        break
                out.append(sorted(comp))
    return out


def timing(cx: Ctx) -> None:
    g = cx.g
    succ: dict[str, list[str]] = {}
    self_loops = set()
    cont_self = set()
    for inst in cx.instances:
        for a in inst.assigns:
            if a.proc is None or inst.procs[a.proc].kind == "assign":
                for lhs in a.lhs:
                    if lhs in a.rhs_refs or lhs in tree_refs({"op": "x", "args": X.cond_exprs(a.cond_stack)}):
                        cont_self.add(f"{inst.path}.{lhs}")
    for e in g.edges:
        if e.delay != 0:
            continue
        if e.src == e.dst:
            if e.src in cont_self:
                self_loops.add(e.src)
            continue
        succ.setdefault(e.src, []).append(e.dst)
    for comp in _sccs(sorted(g.nodes), {k: sorted(set(v)) for k, v in succ.items()}):
        if len(comp) < 2:
            continue
        inst = cx.by_path.get(g.nodes[comp[0]].inst)
        cx.find("timing.comb_loop", inst, f"组合环：{' → '.join(comp)}（按信号粒度，若只涉及不同位可能是假环）",
                ERROR, comp)
    for n in sorted(self_loops):
        node = g.nodes[n]
        cx.find("timing.comb_loop", cx.by_path.get(node.inst), f"组合环：{n} 的连续赋值读取了自身", ERROR, [n])
    for inst in cx.instances:
        for p in inst.procs:
            if p.kind == "always_latch" and not any(d["check"] == "timing.latch" and d["module"] == inst.module
                                                    for d in cx.scan.diags):
                cx.find("timing.latch", inst, "always_latch：显式锁存器", INFO, [], p.location)


# --------------------------------------------------------------------------- #
# Recognized structures: counters and FIFOs
# --------------------------------------------------------------------------- #

def _is_address(cx: Ctx, node: str) -> bool:
    """The counter feeds a memory index, directly or through plain port connections:
    a pointer, whose wrap-around is the intended behavior."""
    addr = {f"{p.inst}.{a}" for p in cx.n2.patterns if p.kind == "memory"
            for a in p.detail.get("write_addr", []) + p.detail.get("read_addr", [])}
    seen, todo = {node}, [node]
    fwd: dict[str, list[str]] = {}
    for e in cx.g.edges:
        if e.delay == 0 and e.kind == "data" and e.via == "port":
            fwd.setdefault(e.src, []).append(e.dst)
    while todo:
        n = todo.pop()
        if n in addr:
            return True
        for m in fwd.get(n, []):
            if m not in seen:
                seen.add(m)
                todo.append(m)
    return False


def structures(cx: Ctx) -> None:
    for p in cx.n2.patterns:
        if p.kind != "counter":
            continue
        inst = cx.by_path[p.inst]
        view = cx.views[p.inst]
        r, W = p.signal, view.widths.get(p.signal, 0)
        if not W or W > 31:
            continue
        rst = cx.g.resets.get(inst.path, {})
        if _is_address(cx, f"{p.inst}.{r}"):
            continue
        writes = [a for a in view.writes.get(r, []) if view.clocked(a) and X.is_ref(a.lhs_tree, r)]
        free = not p.detail.get("compared_with")
        for a in writes:
            guard0, in_reset = stack_guard(a.cond_stack, rst)
            if in_reset:
                continue
            for leaf, extra in X.leaves(a.rhs_tree):
                terms = _sum_terms(leaf)
                if [s for s, x in terms if X.is_ref(x, r)] != [1]:
                    continue
                up = [x for s, x in terms if s > 0 and not X.is_ref(x, r)]
                down = [x for s, x in terms if s < 0]
                guard = AND(guard0, *[c["expr"] if c["polarity"] else NOT(c["expr"]) for c in extra])
                if free and guard is None:
                    continue  # free-running counter: wrapping is what it is for
                lhs = r_ = ref(r)
                for x in up:
                    lhs = op("+", lhs, x)
                clock = cx.proc_clock(inst, a.proc)
                if up:
                    rhs = const(2 ** W - 1)
                    for x in down:
                        rhs = op("+", rhs, x)
                    cx.prop(inst, "assert", "counter.overflow", op("<=", lhs, rhs), guard=guard, clock=clock,
                            message=f"计数器 {r}（{W} 位）递增时不越过 {2 ** W - 1}", location=a.location,
                            on_trigger={"message": f"计数器 {r} 会上溢回绕（{2 ** W - 1} → 0）", "severity": WARNING,
                                        "signals": [r]})
                if down:
                    sub = down[0]
                    for x in down[1:]:
                        sub = op("+", sub, x)
                    cx.prop(inst, "assert", "counter.underflow", op(">=", lhs if up else r_, sub), guard=guard,
                            clock=clock, message=f"计数器 {r} 递减时不低于 0", location=a.location,
                            on_trigger={"message": f"计数器 {r} 会下溢回绕（0 → {2 ** W - 1}）", "severity": WARNING,
                                        "signals": [r]})
    fifos(cx)


def fifos(cx: Ctx) -> None:
    roles: dict[str, dict[str, str]] = {}
    for e in cx.n2.events:
        if e.kind in ("push_full", "pop_empty", "push_pop", "full_empty"):
            roles.setdefault(e.inst, {}).update(e.signals)
    for path, rs in sorted(roles.items()):
        inst = cx.by_path[path]
        view = cx.views[path]
        occ = [p for p in cx.n2.patterns if p.inst == path and p.kind == "counter"
               and p.detail.get("direction") == "up_down"]
        clock = cx.inst_clock(inst)
        if not clock:
            continue
        push, pop, full, empty = (rs.get(k) for k in ("push", "pop", "full", "empty"))
        ptrs = [p for p in cx.n2.patterns if p.inst == path and p.kind == "counter" and p.detail.get("direction") == "up"]

        def guarded_by(p, sig):
            return any(sig in tree_refs({"op": "x", "args": X.cond_exprs(a.cond_stack)}) or sig in a.rhs_refs
                       for a in view.writes.get(p.signal, []))

        for side, req, flag, other in (("overflow", push, full, pop), ("underflow", pop, empty, push)):
            if not (req and flag):
                continue
            ante = AND(ref(flag), ref(req), NOT(ref(other)) if other else None)
            for c in occ + [p for p in ptrs if guarded_by(p, req)]:
                what = "占用计数" if c in occ else "指针"
                cx.prop(inst, "assert", f"fifo.{side}", EQ(ref(c.signal), PAST(ref(c.signal))), guard=ante,
                        next=True, clock=clock,
                        message=f"{'满时写' if side == 'overflow' else '空时读'}被忽略：{what} {c.signal} 不变",
                        on_trigger={"message": f"FIFO {'上溢：满时写入' if side == 'overflow' else '下溢：空时读出'}"
                                               f"仍改变了{what} {c.signal}", "severity": ERROR,
                                    "signals": [c.signal, req, flag]})
        for flag in (full, empty):
            if flag:
                cx.prop(inst, "cover", "fifo.reachable", ref(flag), clock=clock, message=f"FIFO 能到达 {flag}",
                        on_trigger={"message": f"FIFO 的 {flag} 不可达", "severity": WARNING, "signals": [flag]})
        if full and occ:
            for a in view.writes.get(full, []):
                for sig, o, k in X.compares(a.rhs_tree):
                    if o in ("==", ">=") and sig == occ[0].signal:
                        cx.prop(inst, "assert", "fifo.bound", op("<=", ref(sig), const(k)), clock=clock,
                                message=f"FIFO 占用 {sig} 不超过满阈值 {k}",
                                on_trigger={"message": f"FIFO 占用 {sig} 超过了满阈值 {k}", "severity": ERROR,
                                            "signals": [sig]})


ALL = [slang_findings, dead_code, fsm_checks, array_arith, x_drive, control, timing, structures]
