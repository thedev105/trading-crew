from __future__ import annotations

import argparse
import asyncio
import math
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import httpx

from polytrading.ai.cli import AIInputError, add_ai_subcommands, run_ai_command
from polytrading.carry.audit import CarryAuditor
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
from polytrading.registry.instruments import InstrumentRegistry
from polytrading.replay import replay_file
from polytrading.storage.store import ConflictingRecordError, DuckDBStore
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
from polytrading.venues.public import AdapterBatch, AdapterWarning, PublicVenueAdapter
from polytrading.venues.recorder import PublicRecorder
from polytrading.venues.synchronized import SynchronizedBookCollector

_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})


class RetryingTransport(httpx.AsyncBaseTransport):
    """Retry a bounded set of public HTTP statuses with deterministic backoff."""

    def __init__(
        self,
        transport: httpx.AsyncBaseTransport,
        *,
        max_attempts: int = 3,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int):
            raise TypeError("max_attempts must be an integer")
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        self._transport = transport
        self._max_attempts = max_attempts
        self._sleep = sleep

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        for attempt in range(1, self._max_attempts + 1):
            response = await self._transport.handle_async_request(request)
            if response.status_code not in _RETRYABLE_STATUSES or attempt == self._max_attempts:
                return response
            await response.aclose()
            await self._sleep(0.25 * 2 ** (attempt - 1))
        raise AssertionError("positive retry budget must return a response")

    async def aclose(self) -> None:
        await self._transport.aclose()


class CliUsageError(ValueError):
    """A user-facing command validation error."""


class CorpusCollectionError(RuntimeError):
    """A public corpus source or integrity failure."""


class FundingCycleCollectionError(RuntimeError):
    """A point-in-time funding cycle could not be durably recorded."""


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CliUsageError(message)


def build_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(prog="polytrading", description="Read-only research tools")
    commands = parser.add_subparsers(dest="command", required=True)

    replay = commands.add_parser("replay", help="replay public adapter batches")
    replay.add_argument("--input", required=True, type=Path)
    replay.add_argument("--db", required=True, type=Path)

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

    funding = commands.add_parser("funding", help="prospective funding evidence operations")
    funding_commands = funding.add_subparsers(dest="funding_command", required=True)
    health = funding_commands.add_parser("health", help="audit hourly funding collection health")
    health.add_argument("--db", required=True, type=Path)
    health.add_argument("--hours", type=int, default=24)
    health.add_argument("--as-of")
    health.add_argument("--format", choices=("text", "json"), default="text")

    collect = commands.add_parser("collect", help="collect public market evidence")
    collect_commands = collect.add_subparsers(dest="collect_command", required=True)
    public = collect_commands.add_parser("public", help="collect public instruments and funding")
    public.add_argument("--venue", choices=("hyperliquid", "bybit", "dydx", "all"), required=True)
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
    books.add_argument("--venue", choices=("hyperliquid", "bybit", "dydx", "all"), required=True)
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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        arguments = build_parser().parse_args(argv)
        if arguments.command == "replay":
            return _replay(arguments)
        if arguments.command == "carry":
            return (
                _carry_audit(arguments)
                if arguments.carry_command == "audit"
                else _carry_study(arguments)
            )
        if arguments.command == "funding":
            return _funding_health(arguments)
        if arguments.command == "ai":
            return run_ai_command(arguments)
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
    except (CliUsageError, ValueError, OSError) as error:
        print(f"polytrading: error: {error}", file=sys.stderr)
        return 2
    except Exception as error:
        print(f"polytrading: collection failed: {error}", file=sys.stderr)
        return 1


def _replay(arguments: argparse.Namespace) -> int:
    store = DuckDBStore(arguments.db)
    try:
        count = replay_file(arguments.input, store)
    finally:
        store.close()
    print(f"replayed {count} public adapter batches")
    return 0


def _carry_audit(arguments: argparse.Namespace) -> int:
    as_of = _parse_timestamp(arguments.as_of)
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


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return normalize_utc_timestamp(parsed)
    except ValueError as error:
        raise CliUsageError(f"invalid timestamp {value!r}") from error


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
        return (Venue.BYBIT, Venue.HYPERLIQUID, Venue.DYDX)
    return (Venue(value),)


def make_public_http_client(
    *, transport: httpx.AsyncBaseTransport | None = None
) -> httpx.AsyncClient:
    base_transport = transport or httpx.AsyncHTTPTransport()
    return httpx.AsyncClient(
        headers={
            "Accept-Encoding": "identity",
            "User-Agent": "polytrading/0.1 public-market-research",
        },
        timeout=httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=30.0),
        transport=RetryingTransport(base_transport),
    )


@asynccontextmanager
async def public_adapter_session(
    store: DuckDBStore, venues: Iterable[Venue]
) -> AsyncIterator[tuple[PublicVenueAdapter, ...]]:
    clients: list[httpx.AsyncClient] = []
    adapters: list[PublicVenueAdapter] = []
    try:
        for venue in venues:
            client = make_public_http_client()
            clients.append(client)
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
            else:
                raise ValueError(f"unsupported public venue: {venue.value}")
        yield tuple(adapters)
    finally:
        await asyncio.gather(*(client.aclose() for client in clients))


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
    store = DuckDBStore(arguments.db)
    try:
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
    finally:
        store.close()
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

    store = DuckDBStore(arguments.db)
    try:
        try:
            if is_late:
                cycle = record_late_funding_cycle(store, assets, cycle_end, now)
            else:
                async with public_adapter_session(
                    store, (Venue.BYBIT, Venue.HYPERLIQUID)
                ) as adapters:
                    cycle = await PointInTimeFundingCollector(store, clock=_utc_now).collect_once(
                        adapters, assets, cycle_end
                    )
        except ConflictingRecordError as error:
            raise FundingCycleCollectionError(str(error)) from error
    finally:
        store.close()

    renderer = (
        render_funding_cycle_json if arguments.format == "json" else render_funding_cycle_text
    )
    print(renderer(cycle))
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
        source="polymarket",
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
    store = DuckDBStore(arguments.db)
    try:
        async with public_adapter_session(store, venues) as adapters:
            await collect_book_cycles(
                adapters,
                assets,
                store,
                duration_seconds=duration,
                interval_seconds=arguments.interval_seconds,
                wall_clock=_utc_now,
            )
    finally:
        store.close()
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
