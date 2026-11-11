"""N3a report: the assumption table, over-strength results, OPEN assumptions on their own, and
the N2b counterexamples attributed to missing assumptions."""
from __future__ import annotations

from verimine.n3a.cands import RECOMMEND

_STATUS = {"PROPOSED": "待确认", "ACCEPTED": "已接受", "REJECTED": "已拒绝", "DISCHARGED": "已由上层保证",
           "OPEN": "未保证（OPEN）"}


def _cell(s) -> str:
    return str(s).replace("|", "\\|").replace("\n", " ")


def _strength(c) -> str:
    s = c.strength
    if not s:
        return "-"
    if s.get("error"):
        return "检查失败"
    if s["lost_key"]:
        return f"过强：关键状态 {len(s['lost_key'])} 个不可达"
    if s["lost_branch"]:
        return f"使 {len(s['lost_branch'])} 个分支不可达"
    return "通过"


def markdown(res) -> str:
    n0 = res.n3.n2.n1.n0
    L = [f"# 假设挖掘报告：{n0.normalized['design']['name']}", "",
         f"- N3a input_hash：`{res.input_hash[:16]}`，基于 N3 traces_hash `{res.n3.traces_hash[:16]}`",
         "- 状态统计：" + "，".join(f"{_STATUS.get(k, k)} {v}" for k, v in res.counts().items()), ""]
    if res.warnings:
        L += ["**提示**", ""] + [f"- [{w['code']}] {w['message']}" for w in res.warnings] + [""]

    L += ["## 顶层输入的假设", "",
          "| ID | 类型 | 假设 | 置信度 | 来源 | 建议 | 状态 | 过强检查 | 说明 |", "|---|---|---|---|---|---|---|---|---|"]
    for c in res.top:
        L.append(f"| {c.id} | {c.type_name} | `{_cell(c.text)}` | {c.confidence or '-'} | {c.source} | "
                 f"{RECOMMEND[c.recommend] or '-'} | {_STATUS[c.status]} | {_strength(c)} | "
                 f"{_cell(c.reason or '；'.join(c.evidence))} |")
    L += ["", "接受：`verimine n3a <yaml> --accept N3A_0001,N3A_0002`；只接受建议接受的高置信项：`--accept-high`；"
          "拒绝：`--reject N3A_0003`。接受的假设写入配置 `assumes`，参与 build_hash。", ""]

    opened = [c for c in res.sub if c.status == "OPEN"]
    L += ["## OPEN：未被上层保证的子模块假设", ""]
    if not opened:
        L += ["无。", ""]
    else:
        L += ["| ID | 实例 | 假设 | 原因 |", "|---|---|---|---|"]
        L += [f"| {c.id} | {c.scope} | `{_cell(c.text)}` | {_cell(c.reason)} |" for c in opened]
        L += ["", "这些假设在子模块单独验证时需要；在当前顶层下它们不一定成立，子模块的结论只在假设成立时有效。", ""]
    dis = [c for c in res.sub if c.status != "OPEN"]
    if dis:
        L += ["## 其他子模块假设", "", "| ID | 实例 | 假设 | 状态 | 说明 |", "|---|---|---|---|---|"]
        L += [f"| {c.id} | {c.scope} | `{_cell(c.text)}` | {_STATUS[c.status]} | {_cell(c.reason)} |" for c in dis]
        L.append("")

    if res.strength.get("ran"):
        s = res.strength
        L += ["## 过强检查", "",
              f"N2b 的 cover {s['covers']} 条，无假设时可达 {s['baseline_reached']} 条。逐条加入假设后重新检查；"
              "使状态机状态或 FIFO 满/空不可达的判为过强并拒绝，只影响分支的给出警告。", ""]
        comb = s.get("combined")
        if comb:
            L.append(f"配置中的假设与建议接受的假设一起加入（{len(comb['assumes'])} 条）："
                     + ("关键状态全部仍可达" if not comb["lost_key"] else "过强：" + "；".join(comb["lost_key"]))
                     + (f"；另有 {len(comb['lost_branch'])} 个分支不可达" if comb["lost_branch"] else ""))
            L.append("")
        for c in res.top:
            st = c.strength
            if st and (st.get("lost_key") or st.get("lost_branch")):
                L.append(f"- {c.id} `{_cell(c.text)}`：" + "；".join(st["lost_key"] + st["lost_branch"]))
        L.append("")

    if res.env_missing:
        L += ["## N2b 失败断言的归类", "", "| 断言 | 实例 | 内容 | 归类 | 违反的候选 |", "|---|---|---|---|---|"]
        for e in res.env_missing:
            L.append(f"| {e['prop']} | {e['inst']} | {_cell(e['message'])} | "
                     f"{'缺环境假设（env_missing）' if e['class'] == 'env_missing' else '其他'} | "
                     f"{', '.join(e['violated']) or '-'} |")
        L += ["", "反例里的输入违反了候选假设时，归为缺环境假设：接受这些假设后重跑 N2b，这些反例应当消失。", ""]
    return "\n".join(L)
