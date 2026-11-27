"""N7 steps 3, 5: the layered proof.

  L0   shallow BMC on every assertion and every cover
  L1   k-induction on the best `3*k` assertions that L0 left undecided (k = n7_l1_k), and on
       the covers L0 did not reach (so that vacuity can be proved)
  L2   longer depth and a longer timeout on the important assertions still undecided

Order is by cost and importance (spec item 5): the COI size (cheap first), then PageRank when
`n7_use_pagerank`, then the name. Traces of unknown legality go last (review required). A run
that times out is recorded as resource_limit, not as a pass.

COI cropping: every proof run gets only the properties of its layer, and Yosys's `prep` keeps
only the logic that can drive what the run proves; the per-property COI sizes from coi.py are
reported next to the result. See docs/N7.md for what is and is not sliced at the source level.
"""
from __future__ import annotations

from pathlib import Path

from verimine.n2b.engine import find_tools
from verimine.n7 import jobs, sched

LAYERS = ("L0", "L1", "L2")
_UNDECIDED = ("BOUNDED", "UNDECIDED", "RESOURCE_LIMIT")


def rank_key(unit, coi_size: int, importance: float, review: bool):
    return (review, coi_size, -importance, unit.name)


def _apply(out: dict, res: dict, layer: str) -> None:
    for name, r in res.get("props", {}).items():
        if name not in out:
            continue
        st = r.get("status") or "NOT_RUN"
        out[name].update({"status": st, "step": r.get("step"), "trace": r.get("trace", ""),
                          "skip": r.get("skip", ""), "layer": layer})
    # timeouts: sby says TIMEOUT, the property is left UNDECIDED
    timed_out = any(r.get("status") == "TIMEOUT" for r in res.get("runs", []))
    if timed_out:
        for u in res.get("props", {}):
            if out.get(u, {}).get("status") in ("UNDECIDED", "BOUNDED"):
                out[u]["status"] = "RESOURCE_LIMIT"


def prove(units, *, spec_common: dict, params: dict, rank: dict, backend: str, workers: int | None,
          workdir: Path, warnings: list[str]) -> dict:
    """units: N7Prop with .name, .kind, .prop, .variant. rank: name -> sort key.
    Returns name -> {"status", "layer", "step", "trace", "skip"}."""
    out = {u.name: {"status": "NOT_RUN", "layer": None, "step": None, "trace": "", "skip": ""} for u in units}
    ok, _solver = find_tools()
    if not units:
        return out
    if not ok:
        warnings.append("没有形式引擎（sby / yosys / SMT 求解器），只有仿真证据")
        return out

    def run(sel, layer: str, depth: int, timeout: int, prove_it: bool, prove_max: int) -> dict:
        if not sel:
            return {}
        spec = jobs.make(sel, depth=depth, timeout=timeout, prove=prove_it, prove_max=prove_max,
                         workdir=workdir / layer, name=layer, **spec_common)
        res = sched.run([spec], backend=backend, workers=workers, warnings=warnings)[0]
        for e in res.get("errors", [])[:3]:
            warnings.append(f"{layer}：{e}")
        _apply(out, res, layer)
        return res

    d0 = int(params["n7_l0_depth"])
    t0 = int(params["n7_timeout"])
    run(units, "L0", d0, t0, False, 0)

    k = max(1, int(params["n7_l1_k"]))
    undecided = sorted((u for u in units if u.kind == "assert" and out[u.name]["status"] in _UNDECIDED),
                       key=lambda u: rank[u.name])
    covers = [u for u in units if u.kind == "cover" and out[u.name]["status"] == "UNREACHED"]
    sel = undecided[: 3 * k] + covers
    if sel:
        run(sel, "L1", max(d0, int(params["n7_l1_depth"])), t0, True, max(1, len(sel)))

    top = max(0, int(params["n7_l2_top"]))
    todo = sorted((u for u in units if u.kind == "assert" and out[u.name]["status"] in _UNDECIDED),
                  key=lambda u: rank[u.name])[:top] if top else []
    if todo:
        run(todo, "L2", int(params["n7_l2_depth"]), int(params["n7_l2_timeout"]), True, max(1, len(todo)))
    return out
