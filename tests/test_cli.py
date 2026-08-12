from __future__ import annotations

import ast
import asyncio
import json
from collections.abc import Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from uuid import UUID

import duckdb
import httpx
import pytest

import polytrading.cli as cli
from polytrading.cli import RetryingTransport, collect_book_cycles, main
from polytrading.domain.models import (
    Asset,
    BookLevel,
    Level2BookSnapshot,
    MarketSnapshot,
    RawEnvelope,
    Venue,
)
from polytrading.replay import replay_file
from polytrading.storage.store import DuckDBStore
from polytrading.venues.public import AdapterBatch
from tests.domain.factories import funding_observation, instrument_spec

FIXTURE = Path("tests/fixtures/replay/public_snapshot.jsonl")
NOW = datetime(2026, 8, 12, 12, tzinfo=UTC)


def test_replay_and_audit_are_deterministic_and_preserve_lineage(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "replay.duckdb"

    assert main(["replay", "--input", str(FIXTURE), "--db", str(database)]) == 0
    capsys.readouterr()
    audit_args = [
        "carry",
        "audit",
        "--db",
        str(database),
        "--as-of",
        "2026-08-12T12:00:00Z",
        "--format",
        "json",
    ]
    assert main(audit_args) == 0
    first = capsys.readouterr().out
    assert main(audit_args) == 0
    second = capsys.readouterr().out

    assert first == second
    report = json.loads(first)
    assert [row["asset"] for row in report["assets"]] == ["BTC", "ETH", "SOL"]
    assert all(row["status"] == "INELIGIBLE" for row in report["assets"])
    with duckdb.connect(str(database), read_only=True) as connection:
        raw_hashes = {
            row[0] for row in connection.execute("SELECT source_hash FROM raw_envelopes").fetchall()
        }
        normalized_hashes = {
            row[0]
            for table in ("instrument_specs", "funding_observations")
            for row in connection.execute(f"SELECT source_hash FROM {table}").fetchall()
        }
        assert connection.execute("SELECT count(*) FROM raw_envelopes").fetchone() == (8,)
        assert connection.execute("SELECT count(*) FROM instrument_specs").fetchone() == (6,)
        assert connection.execute("SELECT count(*) FROM funding_observations").fetchone() == (6,)
    assert normalized_hashes <= raw_hashes


def test_replay_aborts_the_entire_file_on_a_malformed_later_row(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    input_path = tmp_path / "malformed.jsonl"
    first_line = FIXTURE.read_text().splitlines()[0]
    input_path.write_text(f"{first_line}\n{{not-json}}\n")
    database = tmp_path / "replay.duckdb"

    assert main(["replay", "--input", str(input_path), "--db", str(database)]) == 2

    assert "line 2" in capsys.readouterr().err
    with duckdb.connect(str(database), read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM raw_envelopes").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM instrument_specs").fetchone() == (0,)


def test_replay_rejects_normalized_lineage_outside_its_raw_batch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    row = json.loads(FIXTURE.read_text().splitlines()[0])
    row["normalized"][0]["source_hash"] = "f" * 64
    input_path = tmp_path / "bad-lineage.jsonl"
    input_path.write_text(json.dumps(row) + "\n")

    assert main(["replay", "--input", str(input_path), "--db", str(tmp_path / "db.duckdb")]) == 2
    assert "lineage" in capsys.readouterr().err.lower()


@pytest.mark.parametrize("corruption", ["payload", "venue"])
def test_replay_rejects_corrupt_raw_hash_and_cross_venue_lineage(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], corruption: str
) -> None:
    row = json.loads(FIXTURE.read_text().splitlines()[0])
    if corruption == "payload":
        row["raw"][0]["payload_json"] += " "
    else:
        row["raw"][0]["venue"] = "hyperliquid"
    input_path = tmp_path / f"bad-{corruption}.jsonl"
    input_path.write_text(json.dumps(row) + "\n")

    assert main(["replay", "--input", str(input_path), "--db", str(tmp_path / "db.duckdb")]) == 2
    message = capsys.readouterr().err.lower()
    assert "hash" in message or "lineage" in message


def test_cli_validation_errors_exit_two_without_tracebacks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        main(
            [
                "carry",
                "audit",
                "--db",
                str(tmp_path / "audit.duckdb"),
                "--as-of",
                "invalid",
            ]
        )
        == 2
    )
    assert "traceback" not in capsys.readouterr().err.lower()

    assert (
        main(
            [
                "collect",
                "public",
                "--venue",
                "all",
                "--start",
                "2026-08-01T12:00:00Z",
                "--end",
                "2026-08-12T12:00:00Z",
                "--db",
                str(tmp_path / "too-much.duckdb"),
            ]
        )
        == 2
    )
    assert "seven days" in capsys.readouterr().err.lower()

    assert (
        main(
            [
                "collect",
                "public",
                "--venue",
                "all",
                "--start",
                "2026-08-12T12:00:01Z",
                "--end",
                "2026-08-12T12:00:00Z",
                "--db",
                str(tmp_path / "reversed.duckdb"),
            ]
        )
        == 2
    )
    assert "must not follow" in capsys.readouterr().err.lower()

    assert (
        main(
            [
                "collect",
                "books",
                "--venue",
                "all",
                "--assets",
                "DOGE",
                "--once",
                "--db",
                str(tmp_path / "bad-asset.duckdb"),
            ]
        )
        == 2
    )
    assert "traceback" not in capsys.readouterr().err.lower()

    for field, value in (("--interval-seconds", "nan"), ("--duration-seconds", "inf")):
        args = [
            "collect",
            "books",
            "--venue",
            "all",
            "--db",
            str(tmp_path / f"nonfinite-{field[2:]}.duckdb"),
        ]
        if field == "--interval-seconds":
            args.extend(("--once", field, value))
        else:
            args.extend((field, value))
        assert main(args) == 2
        assert "finite positive" in capsys.readouterr().err.lower()
    assert (
        main(
            [
                "collect",
                "public",
                "--venue",
                "unknown",
                "--db",
                str(tmp_path / "public.duckdb"),
            ]
        )
        == 2
    )
    assert "traceback" not in capsys.readouterr().err.lower()


