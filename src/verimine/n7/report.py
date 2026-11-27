"""N7 report: statuses, the four coverage figures (honest), the gap list, the proof layers."""
from __future__ import annotations

from verimine.n7.status import STATUSES


def _cell(s) -> str:
    return str(s).replace("|", "\\|").replace("\n", " ")


def _pct(x) -> str:
    return "-" if x is None else f"{100 * x:.1f}%"


def markdown(res) -> str:
    n0 = res.n6.n5.n4.n3.n2.n1.n0
    c = res.counts()
    cov = res.coverage
    L = [f"# 验证与覆盖报告：{n0.normalized['design']['name']}", "",
         f"- N7 input_hash：`{res.input_hash[:16]}`，基于 N6 n6_hash `{res.n6.n6_hash[:16]}`",
         f"- 断言 {c['asserts']} 条：" + "，".join(f"{s} {c[s]}" for s in STATUSES if c.get(s)),
         f"- 已证明比例 {_pct(c['proven_ratio'])}；过拟合淘汰 {c['eliminated']} 条",
         f"- 证明：L0 全部 {c['asserts']} 条；最终停在 L1 的 {res.layers.get('L1', 0)} 条、L2 的 "
         f"{res.layers.get('L2', 0)} 条；cover {res.layers.get('covers', 0)} 条；"
         f"resource_limit {res.layers.get('resource_limit', 0)}", ""]
    if res.warnings:
        L += ["**提示**", ""] + [f"- [{w['code']}] {_cell(w['message'])}" for w in res.warnings] + [""]

    L += ["## 覆盖", "", "| 指标 | 值 | 说明 |", "|---|---|---|"]
    coi = cov["coi"]
    L.append(f"| COI 覆盖 | {_pct(coi['ratio'])}（{coi['touched']}/{coi['blocks']} 个逻辑块） | {_cell(coi['note'])} |")
    pc = cov["proof_core"]
    core = "-（缺）" if pc.get("ratio") is None else f"≈{_pct(pc['ratio'])}（变异杀伤近似）"
    L.append(f"| 证明核覆盖 | {core} | {_cell(pc.get('note', ''))} |")
    v = cov["vacuity"]
    L.append(f"| 空泛性 | {_pct(v['ratio'])}（{v['unreachable']}/{v['antecedents']}） | {_cell(v['note'])} |")
    r = cov["reachability"]
    tr = r["branch"]["traces"]
    en = r["branch"]["engine"]
    st = r["state"]["traces"]
    L.append(f"| 可达性（分支） | 轨迹 {_pct(tr.get('ratio'))}；引擎 cover 到达 {en['reached']}/{en['covers']} | "
             f"引擎证不可达 {en['unreachable']} 条 |")
    L.append(f"| 可达性（状态） | 轨迹 {_pct(st.get('ratio'))}；状态迁移 {_pct(st.get('transition_ratio'))} | "
             f"{_cell(r['note'])} |")
    L += ["", "**诚实标注**：COI 覆盖只表示“被已证明断言的影响锥触及”，不等于验证充分；"
              "证明核覆盖在开源栈上暂不可得（能力矩阵 proof_core 为空），以变异杀伤作为补充。", ""]
    ag = res.layers.get("agreement") or {}
    if ag.get("compared"):
        L += [f"跨引擎一致率（{'交付版 Verilator × 验证版 Yosys'}）：{ag['agree']}/{ag['compared']}"
              f"（{_pct(ag['ratio'])}）。{_cell(ag.get('note', ''))}", ""]
    mk = res.coverage["proof_core"]
    if mk.get("ratio") is not None:
        L += [f"变异杀伤：{mk['killed']}/{mk['total']}（{_pct(mk['ratio'])}），另有 {mk.get('invalid', 0)} 个变异体编译不过。", ""]

    L += ["## 覆盖缺口", ""]
    L.append(f"未被任何已证明断言触及的逻辑块 {res.gaps['block_count']} 个：")
    for b in res.gaps["blocks"][:30]:
        L.append(f"- {b['inst']}：{'、'.join(s['signal'] for s in b['signals'][:12])}"
                 + (" …" if len(b["signals"]) > 12 else ""))
    if len(res.gaps["blocks"]) > 30:
        L.append(f"- …共 {len(res.gaps['blocks'])} 个实例")
    tg = res.gaps["trace_gaps"]
    if tg["branches"] or tg["states"]:
        L.append("")
        L.append(f"轨迹未覆盖的分支 {len(tg['branches'])} 个、状态 {len(tg['states'])} 个（形式 cover 未解释为不可达的）：")
        for b in tg["branches"][:10]:
            L.append(f"- {b['inst']}：{_cell(b['branch'])}（{b.get('location', '')}）")
        for s in tg["states"][:10]:
            L.append(f"- {s['inst']}：状态 {s['state']} = {s.get('name', s['value'])}")
    L.append("")

    if res.rounds:
        L += ["## 覆盖驱动闭环", "", "| 轮 | COI 覆盖 | 已证明 | 新增候选 | 缺口信号 | 停止原因 |", "|---|---|---|---|---|---|"]
        for r in res.rounds:
            L.append(f"| {r['round']} | {_pct(r.get('coi'))} | {r.get('proven', '-')} | {r.get('candidates', '-')} | "
                     f"{r.get('gap_signals', '-')} | {_cell(r.get('stop', ''))} |")
        L.append("")

    bad = [p for p in res.asserts if p.status in ("FAILED", "REVIEW_REQUIRED", "VACUOUS")]
    L += ["## 需要处理", ""]
    if not bad:
        L += ["无。", ""]
    for p in bad:
        why = p.eliminated or {"VACUOUS": "前件不可达", "FAILED": "反例"}.get(p.status, "未定")
        L.append(f"- **{p.status}** {p.name}：{why}" + (f"（{_cell(p.cand.text)}）" if p.cand else ""))
    L.append("")

    L += ["## 断言", "", "| 名字 | 实例 | 属性 | sim | formal（层） | cover | 状态 | COI |", "|---|---|---|---|---|---|---|---|"]
    for p in res.asserts:
        f = p.formal.get("status", "NOT_RUN")
        L.append(f"| {p.name} | {p.prop.inst} | `{_cell(p.cand.text if p.cand else p.prop.message)}` | "
                 f"{p.sim.get('status', '-')} | {f}（{p.formal.get('layer') or '-'}） | {p.cover} | **{p.status}** | "
                 f"{p.coi.get('signals', '-')} |")
    L.append("")
    return "\n".join(L)
