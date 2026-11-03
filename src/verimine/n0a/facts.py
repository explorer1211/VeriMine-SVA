"""N0a step 2: per-file facts from a syntax-only parse (no elaboration).

Each file is parsed on its own. Undefined macros and unresolved includes only produce
diagnostics. To see every `ifdef branch, the file is parsed once with no macros defined, once
with all tested macros defined, and once per tested macro alone (this last pass catches `elsif
branches); instances found only in the extra passes are marked conditional.

Files slang cannot parse at all fall back to a regex scan. A .v file that only parses with the
Verilog-2005 keyword set (signals named `logic`, `bit`, ...) is flagged as such.

Facts are cached by content hash, so unchanged files are not parsed again.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

import pyslang as ps
from pyslang import parsing, syntax

from verimine.hashing import normalize_text

FACTS_VERSION = 1
MAX_SINGLE_MACRO_PASSES = 16

DIRECTIVES = {
    "define", "undef", "undefineall", "ifdef", "ifndef", "elsif", "else", "endif", "include",
    "timescale", "resetall", "celldefine", "endcelldefine", "default_nettype", "pragma", "line",
    "begin_keywords", "end_keywords", "unconnected_drive", "nounconnected_drive", "protect",
    "endprotect", "__FILE__", "__LINE__", "default_decay_time", "default_trireg_strength",
    "delay_mode_distributed", "delay_mode_path", "delay_mode_unit", "delay_mode_zero",
}

DECL_KINDS = {
    "SyntaxKind.ModuleDeclaration": "module",
    "SyntaxKind.InterfaceDeclaration": "interface",
    "SyntaxKind.PackageDeclaration": "package",
    "SyntaxKind.ProgramDeclaration": "program",
    "SyntaxKind.UdpDeclaration": "udp",
}

_COMMENT_RE = re.compile(r"//[^\n]*|/\*.*?\*/", re.S)
_STRING_RE = re.compile(r'"(?:\\.|[^"\\\n])*"')
_DEFINE_RE = re.compile(r"`define\s+(\w+)")
_COND_RE = re.compile(r"`(?:ifdef|ifndef|elsif)\s+(\w+)")
_USE_RE = re.compile(r"`(\w+)")
_INCLUDE_RE = re.compile(r'`include\s+"([^"]+)"')
_SIM_TASK_RE = re.compile(r"\$(finish|stop|display|monitor|write|strobe|readmemh|readmemb|dumpfile|fatal)\b")
_CLOCK_GEN_RE = re.compile(r"\b(always|forever)\s*#")
# Fallback only: good enough to find definitions and the obvious `type name (` instances.
_RX_DECL_RE = re.compile(r"^\s*(module|macromodule|interface|package|program|primitive)\s+(\w+)", re.M)
_RX_END_RE = re.compile(r"^\s*end(module|interface|package|program|primitive)\b", re.M)
_RX_INST_RE = re.compile(r"^\s*(\w+)\s*(?:#\s*\(.*?\)\s*)?(\w+)\s*(?:\[[^\]]*\]\s*)?\(", re.M | re.S)
_RX_IMPORT_RE = re.compile(r"\b(\w+)\s*::")
_RX_KEYWORDS = {
    "module", "if", "for", "while", "case", "casez", "casex", "begin", "end", "assign", "always",
    "always_ff", "always_comb", "always_latch", "initial", "function", "task", "wire", "reg",
    "logic", "input", "output", "inout", "else", "return", "repeat", "foreach", "assert",
    "assume", "cover", "property", "sequence", "and", "or", "not", "nand", "nor", "xor", "xnor",
    "buf", "bufif0", "bufif1", "notif0", "notif1", "generate", "posedge", "negedge", "unique",
    "priority", "default", "localparam", "parameter", "typedef", "import", "export",
}


@dataclass
class Definition:
    name: str
    kind: str  # module | interface | package | program | udp
    has_ports: bool = True
    instances: list[str] = field(default_factory=list)  # child definitions on the default branches
    cond_instances: list[str] = field(default_factory=list)  # only on other `ifdef branches
    pkg_refs: list[str] = field(default_factory=list)
    iface_refs: list[str] = field(default_factory=list)
    tb_features: list[str] = field(default_factory=list)


@dataclass
class FileFacts:
    path: str
    sha: str
    definitions: list[Definition] = field(default_factory=list)
    pkg_refs: list[str] = field(default_factory=list)  # $unit-level imports
    includes: list[str] = field(default_factory=list)
    macros_defined: list[str] = field(default_factory=list)
    macros_used: list[str] = field(default_factory=list)  # `NAME in code: needs a value
    macros_tested: list[str] = field(default_factory=list)  # only in `ifdef / `ifndef / `elsif
    parse_errors: list[str] = field(default_factory=list)
    fallback: bool = False  # slang could not parse it; regex scan used
    verilog2005: bool = False  # only parses with the Verilog-2005 keyword set

    @staticmethod
    def from_dict(d: dict) -> "FileFacts":
        d = dict(d)
        defs = [Definition(**x) for x in d.pop("definitions")]
        return FileFacts(definitions=defs, **d)


def _strip(text: str) -> str:
    return _STRING_RE.sub('""', _COMMENT_RE.sub(" ", text))


def _lexical(text: str) -> tuple[list[str], list[str], list[str], list[str]]:
    """Macro and include facts from the raw text: these live in trivia, so regex is exact enough."""
    includes = sorted(set(_INCLUDE_RE.findall(_COMMENT_RE.sub(" ", text))))
    code = _strip(text)
    defined = set(_DEFINE_RE.findall(code))
    tested = set(_COND_RE.findall(code))
    used = {m for m in _USE_RE.findall(code) if m not in DIRECTIVES}
    return includes, sorted(defined), sorted(used), sorted(tested - used)


def _has_ports(header) -> bool:
    ports = header.ports
    if ports is None:
        return False
    return len(getattr(ports, "ports", [])) > 0


def _decl_facts(node, kind: str) -> Definition:
    d = Definition(name=node.header.name.valueText, kind=kind)
    if kind != "udp":
        d.has_ports = _has_ports(node.header)
    inst, pkgs, ifaces = set(), set(), set()

    def visit(n):
        k = str(n.kind)
        if k == "SyntaxKind.HierarchyInstantiation":
            inst.add(n.type.valueText)
        elif k == "SyntaxKind.PackageImportItem":
            pkgs.add(n.package.valueText)
        elif k == "SyntaxKind.ScopedName" and str(n.left.kind) == "SyntaxKind.IdentifierName":
            if n.separator.valueText == "::":
                pkgs.add(n.left.identifier.valueText)
        elif k == "SyntaxKind.InterfacePortHeader":
            ifaces.add(n.nameOrKeyword.valueText)
        return True

    node.visit(visit)
    d.instances = sorted(inst - {""})
    d.pkg_refs = sorted(pkgs - {"", "$unit", "std", "local", d.name})
    d.iface_refs = sorted(ifaces - {"", "interface"})
    if kind in ("module", "program"):
        text = _strip(str(node))
        if not d.has_ports:
            d.tb_features.append("no_ports")
        if "initial" in text and _SIM_TASK_RE.search(text):
            d.tb_features.append("sim_tasks")
        if _CLOCK_GEN_RE.search(text):
            d.tb_features.append("clock_gen")
    return d


def _parse(text: str, path: str, predefines: list[str], v2005: bool = False):
    sm = ps.SourceManager()
    po = parsing.PreprocessorOptions()
    po.predefines = predefines
    if v2005:
        text = '`begin_keywords "1364-2005"\n' + text + "\n`end_keywords\n"
    tree = syntax.SyntaxTree.fromText(text, sm, Path(path).name, path, ps.Bag([po]))
    engine = ps.DiagnosticEngine(sm)
    # Missing includes and undefined macros are expected when a file is parsed alone.
    errors = [
        engine.formatMessage(d)
        for d in tree.diagnostics
        if d.isError() and d.code not in (ps.Diags.CouldNotOpenIncludeFile, ps.Diags.UnknownDirective)
    ]
    defs, unit_pkgs = [], set()
    # A file with a single declaration comes back with that declaration as the root.
    root = tree.root
    members = root.members if str(root.kind) == "SyntaxKind.CompilationUnit" else [root]
    for m in members:
        k = str(m.kind)
        if k in DECL_KINDS:
            defs.append(_decl_facts(m, DECL_KINDS[k]))
        elif k == "SyntaxKind.PackageImportDeclaration":
            for item in m.items:
                unit_pkgs.add(item.package.valueText)
    return defs, sorted(unit_pkgs), errors


def _regex_facts(text: str) -> tuple[list[Definition], list[str]]:
    code = _strip(text)
    defs = []
    starts = [(m.start(), m.end(), m.group(1), m.group(2)) for m in _RX_DECL_RE.finditer(code)]
    ends = [m.end() for m in _RX_END_RE.finditer(code)]
    for start, head_end, kw, name in starts:
        end = next((e for e in ends if e > start), len(code))
        body = code[head_end:end]
        kind = {"macromodule": "module", "primitive": "udp"}.get(kw, kw)
        d = Definition(name=name, kind=kind)
        if kind in ("module", "interface", "program"):
            d.instances = sorted({
                t for t, _ in _RX_INST_RE.findall(body) if t not in _RX_KEYWORDS and t != name
            })
            d.pkg_refs = sorted(set(_RX_IMPORT_RE.findall(body)) - {"std", "$unit", "local"})
        defs.append(d)
    return defs, []


def extract(root: Path, path: str, sha: str) -> FileFacts:
    text = normalize_text((root / path).read_bytes()).decode("utf-8", errors="replace")
    includes, defined, used, tested = _lexical(text)
    facts = FileFacts(path, sha, includes=includes, macros_defined=defined,
                      macros_used=used, macros_tested=tested)

    defs, unit_pkgs, errors = _parse(text, path, [])
    if errors and path.endswith(".v"):
        defs05, pkgs05, errors05 = _parse(text, path, [], v2005=True)
        if len(errors05) < len(errors):
            defs, unit_pkgs, errors = defs05, pkgs05, errors05
            facts.verilog2005 = True
    if errors and not defs:
        defs, unit_pkgs = _regex_facts(text)
        facts.fallback = True
    facts.parse_errors = errors[:20]

    # Extra passes for inactive `ifdef branches.
    cond = sorted(set(_COND_RE.findall(_strip(text))))
    if cond and not facts.fallback:
        passes = [cond] + ([[m] for m in cond] if len(cond) <= MAX_SINGLE_MACRO_PASSES else [])
        by_name = {d.name: d for d in defs}
        for pre in passes:
            extra, extra_pkgs, _ = _parse(text, path, pre, v2005=facts.verilog2005)
            unit_pkgs = sorted(set(unit_pkgs) | set(extra_pkgs))
            for e in extra:
                base = by_name.get(e.name)
                if base is None:
                    e.cond_instances, e.instances = e.instances, []
                    by_name[e.name] = e
                    defs.append(e)
                    continue
                new = set(e.instances) - set(base.instances)
                base.cond_instances = sorted(set(base.cond_instances) | new)
                base.pkg_refs = sorted(set(base.pkg_refs) | set(e.pkg_refs))
                base.iface_refs = sorted(set(base.iface_refs) | set(e.iface_refs))
    facts.definitions = sorted(defs, key=lambda d: (d.name, d.kind))
    facts.pkg_refs = unit_pkgs
    return facts


def extract_all(root: Path, files, cache_dir: Path | None = None) -> dict[str, FileFacts]:
    """files: iterable of ScannedFile. Returns path -> FileFacts."""
    out: dict[str, FileFacts] = {}
    for f in files:
        cached = cache_dir / f"{f.sha}{Path(f.path).suffix}.v{FACTS_VERSION}.json" if cache_dir else None
        if cached and cached.is_file():
            facts = FileFacts.from_dict(json.loads(cached.read_text(encoding="utf-8")))
            facts.path = f.path  # same content may live at another path
        else:
            facts = extract(root, f.path, f.sha)
            if cached:
                cached.parent.mkdir(parents=True, exist_ok=True)
                cached.write_text(json.dumps(asdict(facts), sort_keys=True), encoding="utf-8")
        out[f.path] = facts
    return out
