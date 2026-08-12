from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest

from polytrading.domain.models import Asset, InstrumentKind, InstrumentSpec, Venue
from polytrading.registry import MissingPointInTimeRecordError
from polytrading.registry.instruments import InstrumentRegistry
from polytrading.storage.store import DuckDBStore
from polytrading.venues.bybit import (
    BybitPublicAdapter,
    PaginationStalledError,
    VenueResponseError,
)
from polytrading.venues.public import AdapterWarning

FIXTURES = Path(__file__).parents[1] / "fixtures" / "bybit"
START = datetime(2026, 8, 11, 0, tzinfo=UTC)
MIDDLE = datetime(2026, 8, 11, 8, tzinfo=UTC)
END = datetime(2026, 8, 11, 16, tzinfo=UTC)
REQUEST_CONTEXT = datetime(2026, 8, 12, 10, tzinfo=UTC)
RECEIVED_1 = datetime(2026, 8, 12, 10, 0, 1, tzinfo=UTC)
RECEIVED_2 = datetime(2026, 8, 12, 10, 0, 2, tzinfo=UTC)
CYCLE_ID = UUID("00000000-0000-0000-0000-000000000701")


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


class SequenceClock:
    def __init__(self, values: list[Any]) -> None:
        self._values = iter(values)

    def __call__(self) -> Any:
        return next(self._values)


def make_registry(tmp_path: Path, *specs: InstrumentSpec) -> InstrumentRegistry:
    registry = InstrumentRegistry(DuckDBStore(tmp_path / "research.duckdb"))
    for spec in specs:
        registry.record(spec)
    return registry


def registry_spec(
    *,
    observed_at: datetime,
    interval_hours: Decimal = Decimal(8),
    source_hash: str = "a" * 64,
) -> InstrumentSpec:
    return InstrumentSpec(
        schema_version=1,
        instrument_id="bybit:BTCUSDT",
        venue=Venue.BYBIT,
        symbol="BTCUSDT",
        asset=Asset.BTC,
        kind=InstrumentKind.LINEAR_PERPETUAL,
        contract_multiplier=Decimal(1),
        index_family=None,
        oracle_family=None,
        mark_method=None,
        liquidation_method=None,
        collateral_asset="USDT",
        pnl_asset="USDT",
        funding_formula_id=None,
        funding_cap=Decimal("0.00375"),
        funding_interval_hours=interval_hours,
        funding_payment_offset_minutes=None,
        min_notional=Decimal(5),
        quantity_step=Decimal("0.001"),
        price_tick=Decimal("0.1"),
        is_inverse=False,
        is_prelaunch=False,
        observed_at=observed_at,
        source_hash=source_hash,
    )


def make_adapter(
    handler: Callable[[httpx.Request], httpx.Response],
    registry: InstrumentRegistry,
    *,
    wall_times: list[datetime],
    monotonic_times: list[int] | None = None,
) -> BybitPublicAdapter:
    return BybitPublicAdapter(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        wall_clock=SequenceClock(wall_times),
        monotonic_ns=SequenceClock(monotonic_times or list(range(100, 5000, 100))),
        instrument_registry=registry,
    )


def response(payload: bytes, status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code, content=payload, headers={"content-type": "application/json"}
    )


def request_shape(request: httpx.Request) -> tuple[str, str, dict[str, str]]:
    return request.method, request.url.path, dict(request.url.params)


def changed_fixture(name: str, mutate: Callable[[dict[str, Any]], None]) -> bytes:
    document = json.loads(fixture_bytes(name))
    mutate(document)
    return json.dumps(document).encode()


