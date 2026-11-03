"""N0a step 8: macros used but never defined.

Value macros (`WIDTH in an expression) block: the user has to give a value, so they go to
unresolved. Conditional macros (only in `ifdef / `ifndef / `elsif) stay undefined by default and
are listed for confirmation without blocking. Macros that pick a build flavour (SYNTHESIS, ...)
get an extra note, since formal should normally follow the synthesizable path.
"""
from __future__ import annotations

from dataclasses import dataclass, field

FLAVOUR_MACROS = {"SYNTHESIS", "SIMULATION", "FPGA", "ASIC", "FORMAL", "VERILATOR", "SIM", "SYNTH"}


@dataclass
class MacroReport:
    value_missing: dict[str, list[str]] = field(default_factory=dict)  # name -> files using it
    cond_missing: dict[str, list[str]] = field(default_factory=dict)
    flavour: list[str] = field(default_factory=list)


def classify(facts: list, defined_elsewhere: set[str]) -> MacroReport:
    """facts: FileFacts of every compile unit and header in scope."""
    defined = set(defined_elsewhere)
    for f in facts:
        defined.update(f.macros_defined)
    rep = MacroReport()
    for f in sorted(facts, key=lambda f: f.path):
        for m in f.macros_used:
            if m not in defined:
                rep.value_missing.setdefault(m, []).append(f.path)
        for m in f.macros_tested:
            if m not in defined and m not in f.macros_used:
                rep.cond_missing.setdefault(m, []).append(f.path)
    for m in list(rep.cond_missing):
        if m in rep.value_missing:
            del rep.cond_missing[m]
    rep.flavour = sorted(m for m in (*rep.value_missing, *rep.cond_missing) if m.upper() in FLAVOUR_MACROS)
    return rep
