"""N0a steps 3-6: classify files, build the dependency graph, infer top, take the closure, sort.

Graph nodes are compile-unit files. Edges:
  - A defines a package that B references       -> A before B
  - A defines a macro that B uses               -> A before B
  - B instantiates a module/interface A defines -> closure only, no ordering
Cycles are reported, never broken automatically.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from verimine.n0a.facts import Definition, FileFacts
from verimine.n0a.scan import HEADER_SUFFIXES

TB_NAME_RE = re.compile(r"(^tb_|_tb$|_tb_|^tb$|test|bench)", re.I)
TB_DIRS = {"tb", "tbs", "sim", "verif", "test", "tests", "testbench", "bench", "dv"}
RTL_DIRS = {"rtl", "src", "hdl", "design", "core", "ip"}
STALE_RE = re.compile(r"(old|bak|backup|copy|model|sim|orig|deprecated)", re.I)
# FPGA primitives and memory macros: undefined, but expected to be black boxes.
PRIMITIVE_RE = re.compile(
    r"^(FD[A-Z]{0,3}|LD[A-Z]{0,3}|BUF[A-Z0-9_]*|[IO]BUF\w*|IBUFDS\w*|MMCM\w*|PLL\w*|DCM\w*|"
    r"DSP48\w*|RAMB\d+\w*|RAM\d+\w*|SRL\w*|LUT\d\w*|CARRY\d?\w*|MUXF\d\w*|[IO]DDR\w*|"
    r"altsyncram|altpll|altera_\w+|lpm_\w+|[ds]cfifo|SB_\w+|EHXPLL\w*|"
    r"(sram|rf|rom|ram|mem|regfile)_\w+|\w+_(sram|ram|rom)(_\w+)?)$"
)


@dataclass
class Graph:
    facts: dict[str, FileFacts]
    headers: set[str]
    units: list[str]
    defs: dict[str, list[tuple[str, Definition]]]  # name -> [(file, def)], file sorted
    issues: list[dict] = field(default_factory=list)


@dataclass
class TopCandidate:
    name: str
    file: str
    size: int  # modules in its instance tree, itself included
    in_rtl_dir: bool


@dataclass
class Closure:
    top: str
    files: list[str]  # compile order
    chosen: dict[str, str]  # definition name -> file it was taken from
    missing: dict[str, list[str]]  # undefined module -> files referencing it
    missing_pkgs: dict[str, list[str]]
    duplicates: dict[str, dict]  # name -> {"chosen": file, "others": [...]}
    cycles: list[list[str]]
    macro_providers: dict[str, str] = field(default_factory=dict)


def build(facts: dict[str, FileFacts], include_matches: dict) -> Graph:
    """Step 3: files included by another file, and every .vh/.svh, are headers."""
    headers = {p for p in facts if PurePosixPath(p).suffix in HEADER_SUFFIXES}
    headers |= {m.chosen for m in include_matches.values() if m.chosen}
    units = sorted(p for p in facts if p not in headers)
    defs: dict[str, list[tuple[str, Definition]]] = {}
    for path in sorted(facts):
        for d in facts[path].definitions:
            defs.setdefault(d.name, []).append((path, d))
    return Graph(facts, headers, units, defs)


def _dirs(path: str) -> set[str]:
    return {p.lower() for p in PurePosixPath(path).parts[:-1]}


def is_testbench(name: str, path: str, d: Definition) -> list[str]:
    """Returns the reasons this module looks like a testbench (empty if it does not)."""
    why = list(d.tb_features)
    if TB_NAME_RE.search(name) or TB_NAME_RE.search(PurePosixPath(path).stem):
        why.append("name")
    if _dirs(path) & TB_DIRS:
        why.append("dir")
    return why


def _module_tree(g: Graph, name: str, seen: set[str] | None = None) -> set[str]:
    seen = set() if seen is None else seen
    if name in seen or name not in g.defs:
        return seen
    seen.add(name)
    _, d = g.defs[name][0]
    for child in (*d.instances, *d.cond_instances):
        _module_tree(g, child, seen)
    return seen


def top_candidates(g: Graph) -> tuple[list[TopCandidate], dict[str, list[str]]]:
    """Step 5. Returns (ranked candidates, excluded testbenches with reasons). Instances inside a
    testbench do not count: the testbench instantiates the very DUT we are looking for."""
    tbs: dict[str, list[str]] = {}
    for name, entries in sorted(g.defs.items()):
        path, d = entries[0]
        if d.kind in ("module", "program") and path not in g.headers:
            why = is_testbench(name, path, d)
            if why:
                tbs[name] = why
    used = {
        x
        for name, entries in g.defs.items() if name not in tbs
        for _, d in entries
        for x in (*d.instances, *d.cond_instances) if x != name
    }
    cands = []
    for name, entries in sorted(g.defs.items()):
        path, d = entries[0]
        if d.kind != "module" or name in used or name in tbs or path in g.headers:
            continue
        cands.append(TopCandidate(name, path, len(_module_tree(g, name)), bool(_dirs(path) & RTL_DIRS)))
    cands.sort(key=lambda c: (-c.size, not c.in_rtl_dir, c.name))
    return cands, tbs


def _choose(name: str, entries: list[tuple[str, Definition]], parent_file: str | None) -> tuple[str, Definition]:
    """Several files define `name`: prefer non-stale paths, then the parent's directory."""
    parent_dir = PurePosixPath(parent_file).parent if parent_file else None

    def key(e):
        path = e[0]
        stale = bool(STALE_RE.search(path))
        near = parent_dir is not None and PurePosixPath(path).parent == parent_dir
        return (stale, not near, path)

    return sorted(entries, key=key)[0]


