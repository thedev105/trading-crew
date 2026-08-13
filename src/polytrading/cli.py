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

import httpx

from polytrading.ai.cli import AIInputError, add_ai_subcommands, run_ai_command
from polytrading.carry.audit import CarryAuditor
from polytrading.carry.report import render_json, render_text
from polytrading.corpus_intake.artifacts import CorpusRunWriter, verify_run
from polytrading.corpus_intake.models import AcquisitionRequest, CorpusIntakeError
from polytrading.corpus_intake.polymarket import acquire_polymarket
from polytrading.domain.models import Asset, Venue, normalize_utc_timestamp
from polytrading.registry.instruments import InstrumentRegistry
from polytrading.replay import replay_file
from polytrading.storage.store import DuckDBStore
from polytrading.venues.bybit import BybitPublicAdapter
from polytrading.venues.hyperliquid import HyperliquidPublicAdapter
from polytrading.venues.public import PublicVenueAdapter
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

    collect = commands.add_parser("collect", help="collect public market evidence")
    collect_commands = collect.add_subparsers(dest="collect_command", required=True)
    public = collect_commands.add_parser("public", help="collect public instruments and funding")
    public.add_argument("--venue", choices=("hyperliquid", "bybit", "all"), required=True)
    public.add_argument("--assets", default="BTC,ETH,SOL")
    public.add_argument("--start")
    public.add_argument("--end")
    public.add_argument("--db", required=True, type=Path)

    books = collect_commands.add_parser("books", help="collect synchronized public books")
    books.add_argument("--venue", choices=("hyperliquid", "bybit", "all"), required=True)
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
    corpus.add_argument("--page-size", type=int, default=100)
    corpus.add_argument("--max-pages", type=int, default=10)
    corpus.add_argument("--max-response-bytes", type=int, default=16 * 1024 * 1024)
    corpus.add_argument("--request-delay-seconds", type=float, default=0.05)
    add_ai_subcommands(commands)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        arguments = build_parser().parse_args(argv)
        if arguments.command == "replay":
            return _replay(arguments)
        if arguments.command == "carry":
            return _carry_audit(arguments)
        if arguments.command == "ai":
            return run_ai_command(arguments)
        if arguments.collect_command == "public":
            return asyncio.run(_collect_public(arguments))
        if arguments.collect_command == "corpus":
            return asyncio.run(_collect_corpus(arguments))
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
        return (Venue.BYBIT, Venue.HYPERLIQUID)
    return (Venue(value),)


def make_public_http_client(
    *, transport: httpx.AsyncBaseTransport | None = None
) -> httpx.AsyncClient:
    base_transport = transport or httpx.AsyncHTTPTransport()
    return httpx.AsyncClient(
        headers={"User-Agent": "polytrading/0.1 public-market-research"},
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
            else:
                adapters.append(HyperliquidPublicAdapter(client, _utc_now, time.monotonic_ns))
        yield tuple(adapters)
    finally:
        await asyncio.gather(*(client.aclose() for client in clients))


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
                recorder.record(await adapter.fetch_instruments(assets, observed_at))
            for adapter in adapters:
                recorder.record(await adapter.fetch_market_snapshots(assets, observed_at))
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
                    recorder.record(
                        await adapter.fetch_funding_history(asset, start, end, observed_at)
                    )
    finally:
        store.close()
    print(
        f"completed public collection for {len(assets)} assets across {len(venues)} venues; "
        "see warnings for skipped evidence"
    )
    return 0


async def _collect_corpus(arguments: argparse.Namespace) -> int:
    request = AcquisitionRequest(
        retrieved_at=_parse_timestamp(arguments.retrieved_at),
        information_cutoff=_parse_timestamp(arguments.information_cutoff),
        max_candidates=arguments.max_candidates,
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
    collector = SynchronizedBookCollector(store, clock=wall_clock)
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
