from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from polytrading.domain.models import Asset, InstrumentKind, Venue
from polytrading.venues.dydx import DydxPublicAdapter, PaginationStalledError
from polytrading.venues.public import AdapterWarning

FIXTURES = Path(__file__).parents[1] / "fixtures" / "dydx"
REQUEST_CONTEXT = datetime(2026, 8, 13, 11, 59, tzinfo=UTC)
RECEIVED_1 = datetime(2026, 8, 13, 12, 0, 1, tzinfo=UTC)
RECEIVED_2 = datetime(2026, 8, 13, 12, 0, 2, tzinfo=UTC)
FUNDING_START = datetime(2026, 8, 13, 10, tzinfo=UTC)
FUNDING_END = datetime(2026, 8, 13, 12, tzinfo=UTC)


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


class SequenceClock:
    def __init__(self, values: list[Any]) -> None:
        self._values = iter(values)

    def __call__(self) -> Any:
        return next(self._values)


def make_adapter(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    wall_times: list[datetime],
    monotonic_times: list[int] | None = None,
    max_funding_pages: int = 10_000,
) -> DydxPublicAdapter:
    return DydxPublicAdapter(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        wall_clock=SequenceClock(wall_times),
        monotonic_ns=SequenceClock(monotonic_times or list(range(100, 10_000, 100))),
        max_funding_pages=max_funding_pages,
    )


def response(payload: bytes) -> httpx.Response:
    return httpx.Response(200, content=payload, headers={"content-type": "application/json"})


def test_fetch_instruments_preserves_exact_raw_and_normalizes_requested_active_markets() -> None:
    # Catches wrong endpoint selection, backdated receipt time, float parsing, or guessed semantics.
    payload = fixture_bytes("perpetual_markets.json")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return response(payload)

    adapter = make_adapter(
        handler,
        wall_times=[RECEIVED_1],
        monotonic_times=[1_000_000, 2_500_000],
    )
    batch = asyncio.run(
        adapter.fetch_instruments(frozenset({Asset.SOL, Asset.BTC, Asset.ETH}), REQUEST_CONTEXT)
    )

    assert [(request.method, str(request.url)) for request in requests] == [
        ("GET", "https://indexer.dydx.trade/v4/perpetualMarkets")
    ]
    assert len(batch.raw) == 1
    raw = batch.raw[0]
    assert raw.venue is Venue.DYDX
    assert raw.endpoint == "/v4/perpetualMarkets"
    assert raw.source_version == "indexer-v4-public"
    assert raw.payload_json == payload.decode()
    assert raw.source_hash == hashlib.sha256(payload).hexdigest()
    assert raw.venue_timestamp is None
    assert raw.observed_at == RECEIVED_1
    assert raw.received_monotonic_ns == 2_500_000
    assert raw.request_latency_ms == Decimal("1.5")

    assert [item.asset for item in batch.normalized] == [Asset.BTC, Asset.ETH, Asset.SOL]
    assert [item.symbol for item in batch.normalized] == ["BTC-USD", "ETH-USD", "SOL-USD"]
    assert [item.quantity_step for item in batch.normalized] == [
        Decimal("0.0001"),
        Decimal("0.001"),
        Decimal("0.01"),
    ]
    assert [item.price_tick for item in batch.normalized] == [
        Decimal("1"),
        Decimal("0.1"),
        Decimal("0.01"),
    ]
    for item in batch.normalized:
        assert item.instrument_id == f"dydx:{item.symbol}"
        assert item.kind is InstrumentKind.LINEAR_PERPETUAL
        assert item.contract_multiplier == Decimal(1)
        assert item.collateral_asset == "USDC"
        assert item.pnl_asset == "USDC"
        assert item.funding_interval_hours == Decimal(1)
        assert item.observed_at == RECEIVED_1
        assert item.source_hash == raw.source_hash
        assert item.is_inverse is False
        assert item.is_prelaunch is False
        assert item.index_family is None
        assert item.oracle_family is None
        assert item.mark_method is None
        assert item.liquidation_method is None
        assert item.funding_formula_id is None
        assert item.funding_cap is None
        assert item.funding_payment_offset_minutes is None
        assert item.min_notional is None


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("missing", "missing"),
        ("inactive", "ACTIVE"),
        ("ticker_mismatch", "ticker"),
        ("zero_step", "positive"),
        ("invalid_tick", "decimal"),
        ("non_mapping_row", "mapping"),
        ("non_mapping_markets", "mapping"),
    ],
)
def test_fetch_instruments_fails_closed_on_incomplete_or_malformed_requested_market(
    mutation: str, message: str
) -> None:
    # Catches partial requested-universe acceptance and malformed numeric evidence.
    document = json.loads(fixture_bytes("perpetual_markets.json"))
    markets = document["markets"]
    if mutation == "missing":
        del markets["BTC-USD"]
    elif mutation == "inactive":
        markets["BTC-USD"]["status"] = "PAUSED"
    elif mutation == "ticker_mismatch":
        markets["BTC-USD"]["ticker"] = "ETH-USD"
    elif mutation == "zero_step":
        markets["BTC-USD"]["stepSize"] = "0"
    elif mutation == "invalid_tick":
        markets["BTC-USD"]["tickSize"] = "not-a-decimal"
    elif mutation == "non_mapping_row":
        markets["BTC-USD"] = []
    else:
        document["markets"] = []
    adapter = make_adapter(
        lambda request: response(json.dumps(document).encode()), wall_times=[RECEIVED_1]
    )

    with pytest.raises(ValueError, match=message):
        asyncio.run(adapter.fetch_instruments(frozenset({Asset.BTC}), REQUEST_CONTEXT))


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (b"not-json", "JSON"),
        (b'\xff{"markets":{}}', "UTF-8"),
        (b"[]", "mapping"),
    ],
)
def test_fetch_instruments_rejects_invalid_response_document(payload: bytes, message: str) -> None:
    # Catches corrupt public bytes becoming normalized evidence.
    adapter = make_adapter(lambda request: response(payload), wall_times=[RECEIVED_1])

    with pytest.raises(ValueError, match=message):
        asyncio.run(adapter.fetch_instruments(frozenset({Asset.BTC}), REQUEST_CONTEXT))


