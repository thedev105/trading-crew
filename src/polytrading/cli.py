from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import duckdb
import httpx

from polytrading.ai.cli import AIInputError, add_ai_subcommands, run_ai_command
from polytrading.carry.audit import CarryAuditor
from polytrading.carry.discovery import evaluate_discovery
from polytrading.carry.discovery_report import render_discovery_json, render_discovery_text
from polytrading.carry.dossier import (
    evaluate_dossier,
    load_bundled_dossier,
    load_bundled_dossiers,
)
from polytrading.carry.dossier_report import render_dossier_json, render_dossier_text
from polytrading.carry.economics import CandidateEconomicsEvaluator
from polytrading.carry.economics_assembler import EconomicsEvidenceAssembler
from polytrading.carry.economics_models import EconomicsPolicy
from polytrading.carry.economics_report import render_economics_json, render_economics_text
from polytrading.carry.fee_import import parse_reviewed_fee_document, record_reviewed_fees
from polytrading.carry.report import render_json, render_text
from polytrading.carry.study import CarryPersistenceStudy, validate_study_window
from polytrading.carry.study_report import render_study_json, render_study_text
from polytrading.corpus_intake.artifacts import CorpusRunWriter, verify_run
from polytrading.corpus_intake.evidence import (
    POLYMARKET_EVIDENCE_TARGETS,
    SourceUseRunWriter,
    capture_evidence,
    verify_source_use_run,
)
from polytrading.corpus_intake.models import AcquisitionRequest, CorpusIntakeError
from polytrading.corpus_intake.polymarket import acquire_polymarket
from polytrading.corpus_intake.review_queue import prepare_review_queue
from polytrading.corpus_intake.source_policy import IntendedUseScope, SourceUseApproval
from polytrading.domain.models import Asset, Venue, normalize_utc_timestamp
from polytrading.http_client import RetryingTransport as RetryingTransport
from polytrading.http_client import make_public_http_client
from polytrading.lifecycle import (
    OwnedResourceCleanupError,
    async_owned_resource_cleanup,
    cleanup_error_cause,
    owned_resource_cleanup,
)
from polytrading.predictions.cli import add_predictions_subcommands, run_predictions_command
from polytrading.predictions.domain import PredictionSource
from polytrading.predictions.pilot.credential_commands import CredentialCommandError
from polytrading.registry.instruments import InstrumentRegistry
from polytrading.replay import replay_file
from polytrading.storage.store import ConflictingRecordError, DuckDBStore
from polytrading.trial.books import run_trial_book_session
from polytrading.trial.funding import (
    LighterDydxFundingCollector,
    PreparedLighterDydxFundingCycle,
    persist_lighter_dydx_funding_cycle,
    record_late_lighter_dydx_cycle,
)
from polytrading.trial.funding_models import (
    TRIAL_FUNDING_POINT_IN_TIME_LAG,
    resolve_current_trial_cycle_end,
    validate_trial_cycle_timing,
)
from polytrading.trial.funding_report import (
    render_trial_funding_json,
    render_trial_funding_text,
)
from polytrading.trial.health import LighterDydxTrialHealthAuditor
from polytrading.trial.health_models import TrialCollectionStatus
from polytrading.trial.health_report import render_trial_health_json, render_trial_health_text
from polytrading.trial.writer_lease import (
    WriterLeaseUnavailable,
    database_writer_lease,
)
from polytrading.venues.bybit import BybitPublicAdapter
from polytrading.venues.dydx import DydxPublicAdapter
from polytrading.venues.funding_cycle import (
    PointInTimeFundingCollector,
    record_late_funding_cycle,
)
from polytrading.venues.funding_cycle_models import (
    resolve_current_cycle_end,
    validate_cycle_timing,
)
from polytrading.venues.funding_cycle_report import (
    render_funding_cycle_json,
    render_funding_cycle_text,
)
from polytrading.venues.funding_health import FundingCollectionHealthAuditor
from polytrading.venues.funding_health_models import (
    FundingCollectionHealthStatus,
    resolve_health_window,
)
from polytrading.venues.funding_health_report import (
    render_funding_health_json,
    render_funding_health_text,
)
from polytrading.venues.hyperliquid import HyperliquidPublicAdapter
from polytrading.venues.lighter import LighterPublicAdapter
from polytrading.venues.public import AdapterBatch, AdapterWarning, PublicVenueAdapter
from polytrading.venues.recorder import PublicRecorder
from polytrading.venues.synchronized import SynchronizedBookCollector
from polytrading.web.server import serve_dashboard, validate_dashboard_database

_WRITER_LEASE_TIMEOUT_SECONDS = 30.0


class CliUsageError(ValueError):
    """A user-facing command validation error."""


class CorpusCollectionError(RuntimeError):
    """A public corpus source or integrity failure."""


class FundingCycleCollectionError(RuntimeError):
    """A point-in-time funding cycle could not be durably recorded."""


class TrialFundingStoreCloseError(OwnedResourceCleanupError):
    """A trial-funding store failed during cleanup."""

    def __init__(self) -> None:
        super().__init__("TRIAL_FUNDING_STORE_CLOSE_ERROR")


class TrialCommandError(RuntimeError):
    """A trial command failed without exposing machine-local details."""


def _classified_funding_cycle_error(error: BaseException) -> FundingCycleCollectionError:
    classified_error = cleanup_error_cause(error)
    if isinstance(classified_error, ConflictingRecordError):
        code = "FUNDING_CYCLE_PERSISTENCE_CONFLICT"
    elif isinstance(classified_error, WriterLeaseUnavailable):
        code = "FUNDING_CYCLE_WRITER_LEASE_UNAVAILABLE"
    elif isinstance(classified_error, duckdb.Error):
        code = "FUNDING_CYCLE_DATABASE_ERROR"
    elif isinstance(classified_error, httpx.HTTPError):
        code = "FUNDING_CYCLE_HTTP_ERROR"
    elif isinstance(classified_error, OSError):
        code = "FUNDING_CYCLE_FILESYSTEM_ERROR"
    elif isinstance(classified_error, ValueError):
        code = "FUNDING_CYCLE_VALIDATION_ERROR"
    else:
        code = "FUNDING_CYCLE_COLLECTION_ERROR"
    return FundingCycleCollectionError(code)


def _classified_trial_error(command: str, error: BaseException) -> TrialCommandError:
    prefix = {"books": "TRIAL_BOOKS", "health": "TRIAL_HEALTH"}[command]
    classified_error = cleanup_error_cause(error)
    if isinstance(classified_error, duckdb.Error):
        suffix = "DATABASE_ERROR"
    elif isinstance(classified_error, httpx.HTTPError):
        suffix = "HTTP_ERROR"
    elif isinstance(classified_error, OSError):
        suffix = "FILESYSTEM_ERROR"
    elif isinstance(classified_error, ValueError):
        suffix = "VALIDATION_ERROR"
    else:
        suffix = "OPERATION_ERROR"
    return TrialCommandError(f"{prefix}_{suffix}")


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CliUsageError(message)


