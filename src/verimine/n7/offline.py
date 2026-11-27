"""N7 steps 1-2: the IR checked offline on S_val, and the overfit candidates dropped.

S_val is the validation share of N3's cases (the ones N4 did not mine). Every candidate is
evaluated there by the IR, with the same semantics as the S_mine check in N4:

  violated on a legal trace (user / constrained) and mined from traces (path B)
      trace_overfit: the pattern only fit the training traces. Eliminated: not proven, status
      REVIEW_REQUIRED, the violating case kept in the report.
  violated on a legal trace, not trace-derived (templates, LLM)
      the design violates it on a legal trace -> a real counterexample: FAILED without formal.
      (N4 already dropped candidates violated on S_mine; one that only fails on S_val is still
      a counterexample of the design.)
  violated only on unknown-legality traces
      the inputs may have been illegal: kept, WEAK_EVIDENCE unless the proof says better.
  not exercised anywhere
      no simulation evidence: the formal result decides; no proof -> WEAK_EVIDENCE.

Also step 2: for every assertion with an antecedent, its cover (`<name>_c`) is looked at on
S_val: whether the antecedent is exercised there at all.
"""
from __future__ import annotations

from verimine.n3.cover import _Unknown, evaluate
from verimine.n3a.cands import series
from verimine.n1.assign import tree_refs
from verimine.n4 import pool


def _exercised(prop, cases, widths: dict[str, int]) -> int:
    """Rows where the property's antecedent holds on S_val (or the invariant holds), for covers
    and invariants alike."""
    pre = prop.inst + "."
    out = 0
    for c in cases:
        cols = {k[len(pre):]: v for k, v in c.signals.items() if k.startswith(pre)}
        need = tree_refs(prop.guard) if prop.guard else tree_refs(prop.expr)
        if not need <= set(cols):
            continue
        vals = series(prop.guard or prop.expr, cols, c.cycles, widths)
        out += sum(1 for v in vals if v)
    return out


def check(cands, cases, widths_of) -> dict[str, dict]:
    """candidate key -> {"status", "support", "cases", "violations", "first", "exercised"}."""
    out = {}
    for c in cands:
        t = pool.check(c, cases, widths_of(c.scope)) if cases else {}
        legal = pool.legal_violations(t)
        unknown = sum(v for k, v in t.get("violations", {}).items() if k not in ("user", "constrained"))
        if legal:
            status = "fail"
        elif unknown:
            status = "warn"
        elif t.get("support"):
            status = "pass"
        else:
            status = "no_data"
        out[c.key] = {"status": status, "support": t.get("support", 0), "cases": t.get("cases", 0),
                      "violations": t.get("violations", {}), "first": t.get("first"),
                      "legal": legal, "unknown": unknown}
    return out


def overfit(cand, sim: dict) -> bool:
    """A trace-derived candidate refuted on a legal validation trace is overfit."""
    return sim["legal"] > 0 and "B" in cand.sources


def exercised(prop, cases, widths: dict[str, int]) -> int:
    if not cases:
        return 0
    return _exercised(prop, cases, widths)
