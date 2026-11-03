"""N2 steps 3-5: state registers and their transition graphs; counter, shift-register and memory
candidates. All structural: every rule looks at N1's assignment trees, never at names.

Rules (see docs/N2.md for the reasoning):
  counter         register r with r <= r +/- terms, every other term a constant or a 1-bit value
  shift register  register r with r <= {r[slice], x} / {x, r[slice]} or r << k / r >> k
  memory          a signal with unpacked dimensions (written through mem[addr] <= ...)
  state register  register s, not one of the above, whose every next value is a constant, a hold
                  (s itself) or a combinational next-state signal ns whose every value is a
                  constant or a hold; s is read as a case subject or compared with a constant;
                  at least two distinct values, and width >= 2 unless s is a case subject
"""
from __future__ import annotations

from dataclasses import dataclass, field

from verimine.n1.elaborate import InstanceInfo
from verimine.n2 import exprs as X
from verimine.n2.vdg import is_clocked

HIGH, MEDIUM, LOW = "高", "中", "低"


@dataclass
class Pattern:
    kind: str                 # counter | shift_register | memory
    inst: str
    signal: str
    confidence: str
    evidence: list[str]
    detail: dict = field(default_factory=dict)


@dataclass
class Fsm:
    inst: str
    state: str
    next_state: str | None
    width: int
    states: list[dict]        # [{"value", "name"}]
    reset_state: int | None
    transitions: list[dict]   # [{"from", "to", "cond"}], from/to are state values
    reachable: list[int]
    confidence: str
    evidence: list[str]


# --------------------------------------------------------------------------- #
# Per-instance view
# --------------------------------------------------------------------------- #

class InstView:
    def __init__(self, inst: InstanceInfo, resets: dict[str, str], child_exprs: list[dict] = ()):
        self.inst = inst
        self.resets = resets
        self.widths = {p.name: p.width for p in inst.ports} | {s.name: s.width for s in inst.signals}
        self.arrays = {s.name for s in inst.signals if s.array_dims}
        self.writes: dict[str, list] = {}
        for a in inst.assigns:
            for lhs in a.lhs:
                self.writes.setdefault(lhs, []).append(a)
        self.registers = sorted(n for n, ws in self.writes.items() if any(is_clocked(inst, a.proc) for a in ws))
        # Expression trees anywhere in the instance: right-hand sides, conditions, port connections
        self.exprs: list[dict] = []
        for a in inst.assigns:
            self.exprs.append(a.rhs_tree)
            self.exprs.extend(X.cond_exprs(a.cond_stack))
        self.exprs.extend(child_exprs)
        self.case_subjects = {
            c["subject"]["ref"] for a in inst.assigns for c in a.cond_stack
            if c["kind"] == "case" and X.is_ref(c["subject"])
        }

    def clocked(self, a) -> bool:
        return is_clocked(self.inst, a.proc)

    def in_reset(self, a) -> bool:
        """The assignment sits on the reset-active branch of an if on a reset signal."""
        for c in a.cond_stack:
            if c["kind"] != "if":
                continue
            refs = X.tree_refs(c["expr"])
            if not refs or not refs <= set(self.resets):
                continue
            env = {r: (0 if self.resets[r] == "low" else 1) for r in refs}
            v = X.evaluate(c["expr"], env)
            if v is not None and bool(v) == c["polarity"]:
                return True
        return False

    def compared_consts(self, name: str) -> set[int]:
        out = set()
        for e in self.exprs:
            for sig, _op, val in X.compares(e):
                if sig == name:
                    out.add(val)
        for a in self.inst.assigns:
            for c in a.cond_stack:
                if c["kind"] == "case" and X.is_ref(c["subject"], name):
                    out.update(X.const_of(x) for x in c.get("items", []) if X.const_of(x) is not None)
        return out


# --------------------------------------------------------------------------- #
# Counters, shift registers, memories
# --------------------------------------------------------------------------- #