def build_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(prog="polytrading", description="Read-only research tools")
    commands = parser.add_subparsers(dest="command", required=True)

    replay = commands.add_parser("replay", help="replay public adapter batches")
    replay.add_argument("--input", required=True, type=Path)
    replay.add_argument("--db", required=True, type=Path)

    dashboard = commands.add_parser("dashboard", help="serve the local read-only evidence console")
    dashboard.add_argument("--db", required=True, type=Path)
    dashboard.add_argument("--port", type=_dashboard_port, default=8787)

    carry = commands.add_parser("carry", help="carry research diagnostics")
    carry_commands = carry.add_subparsers(dest="carry_command", required=True)
    audit = carry_commands.add_parser("audit", help="audit point-in-time carry evidence")
    audit.add_argument("--db", required=True, type=Path)
    audit.add_argument("--as-of", required=True)
    audit.add_argument("--format", choices=("text", "json"), default="text")
    study = carry_commands.add_parser(
        "study", help="study fixed-direction cross-venue funding persistence"
    )
    study.add_argument("--db", required=True, type=Path)
    study.add_argument("--asset", required=True, choices=("BTC", "ETH", "SOL"))
    study.add_argument("--start", required=True)
    study.add_argument("--end", required=True)
    study.add_argument("--known-as-of", required=True)
    study.add_argument("--format", choices=("text", "json"), default="text")
    dossier = carry_commands.add_parser(
        "dossier", help="inspect the bundled contract-compatibility evidence"
    )
    dossier.add_argument("--id", default="hyperliquid-dydx-core-v1")
    dossier.add_argument("--format", choices=("text", "json"), default="text")
    discovery = carry_commands.add_parser(
        "discovery", help="rank the bundled venue-compatibility evidence"
    )
    discovery.add_argument("--format", choices=("text", "json"), default="text")
    economics = carry_commands.add_parser(
        "economics", help="evaluate frozen Lighter-dYdX shadow economics"
    )
    economics.add_argument("--policy", required=True, type=Path)
    economics.add_argument("--db", required=True, type=Path)
    economics.add_argument("--evaluated-at", required=True)
    economics.add_argument("--evaluation-id", required=True)
    economics.add_argument("--format", choices=("text", "json"), default="text")

    fees = commands.add_parser("fees", help="reviewed fee evidence operations")
    fee_commands = fees.add_subparsers(dest="fees_command", required=True)
    fee_import = fee_commands.add_parser("import", help="import reviewed fee evidence")
    fee_import.add_argument("--input", required=True, type=Path)
    fee_import.add_argument("--db", required=True, type=Path)

    funding = commands.add_parser("funding", help="prospective funding evidence operations")
    funding_commands = funding.add_subparsers(dest="funding_command", required=True)
    health = funding_commands.add_parser("health", help="audit hourly funding collection health")
    health.add_argument("--db", required=True, type=Path)
    health.add_argument("--hours", type=int, default=24)
    health.add_argument("--as-of")
    health.add_argument("--format", choices=("text", "json"), default="text")

    trial = commands.add_parser("trial", help="candidate evidence operations")
    trial_commands = trial.add_subparsers(dest="trial_command", required=True)
    trial_funding = trial_commands.add_parser(
        "funding", help="collect one prospective Lighter-dYdX funding boundary"
    )
    trial_funding.add_argument("--db", required=True, type=Path)
    trial_funding_mode = trial_funding.add_mutually_exclusive_group(required=True)
    trial_funding_mode.add_argument("--cycle-end")
    trial_funding_mode.add_argument("--current", action="store_true")
    trial_funding.add_argument("--format", choices=("text", "json"), default="text")
    trial_books = trial_commands.add_parser(
        "books", help="collect bounded synchronized Lighter-dYdX books"
    )
    trial_books.add_argument("--db", required=True, type=Path)
    trial_books_mode = trial_books.add_mutually_exclusive_group(required=True)
    trial_books_mode.add_argument("--once", action="store_true")
    trial_books_mode.add_argument("--duration-seconds", type=float)
    trial_books.add_argument("--interval-seconds", type=float, default=5.0)
    trial_health = trial_commands.add_parser(
        "health", help="audit prospective Lighter-dYdX trial evidence"
    )
    trial_health.add_argument("--db", required=True, type=Path)
    trial_health.add_argument("--recent-hours", type=_trial_recent_hours, default=24)
    trial_health.add_argument("--as-of")
    trial_health.add_argument("--format", choices=("text", "json"), default="text")
    trial_paper = trial_commands.add_parser("paper", help="simulated forward paper execution")
    trial_paper_commands = trial_paper.add_subparsers(dest="trial_paper_command", required=True)
    trial_paper_open = trial_paper_commands.add_parser("open", help="open a paper position")
    trial_paper_open.add_argument("--evaluation-id", required=True)
    trial_paper_open.add_argument("--db", required=True, type=Path)
    trial_paper_open.add_argument("--confirm", action="store_true")
    trial_paper_close = trial_paper_commands.add_parser("close", help="close a paper position")
    trial_paper_close.add_argument("--position-id", required=True)
    trial_paper_close.add_argument("--db", required=True, type=Path)
    trial_paper_close.add_argument("--confirm", action="store_true")
    trial_paper_monitor = trial_paper_commands.add_parser(
        "monitor", help="close-eligible positions and accrue hourly funding"
    )
    trial_paper_monitor.add_argument("--db", required=True, type=Path)
    trial_paper_monitor.add_argument("--as-of")

    collect = commands.add_parser("collect", help="collect public market evidence")
    collect_commands = collect.add_subparsers(dest="collect_command", required=True)
    public = collect_commands.add_parser("public", help="collect public instruments and funding")
    public.add_argument(
        "--venue",
        choices=("hyperliquid", "bybit", "dydx", "lighter", "all"),
        required=True,
    )
    public.add_argument("--assets", default="BTC,ETH,SOL")
    public.add_argument("--start")
    public.add_argument("--end")
    public.add_argument("--db", required=True, type=Path)

    funding_cycle = collect_commands.add_parser(
        "funding-cycle", help="collect one point-in-time funding boundary"
    )
    funding_cycle.add_argument("--db", required=True, type=Path)
    funding_cycle.add_argument("--assets", default="BTC,ETH,SOL")
    funding_cycle_mode = funding_cycle.add_mutually_exclusive_group(required=True)
    funding_cycle_mode.add_argument("--cycle-end")
    funding_cycle_mode.add_argument("--current", action="store_true")
    funding_cycle.add_argument("--format", choices=("text", "json"), default="text")

    books = collect_commands.add_parser("books", help="collect synchronized public books")
    books.add_argument(
        "--venue",
        choices=("hyperliquid", "bybit", "dydx", "lighter", "all"),
        required=True,
    )
    books.add_argument("--assets", default="BTC,ETH,SOL")
    mode = books.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--duration-seconds", type=float)
    books.add_argument("--interval-seconds", type=float, default=5.0)
    books.add_argument("--db", required=True, type=Path)

    corpus = collect_commands.add_parser(
        "corpus", help="collect quarantined public corpus review candidates"
    )
    corpus.add_argument("--source", choices=("polymarket",), required=True)
    corpus.add_argument("--output", required=True, type=Path)
    corpus.add_argument("--retrieved-at", required=True)
    corpus.add_argument("--information-cutoff", required=True)
    corpus.add_argument("--max-candidates", required=True, type=int)
    corpus.add_argument("--market-state", choices=("open", "closed"), default="open")
    corpus.add_argument("--page-size", type=int, default=100)
    corpus.add_argument("--max-pages", type=int, default=10)
    corpus.add_argument("--max-response-bytes", type=int, default=16 * 1024 * 1024)
    corpus.add_argument("--request-delay-seconds", type=float, default=0.05)

    source_use = collect_commands.add_parser(
        "source-use", help="capture hash-only official source-use evidence"
    )
    source_use.add_argument("--source", choices=("polymarket",), required=True)
    source_use.add_argument("--output", required=True, type=Path)
    source_use.add_argument("--retrieved-at", required=True)
    source_use.add_argument("--max-response-bytes", type=int, default=2 * 1024 * 1024)

    review_queue = collect_commands.add_parser(
        "review-queue", help="prepare a source-use-gated offline review queue"
    )
    review_queue.add_argument("--intake", required=True, action="append", type=Path)
    review_queue.add_argument("--source-use", required=True, type=Path)
    review_queue.add_argument("--output", required=True, type=Path)
    review_queue.add_argument("--as-of", required=True)
    review_queue.add_argument("--ontology-version", required=True)
    review_queue.add_argument("--approval", type=Path)
    review_queue.add_argument("--reviewer-a")
    review_queue.add_argument("--reviewer-b")
    add_ai_subcommands(commands)
    add_predictions_subcommands(commands)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        arguments = build_parser().parse_args(argv)
        if arguments.command == "dashboard":
            validate_dashboard_database(arguments.db)
            serve_dashboard(arguments.db, arguments.port)
            return 0
        if arguments.command == "replay":
            return _replay(arguments)
        if arguments.command == "carry":
            if arguments.carry_command == "audit":
                return _carry_audit(arguments)
            if arguments.carry_command == "study":
                return _carry_study(arguments)
            if arguments.carry_command == "dossier":
                return _carry_dossier(arguments)
            if arguments.carry_command == "discovery":
                return _carry_discovery(arguments)
            return _carry_economics(arguments)
        if arguments.command == "fees":
            return _fees_import(arguments)
        if arguments.command == "funding":
            return _funding_health(arguments)
        if arguments.command == "trial":
            if arguments.trial_command == "funding":
                return asyncio.run(_trial_funding(arguments))
            if arguments.trial_command == "books":
                return asyncio.run(_trial_books(arguments))
            if arguments.trial_command == "health":
                return _trial_health(arguments)
            if arguments.trial_paper_command == "open":
                return _trial_paper_open(arguments)
            if arguments.trial_paper_command == "close":
                return _trial_paper_close(arguments)
            return _trial_paper_monitor(arguments)
        if arguments.command == "ai":
            return run_ai_command(arguments)
        if arguments.command == "predictions":
            return run_predictions_command(arguments)
        if arguments.collect_command == "public":
            return asyncio.run(_collect_public(arguments))
        if arguments.collect_command == "funding-cycle":
            return asyncio.run(_collect_funding_cycle(arguments))
        if arguments.collect_command == "corpus":
            return asyncio.run(_collect_corpus(arguments))
        if arguments.collect_command == "source-use":
            return asyncio.run(_collect_source_use(arguments))
        if arguments.collect_command == "review-queue":
            return _prepare_review_queue(arguments)
        return asyncio.run(_collect_books(arguments))
    except AIInputError as error:
        print(f"polytrading: AI input rejected: {error}", file=sys.stderr)
        return 1
    except CredentialCommandError as error:
        print(f"polytrading: credential command failed: {error.code}", file=sys.stderr)
        return 64
    except (CliUsageError, ValueError, OSError) as error:
        print(f"polytrading: error: {error}", file=sys.stderr)
        return 2
    except Exception as error:
        print(f"polytrading: collection failed: {error}", file=sys.stderr)
        return 1


