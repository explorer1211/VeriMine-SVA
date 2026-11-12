"""N4 report: the candidate pool by source, with its tags, and what was dropped or deferred."""
from __future__ import annotations

from verimine.n4.ir import SOURCES

_STATUS = {"POOL": "入池", "DEFERRED": "超出配额（暂缓）", "DROPPED": "已淘汰"}
_LEGALITY = {"none": "无轨迹（结构）", "user": "用户轨迹", "constrained": "受约束激励", "unknown": "未知"}


def _cell(s) -> str:
    return str(s).replace("|", "\\|").replace("\n", " ")


def markdown(res) -> str:
    n0 = res.n3.n2.n1.n0
    L = [f"# 候选挖掘报告：{n0.normalized['design']['name']}", "",
         f"- N4 input_hash：`{res.input_hash[:16]}`，基于 N3 traces_hash `{res.n3.traces_hash[:16]}`",
         "- 状态统计：" + "，".join(f"{_STATUS[k]} {v}" for k, v in res.counts().items()),
         "- 入池来源：" + "，".join(f"{k}（{SOURCES[k]}）{v}" for k, v in res.by_source().items()), ""]
    L += ["## 三条路径", "", "| 路径 | 是否运行 | 原始候选 | 说明 |", "|---|---|---|---|"]
    for k in SOURCES:
        p = res.paths[k]
        note = p.get("reason", "")
        if k == "C" and p.get("ran"):
            note = f"调用 {p['calls']} 次，失败 {p['failed']} 次，数据级别 {p['level']}"
        L.append(f"| {k} {SOURCES[k]} | {'是' if p.get('ran') else '否'} | {p.get('raw', '-')} | {_cell(note)} |")
    d = res.paths["dedupe"]
    L += ["", f"规范化去重：{d['raw']} → {d['unique']}。", ""]
    if res.warnings:
        L += ["**提示**", ""] + [f"- [{w['code']}] {w['message']}" for w in res.warnings] + [""]

    b = res.budget
    L += ["## 候选池", "",
          f"按区域（实例 + 后件信号）分配配额：每区域 {b['quota']} 条，覆盖缺口区域 ×{b['gap_boost']}；"
          f"共 {b['regions']} 个区域，其中 {b['gap_regions']} 个触及缺口。", "",
          "| ID | 实例 | 类型 | 候选 | 来源 | 轨迹合法性 | 依赖假设 | 层级 | 缺口 | S_mine 支持 |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    for c in res.pool:
        L.append(f"| {c.id} | {c.scope} | {c.type} | `{_cell(c.text)}` | {'+'.join(c.sources)} | "
                 f"{_LEGALITY[c.legality]}{'，REVIEW_REQUIRED' if c.review_required else ''} | "
                 f"{', '.join(c.assumes) or '-'} | {'端口级' if c.level == 'port' else '内部级'} | "
                 f"{'是' if c.gap else ''} | {c.trace.get('support', '-') if c.trace else '-'} |")
    other = [c for c in res.cands if c.status != "POOL"]
    if other:
        L += ["", "## 淘汰与暂缓", "", "| ID | 实例 | 候选 | 来源 | 状态 | 原因 |", "|---|---|---|---|---|---|"]
        L += [f"| {c.id} | {c.scope} | `{_cell(c.text)}` | {'+'.join(c.sources)} | {_STATUS[c.status]} | "
              f"{_cell(c.reason)} |" for c in other]
    L += ["", "入池候选交给 N5 渲染、N6 编译、N7 验证；这里的 S_mine 检查只用于淘汰在合法轨迹上已被违反的候选。", ""]
    return "\n".join(L)
