"""N4 candidate pool (spec 合并候选池): one IR for all three paths, normalized and deduplicated,
checked on S_mine, tagged with the assumptions it depends on, and cut to a budget driven by the
coverage gaps.

  dedupe      same canonical IR (ir.canon) in the same scope = one candidate; sources merged
  S_mine      A / C candidates are evaluated on S_mine. A violation on a trace with legal inputs
              (user / constrained) is a real counterexample: DROPPED. A violation only on
              unknown-legality traces is noted, not acted on (the inputs may have been illegal)
  assumes     a candidate depends on the config assumes whose signals are top inputs in the
              cone of influence of its own signals
  budget      candidates are grouped by region (scope + consequent signals). Each region gets
              `n4_quota` slots; a region that touches an N3 coverage gap gets `n4_gap_boost`
              times as many (N7's gap list takes over once N7 exists). Over quota -> DEFERRED.
              Importance (PageRank) only breaks ties, and only with `n4_use_pagerank`
"""
from __future__ import annotations

import re

from verimine.n1.assign import tree_refs
from verimine.n3a.cands import series
from verimine.n4.ir import LEGALITY, Cand

_PRIORITY = {"A": 0, "B": 1, "C": 2}
_ID = re.compile(r"\b[A-Za-z_][A-Za-z0-9_$]*\b")


def dedupe(cands: list[Cand]) -> list[Cand]:
    by_key: dict[str, Cand] = {}
    for c in cands:
        have = by_key.get(c.key)
        if have is None:
            by_key[c.key] = c
            continue
        have.sources = sorted(set(have.sources) | set(c.sources), key=_PRIORITY.get)
        have.evidence += [e for e in c.evidence if e not in have.evidence]
        if LEGALITY.index(c.legality) < LEGALITY.index(have.legality):
            have.legality = c.legality
        have.detail.setdefault("merged", []).append(c.type)
    return list(by_key.values())


def check(c: Cand, cases, widths: dict[str, int]) -> dict:
    """Offline check on traces: support (antecedent held), violations per legality."""
    pre = c.scope + "."
    need = set(c.features)
    out = {"support": 0, "violations": {}, "first": None, "cases": 0}
    for case in cases:
        cols = {k[len(pre):]: v for k, v in case.signals.items() if k.startswith(pre)}
        if not need <= set(cols):
            continue
        n = case.cycles
        act = _active_rows(c, cols, n)
        ante = series(c.ante, cols, n, widths) if c.ante else [1] * n
        cons = series(c.cons, cols, n, widths)
        hit = 0
        for i in range(n):
            j = i + 1 if c.next else i
            if j >= n or not ante[i] or cons[j] is None or not act[i] or not act[j]:
                continue
            hit += 1
            if not cons[j]:
                out["violations"][case.legality] = out["violations"].get(case.legality, 0) + 1
                if out["first"] is None:
                    out["first"] = {"case": case.id, "row": j, "legality": case.legality}
        out["support"] += hit
        out["cases"] += bool(hit)
    return out


def _active_rows(c: Cand, cols: dict, n: int) -> list[bool]:
    """Rows where `disable iff` does not hold (reset inactive)."""
    act = [True] * n
    for r, lv in c.resets.items():
        col = cols.get(r)
        if col is None:
            continue
        for i, v in enumerate(col):
            if v is None or v == (0 if lv == "low" else 1):
                act[i] = False
    return act


def legal_violations(t: dict) -> int:
    return sum(v for k, v in t.get("violations", {}).items() if k in ("user", "constrained"))


# --------------------------------------------------------------------------- #

def cone_inputs(g, nodes: list[str]) -> set[str]:
    """Top-level input ports that can influence any of the nodes."""
    preds = g.preds()
    seen, todo = set(nodes), list(nodes)
    while todo:
        n = todo.pop()
        for e in preds.get(n, []):
            if e.src not in seen:
                seen.add(e.src)
                todo.append(e.src)
    return {n for n in seen if g.nodes.get(n) and g.nodes[n].top_port and g.nodes[n].kind == "input"}


def depends(c: Cand, g, top: str, assumes: list[dict]) -> list[str]:
    if not assumes or c.type == "reset_value":
        return []  # under reset the inputs do not matter
    inputs = {n[len(top) + 1:] for n in cone_inputs(g, [f"{c.scope}.{f}" for f in c.features])}
    out = []
    for a in assumes:
        names = set(_ID.findall(re.sub(r"\$\w+|\d+'[sS]?[bBoOdDhH][0-9a-fA-FxXzZ_?]+", " ", a["expr"])))
        if names & inputs:
            out.append(a["name"])
    return sorted(out)


# --------------------------------------------------------------------------- #

def gap_signals(coverage) -> dict[str, set[str]]:
    """scope -> signals in an uncovered region (N3): branch targets and condition signals of
    branches never taken, FSM state registers with unvisited states or transitions."""
    out: dict[str, set[str]] = {}
    for b in coverage.branches:
        if b.status == "gap":
            s = out.setdefault(b.inst, set())
            s.update(b.lhs)
            s.update(r.rsplit(".", 1)[-1] for r in b.refs)
    for f in coverage.states:
        if set(f["states"]) - set(f["visited"]) - set(f.get("unreachable", [])) or \
                any(t not in f["transitions_seen"] and t not in f.get("unreachable_transitions", [])
                    for t in f["transitions"]):
            out.setdefault(f["inst"], set()).add(f["state"])
    return out


def region(c: Cand) -> str:
    return c.scope + ":" + ",".join(sorted(tree_refs(c.cons)))


def budget(cands: list[Cand], gaps: dict[str, set[str]], params: dict, importance: dict[str, float] | None) -> dict:
    quota, boost = int(params["n4_quota"]), int(params["n4_gap_boost"])
    total = int(params["n4_max_candidates"])
    for c in cands:
        c.gap = bool(set(c.features) & gaps.get(c.scope, set()))

    def score(c: Cand):
        imp = -max((importance or {}).get(f"{c.scope}.{f}", 0.0) for f in c.features) if importance else 0.0
        return (c.review_required, min(_PRIORITY[s] for s in c.sources), LEGALITY.index(c.legality),
                -c.trace.get("support", 0), imp, len(c.text), c.key)

    live = sorted((c for c in cands if c.status == "POOL"), key=score)
    regions: dict[str, list[Cand]] = {}
    for c in live:
        regions.setdefault(region(c), []).append(c)
    kept = []
    for r, cs in sorted(regions.items()):
        q = quota * (boost if any(c.gap for c in cs) else 1)
        for i, c in enumerate(cs):
            if i < q:
                kept.append(c)
            else:
                c.status, c.reason = "DEFERRED", f"区域 {r} 超出配额 {q}"
    kept.sort(key=lambda c: (not c.gap, score(c)))
    for c in kept[total:]:
        c.status, c.reason = "DEFERRED", f"超出候选总数上限 {total}"
    return {"regions": len(regions), "gap_regions": sum(1 for cs in regions.values() if any(c.gap for c in cs)),
            "quota": quota, "gap_boost": boost, "max": total}
