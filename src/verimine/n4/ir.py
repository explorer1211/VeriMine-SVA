"""N4 intermediate representation (spec 合并候选池 1): every candidate, whatever path it came
from, is antecedent, consequent, delay, type, source and the features (signals) it uses.

  ante |-> cons     next = False
  ante |=> cons     next = True
  cons              ante = None (an invariant)

Trees are N1 expression trees with `$past` nodes (`k` = depth), as in N2b, so N5 can render
them both ways and N7 can check them offline. Signals are relative to `scope` (an instance path).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from verimine.n1.assign import tree_refs
from verimine.n2b.props import Prop
from verimine.n3a.cands import text_of

SOURCES = {"A": "规则和模板", "B": "决策树挖掘", "C": "LLM"}
# trace legality of the evidence behind a candidate, most trusted first (spec: 轨迹合法性)
LEGALITY = ("none", "user", "constrained", "unknown")
STATUS = ("POOL", "DEFERRED", "DROPPED")


@dataclass
class Cand:
    type: str                        # template name, "tree" (path B) or an LLM kind
    scope: str
    module: str
    ante: dict | None
    cons: dict
    next: bool = False
    sources: list[str] = field(default_factory=list)       # A / B / C
    legality: str = "none"          # none: no trace behind it (templates, LLM)
    assumes: list[str] = field(default_factory=list)       # names of the config assumes it depends on
    level: str = "port"                                    # port | internal (v5: port level by default)
    clock: dict | None = None
    resets: dict = field(default_factory=dict)             # disable iff: name -> low | high
    evidence: list[str] = field(default_factory=list)
    detail: dict = field(default_factory=dict)
    trace: dict = field(default_factory=dict)              # offline check on S_mine
    gap: bool = False                                      # touches a coverage gap
    status: str = "POOL"
    reason: str = ""
    id: str = ""

    @property
    def text(self) -> str:
        return text_of(self.ante, self.cons, self.next)

    @property
    def features(self) -> list[str]:
        out = tree_refs(self.cons)
        if self.ante:
            tree_refs(self.ante, out)
        return sorted(out)

    @property
    def review_required(self) -> bool:
        """Spec N3 item 10: candidates from unknown traces are down-weighted and go to
        REVIEW_REQUIRED, never to automatic acceptance."""
        return self.legality == "unknown"

    @property
    def key(self) -> str:
        return self.scope + "|" + json.dumps([canon(self.ante) if self.ante else None, self.next,
                                              canon(self.cons)], sort_keys=True, separators=(",", ":"))

    def ir(self) -> dict:
        return {"ante": self.ante, "cons": self.cons, "delay": 1 if self.next else 0, "type": self.type,
                "source": self.sources, "features": self.features}


# --------------------------------------------------------------------------- #
# Normalization (spec 合并候选池 2: IR normalized, then syntactic duplicates removed)
# --------------------------------------------------------------------------- #

_COMMUTATIVE = {"&&", "||", "==", "!=", "&", "|", "^", "+", "*"}
_FLAT = {"&&", "||", "&", "|", "^", "+", "*"}


def canon(t: dict) -> dict:
    """Same meaning, same tree: nested &&/||/&/| flattened, operands of commutative operators
    sorted, double negation removed, const text dropped."""
    if "ref" in t:
        return {"ref": t["ref"]}
    if "const" in t:
        return {"const": t["const"]}
    o = t.get("op")
    args = [canon(a) for a in t.get("args", [])]
    if o == "!" and args and args[0].get("op") == "!":
        return args[0]["args"][0]
    if o in _FLAT:
        flat = []
        for a in args:
            flat += a["args"] if a.get("op") == o else [a]
        args = flat
    if o in _COMMUTATIVE:
        args = sorted(args, key=lambda a: json.dumps(a, sort_keys=True))
        if o in ("&&", "||"):
            uniq = []
            for a in args:
                if a not in uniq:
                    uniq.append(a)
            args = uniq
            if len(args) == 1:
                return args[0]
    out = {"op": o, "args": args}
    if o == "$past" and t.get("k", 1) != 1:
        out["k"] = t["k"]
    return out


def to_prop(c: Cand, pid: str = "") -> Prop:
    """The candidate as an N2b property, for the engine (N5/N7 take over from here)."""
    p = Prop("assert", f"n4.{c.type}", c.scope, c.module, c.cons, guard=c.ante, next=c.next, clock=c.clock,
             resets=dict(c.resets), message=c.text, key=c.key)
    p.id = pid or c.id
    return p