def _sum_terms(t: dict, sign: int = 1) -> list[tuple[int, dict]]:
    op = t.get("op")
    if op in ("+", "-") and len(t.get("args", [])) == 2:
        a, b = t["args"]
        return _sum_terms(a, sign) + _sum_terms(b, sign if op == "+" else -sign)
    return [(sign, t)]


def _counter_step(r: str, t: dict, v: InstView) -> set[str] | None:
    """Directions ("up"/"down") if t is r +/- small terms, else None."""
    terms = _sum_terms(t)
    selfs = [s for s, x in terms if X.is_ref(x, r)]
    if selfs != [1] or len(terms) < 2:
        return None
    dirs = set()
    for sign, x in terms:
        if X.is_ref(x, r):
            continue
        c = X.const_of(x)
        if c is not None:
            if c == 0:
                continue
            dirs.add("up" if (c > 0) == (sign > 0) else "down")
        elif X.one_bit(x, v.widths):
            dirs.add("up" if sign > 0 else "down")
        else:
            return None
    return dirs or None


def _shift_step(r: str, t: dict) -> tuple[str, list[str]] | None:
    """("left"|"right", serial inputs) if t shifts r."""
    op, args = t.get("op"), t.get("args", [])
    if op == "{}" and len(args) >= 2:
        self_slice = [i for i, a in enumerate(args)
                      if a.get("op") in ("[:]", "[]") and X.is_ref(a["args"][0], r)]
        if len(self_slice) == 1:
            others = sorted(X.tree_refs({"op": "{}", "args": [a for i, a in enumerate(args) if i != self_slice[0]]}))
            return ("left" if self_slice[0] == 0 else "right"), others
    if op in ("<<", ">>", "<<<", ">>>") and len(args) == 2 and X.is_ref(args[0], r) and X.const_of(args[1]):
        return ("left" if op.startswith("<") else "right"), []
    if op in ("|", "^") and len(args) == 2:
        for i in (0, 1):
            got = _shift_step(r, args[i])
            if got and got[1] == [] and args[1 - i].get("op") != "{}":
                return got[0], sorted(X.tree_refs(args[1 - i]))
    return None


def find_patterns(v: InstView) -> list[Pattern]:
    out: list[Pattern] = []
    path = v.inst.path
    for r in v.registers:
        if r in v.arrays:
            continue
        ws = [a for a in v.writes[r] if v.clocked(a)]
        dirs: set[str] = set()
        shift = None
        for a in ws:
            for leaf, _ in X.leaves(a.rhs_tree):
                d = _counter_step(r, leaf, v)
                if d:
                    dirs |= d
                s = _shift_step(r, leaf)
                if s:
                    shift = s
        if dirs:
            direction = "up_down" if dirs == {"up", "down"} else dirs.pop()
            resets = sorted({X.const_of(a.rhs_tree) for a in ws
                             if X.const_of(a.rhs_tree) is not None and v.in_reset(a)})
            loads = sorted({X.const_of(a.rhs_tree) for a in ws
                            if X.const_of(a.rhs_tree) is not None and not v.in_reset(a)})
            out.append(Pattern("counter", path, r, HIGH, [f"{r} <= {r} ± 常量或 1 位量"], {
                "direction": direction, "width": v.widths.get(r, 0),
                "reset_values": resets, "load_values": loads,
                "compared_with": sorted(v.compared_consts(r)),
            }))
        elif shift:
            out.append(Pattern("shift_register", path, r, HIGH, [f"{r} 由自身移位后拼接得到"], {
                "direction": shift[0], "serial_inputs": shift[1], "width": v.widths.get(r, 0),
            }))
    for s in v.inst.signals:
        if not s.array_dims:
            continue
        writes = v.writes.get(s.name, [])
        addr = sorted({x for a in writes if a.lhs_tree.get("op") == "[]" for x in X.tree_refs(a.lhs_tree["args"][1])})
        data = sorted({x for a in writes for x in a.rhs_refs} - set(addr))
        reads = sorted({x for e in v.exprs for x in _index_reads(e, s.name)})
        conf = HIGH if writes else MEDIUM
        ev = ["信号带非压缩维度 " + s.array_dims] + (["按地址写入"] if addr else [])
        out.append(Pattern("memory", path, s.name, conf, ev, {
            "dims": s.array_dims, "width": s.width, "write_addr": addr, "write_data": data,
            "read_addr": reads, "clocked_write": any(v.clocked(a) for a in writes),
        }))
    return out


