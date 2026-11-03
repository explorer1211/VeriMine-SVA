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
