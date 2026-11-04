"""
Slang-based elaboration: build the full instance tree and structural index.

Returns a flat list of InstanceInfo objects covering every instance in the
hierarchy (including the top-level wrapper created by slang).
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

from pyslang.ast import (
    Compilation,
    InstanceSymbol,
    PortSymbol,
    NetSymbol,
    VariableSymbol,
    ProceduralBlockSymbol,
    ProceduralBlockKind,
    ContinuousAssignSymbol,
    VisitAction,
    SymbolKind,
    ParameterSymbol,
)
from pyslang.analysis import AnalysisManager
from pyslang.syntax import SyntaxTree
from pyslang.pyslang import (
    Bag,
    SourceManager,
    DiagnosticEngine,
    TextDiagnosticClient,
)
from pyslang.parsing import PreprocessorOptions

from .assign import AssignInfo, Extractor


@dataclasses.dataclass
class PortInfo:
    name: str
    direction: str       # "in" | "out" | "inout" | "ref"
    type_str: str
    is_interface: bool
    width: int = 0
    signed: bool = False


@dataclasses.dataclass
class SignalInfo:
    name: str            # relative to the instance, e.g. "g_lane[1].tmp" inside a generate block
    kind: str            # "net" | "variable"
    type_str: str
    location: str        # "file:line"
    width: int = 0       # element width for arrays
    signed: bool = False
    array_dims: str = ""  # unpacked dimensions, e.g. "[0:15]"; non-empty = memory candidate
    scope: str = ""      # enclosing generate block, relative to the instance ("" = module level)


@dataclasses.dataclass
class ProcInfo:
    kind: str            # "always_ff" | "always_comb" | "always_latch" | "always" | "assign" | "initial"
    sensitivity: list[dict]  # [{"signal": "clk", "edge": "posedge"}, ...]
    location: str
    scope: str = ""      # enclosing generate block, relative to the instance


@dataclasses.dataclass
class DriverInfo:
    signal: str
    driver_kind: str     # "Procedural" | "Continuous" | "Port"
    proc_kind: str | None
    location: str


@dataclasses.dataclass
class ClockResetCandidate:
    signal: str
    edge: str | None     # "posedge" | "negedge" | None
    is_async_reset: bool
    basis: str           # "sensitivity_list" | "first_if"


@dataclasses.dataclass
class InstanceInfo:
    path: str            # hierarchical path e.g. "top.u_child"
    module: str          # definition name
    parent: str | None
    params: dict[str, Any]
    ports: list[PortInfo]
    signals: list[SignalInfo]
    procs: list[ProcInfo]
    drivers: list[DriverInfo]
    clk_rst_candidates: list[ClockResetCandidate]
    clocks: set[str]     # clock signal names seen in this instance
    is_incomplete: bool  # true if elaboration had errors in this scope
    assigns: list[AssignInfo] = dataclasses.field(default_factory=list)
    # How the parent connects this instance: [{"port", "direction", "expr"}], expr in parent names
    connections: list[dict] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class ElaborationResult:
    instances: list[InstanceInfo]
    top_path: str
    errors: list[dict]    # [{"message": ..., "where": ...}]
    warnings: list[dict]


# --------------------------------------------------------------------------- #
# Internal helpers
# --------------------------------------------------------------------------- #

def _dir_str(d) -> str:
    s = str(d)
    mapping = {
        "ArgumentDirection.In": "in",
        "ArgumentDirection.Out": "out",
        "ArgumentDirection.InOut": "inout",
        "ArgumentDirection.Ref": "ref",
    }
    return mapping.get(s, s.split(".")[-1].lower())


def _edge_str(e) -> str | None:
    s = str(e)
    if "Pos" in s:
        return "posedge"
    if "Neg" in s:
        return "negedge"
    if "None" in s or "BothEdges" in s:
        return None
    return s.split(".")[-1].lower()


def _proc_kind_str(k) -> str:
    s = str(k)
    mapping = {
        "ProceduralBlockKind.AlwaysFF": "always_ff",
        "ProceduralBlockKind.AlwaysComb": "always_comb",
        "ProceduralBlockKind.AlwaysLatch": "always_latch",
        "ProceduralBlockKind.Always": "always",
        "ProceduralBlockKind.Initial": "initial",
        "ProceduralBlockKind.Final": "final",
    }
    return mapping.get(s, s.split(".")[-1].lower())


def _relative_loc(loc: str, root: Path) -> str:
    """'/abs/root/rtl/a.sv:3:5' -> 'rtl/a.sv:3:5'. The index must not contain absolute paths."""
    if not loc:
        return loc
    path, sep, rest = loc.partition(":")
    try:
        p = Path(path)
        if p.is_absolute():
            path = p.resolve().relative_to(root.resolve()).as_posix()
    except (ValueError, OSError):
        pass
    return path + sep + rest


def _relativize(instances: list[InstanceInfo], diags: list[dict], root: Path) -> None:
    for inst in instances:
        for group in (inst.signals, inst.procs, inst.drivers, inst.assigns):
            for x in group:
                x.location = _relative_loc(x.location, root)
    for d in diags:
        d["where"] = _relative_loc(d.get("where", ""), root)


def _location_str(sm: SourceManager, loc) -> str:
    if loc is None:
        return ""
    try:
        buf = sm.getBufferFromLocation(loc)
        path = sm.getFileName(loc)
        line = sm.getLineNumber(loc)
        col = sm.getColumnNumber(loc)
        return f"{path}:{line}:{col}"
    except Exception:
        return ""


def _sensitivity(sm: SourceManager, proc: ProceduralBlockSymbol) -> list[dict]:
    """Extract sensitivity list entries from an always_ff/always block."""
    result = []
    try:
        body = proc.body  # TimedStatement
        if str(body.kind) != "StatementKind.Timed":
            return result
        timing = body.timing
        events = []
        if str(timing.kind) == "TimingControlKind.EventList":
            events = list(timing.events)
        elif str(timing.kind) == "TimingControlKind.SignalEvent":
            events = [timing]
        for ev in events:
            edge = _edge_str(ev.edge)
            expr = ev.expr
            sym_ref = None
            try:
                sym_ref = expr.symbol
                sig_name = sym_ref.name
            except Exception:
                sig_name = None
            if sig_name:
                result.append({"signal": sig_name, "edge": edge})
    except Exception:
        pass
    return result


_SCOPE_KINDS = ("SymbolKind.GenerateBlock", "SymbolKind.GenerateBlockArray")


def _type_facts(t) -> tuple[int, bool, str]:
    """(element width, signed, unpacked dims) of a slang type."""
    dims = ""
    s = str(t)
    if "$" in s:
        dims = s.split("$", 1)[1]
    try:
        for _ in range(8):
            if not t.isUnpackedArray:
                break
            t = t.arrayElementType
        return int(t.bitWidth), bool(t.isSigned), dims
    except Exception:
        return 0, False, dims


def iter_child_instances(body):
    """Instances directly under `body`, looking through instantiated generate blocks and
    instance arrays. Uninstantiated generate branches are skipped."""
    found = []

    def visit(s):
        k = str(s.kind)
        if not k.startswith("SymbolKind."):
            return VisitAction.Skip
        if k == "SymbolKind.InstanceBody":
            return VisitAction.Advance if s is body or s.hierarchicalPath == body.hierarchicalPath else VisitAction.Skip
        if k in _SCOPE_KINDS:
            return VisitAction.Skip if getattr(s, "isUninstantiated", False) else VisitAction.Advance
        if k == "SymbolKind.InstanceArray":
            return VisitAction.Advance
        if k == "SymbolKind.Instance":
            found.append(s)
            return VisitAction.Skip
        return VisitAction.Skip

    body.visit(visit)
    return found


def _collect_instance(
    sym: InstanceSymbol,
    sm: SourceManager,
    mgr: AnalysisManager,
    parent_path: str | None,
    root_path: Path,
    connections: list[dict] | None = None,
) -> InstanceInfo:
    body = sym.body
    path = sym.hierarchicalPath
    module = body.definition.name
    loc_of = lambda l: _location_str(sm, l)  # noqa: E731
    ex = Extractor(path, body, loc_of)

    # params
    params: dict[str, Any] = {}
    for p in body.parameters:
        try:
            v = p.value
            params[p.name] = str(v)
        except Exception:
            params[p.name] = None

    # ports, signals, procs, drivers, assignments
    ports: list[PortInfo] = []
    signals: list[SignalInfo] = []
    procs: list[ProcInfo] = []
    drivers: list[DriverInfo] = []
    assigns: list[AssignInfo] = []
    clocks: set[str] = set()

    for s in body.portList:
        if str(s.kind) != "SymbolKind.Port":
            continue
        try:
            t = str(s.type)
            width, signed, _ = _type_facts(s.type)
        except Exception:
            t, width, signed = "", 0, False
        ports.append(PortInfo(
            name=s.name,
            direction=_dir_str(s.direction),
            type_str=t,
            is_interface="interface" in t.lower() or "modport" in t.lower(),
            width=width,
            signed=signed,
        ))
    port_names = {p.name for p in ports}

    def scope_of(s) -> str:
        name = ex.rel(s) or s.name
        return name.rsplit(".", 1)[0] if "." in name else ""

    def visitor(s):
        k = str(s.kind)
        if not k.startswith("SymbolKind."):
            return VisitAction.Skip
        if k == "SymbolKind.InstanceBody":
            if s.hierarchicalPath != path:
                return VisitAction.Skip
            return VisitAction.Advance
        if k in _SCOPE_KINDS:
            return VisitAction.Skip if getattr(s, "isUninstantiated", False) else VisitAction.Advance
        if k in ("SymbolKind.Instance", "SymbolKind.InstanceArray", "SymbolKind.Port"):
            return VisitAction.Skip  # instances handled recursively; ports above
        if k in ("SymbolKind.Variable", "SymbolKind.Net"):
            name = ex.rel(s) or s.name
            if name not in port_names:  # internal symbols backing ports are not new signals
                width, signed, dims = _type_facts(s.type)
                signals.append(SignalInfo(
                    name=name,
                    kind="net" if k == "SymbolKind.Net" else "variable",
                    type_str=str(s.type),
                    location=loc_of(s.location),
                    width=width,
                    signed=signed,
                    array_dims=dims,
                    scope=scope_of(s),
                ))
            # collect drivers
            try:
                for d in mgr.getDrivers(s):
                    cs = d.containingSymbol
                    pk = None
                    if str(cs.kind) == "SymbolKind.ProceduralBlock":
                        pk = _proc_kind_str(cs.procedureKind)
                    drivers.append(DriverInfo(
                        signal=name,
                        driver_kind=str(d.kind).split(".")[-1],
                        proc_kind=pk,
                        location=loc_of(d.sourceRange.start if hasattr(d, "sourceRange") else None),
                    ))
            except Exception:
                pass
            return VisitAction.Skip
        if k == "SymbolKind.ProceduralBlock":
            pk = _proc_kind_str(s.procedureKind)
            sens = _sensitivity(sm, s)
            # mark clock signals
            for ev in sens:
                if ev.get("edge"):
                    clocks.add(ev["signal"])
            procs.append(ProcInfo(
                kind=pk,
                sensitivity=sens,
                location=loc_of(s.location),
                scope=scope_of(s),
            ))
            try:
                ex.statement(s.body, len(procs) - 1, [], assigns)
            except Exception:
                pass
            return VisitAction.Skip
        if k == "SymbolKind.ContinuousAssign":
            procs.append(ProcInfo(kind="assign", sensitivity=[], location=loc_of(s.location), scope=scope_of(s)))
            for a in ex.continuous(s):
                a.proc = len(procs) - 1
                assigns.append(a)
            return VisitAction.Skip
        return VisitAction.Advance

    body.visit(visitor)

    # clock/reset candidates from sensitivity lists
    cands: list[ClockResetCandidate] = []
    for proc in procs:
        evs = proc.sensitivity
        if len(evs) == 1 and evs[0].get("edge"):
            cands.append(ClockResetCandidate(
                signal=evs[0]["signal"],
                edge=evs[0]["edge"],
                is_async_reset=False,
                basis="sensitivity_list",
            ))
        elif len(evs) >= 2:
            # first one is clock, rest are async resets
            cands.append(ClockResetCandidate(
                signal=evs[0]["signal"],
                edge=evs[0]["edge"],
                is_async_reset=False,
                basis="sensitivity_list",
            ))
            for ev in evs[1:]:
                cands.append(ClockResetCandidate(
                    signal=ev["signal"],
                    edge=ev["edge"],
                    is_async_reset=True,
                    basis="sensitivity_list",
                ))

    # deduplicate candidates
    seen_cands: set[tuple] = set()
    dedup_cands: list[ClockResetCandidate] = []
    for c in cands:
        key = (c.signal, c.edge, c.is_async_reset)
        if key not in seen_cands:
            seen_cands.add(key)
            dedup_cands.append(c)

    return InstanceInfo(
        path=path,
        module=module,
        parent=parent_path,
        params=params,
        ports=ports,
        signals=signals,
        procs=procs,
        drivers=drivers,
        clk_rst_candidates=dedup_cands,
        clocks=clocks,
        is_incomplete=False,
        assigns=assigns,
        connections=connections or [],
    )


def _connections(child, parent_ex: Extractor) -> list[dict]:
    out = []
    try:
        for pc in child.portConnections:
            out.append({
                "port": pc.port.name,
                "direction": _dir_str(pc.port.direction),
                "expr": parent_ex.tree(pc.expression) if pc.expression is not None else None,
            })
    except Exception:
        pass
    return out


def _walk_tree(
    sym: InstanceSymbol,
    sm: SourceManager,
    mgr: AnalysisManager,
    parent_path: str | None,
    root_path: Path,
    out: list[InstanceInfo],
    connections: list[dict] | None = None,
) -> None:
    info = _collect_instance(sym, sm, mgr, parent_path, root_path, connections)
    out.append(info)
    ex = Extractor(info.path, sym.body, lambda l: _location_str(sm, l))
    for child in iter_child_instances(sym.body):
        _walk_tree(child, sm, mgr, info.path, root_path, out, _connections(child, ex))


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #

def elaborate(
    files: list[Path],
    top: str,
    include_dirs: list[Path],
    defines: dict[str, str | None],
    root: Path,
) -> ElaborationResult:
    """
    Compile and elaborate the design using slang.

    Returns an ElaborationResult with the full instance tree, errors, and
    warnings. Elaboration continues even if there are errors; affected
    instances are marked is_incomplete=True.
    """
    sm = SourceManager()
    po = PreprocessorOptions()
    po.additionalIncludePaths = [str(d) for d in include_dirs]
    po.predefines = [
        f"{k}={v}" if v is not None else k
        for k, v in defines.items()
    ]

    trees = SyntaxTree.fromFiles([str(f) for f in files], sm, Bag([po]))
    if not isinstance(trees, (list, tuple)):
        trees = [trees]

    comp = Compilation(Bag([]))
    for tree in trees:
        comp.addSyntaxTree(tree)

    # force elaboration
    top_instances = comp.getRoot().topInstances
    all_diags = comp.getAllDiagnostics()

    # run analysis for driver info
    mgr = AnalysisManager()
    mgr.analyze(comp)

    # format diagnostics
    engine = DiagnosticEngine(sm)
    client = TextDiagnosticClient()
    engine.addClient(client)
    for d in all_diags:
        engine.issue(d)

    errors: list[dict] = []
    warnings: list[dict] = []
    for d in all_diags:
        # formatMessage is an instance method on DiagnosticEngine, not a class method
        try:
            msg = engine.formatMessage(d)
        except Exception:
            msg = str(d.code)
        loc = _location_str(sm, d.location)
        entry = {"message": msg, "where": loc, "code": str(d.code)}
        if d.isError():
            errors.append(entry)
        else:
            warnings.append(entry)

    # find the top-level instance
    top_inst = None
    for inst in top_instances:
        if inst.name == top or inst.body.definition.name == top:
            top_inst = inst
            break
    if top_inst is None and top_instances:
        top_inst = top_instances[0]

    instances: list[InstanceInfo] = []
    if top_inst is not None:
        _walk_tree(top_inst, sm, mgr, None, root, instances)
        _relativize(instances, errors + warnings, root)
        # mark all instances incomplete if there are errors
        if errors:
            for inst in instances:
                inst.is_incomplete = True

    return ElaborationResult(
        instances=instances,
        top_path=top_inst.hierarchicalPath if top_inst else top,
        errors=errors,
        warnings=warnings,
    )
