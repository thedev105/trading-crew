import argparse


def build_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(prog="polytrading", description="Read-only research tools")


def main() -> int:
    build_parser().parse_args()
    return 0
