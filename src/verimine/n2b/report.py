"""N2b step 4: the standalone structural check report (Markdown; the same data is in n2b.json)."""
from __future__ import annotations

from verimine.n2b.props import CLASSES

_STATUS = {
    "FAILED": "失败（反例）", "PROVEN": "已证明", "BOUNDED": "有界通过", "REACHED": "可达",
    "UNREACHED": "有界内不可达", "UNREACHABLE": "已证明不可达", "ENV_ERROR": "环境错误",
    "SKIPPED": "未验证（无法渲染）", "NOT_RUN": "未运行", "UNDECIDED": "未决",
}


def _cell(s: str) -> str:
    return str(s).replace("|", "\\|").replace("\n", " ")


def markdown(res) -> str:
    n0 = res.n2.n1.n0
    d = n0.normalized["design"]
    e = res.engine
    L = [f"# 结构性检查报告：{d['name']}", ""]
    L += [f"- 顶层：`{d['top']}`",
          f"- build_hash：`{n0.hashes['build_hash'][:16]}`，N2b input_hash：`{res.input_hash[:16]}`",
          "- 引擎：" + (f"Yosys + SymbiYosys（smtbmc {e.solver}），BMC 深度 {e.depth}，k-induction 证明"
                         if e.available else "未运行（工具缺失或已关闭），只有静态结论"),
          "- 交付版：`n2b_bind.sv`，slang 编译" + ("通过" if res.delivery_check.get("ok") else
                                                  f"有 {len(res.delivery_check.get('errors', []))} 个错误"),
          ""]
    if res.warnings:
        L += ["**提示**", ""] + [f"- [{w['code']}] {w['message']}" for w in res.warnings] + [""]

    L += ["## 候选 RTL 缺陷（形式验证失败，优先审阅）", ""]
    defects = res.defects
    if not defects:
        L += ["无。", ""]
    props = {p.id: p for p in res.props}
    for f in defects:
        p = props.get(f.props[0]) if f.props else None
        L += [f"### {f.klass_name} / `{f.check}` — {f.inst}", "", f"- {f.message}"]
        if f.location:
            L.append(f"- 位置：`{f.location}`")
        if p is not None:
            if p.trace:
                L.append(f"- 反例波形：`{p.trace}`")
            if p.delivery:
                L += ["- 断言（交付版）：", "", "```systemverilog", p.delivery, "```"]
        L += ["- 下一步：N8 判根因（可能是 RTL 缺陷，也可能缺环境假设）", ""]

    L += ["## 按检查类汇总", "", "| 检查类 | 发现 | 错误 | 警告 | 提示 | 断言 | cover | 属性状态 |",
          "|---|---|---|---|---|---|---|---|"]
    for k, s in res.summary().items():
        st = "，".join(f"{_STATUS.get(x, x)} {n}" for x, n in s["status"].items()) or "-"
        b = s["by_severity"]
        L.append(f"| {s['name']} | {s['findings']} | {b['错误']} | {b['警告']} | {b['提示']} | "
                 f"{s['asserts']} | {s['covers']} | {st} |")
    L.append("")

    L += ["## 全部发现", ""]
    for k, name in CLASSES.items():
        fs = [f for f in res.findings if f.klass == k]
        if not fs:
            continue
        L += [f"### {name}", "", "| 级别 | 检查 | 实例 | 内容 | 依据 | 位置 |", "|---|---|---|---|---|---|"]
        for f in fs:
            ev = {"static": "静态", "formal": "形式", "static+formal": "静态+形式"}[f.evidence]
            if f.status == "REFUTED":
                ev = "静态（被形式否定）"
            L.append(f"| {f.severity} | `{f.check}` | {_cell(f.inst or f.module)} | {_cell(f.message)} | {ev} | "
                     f"{_cell(f.location)} |")
        L.append("")

    L += ["## 属性明细", "", "| ID | 类型 | 检查 | 实例 | 状态 | 说明 |", "|---|---|---|---|---|---|"]
    for p in res.props:
        st = _STATUS.get(p.status, p.status) + (f"（第 {p.step} 拍）" if p.step is not None else "")
        note = p.message + (f"；未验证原因：{p.skip}" if p.skip else "")
        L.append(f"| {p.id} | {p.kind} | `{p.check}` | {_cell(p.inst)} | {st} | {_cell(note)} |")
    L += ["", "## 说明", "",
          "- 输入端口除复位序列和配置中的 assumes 外不受约束，失败的断言可能是缺环境假设（N3a 补充），由 N8 判定。",
          "- “有界通过/有界内不可达”只在 BMC 深度内成立；“已证明/已证明不可达”由 k-induction 给出。",
          "- 静态结论来自 N1 索引、N2 模型和 slang 分析；组合环按信号粒度计算，只涉及不同位时可能是假环。", ""]
    return "\n".join(L)
