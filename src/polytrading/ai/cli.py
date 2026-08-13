from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from polytrading.ai.corpus import (
    append_review,
    freeze_manifest,
    import_contract_rows,
    load_contract_imports,
    preregister_corpus,
    validate_corpus,
    write_imported_contracts,
)
from polytrading.ai.review import ReviewRecord


def add_ai_subcommands(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    ai = subparsers.add_parser("ai", help="offline semantic research tools")
    ai_commands = ai.add_subparsers(dest="ai_command", required=True)
    corpus = ai_commands.add_parser("corpus", help="local reviewed corpus workflow")
    corpus_commands = corpus.add_subparsers(dest="corpus_command", required=True)

    preregister = corpus_commands.add_parser("preregister", help="validate corpus policy")
    preregister.add_argument("--policy", required=True, type=Path)
    preregister.add_argument("--dir", required=True, type=Path)

    import_command = corpus_commands.add_parser("import", help="import inert contract rules")
    import_command.add_argument("--input", required=True, type=Path)
    import_command.add_argument("--output", required=True, type=Path)

    for command in ("review", "adjudicate"):
        parser = corpus_commands.add_parser(command, help=f"append a {command} record")
        parser.add_argument("--item-type", choices=("contract", "relationship"), required=True)
        parser.add_argument("--item-id", required=True)
        parser.add_argument("--review-file", required=True, type=Path)

    validate = corpus_commands.add_parser("validate", help="validate local corpus state")
    validate.add_argument("--dir", required=True, type=Path)

    freeze = corpus_commands.add_parser("freeze", help="freeze a reviewed corpus version")
    freeze.add_argument("--dir", required=True, type=Path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="polytrading", description="Offline research tools")
    add_ai_subcommands(parser.add_subparsers(dest="command", required=True))
    return parser


def run_ai_command(arguments: argparse.Namespace) -> int:
    if arguments.corpus_command == "preregister":
        rows = preregister_corpus(arguments.policy, arguments.dir)
        print(f"preregistered {len(rows)} pending progress units")
        return 0
    if arguments.corpus_command == "import":
        imported = import_contract_rows(load_contract_imports(arguments.input))
        write_imported_contracts(arguments.output, imported)
        warning_count = sum(len(item.warnings) for item in imported)
        print(f"imported {len(imported)} immutable contracts with {warning_count} warnings")
        return 0
    if arguments.corpus_command in {"review", "adjudicate"}:
        record = ReviewRecord.model_validate_json(arguments.review_file.read_bytes())
        expected_role = "adjudicator" if arguments.corpus_command == "adjudicate" else "reviewer"
        if record.reviewer_role != expected_role:
            raise ValueError(f"{arguments.corpus_command} requires reviewer role {expected_role!r}")
        if record.item_type != arguments.item_type or record.item_id != arguments.item_id:
            raise ValueError("review file item identity does not match command arguments")
        append_review(Path("data/gold/reviews.jsonl"), record)
        print(f"recorded immutable {expected_role} record {record.review_id}")
        return 0
    if arguments.corpus_command == "validate":
        completion = validate_corpus(arguments.dir)
        print(json.dumps(completion, sort_keys=True))
        return 0
    manifest = freeze_manifest(arguments.dir)
    print(manifest.dataset_id)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    return run_ai_command(build_parser().parse_args(argv))