def _header_macros(g: Graph, path: str, include_matches: dict, seen: set[str] | None = None) -> set[str]:
    """Macros a file gets from its own text and everything it includes, transitively."""
    seen = set() if seen is None else seen
    if path in seen or path not in g.facts:
        return set()
    seen.add(path)
    f = g.facts[path]
    out = set(f.macros_defined)
    for inc in f.includes:
        m = include_matches.get((path, inc))
        if m and m.chosen:
            out |= _header_macros(g, m.chosen, include_matches, seen)
    return out


def closure(
    g: Graph,
    top: str,
    include_matches: dict,
    extra: set[str] = frozenset(),
    blackbox: set[str] = frozenset(),
    fixed_files: list[str] | None = None,
) -> Closure:
    """Step 6. With fixed_files (a .f was given) the file set and order come from it unchanged."""
    chosen: dict[str, str] = {}
    missing: dict[str, list[str]] = {}
    missing_pkgs: dict[str, list[str]] = {}
    duplicates: dict[str, dict] = {}
    files: set[str] = set()
    allowed = set(fixed_files) if fixed_files is not None else None

    def entries_for(name: str):
        ents = g.defs.get(name, [])
        ents = [e for e in ents if e[0] not in g.headers]
        if allowed is not None:
            ents = [e for e in ents if e[0] in allowed]
        return ents

    todo: list[tuple[str, str | None, bool]] = [(top, None, False)] + [(x, None, False) for x in sorted(extra)]
    while todo:
        name, parent, conditional = todo.pop(0)
        if name in chosen or name in blackbox:
            continue
        ents = entries_for(name)
        if not ents:
            if not conditional and name not in g.defs:
                missing.setdefault(name, [])
                if parent and parent not in missing[name]:
                    missing[name].append(parent)
            continue
        path, d = _choose(name, ents, parent)
        if len(ents) > 1:
            duplicates[name] = {"chosen": path, "others": sorted(p for p, _ in ents if p != path)}
        chosen[name] = path
        files.add(path)
        for child in d.instances:
            todo.append((child, path, False))
        for child in d.cond_instances:
            todo.append((child, path, True))
        for ref in (*d.iface_refs, *d.pkg_refs):
            todo.append((ref, path, True))
        for pkg in g.facts[path].pkg_refs:
            todo.append((pkg, path, True))
        for pkg in d.pkg_refs + g.facts[path].pkg_refs:
            if pkg not in g.defs:
                missing_pkgs.setdefault(pkg, [])
                if path not in missing_pkgs[pkg]:
                    missing_pkgs[pkg].append(path)
        todo.sort(key=lambda t: (t[0], t[1] or ""))

    # Macro providers: a compile unit defining a macro another unit uses but does not get from
    # its own includes. Pulled into the closure and ordered before its users.
    macro_def_units: dict[str, list[str]] = {}
    for u in sorted(allowed if allowed is not None else g.units):
        if u in g.facts and u not in g.headers:
            for m in g.facts[u].macros_defined:
                macro_def_units.setdefault(m, []).append(u)
    providers: dict[str, str] = {}
    edges: dict[str, set[str]] = {}
    pending = sorted(files)
    while pending:
        u = pending.pop(0)
        own = _header_macros(g, u, include_matches)
        for m in sorted(set(g.facts[u].macros_used) | set(g.facts[u].macros_tested)):
            if m in own or m not in macro_def_units:
                continue
            src = macro_def_units[m][0]
            if src == u:
                continue
            providers.setdefault(m, src)
            edges.setdefault(src, set()).add(u)
            if src not in files and m in g.facts[u].macros_used:
                files.add(src)
                pending.append(src)

    # Package edges.
    pkg_file = {n: p for n, p in chosen.items() if g.defs[n][0][1].kind == "package"}
    for name, path in chosen.items():
        d = next(d for p, d in g.defs[name] if p == path)
        for pkg in (*d.pkg_refs, *g.facts[path].pkg_refs):
            src = pkg_file.get(pkg)
            if src and src != path:
                edges.setdefault(src, set()).add(path)

    if fixed_files is not None:
        order, cycles = list(fixed_files), []
    else:
        order, cycles = _toposort(sorted(files), edges)
    return Closure(top, order, chosen, missing, missing_pkgs, duplicates, cycles, providers)