def _index_reads(t: dict, mem: str) -> set[str]:
    out = set()
    if t.get("op") == "[]" and X.is_ref(t["args"][0], mem):
        out |= X.tree_refs(t["args"][1])
    for a in t.get("args", []):
        out |= _index_reads(a, mem)
    return out


# --------------------------------------------------------------------------- #
# State registers and transitions
# --------------------------------------------------------------------------- #

def _values(v: InstView, target: str, hold: set[str]) -> list[tuple] | None:
    """[(value | "hold", assignment, extra_conds)] for every way target is assigned, or None if
    some assignment is neither a constant nor a hold."""
    out = []
    for a in v.writes.get(target, []):
        if a.lhs_tree != {"ref": target}:
            return None
        for leaf, extra in X.leaves(a.rhs_tree):
            c = X.const_of(leaf)
            if c is not None:
                out.append((c, a, extra))
            elif X.is_ref(leaf) and leaf["ref"] in hold:
                out.append(("hold", a, extra))
            else:
                return None
    return out


def _from_states(stack: list[dict], s: str, all_states: set[int]) -> set[int] | None:
    """States the assignment is reached from, read off the condition stack. None = any state."""
    cur: set[int] | None = None

    def narrow(x: set[int]):
        nonlocal cur
        cur = x if cur is None else cur & x

    for c in stack:
        if c["kind"] == "case" and X.is_ref(c["subject"], s):
            if c.get("default"):
                others = {X.const_of(x) for x in c.get("others", [])}
                narrow(all_states - others)
            else:
                narrow({X.const_of(x) for x in c.get("items", []) if X.const_of(x) is not None})
        elif c["kind"] == "if":
            e = c["expr"]
            terms = e["args"] if e.get("op") == "&&" and c["polarity"] else [e]
            for t in terms:
                for sig, op, val in X.compares(t):
                    if sig != s or t.get("op") not in ("==", "!=", "===", "!=="):
                        continue
                    positive = (op in ("==", "===")) == (c["polarity"] if t is e else True)
                    narrow({val} if positive else all_states - {val})
    return cur


def _cond_text(stack: list[dict], s: str, extra: list[dict], resets: set[str]) -> list[str]:
    """Readable guard of a transition: conditions on the state itself (already expressed by
    `from`) and on resets (the reset branch is not a transition) are left out."""
    parts = []
    for c in stack + [{"kind": "if", **x} for x in extra]:
        if c["kind"] == "case":
            if X.is_ref(c["subject"], s):
                continue
            parts.append(f"{X.text(c['subject'])} 为 {c.get('item_text', '')}")
        elif c["kind"] == "if":
            refs = X.tree_refs(c["expr"])
            if refs == {s} or (refs and refs <= resets):
                continue
            t = X.text(c["expr"])
            parts.append(t if c["polarity"] else f"!{t}" if t.startswith("(") else f"!({t})")
    return parts


