"""Waveform reading (N3 step 3). wellen (pywellen) reads VCD and FST; when it is not installed a
small built-in VCD reader is used instead (FST then needs wellen).

The rest of N3 sees one interface:
    w = open_wave(path)
    w.vars           {normalized full name: width}
    w.changes(name)  [(time, value)], value an int, or None for anything with x/z bits
    w.end_time

Names are normalized: escaped identifiers lose their backslash and trailing space, and
`g(0)` (some simulators' generate-block spelling) becomes `g[0]`, so they line up with N1 paths.
"""
from __future__ import annotations

import re
from pathlib import Path

try:  # optional: pip install pywellen (or verimine[waves])
    import pywellen as _wellen
except Exception:  # pragma: no cover - depends on the environment
    _wellen = None

_PAREN_IDX = re.compile(r"\((\d+)\)")


def reader_name() -> str:
    if _wellen is None:
        return "builtin-vcd"
    try:
        from importlib import metadata
        return f"wellen {metadata.version('pywellen')}"
    except Exception:
        return "wellen"


def normalize(name: str) -> str:
    parts = []
    for p in name.split("."):
        p = p.strip()
        if p.startswith("\\"):
            p = p[1:]
        parts.append(_PAREN_IDX.sub(r"[\1]", p))
    return ".".join(parts)


def _value(v) -> int | None:
    if isinstance(v, int):
        return v
    if isinstance(v, str):
        s = v.strip().lower()
        if s and all(c in "01" for c in s):
            return int(s, 2)
    return None


class Wave:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.vars: dict[str, int] = {}
        self.end_time = 0

    def changes(self, name: str) -> list[tuple[int, int | None]]:
        raise NotImplementedError


class _WellenWave(Wave):
    def __init__(self, path: Path):
        super().__init__(path)
        self._w = _wellen.Waveform(str(path))
        self._by_name = {}
        for v in self._w.all_vars():
            if v.is_real or v.is_string:
                continue
            n = normalize(v.full_name)
            self._by_name.setdefault(n, v)
            self.vars.setdefault(n, v.bitwidth or v.size or 1)
        self._cache: dict[str, list] = {}
        t = 0
        for v in list(self._by_name.values())[:64]:  # the end time is the last change anywhere;
            tv = list(v.tv)                          # a sample of vars is enough in practice
            if tv:
                t = max(t, tv[-1][0])
        self.end_time = t

    def changes(self, name):
        if name not in self._cache:
            self._cache[name] = [(t, _value(x)) for t, x in self._by_name[name].tv]
            if self._cache[name]:
                self.end_time = max(self.end_time, self._cache[name][-1][0])
        return self._cache[name]


class _VcdWave(Wave):
    """Plain VCD reader: enough for simulator and smtbmc output."""

    def __init__(self, path: Path):
        super().__init__(path)
        ids: dict[str, list[str]] = {}
        scope: list[str] = []
        self._ch: dict[str, list] = {}
        with open(path, "r", errors="replace") as fh:
            text = fh.read()
        head, _, body = text.partition("$enddefinitions")
        toks = head.split()
        i = 0
        while i < len(toks):
            t = toks[i]
            if t == "$scope":
                scope.append(toks[i + 2])
                i += 3
            elif t == "$upscope":
                scope.pop()
                i += 1
            elif t == "$var":
                end = toks.index("$end", i)
                kind, width, code, ref = toks[i + 1], toks[i + 2], toks[i + 3], toks[i + 4]
                if kind not in ("real", "realtime", "string", "event"):
                    n = normalize(".".join(scope + [ref]))
                    ids.setdefault(code, []).append(n)
                    self.vars.setdefault(n, int(width))
                i = end + 1
            else:
                i += 1
        series: dict[str, list] = {c: [] for c in ids}
        time = 0
        for line in body.split("\n")[1:]:
            line = line.strip()
            if not line or line.startswith("$"):
                continue
            c = line[0]
            if c == "#":
                time = int(line[1:])
            elif c in "bB":
                val, _, code = line[1:].partition(" ")
                if code in series:
                    series[code].append((time, _value(val)))
            elif c in "rR":
                continue
            elif c in "01xXzZ":
                code = line[1:]
                if code in series:
                    series[code].append((time, _value(c)))
        self.end_time = time
        for code, names in ids.items():
            for n in names:
                self._ch.setdefault(n, series[code])

    def changes(self, name):
        return self._ch.get(name, [])


def open_wave(path: Path) -> Wave:
    path = Path(path)
    if _wellen is not None:
        return _WellenWave(path)
    if path.suffix.lower() == ".fst":
        raise RuntimeError("读取 FST 需要 wellen：pip install pywellen")
    return _VcdWave(path)
