"""命令行：

    python3 -m dramio_drama_ir validate [--strict] [--json] FILE...
    python3 -m dramio_drama_ir render FILE

validate 退出码：0 通过；1 文档不合法（--strict 时警告也算）；2 用法或读取错误。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dramio_drama_ir.checks import validate
from dramio_drama_ir.render import render_markdown
from dramio_drama_ir.report import Issue, Report


def _load(path: str) -> tuple[object, Report | None]:
    """返回 (文档, None)；JSON 语法错误时返回 (None, 含错误的 Report)。读取失败时抛 OSError。"""
    text = Path(path).read_text(encoding="utf-8")
    try:
        return json.loads(text), None
    except json.JSONDecodeError as exc:
        return None, Report(errors=[Issue("$", f"不是合法的 JSON：第 {exc.lineno} 行第 {exc.colno} 列，{exc.msg}")])


def cmd_validate(args: argparse.Namespace) -> int:
    results = []
    failed = False
    for path in args.files:
        try:
            doc, report = _load(path)
        except OSError as exc:
            print(f"{path}: 无法读取：{exc.strerror or exc}", file=sys.stderr)
            return 2
        if report is None:
            report = validate(doc)
        ok = report.ok(strict=args.strict)
        failed |= not ok
        results.append({"file": path, "ok": ok, **report.to_dict()})
        if not args.json:
            for issue in report.errors:
                print(f"{path}: 错误 {issue}")
            for issue in report.warnings:
                print(f"{path}: 警告 {issue}")
            verdict = "通过" if ok else "不通过"
            print(f"{path}: {verdict}（{len(report.errors)} 个错误，{len(report.warnings)} 个警告）")
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    return 1 if failed else 0


def cmd_render(args: argparse.Namespace) -> int:
    try:
        doc, report = _load(args.file)
    except OSError as exc:
        print(f"{args.file}: 无法读取：{exc.strerror or exc}", file=sys.stderr)
        return 2
    if report is None:
        report = validate(doc)
    if report.errors:
        for issue in report.errors:
            print(f"{args.file}: 错误 {issue}", file=sys.stderr)
        return 1
    sys.stdout.write(render_markdown(doc))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m dramio_drama_ir", description="DramaIR v0 校验与渲染")
    sub = parser.add_subparsers(dest="command", required=True, metavar="<command>")
    v = sub.add_parser("validate", help="校验 DramaIR 文档")
    v.add_argument("--strict", action="store_true", help="警告也算失败")
    v.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    v.add_argument("files", nargs="+", metavar="FILE")
    v.set_defaults(func=cmd_validate)
    r = sub.add_parser("render", help="渲染成 Markdown 剧本摘要")
    r.add_argument("file", metavar="FILE")
    r.set_defaults(func=cmd_render)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