def find_fsms(v: InstView, taken: set[str]) -> list[Fsm]:
    out = []
    path = v.inst.path
    for s in v.registers:
        if s in taken or s in v.arrays or s in v.resets:
            continue
        ws = [a for a in v.writes[s] if v.clocked(a)]
        if len(ws) != len(v.writes[s]):
            continue
        # direct constants / hold, or s <= ns with ns combinational
        ns = None
        sources = {leaf["ref"] for a in ws for leaf, _ in X.leaves(a.rhs_tree) if X.is_ref(leaf)} - {s}
        if len(sources) == 1:
            cand = next(iter(sources))
            cw = v.writes.get(cand, [])
            if cw and not any(v.clocked(a) for a in cw):
                ns = cand
        direct = _values(v, s, {s} | ({ns} if ns else set()))
        if direct is None:
            continue
        nvals = _values(v, ns, {s, ns}) if ns else []
        if ns and nvals is None:
            continue
        consts = {x for x, _, _ in direct + (nvals or []) if x != "hold"}
        compared = v.compared_consts(s)
        in_case = s in v.case_subjects
        if not (in_case or compared) or len(consts | compared) < 2:
            continue
        if v.widths.get(s, 0) < 2 and not in_case:
            continue
        states = sorted(consts | compared)
        names: dict[int, str] = {}
        for vals in (direct, nvals or []):
            for x, a, _ in vals:
                if x != "hold":
                    leaf_text = next((lf.get("text", "") for lf, _ in X.leaves(a.rhs_tree)
                                      if X.const_of(lf) == x), "")
                    if X.is_name(leaf_text):
                        names.setdefault(x, leaf_text)
        for a in v.inst.assigns:
            for c in a.cond_stack:
                if c["kind"] == "case" and X.is_ref(c["subject"], s):
                    for it in c.get("items", []):
                        if X.const_of(it) is not None and X.is_name(it.get("text", "")):
                            names.setdefault(it["const"], it["text"])
        all_states = set(states)
        reset_state = next((x for x, a, _ in direct if x != "hold" and v.in_reset(a)), None)

        # Candidate transitions in source order: (order, proc, from-states, to, guard).
        cands = []
        order = {id(a): i for i, a in enumerate(v.inst.assigns)}
        for vals in ([direct] if not ns else [[x for x in direct if x[0] != "hold" and not v.in_reset(x[1])], nvals]):
            for x, a, extra in vals:
                if v.in_reset(a):
                    continue
                frm = _from_states(a.cond_stack, s, all_states)
                frm = all_states if frm is None else frm
                cond = tuple(_cond_text(a.cond_stack, s, extra, set(v.resets)))
                cands.append((order[id(a)], a.proc, frozenset(frm), x, cond))
        # A later assignment in the same block with no guard (beyond the state itself) overrides
        # an earlier one for those states: `nxt = state;` followed by `REQ: nxt = WAIT;` means
        # REQ never holds.
        transitions: set[tuple] = set()
        for o, proc, frm, x, cond in cands:
            for f in sorted(frm):
                if any(o2 > o and p2 == proc and f in frm2 and not cond2
                       for o2, p2, frm2, _, cond2 in cands):
                    continue
                if x == "hold" and not cond:
                    cond = ("其余情况保持",)  # the default `nxt = state;` before a case
                transitions.add((f, f if x == "hold" else x, cond, False))
        if not ns:
            # A register keeps its value on paths that do not assign it. States with an
            # unguarded assignment never take that path; for the rest, whether it exists needs
            # the conditions solved, so the self-loop is listed as implicit for N2b/N7 to confirm.
            looped = {f for f, t, _, _ in transitions if f == t}
            always = {f for _, _, frm, _, cond in cands if not cond for f in frm}
            for f in sorted(all_states - looped - always):
                transitions.add((f, f, ("无赋值时保持",), True))
        trans = [{"from": f, "to": t, "cond": list(c), "implicit": imp}
                 for f, t, c, imp in sorted(transitions, key=lambda z: (z[0], z[1], z[2]))]
        reach = _reachable(reset_state, trans, all_states)
        ev = []
        ev.append("作为 case 的选择表达式" if in_case else "与常量比较")
        ev.append(f"下一状态来自组合信号 {ns}" if ns else "只被赋常量或保持")
        out.append(Fsm(path, s, ns, v.widths.get(s, 0),
                       [{"value": x, "name": names.get(x, str(x))} for x in states],
                       reset_state, trans, reach, HIGH if in_case else MEDIUM, ev))
    return out


def _reachable(start: int | None, trans: list[dict], states: set[int]) -> list[int]:
    if start is None:
        return sorted(states)
    seen, todo = {start}, [start]
    while todo:
        cur = todo.pop()
        for t in trans:
            if t["from"] == cur and t["to"] not in seen:
                seen.add(t["to"])
                todo.append(t["to"])
    return sorted(seen)