def _replay(arguments: argparse.Namespace) -> int:
    with database_writer_lease(arguments.db, timeout_seconds=_WRITER_LEASE_TIMEOUT_SECONDS):
        store = DuckDBStore(arguments.db)
        try:
            count = replay_file(arguments.input, store)
        finally:
            store.close()
    print(f"replayed {count} public adapter batches")
    return 0


def _carry_audit(arguments: argparse.Namespace) -> int:
    as_of = _parse_timestamp(arguments.as_of)
    with database_writer_lease(arguments.db, timeout_seconds=_WRITER_LEASE_TIMEOUT_SECONDS):
        store = DuckDBStore(arguments.db)
        try:
            report = CarryAuditor(
                store,
                max_instrument_age=timedelta(days=7),
                max_funding_age=timedelta(days=7),
                max_book_age=timedelta(seconds=30),
                max_book_cycle_skew=timedelta(seconds=1),
            ).audit(as_of)
        finally:
            store.close()
    renderer = render_json if arguments.format == "json" else render_text
    print(renderer(report))
    return 0


def _carry_study(arguments: argparse.Namespace) -> int:
    start = _parse_timestamp(arguments.start)
    end = _parse_timestamp(arguments.end)
    known_as_of = _parse_timestamp(arguments.known_as_of)
    start, end, known_as_of = validate_study_window(start, end, known_as_of)
    store = DuckDBStore(arguments.db, read_only=True)
    try:
        report = CarryPersistenceStudy(store).run(Asset(arguments.asset), start, end, known_as_of)
    finally:
        store.close()
    renderer = render_study_json if arguments.format == "json" else render_study_text
    print(renderer(report))
    return 0


def _carry_dossier(arguments: argparse.Namespace) -> int:
    report = evaluate_dossier(load_bundled_dossier(arguments.id))
    renderer = render_dossier_json if arguments.format == "json" else render_dossier_text
    print(renderer(report))
    return 0


def _carry_discovery(arguments: argparse.Namespace) -> int:
    reports = tuple(evaluate_dossier(dossier) for dossier in load_bundled_dossiers())
    report = evaluate_discovery(reports)
    renderer = render_discovery_json if arguments.format == "json" else render_discovery_text
    print(renderer(report))
    return 0


class _DuplicatePolicyKeyError(ValueError):
    pass


def _unique_policy_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicatePolicyKeyError("economics policy contains a duplicate JSON key")
        result[key] = value
    return result


_POLICY_DECIMAL_FIELDS = frozenset(
    {
        "account_equity_usd",
        "cash_benchmark_annual_rate",
        "operational_cost_usd",
        "minimum_coverage",
        "maximum_book_age_seconds",
        "maximum_cycle_skew_ms",
        "maximum_hourly_book_age_seconds",
        "maximum_assigned_equity_fraction",
        "maximum_assigned_usd",
        "incomplete_leg_shock",
        "maximum_incomplete_loss_equity_fraction",
        "minimum_hold_return",
        "minimum_profit_usd",
        "minimum_annualized_return",
        "cash_benchmark_spread",
        "maximum_stress_loss_equity_fraction",
        "maximum_drawdown_fraction",
        "forced_exit_depth_multiplier",
        "doubled_cost_multiplier",
    }
)


