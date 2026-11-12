"""N4 path A: rules and templates over N2's structural evidence. No traces needed; every
instance goes straight to formal verification (N5-N7).

  reset_value     register r reset to constant v            rst active |=> r == v
  onehot          FSM whose every state is one bit           (s & (s - 1)) == 0 && s != 0
  fsm_encoding    FSM with unused encodings                  s == A || s == B || ...
  fsm_transition  FSM state v and the states it can go to    s == v |=> s == t1 || s == t2
  mutex           two 1-bit combinational signals whose      !(a && b)
                  drivers contradict (a = x, b = y && !x; a = s == 1, b = s == 2)
  handshake_hold  the instance drives valid (req), receives  valid && !ready |=> valid
                  ready (ack): what it guarantees
  handshake_data  data with the same prefix as valid         valid && !ready |=> data == $past(data)
  counter_range   counter compared with K, never above K     cnt <= K
                  by reset or load
  fifo_flags      FIFO full / empty never together            !(full && empty)
  fifo_bound      occupancy counter vs. the full threshold    count <= K
  fifo_overflow   full and push (no pop): occupancy holds     full && push && !pop |=> count == $past(count)
  fifo_underflow  empty and pop (no push)                     empty && pop && !push |=> count == $past(count)

Templates propose; they do not decide. A wrong instance (a counter that does run past K) is
caught by N7 as FAILED, not filtered here.
"""
from __future__ import annotations

from verimine.n2 import exprs as X
from verimine.n2.events import _role_of
from verimine.n2.patterns import InstView
from verimine.n2b.props import AND, EQ, NOT, OR, PAST, const, op, ref
from verimine.n4.ir import Cand, canon


def reset_expr(resets: dict[str, str]) -> dict | None:
    t = None
    for r, lv in sorted(resets.items()):
        x = NOT(ref(r)) if lv == "low" else ref(r)
        t = x if t is None else op("||", t, x)
    return t


def inst_clock(inst, clocks: set[str]) -> dict | None:
    if len(clocks) != 1:
        return None
    clk = next(iter(clocks))
    for p in inst.procs:
        for ev in p.sensitivity:
            if ev.get("signal") == clk and ev.get("edge"):
                return {"name": clk, "edge": ev["edge"]}
    return {"name": clk, "edge": "posedge"}


class Scope:
    """What the templates need about one modeled instance."""

    def __init__(self, n2, inst, child_exprs: list[dict]):
        g = n2.vdg
        self.inst = inst
        self.path = inst.path
        self.resets = dict(g.resets.get(inst.path, {}))
        self.clocks = set(g.clocks.get(inst.path, set()))
        self.view = InstView(inst, self.resets, child_exprs)
        self.clock = inst_clock(inst, self.clocks)
        self.ports = {p.name for p in inst.ports}
        self.dirs = {p.name: p.direction for p in inst.ports}

    def level(self, names) -> str:
        return "port" if set(names) <= self.ports else "internal"

    def cand(self, type_: str, ante, cons, nxt=False, evidence=(), **detail) -> Cand:
        c = Cand(type_, self.path, self.inst.module, ante, cons, nxt, ["A"], "none", clock=self.clock,
                 resets=dict(self.resets), evidence=list(evidence), detail=detail)
        c.level = self.level(c.features)
        return c


def generate(n2, scopes: dict[str, Scope]) -> list[Cand]:
    out: list[Cand] = []
    for path in sorted(scopes):
        sc = scopes[path]
        if sc.clock is None:
            continue  # every template here is clocked (or needs a clock for disable iff)
        out += _reset_values(sc)
        out += _mutex(sc)
    for f in n2.fsms:
        if f.inst in scopes and scopes[f.inst].clock:
            out += _fsm(scopes[f.inst], f)
    for p in n2.patterns:
        if p.kind == "counter" and p.inst in scopes and scopes[p.inst].clock:
            out += _counter(scopes[p.inst], p)
    out += _handshakes(n2, scopes)
    out += _fifos(n2, scopes)
    return out


