"""N5: SVA generation. N4's candidate pool in; two renderings per property out:

  1  delivery version (standard SVA, checker modules + bind file) and verification version
     (what the open-source engine reads; Yosys-unsupported forms degraded to monitors)
  2  clock, `disable iff` (resets) and assertion names generated in one place
  3  a cover for every assertion's antecedent, to catch vacuous proofs later (N7)
  4  three-way differential test: IR offline, simulation, formal must agree (diff.py)
  5  rendering reads the engine capability matrix (verimine/engines.py); degradations are
     recorded in the report
  6  every property is tagged with the v5 fragment it belongs to and its window

Entry point: run_n5(config_path, workspace, n4_result, ...)
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from verimine import engines
from verimine.hashing import tagged_hash
from verimine.issues import IssueLog
from verimine.n2b.engine import find_tools
from verimine.n2b.scan import scan as n2b_scan
from verimine.n3.pipeline import _design
from verimine.n4 import N4Result, run_n4
from verimine.n5 import diff as diff_mod
from verimine.n5.render import Rendered, delivery_files, props_of, verification_files
from verimine.n5.store import write_run

N5_VERSION = 1
DEFAULTS = {"n5_diff": True, "n5_diff_cases": 3, "n5_diff_cycles": 40, "n5_diff_reset_cycles": 2,
            "n5_diff_timeout": 300}


@dataclass
class N5Result:
    n4: N4Result
    props: list[Rendered]
    delivery: str
    bind: str
    verification: dict[str, str]
    diff: dict
    warnings: list[dict]
    input_hash: str
    n5_hash: str
    db: dict = field(default_factory=dict)

    @property
    def asserts(self) -> list[Rendered]:
        return [r for r in self.props if r.kind == "assert"]

    def counts(self) -> dict:
        d: dict[str, int] = {}
        for r in self.props:
            k = r.diff.get("status", "not_run")
            d[k] = d.get(k, 0) + 1
        return {
            "asserts": len(self.asserts), "covers": len(self.props) - len(self.asserts),
            "delivery": sum(1 for r in self.props if r.delivery),
            "verification": sum(1 for r in self.props if r.verification),
            "degraded": sum(1 for r in self.props if r.degraded), "diff": dict(sorted(d.items())),
        }

    def degradations(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self.props:
            for x in r.degraded:
                out[x] = out.get(x, 0) + 1
        return dict(sorted(out.items()))

    def result_dict(self) -> dict:
        return {"props": [prop_dict(r, full=False) for r in self.props], "delivery": self.delivery,
                "bind": self.bind, "verification": self.verification}

    def to_dict(self) -> dict:
        return {"stage": "N5", "hashes": self.n4.n3.n2.n1.n0.hashes, "n4_input_hash": self.n4.input_hash,
                "n4_hash": self.n4.n4_hash, "input_hash": self.input_hash, "n5_hash": self.n5_hash,
                "counts": self.counts(), "degradations": self.degradations(), "capabilities": engines.CAPS,
                "props": [prop_dict(r) for r in self.props], "diff": self.diff, "warnings": self.warnings,
                "db": self.db}


def prop_dict(r: Rendered, full: bool = True) -> dict:
    c = r.cand
    d = {"name": r.name, "kind": r.kind, "cand": c.id, "scope": c.scope, "type": c.type, "sources": c.sources,
         "legality": c.legality, "assumes": c.assumes, "text": c.text if r.kind == "assert" else f"cover {r.name}",
         "delivery": r.delivery, "verification": r.verification, "degraded": r.degraded, "skip": r.skip,
         "engines": r.engines, "fragment": r.fragment, "window": r.window}
    if full:
        d["diff"] = r.diff
    else:  # what the hash covers: the outcome, not traces or paths
        d["diff"] = {k: r.diff.get(k) for k in ("status", "delivery_ok", "verification_ok", "cross")}
    return d


def run_n5(
    config_path: Path,
    workspace: Path | None = None,
    n4_result: N4Result | None = None,
    env: dict | None = None,
    tools: dict | None = None,
    write: bool = True,
    differential: bool | None = None,
) -> N5Result:
    if n4_result is None:
        n4_result = run_n4(config_path, workspace=workspace, env=env, tools=tools, write=write)
    n4 = n4_result
    n3, n2, n1, n0 = n4.n3, n4.n3.n2, n4.n3.n2.n1, n4.n3.n2.n1.n0
    norm = n0.normalized
    workspace = Path(workspace) if workspace else n0.workspace
    params = DEFAULTS | {k: v for k, v in norm.get("run", {}).get("params", {}).items() if k in DEFAULTS}
    if differential is not None:
        params["n5_diff"] = differential
    log = IssueLog()
    d, widths, _t = _design(n2, norm, log)
    by_path = {i.path: i for i in n1.elaboration.instances}
    mems = {(i.path, s.name) for i in n1.elaboration.instances for s in i.signals if s.array_dims}

    # Steps 1-3, 5-6
    rendered = props_of(n4.pool)
    delivery, bind, _chk = delivery_files(rendered, by_path, mems)
    verif = verification_files(rendered)
    for r in rendered:
        if r.skip:
            log.warn("N5_PARTIAL", f"{r.name}：{r.skip}")

    ih = tagged_hash("n5_input", {
        "n4_input_hash": n4.input_hash, "n4_hash": n4.n4_hash, "traces_hash": n3.traces_hash,
        "build_hash": n0.hashes["build_hash"], "env_hash": n0.hashes["env_hash"], "params": params,
        "caps_version": engines.CAPS_VERSION, "n5_version": N5_VERSION,
    })
    work = workspace / "n5" / ih[:16]

    # Step 4
    diff_info: dict = {"ran": False}
    if not params["n5_diff"]:
        diff_info["reason"] = "已关闭（n5_diff: false）"
    elif not rendered:
        diff_info["reason"] = "候选池为空"
    else:
        setup = diff_setup(n4, params, (d, widths))
        if isinstance(setup, str):
            diff_info["reason"] = setup
        else:
            diff_info = diff_mod.run(rendered, workdir=work / "diff", params=params, **setup)
    if not diff_info["ran"]:
        log.warn("N5_NO_DIFF", f"未做三方差分测试：{diff_info['reason']}")
    else:
        for k in ("sim_error", "formal_error"):
            if diff_info.get(k):
                log.warn("N5_DIFF_LEG", f"差分测试的{'仿真' if k == 'sim_error' else '形式'}一路没有结果：{diff_info[k]}")
        bad = [r for r in rendered if r.diff.get("status") == "mismatch"]
        if bad:
            log.warn("N5_RENDER_MISMATCH", f"{len(bad)} 条属性三方结果不一致（渲染有语义错误），不应进入 N7："
                     + "、".join(r.name for r in bad[:5]))
        if diff_info["model"]["mismatches"]:
            m = diff_info["model"]["mismatches"][0]
            log.warn("N5_MODEL_MISMATCH", f"Verilator 与 Yosys 对设计本身的取值不一致（{m['signal']} 第 {m['row']} 拍："
                                          f"{m['sim']} 对 {m['formal']}），相关属性的跨引擎比较不作为渲染错误")

    res = N5Result(n4, rendered, delivery, bind, verif, diff_info, [w.to_dict() for w in log.warnings], ih, "")
    res.n5_hash = tagged_hash("n5_result", res.result_dict())
    if write:
        res.db = write_run(workspace / "verimine.db", res)
        run_dir = workspace / "runs" / n0.hashes["run_hash"]
        (run_dir / "n5_verification").mkdir(parents=True, exist_ok=True)
        (run_dir / "n5_sva.sv").write_text(delivery, encoding="utf-8")
        (run_dir / "n5_bind.sv").write_text(bind, encoding="utf-8")
        for mod, text in verif.items():
            (run_dir / "n5_verification" / f"vm_n5_{mod}.svh").write_text(text, encoding="utf-8")
        (run_dir / "n5.json").write_text(json.dumps(res.to_dict(), indent=2, ensure_ascii=False, sort_keys=True,
                                                    default=str) + "\n", encoding="utf-8")
        from verimine.n5.report import markdown
        (run_dir / "n5_report.md").write_text(markdown(res), encoding="utf-8")
    return res


def diff_setup(n4: N4Result, params: dict, design_info: tuple | None = None) -> dict | str:
    """Everything the differential test needs besides the properties (keyword arguments of
    diff.run), or why it cannot run. Shared with N6, which re-tests what it re-rendered."""
    n3, n2, n1, n0 = n4.n3, n4.n3.n2, n4.n3.n2.n1, n4.n3.n2.n1.n0
    norm = n0.normalized
    root, design = n0.root, norm["design"]
    d, widths = design_info or _design(n2, norm, IssueLog())[:2]
    if not find_tools()[0]:
        return "没有形式引擎（sby / yosys / SMT 求解器）"
    if n1.yosys_report.blocked:
        return "Yosys 读不了设计（见 N1 基线）"
    if len(norm.get("clocks", [])) > 1:
        return "多时钟设计：向量按单一时钟驱动，暂不做差分测试"
    vec = diff_mod.build_vector(n3.cases, d, params)
    if vec is None:
        return "N3 没有可用作向量的测试用例"
    by_path = {i.path: i for i in n1.elaboration.instances}
    files = [root / f for f in design.get("files", [])]
    incdirs = [root / x for x in design.get("include_dirs", [])]
    defines = {k: (str(v) if v is not None else None) for k, v in design.get("defines", {}).items()}
    ctx = dict(instances=n1.elaboration.instances, files=files, include_dirs=incdirs, defines=defines,
               top=design["top"], root=root, scan=n2b_scan(files, design["top"], incdirs, defines, root),
               top_resets=norm.get("resets", []), top_clock=vec.clock, top_path=n1.elaboration.top_path,
               parameters=dict(design.get("parameters", {})))
    return dict(vec=vec, d=d, widths_of=lambda s: widths.get(s, {}), ctx=ctx, by_path=by_path, files=files,
                include_dirs=incdirs, defines=defines)