def _parse_economics_policy_document(payload: bytes) -> EconomicsPolicy:
    try:
        text = payload.decode("utf-8", errors="strict")
        raw = json.loads(text, object_pairs_hook=_unique_policy_object)
    except _DuplicatePolicyKeyError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise CliUsageError("invalid economics policy document") from error
    if not isinstance(raw, dict):
        raise CliUsageError("invalid economics policy document")
    decimal_values = tuple(raw.get(name) for name in _POLICY_DECIMAL_FIELDS if name in raw)
    execution = raw.get("execution_assumptions")
    margins = raw.get("margin_assumptions")
    if isinstance(execution, list):
        decimal_values += tuple(
            item.get("taker_latency_ms")
            for item in execution
            if isinstance(item, dict) and "taker_latency_ms" in item
        )
    if isinstance(margins, list):
        decimal_values += tuple(
            item.get(name)
            for item in margins
            if isinstance(item, dict)
            for name in (
                "initial_margin_fraction",
                "maintenance_margin_fraction",
                "close_out_margin_fraction",
                "liquidation_penalty_fraction",
            )
            if name in item
        )
    if any(not isinstance(value, str) for value in decimal_values):
        raise CliUsageError("invalid economics policy document")
    try:
        return EconomicsPolicy.model_validate_json(text)
    except ValueError as error:
        raise CliUsageError("invalid economics policy document") from error


def _read_cli_bytes(path: Path, label: str) -> bytes:
    try:
        return path.read_bytes()
    except OSError as error:
        raise CliUsageError(f"{label} is unavailable") from error


def _fees_import(arguments: argparse.Namespace) -> int:
    document = parse_reviewed_fee_document(
        _read_cli_bytes(arguments.input, "reviewed fee document")
    )
    store: DuckDBStore | None = None
    try:
        with database_writer_lease(arguments.db, timeout_seconds=_WRITER_LEASE_TIMEOUT_SECONDS):
            store = DuckDBStore(arguments.db)
            inserted = record_reviewed_fees(store, document)
    except (duckdb.Error, ConflictingRecordError, RuntimeError) as error:
        raise CliUsageError("reviewed fee import failed") from error
    finally:
        if store is not None:
            store.close()
    print(f"imported {inserted} reviewed fee schedules")
    return 0


def _carry_economics(arguments: argparse.Namespace) -> int:
    loaded_policy = _parse_economics_policy_document(
        _read_cli_bytes(arguments.policy, "economics policy")
    )
    evaluated_at = _parse_timestamp(arguments.evaluated_at)
    try:
        evaluation_id = UUID(arguments.evaluation_id)
    except (TypeError, ValueError, AttributeError) as error:
        raise CliUsageError("invalid evaluation UUID") from error
    if not arguments.db.is_file():
        raise CliUsageError("economics database is unavailable or not current")
    store: DuckDBStore | None = None
    try:
        with database_writer_lease(arguments.db, timeout_seconds=_WRITER_LEASE_TIMEOUT_SECONDS):
            store = DuckDBStore(arguments.db)
            assembly = EconomicsEvidenceAssembler(store).assemble(loaded_policy)
            report = CandidateEconomicsEvaluator().evaluate(
                assembly,
                evaluated_at=evaluated_at,
                evaluation_id=evaluation_id,
            )
            store.append_economic_evaluation(report)
    except ConflictingRecordError as error:
        raise CliUsageError("economics report persistence conflict") from error
    except (duckdb.Error, RuntimeError) as error:
        raise CliUsageError("economics database is unavailable or not current") from error
    finally:
        if store is not None:
            store.close()
    renderer = render_economics_json if arguments.format == "json" else render_economics_text
    print(renderer(report), end="" if arguments.format == "text" else "\n")
    return 0


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return normalize_utc_timestamp(parsed)
    except ValueError as error:
        raise CliUsageError(f"invalid timestamp {value!r}") from error


def _dashboard_port(value: str) -> int:
    message = "port must be an integer between 1 and 65535"
    try:
        port = int(value, 10)
    except ValueError as error:
        raise argparse.ArgumentTypeError(message) from error
    if not 1 <= port <= 65_535:
        raise argparse.ArgumentTypeError(message)
    return port


def _trial_recent_hours(value: str) -> int:
    message = "recent hours must be an integer between 1 and 2160"
    try:
        hours = int(value, 10)
    except ValueError as error:
        raise argparse.ArgumentTypeError(message) from error
    if str(hours) != value or not 1 <= hours <= 2_160:
        raise argparse.ArgumentTypeError(message)
    return hours


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _parse_assets(value: str) -> frozenset[Asset]:
    names = value.split(",")
    if not names or any(not name for name in names):
        raise CliUsageError("assets must be a comma-separated subset of BTC,ETH,SOL")
    try:
        assets = frozenset(Asset(name) for name in names)
    except ValueError as error:
        raise CliUsageError("assets must be a comma-separated subset of BTC,ETH,SOL") from error
    if not assets:
        raise CliUsageError("at least one asset is required")
    return assets


def _parse_venues(value: str) -> tuple[Venue, ...]:
    if value == "all":
        return (Venue.BYBIT, Venue.HYPERLIQUID, Venue.DYDX, Venue.LIGHTER)
    return (Venue(value),)


@asynccontextmanager
async def public_adapter_session(
    store: DuckDBStore, venues: Iterable[Venue]
) -> AsyncIterator[tuple[PublicVenueAdapter, ...]]:
    adapters: list[PublicVenueAdapter] = []
    async with async_owned_resource_cleanup() as cleanup:
        for venue in venues:
            client = make_public_http_client()
            cleanup.add(client.aclose)
            if venue is Venue.BYBIT:
                adapters.append(
                    BybitPublicAdapter(
                        client,
                        _utc_now,
                        time.monotonic_ns,
                        instrument_registry=InstrumentRegistry(store),
                    )
                )
            elif venue is Venue.HYPERLIQUID:
                adapters.append(HyperliquidPublicAdapter(client, _utc_now, time.monotonic_ns))
            elif venue is Venue.DYDX:
                adapters.append(DydxPublicAdapter(client, _utc_now, time.monotonic_ns))
            elif venue is Venue.LIGHTER:
                adapters.append(LighterPublicAdapter(client, _utc_now, time.monotonic_ns))
            else:
                raise ValueError(f"unsupported public venue: {venue.value}")
        yield tuple(adapters)


