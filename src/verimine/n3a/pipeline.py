"""N3a: assumption mining. Input constraints (assumes) for formal verification and for
generated stimulus, so neither works on illegal inputs.

  1-3  candidates for top inputs and key submodule inputs, from N2 interface events and from
       the input behavior in the user's traces; each checked against those traces
  4    over-strength check with N2b's covers
  5    user confirmation: accepted assumptions go into the config's `assumes` (and so into
       build_hash); rejections are remembered
  6    assume-guarantee for submodule assumptions: DISCHARGED / OPEN
  7    storage: the assumption table with expression, source, confidence and status

Entry points: run_n3a(config_path, ...) and apply_decisions(config_path, result, ...).
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

from verimine.config import Config
from verimine.hashing import tagged_hash
from verimine.issues import Issue, IssueLog, StageError
from verimine.n2b.checks import ALL as N2B_CHECKS, Ctx
from verimine.n2b.engine import find_tools
from verimine.n2b.scan import scan as n2b_scan
from verimine.n3 import N3Result, run_n3
from verimine.n3.pipeline import _design, _from_wave
from verimine.n3a import cands as C
from verimine.n3a import formal
from verimine.n3a.store import load_decisions, record_decisions, write_run

N3A_VERSION = 1
DEFAULTS = {"n3a_min_support": 3, "n3a_min_pulses": 3, "n3a_max_mutex": 16,
            "n3a_depth": 20, "n3a_timeout": 120}
_TYPE_ORDER = list(C.TYPES)


@dataclass
class N3aResult:
    n3: N3Result
    cands: list[C.Cand]
    strength: dict
    env_missing: list[dict]
    warnings: list[dict]
    input_hash: str
    n3a_hash: str
    db: dict = field(default_factory=dict)

    @property
    def top(self) -> list[C.Cand]:
        return [c for c in self.cands if c.scope == self.n3.n2.n1.elaboration.top_path]

    @property
    def sub(self) -> list[C.Cand]:
        return [c for c in self.cands if c not in self.top]

    def counts(self) -> dict:
        out: dict[str, int] = {}
        for c in self.cands:
            out[c.status] = out.get(c.status, 0) + 1
        return dict(sorted(out.items()))

    def result_dict(self) -> dict:
        return {"cands": [_cand_dict(c, full=False) for c in self.cands], "env_missing": self.env_missing}

    def to_dict(self) -> dict:
        return {"stage": "N3a", "hashes": self.n3.n2.n1.n0.hashes, "n3_input_hash": self.n3.input_hash,
                "traces_hash": self.n3.traces_hash, "input_hash": self.input_hash, "n3a_hash": self.n3a_hash,
                "counts": self.counts(), "assumptions": [_cand_dict(c) for c in self.cands],
                "strength": self.strength, "env_missing": self.env_missing, "warnings": self.warnings,
                "db": self.db}


def _cand_dict(c: C.Cand, full: bool = True) -> dict:
    d = asdict(c) | {"key": c.key, "type_name": c.type_name, "recommend_text": C.RECOMMEND[c.recommend]}
    if not full:
        for k in ("strength", "discharge"):  # engine paths and traces: not part of the result hash
            d[k] = {kk: vv for kk, vv in d[k].items() if kk not in ("trace",)}
    return d


def _config_rows(norm: dict, top: str, top_module: str) -> list[C.Cand]:
    out = []
    for a in norm.get("assumes", []):
        c = C.Cand("config", top, top_module, a["name"], a["expr"], confidence="", recommend="",
                   source="config", evidence=["用户在配置中给出"], status="ACCEPTED", reason="已在配置 assumes 中")
        out.append(c)
    return out


def run_n3a(
    config_path: Path,
    workspace: Path | None = None,
    n3_result: N3Result | None = None,
    env: dict | None = None,
    tools: dict | None = None,
    write: bool = True,
    formal_checks: bool = True,
    classify: bool = True,
) -> N3aResult:
    if n3_result is None:
        n3_result = run_n3(config_path, workspace=workspace, env=env, tools=tools, write=write)
    n3 = n3_result
    n2, n1, n0 = n3.n2, n3.n2.n1, n3.n2.n1.n0
    norm = n0.normalized
    root, design = n0.root, norm["design"]
    workspace = Path(workspace) if workspace else n0.workspace
    params = DEFAULTS | {k: v for k, v in norm.get("run", {}).get("params", {}).items() if k in DEFAULTS}
    log = IssueLog()
    d, widths, _toggles = _design(n2, norm, log)
    top = n1.elaboration.top_path
    by_path = {i.path: i for i in n1.elaboration.instances}
    top_module = by_path[top].module
    db = workspace / "verimine.db"
    design_id = f"{design['name']}/{design['top']}"
    decisions = load_decisions(db, design_id)

    # Steps 1-3
    special = {p: set(n2.vdg.clocks.get(p, set())) | set(n2.vdg.resets.get(p, {})) for p in by_path}
    modeled = set(n2.vdg.modeled)
    cs = [c for c in C.structural(n2, special) if c.scope in modeled]
    user = [c for c in n3.cases if c.legality == "user"]
    if not user:
        log.warn("N3A_NO_USER_TRACES", "没有用户轨迹（波形或 testbench）：只有来自 N2 接口事件的结构候选；"
                                      "互斥、独热、复位值、保持周期、取值范围需要用户轨迹")
    cs = C._dedupe(cs + C.mine(user, d, top_module, params))
    config = _config_rows(norm, top, top_module)
    config_keys = {c.key: c for c in config}
    for c in cs:
        if c.key in config_keys:
            c.status, c.reason, c.source = "ACCEPTED", "已在配置 assumes 中", c.source + "+config"
            config_keys.pop(c.key).status = "MERGED"
    config = [c for c in config if c.status != "MERGED"]

    for c in cs:
        w = widths.get(c.scope, {})
        c.trace = C.check(c, user, w) if user else {}
        if c.trace.get("violations") and c.status != "ACCEPTED":
            c.status = "REJECTED"
            c.reason = f"用户轨迹违反 {c.trace['violations']} 次（首次在 {c.trace['first']['case']} 第 {c.trace['first']['row']} 行）"
        elif c.trace.get("support") and "trace" not in c.source:
            c.source += "+trace"
            c.evidence.append(f"用户轨迹中前件成立 {c.trace['support']} 次，从未违反")
        dec = decisions.get(c.key)
        if dec and dec["status"] == "REJECTED" and c.status == "PROPOSED":
            c.status, c.reason = "REJECTED", "用户已拒绝" + (f"：{dec['reason']}" if dec["reason"] else "")
    rows = config + cs
    _assign_ids(rows, top)

    # Steps 4 and 6 need the engine
    strength_info: dict = {"ran": False}
    env_info: list[dict] = []
    engine_ok = formal_checks and find_tools()[0] and not n1.yosys_report.blocked
    if formal_checks and not engine_ok:
        log.warn("N3A_NO_ENGINE", "没有形式引擎或 Yosys 读不了设计：跳过过强检查和层级复用证明")
    files = [root / f for f in design.get("files", [])]
    incdirs = [root / x for x in design.get("include_dirs", [])]
    defines = {k: (str(v) if v is not None else None) for k, v in design.get("defines", {}).items()}
    ih = tagged_hash("n3a_input", {
        "n3_input_hash": n3.input_hash, "traces_hash": n3.traces_hash, "build_hash": n0.hashes["build_hash"],
        "env_hash": n0.hashes["env_hash"], "params": params, "formal": bool(engine_ok), "classify": classify,
        "rejected": sorted(k for k, v in decisions.items() if v["status"] == "REJECTED"),
        "n3a_version": N3A_VERSION,
    })
    work = workspace / "n3a" / ih[:16]
    if engine_ok:
        sc = n2b_scan(files, design["top"], incdirs, defines, root)
        cx = Ctx(n2, sc, {})
        for fn in N2B_CHECKS:
            fn(cx)
        covers = [p for p in cx.props if p.kind == "cover" and p.check in formal.STRENGTH_COVERS]
        ctx = dict(instances=n1.elaboration.instances, files=files, include_dirs=incdirs, defines=defines,
                   top=design["top"], root=root, scan=sc, top_resets=norm.get("resets", []),
                   top_clock={"name": d.clock, "edge": d.edge})
        depth, timeout = int(params["n3a_depth"]), int(params["n3a_timeout"])
        tops = [c for c in rows if c.scope == top and c.status in ("PROPOSED", "ACCEPTED")]
        combined = [c for c in tops if c.status == "ACCEPTED" or c.recommend == "accept"]
        strength_info = formal.strength(tops, combined, covers, ctx, work / "strength", depth, timeout) | {"ran": True}
        for c in tops:
            s = c.strength
            if s.get("error"):
                log.warn("N3A_STRENGTH_ERROR", f"{c.name}：过强检查失败（{s['error'][:120]}）")
            elif s["lost_key"]:
                msg = f"{c.name}（{c.text}）过强：使关键状态不可达 —— " + "；".join(s["lost_key"][:3])
                if c.status == "ACCEPTED":
                    log.warn("N3A_OVERSTRONG", msg + "。它在配置中，建议删除或改写")
                else:
                    c.status, c.reason = "REJECTED", "过强：" + "；".join(s["lost_key"][:3])
            elif s["lost_branch"]:
                log.warn("N3A_OVERSTRONG_BRANCH", f"{c.name}（{c.text}）使 {len(s['lost_branch'])} 个分支不可达："
                                                  + "；".join(s["lost_branch"][:3]) + "。请确认这些分支只服务于非法输入")
                if c.recommend == "accept" and c.status == "PROPOSED":
                    c.recommend = "confirm"
        comb = strength_info.get("combined")
        if comb and comb["lost_key"]:
            log.warn("N3A_OVERSTRONG_COMBINED", "配置中的假设与建议接受的假设合在一起过强：" + "；".join(comb["lost_key"][:3]))
        subs = [c for c in rows if c.scope != top and c.status == "PROPOSED"]
        formal.discharge(subs, cx, ctx, list(norm.get("assumes", [])), work / "guarantee", depth, timeout)
        if classify:
            from verimine.n2b import run_n2b
            n2b = run_n2b(config_path, workspace=workspace, n2_result=n2, write=write)
            env_info = formal.env_missing(n2b, rows, d, _from_wave, lambda s: widths.get(s, {}))
            for e in env_info:
                if e["class"] == "env_missing":
                    names = {c.id: c.name for c in rows}
                    log.warn("N3A_ENV_MISSING", f"N2b 断言 {e['prop']}（{e['inst']}：{e['message']}）的反例违反了候选假设 "
                                                + "、".join(names[i] for i in e["violated"]) + "：属于缺环境假设（env_missing）")
    else:
        for c in rows:
            if c.scope != top and c.status == "PROPOSED":
                c.status, c.reason = "OPEN", "未做层级复用证明（没有形式引擎）"
    for c in rows:
        if c.scope != top and c.status == "PROPOSED":
            c.status = "OPEN"
    opened = [c for c in rows if c.status == "OPEN"]
    if opened:
        log.warn("N3A_OPEN", f"{len(opened)} 条子模块假设没有被上层约束保证（OPEN），见报告")

    res = N3aResult(n3, rows, strength_info, env_info, [w.to_dict() for w in log.warnings], ih, "")
    res.n3a_hash = tagged_hash("n3a_result", res.result_dict())
    if write:
        res.db = write_run(db, res)
        run_dir = workspace / "runs" / n0.hashes["run_hash"]
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "n3a.json").write_text(json.dumps(res.to_dict(), indent=2, ensure_ascii=False, sort_keys=True)
                                          + "\n", encoding="utf-8")
        from verimine.n3a.report import markdown
        (run_dir / "n3a_report.md").write_text(markdown(res), encoding="utf-8")
    return res


def _assign_ids(rows: list[C.Cand], top: str) -> None:
    rows.sort(key=lambda c: (c.scope != top, c.type != "config", _TYPE_ORDER.index(c.type), c.scope, c.text))
    for i, c in enumerate(rows, 1):
        c.id = f"N3A_{i:04d}"


# --------------------------------------------------------------------------- #
# Step 5: confirmation
# --------------------------------------------------------------------------- #

def apply_decisions(config_path: Path, res: N3aResult, accept: list[str] = (), reject: list[str] = (),
                    accept_high: bool = False, reason: str = "") -> dict:
    """Writes accepted assumptions into the config's `assumes` (round-trip, comments kept) and
    records rejections. Returns {"accepted": [...], "rejected": [...], "warnings": [...]}."""
    by_id = {c.id: c for c in res.cands}
    unknown = [i for i in [*accept, *reject] if i not in by_id]
    if unknown:
        raise StageError([Issue("N3A_UNKNOWN_ID", f"没有这些候选：{', '.join(unknown)}", str(config_path))])
    top = res.n3.n2.n1.elaboration.top_path
    warn, take = [], []
    for i in accept:
        c = by_id[i]
        if c.scope != top:
            raise StageError([Issue("N3A_NOT_TOP", f"{i} 是子模块 {c.scope} 的假设，不能写进顶层配置", str(config_path))])
        if c.status == "ACCEPTED":
            continue
        if c.status == "REJECTED":
            warn.append(f"{i} 已被拒绝（{c.reason}），按你的要求仍然接受")
        take.append(c)
    if accept_high:
        take += [c for c in res.top if c.status == "PROPOSED" and c.recommend == "accept" and c not in take]
    path = Path(config_path)
    y = YAML()
    y.preserve_quotes = True
    data = y.load(path.read_text(encoding="utf-8"))
    seq = data.get("assumes")
    if seq is None:
        seq = CommentedSeq()
        data["assumes"] = seq
    names = {a["name"] for a in seq}
    for c in take:
        name, k = c.name, 2
        while name in names:
            name, k = f"{c.name}_{k}", k + 1
        names.add(name)
        m = CommentedMap(name=name, expr=c.text)
        m.fa.set_flow_style()
        seq.append(m)
        seq.yaml_add_eol_comment(f"N3a {c.id} {c.type_name}，{c.confidence or '-'}置信", len(seq) - 1)
    Config.model_validate(json.loads(json.dumps(data)))
    if take:
        with path.open("w", encoding="utf-8") as fh:
            y.dump(data, fh)
    rej = [by_id[i] for i in reject]
    workspace = res.n3.n2.n1.n0.workspace
    d = res.n3.n2.n1.n0.normalized["design"]
    record_decisions(workspace / "verimine.db", f"{d['name']}/{d['top']}",
                     [(c.key, "ACCEPTED", "") for c in take] + [(c.key, "REJECTED", reason) for c in rej])
    for c in take:
        c.status, c.reason = "ACCEPTED", "已写入配置 assumes"
    for c in rej:
        c.status, c.reason = "REJECTED", "用户拒绝" + (f"：{reason}" if reason else "")
    return {"accepted": [c.id for c in take], "rejected": [c.id for c in rej], "warnings": warn}
