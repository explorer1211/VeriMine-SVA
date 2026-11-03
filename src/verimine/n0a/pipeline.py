"""N0a: complete the YAML configuration (draft + inference report), and confirm it.

Step 0 decides what to do from what the user gave:
  YAML, complete               -> skip, go straight to N0
  .f, no YAML                  -> YAML references the .f via design.filelist; infer top,
                                  clocks/resets, macro values, black boxes
  RTL only                     -> steps 1-9 infer files, order and include dirs, then the rest
  YAML with missing fields     -> fill only the missing ones; disagreements go to the report

status, unresolved and the report never enter any hash: only the confirmed YAML content does,
so changing the inference algorithm never invalidates past results.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from verimine.hashing import file_hash
from verimine.issues import Issue, IssueLog, StageError
from verimine.n0.filelist import parse_filelist
from verimine.n0a import check as check_mod
from verimine.n0a import clkrst, emit, graph, incdir, macros
from verimine.n0a.facts import extract_all
from verimine.n0a.scan import ScannedFile, scan

MAX_CHECK_ROUNDS = 3
HIGH, MEDIUM, LOW, CONFIRM = "高", "中", "低", "需确认"


@dataclass
class N0aResult:
    mode: str  # skip | rtl_only | filelist | fill_missing
    top: str | None
    config_path: Path | None
    report_path: Path | None
    filelist_path: Path | None
    document: Any
    report: dict
    unresolved: list[str] = field(default_factory=list)
    written: bool = False

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "top": self.top,
            "config": str(self.config_path) if self.config_path else None,
            "report": str(self.report_path) if self.report_path else None,
            "filelist": str(self.filelist_path) if self.filelist_path else None,
            "unresolved": self.unresolved,
            "written": self.written,
        }


def _field(value, basis: str, confidence: str, alternatives=None) -> dict:
    return {"value": value, "basis": basis, "confidence": confidence, "alternatives": alternatives or []}


# --------------------------------------------------------------------------- #
# Context shared by every top inferred from one run
# --------------------------------------------------------------------------- #

@dataclass
class _Ctx:
    root: Path
    graph: graph.Graph
    matches: dict
    copies: dict[str, list[str]]
    fixed_files: list[str] | None = None  # from .f / YAML files: never re-inferred
    known_incdirs: list[str] | None = None
    known_defines: dict[str, str | None] | None = None
    known_blackbox: list[str] = field(default_factory=list)


def _build_ctx(root: Path, workspace: Path | None, extra_files: list[str] = ()) -> _Ctx:
    sr = scan(root)
    have = {f.path for f in sr.files} | {c for cs in sr.copies.values() for c in cs}
    for rel in sorted(set(extra_files) - have):  # .f entries in directories the scan skips
        if (root / rel).is_file():
            sr.files.append(ScannedFile(rel, file_hash(root / rel)[0]))
    cache = (workspace / "n0a_cache") if workspace else None
    facts = extract_all(root, sr.files, cache)
    paths = sorted(facts)
    matches = incdir.resolve_all(facts, paths)
    return _Ctx(root, graph.build(facts, matches), matches, sr.copies)


def _rel(root: Path, p: Path) -> str:
    try:
        return p.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return p.as_posix()


# --------------------------------------------------------------------------- #
# Inference for one top
# --------------------------------------------------------------------------- #

def _included_headers(ctx: _Ctx, files: list[str]) -> tuple[list[str], list[incdir.IncludeMatch]]:
    """Headers reachable from the given files, and every include reference on the way."""
    seen, refs, todo = set(), [], list(files)
    while todo:
        f = todo.pop(0)
        if f in seen or f not in ctx.graph.facts:
            continue
        seen.add(f)
        for inc in ctx.graph.facts[f].includes:
            m = ctx.matches[(f, inc)]
            refs.append(m)
            if m.chosen:
                todo.append(m.chosen)
    return sorted(seen - set(files)), refs


def _infer_top(ctx: _Ctx, top: str, top_field: dict, user: dict) -> tuple[dict, dict, list[str]]:
    """Returns (inferred design/clock/reset values, report, unresolved). `user` holds fields the
    user already wrote; those are kept, and only disagreements are reported."""
    g, root = ctx.graph, ctx.root
    unresolved: list[str] = []
    report: dict[str, Any] = {"fields": {"design.top": top_field}, "conflicts": []}
    blackbox = set(ctx.known_blackbox)
    extra: set[str] = set()

    def run_closure():
        return graph.closure(g, top, ctx.matches, extra, blackbox, ctx.fixed_files)

    cl = run_closure()

    # Black-box candidates: undefined, but named like a vendor primitive or memory macro.
    def triage_missing(names: dict[str, list[str]]) -> dict[str, list[str]]:
        rest = {}
        for name, refs in names.items():
            if graph.PRIMITIVE_RE.match(name):
                blackbox.add(name)
            else:
                rest[name] = refs
        return rest

    missing = triage_missing(cl.missing)

    # Step 7: include dirs.
    headers, inc_refs = _included_headers(ctx, cl.files)
    missing_inc = sorted({(m.name, m.includer) for m in inc_refs if not m.chosen})
    # Always inferred, so a user-written value can be compared; the user's value is what we use.
    ours_dirs = sorted({m.incdir for m in inc_refs if m.chosen})
    alts = sorted({a for m in inc_refs for a in m.alternatives})
    report["fields"]["design.include_dirs"] = _field(
        ours_dirs, "按 `include 引用在扫描到的文件中反推路径前缀", HIGH if not alts else CONFIRM, alts)
    dirs = ours_dirs if ctx.known_incdirs is None else list(ctx.known_incdirs)
    report["includes"] = [
        {"includer": m.includer, "name": m.name, "chosen": m.chosen, "incdir": m.incdir,
         "alternatives": m.alternatives}
        for m in sorted(inc_refs, key=lambda m: (m.includer, m.name))
    ]
    for name, includer in missing_inc:
        unresolved.append(f"include 文件 {name} 找不到（被 {includer} 引用）")

    # Step 8: macros.
    scope = [g.facts[f] for f in (*cl.files, *headers) if f in g.facts]
    known_defs = dict(ctx.known_defines or {})
    mrep = macros.classify(scope, set(known_defs))
    for name, users in sorted(mrep.value_missing.items()):
        unresolved.append(f"宏 {name} 需要取值（用于 {', '.join(users)}）")
    report["macros"] = {
        "value_missing": mrep.value_missing,
        "cond_missing": {k: {"files": v, "default": "不定义", "confidence": CONFIRM}
                         for k, v in mrep.cond_missing.items()},
        "flavour": [
            f"{m}：决定综合/仿真路径，形式验证一般应走可综合路径（通常定义 SYNTHESIS，不定义 SIMULATION）"
            for m in mrep.flavour
        ],
    }

    # Step 9: closed-loop elaboration, at most three rounds.
    rounds, chk = 0, None
    for rounds in range(1, MAX_CHECK_ROUNDS + 1):
        chk = check_mod.elaborate_draft(root, cl.files, dirs, known_defs, top, blackbox)
        changed = False
        for name, where in sorted(chk.unknown_modules.items()):
            defined = [p for p, _ in g.defs.get(name, []) if p not in g.headers]
            if defined and ctx.fixed_files is None and name not in extra:
                extra.add(name)
                changed = True
            elif graph.PRIMITIVE_RE.match(name) and name not in blackbox:
                blackbox.add(name)
                changed = True
            elif defined and ctx.fixed_files is not None:
                missing.setdefault(name, [])
                report.setdefault("outside_filelist", {})[name] = defined
            else:
                missing.setdefault(name, sorted(set(where)))
        for name in sorted(chk.unknown_packages):
            if name in g.defs and ctx.fixed_files is None and name not in extra:
                extra.add(name)
                changed = True
        if not changed:
            break
        cl = run_closure()
        missing.update(triage_missing(cl.missing))
        headers, inc_refs = _included_headers(ctx, cl.files)
        ours_dirs = sorted({m.incdir for m in inc_refs if m.chosen})
        if ctx.known_incdirs is None:
            dirs = ours_dirs

    report["fields"]["design.include_dirs"]["value"] = ours_dirs  # closure may have grown
    for name, refs in sorted(missing.items()):
        if name in blackbox:
            continue
        outside = report.get("outside_filelist", {}).get(name)
        if outside:
            unresolved.append(f"模块 {name} 不在 .f 中，但 {', '.join(outside)} 里有定义：是否漏了文件")
        else:
            by = f"（被 {', '.join(sorted(set(refs)))} 引用）" if refs else ""
            unresolved.append(f"模块 {name} 被引用但找不到定义{by}：可能是 SRAM 宏、FPGA 原语或漏了文件")
    for name in sorted(set(blackbox) - set(ctx.known_blackbox)):
        unresolved.append(f"{name}：是黑盒还是漏了文件")
    for name in sorted(chk.unknown_packages if chk else []):
        if name not in g.defs:
            unresolved.append(f"package {name} 找不到定义")
    for name, dup in sorted(cl.duplicates.items()):
        unresolved.append(
            f"模块 {name} 在多个文件中定义：已选 {dup['chosen']}，另有 {', '.join(dup['others'])}")
    for cyc in cl.cycles:
        unresolved.append(f"文件依赖出现环：{' -> '.join(cyc + cyc[:1])}")
    macro_names = set(mrep.value_missing)
    extra_macros = sorted(chk.undefined_macros - macro_names - set(known_defs)) if chk else []
    for m in extra_macros:
        unresolved.append(f"宏 {m} 未定义（elaboration 时发现）")
    if chk and chk.other_errors:
        e = chk.other_errors[0]
        hint = "（可能由未取值的宏引起）" if macro_names else ""
        unresolved.append(
            f"slang elaboration 仍有 {len(chk.other_errors)} 个错误{hint}，第一个：{e['message']}"
            + (f"（{e['where']}）" if e["where"] else ""))

    report["check"] = {
        "rounds": rounds,
        "clean": bool(chk and chk.clean),
        "remaining_errors": chk.other_errors[:50] if chk else [],
        "duplicates_seen_by_slang": sorted(chk.duplicates) if chk else [],
    }

    # Step 10: clocks and resets.
    cr = clkrst.infer(chk.compilation if chk else None, top)
    clocks = [{"name": c.name, "edge": c.edge} for c in cr.clocks]
    resets = [{"name": r.name, "active": r.active, "type": r.type} for r in cr.resets]
    if "clocks" not in user:
        if not clocks:
            unresolved.append("未能推断出时钟：top 的输入端口没有出现在任何 always_ff/always 的边沿敏感列表里")
        elif len(clocks) > 1:
            unresolved.append(
                f"推断出多个时钟 {', '.join(c['name'] for c in clocks)}：多时钟域超出 MVP 范围，"
                "N1 会对多时钟模块给出 UNSUPPORTED_MULTICLOCK 诊断")
    conf = HIGH if len(clocks) == 1 else CONFIRM
    report["fields"]["clocks"] = _field(
        clocks, "top 中 always_ff/always 敏感列表里来自输入端口的边沿信号", conf,
    )
    report["fields"]["resets"] = _field(
        resets, "敏感列表里且作为首个 if 条件的是异步复位；单时钟块首个 if 条件且名字像复位的是同步复位",
        HIGH if resets else LOW,
    )

    # Files, defines, black boxes.
    if ctx.fixed_files is None:
        order_basis = "从 top 沿实例、package、接口、宏依赖取闭包，按 package/宏定义先于使用做拓扑排序，同层按路径排序"
        report["fields"]["design.files"] = _field(cl.files, order_basis, HIGH if not cl.cycles else CONFIRM)
    report["fields"]["design.blackbox"] = _field(
        sorted(blackbox), "被引用但没有定义、名字像 FPGA 原语或存储器宏的模块", CONFIRM if blackbox else HIGH)
    report["fields"]["design.defines"] = _field(
        {}, "未定义的条件宏默认不定义；值宏需用户取值", CONFIRM if mrep.cond_missing else HIGH,
        sorted(mrep.cond_missing))
    report["missing_modules"] = {k: v for k, v in sorted(missing.items()) if k not in blackbox}
    report["duplicates"] = cl.duplicates
    report["cycles"] = cl.cycles
    report["macro_providers"] = cl.macro_providers
    v05 = sorted(f for f in cl.files if g.facts.get(f) and g.facts[f].verilog2005)
    fallback = sorted(f for f in cl.files if g.facts.get(f) and g.facts[f].fallback)
    report["parse"] = {
        "verilog2005": v05,
        "regex_fallback": fallback,
        "note": "verilog2005 中的文件按 SystemVerilog 解析失败，按 Verilog-2005 关键字集解析成功" if v05 else "",
    }

    inferred = {
        "files": cl.files,
        "include_dirs": ours_dirs,
        "defines": {},
        "blackbox": sorted(blackbox),
        "clocks": clocks,
        "resets": resets,
    }
    return inferred, report, _dedupe(unresolved)


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


# --------------------------------------------------------------------------- #
# Step 0 and output
# --------------------------------------------------------------------------- #

def _complete(doc: dict) -> bool:
    d = doc.get("design") or {}
    return (doc.get("status") == "confirmed" and not doc.get("unresolved")
            and bool(d.get("top")) and bool(d.get("files") or d.get("filelist"))
            and bool(doc.get("clocks")))


def _choose_tops(ctx: _Ctx, top: str | None) -> tuple[list[str], dict]:
    cands, tbs = graph.top_candidates(ctx.graph)
    info = {
        "candidates": [{"name": c.name, "file": c.file, "tree_size": c.size, "in_rtl_dir": c.in_rtl_dir}
                       for c in cands],
        "testbenches": tbs,
    }
    if top:
        if top not in ctx.graph.defs:
            raise StageError([Issue("N0A_TOP_UNKNOWN", f"指定的 top {top} 在扫描到的文件里没有定义")])
        return [top], info
    if ctx.fixed_files is not None:
        allowed = set(ctx.fixed_files)
        cands = [c for c in cands if c.file in allowed]
        info["candidates"] = [c for c in info["candidates"] if c["file"] in allowed]
    if not cands:
        raise StageError([Issue("N0A_NO_TOP", "找不到 top 候选（所有未被实例化的模块都像 testbench）；请用 --top 指定")])
    return [c.name for c in cands], info


def _top_field(name: str, info: dict, user_given: bool) -> dict:
    if user_given:
        return _field(name, "用户指定", HIGH)
    alts = [c["name"] for c in info["candidates"] if c["name"] != name]
    basis = "没被实例化、不像 testbench 的模块，按实例树大小和是否位于 rtl/src 目录排序"
    return _field(name, basis, HIGH if not alts else MEDIUM, alts)


def _out_names(base: Path, top: str, many: bool) -> tuple[Path, Path, Path]:
    stem = f"{base.stem}.{top}" if many else base.stem
    report = "n0a_report" + (f".{top}" if many else "") + ".json"
    return base.with_name(stem + base.suffix), base.with_name(report), base.with_name(stem + ".f")


def infer(
    root: Path | None = None,
    *,
    config: Path | None = None,
    filelist: Path | None = None,
    top: str | None = None,
    out: Path | None = None,
    export_f: bool = False,
    force: bool = False,
    write: bool = True,
    workspace: Path | None = None,
    env: dict[str, str] | None = None,
) -> list[N0aResult]:
    """Runs N0a. Returns one result per YAML draft (several when several tops are found)."""
    user_doc = None
    if config is not None:
        config = Path(config)
        if not config.is_file():
            raise StageError([Issue("N0A_NO_CONFIG", f"YAML 不存在：{config}")])
        user_doc = emit.load_yaml_rt(config)
        if not isinstance(user_doc, dict) or not isinstance(user_doc.get("design"), dict):
            raise StageError([Issue("N0A_YAML", "YAML 顶层必须是映射，并包含 design", str(config))])
        root = (config.parent / user_doc["design"].get("root", ".")).resolve()
        if _complete(user_doc):
            return [N0aResult("skip", user_doc["design"].get("top"), config, None, None, user_doc,
                              {"mode": "skip"}, [], False)]
    root = Path(root or ".").resolve()
    if not root.is_dir():
        raise StageError([Issue("N0A_NO_ROOT", f"目录不存在：{root}")])
    workspace = Path(workspace) if workspace else root / ".verimine"

    log = IssueLog()
    ud = dict(user_doc["design"]) if user_doc is not None else {}
    fl_paths = [root / f for f in ud.get("filelist", [])] + ([Path(filelist)] if filelist else [])
    fixed: list[str] | None = None
    fl_incdirs, fl_defines = [], {}
    if fl_paths:
        fixed = []
        for p in fl_paths:
            fl = parse_filelist(p if p.is_absolute() else Path.cwd() / p, root, log, env=env)
            fixed += [_rel(root, f) for f, _ in fl.files]
            fl_incdirs += [_rel(root, d) for d, _ in fl.include_dirs]
            fl_defines.update({k: v for k, v, _ in fl.defines})
        log.raise_if_errors()
    if ud.get("files"):
        fixed = (fixed or []) + list(ud["files"])

    mode = "fill_missing" if user_doc is not None else "filelist" if fl_paths else "rtl_only"
    ctx = _build_ctx(root, workspace if write else None, fixed or [])
    ctx.fixed_files = fixed
    if fl_paths or "include_dirs" in ud:
        ctx.known_incdirs = fl_incdirs + list(ud.get("include_dirs", []))
    if fl_paths or "defines" in ud:
        ctx.known_defines = {**fl_defines, **{k: (None if v is None else str(v))
                                              for k, v in (ud.get("defines") or {}).items()}}
    ctx.known_blackbox = list(ud.get("blackbox", []))

    user_top = top or ud.get("top")
    tops, top_info = _choose_tops(ctx, user_top)
    many = len(tops) > 1

    if out is None:
        out = config if config is not None else root / "verimine.yaml"
    out = Path(out)
    results = []
    for t in tops:
        top_field = _top_field(t, top_info, bool(user_top))
        user_fields = {k for k in ("clocks", "resets") if user_doc is not None and user_doc.get(k)}
        inferred, report, unresolved = _infer_top(ctx, t, top_field, {k: True for k in user_fields})
        report.update({"mode": mode, "top_candidates": top_info["candidates"],
                       "testbenches": top_info["testbenches"], "copies": ctx.copies})
        yaml_path, report_path, f_path = _out_names(out, t, many)
        if many:
            unresolved.insert(0, f"发现 {len(tops)} 个 top 候选（{', '.join(tops)}），每个各生成了一份草稿；只保留需要的那份")

        if user_doc is None:
            design: dict[str, Any] = {"name": t, "root": os.path.relpath(root, yaml_path.parent.resolve()).replace(os.sep, "/"), "top": t}
            if fl_paths:
                design["filelist"] = [_rel(root, p if p.is_absolute() else Path.cwd() / p) for p in fl_paths]
            else:
                design["files"] = inferred["files"]
                design["include_dirs"] = inferred["include_dirs"]
            design.update({"defines": {}, "parameters": {}, "language": "sv2017", "blackbox": inferred["blackbox"]})
            cfg = {"schema_version": 1, "status": "draft", "unresolved": unresolved, "design": design,
                   "clocks": inferred["clocks"], "resets": inferred["resets"]}
            doc = emit.draft_document(cfg)
            if yaml_path.exists() and not force and write:
                raise StageError([Issue("N0A_EXISTS", f"{yaml_path} 已存在；用 --config 只补缺字段，或加 --force 覆盖")])
        else:
            doc, unresolved = _fill_missing(user_doc, inferred, unresolved, report, t)

        report["unresolved"] = list(unresolved)
        emit.validate(doc)
        res = N0aResult(mode, t, yaml_path, report_path, None, doc, report, list(unresolved))
        if write:
            yaml_path.write_text(emit.dump_yaml(doc), encoding="utf-8")
            report_path.write_text(emit.dump_report(report), encoding="utf-8")
            if export_f and doc["design"].get("files"):
                f_path.write_text(emit.export_filelist(yaml_path.name, doc["design"]), encoding="utf-8")
                res.filelist_path = f_path
            res.written = True
        results.append(res)
    return results


def _fill_missing(doc, inferred: dict, unresolved: list[str], report: dict, top: str):
    """Only absent (or empty) fields are filled. A user value is never changed; when inference
    disagrees with it, that goes to report["conflicts"]."""
    d = doc["design"]
    filled = []

    def conflict(key, user, ours):
        if user != ours:
            report["conflicts"].append({"field": key, "user": user, "inferred": ours})

    if not d.get("top"):
        d["top"] = top
        filled.append("design.top")
    if not d.get("files") and not d.get("filelist"):
        d["files"] = inferred["files"]
        filled.append("design.files")
    if "include_dirs" not in d and not d.get("filelist"):
        d["include_dirs"] = inferred["include_dirs"]
        filled.append("design.include_dirs")
    elif "include_dirs" in d:
        conflict("design.include_dirs", list(d["include_dirs"]), inferred["include_dirs"])
    if "blackbox" not in d:
        d["blackbox"] = inferred["blackbox"]
        filled.append("design.blackbox")
    elif sorted(d["blackbox"]) != inferred["blackbox"]:
        conflict("design.blackbox", sorted(d["blackbox"]), inferred["blackbox"])
    for key in ("clocks", "resets"):
        if not doc.get(key):
            doc[key] = emit.flow_seq(inferred[key])
            filled.append(key)
        else:
            conflict(key, [dict(x) for x in doc[key]], inferred[key])

    existing = list(doc.get("unresolved") or [])
    merged = _dedupe(existing + unresolved)
    doc["unresolved"] = merged
    if filled or merged:
        doc["status"] = "draft"
    report["filled"] = filled
    return doc, merged


# --------------------------------------------------------------------------- #
# Step 14: confirm
# --------------------------------------------------------------------------- #

def confirm(
    config: Path,
    *,
    resolve: list[int] | None = None,
    resolve_all: bool = False,
    defines: dict[str, str | None] | None = None,
    ask: Callable[[str], bool] | None = None,
    write: bool = True,
) -> list[str]:
    """Works through unresolved items. Returns what is still unresolved; when nothing is, status
    becomes confirmed. `ask(item)` -> True marks an item as handled (interactive mode)."""
    config = Path(config)
    doc = emit.load_yaml_rt(config)
    items = list(doc.get("unresolved") or [])
    keep = []
    for name, value in (defines or {}).items():
        doc["design"].setdefault("defines", {})[name] = value
    for i, item in enumerate(items, 1):
        handled = (
            resolve_all
            or (resolve and i in resolve)
            or any(item.startswith(f"宏 {n} ") for n in (defines or {}))
            or (ask is not None and ask(item))
        )
        if not handled:
            keep.append(item)
    doc["unresolved"] = keep
    doc["status"] = "draft" if keep else "confirmed"
    try:
        emit.validate(doc)
    except Exception as e:
        raise StageError([Issue("N0A_SCHEMA", f"YAML 不符合配置模型：{e}", str(config))])
    if write:
        config.write_text(emit.dump_yaml(doc), encoding="utf-8")
    return keep
