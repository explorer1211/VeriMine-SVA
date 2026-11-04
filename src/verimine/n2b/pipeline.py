"""N2b: structural checks. No traces, no LLM: N2's structural evidence goes through templates
into assertions and covers, which the engine decides; failed assertions are candidate RTL
defects for N8; the result is a standalone structural check report.

Entry point: run_n2b(config_path, workspace, n2_result, ...)
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from verimine.hashing import tagged_hash
from verimine.issues import IssueLog
from verimine.n2 import N2Result, run_n2
from verimine.n2b import checks, engine, regmap, report
from verimine.n2b.props import CLASSES, Finding, Prop
from verimine.n2b.render import Unrenderable, checker_module, delivery, refs
from verimine.n2b.scan import scan
from verimine.n2b.store import write_result

# Bump when a check or template changes: part of the input hash.
N2B_VERSION = 1
TRIGGERS = {"assert": {"FAILED"}, "cover": {"UNREACHED", "UNREACHABLE"}}


@dataclass
class N2bResult:
    n2: N2Result
    findings: list[Finding]
    props: list[Prop]
    engine: engine.EngineResult
    bind_file: str
    delivery_check: dict
    warnings: list[dict]
    input_hash: str
    n2b_hash: str
    db: dict

    @property
    def defects(self) -> list[Finding]:
        return [f for f in self.findings if f.candidate_defect]

    def summary(self) -> dict:
        out = {}
        for k, name in CLASSES.items():
            fs = [f for f in self.findings if f.klass == k]
            ps = [p for p in self.props if checks.klass_of(p.check) == k]
            st: dict[str, int] = {}
            for p in ps:
                st[p.status] = st.get(p.status, 0) + 1
            out[k] = {"name": name, "findings": len(fs),
                      "by_severity": {s: sum(1 for f in fs if f.severity == s) for s in ("错误", "警告", "提示")},
                      "asserts": sum(1 for p in ps if p.kind == "assert"),
                      "covers": sum(1 for p in ps if p.kind == "cover"),
                      "status": dict(sorted(st.items()))}
        return out

    def result_dict(self) -> dict:
        """What n2b_hash covers: findings and property outcomes, no paths or timings."""
        return {
            "findings": [_finding_dict(f) for f in self.findings],
            "props": [{"id": p.id, "kind": p.kind, "check": p.check, "inst": p.inst, "delivery": p.delivery,
                       "verification": p.verification, "skip": p.skip, "status": p.status, "step": p.step}
                      for p in self.props],
        }

    def to_dict(self) -> dict:
        return {
            "stage": "N2b",
            "hashes": self.n2.n1.n0.hashes,
            "n2_input_hash": self.n2.input_hash,
            "input_hash": self.input_hash,
            "n2b_hash": self.n2b_hash,
            "summary": self.summary(),
            "findings": [_finding_dict(f) for f in self.findings],
            "props": [asdict(p) for p in self.props],
            "engine": asdict(self.engine),
            "delivery_check": self.delivery_check,
            "warnings": self.warnings,
            "db": self.db,
        }


def _finding_dict(f: Finding) -> dict:
    return asdict(f) | {"klass_name": f.klass_name}


def _top_clock(norm: dict, cx) -> dict | None:
    clocks = norm.get("clocks", [])
    if clocks:
        return {"name": clocks[0]["name"], "edge": clocks[0].get("edge", "posedge")}
    top = cx.by_path.get(cx.n2.n1.elaboration.top_path)
    return cx.inst_clock(top) if top else None


def _assign_ids(props: list[Prop]) -> None:
    order = {k: i for i, k in enumerate(CLASSES)}
    props.sort(key=lambda p: (order[checks.klass_of(p.check)], p.check, p.inst, p.kind, p.key))
    for i, p in enumerate(props, 1):
        p.id = f"N2B_{i:04d}"


def _delivery(cx, props: list[Prop]) -> str:
    mem = {(i.path, s.name) for i in cx.instances for s in i.signals if s.array_dims}
    by_inst: dict[str, list[Prop]] = {}
    for p in props:
        if any((p.inst, r) in mem for r in refs(p)):  # memories cannot be checker ports
            p.delivery = ""
            continue
        try:
            p.delivery = delivery(p)
        except Unrenderable as e:
            p.delivery = ""
            p.skip = p.skip or str(e)
            continue
        by_inst.setdefault(p.inst, []).append(p)
    mods, binds = [], []
    for n, (inst, ps) in enumerate(sorted(by_inst.items())):
        info = cx.by_path[inst]
        widths = {x.name: (x.width or 1, x.signed) for x in [*info.ports, *info.signals]}
        text, bind = checker_module(f"vm_n2b_chk_{n}", inst, ps, widths)
        mods.append(text)
        binds.append(bind)
    head = ("// VeriMine N2b 结构性检查：交付版断言与 cover（标准 SVA，bind 到实例）\n"
            "// 由 verimine n2b 生成，请勿手工修改\n\n")
    return head + "\n".join(mods) + ("\n" + "\n".join(binds) + "\n" if binds else "")


def _check_delivery(files, include_dirs, defines, top, bind_text: str, tmp: Path) -> dict:
    """N6 step 1 for the bind file: slang compiles the design plus the bind file."""
    from pyslang.ast import Compilation, CompilationOptions
    from pyslang.parsing import PreprocessorOptions
    from pyslang.pyslang import Bag, DiagnosticEngine, SourceManager
    from pyslang.syntax import SyntaxTree

    tmp.mkdir(parents=True, exist_ok=True)
    bind = tmp / "n2b_bind.sv"
    bind.write_text(bind_text, encoding="utf-8")
    sm = SourceManager()
    po = PreprocessorOptions()
    po.additionalIncludePaths = [str(d) for d in include_dirs]
    po.predefines = [f"{k}={v}" if v is not None else k for k, v in defines.items()]
    opts = CompilationOptions()
    opts.topModules = {top}
    comp = Compilation(Bag([opts]))
    trees = SyntaxTree.fromFiles([str(f) for f in [*files, bind]], sm, Bag([po]))
    for t in trees if isinstance(trees, (list, tuple)) else [trees]:
        comp.addSyntaxTree(t)
    eng = DiagnosticEngine(sm)
    errs = []
    for d in comp.getAllDiagnostics():
        if d.isError() and str(sm.getFullPath(d.location.buffer)) == str(bind):
            errs.append(f"{sm.getLineNumber(d.location)}: {eng.formatMessage(d)}")
    return {"ok": not errs, "errors": errs}


def _merge(findings: list[Finding], props: list[Prop], depth: int) -> list[Finding]:
    by_merge = {f.detail.get("merge"): f for f in findings if f.detail.get("merge")}
    out = list(findings)
    for p in props:
        trig = p.on_trigger
        mk = trig.get("merge")
        static = by_merge.get(mk) if mk else None
        if static is not None:
            static.props.append(p.id)
        if p.status in TRIGGERS[p.kind]:
            suffix = {"FAILED": f"（形式验证：第 {p.step} 拍出现反例）" if p.step is not None else "（形式验证：反例）",
                      "UNREACHED": f"（形式验证：{depth} 拍内不可达）",
                      "UNREACHABLE": "（形式验证：已证明不可达）"}[p.status]
            if static is not None:
                static.evidence, static.status = "static+formal", p.status
                static.message += suffix
                continue
            out.append(Finding(p.check, checks.klass_of(p.check), p.inst, p.module, trig["message"] + suffix,
                               trig.get("severity", "警告"), "formal", sorted(trig.get("signals", [])),
                               p.location, [p.id], {"trace": p.trace} if p.trace else {}, p.status,
                               candidate_defect=p.kind == "assert"))
        elif static is not None and p.status == "REACHED" and p.kind == "cover":
            # The static reasoning (N2's transition graph) said impossible, a trace says otherwise.
            static.status, static.severity = "REFUTED", "提示"
            static.message += f"（形式验证在第 {p.step} 拍到达，静态结论不成立）"
    order = {k: i for i, k in enumerate(CLASSES)}
    sev = {"错误": 0, "警告": 1, "提示": 2}
    out.sort(key=lambda f: (not f.candidate_defect, order[f.klass], sev.get(f.severity, 3), f.check, f.inst,
                            f.message))
    return out


def run_n2b(
    config_path: Path,
    workspace: Path | None = None,
    n2_result: N2Result | None = None,
    env: dict | None = None,
    tools: dict | None = None,
    write: bool = True,
    formal: bool = True,
    depth: int | None = None,
    timeout: int | None = None,
) -> N2bResult:
    if n2_result is None:
        n2_result = run_n2(config_path, workspace=workspace, env=env, tools=tools, write=write)
    n2 = n2_result
    n1, n0 = n2.n1, n2.n1.n0
    norm = n0.normalized
    design = norm["design"]
    root = n0.root
    workspace = Path(workspace) if workspace else n0.workspace
    params = dict(norm.get("run", {}).get("params", {}))
    depth = int(depth or params.get("n2b_depth", 20))
    timeout = int(timeout or params.get("n2b_timeout", 120))
    prove = bool(params.get("n2b_prove", True))
    log = IssueLog()

    files = [root / f for f in design.get("files", [])]
    incdirs = [root / d for d in design.get("include_dirs", [])]
    defines = {k: (str(v) if v is not None else None) for k, v in design.get("defines", {}).items()}
    top = design["top"]

    sc = scan(files, top, incdirs, defines, root)
    for e in sc.errors:
        log.warn("N2B_SCAN", e)
    cx = checks.Ctx(n2, sc, params)
    for fn in checks.ALL:
        fn(cx)
    if norm.get("registers"):
        regmap.register_checks(cx, norm["registers"], root)
    _assign_ids(cx.props)

    ih = tagged_hash("n2b_input", {
        "n2_input_hash": n2.input_hash, "build_hash": n0.hashes["build_hash"],
        "env_hash": n0.hashes.get("env_hash"), "registers": norm.get("registers"),
        "n2b_version": N2B_VERSION, "formal": formal, "depth": depth, "prove": prove,
        "max_branch_covers": params.get("n2b_max_branch_covers", 200),
    })
    bind_text = _delivery(cx, cx.props)
    tmp = workspace / "n2b" / ih[:16]
    try:
        dcheck = _check_delivery(files, incdirs, defines, top, bind_text, tmp)
    except Exception as e:  # never lose the report over the compile check
        dcheck = {"ok": False, "errors": [f"slang 编译交付版失败：{e}"]}
    if not dcheck["ok"]:
        log.warn("N2B_DELIVERY_COMPILE", f"交付版 bind 文件 slang 编译有 {len(dcheck['errors'])} 个错误")

    clocks = norm.get("clocks", [])
    if len(clocks) > 1:
        log.warn("N2B_MULTICLOCK", "多时钟设计：验证版按同一步长推进所有时钟（近似）")
    formal_ok = formal
    if formal and n1.yosys_report.blocked:
        formal_ok = False
        log.warn("N2B_NO_FORMAL", "N1 中 Yosys 无法读入设计（见 N1 基线），本次只有静态结论；修复后重跑可得到形式结论")
    eng = engine.run(cx.props, instances=[i for i in n1.elaboration.instances], files=files,
                     include_dirs=incdirs, defines=defines, top=top, root=root, scan=sc,
                     top_resets=norm.get("resets", []), assumes=norm.get("assumes", []),
                     top_clock=_top_clock(norm, cx), workdir=tmp / "formal", depth=depth, timeout=timeout,
                     prove=prove, prove_max=int(params.get("n2b_prove_max", 64)), enabled=formal_ok)
    for w in eng.warnings:
        log.warn("N2B_ENGINE", w)
    for e in eng.errors:
        log.warn("N2B_ENV_ERROR", e)

    findings = _merge(cx.findings, cx.props, depth)
    res = N2bResult(n2, findings, cx.props, eng, bind_text, dcheck, [w.to_dict() for w in log.warnings],
                    ih, "", {"new_result": False})
    res.n2b_hash = tagged_hash("n2b_result", res.result_dict())

    if write:
        res.db = write_result(workspace / "verimine.db", ih, n0.hashes["build_hash"], n2.input_hash,
                              res.n2b_hash, res)
        run_dir = workspace / "runs" / n0.hashes["run_hash"]
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "n2b.json").write_text(
            json.dumps(res.to_dict(), indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        (run_dir / "n2b_bind.sv").write_text(bind_text, encoding="utf-8")
        (run_dir / "n2b_report.md").write_text(report.markdown(res), encoding="utf-8")
    return res
