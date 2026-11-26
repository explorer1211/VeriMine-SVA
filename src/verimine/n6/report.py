"""N6 report: what compiled, what was re-rendered and how, and what is ENV_ERROR."""
from __future__ import annotations

from verimine.n6.variants import D_NAMES, V_NAMES


def _cell(s) -> str:
    return str(s).replace("|", "\\|").replace("\n", " ")


def _leg(ok) -> str:
    return {True: "通过", False: "失败", None: "无"}[ok]


def markdown(res) -> str:
    n0 = res.n5.n4.n3.n2.n1.n0
    c = res.counts()
    L = [f"# 编译检查报告：{n0.normalized['design']['name']}", "",
         f"- N6 input_hash：`{res.input_hash[:16]}`，基于 N5 n5_hash `{res.n5.n5_hash[:16]}`",
         f"- 属性 {c['props']} 条：通过 {c['ok']}，ENV_ERROR {c['env_error']}；重渲染 {c['rerendered']} 条；"
         f"进入 N7 {c['to_n7']} 条",
         "- 文件：`n6_sva.sv`、`n6_bind.sv`（交付版，slang 编译通过的就是这两个文件）、"
         "`n6_verification/`（验证版，Yosys 读入通过）", ""]
    if res.warnings:
        L += ["**提示**", ""] + [f"- [{w['code']}] {_cell(w['message'])}" for w in res.warnings] + [""]

    L += ["## 两路检查", "", "| 版本 | 工具 | 结果 |", "|---|---|---|"]
    for leg, name in (("delivery", "交付版"), ("verification", "验证版")):
        g = res.legs[leg]
        if g["ran"]:
            n = c["delivery_ok" if leg == "delivery" else "verification_ok"]
            L.append(f"| {name} | {g['tool']} | 通过 {n} 条 |")
        else:
            L.append(f"| {name} | - | 未检查：{_cell(g.get('reason', ''))} |")
    L += ["", "## 轮次", "", "| 轮 | 交付版检查 | 交付版失败 | 验证版检查 | 验证版失败 |", "|---|---|---|---|---|"]
    for r in res.rounds:
        d, v = r["delivery"], r["verification"]
        L.append(f"| {r['round']} | {d['checked']} | {_cell('、'.join(d['failed']) or '-')} | {v['checked']} | "
                 f"{_cell('、'.join(v['failed']) or '-')} |")
    L.append("")

    bad = [p for p in res.props if p.status == "ENV_ERROR"]
    L += ["## ENV_ERROR（不进入 N7）", ""]
    if not bad:
        L += ["无。", ""]
    for p in bad:
        L.append(f"### {p.name}")
        L.append("")
        for h in p.history:
            names = D_NAMES if h["leg"] == "delivery" else V_NAMES
            L.append(f"- 第 {h['round']} 轮，{'交付版' if h['leg'] == 'delivery' else '验证版'}"
                     f"（{names.get(h['variant'], h['variant'])}）：{_cell('；'.join(h['diags'][:3]))}")
        L.append("")

    re = [p for p in res.props if p.rerendered and p.status == "OK"]
    L += ["## 重渲染后通过", ""]
    if not re:
        L += ["无。", ""]
    else:
        L += ["| 名字 | 交付版写法 | 验证版写法 | 首次失败原因 | 重做差分 |", "|---|---|---|---|---|"]
        for p in re:
            first = p.history[0]["diags"][0] if p.history and p.history[0]["diags"] else ""
            rd = p.rediff.get("status", "-") if p.v_variant else "-"
            L.append(f"| {p.name} | {D_NAMES[p.d_variant]} | {V_NAMES[p.v_variant]} | {_cell(first)} | {rd} |")
        L.append("")

    L += ["## 属性", "", "| 名字 | 类型 | 实例 | 交付版 | 验证版 | 状态 | 进入 N7 | 说明 |", "|---|---|---|---|---|---|---|---|"]
    for p in res.props:
        note = p.rendered.skip
        if p.diff_status == "mismatch":
            note = (note + "；" if note else "") + "差分测试不一致"
        L.append(f"| {p.name} | {p.kind} | {p.rendered.cand.scope} | {_leg(p.d_ok)} | {_leg(p.v_ok)} | {p.status} | "
                 f"{'是' if p.to_n7 else '否'} | {_cell(note)} |")
    L.append("")
    return "\n".join(L)
