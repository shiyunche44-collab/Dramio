"""命令行：

    python3 -m dramio_drama_ir validate [--strict] [--json] FILE...
    python3 -m dramio_drama_ir render FILE

validate 退出码：0 通过；1 文档不合法（--strict 时警告也算）；2 用法或读取错误（文件无法读取、不是 UTF-8、Schema 不合法）。
有文件读取失败时仍会继续校验其余文件。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dramio_drama_ir.checks import validate
from dramio_drama_ir.render import render_markdown
from dramio_drama_ir.report import Issue, Report
from dramio_drama_ir.schema import SchemaError


class ReadError(Exception):
    """文件无法读取或不是 UTF-8，或 Schema 本身不合法：属于用法 / 环境错误（退出码 2）。"""


def _reject_constant(name: str) -> None:
    raise ValueError(f"{name} 不是合法的 JSON 数值")


def _load(path: str) -> tuple[object, Report | None]:
    """返回 (文档, None)；不是合法 JSON 时返回 (None, 含错误的 Report)。无法读取时抛 ReadError。"""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ReadError(f"无法读取：{exc.strerror or exc}") from None
    except UnicodeDecodeError:
        raise ReadError("无法读取：文件不是 UTF-8 编码") from None
    try:
        return json.loads(text, parse_constant=_reject_constant), None
    except json.JSONDecodeError as exc:
        return None, Report(errors=[Issue("$", f"不是合法的 JSON：第 {exc.lineno} 行第 {exc.colno} 列，{exc.msg}")])
    except ValueError as exc:  # NaN / Infinity
        return None, Report(errors=[Issue("$", f"不是合法的 JSON：{exc}")])


def _check(path: str) -> tuple[object, Report]:
    doc, report = _load(path)
    if report is None:
        try:
            report = validate(doc)
        except SchemaError as exc:
            raise ReadError(f"Schema 不合法：{exc}") from None
    return doc, report


def cmd_validate(args: argparse.Namespace) -> int:
    results = []
    failed = usage_error = False
    for path in args.files:
        try:
            _, report = _check(path)
        except ReadError as exc:
            usage_error = True
            print(f"{path}: {exc}", file=sys.stderr)
            results.append({"file": path, "ok": False, "read_error": str(exc), "errors": [], "warnings": []})
            continue
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
    return 2 if usage_error else 1 if failed else 0


def cmd_render(args: argparse.Namespace) -> int:
    try:
        doc, report = _check(args.file)
    except ReadError as exc:
        print(f"{args.file}: {exc}", file=sys.stderr)
        return 2
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
