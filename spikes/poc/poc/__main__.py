"""命令行入口：python -m poc <子命令>。"""

from __future__ import annotations

import argparse
import sys

from poc import config, doctor


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m poc", description="Dramio P0 技术验证脚手架")
    sub = parser.add_subparsers(dest="command", required=True, metavar="<command>")
    doctor.add_parser(sub)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config.load_env()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
