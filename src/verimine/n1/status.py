"""
Module status judgment: OK / OK_WITH_WARNINGS / BLOCKED / CONTAINS_BLOCKED.

The status of an instance depends on its own tool reports and on the status
of its children. CONTAINS_BLOCKED propagates upward: a parent whose child is
BLOCKED becomes CONTAINS_BLOCKED unless the parent itself is BLOCKED.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .elaborate import InstanceInfo
from .baseline import ToolReport


# Severities that block a module from entering N2
BLOCKING_CODES = {
    # slang
    "UnknownModule",
    "MultipleDrivers",
    # verilator — combination of drivers (comb-loop is detected elsewhere)
    "MULTIDRIVEN",
    "COMBDLY",
    # yosys
    "YOSYS_ERROR",
}


@dataclass
class ModuleStatus:
    path: str
    module: str
    status: str            # OK | OK_WITH_WARNINGS | BLOCKED | CONTAINS_BLOCKED
    reason: str
    blocked_tools: list[str]
    blocking_codes: list[str]


def _instance_blocked(inst: InstanceInfo, slang: ToolReport) -> tuple[bool, list[str]]:
    """Check whether this instance itself has blocking issues from slang."""
    blocked_tools: list[str] = []
    # slang errors that mention this instance path
    for err in slang.errors:
        # cheap path-prefix match on message/where
        if inst.path in err.get("message", "") or inst.module in err.get("message", ""):
            if any(code in err["code"] for code in BLOCKING_CODES):
                blocked_tools.append("slang")
                break
    # check multi-driver via driver count (procedural drivers > 1 for same signal)
    driver_count: dict[str, int] = {}
    for d in inst.drivers:
        if d.driver_kind == "Procedural":
            driver_count[d.signal] = driver_count.get(d.signal, 0) + 1
    if any(c > 1 for c in driver_count.values()):
        blocked_tools.append("slang(multi-driver)")
    return bool(blocked_tools), blocked_tools


def judge_status(
    instances: list[InstanceInfo],
    slang: ToolReport,
    verilator: ToolReport,
    yosys: ToolReport,
) -> list[ModuleStatus]:
    """
    Walk the instance tree bottom-up and assign a status to each instance.
    """
    by_path = {inst.path: inst for inst in instances}
    # parent -> children
    children: dict[str | None, list[InstanceInfo]] = {}
    for inst in instances:
        children.setdefault(inst.parent, []).append(inst)

    statuses: dict[str, ModuleStatus] = {}

    # process in reverse order so children come before parents
    # (instances list is in pre-order; reverse to go bottom-up)
    for inst in reversed(instances):
        blocked_tools: list[str] = []
        blocking_codes: list[str] = []
        reason = ""

        # 1) this instance's own slang blocking issues
        own_blocked, own_tools = _instance_blocked(inst, slang)
        if own_blocked:
            blocked_tools.extend(own_tools)
            for d in slang.errors:
                if inst.path in d.get("message", "") or inst.module in d.get("message", ""):
                    blocking_codes.append(d["code"])
            reason = "slang reported blocking errors for this instance"

        # 2) tool-level blocked status: if any tool couldn't read the design,
        #    every instance is considered blocked
        if slang.blocked:
            blocked_tools.append("slang")
        if verilator.blocked:
            blocked_tools.append("verilator")
            reason = reason or "verilator could not read the design"
        if yosys.blocked:
            blocked_tools.append("yosys")
            reason = reason or "yosys could not read the design"

        # collect warnings relevant to this instance from verilator
        v_warnings = [
            w for w in (verilator.warnings + verilator.errors)
            if inst.module in w.get("where", "") or inst.path in w.get("message", "")
        ]
        y_warnings = [
            w for w in (yosys.warnings + yosys.errors)
            if inst.module in w.get("where", "") or inst.path in w.get("message", "")
        ]
        has_warnings = bool(v_warnings or y_warnings)

        # 3) children's statuses
        child_blocked = False
        kids = children.get(inst.path, [])
        for child in kids:
            cs = statuses.get(child.path)
            if cs and cs.status in ("BLOCKED", "CONTAINS_BLOCKED"):
                child_blocked = True

        # decide
        if blocked_tools:
            status = "BLOCKED"
        elif child_blocked:
            status = "CONTAINS_BLOCKED"
            reason = "a sub-module is blocked; consider blackboxing it"
        elif has_warnings:
            status = "OK_WITH_WARNINGS"
            reason = "tool warnings reported (latch, width, etc.)"
        else:
            status = "OK"
            reason = ""

        statuses[inst.path] = ModuleStatus(
            path=inst.path,
            module=inst.module,
            status=status,
            reason=reason,
            blocked_tools=sorted(set(blocked_tools)),
            blocking_codes=sorted(set(blocking_codes)),
        )

    return list(statuses.values())