def test_fetch_instruments_paginates_and_normalizes_only_supported_active_perpetuals(
    tmp_path: Path,
) -> None:
    # Catches wrong cursor requests, accepting inverse/pre-launch/unsupported rows, or guessed
    # compatibility metadata instead of the documented Bybit contract fields.
    pages = iter(
        [fixture_bytes("instruments_page_1.json"), fixture_bytes("instruments_page_2.json")]
    )
    requests: list[tuple[str, str, dict[str, str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request_shape(request))
        return response(next(pages))

    adapter = make_adapter(
        handler,
        make_registry(tmp_path),
        wall_times=[RECEIVED_1, RECEIVED_2],
        monotonic_times=[1_000_000, 2_500_000, 3_000_000, 5_000_000],
    )
    batch = asyncio.run(
        adapter.fetch_instruments(
            frozenset({Asset.BTC, Asset.ETH, Asset.SOL}), REQUEST_CONTEXT
        )
    )

    assert requests == [
        (
            "GET",
            "/v5/market/instruments-info",
            {"category": "linear", "limit": "1000"},
        ),
        (
            "GET",
            "/v5/market/instruments-info",
            {"category": "linear", "limit": "1000", "cursor": "page-2"},
        ),
    ]
    assert [raw.payload_json for raw in batch.raw] == [
        fixture_bytes("instruments_page_1.json").decode(),
        fixture_bytes("instruments_page_2.json").decode(),
    ]
    assert batch.raw[0].source_hash == hashlib.sha256(
        batch.raw[0].payload_json.encode()
    ).hexdigest()
    assert batch.raw[0].observed_at == RECEIVED_1
    assert batch.raw[0].received_monotonic_ns == 2_500_000
    assert batch.raw[0].request_latency_ms == Decimal("1.5")
    assert [item.symbol for item in batch.normalized] == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    assert [item.observed_at for item in batch.normalized] == [
        RECEIVED_1,
        RECEIVED_1,
        RECEIVED_2,
    ]
    btc, eth, sol = batch.normalized
    assert btc.instrument_id == "bybit:BTCUSDT"
    assert btc.kind is InstrumentKind.LINEAR_PERPETUAL
    assert btc.contract_multiplier == Decimal(1)
    assert btc.collateral_asset == "USDT"
    assert btc.pnl_asset == "USDT"
    assert btc.funding_interval_hours == Decimal(8)
    assert btc.funding_cap == Decimal("0.00375")
    assert btc.min_notional == Decimal(5)
    assert btc.quantity_step == Decimal("0.001")
    assert btc.price_tick == Decimal("0.10")
    assert eth.quantity_step == Decimal("0.01")
    assert sol.funding_interval_hours == Decimal(1)
    for item in batch.normalized:
        assert item.is_inverse is False
        assert item.is_prelaunch is False
        assert item.index_family is None
        assert item.oracle_family is None
        assert item.mark_method is None
        assert item.liquidation_method is None
        assert item.funding_formula_id is None
        assert item.funding_payment_offset_minutes is None
        assert item.source_hash in {raw.source_hash for raw in batch.raw}


def test_fetch_instruments_rejects_repeated_cursor(tmp_path: Path) -> None:
    # Catches an infinite cursor loop when Bybit repeats a non-empty cursor.
    first = changed_fixture(
        "instruments_page_1.json",
        lambda document: document["result"].update(nextPageCursor="same"),
    )
    second = changed_fixture(
        "instruments_page_2.json",
        lambda document: document["result"].update(list=[], nextPageCursor="same"),
    )
    pages = iter([first, second])
    adapter = make_adapter(
        lambda request: response(next(pages)),
        make_registry(tmp_path),
        wall_times=[RECEIVED_1, RECEIVED_2],
    )

    with pytest.raises(PaginationStalledError, match="cursor"):
        asyncio.run(adapter.fetch_instruments(frozenset({Asset.BTC}), REQUEST_CONTEXT))


def test_fetch_instruments_keeps_asymmetric_funding_bounds_unknown(tmp_path: Path) -> None:
    # Catches inventing one symmetric cap from asymmetric documented bounds.
    first = changed_fixture(
        "instruments_page_1.json",
        lambda document: document["result"]["list"][0].update(lowerFundingRate="-0.002"),
    )
    pages = iter([first, fixture_bytes("instruments_page_2.json")])
    adapter = make_adapter(
        lambda request: response(next(pages)),
        make_registry(tmp_path),
        wall_times=[RECEIVED_1, RECEIVED_2],
    )

    batch = asyncio.run(adapter.fetch_instruments(frozenset({Asset.BTC}), REQUEST_CONTEXT))

    assert batch.normalized[0].funding_cap is None


def test_fetch_market_snapshots_uses_one_ticker_response_and_exact_symbols(
    tmp_path: Path,
) -> None:
    # Catches per-symbol ticker requests, suffix matching, wrong response lineage, or backdating
    # normalized observations to the caller's collection context.
    payload = fixture_bytes("tickers.json")
    requests: list[tuple[str, str, dict[str, str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request_shape(request))
        return response(payload)

    adapter = make_adapter(
        handler,
        make_registry(tmp_path),
        wall_times=[RECEIVED_1],
        monotonic_times=[50, 150],
    )
    batch = asyncio.run(
        adapter.fetch_market_snapshots(
            frozenset({Asset.BTC, Asset.ETH, Asset.SOL}), REQUEST_CONTEXT
        )
    )

    assert requests == [
        ("GET", "/v5/market/tickers", {"category": "linear"})
    ]
    assert batch.raw[0].payload_json == payload.decode()
    assert batch.raw[0].venue_timestamp == REQUEST_CONTEXT
    assert [item.symbol for item in batch.normalized] == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    btc = batch.normalized[0]
    assert (btc.bid, btc.ask, btc.mark, btc.index, btc.open_interest) == (
        Decimal("118999.90"),
        Decimal("119000.10"),
        Decimal("118998.75"),
        Decimal("118995.25"),
        Decimal("81234.567"),
    )
    assert btc.effective_at == REQUEST_CONTEXT
    assert btc.observed_at == RECEIVED_1
    assert btc.source_hash == batch.raw[0].source_hash
    assert batch.warnings == ()


def test_fetch_market_snapshots_warns_and_omits_missing_ticker(tmp_path: Path) -> None:
    # Catches silent absence, invented zero snapshots, or unstructured logging-only warnings.
    payload = changed_fixture(
        "tickers.json",
        lambda document: document["result"]["list"].pop(1),
    )
    adapter = make_adapter(
        lambda request: response(payload),
        make_registry(tmp_path),
        wall_times=[RECEIVED_1],
    )

    batch = asyncio.run(
        adapter.fetch_market_snapshots(frozenset({Asset.BTC, Asset.ETH}), REQUEST_CONTEXT)
    )

    assert [item.symbol for item in batch.normalized] == ["BTCUSDT"]
    assert batch.warnings == (
        AdapterWarning(
            code="ticker_missing",
            venue=Venue.BYBIT,
            endpoint="/v5/market/tickers",
            symbol="ETHUSDT",
            message="ticker missing for requested symbol",
        ),
    )


@pytest.mark.parametrize(
    "field,value,message",
    [("markPrice", "NaN", "finite"), ("bid1Price", 1, "string")],
)
def test_fetch_market_snapshots_rejects_malformed_required_prices(
    tmp_path: Path, field: str, value: object, message: str
) -> None:
    # Catches malformed required ticker numerics entering normalized evidence.
    payload = changed_fixture(
        "tickers.json",
        lambda document: document["result"]["list"][0].update({field: value}),
    )
    adapter = make_adapter(
        lambda request: response(payload),
        make_registry(tmp_path),
        wall_times=[RECEIVED_1],
    )

    with pytest.raises(ValueError, match=message):
        asyncio.run(adapter.fetch_market_snapshots(frozenset({Asset.BTC}), REQUEST_CONTEXT))


def test_fetch_market_snapshots_rejects_duplicate_symbols(tmp_path: Path) -> None:
    # Catches nondeterministic exact-symbol joins when a ticker symbol repeats.
    def duplicate(document: dict[str, Any]) -> None:
        document["result"]["list"].append(document["result"]["list"][0])

    adapter = make_adapter(
        lambda request: response(changed_fixture("tickers.json", duplicate)),
        make_registry(tmp_path),
        wall_times=[RECEIVED_1],
    )

    with pytest.raises(ValueError, match="duplicate"):
        asyncio.run(adapter.fetch_market_snapshots(frozenset({Asset.BTC}), REQUEST_CONTEXT))


def test_fetch_funding_history_paginates_backward_and_uses_historical_specs(
    tmp_path: Path,
) -> None:
    # Catches forward pagination, overlap duplication, assumed eight-hour intervals, current-spec
    # lookahead, incorrect ordering, or a normalized receipt timestamp from the wrong raw page.
    pages = iter(
        [
            fixture_bytes("funding_history_page_1.json"),
            fixture_bytes("funding_history_page_2.json"),
        ]
    )
    requests: list[tuple[str, str, dict[str, str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request_shape(request))
        return response(next(pages))

    registry = make_registry(
        tmp_path,
        registry_spec(observed_at=datetime(2026, 8, 10, tzinfo=UTC)),
        registry_spec(
            observed_at=datetime(2026, 8, 11, 12, tzinfo=UTC),
            interval_hours=Decimal(4),
            source_hash="b" * 64,
        ),
    )
    adapter = make_adapter(
        handler,
        registry,
        wall_times=[RECEIVED_1, RECEIVED_2],
        monotonic_times=[100, 200, 300, 450],
    )
    batch = asyncio.run(
        adapter.fetch_funding_history(Asset.BTC, START, END, REQUEST_CONTEXT)
    )

    assert requests == [
        (
            "GET",
            "/v5/market/funding/history",
            {
                "category": "linear",
                "symbol": "BTCUSDT",
                "startTime": "1786406400000",
                "endTime": "1786464000000",
                "limit": "200",
            },
        ),
        (
            "GET",
            "/v5/market/funding/history",
            {
                "category": "linear",
                "symbol": "BTCUSDT",
                "startTime": "1786406400000",
                "endTime": "1786435199999",
                "limit": "200",
            },
        ),
    ]
    assert [item.effective_at for item in batch.normalized] == [START, MIDDLE, END]
    assert [item.rate for item in batch.normalized] == [
        Decimal("-0.0000200"),
        Decimal("0.0000125"),
        Decimal("-0.0000030"),
    ]
    assert [item.interval_hours for item in batch.normalized] == [
        Decimal(8),
        Decimal(8),
        Decimal(4),
    ]
    assert [item.observed_at for item in batch.normalized] == [
        RECEIVED_2,
        RECEIVED_1,
        RECEIVED_1,
    ]
    assert [item.source_hash for item in batch.normalized] == [
        batch.raw[1].source_hash,
        batch.raw[0].source_hash,
        batch.raw[0].source_hash,
    ]


def test_fetch_funding_history_fails_without_point_in_time_instrument(
    tmp_path: Path,
) -> None:
    # Catches applying a default/current funding interval when historical evidence is absent.
    adapter = make_adapter(
        lambda request: response(fixture_bytes("funding_history_page_1.json")),
        make_registry(tmp_path),
        wall_times=[RECEIVED_1],
    )

    with pytest.raises(MissingPointInTimeRecordError):
        asyncio.run(adapter.fetch_funding_history(Asset.BTC, START, END, REQUEST_CONTEXT))


@pytest.mark.parametrize(
    "mutate,message",
    [
        (
            lambda document: document["result"]["list"][0].update(symbol="ETHUSDT"),
            "symbol",
        ),
        (
            lambda document: document["result"]["list"][0].update(
                fundingRateTimestamp="1786464000001"
            ),
            "outside requested range",
        ),
        (
            lambda document: document["result"]["list"].append(
                {
                    "symbol": "BTCUSDT",
                    "fundingRate": "0.5",
                    "fundingRateTimestamp": "1786435200000",
                }
            ),
            "conflicting duplicate",
        ),
    ],
)
def test_fetch_funding_history_rejects_invalid_rows(
    tmp_path: Path, mutate: Callable[[dict[str, Any]], None], message: str
) -> None:
    # Catches cross-symbol/range contamination and mutable overwrite of funding identity.
    adapter = make_adapter(
        lambda request: response(changed_fixture("funding_history_page_1.json", mutate)),
        make_registry(
            tmp_path, registry_spec(observed_at=datetime(2026, 8, 10, tzinfo=UTC))
        ),
        wall_times=[RECEIVED_1],
    )

    with pytest.raises(ValueError, match=message):
        asyncio.run(adapter.fetch_funding_history(Asset.BTC, START, END, REQUEST_CONTEXT))


def test_fetch_funding_history_rejects_unchanged_earliest_timestamp(
    tmp_path: Path,
) -> None:
    # Catches an endless backward paginator when Bybit repeats the same earliest timestamp.
    payload = fixture_bytes("funding_history_page_1.json")
    adapter = make_adapter(
        lambda request: response(payload),
        make_registry(
            tmp_path, registry_spec(observed_at=datetime(2026, 8, 10, tzinfo=UTC))
        ),
        wall_times=[RECEIVED_1, RECEIVED_2],
    )

    with pytest.raises(PaginationStalledError, match="progress"):
        asyncio.run(adapter.fetch_funding_history(Asset.BTC, START, END, REQUEST_CONTEXT))


def test_fetch_order_books_preserves_engine_time_and_sequence(tmp_path: Path) -> None:
    # Catches wrong request shape, side reversal, dropped u/seq, exchange-time confusion, or
    # backdating observation time to request context.
    payload = fixture_bytes("orderbook.json")
    requests: list[tuple[str, str, dict[str, str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request_shape(request))
        return response(payload)

    adapter = make_adapter(
        handler,
        make_registry(tmp_path),
        wall_times=[RECEIVED_1],
        monotonic_times=[25, 75],
    )
    batch = asyncio.run(
        adapter.fetch_order_books(frozenset({Asset.BTC}), REQUEST_CONTEXT, CYCLE_ID)
    )

    assert requests == [
        (
            "GET",
            "/v5/market/orderbook",
            {"category": "linear", "symbol": "BTCUSDT", "limit": "20"},
        )
    ]
    book = batch.normalized[0]
    assert book.cycle_id == CYCLE_ID
    assert book.symbol == "BTCUSDT"
    assert book.depth_limit == 20
    assert book.sequence == "u=987654321;seq=456789012"
    assert book.effective_at == datetime(2026, 8, 11, 16, 0, 0, 123000, tzinfo=UTC)
    assert book.observed_at == RECEIVED_1
    assert [level.price for level in book.bids] == [
        Decimal("118999.90"),
        Decimal("118999.80"),
        Decimal("118999.70"),
    ]
    assert [level.price for level in book.asks] == [
        Decimal("119000.10"),
        Decimal("119000.20"),
        Decimal("119000.30"),
    ]
    assert all(level.order_count is None for level in (*book.bids, *book.asks))
    assert batch.raw[0].venue_timestamp == REQUEST_CONTEXT
    assert book.source_hash == batch.raw[0].source_hash


@pytest.mark.parametrize(
    "mutate,message",
    [
        (lambda document: document["result"].pop("cts"), "cts"),
        (
            lambda document: document["result"].update(
                b=[["118999.80", "1"], ["118999.90", "1"]]
            ),
            "descending",
        ),
        (
            lambda document: document["result"].update(
                a=[["119000.20", "1"], ["119000.10", "1"]]
            ),
            "ascending",
        ),
        (
            lambda document: document["result"].update(
                b=[["119000.10", "1"]], a=[["119000.10", "1"]]
            ),
            "cross",
        ),
    ],
)
def test_fetch_order_books_fails_closed_on_malformed_book(
    tmp_path: Path, mutate: Callable[[dict[str, Any]], None], message: str
) -> None:
    # Catches missing matching-engine time or structurally invalid L2 evidence.
    adapter = make_adapter(
        lambda request: response(changed_fixture("orderbook.json", mutate)),
        make_registry(tmp_path),
        wall_times=[RECEIVED_1],
    )

    with pytest.raises(ValueError, match=message):
        asyncio.run(adapter.fetch_order_books(frozenset({Asset.BTC}), REQUEST_CONTEXT, CYCLE_ID))


def test_fetch_order_books_rejects_repeated_sequence_pair_within_cycle(
    tmp_path: Path,
) -> None:
    # Catches treating the same REST sequence evidence as two distinct books in one cycle.
    def handler(request: httpx.Request) -> httpx.Response:
        symbol = request.url.params["symbol"]
        payload = changed_fixture(
            "orderbook.json", lambda document: document["result"].update(s=symbol)
        )
        return response(payload)

    adapter = make_adapter(
        handler,
        make_registry(tmp_path),
        wall_times=[RECEIVED_1, RECEIVED_2],
    )

    with pytest.raises(ValueError, match=r"repeated.*sequence"):
        asyncio.run(
            adapter.fetch_order_books(
                frozenset({Asset.BTC, Asset.ETH}), REQUEST_CONTEXT, CYCLE_ID
            )
        )


def test_nonzero_retcode_error_is_sanitized(tmp_path: Path) -> None:
    # Catches leaking retMsg or full venue payload through adapter exceptions.
    payload = json.dumps(
        {
            "retCode": 10001,
            "retMsg": "secret diagnostic with customer material",
            "result": {},
            "retExtInfo": {"credential": "never expose"},
            "time": 1786528800000,
        }
    ).encode()
    adapter = make_adapter(
        lambda request: response(payload),
        make_registry(tmp_path),
        wall_times=[RECEIVED_1],
    )

    with pytest.raises(VenueResponseError) as captured:
        asyncio.run(adapter.fetch_market_snapshots(frozenset({Asset.BTC}), REQUEST_CONTEXT))

    assert captured.value.endpoint == "/v5/market/tickers"
    assert captured.value.code == 10001
    rendered = str(captured.value)
    assert "/v5/market/tickers" in rendered
    assert "10001" in rendered
    assert "secret" not in rendered
    assert "credential" not in rendered


@pytest.mark.parametrize(
    "document,message",
    [
        ({"retCode": False, "result": {"list": []}, "time": 1786528800000}, "integer"),
        (
            {"retCode": 0, "result": {"category": "linear"}, "time": 1786528800000},
            "list",
        ),
    ],
)
def test_response_envelope_rejects_malformed_required_fields(
    tmp_path: Path, document: object, message: str
) -> None:
    # Catches bool-as-integer coercion and treating absent result.list as an empty success.
    adapter = make_adapter(
        lambda request: response(json.dumps(document).encode()),
        make_registry(tmp_path),
        wall_times=[RECEIVED_1],
    )

    with pytest.raises(ValueError, match=message):
        asyncio.run(adapter.fetch_market_snapshots(frozenset({Asset.BTC}), REQUEST_CONTEXT))


def test_non_2xx_response_fails(tmp_path: Path) -> None:
    # Catches parsing an HTTP failure page as successful venue evidence.
    adapter = make_adapter(
        lambda request: response(b'{"retCode":0,"result":{"list":[]}}', 503),
        make_registry(tmp_path),
        wall_times=[RECEIVED_1],
    )

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(adapter.fetch_market_snapshots(frozenset({Asset.BTC}), REQUEST_CONTEXT))


def test_collection_context_must_be_timezone_aware_before_request(tmp_path: Path) -> None:
    # Catches accepting an ambiguous caller context even though receipt time comes later.
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return response(fixture_bytes("tickers.json"))

    adapter = make_adapter(handler, make_registry(tmp_path), wall_times=[RECEIVED_1])

    with pytest.raises(ValueError, match="timezone-aware"):
        asyncio.run(
            adapter.fetch_market_snapshots(
                frozenset({Asset.BTC}), datetime(2026, 8, 12, 10)
            )
        )
    assert calls == 0
