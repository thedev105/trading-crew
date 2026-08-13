from __future__ import annotations

import argparse


def add_ai_subcommands(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    subparsers.add_parser("ai", help="offline semantic research tools")
