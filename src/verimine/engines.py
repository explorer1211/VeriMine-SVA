"""Engine capability matrix (spec 引擎抽象层 3). N5 reads it before rendering: a construct an
engine does not support is degraded by the rule listed here, or the property is not rendered
for that engine; every degradation goes into the report.

Values: True / False, or None = not known yet. `basis` says where a row comes from:
  measured   checked on this machine with the installed version (see docs/N5.md)
  declared   vendor documentation, not verified here
  pending    interface still to be confirmed with the vendor
"""
from __future__ import annotations

FEATURES = {
    "concurrent": "并发断言 assert property",
    "disable_iff": "disable iff（复位时关闭）",
    "next": "|=> 下一拍蕴含",
    "past": "$past(e)",
    "past_k": "$past(e, k)，k > 1",
    "wildcard_eq": "通配比较 ==? / !=?",
    "inside": "inside 集合运算",
    "hier_ref": "生成块内信号（层次名）",
    "sequence": "序列（##、[*]、throughout 等）",
    "liveness": "活性（s_eventually）",
    "cover_action": "cover 命中时执行动作",
    "proof_core": "证明核",
    "word_level": "字级建模",
    "incremental": "增量证明",
}

# feature -> (degraded form, description). Missing = no fallback: the property is not rendered
# for that engine.
DEGRADE = {
    "concurrent": "monitor",
    "disable_iff": "gate",
    "next": "past",
    "wildcard_eq": "mask",
    "inside": "or_chain",
}
DEGRADE_TEXT = {
    "monitor": "并发断言降级为监视器：always @(时钟沿) 里的即时断言，跳过初始拍",
    "gate": "disable iff 降级为门控：当拍与上一拍复位都无效时才检查",
    "past": "|=> 降级为 $past：!$past(前件) || 后件，由 past-valid 寄存器门控",
    "mask": "通配比较降级为掩码比较 (x & mask) == value",
    "or_chain": "inside 降级为相等比较的或",
}

CAPS: dict[str, dict] = {
    # measured: Yosys rejects `assert property (@...)` and `==?` (N2b), reads `$past(e, k)`,
    # no hierarchical names; `inside` is never handed to it (always rendered as an or-chain)
    "yosys-sby": {
        "basis": "measured", "role": "formal",
        "concurrent": False, "disable_iff": False, "next": False, "past": True, "past_k": True,
        "wildcard_eq": False, "inside": False, "hier_ref": False, "sequence": False, "liveness": False,
        "cover_action": False, "proof_core": None, "word_level": True, "incremental": False,
    },
    # measured: everything N5 renders, in the differential test (cover actions need --coverage-user)
    "verilator": {
        "basis": "measured", "role": "simulation",
        "concurrent": True, "disable_iff": True, "next": True, "past": True, "past_k": True,
        "wildcard_eq": True, "inside": True, "hier_ref": True, "sequence": None, "liveness": False,
        "cover_action": True, "proof_core": False, "word_level": False, "incremental": False,
    },
    "icarus": {   # measured: rejects `assert property`; hierarchical names work (N3's bench)
        "basis": "measured", "role": "simulation",
        "concurrent": False, "disable_iff": False, "next": False, "past": None, "past_k": None,
        "wildcard_eq": None, "inside": None, "hier_ref": True, "sequence": False, "liveness": False,
        "cover_action": False, "proof_core": False, "word_level": False, "incremental": False,
    },
    "slang": {    # measured: compiles the delivery version and bind file (N6 step 1)
        "basis": "measured", "role": "compile",
        "concurrent": True, "disable_iff": True, "next": True, "past": True, "past_k": True,
        "wildcard_eq": True, "inside": True, "hier_ref": True, "sequence": None, "liveness": None,
        "cover_action": True, "proof_core": False, "word_level": False, "incremental": False,
    },
    **{name: {"basis": "declared", "role": "formal",
              "concurrent": True, "disable_iff": True, "next": True, "past": True, "past_k": True,
              "wildcard_eq": True, "inside": True, "hier_ref": True, "sequence": True, "liveness": True,
              "cover_action": True, "proof_core": None, "word_level": None, "incremental": None}
       for name in ("jaspergold", "vc-formal", "questa-formal")},
    **{name: {"basis": "pending", "role": "formal", **{f: None for f in FEATURES}}
       for name in ("galaxfv", "empyrean-formal-mc", "himaformal-mc")},
}
CAPS_VERSION = 1


def features(ante: dict | None, cons: dict, nxt: bool, resets: dict) -> set[str]:
    """Constructs a property uses (the keys of FEATURES)."""
    out = {"concurrent"}
    if resets:
        out.add("disable_iff")
    if nxt:
        out.add("next")

    def walk(t: dict) -> None:
        if "ref" in t and not t["ref"].replace("_", "a").replace("$", "a").isalnum():
            out.add("hier_ref")
        o = t.get("op")
        if o == "$past":
            out.add("past_k" if t.get("k", 1) > 1 else "past")
        elif o in ("==?", "!=?"):
            out.add("wildcard_eq")
        elif o == "inside":
            out.add("inside")
        for a in t.get("args", []):
            walk(a)

    walk(cons)
    if ante:
        walk(ante)
    return out


def plan(engine: str, used: set[str]) -> tuple[list[str], list[str]]:
    """(degradations applied, unsupported features with no fallback) for an engine."""
    caps = CAPS[engine]
    deg, bad = [], []
    for f in sorted(used):
        if caps.get(f) is True:
            continue
        if f in DEGRADE and caps.get(f) is False:
            deg.append(DEGRADE[f])
        else:
            bad.append(f)
    return deg, bad