def test_fetch_market_snapshots_preserves_raw_and_warns_instead_of_inventing_mark() -> None:
    # Catches an oracle/midpoint substitution into the required mark-price field.
    payload = fixture_bytes("perpetual_markets.json")
    adapter = make_adapter(lambda request: response(payload), wall_times=[RECEIVED_1])

    batch = asyncio.run(
        adapter.fetch_market_snapshots(frozenset({Asset.SOL, Asset.BTC}), REQUEST_CONTEXT)
    )

    assert len(batch.raw) == 1
    assert batch.raw[0].payload_json == payload.decode()
    assert batch.normalized == ()
    assert batch.warnings == (
        AdapterWarning(
            code="DYDX_MARK_PRICE_UNAVAILABLE",
            venue=Venue.DYDX,
            endpoint="/v4/perpetualMarkets",
            symbol="BTC-USD",
            message="dYdX public market evidence has no documented mark-price field",
        ),
        AdapterWarning(
            code="DYDX_MARK_PRICE_UNAVAILABLE",
            venue=Venue.DYDX,
            endpoint="/v4/perpetualMarkets",
            symbol="SOL-USD",
            message="dYdX public market evidence has no documented mark-price field",
        ),
    )


def test_collection_context_must_be_aware_before_any_request() -> None:
    # Catches network I/O before the protocol validates its caller-supplied time context.
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return response(fixture_bytes("perpetual_markets.json"))

    adapter = make_adapter(handler, wall_times=[])

    with pytest.raises(ValueError, match="timezone-aware"):
        asyncio.run(
            adapter.fetch_instruments(frozenset({Asset.BTC}), datetime(2026, 8, 13, 12, 0, 0))
        )
    assert called is False