# --------------------------------------------------------------------------- #

def _reset_values(sc: Scope) -> list[Cand]:
    v, out = sc.view, []
    act = reset_expr(sc.resets)
    if act is None:
        return []
    for r in v.registers:
        if r in v.arrays or r in sc.resets:
            continue
        vals = {X.const_of(a.rhs_tree) for a in v.writes[r]
                if v.clocked(a) and v.in_reset(a) and X.is_ref(a.lhs_tree, r)}
        if len(vals) != 1 or None in vals:
            continue
        val = vals.pop()
        c = sc.cand("reset_value", act, EQ(ref(r), const(val)), True, [f"{r} 在复位分支被赋常量 {val}"])
        c.resets = {}  # checked while reset is active: no disable iff
        out.append(c)
    return out


def _conjuncts(t: dict) -> list[dict]:
    if t.get("op") == "&&":
        return [x for a in t["args"] for x in _conjuncts(a)]
    return [t]


def _contradict(a: dict, b: dict) -> bool:
    ca, cb = canon(a), canon(b)
    if ca == canon(NOT(b)) or cb == canon(NOT(a)):
        return True
    ea, eb = list(X.compares(a)), list(X.compares(b))
    if a.get("op") == "==" and b.get("op") == "==" and len(ea) == 1 and len(eb) == 1:
        (sa, _, ka), (sb, _, kb) = ea[0], eb[0]
        return sa == sb and ka != kb
    return False


def _mutex(sc: Scope) -> list[Cand]:
    v = sc.view
    drivers = {}
    for name, ws in v.writes.items():
        if len(ws) != 1 or v.widths.get(name) != 1 or name in v.arrays:
            continue
        a = ws[0]
        if v.clocked(a) or a.cond_stack or not X.is_ref(a.lhs_tree, name):
            continue
        drivers[name] = _conjuncts(a.rhs_tree)
    names = sorted(drivers)
    out = []
    for i, x in enumerate(names):
        for y in names[i + 1:]:
            if any(_contradict(p, q) for p in drivers[x] for q in drivers[y]):
                out.append(sc.cand("mutex", None, NOT(AND(ref(x), ref(y))),
                                   evidence=[f"{x} 与 {y} 的驱动条件互斥"]))
    return out


def _fsm(sc: Scope, f) -> list[Cand]:
    s = f.state
    values = [x["value"] for x in f.states]
    names = {x["value"]: x["name"] for x in f.states}
    out = []
    ev = [f"N2 状态机 {s}（{f.confidence}）"]
    if len(values) >= 3 and all(x and x & (x - 1) == 0 for x in values):
        out.append(sc.cand("onehot", None, AND(EQ(op("&", ref(s), op("-", ref(s), const(1))), const(0)),
                                                op("!=", ref(s), const(0))), evidence=ev + ["所有状态编码都是单热码"]))
    elif f.width and 2 ** f.width > len(values):
        out.append(sc.cand("fsm_encoding", None, OR(*[EQ(ref(s), const(x)) for x in values]),
                           evidence=ev + [f"{s} 只取 {len(values)} 个合法编码"]))
    for x in f.reachable:
        to = sorted({t["to"] for t in f.transitions if t["from"] == x})
        if not to or set(to) >= set(values):
            continue
        out.append(sc.cand("fsm_transition", EQ(ref(s), const(x)), OR(*[EQ(ref(s), const(t)) for t in to]), True,
                           ev + [f"状态 {names.get(x, x)} 只能迁移到 " + "、".join(str(names.get(t, t)) for t in to)],
                           state=s, frm=x, to=to))
    return out


