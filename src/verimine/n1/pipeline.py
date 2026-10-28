"""
N1 pipeline: orchestrates elaboration → fingerprints → baseline checks →
status judgment → clock-domain analysis → storage.

Entry point: run_n1(config_path, workspace, n0_result, ...)
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path
from typing import Any

from ..n0.pipeline import N0Result, run_n0
from ..issues import IssueLog, StageError
from .elaborate import elaborate, ElaborationResult, InstanceInfo
from .fingerprint import compute_fingerprints
from .baseline import run_verilator, run_yosys, run_slang_baseline, cross_check_ports, ToolReport
from .status import judge_status, ModuleStatus
from .store import write_index


# --------------------------------------------------------------------------- #
# Unsupported-construct detection (step 12)
# --------------------------------------------------------------------------- #

# Slang diagnostic codes that correspond to constructs formal tools can't handle
_UNSUPPORTED_CODES = {
    "RealLiteralOverflow",
    "SystemTimingCheck",
    "ExpectedIdentifier",  # real / delay artefacts surface as parse errors
}

_UNSUPPORTED_PATTERNS = [
    ("real", "real type"),
    ("#", "delay (#)"),
    ("force", "force inside initial"),
    ("release", "release inside initial"),
    ("$time", "$time system task"),
    ("$realtime", "$realtime system task"),
]


def _find_unsupported(elab: ElaborationResult) -> list[dict]:
    """
    Scan slang diagnostics for known unsupported constructs and return a list
    of diagnostic dicts with code N1_UNSUPPORTED.
    """
    found: list[dict] = []
    for err in elab.errors + elab.warnings:
        code = err.get("code", "")
        msg = err.get("message", "").lower()
        for pat, label in _UNSUPPORTED_PATTERNS:
            if pat.lower() in msg:
                found.append({
                    "code": "N1_UNSUPPORTED",
                    "message": f"Unsupported construct: {label} ({err['message']})",
                    "where": err.get("where", ""),
                })
    return found


# --------------------------------------------------------------------------- #
# Clock-domain analysis (steps 9-11)
# --------------------------------------------------------------------------- #

@dataclasses.dataclass
class ClockDomainInfo:
    clocks_by_instance: dict[str, list[str]]   # inst_path -> list of clock names
    multi_clock_instances: list[dict]           # [{"path", "module", "clocks": [...]}]
    cdc_signals: list[dict]                     # [{"signal", "inst_path", "clocks": [...]}]


def _analyze_clock_domains(
    instances: list[InstanceInfo],
    configured_clock: str | None,
    configured_reset_names: set[str],
) -> ClockDomainInfo:
    """
    Build clock domain map from the sensitivity lists of procedural blocks.
    Signals in configured_reset_names are treated as resets, not clocks.
    """
    clocks_by_instance: dict[str, list[str]] = {}
    multi_clock: list[dict] = []
    cdc_signals: list[dict] = []

    for inst in instances:
        # Filter out known reset signals from the clock set
        clk_set: set[str] = inst.clocks - configured_reset_names
        clocks_by_instance[inst.path] = sorted(clk_set)
        if len(clk_set) > 1:
            multi_clock.append({
                "path": inst.path,
                "module": inst.module,
                "clocks": sorted(clk_set),
            })

    # CDC: find signals driven in one clock domain and read in another
    # (simple heuristic: signal appears as driver in one proc with clk A
    #  and as sensitivity in another proc with clk B)
    for inst in instances:
        sig_clocks: dict[str, set[str]] = {}
        for proc in inst.procs:
            clks = {ev["signal"] for ev in proc.sensitivity if ev.get("edge")}
            clks -= configured_reset_names   # exclude resets from clock domain set
            for d in inst.drivers:
                if d.proc_kind in ("always_ff", "always") and d.driver_kind == "Procedural":
                    for clk in clks:
                        sig_clocks.setdefault(d.signal, set()).add(clk)
        for sig, clks in sig_clocks.items():
            if len(clks) > 1:
                cdc_signals.append({
                    "signal": sig,
                    "inst_path": inst.path,
                    "clocks": sorted(clks),
                })

    return ClockDomainInfo(
        clocks_by_instance=clocks_by_instance,
        multi_clock_instances=multi_clock,
        cdc_signals=cdc_signals,
    )


# --------------------------------------------------------------------------- #
# Clock/reset confirmation (step 4)
# --------------------------------------------------------------------------- #

def _confirm_clock_reset(
    instances: list[InstanceInfo],
    configured_clocks: list[dict],
    configured_resets: list[dict],
    log: IssueLog,
) -> None:
    """
    Compare N0's configured clocks/resets with what slang found in the
    sensitivity lists of always_ff blocks. Emit N1_CLOCK_MISMATCH if they
    differ.
    """
    cfg_clock_names = {c["name"] for c in configured_clocks}
    cfg_reset_names = {r["name"] for r in configured_resets}

    found_clocks: set[str] = set()
    found_resets: set[str] = set()

    for inst in instances:
        for cand in inst.clk_rst_candidates:
            if cand.is_async_reset:
                found_resets.add(cand.signal)
            elif cand.edge is not None:
                found_clocks.add(cand.signal)

    for clk in cfg_clock_names:
        if found_clocks and clk not in found_clocks:
            log.warn(
                "N1_CLOCK_MISMATCH",
                f"configured clock '{clk}' not found in any sensitivity list; "
                f"found: {sorted(found_clocks)}",
            )
    for rst in cfg_reset_names:
        if found_resets and rst not in found_resets:
            log.warn(
                "N1_RESET_MISMATCH",
                f"configured reset '{rst}' not found in any sensitivity list; "
                f"found: {sorted(found_resets)}",
            )


# --------------------------------------------------------------------------- #
# Index hash
# --------------------------------------------------------------------------- #

def _compute_index_hash(
    elab: ElaborationResult,
    statuses: list[ModuleStatus],
    fingerprints: dict[str, str],
) -> str:
    """
    Canonical hash of the full structural index.  Used as a reproducibility
    check: same build_hash → same index_hash.
    """
    obj = {
        "instances": [
            {
                "path": i.path,
                "module": i.module,
                "parent": i.parent,
                "params": sorted(i.params.items()),
                "ports": [
                    {"name": p.name, "direction": p.direction,
                     "type": p.type_str, "iface": p.is_interface}
                    for p in sorted(i.ports, key=lambda x: x.name)
                ],
                "signals": [
                    {"name": s.name, "kind": s.kind, "type": s.type_str}
                    for s in sorted(i.signals, key=lambda x: x.name)
                ],
                "procs": [
                    {"kind": p.kind, "sensitivity": p.sensitivity}
                    for p in i.procs
                ],
            }
            for i in sorted(elab.instances, key=lambda x: x.path)
        ],
        "statuses": [
            {"path": s.path, "status": s.status}
            for s in sorted(statuses, key=lambda x: x.path)
        ],
        "fingerprints": sorted(fingerprints.items()),
    }
    payload = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    raw = b"verimine/index/v1\x00" + payload.encode()
    return hashlib.sha256(raw).hexdigest()


# --------------------------------------------------------------------------- #
# Result dataclass
# --------------------------------------------------------------------------- #

@dataclasses.dataclass
class N1Result:
    n0: N0Result
    elaboration: ElaborationResult
    statuses: list[ModuleStatus]
    fingerprints: dict[str, str]
    clock_domains: ClockDomainInfo
    slang_report: Any
    verilator_report: Any
    yosys_report: Any
    port_mismatches: list[dict]
    unsupported: list[dict]
    warnings: list[dict]
    index_hash: str
    db: dict   # {"new_index": bool}

    @property
    def top_status(self) -> str:
        """Overall status of the top-level instance."""
        top_path = self.elaboration.top_path
        for s in self.statuses:
            if s.path == top_path:
                return s.status
        return "UNKNOWN"

    def to_dict(self) -> dict:
        def _inst(i: InstanceInfo) -> dict:
            return {
                "path": i.path,
                "module": i.module,
                "parent": i.parent,
                "params": i.params,
                "ports": [dataclasses.asdict(p) for p in i.ports],
                "signals": [dataclasses.asdict(s) for s in i.signals],
                "procs": [dataclasses.asdict(p) for p in i.procs],
                "drivers": [dataclasses.asdict(d) for d in i.drivers],
                "clk_rst_candidates": [dataclasses.asdict(c) for c in i.clk_rst_candidates],
                "clocks": sorted(i.clocks),
                "is_incomplete": i.is_incomplete,
            }

        return {
            "hashes": self.n0.hashes,
            "index_hash": self.index_hash,
            "top_status": self.top_status,
            "instances": [_inst(i) for i in self.elaboration.instances],
            "module_status": [dataclasses.asdict(s) for s in self.statuses],
            "fingerprints": self.fingerprints,
            "clock_domains": {
                "clocks_by_instance": self.clock_domains.clocks_by_instance,
                "multi_clock_instances": self.clock_domains.multi_clock_instances,
                "cdc_signals": self.clock_domains.cdc_signals,
            },
            "baseline": {
                "slang": {
                    "ok": self.slang_report.ok,
                    "blocked": self.slang_report.blocked,
                    "errors": self.slang_report.errors,
                    "warnings": self.slang_report.warnings,
                },
                "verilator": {
                    "ok": self.verilator_report.ok,
                    "blocked": self.verilator_report.blocked,
                    "errors": self.verilator_report.errors,
                    "warnings": self.verilator_report.warnings,
                },
                "yosys": {
                    "ok": self.yosys_report.ok,
                    "blocked": self.yosys_report.blocked,
                    "errors": self.yosys_report.errors,
                    "warnings": self.yosys_report.warnings,
                },
            },
            "port_mismatches": self.port_mismatches,
            "unsupported": self.unsupported,
            "warnings": self.warnings,
            "db": self.db,
        }


# --------------------------------------------------------------------------- #
# Main entry point
# --------------------------------------------------------------------------- #

def run_n1(
    config_path: Path,
    workspace: Path | None = None,
    n0_result: N0Result | None = None,
    env: dict | None = None,
    tools: dict | None = None,
    write: bool = True,
) -> N1Result:
    """
    Run the N1 stage.

    If n0_result is not provided, run_n0 is called first.
    If write=False, no files or database records are written.
    """
    config_path = Path(config_path)

    # ---- step 0: get N0 result ------------------------------------------
    if n0_result is None:
        n0_result = run_n0(
            config_path,
            workspace=workspace,
            env=env,
            tools=tools,
            write=write,
        )

    norm = n0_result.normalized
    design = norm["design"]
    root = n0_result.root          # authoritative root from N0
    top = design["top"]
    files = [root / f for f in design.get("files", [])]
    include_dirs = [root / d for d in design.get("include_dirs", [])]
    defines: dict[str, str | None] = {
        k: (str(v) if v is not None else None)
        for k, v in design.get("defines", {}).items()
    }
    configured_clocks = norm.get("clocks", [])
    configured_resets = norm.get("resets", [])

    if workspace is None:
        workspace = n0_result.workspace
    workspace = Path(workspace)

    log = IssueLog()

    # ---- step 1: elaborate (slang) ---------------------------------------
    elab = elaborate(files, top, include_dirs, defines, root)

    # ---- step 2: baseline checks -----------------------------------------
    # slang report comes from the already-run elaboration
    slang_report = ToolReport(
        tool="slang",
        ok=len(elab.errors) == 0,
        blocked=False,
        errors=elab.errors,
        warnings=elab.warnings,
        raw_log="",
    )
    verilator_report = run_verilator(files, top, include_dirs, defines)
    yosys_report = run_yosys(files, top, include_dirs, defines)

    # build slang port list from elab
    slang_ports: list[dict] = []
    for inst in elab.instances:
        if inst.parent is None:  # top-level
            slang_ports = [
                {"name": p.name, "direction": p.direction, "type": p.type_str}
                for p in inst.ports
            ]
            break

    # yosys port list from log (not parsed here; just report mismatches if both ok)
    port_mismatches: list[dict] = []
    if not yosys_report.blocked and not slang_report.blocked and slang_ports:
        # parse yosys port list from raw_log
        from .baseline import _yosys_top_ports, cross_check_ports
        yosys_ports = _yosys_top_ports(yosys_report.raw_log, top)
        if yosys_ports:
            port_mismatches = cross_check_ports(slang_ports, yosys_ports)

    # ---- step 3: fingerprints -------------------------------------------
    fingerprints = compute_fingerprints(files, top, include_dirs, defines)

    # ---- step 4: module status ------------------------------------------
    statuses = judge_status(elab.instances, slang_report, verilator_report, yosys_report)

    # ---- step 5: clock/reset confirmation --------------------------------
    _confirm_clock_reset(elab.instances, configured_clocks, configured_resets, log)

    # ---- step 6: clock-domain analysis -----------------------------------
    first_clock = configured_clocks[0]["name"] if configured_clocks else None
    reset_names = {r["name"] for r in configured_resets}
    clock_domains = _analyze_clock_domains(elab.instances, first_clock, reset_names)

    # emit multi-clock warnings
    for mc in clock_domains.multi_clock_instances:
        log.warn(
            "N1_UNSUPPORTED_MULTICLOCK",
            f"module '{mc['module']}' (path '{mc['path']}') has multiple clocks: "
            + ", ".join(mc["clocks"]),
        )

    # emit CDC signal warnings
    for cdc in clock_domains.cdc_signals:
        log.warn(
            "N1_CDC_SIGNAL",
            f"signal '{cdc['signal']}' in '{cdc['inst_path']}' crosses clock domains: "
            + ", ".join(cdc["clocks"]),
        )

    # ---- step 7: unsupported constructs ----------------------------------
    unsupported = _find_unsupported(elab)
    for u in unsupported:
        log.warn("N1_UNSUPPORTED", u["message"], u.get("where", ""))

    # ---- step 8: port mismatches -----------------------------------------
    for pm in port_mismatches:
        log.warn("N1_PORT_MISMATCH", pm["message"], pm.get("where", ""))

    # ---- step 9: index hash ---------------------------------------------
    index_hash = _compute_index_hash(elab, statuses, fingerprints)

    # ---- step 10: write to disk -----------------------------------------
    db_result: dict = {"new_index": False}
    if write:
        db_path = workspace / "verimine.db"
        db_result = write_index(
            db_path,
            build_hash=n0_result.hashes["build_hash"],
            elab=elab,
            statuses=statuses,
            fingerprints=fingerprints,
            index_hash=index_hash,
        )
        # write runs/<run_hash>/n1.json
        run_dir = workspace / "runs" / n0_result.hashes["run_hash"]
        run_dir.mkdir(parents=True, exist_ok=True)
        result_tmp = N1Result(
            n0=n0_result,
            elaboration=elab,
            statuses=statuses,
            fingerprints=fingerprints,
            clock_domains=clock_domains,
            slang_report=slang_report,
            verilator_report=verilator_report,
            yosys_report=yosys_report,
            port_mismatches=port_mismatches,
            unsupported=unsupported,
            warnings=[{"code": w.code, "message": w.message,
                       "where": w.where or ""} for w in log.warnings],
            index_hash=index_hash,
            db=db_result,
        )
        (run_dir / "n1.json").write_text(
            json.dumps(result_tmp.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    return N1Result(
        n0=n0_result,
        elaboration=elab,
        statuses=statuses,
        fingerprints=fingerprints,
        clock_domains=clock_domains,
        slang_report=slang_report,
        verilator_report=verilator_report,
        yosys_report=yosys_report,
        port_mismatches=port_mismatches,
        unsupported=unsupported,
        warnings=[{"code": w.code, "message": w.message,
                   "where": w.where or ""} for w in log.warnings],
        index_hash=index_hash,
        db=db_result,
    )