@asynccontextmanager
async def _lighter_dydx_adapter_session() -> AsyncIterator[tuple[PublicVenueAdapter, ...]]:
    async with async_owned_resource_cleanup() as cleanup:
        dydx_client = make_public_http_client()
        cleanup.add(dydx_client.aclose)
        lighter_client = make_public_http_client()
        cleanup.add(lighter_client.aclose)
        yield (
            DydxPublicAdapter(dydx_client, _utc_now, time.monotonic_ns),
            LighterPublicAdapter(lighter_client, _utc_now, time.monotonic_ns),
        )


def _render_adapter_warning(warning: AdapterWarning) -> str:
    return (
        f"polytrading: warning: {warning.venue.value} {warning.code} "
        f"{warning.symbol} {warning.endpoint}: {warning.message}"
    )


def _print_adapter_warning(warning: AdapterWarning) -> None:
    print(_render_adapter_warning(warning), file=sys.stderr)


def _record_public_batch(recorder: PublicRecorder, batch: AdapterBatch) -> None:
    recorder.record(batch)
    for warning in sorted(
        batch.warnings,
        key=lambda item: (
            item.venue.value,
            item.code,
            item.symbol,
            item.endpoint,
            item.message,
        ),
    ):
        _print_adapter_warning(warning)


async def _collect_public(arguments: argparse.Namespace) -> int:
    assets = _parse_assets(arguments.assets)
    venues = _parse_venues(arguments.venue)
    end = _parse_timestamp(arguments.end) if arguments.end else _utc_now()
    start = _parse_timestamp(arguments.start) if arguments.start else end - timedelta(days=7)
    if start > end:
        raise CliUsageError("collection start must not follow end")
    if end - start > timedelta(days=7):
        raise CliUsageError("public funding collection is limited to seven days")
    observed_at = _utc_now()
    with (
        database_writer_lease(arguments.db, timeout_seconds=_WRITER_LEASE_TIMEOUT_SECONDS),
        owned_resource_cleanup() as cleanup,
    ):
        store = DuckDBStore(arguments.db)
        cleanup.add(store.close)
        recorder = PublicRecorder(store)
        async with public_adapter_session(store, venues) as adapters:
            for adapter in adapters:
                _record_public_batch(recorder, await adapter.fetch_instruments(assets, observed_at))
            for adapter in adapters:
                _record_public_batch(
                    recorder, await adapter.fetch_market_snapshots(assets, observed_at)
                )
                for asset in sorted(assets, key=lambda item: item.value):
                    if adapter.venue is Venue.BYBIT and not _has_bybit_history_basis(
                        store, asset, start
                    ):
                        print(
                            f"polytrading: warning: Bybit {asset.value} funding was not collected "
                            f"for {start.isoformat()}..{end.isoformat()}: no Bybit instrument "
                            "specification was known at the range start",
                            file=sys.stderr,
                        )
                        continue
                    _record_public_batch(
                        recorder,
                        await adapter.fetch_funding_history(asset, start, end, observed_at),
                    )
    print(
        f"completed public collection for {len(assets)} assets across {len(venues)} venues; "
        "see warnings for skipped evidence"
    )
    return 0


async def _collect_funding_cycle(arguments: argparse.Namespace) -> int:
    assets = _parse_assets(arguments.assets)
    now = _utc_now()
    cycle_end = (
        resolve_current_cycle_end(now)
        if arguments.current
        else _parse_timestamp(arguments.cycle_end)
    )
    _, _, is_late = validate_cycle_timing(cycle_end, now)

    try:
        with (
            database_writer_lease(arguments.db, timeout_seconds=_WRITER_LEASE_TIMEOUT_SECONDS),
            owned_resource_cleanup() as cleanup,
        ):
            store = DuckDBStore(arguments.db)
            cleanup.add(store.close)
            if is_late:
                cycle = record_late_funding_cycle(store, assets, cycle_end, now)
            else:
                async with public_adapter_session(
                    store, (Venue.BYBIT, Venue.HYPERLIQUID)
                ) as adapters:
                    cycle = await PointInTimeFundingCollector(store, clock=_utc_now).collect_once(
                        adapters, assets, cycle_end
                    )
    except (
        ConflictingRecordError,
        duckdb.Error,
        httpx.HTTPError,
        OSError,
        RuntimeError,
        ValueError,
    ) as error:
        raise _classified_funding_cycle_error(error) from error

    renderer = (
        render_funding_cycle_json if arguments.format == "json" else render_funding_cycle_text
    )
    print(renderer(cycle))
    return 0


async def _trial_funding(arguments: argparse.Namespace) -> int:
    invocation_now = _utc_now()
    cycle_end = (
        resolve_current_trial_cycle_end(invocation_now)
        if arguments.current
        else _parse_timestamp(arguments.cycle_end)
    )
    normalized_cycle_end, normalized_now, is_late = validate_trial_cycle_timing(
        cycle_end, invocation_now
    )
    assets = frozenset(Asset)

    try:
        if is_late:
            cycle = record_late_lighter_dydx_cycle(assets, normalized_cycle_end, normalized_now)
            prepared = PreparedLighterDydxFundingCycle((), (), (), cycle)
            with database_writer_lease(arguments.db, timeout_seconds=0.0):
                _persist_trial_funding(arguments.db, prepared)
        else:
            lease_timeout = (
                normalized_cycle_end + TRIAL_FUNDING_POINT_IN_TIME_LAG - normalized_now
            ).total_seconds()
            with database_writer_lease(arguments.db, timeout_seconds=lease_timeout):
                acquired_at = _utc_now()
                _, acquired_at, became_late = validate_trial_cycle_timing(
                    normalized_cycle_end, acquired_at
                )
                if became_late:
                    cycle = record_late_lighter_dydx_cycle(
                        assets, normalized_cycle_end, acquired_at
                    )
                    prepared = PreparedLighterDydxFundingCycle((), (), (), cycle)
                else:
                    async with _lighter_dydx_adapter_session() as adapters:
                        prepared = await LighterDydxFundingCollector(clock=_utc_now).prepare_once(
                            adapters, assets, normalized_cycle_end
                        )
                    cycle = prepared.cycle
                _persist_trial_funding(arguments.db, prepared)
    except Exception as error:
        raise _classified_funding_cycle_error(error) from error

    renderer = (
        render_trial_funding_json if arguments.format == "json" else render_trial_funding_text
    )
    print(renderer(cycle))
    return 0


async def _trial_books(arguments: argparse.Namespace) -> int:
    duration = None if arguments.once else arguments.duration_seconds
    if duration is not None and (not math.isfinite(duration) or duration <= 0):
        raise CliUsageError("duration seconds must be a finite positive number")
    if not math.isfinite(arguments.interval_seconds) or arguments.interval_seconds <= 0:
        raise CliUsageError("interval seconds must be a finite positive number")

    try:
        async with _lighter_dydx_adapter_session() as adapters:
            summary = await run_trial_book_session(
                adapters,
                arguments.db,
                duration_seconds=duration,
                interval_seconds=arguments.interval_seconds,
                monotonic=time.monotonic,
                wall_clock=_utc_now,
                sleep=asyncio.sleep,
                store_factory=DuckDBStore,
            )
    except Exception as error:
        raise _classified_trial_error("books", error) from error
    print(
        f"trial books: attempted_cycles={summary.attempted_cycles} "
        f"persisted_cycles={summary.persisted_cycles} failed_cycles={summary.failed_cycles} "
        f"skewed_cycles={summary.skewed_cycles} "
        f"lease_skipped_cycles={summary.lease_skipped_cycles}; "
        "Research only — no trading authority."
    )
    return 0 if summary.persisted_cycles else 1


