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

FIXTURES = Path(__file__).parents[1] / "fixtures" / "lighter"
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
) -> Any:
    from polytrading.venues.lighter import LighterPublicAdapter

    return LighterPublicAdapter(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        wall_clock=SequenceClock(wall_times),
        monotonic_ns=SequenceClock(monotonic_times or list(range(100, 10_000, 100))),
    )


def response(payload: bytes) -> httpx.Response:
    return httpx.Response(200, content=payload, headers={"content-type": "application/json"})


def test_fetch_instruments_preserves_raw_and_normalizes_resolved_active_markets() -> None:
    # Catches hard-coded market IDs, response-order leakage, float parsing, or guessed fields.
    payload = fixture_bytes("order_books.json")
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
        ("GET", "https://mainnet.zklighter.elliot.ai/api/v1/orderBooks?filter=perp")
    ]
    assert len(batch.raw) == 1
    raw = batch.raw[0]
    assert raw.venue is Venue.LIGHTER
    assert raw.endpoint == "/api/v1/orderBooks"
    assert raw.source_version == "mainnet-v1-public"
    assert raw.payload_json == payload.decode()
    assert raw.source_hash == hashlib.sha256(payload).hexdigest()
    assert raw.venue_timestamp is None
    assert raw.observed_at == RECEIVED_1
    assert raw.received_monotonic_ns == 2_500_000
    assert raw.request_latency_ms == Decimal("1.5")

    assert [item.asset for item in batch.normalized] == [Asset.BTC, Asset.ETH, Asset.SOL]
    assert [item.symbol for item in batch.normalized] == ["BTC", "ETH", "SOL"]
    assert [item.quantity_step for item in batch.normalized] == [
        Decimal("0.00001"),
        Decimal("0.0001"),
        Decimal("0.001"),
    ]
    assert [item.price_tick for item in batch.normalized] == [
        Decimal("0.1"),
        Decimal("0.01"),
        Decimal("0.001"),
    ]
    for item in batch.normalized:
        assert item.instrument_id == f"lighter:{item.symbol}"
        assert item.kind is InstrumentKind.LINEAR_PERPETUAL
        assert item.contract_multiplier == Decimal(1)
        assert item.collateral_asset == "USDC"
        assert item.pnl_asset == "USDC"
        assert item.funding_interval_hours == Decimal(1)
        assert item.min_notional == Decimal("10.000000")
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


def test_fetch_instruments_rejects_one_market_id_assigned_to_two_requested_symbols() -> None:
    # Catches contradictory market-ID resolution routing later funding or books to the wrong asset.
    document = json.loads(fixture_bytes("order_books.json"))
    rows = {row["symbol"]: row for row in document["order_books"]}
    rows["BTC"]["market_id"] = rows["ETH"]["market_id"]
    adapter = make_adapter(
        lambda request: response(json.dumps(document).encode()), wall_times=[RECEIVED_1]
    )

    with pytest.raises(ValueError, match="market_id"):
        asyncio.run(adapter.fetch_instruments(frozenset({Asset.BTC, Asset.ETH}), REQUEST_CONTEXT))


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("missing", "missing"),
        ("inactive", "active"),
        ("spot", "perpetual"),
        ("duplicate_symbol", "duplicate symbol"),
        ("bool_market_id", "integer"),
        ("negative_market_id", "nonnegative"),
        ("bad_size_decimals", "between 0 and 18"),
        ("bad_price_decimals", "integer"),
        ("zero_multiplier", "positive"),
        ("invalid_multiplier", "decimal"),
        ("zero_min_quote", "positive"),
        ("non_mapping_row", "mapping"),
        ("non_list_markets", "list"),
    ],
)
def test_fetch_instruments_fails_closed_on_malformed_requested_metadata(
    mutation: str, message: str
) -> None:
    # Catches partial-universe acceptance and malformed market metadata becoming normalized data.
    document = json.loads(fixture_bytes("order_books.json"))
    rows = document["order_books"]
    btc = next(row for row in rows if row["symbol"] == "BTC")
    if mutation == "missing":
        rows.remove(btc)
    elif mutation == "inactive":
        btc["status"] = "inactive"
    elif mutation == "spot":
        btc["market_type"] = "spot"
    elif mutation == "duplicate_symbol":
        rows.append(dict(btc))
    elif mutation == "bool_market_id":
        btc["market_id"] = True
    elif mutation == "negative_market_id":
        btc["market_id"] = -1
    elif mutation == "bad_size_decimals":
        btc["supported_size_decimals"] = 19
    elif mutation == "bad_price_decimals":
        btc["supported_price_decimals"] = 1.5
    elif mutation == "zero_multiplier":
        btc["multiplier"] = "0"
    elif mutation == "invalid_multiplier":
        btc["multiplier"] = "not-a-number"
    elif mutation == "zero_min_quote":
        btc["min_quote_amount"] = "0"
    elif mutation == "non_mapping_row":
        rows[rows.index(btc)] = []
    else:
        document["order_books"] = {}
    adapter = make_adapter(
        lambda request: response(json.dumps(document).encode()), wall_times=[RECEIVED_1]
    )

    with pytest.raises(ValueError, match=message):
        asyncio.run(adapter.fetch_instruments(frozenset({Asset.BTC}), REQUEST_CONTEXT))


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (b"not-json", "JSON"),
        (b'\xff{"code":200}', "UTF-8"),
        (b"[]", "mapping"),
        (b'{"order_books":[]}', "code"),
        (b'{"code":true,"order_books":[]}', "integer"),
    ],
)
def test_fetch_instruments_rejects_invalid_response_document(payload: bytes, message: str) -> None:
    # Catches corrupt public bytes or ambiguous API success becoming evidence.
    adapter = make_adapter(lambda request: response(payload), wall_times=[RECEIVED_1])

    with pytest.raises(ValueError, match=message):
        asyncio.run(adapter.fetch_instruments(frozenset({Asset.BTC}), REQUEST_CONTEXT))


def test_fetch_instruments_rejects_duplicate_json_key() -> None:
    # Catches the JSON decoder silently selecting the last of contradictory values.
    adapter = make_adapter(
        lambda request: response(b'{"code":200,"code":400,"order_books":[]}'),
        wall_times=[RECEIVED_1],
    )

    with pytest.raises(ValueError, match="duplicate JSON object key"):
        asyncio.run(adapter.fetch_instruments(frozenset({Asset.BTC}), REQUEST_CONTEXT))


def test_fetch_instruments_sanitizes_api_error_message() -> None:
    # Catches an upstream message or response body being copied into operator-visible exceptions.
    payload = b'{"code":400,"message":"wallet-secret-value","order_books":[]}'
    adapter = make_adapter(lambda request: response(payload), wall_times=[RECEIVED_1])

    with pytest.raises(ValueError, match="API code 400") as raised:
        asyncio.run(adapter.fetch_instruments(frozenset({Asset.BTC}), REQUEST_CONTEXT))
    assert "wallet-secret-value" not in str(raised.value)


def test_collection_context_must_be_aware_before_any_request() -> None:
    # Catches network I/O before caller-supplied temporal context is validated.
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return response(fixture_bytes("order_books.json"))

    adapter = make_adapter(handler, wall_times=[])

    with pytest.raises(ValueError, match="timezone-aware"):
        asyncio.run(
            adapter.fetch_instruments(frozenset({Asset.BTC}), datetime(2026, 8, 13, 12, 0, 0))
        )
    assert called is False