def _counter(sc: Scope, p) -> list[Cand]:
    d, r = p.detail, p.signal
    W = d.get("width") or 0
    cmp = [k for k in d.get("compared_with", []) if isinstance(k, int)]
    if not W or not cmp:
        return []
    K = max(cmp)
    starts = d.get("reset_values", []) + d.get("load_values", [])
    if K >= 2 ** W - 1 or any(v > K for v in starts):
        return []
    return [sc.cand("counter_range", None, op("<=", ref(r), const(K)),
                    evidence=[f"计数器 {r}（{W} 位）与 {K} 比较，复位 / 装载值都不超过 {K}"])]


def _handshakes(n2, scopes: dict[str, Scope]) -> list[Cand]:
    out = []
    roles_of: dict[str, set[str]] = {}
    for e in n2.events:
        roles_of.setdefault(e.inst, set()).update(e.signals.values())
    for e in n2.events:
        if e.kind not in ("valid_ready", "req_ack") or e.confidence == "低" or e.inst not in scopes:
            continue
        sc = scopes[e.inst]
        if sc.clock is None:
            continue
        hold, give = ("valid", "ready") if e.kind == "valid_ready" else ("req", "ack")
        v, r = e.signals[hold], e.signals[give]
        if sc.dirs.get(v) != "out" or sc.dirs.get(r) != "in":
            continue  # the other side holds: that is an assumption (N3a), not an assertion
        ante = AND(ref(v), NOT(ref(r)))
        ev = [f"N2 接口事件 {e.kind}（{e.confidence}）：{v} 是输出、{r} 是输入"]
        out.append(sc.cand("handshake_hold", ante, ref(v), True, ev + [f"{v} 在 {r} 之前不撤销"]))
        prefix = next((p for role, p in _role_of(v) if role == hold), "")
        if not prefix:
            continue
        for p in sc.inst.ports:
            if (p.direction == "out" and not p.is_interface and p.name.lower().startswith(prefix)
                    and p.name not in roles_of.get(e.inst, set())):
                out.append(sc.cand("handshake_data", ante, EQ(ref(p.name), PAST(ref(p.name))), True,
                                   ev + [f"{p.name} 与 {v} 同前缀，按命名认为是它携带的数据（弱证据）"]))
    return out


def _fifos(n2, scopes: dict[str, Scope]) -> list[Cand]:
    roles: dict[str, dict[str, str]] = {}
    for e in n2.events:
        if e.kind in ("push_full", "pop_empty", "push_pop", "full_empty") and e.confidence != "低":
            roles.setdefault(e.inst, {}).update(e.signals)
    out = []
    for path, rs in sorted(roles.items()):
        if path not in scopes or scopes[path].clock is None:
            continue
        sc = scopes[path]
        push, pop, full, empty = (rs.get(k) for k in ("push", "pop", "full", "empty"))
        ev = [f"N2 接口事件：{', '.join(f'{k}={v}' for k, v in sorted(rs.items()))}"]
        if full and empty:
            out.append(sc.cand("fifo_flags", None, NOT(AND(ref(full), ref(empty))), evidence=ev))
        occ = [p.signal for p in n2.patterns if p.inst == path and p.kind == "counter"
               and p.detail.get("direction") == "up_down"]
        if not occ:
            continue
        cnt = occ[0]
        if full:
            for a in sc.view.writes.get(full, []):
                for sig, o, k in X.compares(a.rhs_tree):
                    if sig == cnt and o in ("==", ">="):
                        out.append(sc.cand("fifo_bound", None, op("<=", ref(cnt), const(k)),
                                           evidence=ev + [f"{full} 由 {cnt} {o} {k} 得到"]))
        for kind, req, flag, other in (("fifo_overflow", push, full, pop), ("fifo_underflow", pop, empty, push)):
            if req and flag:
                out.append(sc.cand(kind, AND(ref(flag), ref(req), NOT(ref(other)) if other else None),
                                   EQ(ref(cnt), PAST(ref(cnt))), True,
                                   ev + [f"{'满时写' if kind == 'fifo_overflow' else '空时读'}被忽略，占用计数 {cnt} 不变"]))
    return out
