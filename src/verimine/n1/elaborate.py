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


@dataclasses.dataclass
class PortInfo:
    name: str
    direction: str       # "in" | "out" | "inout" | "ref"
    type_str: str
    is_interface: bool


@dataclasses.dataclass
class SignalInfo:
    name: str
    kind: str            # "net" | "variable"
    type_str: str
    location: str        # "file:line"


@dataclasses.dataclass
class ProcInfo:
    kind: str            # "always_ff" | "always_comb" | "always_latch" | "always" | "assign" | "initial"
    sensitivity: list[dict]  # [{"signal": "clk", "edge": "posedge"}, ...]
    location: str


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


def _collect_instance(
    sym: InstanceSymbol,
    sm: SourceManager,
    mgr: AnalysisManager,
    parent_path: str | None,
    root_path: Path,
) -> InstanceInfo:
    body = sym.body
    path = sym.hierarchicalPath
    module = body.definition.name
    loc = _location_str(sm, sym.location)

    # params
    params: dict[str, Any] = {}
    for p in body.parameters:
        try:
            v = p.value
            params[p.name] = str(v)
        except Exception:
            params[p.name] = None

    # ports, signals, procs, drivers
    ports: list[PortInfo] = []
    signals: list[SignalInfo] = []
    procs: list[ProcInfo] = []
    drivers: list[DriverInfo] = []
    clocks: set[str] = set()

    def visitor(s):
        k = str(s.kind)
        if k == "SymbolKind.InstanceBody":
            if s.hierarchicalPath != path:
                return VisitAction.Skip
            return VisitAction.Advance
        if k == "SymbolKind.Instance":
            return VisitAction.Skip  # handled recursively
        if k == "SymbolKind.Port":
            try:
                t = str(s.type)
                is_iface = "interface" in t.lower() or s.isNetPort is False and "modport" in t.lower()
            except Exception:
                t = ""
                is_iface = False
            ports.append(PortInfo(
                name=s.name,
                direction=_dir_str(s.direction),
                type_str=t,
                is_interface=is_iface,
            ))
            return VisitAction.Skip
        if k in ("SymbolKind.Variable", "SymbolKind.Net"):
            # Skip internal copies of ports (same name)
            if any(p.name == s.name for p in ports):
                return VisitAction.Skip
            t = str(s.type)
            signals.append(SignalInfo(
                name=s.name,
                kind="net" if k == "SymbolKind.Net" else "variable",
                type_str=t,
                location=_location_str(sm, s.location),
            ))
            # collect drivers
            try:
                for d in mgr.getDrivers(s):
                    cs = d.containingSymbol
                    pk = None
                    if str(cs.kind) == "SymbolKind.ProceduralBlock":
                        pk = _proc_kind_str(cs.procedureKind)
                    drivers.append(DriverInfo(
                        signal=s.name,
                        driver_kind=str(d.kind).split(".")[-1],
                        proc_kind=pk,
                        location=_location_str(sm, d.sourceRange.start if hasattr(d, "sourceRange") else None),
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
                location=_location_str(sm, s.location),
            ))
            return VisitAction.Skip
        if k == "SymbolKind.ContinuousAssign":
            procs.append(ProcInfo(kind="assign", sensitivity=[], location=_location_str(sm, s.location)))
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
    )


def _walk_tree(
    sym: InstanceSymbol,
    sm: SourceManager,
    mgr: AnalysisManager,
    parent_path: str | None,
    root_path: Path,
    out: list[InstanceInfo],
) -> None:
    info = _collect_instance(sym, sm, mgr, parent_path, root_path)
    out.append(info)
    # recurse into sub-instances
    def child_visitor(s):
        if str(s.kind) == "SymbolKind.InstanceBody":
            if s.hierarchicalPath != info.path:
                return VisitAction.Skip
            return VisitAction.Advance
        if str(s.kind) == "SymbolKind.Instance":
            _walk_tree(s, sm, mgr, info.path, root_path, out)
            return VisitAction.Skip
        return VisitAction.Skip
    sym.body.visit(child_visitor)


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