def test_fetch_funding_history_paginates_backward_filters_range_and_keeps_page_lineage() -> None:
    # Catches a forward cursor, inclusive-boundary loop, lost raw page, or wrong page lineage.
    pages = iter(
        [
            fixture_bytes("funding_history_page_1.json"),
            fixture_bytes("funding_history_page_2.json"),
        ]
    )
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return response(next(pages))

    adapter = make_adapter(
        handler,
        wall_times=[RECEIVED_1, RECEIVED_2],
        monotonic_times=[100, 200, 300, 450],
    )
    batch = asyncio.run(
        adapter.fetch_funding_history(Asset.BTC, FUNDING_START, FUNDING_END, REQUEST_CONTEXT)
    )

    assert [request.method for request in requests] == ["GET", "GET"]
    assert [request.url.path for request in requests] == [
        "/v4/historicalFunding/BTC-USD",
        "/v4/historicalFunding/BTC-USD",
    ]
    assert [dict(request.url.params) for request in requests] == [
        {"limit": "100", "effectiveBeforeOrAt": "2026-08-13T12:00:00Z"},
        {"limit": "100", "effectiveBeforeOrAt": "2026-08-13T10:59:59.999999Z"},
    ]
    assert [raw.payload_json for raw in batch.raw] == [
        fixture_bytes("funding_history_page_1.json").decode(),
        fixture_bytes("funding_history_page_2.json").decode(),
    ]
    assert [item.effective_at for item in batch.normalized] == [
        FUNDING_START,
        FUNDING_START + timedelta(hours=1),
        FUNDING_END,
    ]
    assert [item.rate for item in batch.normalized] == [
        Decimal("0.000004"),
        Decimal("0.0000125"),
        Decimal("-0.000003"),
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
    assert all(item.venue is Venue.DYDX for item in batch.normalized)
    assert all(item.symbol == "BTC-USD" for item in batch.normalized)
    assert all(item.interval_hours == Decimal(1) for item in batch.normalized)


def test_fetch_funding_history_normalizes_non_utc_bounds_in_query() -> None:
    # Catches local-offset text leaking into a supposedly canonical UTC cursor.
    eastern = timezone(-timedelta(hours=4))
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return response(b'{"historicalFunding":[]}')

    adapter = make_adapter(handler, wall_times=[RECEIVED_1])
    batch = asyncio.run(
        adapter.fetch_funding_history(
            Asset.BTC,
            FUNDING_START.astimezone(eastern),
            FUNDING_END.astimezone(eastern),
            REQUEST_CONTEXT,
        )
    )

    assert dict(requests[0].url.params)["effectiveBeforeOrAt"] == "2026-08-13T12:00:00Z"
    assert len(batch.raw) == 1
    assert batch.normalized == ()


@pytest.mark.parametrize(
    ("start", "end", "message"),
    [
        (datetime(2026, 8, 13, 10), FUNDING_END, "timezone-aware"),
        (FUNDING_START, datetime(2026, 8, 13, 12), "timezone-aware"),
        (FUNDING_END, FUNDING_START, "must not precede"),
    ],
)
def test_fetch_funding_history_rejects_invalid_range_before_request(
    start: datetime, end: datetime, message: str
) -> None:
    # Catches ambiguous or reversed evidence windows reaching the network.
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return response(b'{"historicalFunding":[]}')

    adapter = make_adapter(handler, wall_times=[])
    with pytest.raises(ValueError, match=message):
        asyncio.run(adapter.fetch_funding_history(Asset.BTC, start, end, REQUEST_CONTEXT))
    assert called is False


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ({}, "historicalFunding"),
        ({"historicalFunding": {}}, "list"),
        ({"historicalFunding": ["bad"]}, "mapping"),
        (
            {
                "historicalFunding": [
                    {
                        "ticker": "ETH-USD",
                        "rate": "0.0001",
                        "effectiveAt": "2026-08-13T11:00:00Z",
                    }
                ]
            },
            "ticker",
        ),
        (
            {
                "historicalFunding": [
                    {
                        "ticker": "BTC-USD",
                        "rate": "bad",
                        "effectiveAt": "2026-08-13T11:00:00Z",
                    }
                ]
            },
            "decimal",
        ),
        (
            {"historicalFunding": [{"ticker": "BTC-USD", "rate": "0.0001"}]},
            "effectiveAt",
        ),
        (
            {
                "historicalFunding": [
                    {
                        "ticker": "BTC-USD",
                        "rate": "0.0001",
                        "effectiveAt": "2026-08-13T11:00:00",
                    }
                ]
            },
            "timezone-aware",
        ),
        (
            {
                "historicalFunding": [
                    {
                        "ticker": "BTC-USD",
                        "rate": "0.0001",
                        "effectiveAt": "not-a-time",
                    }
                ]
            },
            "ISO-8601",
        ),
        (
            {
                "historicalFunding": [
                    {
                        "ticker": "BTC-USD",
                        "rate": "0.0001",
                        "effectiveAt": "2026-08-13T12:00:02Z",
                    }
                ]
            },
            "response receipt",
        ),
        (
            {
                "historicalFunding": [
                    {
                        "ticker": "BTC-USD",
                        "rate": "0.0000000000000000001",
                        "effectiveAt": "2026-08-13T11:00:00Z",
                    }
                ]
            },
            "decimal places",
        ),
    ],
)
def test_fetch_funding_history_fails_closed_on_malformed_rows(
    document: object, message: str
) -> None:
    # Catches malformed, cross-market, future, or unrepresentable funding evidence.
    adapter = make_adapter(
        lambda request: response(json.dumps(document).encode()), wall_times=[RECEIVED_1]
    )

    with pytest.raises((ValueError, TypeError), match=message):
        asyncio.run(
            adapter.fetch_funding_history(Asset.BTC, FUNDING_START, FUNDING_END, REQUEST_CONTEXT)
        )