def _persist_trial_funding(database: Path, prepared: PreparedLighterDydxFundingCycle) -> None:
    with owned_resource_cleanup(marker_factory=TrialFundingStoreCloseError) as cleanup:
        store = DuckDBStore(database)
        cleanup.add(store.close)
        persist_lighter_dydx_funding_cycle(store, prepared)


def _trial_health(arguments: argparse.Namespace) -> int:
    as_of = _parse_timestamp(arguments.as_of) if arguments.as_of else _utc_now()
    if not arguments.db.is_file():
        raise CliUsageError("trial health database is unavailable or not current")

    try:
        with owned_resource_cleanup() as cleanup:
            store = DuckDBStore(arguments.db, read_only=True)
            cleanup.add(store.close)
            report = LighterDydxTrialHealthAuditor(store).audit(as_of, arguments.recent_hours)
            renderer = (
                render_trial_health_json if arguments.format == "json" else render_trial_health_text
            )
            rendered = renderer(report)
    except Exception as error:
        raise _classified_trial_error("health", error) from error

    print(rendered, end="" if arguments.format == "text" else "\n")
    return (
        0
        if report.status
        in (
            TrialCollectionStatus.COLLECTING,
            TrialCollectionStatus.READY_FOR_ECONOMICS_EVALUATION,
        )
        else 1
    )


def _trial_paper_open(arguments: argparse.Namespace) -> int:
    if not arguments.confirm:
        print(
            "polytrading: dry run — pass --confirm to open a paper position",
            file=sys.stderr,
        )
        return 2
    try:
        evaluation_id = UUID(arguments.evaluation_id)
    except (TypeError, ValueError, AttributeError) as error:
        raise CliUsageError("invalid evaluation UUID") from error
    if not arguments.db.is_file():
        raise CliUsageError("paper execution database is unavailable or not current")

    from polytrading.carry.economics_models import EconomicsDecision
    from polytrading.trial.book_evidence import eligible_lighter_dydx_book_pair
    from polytrading.trial.paper_execution import PaperOpenRejected, open_paper_position

    store: DuckDBStore | None = None
    try:
        with database_writer_lease(arguments.db, timeout_seconds=_WRITER_LEASE_TIMEOUT_SECONDS):
            store = DuckDBStore(arguments.db)
            report = store.get_economic_evaluation(evaluation_id)
            if report is None or report.decision is not EconomicsDecision.SHADOW_CANDIDATE:
                raise CliUsageError("evaluation not found or not a SHADOW_CANDIDATE")
            if store.open_paper_position_for_asset(report.asset) is not None:
                raise CliUsageError(f"a paper position is already open for {report.asset.value}")
            now = _utc_now()
            if now - report.evaluated_at > timedelta(hours=24):
                raise CliUsageError("SHADOW_CANDIDATE report is stale; re-run carry economics")
            cycles = store.book_collection_cycles_between(now - timedelta(minutes=5), now, now)
            eligible = next(
                (
                    item
                    for cycle in sorted(cycles, key=lambda c: c.request_completed_at, reverse=True)
                    if (
                        item := eligible_lighter_dydx_book_pair(
                            store, cycle, report.asset, now, Decimal("1000")
                        )
                    )
                    is not None
                ),
                None,
            )
            if eligible is None:
                raise CliUsageError("no eligible current book cycle to open against")
            lighter_instrument = store.latest_instrument_as_of(
                Venue.LIGHTER, eligible.pair.lighter.symbol, now
            )
            dydx_instrument = store.latest_instrument_as_of(
                Venue.DYDX, eligible.pair.dydx.symbol, now
            )
            if lighter_instrument is None or dydx_instrument is None:
                raise CliUsageError("current instrument specification is unavailable")
            try:
                position, transaction = open_paper_position(
                    report=report,
                    current_books=eligible.pair,
                    lighter_instrument=lighter_instrument,
                    dydx_instrument=dydx_instrument,
                    position_id=uuid4(),
                    opening_book_cycle_id=eligible.cycle.cycle_id,
                    opened_at=now,
                )
            except PaperOpenRejected as error:
                raise CliUsageError(str(error)) from error
            with store.transaction():
                store.append_paper_position(position)
                store.append_journal_transaction(transaction)
    except ConflictingRecordError as error:
        raise CliUsageError("paper position persistence conflict") from error
    finally:
        if store is not None:
            store.close()
    print(f"opened paper position {position.position_id} for {position.asset.value}")
    return 0


def _trial_paper_close(arguments: argparse.Namespace) -> int:
    if not arguments.confirm:
        print(
            "polytrading: dry run — pass --confirm to close a paper position",
            file=sys.stderr,
        )
        return 2
    try:
        position_id = UUID(arguments.position_id)
    except (TypeError, ValueError, AttributeError) as error:
        raise CliUsageError("invalid position UUID") from error
    if not arguments.db.is_file():
        raise CliUsageError("paper execution database is unavailable or not current")

    from polytrading.trial.book_evidence import eligible_lighter_dydx_book_pair
    from polytrading.trial.paper_execution import PaperOpenRejected, close_paper_position
    from polytrading.trial.paper_models import PaperCloseReason

    store: DuckDBStore | None = None
    try:
        with database_writer_lease(arguments.db, timeout_seconds=_WRITER_LEASE_TIMEOUT_SECONDS):
            store = DuckDBStore(arguments.db)
            position = store.paper_position(position_id)
            if position is None or store.paper_position_closure(position_id) is not None:
                raise CliUsageError("position not found or already closed")
            now = _utc_now()
            cycles = store.book_collection_cycles_between(now - timedelta(minutes=5), now, now)
            eligible = next(
                (
                    item
                    for cycle in sorted(cycles, key=lambda c: c.request_completed_at, reverse=True)
                    if (
                        item := eligible_lighter_dydx_book_pair(
                            store, cycle, position.asset, now, Decimal("1000")
                        )
                    )
                    is not None
                ),
                None,
            )
            if eligible is None:
                raise CliUsageError("no eligible current book cycle to close against")
            lighter_instrument = store.latest_instrument_as_of(
                Venue.LIGHTER, eligible.pair.lighter.symbol, now
            )
            dydx_instrument = store.latest_instrument_as_of(
                Venue.DYDX, eligible.pair.dydx.symbol, now
            )
            if lighter_instrument is None or dydx_instrument is None:
                raise CliUsageError("current instrument specification is unavailable")
            realized_funding = store.paper_position_realized_funding(position_id)
            try:
                closure, transaction = close_paper_position(
                    position=position,
                    current_books=eligible.pair,
                    lighter_instrument=lighter_instrument,
                    dydx_instrument=dydx_instrument,
                    closing_book_cycle_id=eligible.cycle.cycle_id,
                    closed_at=now,
                    close_reason=PaperCloseReason.OPERATOR_CLOSED,
                    realized_funding_usd=realized_funding,
                )
            except PaperOpenRejected as error:
                raise CliUsageError(str(error)) from error
            with store.transaction():
                store.append_paper_position_closure(closure)
                store.append_journal_transaction(transaction)
    except ConflictingRecordError as error:
        raise CliUsageError("paper position closure persistence conflict") from error
    finally:
        if store is not None:
            store.close()
    print(f"closed paper position {position_id}: realized pnl {closure.realized_pnl_usd}")
    return 0


