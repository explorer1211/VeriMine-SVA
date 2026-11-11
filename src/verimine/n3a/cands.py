"""N3a steps 1-3: assumption candidates, and checking them against traces.

Two sources (spec step 2):
  structure  N2 interface events of every modeled instance (top and submodules), turned into
             protocol templates when the instance is the receiving side:
               valid/ready, req/ack   valid && !ready |=> valid          protocol_hold
                                      valid && !ready |=> data stable    protocol_hold_data
               push/full, pop/empty   full |-> !push                     protocol_flow
  traces     input-port behavior in the user's traces (waveforms / testbench, legality "user";
             generated stimulus says nothing about the environment):
               mutex        1-bit inputs each seen high, never together  !(a && b)
               onehot       a vector input never has two bits set         (x & (x - 1)) == 0
               reset_value  input constant during reset                   !rst_n |-> x == v
               min_hold     every high pulse of x lasts >= L cycles       !x && $past(x) |-> $past(x, 2) ...
               range        observed min/max of a vector input            x <= hi

Default handling follows the spec table: protocol hold and reset value are high confidence
(recommended), mutex / onehot / min hold need confirmation, data ranges are not adopted.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from verimine.n1.assign import tree_refs
from verimine.n2.events import _role_of
from verimine.n2b.props import AND, EQ, NOT, PAST, const, op, ref
from verimine.n3.cover import _Unknown, evaluate

HIGH, MEDIUM, LOW = "高", "中", "低"
TYPES = {
    "protocol_hold": ("协议保持", HIGH, "accept"),
    "protocol_hold_data": ("协议保持（数据）", MEDIUM, "confirm"),
    "protocol_flow": ("流控（满不写 / 空不读）", MEDIUM, "confirm"),
    "reset_value": ("复位期间固定值", HIGH, "accept"),
    "mutex": ("互斥", MEDIUM, "confirm"),
    "onehot": ("独热", MEDIUM, "confirm"),
    "min_hold": ("最小保持周期", MEDIUM, "confirm"),
    "range": ("数据取值范围", LOW, "reject"),
    "config": ("配置中的假设", "", ""),
}
RECOMMEND = {"accept": "建议接受", "confirm": "需确认", "reject": "默认不采纳", "": ""}


@dataclass
class Cand:
    type: str
    scope: str                  # instance path the signals are relative to
    module: str
    name: str
    text: str                   # SVA text, as it goes into `assumes:`
    ante: dict | None = None
    cons: dict | None = None
    next: bool = False          # ante |=> cons (else |->)
    confidence: str = ""
    recommend: str = ""
    source: str = "structure"   # structure | trace | structure+trace | config
    evidence: list[str] = field(default_factory=list)
    signals: list[str] = field(default_factory=list)
    status: str = "PROPOSED"    # PROPOSED | ACCEPTED | REJECTED | DISCHARGED | OPEN
    reason: str = ""
    trace: dict = field(default_factory=dict)       # {"support", "violations", "cases"}
    strength: dict = field(default_factory=dict)    # over-strength check
    discharge: dict = field(default_factory=dict)   # assume-guarantee
    id: str = ""

    @property
    def key(self) -> str:
        return f"{self.scope}|{norm_text(self.text)}"

    @property
    def type_name(self) -> str:
        return TYPES[self.type][0]


def norm_text(s: str) -> str:
    return re.sub(r"[\s()]", "", s)


# --------------------------------------------------------------------------- #
# Readable SVA text from expression trees
# --------------------------------------------------------------------------- #

_PREC = {"*": 2, "/": 2, "%": 2, "+": 3, "-": 3, "<<": 4, ">>": 4, "<": 5, "<=": 5, ">": 5, ">=": 5,
         "==": 6, "!=": 6, "&": 7, "^": 8, "|": 9, "&&": 10, "||": 11}


def pretty(t: dict) -> str:
    if "ref" in t:
        return t["ref"]
    if "const" in t:
        return t.get("text") or str(t["const"])
    o, a = t.get("op"), t.get("args", [])
    if o == "$past":
        k = t.get("k", 1)
        return f"$past({pretty(a[0])})" if k == 1 else f"$past({pretty(a[0])}, {k})"
    if o in ("!", "~"):
        inner = pretty(a[0])
        return f"{o}{inner}" if "ref" in a[0] or a[0].get("op") == "$past" else f"{o}({inner})"
    if o in _PREC and len(a) == 2:
        p = _PREC[o]

        def side(x, right):
            s = pretty(x)
            q = _PREC.get(x.get("op"), 0) if x.get("args") and x.get("op") in _PREC else 0
            # bitwise / comparison over arithmetic gets parentheses too, for readability
            if q > p or (q and q != p and 5 <= p <= 9) or (right and q == p and o not in ("&&", "||", "&", "|", "+", "*")):
                return f"({s})"
            return s
        return f"{side(a[0], False)} {o} {side(a[1], True)}"
    from verimine.n2b.render import sv
    return sv(t)


def text_of(ante: dict | None, cons: dict, nxt: bool) -> str:
    if ante is None:
        return pretty(cons)
    return f"{pretty(ante)} {'|=>' if nxt else '|->'} {pretty(cons)}"


def _cand(type_: str, scope: str, module: str, name: str, ante, cons, nxt=False, evidence=(), source="structure",
          signals=()) -> Cand:
    tn, conf, rec = TYPES[type_]
    return Cand(type_, scope, module, name, text_of(ante, cons, nxt), ante, cons, nxt, conf, rec, source,
                list(evidence), sorted(signals) or sorted(tree_refs(cons) | (tree_refs(ante) if ante else set())))


def _ident(*parts: str) -> str:
    return "n3a_" + "_".join(re.sub(r"\W+", "_", p).strip("_") for p in parts if p)


# --------------------------------------------------------------------------- #
# Structural candidates (N2 interface events)
# --------------------------------------------------------------------------- #

_DATA_RE = re.compile(r"(data|payload|addr|wdata|din|dat|len|id|tag|strb|keep|last|user)", re.I)


def structural(n2, special: dict[str, set[str]]) -> list[Cand]:
    """special: instance path -> clock/reset port names (never constrained)."""
    by_path = {i.path: i for i in n2.n1.elaboration.instances}
    out: list[Cand] = []
    roles_of = {}
    for e in n2.events:
        roles_of.setdefault(e.inst, set()).update(e.signals.values())
    for e in n2.events:
        if e.confidence == LOW or e.inst not in by_path:
            continue
        inst = by_path[e.inst]
        dirs = {p.name: p.direction for p in inst.ports}
        ev = [f"N2 接口事件 {e.kind}（{e.confidence}）：" + "；".join(e.evidence)]
        if e.kind in ("valid_ready", "req_ack"):
            hold, give = ("valid", "ready") if e.kind == "valid_ready" else ("req", "ack")
            v, r = e.signals[hold], e.signals[give]
            if dirs.get(v) != "in" or dirs.get(r) != "out":
                continue  # the instance drives the request: a guarantee, not an assumption
            ante = AND(ref(v), NOT(ref(r)))
            out.append(_cand("protocol_hold", e.inst, inst.module, _ident("hold", e.inst.rsplit(".", 1)[-1], v),
                             ante, ref(v), True, ev + [f"{v} 是输入、{r} 是输出：{v} 在 {r} 之前不撤销"]))
            prefix = next((p for role, p in _role_of(v) if role == hold), "")
            data = [p.name for p in inst.ports if p.direction == "in" and not p.is_interface
                    and p.name not in roles_of.get(e.inst, set()) and p.name not in special.get(e.inst, set())
                    and (p.name.lower().startswith(prefix) if prefix else _DATA_RE.search(p.name))]
            for dname in sorted(data):
                out.append(_cand("protocol_hold_data", e.inst, inst.module,
                                 _ident("stable", e.inst.rsplit(".", 1)[-1], dname),
                                 ante, EQ(ref(dname), PAST(ref(dname))), True,
                                 ev + [f"{dname} 与 {v} 同前缀，按命名认为是它携带的数据（弱证据）"]))
        elif e.kind in ("push_full", "pop_empty"):
            act, flag = ("push", "full") if e.kind == "push_full" else ("pop", "empty")
            a, f = e.signals[act], e.signals[flag]
            if dirs.get(a) != "in" or dirs.get(f) != "out":
                continue
            note = []
            if e.confidence == HIGH:
                note = [f"设计里 {a} 与 {f} 一起使用（可能已自行屏蔽{'满时写' if act == 'push' else '空时读'}），"
                        "此假设可能不必要"]
            out.append(_cand("protocol_flow", e.inst, inst.module, _ident("no", act, "when", flag), ref(f),
                             NOT(ref(a)), False, ev + note))
    return _dedupe(out)


def _dedupe(cs: list[Cand]) -> list[Cand]:
    seen, out = set(), []
    for c in cs:
        if c.key not in seen:
            seen.add(c.key)
            out.append(c)
    return out


# --------------------------------------------------------------------------- #
# Evaluating candidates on traces
# --------------------------------------------------------------------------- #

def _subst_past(t: dict, out: list) -> dict:
    if t.get("op") == "$past":
        name = f"__past{len(out)}"
        out.append((name, t["args"][0], t.get("k", 1)))
        return {"ref": name}
    if "args" in t:
        return {**t, "args": [_subst_past(a, out) for a in t["args"]]}
    return t


def series(t: dict, cols: dict[str, list], n: int, widths: dict[str, int]) -> list:
    """Value of tree t on every row (None = unknown); $past reads earlier rows."""
    pasts: list = []
    t2 = _subst_past(t, pasts)
    extra = {}
    for name, arg, k in pasts:
        s = series(arg, cols, n, widths)
        extra[name] = [s[i - k] if i - k >= 0 else None for i in range(n)]
    refs = tree_refs(t2)
    out = []
    for i in range(n):
        env = {r: (extra[r][i] if r in extra else (cols[r][i] if r in cols else None)) for r in refs}
        try:
            out.append(evaluate(t2, env, widths))
        except _Unknown:
            out.append(None)
    return out


def check(c: Cand, cases, widths: dict[str, int]) -> dict:
    """{"support": antecedent held with a known consequent, "violations", "cases": with support,
    "first": first violation}."""
    pre = c.scope + "."
    support = violations = ncases = 0
    first = None
    for case in cases:
        cols = {k[len(pre):]: v for k, v in case.signals.items() if k.startswith(pre)}
        n = case.cycles
        need = tree_refs(c.cons) | (tree_refs(c.ante) if c.ante else set())
        if not need <= set(cols):
            continue
        ante = series(c.ante, cols, n, widths) if c.ante else [1] * n
        cons = series(c.cons, cols, n, widths)
        hit = 0
        for i in range(n):
            j = i + 1 if c.next else i
            if j >= n or not ante[i] or cons[j] is None:
                continue
            hit += 1
            if not cons[j]:
                violations += 1
                if first is None:
                    first = {"case": case.id, "row": j}
        support += hit
        ncases += bool(hit)
    return {"support": support, "violations": violations, "cases": ncases, "first": first}


# --------------------------------------------------------------------------- #
# Mining the user's traces
# --------------------------------------------------------------------------- #

def mine(cases, design, top_module: str, params: dict) -> list[Cand]:
    if not cases:
        return []
    top = design.top
    min_sup = int(params["n3a_min_support"])
    width = {x: design.nodes[x][2] for x in design.inputs}
    name = lambda x: x[len(top) + 1:]  # noqa: E731
    one = [x for x in design.inputs if width[x] == 1]
    vec = [x for x in design.inputs if 1 < width[x] <= 64]
    out: list[Cand] = []
    tr = "用户轨迹"

    # mutex: greedy groups of 1-bit inputs pairwise never high together
    high = {x: sum(1 for c in cases for v in c.signals.get(x, []) if v == 1) for x in one}
    act = [x for x in one if high[x] >= min_sup]

    def together(a, b):
        return sum(1 for c in cases for u, v in zip(c.signals.get(a, []), c.signals.get(b, [])) if u == 1 and v == 1)

    mutex = {(a, b) for i, a in enumerate(act) for b in act[i + 1:] if together(a, b) == 0}
    left = list(act)
    while left and len(out) < int(params["n3a_max_mutex"]):
        g = [left.pop(0)]
        for x in list(left):
            if all(tuple(sorted((x, y), key=act.index)) in mutex for y in g):
                g.append(x)
                left.remove(x)
        if len(g) < 2:
            continue
        pairs = [NOT(AND(ref(name(a)), ref(name(b)))) for i, a in enumerate(g) for b in g[i + 1:]]
        out.append(_cand("mutex", top, top_module, _ident("mutex", *[name(x) for x in g]), None, AND(*pairs),
                         evidence=[f"{tr}：" + "、".join(f"{name(x)} 有效 {high[x]} 拍" for x in g) + "，从未同时有效"],
                         source="trace"))

    # onehot0 vectors
    onehot = set()
    for x in vec:
        vals = [v for c in cases for v in c.signals.get(x, []) if v is not None]
        nz = [v for v in vals if v]
        if len(nz) >= min_sup and all(v & (v - 1) == 0 for v in vals) and len(set(nz)) >= 2:
            onehot.add(x)
            n = name(x)
            out.append(_cand("onehot", top, top_module, _ident("onehot", n), None,
                             EQ(op("&", ref(n), op("-", ref(n), const(1))), const(0)),
                             evidence=[f"{tr}：{n} 取过 {len(set(nz))} 个不同的单热值，从未有两位同时为 1"],
                             source="trace"))

    # reset values
    for x in design.inputs:
        vals, rows = set(), 0
        for c in cases:
            ri = c.detail.get("reset_inputs") or {}
            rows += ri.get("rows", 0)
            vals |= {(-1 if v is None else v) for v in ri.get("values", {}).get(x, [])}
        if rows >= 2 and len(vals) == 1 and -1 not in vals:
            v = vals.pop()
            act_rst = None
            for r, lv in sorted(design.resets.items()):
                t = NOT(ref(name(r))) if lv == "low" else ref(name(r))
                act_rst = t if act_rst is None else op("||", act_rst, t)
            if act_rst is not None:
                out.append(_cand("reset_value", top, top_module, _ident("rstval", name(x)), act_rst,
                                 EQ(ref(name(x)), const(v)),
                                 evidence=[f"{tr}：复位期间 {rows} 拍里 {name(x)} 始终为 {v}"], source="trace"))

    # minimum high-pulse length of 1-bit inputs
    for x in one:
        runs = []
        for c in cases:
            col = c.signals.get(x, [])
            i = 0
            while i < len(col):
                if col[i] == 1:
                    j = i
                    while j < len(col) and col[j] == 1:
                        j += 1
                    if i > 0 and j < len(col) and col[i - 1] == 0:   # whole pulse inside the case
                        runs.append(j - i)
                    i = j
                else:
                    i += 1
        if len(runs) >= int(params["n3a_min_pulses"]) and min(runs) >= 2:
            L, n = min(runs), name(x)
            cons = AND(*[{"op": "$past", "args": [ref(n)], "k": k} for k in range(2, L + 1)])
            out.append(_cand("min_hold", top, top_module, _ident("hold", n, str(L)),
                             AND(NOT(ref(n)), PAST(ref(n))), cons,
                             evidence=[f"{tr}：{n} 的 {len(runs)} 个高脉冲最短 {L} 拍"], source="trace"))

    # ranges of vectors (not adopted by default)
    for x in vec:
        if x in onehot:
            continue
        vals = [v for c in cases for v in c.signals.get(x, []) if v is not None]
        if len(vals) < min_sup:
            continue
        lo, hi, n = min(vals), max(vals), name(x)
        if lo == 0 and hi == (1 << width[x]) - 1:
            continue
        if lo == hi:
            cons = EQ(ref(n), const(lo))
        elif lo == 0:
            cons = op("<=", ref(n), const(hi))
        else:
            cons = AND(op(">=", ref(n), const(lo)), op("<=", ref(n), const(hi)))
        out.append(_cand("range", top, top_module, _ident("range", n), None, cons,
                         evidence=[f"{tr}：{n} 观察到的取值在 [{lo}, {hi}]（{len(vals)} 个样本）；"
                                   "取值范围容易过拟合、过强，默认不采纳"], source="trace"))
    return out