def _toposort(nodes: list[str], edges: dict[str, set[str]]) -> tuple[list[str], list[list[str]]]:
    """Layered Kahn: each layer sorted by path, so the result is the same every run."""
    nodeset = set(nodes)
    indeg = {n: 0 for n in nodes}
    for src, dsts in edges.items():
        if src not in nodeset:
            continue
        for d in dsts:
            if d in nodeset:
                indeg[d] += 1
    order: list[str] = []
    layer = sorted(n for n, k in indeg.items() if k == 0)
    while layer:
        order.extend(layer)
        nxt = []
        for n in layer:
            for d in edges.get(n, ()):
                if d in indeg:
                    indeg[d] -= 1
                    if indeg[d] == 0:
                        nxt.append(d)
        layer = sorted(nxt)
    rest = sorted(n for n in nodes if n not in set(order))
    cycles = _find_cycles(rest, edges) if rest else []
    return order + rest, cycles


def _find_cycles(nodes: list[str], edges: dict[str, set[str]]) -> list[list[str]]:
    nodeset, cycles, done = set(nodes), [], set()
    for start in nodes:
        if start in done:
            continue
        path, pos = [start], {start: 0}
        while path:
            nxt = sorted(d for d in edges.get(path[-1], ()) if d in nodeset)
            step = next((d for d in nxt if d in pos), None)
            if step is not None:
                cyc = path[pos[step]:]
                cycles.append(cyc)
                done.update(cyc)
                break
            step = next((d for d in nxt if d not in done), None)
            if step is None:
                gone = path.pop()
                del pos[gone]
                done.add(gone)
                continue
            pos[step] = len(path)
            path.append(step)
    return cycles
