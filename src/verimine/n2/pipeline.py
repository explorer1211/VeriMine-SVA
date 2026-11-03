"""N2: behavioral modeling. N1's structural index in; VDG, state machines, counters, shift
registers, memories, interface events, signal roles, PageRank, COI fingerprints out.

Entry point: run_n2(config_path, workspace, n1_result, ...)
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from verimine.hashing import tagged_hash
from verimine.issues import IssueLog
from verimine.n1 import N1Result, run_n1
from verimine.n2 import coi as coi_mod
from verimine.n2 import roles as roles_mod
from verimine.n2 import vdg as vdg_mod
from verimine.n2.events import Event, find_events
from verimine.n2.patterns import Fsm, InstView, Pattern, find_fsms, find_patterns
from verimine.n2.rank import pagerank
from verimine.n2.store import write_model

# Bump when a recognition rule changes: it is part of the input hash, so old models are not
# mistaken for current ones.
N2_VERSION = 1


@dataclass
class N2Result:
    n1: N1Result
    vdg: vdg_mod.Vdg
    fsms: list[Fsm]
    patterns: list[Pattern]
    events: list[Event]
    roles: list[roles_mod.SignalRoles]
    pagerank: dict[str, float]
    cois: list[coi_mod.Coi]
    warnings: list[dict]
    input_hash: str
    n2_hash: str
    db: dict

    def model_dict(self) -> dict[str, Any]:
        """The model itself, canonical: this is what n2_hash covers."""
        g = self.vdg
        return {
            "modeled": g.modeled,
            "skipped": g.skipped,
            "nodes": [asdict(n) | {"pagerank": self.pagerank.get(n.id, 0.0)}
                      for n in sorted(g.nodes.values(), key=lambda n: n.id)],
            "edges": [asdict(e) for e in g.edges],
            "fsms": [asdict(f) for f in self.fsms],
            "patterns": [asdict(p) for p in self.patterns],
            "events": [asdict(e) for e in self.events],
            "roles": [asdict(r) for r in self.roles if r.roles],
            "coi": [{"target": c.target, "inst": c.inst, "size": len(c.nodes), "inputs": c.inputs,
                     "sequential_depth": c.sequential_depth, "fingerprint": c.fingerprint}
                    for c in self.cois],
        }

    def to_dict(self) -> dict:
        return {
            "stage": "N2",
            "hashes": self.n1.n0.hashes,
            "index_hash": self.n1.index_hash,
            "input_hash": self.input_hash,
            "n2_hash": self.n2_hash,
            **self.model_dict(),
            "warnings": self.warnings,
            "db": self.db,
        }


def input_hash(n1: N1Result) -> str:
    norm = n1.n0.normalized
    return tagged_hash("n2_input", {
        "index_hash": n1.index_hash,
        "clocks": norm.get("clocks", []),
        "resets": norm.get("resets", []),
        "n2_version": N2_VERSION,
    })


def run_n2(
    config_path: Path,
    workspace: Path | None = None,
    n1_result: N1Result | None = None,
    env: dict | None = None,
    tools: dict | None = None,
    write: bool = True,
) -> N2Result:
    if n1_result is None:
        n1_result = run_n1(config_path, workspace=workspace, env=env, tools=tools, write=write)
    n1 = n1_result
    norm = n1.n0.normalized
    workspace = Path(workspace) if workspace else n1.n0.workspace
    log = IssueLog()

    elab = n1.elaboration
    statuses = {s.path: s.status for s in n1.statuses}
    top = elab.top_path
    if statuses.get(top) == "BLOCKED":
        log.warn("N2_TOP_BLOCKED", f"顶层 {top} 在 N1 中为 BLOCKED，只对未阻塞的子模块建模")
    if elab.errors:
        log.warn("N2_INDEX_INCOMPLETE", f"N1 索引不完整（slang 有 {len(elab.errors)} 个 error），模型可能缺项")

    # Steps 1-2: VDG
    g = vdg_mod.build(elab.instances, top, statuses, norm.get("clocks", []), norm.get("resets", []))
    for s in g.skipped:
        log.warn("N2_SKIPPED_BLOCKED", f"{s['path']}（{s['module']}）{s['reason']}")

    # Steps 3-6: structures, per modeled instance
    by_path = {i.path: i for i in elab.instances}
    fsms: list[Fsm] = []
    patterns: list[Pattern] = []
    events: list[Event] = []
    child_exprs: dict[str, list[dict]] = {}
    for i in elab.instances:
        if i.parent is not None:
            child_exprs.setdefault(i.parent, []).extend(c["expr"] for c in i.connections if c.get("expr"))
    for path in g.modeled:
        inst = by_path[path]
        view = InstView(inst, g.resets.get(path, {}), child_exprs.get(path, []))
        pats = find_patterns(view)
        patterns += pats
        fsms += find_fsms(view, {p.signal for p in pats})
        events += find_events(inst, child_exprs.get(path, []))

    # Step 7, 11: roles and abstraction levels
    roles = roles_mod.build(g, patterns, fsms, events)
    # Step 8: PageRank (optional feature)
    pr = pagerank(list(g.nodes), g.edges)
    # Step 9: COI and COI fingerprints
    cois = coi_mod.compute(g)

    ih = input_hash(n1)
    res = N2Result(n1, g, fsms, patterns, events, roles, pr, cois,
                   [w.to_dict() for w in log.warnings], ih, "", {"new_model": False})
    res.n2_hash = tagged_hash("n2_model", res.model_dict())

    # Step 10: storage, with the input hash
    if write:
        res.db = write_model(workspace / "verimine.db", ih, n1.n0.hashes["build_hash"],
                             n1.index_hash, res.n2_hash, res)
        run_dir = workspace / "runs" / n1.n0.hashes["run_hash"]
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "n2.json").write_text(
            json.dumps(res.to_dict(), indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    return res