def test_fetch_funding_history_rejects_conflicting_duplicate_in_one_page() -> None:
    # Catches nondeterministic overwrite of immutable timestamp identity.
    document = json.loads(fixture_bytes("funding_history_page_1.json"))
    document["historicalFunding"][2]["rate"] = "0.9"
    adapter = make_adapter(
        lambda request: response(json.dumps(document).encode()), wall_times=[RECEIVED_1]
    )

    with pytest.raises(ValueError, match="conflicting duplicate"):
        asyncio.run(
            adapter.fetch_funding_history(Asset.BTC, FUNDING_START, FUNDING_END, REQUEST_CONTEXT)
        )


def test_fetch_funding_history_rejects_response_larger_than_requested_page() -> None:
    # Catches a changed server contract invalidating the paginator's finite evidence assumptions.
    row = {
        "ticker": "BTC-USD",
        "rate": "0.0001",
        "effectiveAt": "2026-08-13T11:00:00Z",
    }
    payload = json.dumps({"historicalFunding": [row] * 101}).encode()
    adapter = make_adapter(lambda request: response(payload), wall_times=[RECEIVED_1])

    with pytest.raises(ValueError, match="100-row"):
        asyncio.run(
            adapter.fetch_funding_history(Asset.BTC, FUNDING_START, FUNDING_END, REQUEST_CONTEXT)
        )


def test_fetch_funding_history_rejects_repeated_page_newer_than_backward_cursor() -> None:
    # Catches an endpoint ignoring the cursor and driving a redundant or infinite loop.
    payload = fixture_bytes("funding_history_page_1.json")
    adapter = make_adapter(lambda request: response(payload), wall_times=[RECEIVED_1, RECEIVED_2])

    with pytest.raises(PaginationStalledError, match="cursor"):
        asyncio.run(
            adapter.fetch_funding_history(Asset.BTC, FUNDING_START, FUNDING_END, REQUEST_CONTEXT)
        )


def test_fetch_funding_history_raises_when_configured_page_budget_is_exhausted() -> None:
    # Catches silent truncation when the finite safety budget is too small for the range.
    adapter = make_adapter(
        lambda request: response(fixture_bytes("funding_history_page_1.json")),
        wall_times=[RECEIVED_1],
        max_funding_pages=1,
    )

    with pytest.raises(PaginationStalledError, match="budget"):
        asyncio.run(
            adapter.fetch_funding_history(Asset.BTC, FUNDING_START, FUNDING_END, REQUEST_CONTEXT)
        )


@pytest.mark.parametrize("value", [True, 1.5, 0, -1])
def test_adapter_rejects_invalid_funding_page_budget(value: object) -> None:
    # Catches bool-as-int acceptance and an unbounded or unusable paginator configuration.
    with pytest.raises((TypeError, ValueError), match="funding pages"):
        DydxPublicAdapter(
            httpx.AsyncClient(transport=httpx.MockTransport(lambda request: response(b"{}"))),
            wall_clock=lambda: RECEIVED_1,
            monotonic_ns=lambda: 1,
            max_funding_pages=value,
        )
