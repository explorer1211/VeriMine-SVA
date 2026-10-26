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

    sc = sub.add_parser("schema", help="导出配置的 JSON Schema")
    sc.add_argument("--out", type=Path)
    sc.set_defaults(func=_cmd_schema)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
