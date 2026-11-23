"""N5 report: what was rendered, the degradations per engine, and the differential test."""
from __future__ import annotations

from verimine import engines

_DIFF = {"consistent": "一致", "mismatch": "不一致", "not_run": "未测"}


def _cell(s) -> str:
    return str(s).replace("|", "\\|").replace("\n", " ")


def markdown(res) -> str:
    n0 = res.n4.n3.n2.n1.n0
    c = res.counts()
    L = [f"# SVA 生成报告：{n0.normalized['design']['name']}", "",
         f"- N5 input_hash：`{res.input_hash[:16]}`，基于 N4 n4_hash `{res.n4.n4_hash[:16]}`",
         f"- 断言 {c['asserts']} 条，前件 cover {c['covers']} 条；交付版 {c['delivery']} 条，验证版 {c['verification']} 条",
         "- 差分测试：" + ("，".join(f"{_DIFF.get(k, k)} {v}" for k, v in c["diff"].items()) or "-"),
         "- 文件：`n5_sva.sv`（检查器模块）、`n5_bind.sv`（bind 语句）、`n5_verification/`（验证版）", ""]
    if res.warnings:
        L += ["**提示**", ""] + [f"- [{w['code']}] {_cell(w['message'])}" for w in res.warnings] + [""]

    L += ["## 降级记录（验证版，引擎 yosys-sby）", ""]
    deg = res.degradations()
    if deg:
        L += ["| 降级 | 条数 | 做法 |", "|---|---|---|"]
        L += [f"| {k} | {v} | {engines.DEGRADE_TEXT[k]} |" for k, v in deg.items()]
    else:
        L.append("无。")
    L += ["", "## 其他引擎（交付版）", "", "| 引擎 | 依据 | 可直接使用 | 需降级 | 不支持或未知 |", "|---|---|---|---|---|"]
    for e, caps in engines.CAPS.items():
        ok = sum(1 for r in res.props if not r.engines[e]["degraded"] and not r.engines[e]["unsupported"])
        dg = sum(1 for r in res.props if r.engines[e]["degraded"] and not r.engines[e]["unsupported"])
        bad = sum(1 for r in res.props if r.engines[e]["unsupported"])
        L.append(f"| {e} | {caps['basis']} | {ok} | {dg} | {bad} |")
    L += ["", "依据：measured 为本机实测，declared 为厂商文档（未在本机验证），pending 为接口待向厂商确认。", ""]

    d = res.diff
    L += ["## 三方差分测试", ""]
    if not d.get("ran"):
        L += [f"未运行：{d.get('reason', '')}", ""]
    else:
        L += [f"向量 {d['cycles']} 拍，来自 N3 用例 " + "、".join(s["case"] for s in d["segments"])
              + f"；Verilator 与 Yosys 的设计取值比较了 {d['model']['compared']} 个点，不一致 {len(d['model']['mismatches'])} 处。", ""]
    L += ["## 属性", "", "| 名字 | 类型 | 实例 | 属性 | 降级 | 差分 | 说明 |", "|---|---|---|---|---|---|---|"]
    for r in res.props:
        text = r.cand.text if r.kind == "assert" else f"cover {_cell(r.prop.expr and '前件')}"
        note = r.skip
        if r.diff.get("status") == "mismatch":
            note = f"IR(仿真轨迹) {r.diff.get('ir_sim')} / 仿真 {r.diff.get('sim')}；IR(形式轨迹) {r.diff.get('ir_formal')} / 形式 {r.diff.get('formal')}"
        L.append(f"| {r.name} | {r.kind} | {r.cand.scope} | `{_cell(text)}` | {', '.join(r.degraded) or '-'} | "
                 f"{_DIFF.get(r.diff.get('status', 'not_run'))} | {_cell(note)} |")
    L.append("")
    return "\n".join(L)