class _ReplayOrderStore:
    def __init__(self) -> None:
        self.calls: list[str] = []

    @contextmanager
    def transaction(self) -> Iterator[_ReplayOrderStore]:
        self.calls.append("begin")
        yield self
        self.calls.append("commit")

    def append_raw(self, record: object) -> bool:
        self.calls.append("raw")
        return True

    def append_instrument(self, record: object) -> bool:
        self.calls.append("instrument")
        return True

    def append_funding(self, record: object) -> bool:
        self.calls.append("funding")
        return True


def test_replay_is_batch_raw_first_and_exact_retries_are_idempotent(tmp_path: Path) -> None:
    one_batch = tmp_path / "one.jsonl"
    one_batch.write_text(FIXTURE.read_text().splitlines()[0] + "\n")
    spy = _ReplayOrderStore()

    assert replay_file(one_batch, spy) == 1  # type: ignore[arg-type]
    assert spy.calls == ["begin", "raw", "instrument", "instrument", "instrument", "commit"]

    database = tmp_path / "retry.duckdb"
    store = DuckDBStore(database)
    assert replay_file(FIXTURE, store) == 8
    assert replay_file(FIXTURE, store) == 8
    store.close()
    with duckdb.connect(str(database), read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM raw_envelopes").fetchone() == (8,)
        assert connection.execute("SELECT count(*) FROM instrument_specs").fetchone() == (6,)
        assert connection.execute("SELECT count(*) FROM funding_observations").fetchone() == (6,)


class _SequenceTransport(httpx.AsyncBaseTransport):
    def __init__(self, statuses: list[int], body: bytes = b"{}") -> None:
        self.statuses = iter(statuses)
        self.body = body
        self.calls = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        return httpx.Response(next(self.statuses), content=self.body, request=request)


def test_http_retry_policy_is_bounded_and_never_retries_parse_errors() -> None:
    async def exercise() -> None:
        delays: list[float] = []
        sequence = _SequenceTransport([429, 503, 200])
        transport = RetryingTransport(
            sequence, max_attempts=3, sleep=lambda delay: _record_delay(delays, delay)
        )
        async with httpx.AsyncClient(transport=transport) as client:
            response = await client.get("https://example.test/public")
        assert response.status_code == 200
        assert sequence.calls == 3
        assert delays == [0.25, 0.5]

        malformed = _SequenceTransport([200, 200], body=b"not-json")
        async with httpx.AsyncClient(
            transport=RetryingTransport(malformed, max_attempts=3)
        ) as client:
            response = await client.get("https://example.test/public")
            with pytest.raises(json.JSONDecodeError):
                response.json()
        assert malformed.calls == 1

        non_transient = _SequenceTransport([404, 200])
        async with httpx.AsyncClient(
            transport=RetryingTransport(non_transient, max_attempts=3)
        ) as client:
            response = await client.get("https://example.test/public")
        assert response.status_code == 404
        assert non_transient.calls == 1

        exhausted_delays: list[float] = []
        exhausted = _SequenceTransport([429, 500, 503, 200])
        async with httpx.AsyncClient(
            transport=RetryingTransport(
                exhausted,
                max_attempts=3,
                sleep=lambda delay: _record_delay(exhausted_delays, delay),
            )
        ) as client:
            response = await client.get("https://example.test/public")
        assert response.status_code == 503
        assert exhausted.calls == 3
        assert exhausted_delays == [0.25, 0.5]

    asyncio.run(exercise())


def test_public_http_client_has_explicit_identity_and_timeouts() -> None:
    client = cli.make_public_http_client(transport=_SequenceTransport([200]))

    assert client.headers["user-agent"] == "polytrading/0.1 public-market-research"
    assert client.timeout.connect == 10
    assert client.timeout.read == 30
    asyncio.run(client.aclose())


async def _record_delay(delays: list[float], delay: float) -> None:
    delays.append(delay)


class _BookAdapter:
    def __init__(self, venue: Venue, starts: list[Venue], *, fail_first: bool = False) -> None:
        self.venue = venue
        self.starts = starts
        self.calls = 0
        self.fail_first = fail_first

    async def fetch_order_books(
        self, assets: frozenset[Asset], observed_at: datetime, cycle_id: UUID
    ) -> AdapterBatch:
        self.calls += 1
        self.starts.append(self.venue)
        await asyncio.sleep(0)
        if self.fail_first and self.calls == 1:
            raise TimeoutError("first cycle failed")
        source_hash = ("b" if self.venue is Venue.BYBIT else "c") * 64
        raw = RawEnvelope(
            schema_version=1,
            event_id=UUID(int=self.calls * 10 + (1 if self.venue is Venue.BYBIT else 2)),
            venue=self.venue,
            endpoint="/public/book",
            venue_timestamp=observed_at,
            observed_at=observed_at,
            received_monotonic_ns=self.calls,
            request_latency_ms=Decimal("1"),
            source_version="test-public-v1",
            payload_json="{}",
            source_hash=source_hash,
        )
        books = tuple(
            _book(self.venue, asset, cycle_id, observed_at, source_hash) for asset in assets
        )
        return AdapterBatch(raw=(raw,), normalized=books)


def _book(
    venue: Venue, asset: Asset, cycle_id: UUID, observed_at: datetime, source_hash: str
) -> Level2BookSnapshot:
    base = {
        Asset.BTC: Decimal("65000"),
        Asset.ETH: Decimal("3500"),
        Asset.SOL: Decimal("150"),
    }[asset]
    return Level2BookSnapshot(
        schema_version=1,
        cycle_id=cycle_id,
        venue=venue,
        symbol=f"{asset.value}{'USDT' if venue is Venue.BYBIT else ''}",
        asset=asset,
        bids=(BookLevel(price=base - 1, quantity=Decimal("1"), order_count=1),),
        asks=(BookLevel(price=base + 1, quantity=Decimal("1"), order_count=1),),
        depth_limit=20,
        sequence="1" if venue is Venue.BYBIT else None,
        effective_at=observed_at,
        observed_at=observed_at,
        source_hash=source_hash,
    )


def test_book_loop_runs_both_venues_concurrently_and_continues_after_failure(
    tmp_path: Path,
) -> None:
    starts: list[Venue] = []
    bybit = _BookAdapter(Venue.BYBIT, starts)
    hyperliquid = _BookAdapter(Venue.HYPERLIQUID, starts, fail_first=True)
    times = iter([0.0, 0.0, 0.5, 0.5, 1.1])
    wall_times = iter(
        [
            NOW,
            NOW,
            NOW + timedelta(milliseconds=10),
            NOW + timedelta(seconds=1),
            NOW + timedelta(seconds=1),
            NOW + timedelta(seconds=1, milliseconds=10),
        ]
    )
    delays: list[float] = []
    store = DuckDBStore(tmp_path / "books.duckdb")

    async def exercise() -> None:
        await collect_book_cycles(
            (bybit, hyperliquid),
            frozenset({Asset.BTC, Asset.ETH, Asset.SOL}),
            store,
            duration_seconds=1,
            interval_seconds=0.5,
            monotonic=lambda: next(times),
            wall_clock=lambda: next(wall_times),
            sleep=lambda delay: _record_delay(delays, delay),
        )

    asyncio.run(exercise())
    assert starts == [Venue.BYBIT, Venue.HYPERLIQUID, Venue.BYBIT, Venue.HYPERLIQUID]
    assert bybit.calls == hyperliquid.calls == 2
    assert delays == [0.5]
    store.close()
    with duckdb.connect(str(tmp_path / "books.duckdb"), read_only=True) as connection:
        cycles = connection.execute(
            "SELECT cycle_id, status FROM book_collection_cycles ORDER BY request_completed_at"
        ).fetchall()
        assert [row[1] for row in cycles] == ["failed", "complete"]
        assert connection.execute(
            "SELECT count(*) FROM book_snapshots WHERE cycle_id = ?", [cycles[0][0]]
        ).fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM book_snapshots").fetchone() == (6,)


class _PublicAdapter:
    def __init__(self, venue: Venue, calls: list[tuple[object, ...]]) -> None:
        self.venue = venue
        self.calls = calls

    async def fetch_instruments(
        self, assets: frozenset[Asset], observed_at: datetime
    ) -> AdapterBatch:
        self.calls.append((self.venue, "instruments", assets, observed_at))
        return AdapterBatch(raw=(), normalized=())

    async def fetch_market_snapshots(
        self, assets: frozenset[Asset], observed_at: datetime
    ) -> AdapterBatch:
        self.calls.append((self.venue, "markets", assets, observed_at))
        return AdapterBatch(raw=(), normalized=())

    async def fetch_funding_history(
        self, asset: Asset, start: datetime, end: datetime, observed_at: datetime
    ) -> AdapterBatch:
        self.calls.append((self.venue, "funding", asset, start, end, observed_at))
        return AdapterBatch(raw=(), normalized=())


class _PersistingPublicAdapter(_PublicAdapter):
    def _raw(self, suffix: int) -> RawEnvelope:
        payload = f'{{"venue":"{self.venue.value}","suffix":{suffix}}}'
        return RawEnvelope(
            schema_version=1,
            event_id=UUID(int=(1000 if self.venue is Venue.BYBIT else 2000) + suffix),
            venue=self.venue,
            endpoint="/public/test",
            venue_timestamp=NOW,
            observed_at=NOW,
            received_monotonic_ns=suffix,
            request_latency_ms=Decimal("1"),
            source_version="test-public-v1",
            payload_json=payload,
            source_hash=sha256(payload.encode()).hexdigest(),
        )

    async def fetch_instruments(
        self, assets: frozenset[Asset], observed_at: datetime
    ) -> AdapterBatch:
        self.calls.append((self.venue, "instruments", assets, observed_at))
        raw = self._raw(1)
        symbol = "BTCUSDT" if self.venue is Venue.BYBIT else "BTC"
        return AdapterBatch(
            raw=(raw,),
            normalized=(
                instrument_spec(
                    venue=self.venue,
                    symbol=symbol,
                    instrument_id=f"{self.venue.value}:{symbol}",
                    observed_at=NOW,
                    source_hash=raw.source_hash,
                ),
            ),
        )

    async def fetch_market_snapshots(
        self, assets: frozenset[Asset], observed_at: datetime
    ) -> AdapterBatch:
        self.calls.append((self.venue, "markets", assets, observed_at))
        raw = self._raw(2)
        symbol = "BTCUSDT" if self.venue is Venue.BYBIT else "BTC"
        return AdapterBatch(
            raw=(raw,),
            normalized=(
                MarketSnapshot(
                    schema_version=1,
                    venue=self.venue,
                    symbol=symbol,
                    asset=Asset.BTC,
                    bid=Decimal("99"),
                    ask=Decimal("101"),
                    mark=Decimal("100"),
                    index=Decimal("100"),
                    open_interest=Decimal("10"),
                    effective_at=NOW,
                    observed_at=NOW,
                    source_hash=raw.source_hash,
                ),
            ),
        )

    async def fetch_funding_history(
        self, asset: Asset, start: datetime, end: datetime, observed_at: datetime
    ) -> AdapterBatch:
        self.calls.append((self.venue, "funding", asset, start, end, observed_at))
        raw = self._raw(3)
        symbol = "BTCUSDT" if self.venue is Venue.BYBIT else "BTC"
        return AdapterBatch(
            raw=(raw,),
            normalized=(
                funding_observation(
                    venue=self.venue,
                    symbol=symbol,
                    effective_at=end,
                    observed_at=NOW,
                    source_hash=raw.source_hash,
                ),
            ),
        )


def test_collect_public_cli_uses_both_public_adapters_and_seven_day_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[object, ...]] = []
    adapters = (
        _PublicAdapter(Venue.BYBIT, calls),
        _PublicAdapter(Venue.HYPERLIQUID, calls),
    )

    @asynccontextmanager
    async def session(store: object, venues: object):
        assert set(venues) == {Venue.BYBIT, Venue.HYPERLIQUID}
        yield adapters

    monkeypatch.setattr(cli, "public_adapter_session", session)
    monkeypatch.setattr(cli, "_utc_now", lambda: NOW)
    monkeypatch.setattr(cli, "_has_bybit_history_basis", lambda store, asset, start: True)

    assert (
        main(
            [
                "collect",
                "public",
                "--venue",
                "all",
                "--assets",
                "BTC,ETH,SOL",
                "--db",
                str(tmp_path / "public.duckdb"),
            ]
        )
        == 0
    )
    assert [(call[0], call[1]) for call in calls[:2]] == [
        (Venue.BYBIT, "instruments"),
        (Venue.HYPERLIQUID, "instruments"),
    ]
    funding_calls = [call for call in calls if call[1] == "funding"]
    assert len(funding_calls) == 6
    assert all(call[3] == NOW - timedelta(days=7) and call[4] == NOW for call in funding_calls)


