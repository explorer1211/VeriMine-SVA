"""N4 path B: decision-tree mining (GoldMine style). FTO-sensitive (US9021409B2): off by default,
an experimental feature until the FTO review is done (`run.params.n4_path_b: true`).

  1  one data set per target: every 1-bit output port and register, and every state of every
     N2 state machine (`s == v`), sampled on S_mine
  2  features: the signals in the target's COI (N2), 1-bit signals and FSM state predicates,
     unrolled over delays 0..k (a register target only gets delays >= 1: it is decided by the
     previous cycle). The target itself enters only with delay >= 1
  3  CART (Gini), depth- and leaf-size-limited, on the rows where every feature is known
  4  every pure leaf with enough support becomes ante |-> target == value, the path conditions
     being the antecedent; when every condition is from an earlier cycle the whole thing moves
     one cycle: $past(a) && $past(b) |-> t  ==  a && b |=> t
  5  at most `n4_tree_max_per_target` candidates per target
"""
from __future__ import annotations

from verimine.n2b.props import AND, EQ, NOT, const, ref
from verimine.n4.ir import Cand
from verimine.n4.templates import Scope


def _feature_tree(name: str, kind: str, val, d: int) -> dict:
    t = ref(name) if kind == "bit" else EQ(ref(name), const(val))
    for _ in range(d):
        t = {"op": "$past", "args": [t]}
    return _fold(t)


def _fold(t: dict) -> dict:
    """$past($past(x)) -> $past(x, 2)."""
    k, x = 0, t
    while x.get("op") == "$past":
        k, x = k + 1, x["args"][0]
    if k <= 1:
        return t
    return {"op": "$past", "args": [x], "k": k}


def _gini(pos: int, n: int) -> float:
    if not n:
        return 0.0
    p = pos / n
    return 2 * p * (1 - p)


def _grow(rows: list[int], X: dict, y: list[int], feats: list, depth: int, min_leaf: int, path: list, out: list):
    n = len(rows)
    pos = sum(y[i] for i in rows)
    if pos in (0, n):
        if n >= min_leaf:
            out.append((list(path), pos == n, n))
        return
    if depth == 0 or n < 2 * min_leaf:
        return
    best = None
    for f in feats:
        col = X[f]
        ones = [i for i in rows if col[i]]
        n1 = len(ones)
        if n1 < min_leaf or n - n1 < min_leaf:
            continue
        p1 = sum(y[i] for i in ones)
        score = (n1 * _gini(p1, n1) + (n - n1) * _gini(pos - p1, n - n1)) / n
        if best is None or score < best[0] - 1e-12:
            best = (score, f, ones)
    if best is None or best[0] >= _gini(pos, n) - 1e-12:
        return
    _, f, ones = best
    s1 = set(ones)
    rest = [f2 for f2 in feats if f2 != f]
    _grow(ones, X, y, rest, depth - 1, min_leaf, path + [(f, True)], out)
    _grow([i for i in rows if i not in s1], X, y, rest, depth - 1, min_leaf, path + [(f, False)], out)


def mine(n2, scopes: dict[str, Scope], cases, params: dict, widths_of) -> list[Cand]:
    if not cases:
        return []
    k = int(params["n4_tree_window"])
    depth, min_leaf = int(params["n4_tree_depth"]), int(params["n4_tree_min_leaf"])
    cap = int(params["n4_tree_max_per_target"])
    cois = {c.target: c for c in n2.cois}
    fsm_of = {(f.inst, f.state): f for f in n2.fsms}
    legal = sorted({c.legality for c in cases})
    out: list[Cand] = []

    targets = []   # (inst, name, kind, value, registered)
    for path, sc in sorted(scopes.items()):
        v = sc.view
        w = widths_of(path)
        regs = set(v.registers)
        for p in sc.inst.ports:
            if p.direction == "out" and w.get(p.name) == 1 and p.name not in sc.clocks:
                targets.append((path, p.name, "bit", None, p.name in regs))
        for r in sorted(regs - sc.ports):
            if w.get(r) == 1 and r not in v.arrays:
                targets.append((path, r, "bit", None, True))
        for (fi, s), f in sorted(fsm_of.items()):
            if fi == path:
                for st in f.states:
                    targets.append((path, s, "state", st["value"], True))

    for path, name, kind, val, registered in targets:
        sc = scopes[path]
        coi = cois.get(f"{path}.{name}")
        if coi is None:
            continue
        w = widths_of(path)
        special = sc.clocks | set(sc.resets)
        base = []
        for node in coi.nodes:
            if not node.startswith(path + ".") or "." in node[len(path) + 1:]:
                continue  # features stay inside the target's instance
            nm = node[len(path) + 1:]
            if nm in special:
                continue
            if w.get(nm) == 1:
                base.append((nm, "bit", None))
            elif (path, nm) in fsm_of:
                base += [(nm, "state", s["value"]) for s in fsm_of[(path, nm)].states]
        feats = []
        for nm, fk, fv in sorted(base, key=lambda b: (b[0], b[1], -1 if b[2] is None else b[2])):
            for d in range(0 if not registered else 1, k + 1):
                if d == 0 and nm == name:
                    continue
                feats.append((nm, fk, fv, d))
        if not feats:
            continue
        X: dict = {f: [] for f in feats}
        y: list[int] = []
        for c in cases:
            cols = c.signals
            tcol = cols.get(f"{path}.{name}")
            if tcol is None:
                continue
            fcols = {f: cols.get(f"{path}.{f[0]}") for f in feats}
            if any(v is None for v in fcols.values()):
                continue
            for i in range(k, c.cycles):
                tv = tcol[i]
                if tv is None:
                    continue
                row = {}
                for f, col in fcols.items():
                    x = col[i - f[3]]
                    if x is None:
                        break
                    row[f] = int(x == f[2]) if f[1] == "state" else int(bool(x))
                else:
                    for f in feats:
                        X[f].append(row[f])
                    y.append(int(tv == val) if kind == "state" else int(bool(tv)))
        if len(y) < 2 * min_leaf:
            continue
        leaves: list = []
        _grow(list(range(len(y))), X, y, feats, depth, min_leaf, [], leaves)
        leaves.sort(key=lambda lf: (-lf[2], len(lf[0])))
        target_t = ref(name) if kind == "bit" else EQ(ref(name), const(val))
        n_t = 0
        for conds, value, support in leaves:
            if not conds or n_t >= cap:
                continue
            shift = 1 if all(f[3] >= 1 for f, _ in conds) else 0
            ante = AND(*[(lambda t: t if pol else NOT(t))(_feature_tree(f[0], f[1], f[2], f[3] - shift))
                         for f, pol in conds])
            cons = target_t if value else NOT(target_t)
            c = sc.cand("tree", ante, cons, bool(shift),
                        [f"决策树：在 S_mine 的 {len(y)} 行上训练，此叶子覆盖 {support} 行，全部取 {int(value)}"],
                        target=f"{path}.{name}" + (f"=={val}" if kind == "state" else ""), support=support,
                        rows=len(y))
            c.sources = ["B"]
            c.legality = _worst(legal)
            out.append(c)
            n_t += 1
    return out


def _worst(legal: list[str]) -> str:
    from verimine.n4.ir import LEGALITY
    return max(legal, key=LEGALITY.index) if legal else "unknown"
