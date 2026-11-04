"""N2b data model: findings (what the report lists) and properties (the assertions and covers
generated from templates, sent to the engine).

A property is kept as N1 expression trees, not text, so it can be rendered twice: the delivery
version (standard concurrent SVA in a bind file) and the verification version (immediate
assertions the open-source Yosys front end accepts). See render.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Check classes, in report order (spec table "检查类")
CLASSES = {
    "dead_code": "死代码与不可达分支",
    "fsm": "状态机",
    "array_arith": "数组与算术",
    "x_drive": "X 与驱动",
    "control": "控制完整性",
    "timing": "时序风险",
    "structure": "已识别结构",
    "register": "寄存器检查",
}

ERROR, WARNING, INFO = "错误", "警告", "提示"


@dataclass
class Prop:
    kind: str                     # assert | cover
    check: str                    # e.g. "fsm.illegal_encoding"
    inst: str
    module: str
    expr: dict                    # assertion body / cover target
    guard: dict | None = None     # antecedent: guard |-> expr (or |=> when next)
    next: bool = False            # guard |=> expr: expr is checked one cycle after guard
    clock: dict | None = None     # {"name", "edge"}; None = combinational (checked every step)
    resets: dict[str, str] = field(default_factory=dict)  # disable while active: name -> low|high
    message: str = ""
    location: str = ""
    key: str = ""                 # identity within (check, inst), used for stable ids
    # What to report when the property triggers (assert FAILED, cover not reached):
    # {"message", "severity", "signals", "merge"}; merge = key of a static finding it confirms
    on_trigger: dict = field(default_factory=dict)
    id: str = ""
    # filled in by render / engine
    delivery: str = ""
    verification: str = ""
    skip: str = ""                # why the verification version could not be built
    status: str = ""              # see engine.py
    step: int | None = None       # failing / reaching step
    trace: str = ""


@dataclass
class Finding:
    check: str
    klass: str
    inst: str
    module: str
    message: str
    severity: str = WARNING
    evidence: str = "static"      # static | formal | static+formal
    signals: list[str] = field(default_factory=list)
    location: str = ""
    props: list[str] = field(default_factory=list)   # ids of properties behind it
    detail: dict = field(default_factory=dict)
    status: str = ""              # for formal evidence: FAILED / UNREACHED / UNREACHABLE ...
    candidate_defect: bool = False  # FAILED assertion: shown first, handed to N8

    @property
    def klass_name(self) -> str:
        return CLASSES[self.klass]


def klass_of(check: str) -> str:
    return {
        "dead": "dead_code", "fsm": "fsm", "array": "array_arith", "x": "x_drive",
        "control": "control", "timing": "timing", "counter": "structure", "fifo": "structure",
        "reg": "register",
    }[check.split(".", 1)[0]]


# ---- tree builders --------------------------------------------------------

def ref(n: str) -> dict:
    return {"ref": n}


def const(v: int) -> dict:
    return {"const": v, "text": str(v)}


def op(o: str, *args: dict) -> dict:
    return {"op": o, "args": list(args)}


def AND(*xs: dict | None) -> dict | None:
    xs = [x for x in xs if x is not None]
    if not xs:
        return None
    out = xs[0]
    for x in xs[1:]:
        out = op("&&", out, x)
    return out


def OR(*xs: dict) -> dict:
    out = xs[0]
    for x in xs[1:]:
        out = op("||", out, x)
    return out


def NOT(x: dict) -> dict:
    return op("!", x)


def EQ(a: dict, b: dict) -> dict:
    return op("==", a, b)


def PAST(x: dict) -> dict:
    return op("$past", x)
