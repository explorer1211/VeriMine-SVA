"""N4: candidate mining. Three paths into one pool:

  A  rules and templates over N2's structural evidence (templates.py), no traces needed
  B  decision trees on S_mine (tree.py), GoldMine style; FTO-sensitive: off by default
  C  LLM proposals from structural summaries (llm.py), off unless `llm.mode` is set

Every candidate carries three tags (spec): its source (A / B / C), the legality of the traces
behind it, and the config assumes it depends on. The pool is one IR (ir.py), normalized and
deduplicated, checked on S_mine, with a budget driven by the coverage gaps (pool.py).

Entry point: run_n4(config_path, workspace, n3_result, ...)
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from verimine.hashing import tagged_hash
from verimine.issues import IssueLog
from verimine.n3 import N3Result, run_n3
from verimine.n3.pipeline import _design
from verimine.n4 import llm as llm_mod
from verimine.n4 import pool, templates, tree
from verimine.n4.ir import SOURCES, Cand
from verimine.n4.store import write_run

N4_VERSION = 1
DEFAULTS = {
    "n4_path_b": False,                    # FTO-sensitive (decision-tree mining): experimental
    "n4_use_pagerank": False,              # FTO-sensitive: importance only as a tie-breaker
    "n4_tree_window": 2, "n4_tree_depth": 3, "n4_tree_min_leaf": 3, "n4_tree_max_per_target": 8,
    "n4_quota": 8, "n4_gap_boost": 3, "n4_max_candidates": 2000,
    "n4_llm_allow_foreign": False, "n4_llm_timeout": 120,
}
FTO_SENSITIVE = ("n4_path_b", "n4_use_pagerank")


@dataclass
class N4Result:
    n3: N3Result
    cands: list[Cand]
    paths: dict
    budget: dict
    audit: list[dict]
    warnings: list[dict]
    input_hash: str
    n4_hash: str
    db: dict = field(default_factory=dict)

    @property
    def pool(self) -> list[Cand]:
        return [c for c in self.cands if c.status == "POOL"]

    def counts(self) -> dict:
        out: dict[str, int] = {}
        for c in self.cands:
            out[c.status] = out.get(c.status, 0) + 1
        return dict(sorted(out.items()))

    def by_source(self) -> dict:
        return {s: sum(1 for c in self.pool if s in c.sources) for s in SOURCES}

    def result_dict(self) -> dict:
        return {"cands": [cand_dict(c) for c in self.cands]}

    def to_dict(self) -> dict:
        return {"stage": "N4", "hashes": self.n3.n2.n1.n0.hashes, "n3_input_hash": self.n3.input_hash,
                "traces_hash": self.n3.traces_hash, "input_hash": self.input_hash, "n4_hash": self.n4_hash,
                "counts": self.counts(), "by_source": self.by_source(), "paths": self.paths, "budget": self.budget,
                "candidates": [cand_dict(c) for c in self.cands],
                "llm_audit": [{k: v for k, v in a.items() if k != "output"} for a in self.audit],
                "warnings": self.warnings, "db": self.db}


def cand_dict(c: Cand) -> dict:
    return asdict(c) | {"key": c.key, "text": c.text, "ir": c.ir(), "review_required": c.review_required}


def run_n4(
    config_path: Path,
    workspace: Path | None = None,
    n3_result: N3Result | None = None,
    env: dict | None = None,
    tools: dict | None = None,
    write: bool = True,
) -> N4Result:
    if n3_result is None:
        n3_result = run_n3(config_path, workspace=workspace, env=env, tools=tools, write=write)
    n3 = n3_result
    n2, n1, n0 = n3.n2, n3.n2.n1, n3.n2.n1.n0
    norm = n0.normalized
    workspace = Path(workspace) if workspace else n0.workspace
    params = DEFAULTS | {k: v for k, v in norm.get("run", {}).get("params", {}).items() if k in DEFAULTS}
    llm_cfg = norm.get("llm", {"mode": "off"})
    log = IssueLog()
    _d, widths, _t = _design(n2, norm, log)
    widths_of = lambda s: widths.get(s, {})  # noqa: E731
    top = n1.elaboration.top_path
    by_path = {i.path: i for i in n1.elaboration.instances}
    child_exprs: dict[str, list[dict]] = {}
    for i in n1.elaboration.instances:
        if i.parent is not None:
            child_exprs.setdefault(i.parent, []).extend(c["expr"] for c in i.connections if c.get("expr"))
    scopes = {p: templates.Scope(n2, by_path[p], child_exprs.get(p, [])) for p in n2.vdg.modeled}
    for p, sc in sorted(scopes.items()):
        if sc.clock is None and sc.view.registers:
            log.warn("N4_NO_CLOCK", f"{p}：时钟不唯一或找不到，跳过路径 A 的时序模板")
    mine_cases = [c for c in n3.cases if c.set == "mine"]
    assumes = norm.get("assumes", [])

    ih = tagged_hash("n4_input", {
        "n3_input_hash": n3.input_hash, "traces_hash": n3.traces_hash, "n2_hash": n2.n2_hash,
        "build_hash": n0.hashes["build_hash"], "params": params,
        "llm": llm_cfg if llm_cfg.get("mode") != "off" else {"mode": "off"},
        "prompt_set": llm_mod.PROMPT_SET_VERSION, "n4_version": N4_VERSION,
    })

    # Path A
    cands = templates.generate(n2, scopes)
    paths = {"A": {"ran": True, "raw": len(cands)}}
    # Path B
    if params["n4_path_b"]:
        log.warn("N4_PATH_B_EXPERIMENTAL", "路径 B（决策树挖掘）是实验功能：FTO 结论出来前不对外发布")
        if not mine_cases:
            log.warn("N4_NO_MINE_SET", "S_mine 为空：路径 B 没有数据")
        b = tree.mine(n2, scopes, mine_cases, params, widths_of)
        paths["B"] = {"ran": True, "raw": len(b), "rows": sum(c.cycles for c in mine_cases)}
        cands += b
    else:
        paths["B"] = {"ran": False, "reason": "FTO 关口前默认关闭（run.params.n4_path_b: true 打开，实验功能）"}
    # Path C
    audit: list[dict] = []
    if llm_cfg.get("mode", "off") != "off":
        c_cands, audit = llm_mod.mine(n2, scopes, llm_cfg, params, widths_of, log)
        paths["C"] = {"ran": True, "raw": len(c_cands), "calls": len(audit),
                      "failed": sum(1 for a in audit if a["error"]), "level": "L1+L2"}
        cands += c_cands
    else:
        paths["C"] = {"ran": False, "reason": "llm.mode = off"}

    # Pool: IR, normalized, deduplicated
    raw = len(cands)
    cands = pool.dedupe(cands)
    paths["dedupe"] = {"raw": raw, "unique": len(cands)}

    # Offline check on S_mine; tags
    unknown_only = 0
    for c in cands:
        c.trace = pool.check(c, mine_cases, widths_of(c.scope)) if mine_cases else {}
        if pool.legal_violations(c.trace):
            f = c.trace["first"]
            c.status = "DROPPED"
            c.reason = f"S_mine 上被违反（{f['case']} 第 {f['row']} 行，轨迹合法性 {f['legality']}）"
        elif c.trace.get("violations"):
            unknown_only += 1
            c.evidence.append("只在合法性未知（unknown）的轨迹上被违反，可能来自非法输入，保留")
        c.assumes = pool.depends(c, n2.vdg, top, assumes)
    if unknown_only:
        log.warn("N4_UNKNOWN_VIOLATIONS", f"{unknown_only} 个候选只在 trace_legality = unknown 的轨迹上被违反，保留，交给 N7")
    if any(c.review_required for c in cands if c.status == "POOL"):
        log.warn("N4_REVIEW_REQUIRED", "部分候选来自合法性未知的轨迹：默认降权，进入 REVIEW_REQUIRED，不自动接受")

    # Budget, driven by coverage gaps
    importance = n2.pagerank if params["n4_use_pagerank"] else None
    if params["n4_use_pagerank"]:
        log.warn("N4_PAGERANK_FTO", "按重要性（PageRank）排序是 FTO 敏感功能，只作并列时的辅助")
    gaps = pool.gap_signals(n3.coverage)
    bud = pool.budget(cands, gaps, params, importance)

    _assign_ids(cands, top)
    res = N4Result(n3, cands, paths, bud, audit, [w.to_dict() for w in log.warnings], ih, "")
    res.n4_hash = tagged_hash("n4_result", res.result_dict())
    if write:
        res.db = write_run(workspace / "verimine.db", res)
        run_dir = workspace / "runs" / n0.hashes["run_hash"]
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "n4.json").write_text(json.dumps(res.to_dict(), indent=2, ensure_ascii=False, sort_keys=True)
                                         + "\n", encoding="utf-8")
        from verimine.n4.report import markdown
        (run_dir / "n4_report.md").write_text(markdown(res), encoding="utf-8")
    return res


_STATUS_ORDER = {"POOL": 0, "DEFERRED": 1, "DROPPED": 2}


def _assign_ids(cands: list[Cand], top: str) -> None:
    cands.sort(key=lambda c: (_STATUS_ORDER[c.status], c.scope != top, c.scope, min(c.sources), c.type, c.key))
    for i, c in enumerate(cands, 1):
        c.id = f"N4_{i:04d}"