def _trial_paper_monitor(arguments: argparse.Namespace) -> int:
    as_of = _parse_timestamp(arguments.as_of) if arguments.as_of else _utc_now()
    if not arguments.db.is_file():
        raise CliUsageError("paper execution database is unavailable or not current")

    from polytrading.carry.economics_funding import orient_funding
    from polytrading.trial.book_evidence import eligible_lighter_dydx_book_pair
    from polytrading.trial.funding_lineage import select_prospective_funding
    from polytrading.trial.paper_execution import (
        close_paper_position,
        current_regime_reversed,
        funding_accrual_transaction,
    )
    from polytrading.trial.paper_models import PaperCloseReason

    symbols = {
        Venue.DYDX: {Asset.BTC: "BTC-USD", Asset.ETH: "ETH-USD", Asset.SOL: "SOL-USD"},
        Venue.LIGHTER: {Asset.BTC: "BTC", Asset.ETH: "ETH", Asset.SOL: "SOL"},
    }
    store: DuckDBStore | None = None
    lines: list[str] = []
    try:
        with database_writer_lease(arguments.db, timeout_seconds=_WRITER_LEASE_TIMEOUT_SECONDS):
            store = DuckDBStore(arguments.db)
            for asset in (Asset.BTC, Asset.ETH, Asset.SOL):
                try:
                    position = store.open_paper_position_for_asset(asset)
                    if position is None:
                        continue
                    window_start = as_of - timedelta(hours=168)
                    lighter_selection = select_prospective_funding(
                        store,
                        Venue.LIGHTER,
                        symbols[Venue.LIGHTER][asset],
                        asset,
                        window_start,
                        as_of,
                        as_of,
                    )
                    dydx_selection = select_prospective_funding(
                        store,
                        Venue.DYDX,
                        symbols[Venue.DYDX][asset],
                        asset,
                        window_start,
                        as_of,
                        as_of,
                    )
                    lighter_by_hour = {o.effective_at: o for o in lighter_selection.observations}
                    dydx_by_hour = {o.effective_at: o for o in dydx_selection.observations}
                    paired_hours = sorted(set(lighter_by_hour) & set(dydx_by_hour))
                    differentials = tuple(
                        lighter_by_hour[hour].rate - dydx_by_hour[hour].rate
                        for hour in paired_hours
                    )
                    # The 28-day max-horizon close needs no funding evidence at all, so
                    # it must be evaluated independently of (and before) the funding
                    # coverage gate below — otherwise a single missing funding hour
                    # anywhere in the trailing week would silently disable the hard
                    # horizon close too, letting a position run indefinitely past its
                    # stated maximum horizon. Only the regime-reversal check and the
                    # funding-accrual step are gated on coverage.
                    age = as_of - position.opened_at
                    horizon_reached = age >= timedelta(days=28)
                    insufficient_coverage = len(differentials) < 168
                    reversed_regime = False
                    if not insufficient_coverage:
                        oriented = orient_funding(differentials[-168:], position.direction)
                        reversed_regime = current_regime_reversed(oriented)
                    should_close = reversed_regime or horizon_reached
                    if should_close:
                        reason = (
                            PaperCloseReason.REGIME_REVERSED
                            if reversed_regime
                            else PaperCloseReason.MAX_HORIZON_REACHED
                        )
                        cycles = store.book_collection_cycles_between(
                            as_of - timedelta(minutes=5), as_of, as_of
                        )
                        eligible = next(
                            (
                                item
                                for cycle in sorted(
                                    cycles, key=lambda c: c.request_completed_at, reverse=True
                                )
                                if (
                                    item := eligible_lighter_dydx_book_pair(
                                        store, cycle, asset, as_of, Decimal("1000")
                                    )
                                )
                                is not None
                            ),
                            None,
                        )
                        if eligible is None:
                            lines.append(
                                f"{asset.value}: held (no eligible book to close against yet)"
                            )
                            continue
                        lighter_instrument = store.latest_instrument_as_of(
                            Venue.LIGHTER, eligible.pair.lighter.symbol, as_of
                        )
                        dydx_instrument = store.latest_instrument_as_of(
                            Venue.DYDX, eligible.pair.dydx.symbol, as_of
                        )
                        if lighter_instrument is None or dydx_instrument is None:
                            lines.append(
                                f"{asset.value}: held (instrument specification unavailable)"
                            )
                            continue
                        realized_funding = store.paper_position_realized_funding(
                            position.position_id
                        )
                        closure, transaction = close_paper_position(
                            position=position,
                            current_books=eligible.pair,
                            lighter_instrument=lighter_instrument,
                            dydx_instrument=dydx_instrument,
                            closing_book_cycle_id=eligible.cycle.cycle_id,
                            closed_at=as_of,
                            close_reason=reason,
                            realized_funding_usd=realized_funding,
                        )
                        with store.transaction():
                            store.append_paper_position_closure(closure)
                            store.append_journal_transaction(transaction)
                        lines.append(
                            f"{asset.value}: closed:{reason.value} pnl={closure.realized_pnl_usd}"
                        )
                        continue
                    if insufficient_coverage:
                        lines.append(
                            f"{asset.value}: held (insufficient regime evidence this cycle)"
                        )
                        continue
                    latest_hour = paired_hours[-1]
                    accrual = None
                    if (
                        latest_hour > position.opened_at
                        and not store.paper_position_funding_accrued(
                            position.position_id, latest_hour
                        )
                    ):
                        accrual = funding_accrual_transaction(
                            position=position,
                            effective_at=latest_hour,
                            lighter_rate=lighter_by_hour[latest_hour].rate,
                            dydx_rate=dydx_by_hour[latest_hour].rate,
                        )
                    if accrual is not None:
                        store.append_journal_transaction(accrual)
                        lines.append(
                            f"{asset.value}: accrued funding for {latest_hour.isoformat()}"
                        )
                    else:
                        lines.append(f"{asset.value}: held")
                except Exception as error:  # isolate one asset's failure from the others
                    lines.append(f"{asset.value}: error ({error})")
    finally:
        if store is not None:
            store.close()
    if not lines:
        print("no open paper positions")
    else:
        for line in lines:
            print(line)
    return 0


