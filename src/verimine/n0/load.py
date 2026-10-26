"""N0 steps 0-3: entry check, YAML + .f merge, validation, normalization."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from ruamel.yaml import YAML, YAMLError

from verimine.config import IDENT_RE, Config
from verimine.issues import Issue, IssueLog, StageError
from verimine.n0.filelist import FileList, parse_filelist

DEFAULT_LIBEXT = [".v", ".sv"]
EXIT_NOT_CONFIRMED = 2


@dataclass
class Loaded:
    config_path: Path
    root: Path  # resolved, absolute
    config: Config
    normalized: dict[str, Any]
    library_dir_files: list[str]  # files pulled in by -y, relative to root
    log: IssueLog = field(default_factory=IssueLog)


def read_yaml(path: Path) -> dict:
    # YAML 1.2 (ruamel default): `mode: off` stays the string "off" instead of becoming False.
    try:
        data = YAML(typ="safe").load(path.read_text(encoding="utf-8"))
    except (OSError, YAMLError) as e:
        raise StageError([Issue("N0_YAML", f"无法读取 YAML：{e}", str(path))])
    if not isinstance(data, dict):
        raise StageError([Issue("N0_YAML", "YAML 顶层必须是映射", str(path))])
    return data


def entry_check(data: dict, where: str) -> None:
    """Step 0. Runs on the raw dict so a draft is rejected for the right reason even if incomplete."""
    issues = []
    status = data.get("status", "draft")
    if status != "confirmed":
        issues.append(Issue(
            "N0_NOT_CONFIRMED",
            f"status 为 {status!r}，N0 只接受 confirmed。请先完成 N0a 的确认（verimine confirm）",
            where,
        ))
    unresolved = data.get("unresolved") or []
    if unresolved:
        items = "；".join(str(u) for u in unresolved)
        issues.append(Issue("N0_UNRESOLVED", f"仍有 {len(unresolved)} 项未解决：{items}", where))
    if issues:
        raise StageError(issues, exit_code=EXIT_NOT_CONFIRMED)


def _schema_issues(e: ValidationError, where: str):
    for err in e.errors():
        loc = ".".join(str(x) for x in err["loc"])
        yield Issue("N0_SCHEMA", f"{loc}: {err['msg']}", where)


class _Paths:
    """Resolves paths against root, enforcing existence, readability and containment."""

    def __init__(self, root: Path, log: IssueLog):
        self.root = root
        self.log = log

    def check(self, p: Path, origin: str, kind: str = "file") -> str | None:
        p = p if p.is_absolute() else self.root / p
        real = p.resolve()
        if not real.is_relative_to(self.root):
            self.log.error("N0_PATH_ESCAPES_ROOT", f"路径跑出了 root：{p}", origin)
            return None
        if kind == "file":
            if not real.is_file():
                self.log.error("N0_FILE_MISSING", f"文件不存在：{p}", origin)
                return None
            if not os.access(real, os.R_OK):
                self.log.error("N0_FILE_UNREADABLE", f"文件不可读：{p}", origin)
                return None
        elif not real.is_dir():
            self.log.error("N0_DIR_MISSING", f"目录不存在：{p}", origin)
            return None
        return real.relative_to(self.root).as_posix()


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def _define_value(v: Any) -> str | None:
    # `WIDTH: 8` and `WIDTH: "8"` mean the same `define and must hash the same.
    return None if v is None else str(v)


def load(config_path: Path, env: dict[str, str] | None = None) -> Loaded:
    config_path = Path(config_path)
    where = str(config_path)
    data = read_yaml(config_path)
    entry_check(data, where)
    try:
        cfg = Config.model_validate(data)
    except ValidationError as e:
        raise StageError(list(_schema_issues(e, where)))

    log = IssueLog()
    root = (config_path.parent / cfg.design.root).resolve()
    if not root.is_dir():
        raise StageError([Issue("N0_DIR_MISSING", f"root 不存在：{root}", f"{where}: design.root")])
    paths = _Paths(root, log)
    d = cfg.design

    if not d.top:
        log.error("N0_TOP_MISSING", "design.top 未填写", where)

    # Step 1: expand .f files. Their entries come first, YAML entries are appended after.
    fl = FileList()
    for f in d.filelist:
        parse_filelist(root / f, root, log, env=env, out=fl)

    ywhere = lambda key: f"{where}: design.{key}"  # noqa: E731
    files = [(p, o) for p, o in fl.files] + [(Path(p), ywhere("files")) for p in d.files]
    lib_files = fl.library_files + [(Path(p), ywhere("library_files")) for p in d.library_files]
    inc_dirs = fl.include_dirs + [(Path(p), ywhere("include_dirs")) for p in d.include_dirs]
    lib_dirs = fl.library_dirs + [(Path(p), ywhere("library_dirs")) for p in d.library_dirs]

    # Step 2: validation. Same file twice is an error even when spelled differently.
    seen: dict[tuple[int, int], str] = {}
    norm_files, norm_lib_files = [], []
    for group, out in ((files, norm_files), (lib_files, norm_lib_files)):
        for p, origin in group:
            rel = paths.check(p, origin)
            if rel is None:
                continue
            st = (root / rel).stat()
            key = (st.st_dev, st.st_ino)
            if key in seen:
                log.error("N0_DUP_FILE", f"文件重复出现：{rel}（首次出现于 {seen[key]}）", origin)
                continue
            seen[key] = origin
            out.append(rel)
    if not norm_files and not log.errors:
        log.error("N0_NO_FILES", "没有任何源文件（design.files 和 design.filelist 都为空）", where)

    norm_inc = _dedupe([r for p, o in inc_dirs if (r := paths.check(p, o, "dir")) is not None])
    norm_lib_dirs = _dedupe([r for p, o in lib_dirs if (r := paths.check(p, o, "dir")) is not None])
    libext = _dedupe(fl.libext + d.libext) or DEFAULT_LIBEXT

    defines: dict[str, str | None] = {}
    origins: dict[str, str] = {}
    for name, value, origin in fl.defines + [(k, v, ywhere("defines")) for k, v in d.defines.items()]:
        value = _define_value(value)
        if not re.match(IDENT_RE, name):
            log.error("N0_BAD_DEFINE", f"宏名不合法：{name!r}", origin)
            continue
        if name in defines and defines[name] != value:
            log.warn("N0_DEFINE_OVERRIDE",
                     f"宏 {name} 由 {defines[name]!r} 改为 {value!r}（先前定义于 {origins[name]}）", origin)
        defines[name] = value
        origins[name] = origin

    _check_clocks_resets(cfg, log, where)

    names = [a.name for a in cfg.assumes]
    for n in sorted({n for n in names if names.count(n) > 1}):
        log.error("N0_DUP_ASSUME", f"假设 {n} 重复定义", f"{where}: assumes")

    waveforms = [r for w in cfg.waveforms if (r := paths.check(Path(w), f"{where}: waveforms")) is not None]

    lib_dir_files = sorted(
        (Path(dr) / f.name).as_posix()
        for dr in norm_lib_dirs
        for f in (root / dr).iterdir()
        if f.is_file() and f.suffix in libext
    )
    log.raise_if_errors()

    # Step 3: the normalized object. Every list whose order carries no meaning is sorted, so
    # equivalent configs serialize identically. status / unresolved are gone from here on.
    normalized = {
        "schema_version": cfg.schema_version,
        "design": {
            "name": d.name or d.top,
            "top": d.top,
            "files": norm_files,  # compile order: kept as given
            "include_dirs": norm_inc,  # search order: kept as given
            "defines": dict(sorted(defines.items())),
            "parameters": dict(sorted(d.parameters.items())),
            "language": d.language,
            "blackbox": sorted(set(d.blackbox)),
            "library_files": norm_lib_files,
            "library_dirs": norm_lib_dirs,
            "libext": libext,
        },
        "clocks": sorted((c.model_dump() for c in cfg.clocks), key=lambda c: c["name"]),
        "resets": sorted((r.model_dump() for r in cfg.resets), key=lambda r: r["name"]),
        "assumes": sorted((a.model_dump() for a in cfg.assumes), key=lambda a: a["name"]),
        "waveforms": waveforms,
        "llm": cfg.llm.model_dump(),
        "run": cfg.run.model_dump(),
        "env": cfg.env.model_dump(),
    }
    return Loaded(config_path, root, cfg, normalized, lib_dir_files, log)


def _check_clocks_resets(cfg: Config, log: IssueLog, where: str) -> None:
    clocks = [c.name for c in cfg.clocks]
    resets = [r.name for r in cfg.resets]
    for kind, names in (("时钟", clocks), ("复位", resets)):
        for n in sorted({n for n in names if names.count(n) > 1}):
            log.error("N0_DUP_SIGNAL", f"{kind} {n} 重复定义", where)
    for n in sorted(set(clocks) & set(resets)):
        log.error("N0_CLOCK_IS_RESET", f"{n} 同时被声明为时钟和复位", where)
    for r in cfg.resets:
        if r.clock is not None and r.clock not in clocks:
            log.error("N0_RESET_CLOCK_UNKNOWN", f"复位 {r.name} 引用了未声明的时钟 {r.clock}", where)
    if not clocks:
        log.warn("N0_NO_CLOCK", "没有声明时钟；断言挖掘需要至少一个时钟", where)
    elif len(set(clocks)) > 1:
        log.warn("N0_MULTICLOCK", "声明了多个时钟，多时钟域超出 MVP 范围，N1 会给出 UNSUPPORTED_MULTICLOCK", where)
