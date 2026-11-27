"""N7 step 4: sim_status, formal_status, cover_status -> one status per assertion.

The statuses are exactly the spec's seven: ENV_ERROR, FAILED, VACUOUS, PROVEN, BOUNDED,
WEAK_EVIDENCE, REVIEW_REQUIRED. The rules, most decisive first:

  ENV_ERROR        the verification version did not compile (N6) or the engine could not read
                   the design with it
  FAILED           a counterexample: the engine's, or a legal S_val trace of a candidate that
                   was not mined from traces
  VACUOUS          the antecedent's cover was proved unreachable: the assertion never fires
  REVIEW_REQUIRED  the candidate rests on traces of unknown legality (N3: never auto-accepted),
                   or a proof run timed out (resource_limit)
  PROVEN           proved for all depths, antecedent reached (or none)
  BOUNDED          no counterexample within the depth, induction inconclusive
  WEAK_EVIDENCE    only simulation evidence, or the antecedent was never reached and could not
                   be proved unreachable either (vacuity cannot be excluded)
"""
from __future__ import annotations

STATUSES = ("ENV_ERROR", "FAILED", "VACUOUS", "PROVEN", "BOUNDED", "WEAK_EVIDENCE", "REVIEW_REQUIRED")


def merge(*, sim: str, formal: str, cover: str, review_required: bool) -> str:
    """sim: pass | fail | warn | no_data | not_run (see offline.check)
    formal: PROVEN | BOUNDED | FAILED | RESOURCE_LIMIT | ENV_ERROR | NOT_RUN | SKIPPED
    cover: REACHED | UNREACHED | UNREACHABLE | NOT_RUN"""
    if formal in ("ENV_ERROR", "SKIPPED"):
        return "ENV_ERROR"
    if formal == "FAILED" or sim == "fail":
        return "FAILED"
    if cover == "UNREACHABLE":
        return "VACUOUS"
    if review_required or formal == "RESOURCE_LIMIT":
        return "REVIEW_REQUIRED"
    if formal == "PROVEN":
        return "PROVEN" if cover in ("REACHED", "NOT_RUN") else "WEAK_EVIDENCE"
    if formal == "BOUNDED":
        return "BOUNDED" if cover in ("REACHED", "NOT_RUN") else "WEAK_EVIDENCE"
    return "WEAK_EVIDENCE"
