from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from polytrading.domain.models import Asset, InstrumentKind, Venue
from polytrading.venues.dydx import DydxPublicAdapter
from polytrading.venues.public import AdapterWarning

FIXTURES = Path(__file__).parents[1] / "fixtures" / "dydx"
REQUEST_CONTEXT = datetime(2026, 8, 13, 11, 59, tzinfo=UTC)
RECEIVED_1 = datetime(2026, 8, 13, 12, 0, 1, tzinfo=UTC)


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
) -> DydxPublicAdapter:
    return DydxPublicAdapter(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        wall_clock=SequenceClock(wall_times),
        monotonic_ns=SequenceClock(monotonic_times or list(range(100, 10_000, 100))),
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
