"""verimine command line."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from verimine.config import json_schema
from verimine.issues import StageError


def _cmd_n0(args: argparse.Namespace) -> int:
    from verimine.n0 import run_n0

    try:
        res = run_n0(args.config, workspace=args.workspace, write=not args.dry_run)
    except StageError as e:
        for i in e.issues:
            print(f"错误 {i}", file=sys.stderr)
        return e.exit_code
    if args.json:
        print(json.dumps(res.to_dict(), indent=2, ensure_ascii=False))
        return 0
    for w in res.warnings:
        print(f"警告 {w}", file=sys.stderr)
    d = res.normalized["design"]
    roles = [s["role"] for s in res.sources]
    print(f"N0 完成：{d['name']} (top={d['top']})")
    print(f"  源文件 {roles.count('source')}，库文件 {roles.count('library')}，头文件 {roles.count('include')}")
    for k, v in res.hashes.items():
        print(f"  {k:<12}{v}")
    if res.db:
        state = "新建" if res.db["new_revision"] else "已存在"
        print(f"  revision {state}；结果写入 {res.workspace / 'runs' / res.hashes['run_hash'] / 'n0.json'}")
    return 0


def _cmd_n1(args: argparse.Namespace) -> int:
    from verimine.n1 import run_n1

    try:
        res = run_n1(args.config, workspace=args.workspace, write=not args.dry_run)
    except StageError as e:
        for i in e.issues:
            print(f"错误 {i}", file=sys.stderr)
        return e.exit_code
    if args.json:
        print(json.dumps(res.to_dict(), indent=2, ensure_ascii=False))
        return 0
    for w in res.warnings:
        print(f"警告 [{w['code']}] {w['message']}", file=sys.stderr)
    insts = res.elaboration.instances
    top_status = res.top_status
    print(f"N1 完成：顶层状态={top_status}，实例数={len(insts)}")
    print(f"  index_hash: {res.index_hash}")
    for ms in res.statuses:
        state_icon = {"OK": "✓", "OK_WITH_WARNINGS": "⚠", "BLOCKED": "✗", "CONTAINS_BLOCKED": "△"}.get(ms.status, "?")
        suffix = f"  ← {ms.reason}" if ms.reason else ""
        print(f"  {state_icon} {ms.path} ({ms.module}){suffix}")
    if res.clock_domains.multi_clock_instances:
        print("  多时钟模块（UNSUPPORTED_MULTICLOCK）：")
        for mc in res.clock_domains.multi_clock_instances:
            print(f"    {mc['path']}: {', '.join(mc['clocks'])}")
    if res.db.get("new_index"):
        n0_run = res.n0.hashes["run_hash"]
        print(f"  索引已写入数据库；结果 runs/{n0_run}/n1.json")
    return 0


def _cmd_n2(args: argparse.Namespace) -> int:
    from verimine.n2 import run_n2

    try:
        res = run_n2(args.config, workspace=args.workspace, write=not args.dry_run)
    except StageError as e:
        for i in e.issues:
            print(f"错误 {i}", file=sys.stderr)
        return e.exit_code
    if args.json:
        print(json.dumps(res.to_dict(), indent=2, ensure_ascii=False, sort_keys=True))
        return 0
    for w in res.warnings:
        print(f"警告 [{w['code']}] {w['message']}", file=sys.stderr)
    g = res.vdg
    print(f"N2 完成：建模实例 {len(g.modeled)}，跳过 {len(g.skipped)}；VDG 节点 {len(g.nodes)}，边 {len(g.edges)}")
    print(f"  input_hash: {res.input_hash}")
    print(f"  n2_hash:    {res.n2_hash}")
    for f in res.fsms:
        names = {s["value"]: s["name"] for s in f.states}
        print(f"  状态机 {f.inst}.{f.state}（{f.confidence}）：{len(f.states)} 个状态，"
              f"{len([t for t in f.transitions if not t['implicit']])} 条迁移，复位到 {names.get(f.reset_state, '?')}")
    for p in res.patterns:
        print(f"  {p.kind:<15}{p.inst}.{p.signal}（{p.confidence}）")
    for e in res.events:
        if e.confidence != "低":
            sigs = " / ".join(e.signals.values())
            print(f"  接口事件 {e.kind:<12}{e.inst}: {sigs}（{e.confidence}）")
    top = sorted(res.pagerank.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
    if top:
        print("  PageRank 前 5：" + "，".join(f"{n} {v:.3f}" for n, v in top))
    print(f"  COI 目标 {len(res.cois)} 个")
    if res.db.get("new_model"):
        print(f"  模型已写入数据库；结果 runs/{res.n1.n0.hashes['run_hash']}/n2.json")
    return 0


def _cmd_n2b(args: argparse.Namespace) -> int:
    from verimine.n2b import run_n2b

    try:
        res = run_n2b(args.config, workspace=args.workspace, write=not args.dry_run,
                      formal=not args.no_formal, depth=args.depth, timeout=args.timeout)
    except StageError as e:
        for i in e.issues:
            print(f"错误 {i}", file=sys.stderr)
        return e.exit_code
    if args.json:
        print(json.dumps(res.to_dict(), indent=2, ensure_ascii=False, sort_keys=True))
        return 0
    for w in res.warnings:
        print(f"警告 [{w['code']}] {w['message']}", file=sys.stderr)
    e = res.engine
    st: dict[str, int] = {}
    for p in res.props:
        st[p.status] = st.get(p.status, 0) + 1
    print(f"N2b 完成：发现 {len(res.findings)} 条，其中候选 RTL 缺陷 {len(res.defects)} 条；"
          f"属性 {len(res.props)} 条（" + "，".join(f"{k} {v}" for k, v in sorted(st.items())) + "）")
    print("  引擎：" + (f"SymbiYosys smtbmc {e.solver}，深度 {e.depth}" if e.available else "未运行"))
    print(f"  input_hash: {res.input_hash}")
    print(f"  n2b_hash:   {res.n2b_hash}")
    for k, s in res.summary().items():
        if s["findings"] or s["asserts"] or s["covers"]:
            print(f"  {s['name']:<10} 发现 {s['findings']:>3}   断言 {s['asserts']:>3}   cover {s['covers']:>3}")
    for f in res.defects[:10]:
        print(f"  [缺陷] {f.inst}: {f.message}")
    if res.db.get("new_result"):
        print(f"  报告 runs/{res.n2.n1.n0.hashes['run_hash']}/n2b_report.md，交付版断言 n2b_bind.sv")
    return 0


def _cmd_n3(args: argparse.Namespace) -> int:
    from verimine.n3 import run_n3

    try:
        res = run_n3(args.config, workspace=args.workspace, write=not args.dry_run, auto=args.auto,
                     explore_gaps=args.explore, formal=not args.no_formal, reuse=not args.fresh)
    except StageError as e:
        for i in e.issues:
            print(f"错误 {i}", file=sys.stderr)
        return e.exit_code
    if args.json:
        print(json.dumps(res.to_dict(), indent=2, ensure_ascii=False, sort_keys=True, default=list))
        return 0
    for w in res.warnings:
        print(f"警告 [{w['code']}] {w['message']}", file=sys.stderr)
    s, q, sets, leg = res.coverage.summary(), res.quality, res.sets(), res.legality()
    pct = lambda x: "-" if x is None else f"{x:.0%}"  # noqa: E731
    print(f"N3 完成：{len(res.cases)} 个测试用例；S_mine {sets['mine']['cases']} 个 / {sets['mine']['cycles']} 拍，"
          f"S_val {sets['val']['cases']} 个 / {sets['val']['cycles']} 拍" + ("（复用已有轨迹）" if res.db.get("cached") else ""))
    print(f"  合法性：user {leg['user']}，constrained {leg['constrained']}，unknown {leg['unknown']}")
    print(f"  覆盖：分支 {pct(s['branch']['ratio'])}，状态 {pct(s['state']['ratio'])}，"
          f"迁移 {pct(s['state']['transition_ratio'])}，翻转 {pct(s['toggle']['ratio'])}；"
          f"质量 {'合格' if q['verdict'] == 'ok' else '偏低'}")
    if res.explore.get("ran"):
        e = res.explore
        print(f"  形式探索：目标 {e['targets']}，到达 {e['reached']}，证明不可达 {len(e['unreachable'])}")
    print(f"  input_hash:  {res.input_hash}")
    print(f"  traces_hash: {res.traces_hash}")
    if not args.dry_run:
        print(f"  报告 runs/{res.n2.n1.n0.hashes['run_hash']}/n3_report.md，轨迹表在 traces/")
    return 0


def _cmd_infer(args: argparse.Namespace) -> int:
    from verimine.n0a import infer

    try:
        results = infer(
            args.root, config=args.config, filelist=args.filelist, top=args.top, out=args.out,
            export_f=args.export_f, force=args.force, write=not args.dry_run,
        )
    except StageError as e:
        for i in e.issues:
            print(f"错误 {i}", file=sys.stderr)
        return e.exit_code
    if args.json:
        print(json.dumps([r.to_dict() for r in results], indent=2, ensure_ascii=False))
        return 0
    for r in results:
        if r.mode == "skip":
            print(f"N0a 跳过：{r.config_path} 已确认且完整，可直接运行 verimine n0")
            continue
        state = "已写入" if r.written else "未写入（--dry-run）"
        print(f"N0a 完成：top={r.top}，草稿{state} {r.config_path}")
        if r.written:
            print(f"  推断报告 {r.report_path}")
        if r.filelist_path:
            print(f"  导出 .f {r.filelist_path}")
        if r.unresolved:
            print(f"  待处理 {len(r.unresolved)} 项：")
            for i, u in enumerate(r.unresolved, 1):
                print(f"    {i}. {u}")
            print(f"  处理后运行：verimine confirm {r.config_path}")
        else:
            print(f"  没有待处理项。核对草稿后运行：verimine confirm {r.config_path}")
    return 0


def _parse_define(text: str) -> tuple[str, str | None]:
    name, eq, value = text.partition("=")
    return name, (value if eq else None)


def _cmd_confirm(args: argparse.Namespace) -> int:
    from verimine.n0a import confirm

    ask = None
    if not args.all and not args.resolve and sys.stdin.isatty():
        def ask(item: str) -> bool:
            reply = input(f"{item}\n  已处理？[y/N] ").strip().lower()
            return reply in ("y", "yes", "是")

    try:
        left = confirm(
            args.config, resolve=args.resolve, resolve_all=args.all,
            defines=dict(_parse_define(d) for d in args.define), ask=ask,
        )
    except StageError as e:
        for i in e.issues:
            print(f"错误 {i}", file=sys.stderr)
        return e.exit_code
    if left:
        print(f"仍有 {len(left)} 项未处理，status 保持 draft：", file=sys.stderr)
        for i, u in enumerate(left, 1):
            print(f"  {i}. {u}", file=sys.stderr)
        return 2
    print(f"已确认：{args.config} 的 status 改为 confirmed，可以运行 verimine n0")
    return 0


def _cmd_schema(args: argparse.Namespace) -> int:
    text = json_schema()
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="verimine")
    sub = p.add_subparsers(dest="cmd", required=True)

    n0 = sub.add_parser("n0", help="工程预处理：校验已确认的 YAML，计算哈希，快照并落库")
    n0.add_argument("config", type=Path, help="status 为 confirmed 的 YAML 配置")
    n0.add_argument("--workspace", type=Path, help="快照和数据库目录，默认 <root>/.verimine")
    n0.add_argument("--dry-run", action="store_true", help="只校验和计算哈希，不写快照和数据库")
    n0.add_argument("--json", action="store_true", help="以 JSON 输出完整结果")
    n0.set_defaults(func=_cmd_n0)

    n1 = sub.add_parser("n1", help="结构索引：elaboration、指纹、基准检查、落库")
    n1.add_argument("config", type=Path, help="status 为 confirmed 的 YAML 配置")
    n1.add_argument("--workspace", type=Path, help="输出目录，默认 <root>/.verimine")
    n1.add_argument("--dry-run", action="store_true", help="只跑不写库")
    n1.add_argument("--json", action="store_true", help="以 JSON 输出完整结果")
    n1.set_defaults(func=_cmd_n1)

    n2 = sub.add_parser("n2", help="行为建模：VDG、状态机、计数器/移位寄存器/存储器、接口事件、信号角色、COI")
    n2.add_argument("config", type=Path, help="status 为 confirmed 的 YAML 配置")
    n2.add_argument("--workspace", type=Path, help="输出目录，默认 <root>/.verimine")
    n2.add_argument("--dry-run", action="store_true", help="只跑不写库")
    n2.add_argument("--json", action="store_true", help="以 JSON 输出完整结果")
    n2.set_defaults(func=_cmd_n2)

    n2b = sub.add_parser("n2b", help="结构性检查：按模板生成断言和 cover，形式验证，输出结构性检查报告")
    n2b.add_argument("config", type=Path, help="status 为 confirmed 的 YAML 配置")
    n2b.add_argument("--workspace", type=Path, help="输出目录，默认 <root>/.verimine")
    n2b.add_argument("--dry-run", action="store_true", help="只跑不写库")
    n2b.add_argument("--json", action="store_true", help="以 JSON 输出完整结果")
    n2b.add_argument("--no-formal", action="store_true", help="不跑形式验证，只出静态结论和交付版断言")
    n2b.add_argument("--depth", type=int, help="BMC / 归纳深度，默认 20（run.params.n2b_depth）")
    n2b.add_argument("--timeout", type=int, help="每个 sby 任务的超时秒数，默认 120")
    n2b.set_defaults(func=_cmd_n2b)

    n3 = sub.add_parser("n3", help="轨迹准备：波形 / testbench / 自动激励 → 按用例切分的轨迹、覆盖与缺口")
    n3.add_argument("config", type=Path, help="status 为 confirmed 的 YAML 配置")
    n3.add_argument("--workspace", type=Path, help="输出目录，默认 <root>/.verimine")
    n3.add_argument("--dry-run", action="store_true", help="只跑不写库")
    n3.add_argument("--json", action="store_true", help="以 JSON 输出完整结果")
    n3.add_argument("--auto", action="store_true", help="已有用户波形或 testbench 时也加自动激励")
    n3.add_argument("--explore", action=argparse.BooleanOptionalAction, default=None,
                    help="对覆盖缺口做形式探索（默认：自动激励时开启）")
    n3.add_argument("--no-formal", action="store_true", help="不调用形式引擎")
    n3.add_argument("--fresh", action="store_true", help="不复用已有轨迹，重新仿真")
    n3.set_defaults(func=_cmd_n3)

    inf = sub.add_parser("infer", help="N0a 配置补全：从 RTL / .f / 不完整的 YAML 生成 YAML 草稿和推断报告")
    inf.add_argument("root", type=Path, nargs="?", default=Path("."), help="RTL 所在目录（给了 --config 时忽略）")
    inf.add_argument("--config", type=Path, help="已有但缺字段的 YAML：只补缺的字段")
    inf.add_argument("--filelist", type=Path, help="已有的 .f：YAML 用 design.filelist 引用它")
    inf.add_argument("--top", help="手动指定 top，重新计算闭包")
    inf.add_argument("--out", type=Path, help="YAML 输出路径，默认 <root>/verimine.yaml")
    inf.add_argument("--export-f", action="store_true", help="同时从 YAML 导出 .f")
    inf.add_argument("--force", action="store_true", help="覆盖已存在的 YAML")
    inf.add_argument("--dry-run", action="store_true", help="只推断，不写文件")
    inf.add_argument("--json", action="store_true", help="以 JSON 输出结果摘要")
    inf.set_defaults(func=_cmd_infer)

    cf = sub.add_parser("confirm", help="N0a 用户确认：逐项处理 unresolved，处理完 status 改为 confirmed")
    cf.add_argument("config", type=Path)
    cf.add_argument("--define", action="append", default=[], metavar="NAME[=VALUE]",
                    help="给宏取值，并标记对应的“宏需要取值”项为已处理")
    cf.add_argument("--resolve", type=int, nargs="+", metavar="N", help="把第 N 项标为已处理（从 1 开始）")
    cf.add_argument("--all", action="store_true", help="所有项都已处理（已手工改好 YAML）")
    cf.set_defaults(func=_cmd_confirm)

    sc = sub.add_parser("schema", help="导出配置的 JSON Schema")
    sc.add_argument("--out", type=Path)
    sc.set_defaults(func=_cmd_schema)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