def test_collect_public_skips_bybit_history_without_point_in_time_instrument_basis(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[tuple[object, ...]] = []
    adapters = (
        _PersistingPublicAdapter(Venue.BYBIT, calls),
        _PersistingPublicAdapter(Venue.HYPERLIQUID, calls),
    )

    @asynccontextmanager
    async def session(store: object, venues: object):
        yield adapters

    monkeypatch.setattr(cli, "public_adapter_session", session)
    monkeypatch.setattr(cli, "_utc_now", lambda: NOW)

    assert (
        main(
            [
                "collect",
                "public",
                "--venue",
                "all",
                "--assets",
                "BTC",
                "--db",
                str(tmp_path / "fresh.duckdb"),
            ]
        )
        == 0
    )
    assert [(call[0], call[1]) for call in calls] == [
        (Venue.BYBIT, "instruments"),
        (Venue.HYPERLIQUID, "instruments"),
        (Venue.BYBIT, "markets"),
        (Venue.HYPERLIQUID, "markets"),
        (Venue.HYPERLIQUID, "funding"),
    ]
    message = capsys.readouterr().err.lower()
    assert "bybit btc funding was not collected" in message
    assert "2026-08-05t12:00:00+00:00..2026-08-12t12:00:00+00:00" in message
    assert "no bybit instrument specification was known at the range start" in message
    with duckdb.connect(str(tmp_path / "fresh.duckdb"), read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM instrument_specs").fetchone() == (2,)
        assert connection.execute("SELECT count(*) FROM market_snapshots").fetchone() == (2,)
        assert connection.execute("SELECT venue FROM funding_observations").fetchall() == [
            (Venue.HYPERLIQUID.value,)
        ]


def test_preseeded_historical_bybit_instrument_enables_funding_collection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "seeded.duckdb"
    start = NOW - timedelta(days=7)
    store = DuckDBStore(database)
    store.append_instrument(
        instrument_spec(
            venue=Venue.BYBIT,
            symbol="BTCUSDT",
            instrument_id="bybit:BTCUSDT",
            observed_at=start,
        )
    )
    store.close()
    calls: list[tuple[object, ...]] = []
    adapters = (_PersistingPublicAdapter(Venue.BYBIT, calls),)

    @asynccontextmanager
    async def session(store: object, venues: object):
        yield adapters

    monkeypatch.setattr(cli, "public_adapter_session", session)
    monkeypatch.setattr(cli, "_utc_now", lambda: NOW)

    assert (
        main(
            [
                "collect",
                "public",
                "--venue",
                "bybit",
                "--assets",
                "BTC",
                "--start",
                start.isoformat(),
                "--end",
                NOW.isoformat(),
                "--db",
                str(database),
            ]
        )
        == 0
    )
    assert [call[1] for call in calls] == ["instruments", "markets", "funding"]
    with duckdb.connect(str(database), read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM funding_observations").fetchone() == (1,)


def test_bybit_history_basis_requires_an_instrument_known_at_range_start(
    tmp_path: Path,
) -> None:
    store = DuckDBStore(tmp_path / "basis.duckdb")
    start = NOW - timedelta(days=7)
    assert not cli._has_bybit_history_basis(store, Asset.BTC, start)
    store.append_instrument(
        instrument_spec(
            venue=Venue.BYBIT,
            symbol="BTCUSDT",
            instrument_id="bybit:BTCUSDT",
            observed_at=start,
        )
    )
    assert cli._has_bybit_history_basis(store, Asset.BTC, start)
    assert not cli._has_bybit_history_basis(store, Asset.ETH, start)
    store.close()


def test_collect_books_once_cli_launches_both_venues_in_one_cycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    starts: list[Venue] = []
    adapters = (
        _BookAdapter(Venue.BYBIT, starts),
        _BookAdapter(Venue.HYPERLIQUID, starts),
    )

    @asynccontextmanager
    async def session(store: object, venues: object):
        assert set(venues) == {Venue.BYBIT, Venue.HYPERLIQUID}
        yield adapters

    monkeypatch.setattr(cli, "public_adapter_session", session)
    monkeypatch.setattr(cli, "_utc_now", lambda: NOW)
    database = tmp_path / "books-once.duckdb"

    assert (
        main(
            [
                "collect",
                "books",
                "--venue",
                "all",
                "--assets",
                "BTC,ETH,SOL",
                "--once",
                "--db",
                str(database),
            ]
        )
        == 0
    )
    assert starts == [Venue.BYBIT, Venue.HYPERLIQUID]
    with duckdb.connect(str(database), read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM book_collection_cycles").fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM book_snapshots").fetchone() == (6,)


def test_venue_modules_define_no_private_or_trading_method_names() -> None:
    prohibited = {
        "place_order",
        "cancel_order",
        "withdraw",
        "transfer",
        "authenticate",
        "sign",
    }
    definitions: set[str] = set()
    for path in Path("src/polytrading/venues").rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        definitions.update(
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        )

    assert definitions.isdisjoint(prohibited)
