"""N3 report: sources, test cases and their split, coverage, the gap list, quality."""
from __future__ import annotations

_KIND = {"waveform": "用户波形", "testbench": "用户 testbench", "random": "随机仿真（约束）",
         "idle": "保守：复位后空闲", "toggle": "保守：单点翻转", "formal": "形式探索"}


def _pct(x) -> str:
    return "-" if x is None else f"{x:.1%}"


def markdown(res, reader: str) -> str:
    n0 = res.n2.n1.n0
    s = res.coverage.summary()
    g = res.coverage.gaps()
    q = res.quality
    L = [f"# 轨迹准备报告：{n0.normalized['design']['name']}", "",
         f"- N3 input_hash：`{res.input_hash[:16]}`，traces_hash：`{res.traces_hash[:16]}`",
         f"- 波形读取：{reader}",
         f"- 质量：**{'合格' if q['verdict'] == 'ok' else '偏低'}**（{'状态' if q['basis'] == 'state' else '分支'}覆盖 "
         f"{_pct(q['ratio'])}，阈值 {_pct(q['threshold'])}）", ""]
    if res.warnings:
        L += ["**提示**", ""] + [f"- [{w['code']}] {w['message']}" for w in res.warnings] + [""]

    L += ["## 测试用例", "", "| 集合 | 用例 | 周期 |", "|---|---|---|"]
    for k, v in res.sets().items():
        L.append(f"| {'S_mine（挖掘）' if k == 'mine' else 'S_val（验证）'} | {v['cases']} | {v['cycles']} |")
    leg = res.legality()
    L += ["", f"合法性：user {leg['user']}，constrained {leg['constrained']}，unknown {leg['unknown']}", "",
          "| 用例 | 来源 | 类型 | 合法性 | 集合 | 周期 |", "|---|---|---|---|---|---|"]
    for c in res.cases:
        L.append(f"| {c.id} | {c.source} | {_KIND.get(c.kind, c.kind)} | {c.legality} | {c.set} | {c.cycles} |")

    L += ["", "## 覆盖", "", "| 项目 | 覆盖 | 比例 |", "|---|---|---|",
          f"| 分支 | {s['branch']['covered']} / {s['branch']['observable']}"
          f"（另有不可观测 {s['branch']['total'] - s['branch']['observable'] - s['branch']['unreachable']}，"
          f"已证明不可达 {s['branch']['unreachable']}） | {_pct(s['branch']['ratio'])} |",
          f"| 状态 | {s['state']['visited']} / {s['state']['total']} | {_pct(s['state']['ratio'])} |",
          f"| 状态迁移 | {s['state']['transitions_seen']} / {s['state']['transitions']} | {_pct(s['state']['transition_ratio'])} |",
          f"| 信号翻转（位） | {s['toggle']['toggled']} / {s['toggle']['bits']} | {_pct(s['toggle']['ratio'])} |", ""]
    if res.explore.get("ran"):
        e = res.explore
        L += [f"形式探索：目标 {e['targets']}，到达 {e['reached']}，证明不可达 {len(e['unreachable'])}，"
              f"未到达 {len(e['unreached'])}", ""]

    L += ["## 缺口清单", ""]
    sec = [("从未执行的分支", [f"`{x['inst']}`：{x['branch']}" + ("（default）" if x["default"] else "")
                         for x in g["branches"]]),
           ("从未到达的状态", [f"`{x['inst']}.{x['state']}` = {x['name']}" for x in g["states"]]),
           ("从未出现的状态迁移", [f"`{x['inst']}.{x['state']}`：{x['from']} → {x['to']}" for x in g["transitions"]]),
           ("没有完整翻转的信号", [f"`{x['signal']}`（{x['width']} 位；未上升位 {x['never_rise'][:8]}，未下降位 "
                             f"{x['never_fall'][:8]}）" for x in g["toggles"]]),
           ("已证明不可达（不算缺口）", [f"`{x['inst']}`：{x['branch']}" for x in g["unreachable_branches"]]
            + [f"`{x['inst']}.{x['state']}` = {x['name']}" for x in g["unreachable_states"]]),
           ("不可观测的分支（波形里缺信号）", [f"`{x['inst']}`：{x['branch']}" for x in g["unobservable_branches"]])]
    for title, items in sec:
        L += [f"### {title}（{len(items)}）", ""] + ([f"- {x}" for x in items[:200]] or ["无。"]) + [""]
    return "\n".join(L)
