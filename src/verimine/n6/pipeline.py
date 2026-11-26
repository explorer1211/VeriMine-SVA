"""N6: compile check. N5's two renderings in; what compiles, and what goes on to N7, out.

  1  slang compiles the delivery version: the design plus the checker modules and bind file
  2  Yosys reads the verification version, inserted the way the formal engine inserts it
  3  a property that fails goes back to N5 for a more conservative rendering (variants.py), at
     most 2 rounds; each round recompiles everything still in, so the final files are exactly
     what was compiled
  4  still failing: ENV_ERROR, in the report, not handed to N7

A property is ENV_ERROR when a leg it has fails all rounds; a leg N5 could not render at all
(`skip`) is absent, not failed. When N5 ran the differential test and N6 changed a verification
text, the changed ones are tested again (n6_rediff), so N7 never gets an untested rendering.

Entry point: run_n6(config_path, workspace, n5_result, ...)
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from verimine.hashing import tagged_hash
from verimine.issues import IssueLog
from verimine.n2b.engine import _assume_lines, _Design
from verimine.n2b.render import Unrenderable, pv_name, uses_past
from verimine.n2b.scan import scan as n2b_scan
from verimine.n5 import N5Result, run_n5
from verimine.n5 import diff as diff_mod
from verimine.n5.pipeline import diff_setup
from verimine.n5.render import widths_of
from verimine.n6 import variants as V
from verimine.n6.compile import Diag, bisect, pin, slang_diags, yosys_available, yosys_diags

N6_VERSION = 1
DEFAULTS = {"n6_rounds": V.MAX_ROUND, "n6_timeout": 300, "n6_bisect_budget": 64, "n6_rediff": True}
SVA, BIND = "n6_sva.sv", "n6_bind.sv"
ENV = "环境"            # pseudo-name for diagnostics on the top module's environment lines


@dataclass
class N6Prop:
    name: str
    kind: str
    rendered: object                 # N5 Rendered
    has_d: bool
    has_v: bool
    d_variant: int = 0
    v_variant: int = 0
    d_ok: bool | None = None         # None: leg absent
    v_ok: bool | None = None
    history: list[dict] = field(default_factory=list)   # {"round", "leg", "variant", "diags"}
    delivery: str = ""               # final property line (inside its checker)
    verification: str = ""           # final text, label = "LABEL" (engines substitute theirs)
    rediff: dict = field(default_factory=dict)

    @property
    def status(self) -> str:
        return "ENV_ERROR" if self.d_ok is False or self.v_ok is False else "OK"

    @property
    def diff_status(self) -> str:
        return (self.rediff or self.rendered.diff).get("status", "not_run")

    @property
    def to_n7(self) -> bool:
        return self.status == "OK" and self.diff_status != "mismatch"

    @property
    def rerendered(self) -> bool:
        return self.d_variant > 0 or self.v_variant > 0


@dataclass
class N6Result:
    n5: N5Result
    props: list[N6Prop]
    rounds: list[dict]
    delivery: str
    bind: str
    verification: dict[str, str]
    legs: dict
    warnings: list[dict]
    input_hash: str
    n6_hash: str
    db: dict = field(default_factory=dict)

    def counts(self) -> dict:
        c = {"props": len(self.props), "ok": sum(p.status == "OK" for p in self.props),
             "env_error": sum(p.status == "ENV_ERROR" for p in self.props),
             "rerendered": sum(p.rerendered for p in self.props), "to_n7": sum(p.to_n7 for p in self.props),
             "delivery_ok": sum(p.d_ok is True for p in self.props),
             "verification_ok": sum(p.v_ok is True for p in self.props)}
        return c

    def result_dict(self) -> dict:
        return {"props": [prop_dict(p, full=False) for p in self.props], "delivery": self.delivery,
                "bind": self.bind, "verification": self.verification}

    def to_dict(self) -> dict:
        n5 = self.n5
        return {"stage": "N6", "hashes": n5.n4.n3.n2.n1.n0.hashes, "n5_input_hash": n5.input_hash,
                "n5_hash": n5.n5_hash, "input_hash": self.input_hash, "n6_hash": self.n6_hash,
                "counts": self.counts(), "legs": self.legs, "rounds": self.rounds,
                "props": [prop_dict(p) for p in self.props], "warnings": self.warnings, "db": self.db}


def prop_dict(p: N6Prop, full: bool = True) -> dict:
    d = {"name": p.name, "kind": p.kind, "cand": p.rendered.cand.id, "scope": p.rendered.cand.scope,
         "status": p.status, "to_n7": p.to_n7, "delivery_ok": p.d_ok, "verification_ok": p.v_ok,
         "delivery_variant": p.d_variant, "verification_variant": p.v_variant, "delivery": p.delivery,
         "verification": p.verification, "diff_status": p.diff_status, "n5_skip": p.rendered.skip}
    if full:
        d["history"] = p.history
        d["rediff"] = p.rediff
    return d


# --------------------------------------------------------------------------- #
# The two compile checks over a set of properties
# --------------------------------------------------------------------------- #

class _Delivery:
    """slang over design + checker modules + bind file."""

    def __init__(self, files, include_dirs, defines, top, by_path, workdir: Path):
        self.files, self.include_dirs, self.defines, self.top = files, include_dirs, defines, top
        self.by_path, self.workdir = by_path, workdir
        workdir.mkdir(parents=True, exist_ok=True)
        self.baseline = {(d.code, d.file, d.line, d.message)
                         for d in slang_diags(files, include_dirs, defines, top, [])}

    def files_for(self, ps: list[N6Prop]) -> tuple[str, str, dict, dict]:
        items = [(p.name, p.rendered.prop, p.d_variant) for p in ps]
        return V.assemble(items, lambda inst: widths_of(self.by_path[inst]) if inst in self.by_path else {})

    def diags(self, ps: list[N6Prop]) -> tuple[dict[str, list[Diag]], list[Diag]]:
        sva, bind, smap, bmap = self.files_for(ps)
        (self.workdir / SVA).write_text(sva, encoding="utf-8")
        (self.workdir / BIND).write_text(bind, encoding="utf-8")
        ds = slang_diags(self.files, self.include_dirs, self.defines, self.top,
                         [self.workdir / SVA, self.workdir / BIND])
        return pin(ds, {SVA: smap, BIND: bmap}, self.baseline)


class _Verification:
    """Yosys over the design with the verification includes (as the n2b engine inserts them)."""

    def __init__(self, ctx: dict, modules: set[str], env: list[str], workdir: Path, timeout: int):
        self.top, self.env, self.workdir, self.timeout = ctx["top"], env, workdir, timeout
        self.design = _Design(ctx["files"], ctx["include_dirs"], ctx["defines"], ctx["top"], ctx["root"],
                              ctx["scan"], workdir)
        self.modules = (modules & set(ctx["scan"].modules)) | {ctx["top"]}
        self.design.write_sources(self.modules)
        self.runs = 0
        self.baseline = yosys_diags(self.design, self.incs([])[0], workdir / "base", timeout)
        self.runs += 1

    def incs(self, ps: list[N6Prop], env: bool = False) -> tuple[dict[str, str], dict[str, dict[int, list[str]]]]:
        texts: dict[str, dict[str, list[str]]] = {m: {} for m in self.modules}   # module -> text -> names
        clocks: dict[str, dict[str, dict]] = {}
        for p in ps:
            q = p.rendered.prop
            texts.setdefault(q.module, {}).setdefault(p.verification, []).append(p.name)
            if q.clock and (q.next or uses_past(q.expr) or uses_past(q.guard)):
                clocks.setdefault(q.module, {})[q.clock["name"]] = q.clock
        out, maps = {}, {}
        for m, by_text in texts.items():
            lines, lm = ["`ifdef VERIMINE_N2B"], {}
            for _, c in sorted(clocks.get(m, {}).items()):
                lines += [f"  reg {pv_name(c)} = 1'b0;", f"  always @({c['edge']} {c['name']}) {pv_name(c)} <= 1'b1;"]
            if m == self.top and env:
                for x in self.env:
                    lines.append(x)
                    lm[len(lines)] = [ENV]
            for k, (text, names) in enumerate(sorted(by_text.items())):
                decl, check = V.split_verification(text.replace("LABEL:", f"vm_n6_{k}:", 1))
                for x in [f"// vm_n6_{k}: " + "、".join(names), *decl, check]:
                    lines.append("  " + x)
                    lm[len(lines)] = names
            lines.append("`endif")
            out[m] = "\n".join(lines) + "\n"
            maps[f"vm_n2b_{m}.svh"] = lm
        return out, maps

    def diags(self, ps: list[N6Prop], env: bool = True) -> tuple[dict[str, list[Diag]], list[Diag]]:
        """Yosys stops at the first error: pin it, take the property out, read again."""
        base = {(d.code, d.file, d.line, d.message) for d in self.baseline}
        pinned: dict[str, list[Diag]] = {}
        live = list(ps)
        while True:
            incs, maps = self.incs(live, env)
            self.runs += 1
            ds = yosys_diags(self.design, incs, self.workdir / f"r{self.runs}", self.timeout)
            got, loose = pin(ds, maps, base)
            for n, x in got.items():
                pinned.setdefault(n, []).extend(x)
            stop = any(d.code in ("error", "timeout") for x in got.values() for d in x)
            if loose or not stop:
                return pinned, loose
            live = [p for p in live if p.name not in got]
            if ENV in got:
                env = False


# --------------------------------------------------------------------------- #

def _render(p: N6Prop) -> None:
    q = p.rendered.prop
    if p.has_d:
        p.delivery = V.delivery_text(q, p.d_variant)
    if p.has_v:
        try:
            p.verification = V.verification(q, "LABEL", p.v_variant)
        except Unrenderable as e:   # a fallback that cannot take this property: fails the round
            p.verification = ""
            p.history.append({"round": -1, "leg": "verification", "variant": p.v_variant,
                              "diags": [f"无法渲染：{e}"]})


def _check(leg: str, checker, ps: list[N6Prop], budget: list[int]) -> tuple[dict[str, list[Diag]], list[Diag]]:
    pinned, loose = checker.diags(ps)
    if not loose:
        return pinned, []
    rest = [p for p in ps if p.name not in pinned]
    by = {p.name: p for p in rest}

    def fails(names: list[str]) -> bool:
        got, lo = checker.diags([by[n] for n in names])
        return bool(set(got) - {ENV} or lo)

    culprits = bisect([p.name for p in rest], fails, budget)
    if not culprits:     # only fails together: every remaining property shares the blame
        culprits = [p.name for p in rest]
    for n in culprits:
        pinned.setdefault(n, []).extend(loose)
    return pinned, loose


def run_n6(
    config_path: Path,
    workspace: Path | None = None,
    n5_result: N5Result | None = None,
    env: dict | None = None,
    tools: dict | None = None,
    write: bool = True,
    rediff: bool | None = None,
) -> N6Result:
    if n5_result is None:
        n5_result = run_n5(config_path, workspace=workspace, env=env, tools=tools, write=write)
    n5 = n5_result
    n4, n1, n0 = n5.n4, n5.n4.n3.n2.n1, n5.n4.n3.n2.n1.n0
    norm = n0.normalized
    root, design = n0.root, norm["design"]
    workspace = Path(workspace) if workspace else n0.workspace
    params = DEFAULTS | {k: v for k, v in norm.get("run", {}).get("params", {}).items() if k in DEFAULTS}
    if rediff is not None:
        params["n6_rediff"] = rediff
    rounds_max = max(0, min(int(params["n6_rounds"]), V.MAX_ROUND))
    log = IssueLog()

    ih = tagged_hash("n6_input", {"n5_input_hash": n5.input_hash, "n5_hash": n5.n5_hash, "params": params,
                                  "n6_version": N6_VERSION})
    work = workspace / "n6" / ih[:16]
    files = [root / f for f in design.get("files", [])]
    incdirs = [root / x for x in design.get("include_dirs", [])]
    defines = {k: (str(v) if v is not None else None) for k, v in design.get("defines", {}).items()}
    by_path = {i.path: i for i in n1.elaboration.instances}

    props = [N6Prop(r.name, r.kind, r, bool(r.delivery), bool(r.verification)) for r in n5.props]
    for p in props:
        _render(p)

    legs: dict = {"delivery": {"ran": False}, "verification": {"ran": False}}
    dchk = vchk = None
    if any(p.has_d for p in props):
        try:
            dchk = _Delivery(files, incdirs, defines, design["top"], by_path, work / "delivery")
            legs["delivery"] = {"ran": True, "tool": "slang", "baseline_errors": len(dchk.baseline)}
        except Exception as e:  # pyslang itself failed: nothing can be said about any property
            legs["delivery"]["reason"] = f"slang 编译失败：{e}"
    else:
        legs["delivery"]["reason"] = "没有交付版属性"
    if not any(p.has_v for p in props):
        legs["verification"]["reason"] = "没有验证版属性"
    elif not yosys_available():
        legs["verification"]["reason"] = "没有 Yosys"
    elif n1.yosys_report.blocked:
        legs["verification"]["reason"] = "Yosys 读不了设计（见 N1 基线）"
    else:
        scan = n2b_scan(files, design["top"], incdirs, defines, root)
        warn: list[str] = []
        env_lines = []
        if norm.get("resets"):
            act = " && ".join(f"!{r['name']}" if r["active"] == "low" else r["name"] for r in norm["resets"])
            env_lines.append(f"  always @* if ($initstate) assume({act});")
        clocks = norm.get("clocks", [])
        env_lines += _assume_lines(norm.get("assumes", []), clocks[0] if clocks else None, warn)
        ctx = dict(files=files, include_dirs=incdirs, defines=defines, top=design["top"], root=root, scan=scan)
        vchk = _Verification(ctx, {p.rendered.prop.module for p in props if p.has_v}, env_lines,
                             work / "verification", int(params["n6_timeout"]))
        hard = [d for d in vchk.baseline if d.code in ("error", "timeout")]
        if hard:
            legs["verification"]["reason"] = "Yosys 读不了设计：" + str(hard[0])
            vchk = None
        else:
            legs["verification"] = {"ran": True, "tool": "yosys"}
    for p in props:     # a leg that is not checked stays as N5 rendered it, unjudged
        if p.has_d and dchk:
            p.d_ok = True
        if p.has_v and vchk:
            p.v_ok = True

    # Steps 1-4
    budget = [int(params["n6_bisect_budget"])]
    rounds: list[dict] = []
    env_diags: list[Diag] = []
    rnd = 0
    while True:
        live_d = [p for p in props if p.d_ok and dchk]
        live_v = [p for p in props if p.v_ok and vchk and p.verification]
        for p in props:
            if p.v_ok and vchk and not p.verification:    # fallback could not render it
                p.v_ok = False
        fd, ld = _check("delivery", dchk, live_d, budget) if live_d else ({}, [])
        fv, lv = _check("verification", vchk, live_v, budget) if live_v else ({}, [])
        if ENV in fv:
            env_diags += fv.pop(ENV)
        rounds.append({"round": rnd, "delivery": {"checked": len(live_d), "failed": sorted(fd)},
                       "verification": {"checked": len(live_v), "failed": sorted(fv)}})
        if not fd and not fv:
            break
        again = False
        for p in props:
            for leg, fails, attr in (("delivery", fd, "d"), ("verification", fv, "v")):
                if p.name not in fails:
                    continue
                var = getattr(p, f"{attr}_variant")
                p.history.append({"round": rnd, "leg": leg, "variant": var, "diags": [str(d) for d in fails[p.name]]})
                if rnd < rounds_max and var < V.MAX_ROUND:
                    setattr(p, f"{attr}_variant", var + 1)
                    again = True
                else:
                    setattr(p, f"{attr}_ok", False)
                    again = True     # recompile without it: the final files must be what passed
            _render(p)
        rnd += 1
        if not again or rnd > rounds_max + 3:
            break
    if vchk:
        legs["verification"]["yosys_runs"] = vchk.runs
    legs["bisect_left"] = budget[0]
    if env_diags:
        log.warn("N6_ENV", "顶层环境（复位假设或用户 assume）Yosys 读不进去，N7 前需要修正：" + str(env_diags[0]))

    # Re-test changed verification texts (same vector as N5)
    changed = [p for p in props if p.v_variant > 0 and p.v_ok]
    if changed and n5.diff.get("ran") and params["n6_rediff"]:
        setup = diff_setup(n4, _n5_params(n5))
        if isinstance(setup, str):
            log.warn("N6_NO_REDIFF", f"重渲染的验证版未重做差分测试：{setup}")
        else:
            import copy
            rs = [copy.copy(p.rendered) for p in changed]
            var = {p.name: p.v_variant for p in changed}
            info = diff_mod.run(rs, workdir=work / "rediff", params=_n5_params(n5), **setup,
                                render=lambda q, lab: V.verification(q, lab, var.get(q.id, 0)))
            for p, r in zip(changed, rs):
                p.rediff = r.diff
            bad = [p.name for p in changed if p.rediff.get("status") == "mismatch"]
            if bad:
                log.warn("N6_REDIFF_MISMATCH", "重渲染后差分测试不一致，不进入 N7：" + "、".join(bad[:5]))
            legs["rediff"] = {"ran": True, "props": len(changed), "sim_error": info.get("sim_error"),
                              "formal_error": info.get("formal_error")}
    elif changed:
        legs["rediff"] = {"ran": False, "reason": "N5 未做差分测试" if not n5.diff.get("ran") else "已关闭（n6_rediff: false）"}

    errs = [p for p in props if p.status == "ENV_ERROR"]
    if errs:
        log.warn("N6_ENV_ERROR", f"{len(errs)} 条属性重渲染 {rounds_max} 轮后仍编译不过，标 ENV_ERROR，不进入 N7："
                 + "、".join(p.name for p in errs[:5]))
    for leg in ("delivery", "verification"):
        if not legs[leg]["ran"] and legs[leg].get("reason") and any(getattr(p, f"has_{leg[0]}") for p in props):
            log.warn("N6_LEG_SKIPPED", f"{'交付版' if leg == 'delivery' else '验证版'}未检查：{legs[leg]['reason']}")

    # Final files: exactly what the last round compiled
    ok_d = [p for p in props if p.d_ok]
    ok_v = [p for p in props if p.v_ok]
    if dchk:
        sva, bind, _s, _b = dchk.files_for(ok_d)
    else:
        sva, bind = n5.delivery, n5.bind
    if vchk:
        incs, _m = vchk.incs(ok_v)
        verif = {m: t for m, t in incs.items() if "vm_n6_" in t}
    else:
        verif = dict(n5.verification)

    res = N6Result(n5, props, rounds, sva, bind, verif, legs, [w.to_dict() for w in log.warnings], ih, "")
    res.n6_hash = tagged_hash("n6_result", res.result_dict())
    if write:
        from verimine.n6.report import markdown
        from verimine.n6.store import write_run
        res.db = write_run(workspace / "verimine.db", res)
        run_dir = workspace / "runs" / n0.hashes["run_hash"]
        (run_dir / "n6_verification").mkdir(parents=True, exist_ok=True)
        (run_dir / SVA).write_text(sva, encoding="utf-8")
        (run_dir / BIND).write_text(bind, encoding="utf-8")
        for mod, text in verif.items():
            (run_dir / "n6_verification" / f"vm_n6_{mod}.svh").write_text(text, encoding="utf-8")
        (run_dir / "n6.json").write_text(json.dumps(res.to_dict(), indent=2, ensure_ascii=False, sort_keys=True,
                                                    default=str) + "\n", encoding="utf-8")
        (run_dir / "n6_report.md").write_text(markdown(res), encoding="utf-8")
    return res


def _n5_params(n5: N5Result) -> dict:
    from verimine.n5.pipeline import DEFAULTS as N5D
    norm = n5.n4.n3.n2.n1.n0.normalized
    return N5D | {k: v for k, v in norm.get("run", {}).get("params", {}).items() if k in N5D}
