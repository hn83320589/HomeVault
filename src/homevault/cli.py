"""命令列介面。"""

from __future__ import annotations

import argparse
import sys

from homevault import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="homevault", description="家用增量備份工具")
    parser.add_argument("--version", action="version", version=f"homevault {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    parser.parse_args(argv)
    return 0


def run() -> None:
    sys.exit(main())
