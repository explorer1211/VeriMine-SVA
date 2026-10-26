"""Parser for simulator-style .f files.

Supported: -f / -F (nested), +incdir+, +define+, -v, -y, +libext+, $VAR / ${VAR} / $(VAR),
// # /* */ comments, quoted paths.

Relative paths follow Verilator semantics: inside a file reached via -f they are relative to the
project root (the "working directory" of the build); inside a file reached via -F they are
relative to that .f file's own directory.
"""
from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path

from verimine.issues import IssueLog

_ENV_RE = re.compile(r"\$\{(\w+)\}|\$\((\w+)\)|\$(\w+)")
_LINE_COMMENT_RE = re.compile(r"(^|\s)(//|#).*$")
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)


@dataclass
class FileList:
    files: list[tuple[Path, str]] = field(default_factory=list)
    include_dirs: list[tuple[Path, str]] = field(default_factory=list)
    defines: list[tuple[str, str | None, str]] = field(default_factory=list)
    library_files: list[tuple[Path, str]] = field(default_factory=list)
    library_dirs: list[tuple[Path, str]] = field(default_factory=list)
    libext: list[str] = field(default_factory=list)


def _expand_env(token: str, env: dict[str, str], log: IssueLog, origin: str) -> str:
    def sub(m: re.Match) -> str:
        name = m.group(1) or m.group(2) or m.group(3)
        if name not in env:
            log.error("N0_FILELIST_ENV", f"环境变量 {name} 未定义", origin)
            return m.group(0)
        return env[name]

    return _ENV_RE.sub(sub, token)


def _tokens(path: Path, log: IssueLog):
    """Yields (token, origin). Block comments are blanked out keeping line numbers."""
    text = path.read_text(encoding="utf-8", errors="replace")
    text = _BLOCK_COMMENT_RE.sub(lambda m: "\n" * m.group(0).count("\n"), text)
    for lineno, line in enumerate(text.splitlines(), 1):
        line = _LINE_COMMENT_RE.sub("", line)
        origin = f"{path}:{lineno}"
        try:
            parts = shlex.split(line, posix=True)
        except ValueError as e:
            log.error("N0_FILELIST_SYNTAX", f"无法解析此行：{e}", origin)
            continue
        for p in parts:
            yield p, origin


def parse_filelist(
    path: Path,
    root: Path,
    log: IssueLog,
    env: dict[str, str] | None = None,
    out: FileList | None = None,
    relative_to_self: bool = False,
    _stack: tuple[Path, ...] = (),
) -> FileList:
    env = dict(os.environ) if env is None else env
    out = FileList() if out is None else out
    real = path.resolve()
    if real in _stack:
        chain = " -> ".join(str(p) for p in (*_stack, real))
        log.error("N0_FILELIST_CYCLE", f".f 嵌套出现环：{chain}", str(path))
        return out
    if not path.is_file():
        where = f"{_stack[-1]}" if _stack else ""
        log.error("N0_FILE_MISSING", f".f 文件不存在：{path}", where)
        return out
    base = path.parent if relative_to_self else root

    def resolve(p: str) -> Path:
        return Path(p) if os.path.isabs(p) else base / p

    toks = iter(_tokens(path, log))
    for raw, origin in toks:
        tok = _expand_env(raw, env, log, origin)
        if tok in ("-f", "-F", "-v", "-y"):
            nxt = next(toks, None)
            if nxt is None:
                log.error("N0_FILELIST_SYNTAX", f"{tok} 后缺少参数", origin)
                break
            arg = _expand_env(nxt[0], env, log, nxt[1])
            if tok in ("-f", "-F"):
                parse_filelist(resolve(arg), root, log, env, out, tok == "-F", (*_stack, real))
            elif tok == "-v":
                out.library_files.append((resolve(arg), origin))
            else:
                out.library_dirs.append((resolve(arg), origin))
        elif tok.startswith("+incdir+"):
            for d in filter(None, tok[len("+incdir+"):].split("+")):
                out.include_dirs.append((resolve(d), origin))
        elif tok.startswith("+define+"):
            for d in filter(None, tok[len("+define+"):].split("+")):
                name, eq, value = d.partition("=")
                out.defines.append((name, value if eq else None, origin))
        elif tok.startswith("+libext+"):
            out.libext.extend(e for e in tok[len("+libext+"):].split("+") if e)
        elif tok.startswith(("-", "+")):
            log.warn("N0_FILELIST_UNKNOWN_OPTION", f"不支持的选项 {tok}，已忽略", origin)
        else:
            out.files.append((resolve(tok), origin))
    return out
