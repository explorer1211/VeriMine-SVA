"""N3 step 6: coverage of the traces, and the gap list.

  branches  every assignment's control path (the same targets as N2b's dead.branch, plus case
            defaults), evaluated on each sampled cycle from N1's expression trees
  states    every state of every N2 state machine, and its transitions (consecutive cycles)
  toggles   every bit of every sampled signal, 0 -> 1 and 1 -> 0

A branch whose condition reads a signal the waveform does not contain is "unobservable", not a
gap: nothing can be said about it from these traces.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from verimine.n1.assign import tree_refs
from verimine.n2b.checks import path_text, stack_guard, width_of
from verimine.n2b.render import wildcard_mask
from verimine.n3.sample import Case


class _Unknown(Exception):
    pass


def _mask(v: int, w: int | None) -> int:
    return v & ((1 << w) - 1) if w else v


def evaluate(t: dict, env: dict[str, int | None], widths: dict[str, int], ctx: int | None = None) -> int:
    """Value of an N1 expression tree under env (signal -> value). Raises _Unknown when a signal
    is missing or X, or the tree has a part that cannot be evaluated."""
    if "ref" in t:
        v = env.get(t["ref"])
        if v is None:
            raise _Unknown
        return v
    if "const" in t:
        return t["const"]
    o, a = t.get("op"), t.get("args", [])
    ev = lambda x, c=None: evaluate(x, env, widths, c)  # noqa: E731
    if o in ("&&", "||"):
        vals = []
        for x in a:
            try:
                vals.append(bool(ev(x)))
            except _Unknown:
                vals.append(None)
        if o == "&&":
            if False in vals:
                return 0
            if None in vals:
                raise _Unknown
            return 1
        if True in vals:
            return 1
        if None in vals:
            raise _Unknown
        return 0
    if o == "!":
        return int(not ev(a[0]))
    if o in ("==?", "!=?"):
        lit = wildcard_mask(a[1].get("text", "")) if a[1].get("op") == "xz" else None
        if lit is None:
            raise _Unknown
        _w, mask, value = lit
        r = (ev(a[0]) & mask) == value
        return int(r if o == "==?" else not r)
    if o == "?:":
        return ev(a[1], ctx) if ev(a[0]) else ev(a[2], ctx)
    if o == "inside":
        x = ev(a[0])
        return int(any(x == ev(y) for y in a[1:]))
    if o in ("&r", "|r", "^r", "~&r", "~|r", "~^r"):
        w = width_of(a[0], widths)
        if not w:
            raise _Unknown
        x = _mask(ev(a[0]), w)
        r = {"&": x == (1 << w) - 1, "|": x != 0, "^": bin(x).count("1") % 2 == 1}[o.lstrip("~")[0]]
        return int(not r if o.startswith("~") else r)
    if o == "~":
        w = ctx or width_of(a[0], widths)
        if not w:
            raise _Unknown
        return _mask(~ev(a[0], w), w)
    if o == "u-":
        w = ctx or width_of(a[0], widths)
        return _mask(-ev(a[0], w), w)
    if o == "u+":
        return ev(a[0], ctx)
    if o == "[]":
        base, idx = a
        return (ev(base) >> ev(idx)) & 1
    if o == "[:]":
        base, hi, lo = a
        h, lo_ = ev(hi), ev(lo)
        h, lo_ = max(h, lo_), min(h, lo_)
        return (ev(base) >> lo_) & ((1 << (h - lo_ + 1)) - 1)
    if o == "{}":
        out = 0
        for x in a:
            w = width_of(x, widths)
            if not w:
                raise _Unknown
            out = (out << w) | _mask(ev(x), w)
        return out
    if o == "{n{}}":
        n = ev(a[0])
        w = width_of(a[1], widths)
        if not w:
            raise _Unknown
        v, out = _mask(ev(a[1]), w), 0
        for _ in range(n):
            out = (out << w) | v
        return out
    if len(a) == 2:
        if o in ("==", "!=", "===", "!==", "<", "<=", ">", ">="):
            ws = [width_of(x, widths) for x in a]
            c = max(w for w in ws if w) if any(ws) else None
            x, y = ev(a[0], c), ev(a[1], c)
            return int({"==": x == y, "===": x == y, "!=": x != y, "!==": x != y, "<": x < y,
                        "<=": x <= y, ">": x > y, ">=": x >= y}[o])
        if o in ("+", "-", "*", "&", "|", "^", "~^", "<<", ">>", "<<<", ">>>", "/", "%"):
            ws = [width_of(x, widths) for x in a] + [ctx]
            c = max(w for w in ws if w) if any(ws) else None
            x, y = ev(a[0], c), ev(a[1], c)
            if o in ("/", "%") and y == 0:
                raise _Unknown
            r = {"+": x + y, "-": x - y, "*": x * y, "&": x & y, "|": x | y, "^": x ^ y,
                 "~^": ~(x ^ y), "<<": x << y, ">>": x >> y, "<<<": x << y, ">>>": x >> y,
                 "/": x // y if y else 0, "%": x % y if y else 0}[o]
            return _mask(r, c)
    raise _Unknown


@dataclass
class Branch:
    inst: str
    text: str
    guard: dict
    proc: int | None
    location: str
    lhs: list[str]
    default: bool
    refs: list[str]                  # node ids the guard reads
    hits: int = 0
    evaluated: int = 0
    status: str = ""                 # covered | gap | unobservable | unreachable (proved by N3 exploration)


@dataclass
class Coverage:
    branches: list[Branch] = field(default_factory=list)
    states: list[dict] = field(default_factory=list)       # per FSM
    toggles: dict[str, dict] = field(default_factory=dict)  # node -> {"width", "rise", "fall"}

    def summary(self) -> dict:
        obs = [b for b in self.branches if b.status not in ("unobservable", "unreachable")]
        st_total = sum(len(f["states"]) - len(f.get("unreachable", [])) for f in self.states)
        st_seen = sum(len(f["visited"]) for f in self.states)
        tr_total = sum(len(f["transitions"]) - len(f.get("unreachable_transitions", [])) for f in self.states)
        tr_seen = sum(len(f["transitions_seen"]) for f in self.states)
        bits = sum(t["width"] for t in self.toggles.values())
        toggled = sum(bin(t["rise"] & t["fall"]).count("1") for t in self.toggles.values())
        ratio = lambda a, b: round(a / b, 4) if b else None  # noqa: E731
        return {
            "branch": {"total": len(self.branches), "observable": len(obs),
                       "unreachable": sum(1 for b in self.branches if b.status == "unreachable"),
                       "covered": sum(1 for b in obs if b.status == "covered"),
                       "ratio": ratio(sum(1 for b in obs if b.status == "covered"), len(obs))},
            "state": {"total": st_total, "visited": st_seen, "ratio": ratio(st_seen, st_total),
                      "transitions": tr_total, "transitions_seen": tr_seen,
                      "transition_ratio": ratio(tr_seen, tr_total)},
            "toggle": {"bits": bits, "toggled": toggled, "ratio": ratio(toggled, bits)},
        }

    def gaps(self) -> dict:
        return {
            "branches": [{"inst": b.inst, "branch": b.text, "lhs": b.lhs, "location": b.location,
                          "default": b.default} for b in self.branches if b.status == "gap"],
            "unreachable_branches": [{"inst": b.inst, "branch": b.text, "location": b.location}
                                     for b in self.branches if b.status == "unreachable"],
            "unobservable_branches": [{"inst": b.inst, "branch": b.text, "missing": b.refs}
                                      for b in self.branches if b.status == "unobservable"],
            "states": [{"inst": f["inst"], "state": f["state"], "value": v, "name": f["names"].get(v, str(v))}
                       for f in self.states for v in f["states"]
                       if v not in f["visited"] and v not in f.get("unreachable", [])],
            "unreachable_states": [{"inst": f["inst"], "state": f["state"], "value": v,
                                    "name": f["names"].get(v, str(v))}
                                   for f in self.states for v in f.get("unreachable", [])],
            "transitions": [{"inst": f["inst"], "state": f["state"], "from": a, "to": b}
                            for f in self.states for a, b in f["transitions"]
                            if [a, b] not in f["transitions_seen"] and [a, b] not in f.get("unreachable_transitions", [])],
            "toggles": [{"signal": n, "width": t["width"],
                         "never_rise": [i for i in range(t["width"]) if not t["rise"] >> i & 1],
                         "never_fall": [i for i in range(t["width"]) if not t["fall"] >> i & 1]}
                        for n, t in sorted(self.toggles.items()) if bin(t["rise"] & t["fall"]).count("1") < t["width"]],
        }


def branch_targets(instances, modeled: set[str], resets: dict[str, dict[str, str]]) -> list[Branch]:
    out = []
    for inst in instances:
        if inst.path not in modeled:
            continue
        rst = resets.get(inst.path, {})
        seen = set()
        for a in inst.assigns:
            if not a.cond_stack:
                continue
            k = json.dumps([{kk: vv for kk, vv in c.items() if kk not in ("text", "item_text")} for c in a.cond_stack],
                           sort_keys=True)
            if k in seen:
                continue
            seen.add(k)
            if any(c["kind"] == "if" and "const" in c["expr"] and bool(c["expr"]["const"]) != c["polarity"]
                   for c in a.cond_stack):
                continue  # constant-false: dead code, N2b reports it
            guard, in_reset = stack_guard(a.cond_stack, rst)
            if in_reset or guard is None:
                continue
            out.append(Branch(inst.path, path_text(a.cond_stack, rst), guard, a.proc, a.location, a.lhs,
                              any(c["kind"] == "case" and c.get("default") for c in a.cond_stack),
                              sorted(f"{inst.path}.{r}" for r in tree_refs(guard))))
    return out


def compute(cases: list[Case], branches: list[Branch], fsms, widths: dict[str, dict[str, int]],
            toggle_nodes: dict[str, int]) -> Coverage:
    """widths: inst path -> {name: width}; toggle_nodes: node id -> width."""
    cov = Coverage()
    for b in branches:
        b.hits = b.evaluated = 0
    present = set().union(*(c.signals.keys() for c in cases)) if cases else set()
    for b in branches:
        if not set(b.refs) <= present:
            b.status = "unobservable"
            continue
        w = widths.get(b.inst, {})
        pre = b.inst + "."
        for c in cases:
            cols = {r[len(pre):]: c.signals[r] for r in b.refs}
            for i in range(c.cycles):
                env = {k: v[i] for k, v in cols.items()}
                try:
                    v = evaluate(b.guard, env, w)
                except _Unknown:
                    continue
                b.evaluated += 1
                if v:
                    b.hits += 1
        b.status = "covered" if b.hits else ("gap" if b.evaluated else "unobservable")
        cov.branches.append(b)

    for f in fsms:
        node = f"{f.inst}.{f.state}"
        states = [s["value"] for s in f.states]
        trans = sorted({(t["from"], t["to"]) for t in f.transitions if not t.get("implicit") and t["from"] != t["to"]})
        visited, seen_tr, other = set(), set(), set()
        for c in cases:
            col = c.signals.get(node)
            if col is None:
                continue
            for i, v in enumerate(col):
                if v is None:
                    continue
                (visited if v in states else other).add(v)
                if i and col[i - 1] is not None and col[i - 1] != v:
                    seen_tr.add((col[i - 1], v))
        cov.states.append({"inst": f.inst, "state": f.state, "states": states,
                           "names": {s["value"]: s["name"] for s in f.states},
                           "visited": sorted(visited), "other_values": sorted(other),
                           "transitions": [list(t) for t in trans],
                           "transitions_seen": sorted([list(t) for t in seen_tr if t in set(trans)]),
                           "observed": any(node in c.signals for c in cases)})

    for node, wd in sorted(toggle_nodes.items()):
        if node not in present:
            continue
        wd = max(1, wd)
        rise = fall = 0
        for c in cases:
            col = c.signals.get(node, [])
            for i in range(1, len(col)):
                a, b = col[i - 1], col[i]
                if a is None or b is None:
                    continue
                rise |= ~a & b
                fall |= a & ~b
        m = (1 << wd) - 1
        cov.toggles[node] = {"width": wd, "rise": rise & m, "fall": fall & m}
    return cov