def _funding_health(arguments: argparse.Namespace) -> int:
    as_of = _parse_timestamp(arguments.as_of) if arguments.as_of else _utc_now()
    resolve_health_window(as_of, arguments.hours)
    if not arguments.db.is_file():
        raise CliUsageError("funding health database is unavailable or not current")

    store: DuckDBStore | None = None
    try:
        store = DuckDBStore(arguments.db, read_only=True)
        report = FundingCollectionHealthAuditor(store).audit(as_of, arguments.hours)
    except (duckdb.Error, RuntimeError) as error:
        raise CliUsageError("funding health database is unavailable or not current") from error
    finally:
        if store is not None:
            store.close()

    renderer = (
        render_funding_health_json if arguments.format == "json" else render_funding_health_text
    )
    print(renderer(report))
    return 0 if report.status is FundingCollectionHealthStatus.HEALTHY else 1


async def _collect_corpus(arguments: argparse.Namespace) -> int:
    request = AcquisitionRequest(
        retrieved_at=_parse_timestamp(arguments.retrieved_at),
        information_cutoff=_parse_timestamp(arguments.information_cutoff),
        max_candidates=arguments.max_candidates,
        market_state=arguments.market_state,
        page_size=arguments.page_size,
        max_pages=arguments.max_pages,
        max_response_bytes=arguments.max_response_bytes,
        request_delay_seconds=arguments.request_delay_seconds,
    )
    writer = CorpusRunWriter(arguments.output, project_root=Path.cwd(), request=request)
    try:
        async with make_public_http_client() as client:
            result = await acquire_polymarket(client, request, writer.append_raw_page)
        writer.complete(result)
        summary = verify_run(writer.output)
    except CorpusIntakeError as error:
        raise CorpusCollectionError(str(error)) from error
    print(
        f"captured {summary.candidate_count} review candidates across "
        f"{summary.event_family_count} event families and {summary.raw_page_count} raw pages; "
        "retention review is still required"
    )
    return 0


async def _collect_source_use(arguments: argparse.Namespace) -> int:
    retrieved_at = _parse_timestamp(arguments.retrieved_at)
    writer = SourceUseRunWriter(
        arguments.output,
        project_root=Path.cwd(),
        retrieved_at=retrieved_at,
    )
    scope = IntendedUseScope(
        schema_version=1,
        source=PredictionSource.POLYMARKET,
        maximum_records=1_000,
        local_retention=True,
        derived_semantic_labels=True,
        offline_model_evaluation=True,
        proprietary_trading_research=True,
        redistribution=False,
        generative_model_training=False,
    )
    try:
        async with make_public_http_client() as client:
            evidence = tuple(
                [
                    await capture_evidence(
                        client,
                        target,
                        retrieved_at=retrieved_at,
                        max_response_bytes=arguments.max_response_bytes,
                    )
                    for target in POLYMARKET_EVIDENCE_TARGETS
                ]
            )
        writer.complete(evidence=evidence, scope=scope)
        verified = verify_source_use_run(writer.output)
    except CorpusIntakeError as error:
        raise CorpusCollectionError(str(error)) from error
    print(
        f"captured {verified.evidence_count} official source-use evidence records; "
        "external confirmation is still required and the inquiry remains unsent"
    )
    return 0


def _prepare_review_queue(arguments: argparse.Namespace) -> int:
    if (arguments.reviewer_a is None) != (arguments.reviewer_b is None):
        raise CliUsageError("reviewer-a and reviewer-b must be supplied together")
    approval = (
        SourceUseApproval.model_validate_json(arguments.approval.read_bytes())
        if arguments.approval is not None
        else None
    )
    reviewer_ids = (
        None if arguments.reviewer_a is None else (arguments.reviewer_a, arguments.reviewer_b)
    )
    result = prepare_review_queue(
        intake_directories=tuple(arguments.intake),
        source_use_directory=arguments.source_use,
        output=arguments.output,
        project_root=Path.cwd(),
        as_of=_parse_timestamp(arguments.as_of),
        approval=approval,
        reviewer_ids=reviewer_ids,
        ontology_version=arguments.ontology_version,
    )
    print(
        f"review queue {result.reason_code}: {result.blocked_item_count} blocked inventory "
        f"items and {result.reviewer_packet_count} reviewer packets"
    )
    return 0


def _has_bybit_history_basis(store: DuckDBStore, asset: Asset, start: datetime) -> bool:
    return store.latest_instrument_as_of(Venue.BYBIT, f"{asset.value}USDT", start) is not None


async def _collect_books(arguments: argparse.Namespace) -> int:
    assets = _parse_assets(arguments.assets)
    venues = _parse_venues(arguments.venue)
    duration = None if arguments.once else arguments.duration_seconds
    if duration is not None and (not math.isfinite(duration) or duration <= 0):
        raise CliUsageError("duration seconds must be a finite positive number")
    if not math.isfinite(arguments.interval_seconds) or arguments.interval_seconds <= 0:
        raise CliUsageError("interval seconds must be a finite positive number")
    with (
        database_writer_lease(arguments.db, timeout_seconds=_WRITER_LEASE_TIMEOUT_SECONDS),
        owned_resource_cleanup() as cleanup,
    ):
        store = DuckDBStore(arguments.db)
        cleanup.add(store.close)
        async with public_adapter_session(store, venues) as adapters:
            await collect_book_cycles(
                adapters,
                assets,
                store,
                duration_seconds=duration,
                interval_seconds=arguments.interval_seconds,
                wall_clock=_utc_now,
            )
    print("completed synchronized public book collection")
    return 0


async def collect_book_cycles(
    adapters: Iterable[PublicVenueAdapter],
    assets: frozenset[Asset],
    store: DuckDBStore,
    *,
    duration_seconds: float | None,
    interval_seconds: float,
    monotonic: Callable[[], float] = time.monotonic,
    wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    max_failure_backoff_seconds: float = 30.0,
) -> None:
    if not math.isfinite(interval_seconds) or interval_seconds <= 0:
        raise ValueError("interval seconds must be positive")
    if duration_seconds is not None and (
        not math.isfinite(duration_seconds) or duration_seconds <= 0
    ):
        raise ValueError("duration seconds must be positive")
    if not math.isfinite(max_failure_backoff_seconds) or max_failure_backoff_seconds <= 0:
        raise ValueError("maximum failure backoff must be positive")
    ordered_adapters = tuple(adapters)
    collector = SynchronizedBookCollector(
        store, clock=wall_clock, warning_sink=_print_adapter_warning
    )
    started = monotonic()
    deadline = started + duration_seconds if duration_seconds is not None else math.inf
    consecutive_failures = 0
    while True:
        if monotonic() >= deadline:
            return
        cycle = await collector.collect_once(
            ordered_adapters, assets, normalize_utc_timestamp(wall_clock())
        )
        if duration_seconds is None:
            return
        consecutive_failures = consecutive_failures + 1 if cycle.status == "failed" else 0
        remaining = deadline - monotonic()
        if remaining <= 0:
            return
        delay = interval_seconds
        if consecutive_failures:
            delay = min(
                max_failure_backoff_seconds,
                interval_seconds * 2 ** (consecutive_failures - 1),
            )
        await sleep(min(delay, remaining))
