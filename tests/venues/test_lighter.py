from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest

from polytrading.domain.models import Asset, InstrumentKind, Venue
from polytrading.venues.public import AdapterWarning

FIXTURES = Path(__file__).parents[1] / "fixtures" / "lighter"
REQUEST_CONTEXT = datetime(2026, 8, 13, 11, 59, tzinfo=UTC)
RECEIVED_1 = datetime(2026, 8, 13, 12, 0, 1, tzinfo=UTC)
RECEIVED_2 = datetime(2026, 8, 13, 12, 0, 2, tzinfo=UTC)
FUNDING_START = datetime(2026, 8, 13, 10, tzinfo=UTC)
FUNDING_END = datetime(2026, 8, 13, 12, tzinfo=UTC)
CYCLE_ID = UUID("00000000-0000-0000-0000-000000000813")


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


def test_fetch_market_snapshots_preserves_raw_and_warns_without_inventing_prices() -> None:
    # Catches last-trade or midpoint substitution into required mark and index fields.
    payload = fixture_bytes("order_books.json")
    adapter = make_adapter(lambda request: response(payload), wall_times=[RECEIVED_1])

    batch = asyncio.run(
        adapter.fetch_market_snapshots(frozenset({Asset.SOL, Asset.BTC}), REQUEST_CONTEXT)
    )

    assert len(batch.raw) == 1
    assert batch.raw[0].payload_json == payload.decode()
    assert batch.normalized == ()
    assert batch.warnings == (
        AdapterWarning(
            code="LIGHTER_MARK_INDEX_UNAVAILABLE",
            venue=Venue.LIGHTER,
            endpoint="/api/v1/orderBooks",
            symbol="BTC",
            message=("Lighter REST evidence has no response-timestamped mark and index price pair"),
        ),
        AdapterWarning(
            code="LIGHTER_MARK_INDEX_UNAVAILABLE",
            venue=Venue.LIGHTER,
            endpoint="/api/v1/orderBooks",
            symbol="SOL",
            message=("Lighter REST evidence has no response-timestamped mark and index price pair"),
        ),
    )


def test_fetch_funding_history_resolves_market_and_normalizes_settled_direction() -> None:
    # Catches use of projected rates, hard-coded IDs, unsigned history, or range leakage.
    market_payload = fixture_bytes("order_books.json")
    funding_payload = fixture_bytes("fundings.json")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return response(
            market_payload if request.url.path.endswith("orderBooks") else funding_payload
        )

    adapter = make_adapter(
        handler,
        wall_times=[RECEIVED_1, RECEIVED_2],
        monotonic_times=[100, 200, 300, 450],
    )
    batch = asyncio.run(
        adapter.fetch_funding_history(Asset.BTC, FUNDING_START, FUNDING_END, REQUEST_CONTEXT)
    )

    assert [request.url.path for request in requests] == [
        "/api/v1/orderBooks",
        "/api/v1/fundings",
    ]
    assert [dict(request.url.params) for request in requests] == [
        {"filter": "perp"},
        {
            "market_id": "1",
            "resolution": "1h",
            "start_timestamp": "1786615200",
            "end_timestamp": "1786622400",
            "count_back": "3",
        },
    ]
    assert "/api/v1/funding-rates" not in [request.url.path for request in requests]
    assert [raw.payload_json for raw in batch.raw] == [
        market_payload.decode(),
        funding_payload.decode(),
    ]
    assert [item.effective_at for item in batch.normalized] == [
        FUNDING_START,
        FUNDING_START + timedelta(hours=1),
        FUNDING_END,
    ]
    assert [item.rate for item in batch.normalized] == [
        Decimal("0.0002"),
        Decimal("-0.0003"),
        Decimal("0"),
    ]
    assert all(item.venue is Venue.LIGHTER for item in batch.normalized)
    assert all(item.symbol == "BTC" for item in batch.normalized)
    assert all(item.asset is Asset.BTC for item in batch.normalized)
    assert all(item.interval_hours == Decimal(1) for item in batch.normalized)
    assert all(item.observed_at == RECEIVED_2 for item in batch.normalized)
    assert all(item.source_hash == batch.raw[1].source_hash for item in batch.normalized)


def test_fetch_funding_history_rejects_more_rows_than_requested_count_back() -> None:
    # Catches a changed endpoint contract invalidating the bounded one-request assumption.
    document = {
        "code": 200,
        "resolution": "1h",
        "fundings": [
            {
                "timestamp": 1786615200,
                "value": "0.1",
                "rate": "0.0001",
                "direction": "long",
            },
            {
                "timestamp": 1786611600,
                "value": "0.1",
                "rate": "0.0001",
                "direction": "long",
            },
        ],
    }
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload = fixture_bytes("order_books.json") if calls == 1 else json.dumps(document).encode()
        return response(payload)

    adapter = make_adapter(handler, wall_times=[RECEIVED_1, RECEIVED_2])

    with pytest.raises(ValueError, match="count_back"):
        asyncio.run(
            adapter.fetch_funding_history(Asset.BTC, FUNDING_START, FUNDING_START, REQUEST_CONTEXT)
        )


@pytest.mark.parametrize(
    ("start", "end", "message"),
    [
        (datetime(2026, 8, 13, 10), FUNDING_END, "timezone-aware"),
        (FUNDING_START, datetime(2026, 8, 13, 12), "timezone-aware"),
        (FUNDING_END, FUNDING_START, "must not precede"),
        (FUNDING_START, FUNDING_START + timedelta(days=7, seconds=1), "seven days"),
    ],
)
def test_fetch_funding_history_rejects_invalid_range_before_request(
    start: datetime, end: datetime, message: str
) -> None:
    # Catches ambiguous, reversed, or unbounded evidence windows reaching the network.
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return response(fixture_bytes("order_books.json"))

    adapter = make_adapter(handler, wall_times=[])

    with pytest.raises(ValueError, match=message):
        asyncio.run(adapter.fetch_funding_history(Asset.BTC, start, end, REQUEST_CONTEXT))
    assert called is False


def test_fetch_funding_history_accepts_exact_seven_days_and_normalizes_query_seconds() -> None:
    # Catches off-by-one rejection at the cap or local-offset/subsecond query corruption.
    eastern = timezone(-timedelta(hours=4))
    start = datetime(2026, 8, 6, 10, 0, 0, 250_000, tzinfo=UTC)
    end = start + timedelta(days=7)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("orderBooks"):
            return response(fixture_bytes("order_books.json"))
        return response(b'{"code":200,"resolution":"1h","fundings":[]}')

    adapter = make_adapter(handler, wall_times=[RECEIVED_1, RECEIVED_2])
    batch = asyncio.run(
        adapter.fetch_funding_history(
            Asset.BTC, start.astimezone(eastern), end.astimezone(eastern), REQUEST_CONTEXT
        )
    )

    assert dict(requests[1].url.params) == {
        "market_id": "1",
        "resolution": "1h",
        "start_timestamp": "1786010400",
        "end_timestamp": "1786615201",
        "count_back": "169",
    }
    assert len(batch.raw) == 2
    assert batch.normalized == ()


def funding_adapter_for(document: object) -> Any:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload = fixture_bytes("order_books.json") if calls == 1 else json.dumps(document).encode()
        return response(payload)

    return make_adapter(handler, wall_times=[RECEIVED_1, RECEIVED_2])


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ({"code": 200, "resolution": "8h", "fundings": []}, "resolution"),
        ({"code": 200, "resolution": "1h"}, "fundings"),
        ({"code": 200, "resolution": "1h", "fundings": {}}, "list"),
        ({"code": 200, "resolution": "1h", "fundings": ["bad"]}, "mapping"),
        (
            {
                "code": 200,
                "resolution": "1h",
                "fundings": [{"timestamp": True, "rate": "0.1", "direction": "long"}],
            },
            "integer",
        ),
        (
            {
                "code": 200,
                "resolution": "1h",
                "fundings": [{"timestamp": 1786615200, "rate": 0.1, "direction": "long"}],
            },
            "decimal string",
        ),
        (
            {
                "code": 200,
                "resolution": "1h",
                "fundings": [{"timestamp": 1786615200, "rate": "-0.1", "direction": "long"}],
            },
            "nonnegative",
        ),
        (
            {
                "code": 200,
                "resolution": "1h",
                "fundings": [{"timestamp": 1786615200, "rate": "0.1", "direction": "both"}],
            },
            "long or short",
        ),
        (
            {
                "code": 200,
                "resolution": "1h",
                "fundings": [{"timestamp": 1786622403, "rate": "0.1", "direction": "long"}],
            },
            "response receipt",
        ),
    ],
)
def test_fetch_funding_history_fails_closed_on_malformed_settled_rows(
    document: object, message: str
) -> None:
    # Catches malformed, projected, or future values becoming settled funding evidence.
    adapter = funding_adapter_for(document)

    with pytest.raises(ValueError, match=message):
        asyncio.run(
            adapter.fetch_funding_history(Asset.BTC, FUNDING_START, FUNDING_END, REQUEST_CONTEXT)
        )


def test_fetch_funding_history_accepts_identical_duplicate_once() -> None:
    # Catches an identical immutable timestamp appearing twice in normalized output.
    document = {
        "code": 200,
        "resolution": "1h",
        "fundings": [
            {
                "timestamp": 1786615200,
                "rate": "0.0002",
                "direction": "long",
            },
            {
                "timestamp": 1786615200,
                "rate": "0.0002",
                "direction": "long",
            },
        ],
    }
    adapter = funding_adapter_for(document)

    batch = asyncio.run(
        adapter.fetch_funding_history(
            Asset.BTC,
            FUNDING_START,
            FUNDING_START + timedelta(hours=1),
            REQUEST_CONTEXT,
        )
    )

    assert len(batch.normalized) == 1
    assert batch.normalized[0].effective_at == FUNDING_START
    assert batch.normalized[0].rate == Decimal("0.0002")


def test_fetch_funding_history_filters_to_closed_requested_range() -> None:
    # Catches API boundary overfetch leaking into normalized settled history.
    document = {
        "code": 200,
        "resolution": "1h",
        "fundings": [
            {"timestamp": 1786615200, "rate": "0.0002", "direction": "long"},
            {"timestamp": 1786618800, "rate": "0.0003", "direction": "long"},
        ],
    }
    adapter = funding_adapter_for(document)

    batch = asyncio.run(
        adapter.fetch_funding_history(
            Asset.BTC,
            FUNDING_START + timedelta(minutes=30),
            FUNDING_START + timedelta(hours=1),
            REQUEST_CONTEXT,
        )
    )

    assert [item.effective_at for item in batch.normalized] == [FUNDING_START + timedelta(hours=1)]


def test_fetch_funding_history_rejects_conflicting_duplicate() -> None:
    # Catches nondeterministic overwrite of an immutable settled timestamp.
    document = {
        "code": 200,
        "resolution": "1h",
        "fundings": [
            {"timestamp": 1786615200, "rate": "0.0002", "direction": "long"},
            {"timestamp": 1786615200, "rate": "0.0002", "direction": "short"},
        ],
    }
    adapter = funding_adapter_for(document)

    with pytest.raises(ValueError, match="conflicting duplicate"):
        asyncio.run(
            adapter.fetch_funding_history(
                Asset.BTC, FUNDING_START, FUNDING_START + timedelta(hours=1), REQUEST_CONTEXT
            )
        )


def test_fetch_order_books_aggregates_orders_and_uses_local_receipt_time() -> None:
    # Catches response-order leakage, missing price aggregation, hard-coded IDs, or invented timing.
    market_payload = fixture_bytes("order_books.json")
    book_payload = fixture_bytes("order_book_orders.json")
    requests: list[httpx.Request] = []
    receipts = [
        RECEIVED_1,
        datetime(2026, 8, 13, 12, 1, 1, tzinfo=UTC),
        datetime(2026, 8, 13, 12, 1, 2, tzinfo=UTC),
        datetime(2026, 8, 13, 12, 1, 3, tzinfo=UTC),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return response(market_payload if request.url.path.endswith("orderBooks") else book_payload)

    adapter = make_adapter(handler, wall_times=receipts)
    batch = asyncio.run(
        adapter.fetch_order_books(
            frozenset({Asset.SOL, Asset.BTC, Asset.ETH}), REQUEST_CONTEXT, CYCLE_ID
        )
    )

    assert [request.url.path for request in requests] == [
        "/api/v1/orderBooks",
        "/api/v1/orderBookOrders",
        "/api/v1/orderBookOrders",
        "/api/v1/orderBookOrders",
    ]
    assert [dict(request.url.params) for request in requests] == [
        {"filter": "perp"},
        {"market_id": "1", "limit": "100"},
        {"market_id": "0", "limit": "100"},
        {"market_id": "2", "limit": "100"},
    ]
    assert [raw.payload_json for raw in batch.raw] == [
        market_payload.decode(),
        book_payload.decode(),
        book_payload.decode(),
        book_payload.decode(),
    ]
    assert all(raw.venue_timestamp is None for raw in batch.raw)
    assert [book.asset for book in batch.normalized] == [Asset.BTC, Asset.ETH, Asset.SOL]
    assert [book.symbol for book in batch.normalized] == ["BTC", "ETH", "SOL"]
    for index, book in enumerate(batch.normalized):
        assert book.cycle_id == CYCLE_ID
        assert book.depth_limit == 20
        assert book.sequence is None
        assert book.effective_at == receipts[index + 1]
        assert book.observed_at == receipts[index + 1]
        assert book.source_hash == batch.raw[index + 1].source_hash
        assert [(level.price, level.quantity, level.order_count) for level in book.bids] == [
            (Decimal("99.00"), Decimal("1.5000"), 2),
            (Decimal("98.00"), Decimal("2.0000"), 1),
            (Decimal("97.00"), Decimal("1.2500"), 1),
        ]
        assert [(level.price, level.quantity, level.order_count) for level in book.asks] == [
            (Decimal("101.00"), Decimal("1.2500"), 2),
            (Decimal("102.00"), Decimal("2.0000"), 1),
            (Decimal("103.00"), Decimal("3.0000"), 1),
        ]
    assert batch.warnings == tuple(
        AdapterWarning(
            code="LIGHTER_REST_BOOK_LOCAL_TIMESTAMP",
            venue=Venue.LIGHTER,
            endpoint="/api/v1/orderBookOrders",
            symbol=symbol,
            message=(
                "Lighter REST book has no venue snapshot timestamp or sequence; "
                "local receipt time was used"
            ),
        )
        for symbol in ("BTC", "ETH", "SOL")
    )


def test_fetch_order_books_rejects_blank_order_id() -> None:
    # Catches distinct public orders collapsing onto an unusable empty identity.
    document = json.loads(fixture_bytes("order_book_orders.json"))
    document["bids"][0]["order_id"] = ""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload = fixture_bytes("order_books.json") if calls == 1 else json.dumps(document).encode()
        return response(payload)

    adapter = make_adapter(handler, wall_times=[RECEIVED_1, RECEIVED_2])

    with pytest.raises(ValueError, match="order_id"):
        asyncio.run(adapter.fetch_order_books(frozenset({Asset.BTC}), REQUEST_CONTEXT, CYCLE_ID))


def book_adapter_for(document: object, *, wall_times: list[datetime] | None = None) -> Any:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload = fixture_bytes("order_books.json") if calls == 1 else json.dumps(document).encode()
        return response(payload)

    return make_adapter(handler, wall_times=wall_times or [RECEIVED_1, RECEIVED_2])


def malformed_book(mutation: str) -> object:
    document = json.loads(fixture_bytes("order_book_orders.json"))
    if mutation == "missing_bids":
        del document["bids"]
    elif mutation == "non_list":
        document["bids"] = {}
    elif mutation == "negative_total":
        document["total_bids"] = -1
    elif mutation == "bool_total":
        document["total_bids"] = True
    elif mutation == "total_mismatch":
        document["total_bids"] = 3
    elif mutation == "non_mapping_order":
        document["bids"][0] = "bad"
    elif mutation == "missing_price":
        del document["bids"][0]["price"]
    elif mutation == "invalid_price":
        document["bids"][0]["price"] = "bad"
    elif mutation == "zero_quantity":
        document["bids"][0]["remaining_base_amount"] = "0"
    elif mutation == "duplicate_id_within":
        document["bids"][1]["order_id"] = document["bids"][0]["order_id"]
    elif mutation == "duplicate_id_across":
        document["asks"][0]["order_id"] = document["bids"][0]["order_id"]
    elif mutation == "empty_side":
        document["asks"] = []
        document["total_asks"] = 0
    elif mutation == "locked":
        document["asks"][0]["price"] = "99.00"
        document["asks"][2]["price"] = "99.00"
    else:
        document["asks"][0]["price"] = "98.00"
        document["asks"][2]["price"] = "98.00"
    return document


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("missing_bids", "bids"),
        ("non_list", "list"),
        ("negative_total", "nonnegative"),
        ("bool_total", "integer"),
        ("total_mismatch", "does not match"),
        ("non_mapping_order", "mapping"),
        ("missing_price", "price"),
        ("invalid_price", "decimal"),
        ("zero_quantity", "positive"),
        ("duplicate_id_within", "duplicate order_id"),
        ("duplicate_id_across", "duplicate order_id"),
        ("empty_side", "empty"),
        ("locked", "locked or crossed"),
        ("crossed", "locked or crossed"),
    ],
)
def test_fetch_order_books_fails_closed_on_malformed_or_impossible_response(
    mutation: str, message: str
) -> None:
    # Catches structurally invalid individual orders becoming executable-depth evidence.
    adapter = book_adapter_for(malformed_book(mutation))

    with pytest.raises(ValueError, match=message):
        asyncio.run(adapter.fetch_order_books(frozenset({Asset.BTC}), REQUEST_CONTEXT, CYCLE_ID))


def complete_order(order_id: str, price: int, quantity: str = "1.0") -> dict[str, object]:
    return {
        "order_index": int(order_id.split("-")[-1]),
        "order_id": order_id,
        "owner_account_index": 1,
        "initial_base_amount": quantity,
        "remaining_base_amount": quantity,
        "price": str(price),
        "order_expiry": 1789059567000,
        "transaction_time": 0,
    }


def test_fetch_order_books_retains_only_best_twenty_aggregated_prices() -> None:
    # Catches truncation before sorting or an unbounded depth result.
    bids = [complete_order(f"b-{index}", 99 - index) for index in range(22)]
    asks = [complete_order(f"a-{index + 100}", 101 + index) for index in range(22)]
    document = {
        "code": 200,
        "total_asks": 22,
        "asks": list(reversed(asks)),
        "total_bids": 22,
        "bids": list(reversed(bids)),
    }
    adapter = book_adapter_for(document)

    batch = asyncio.run(
        adapter.fetch_order_books(frozenset({Asset.BTC}), REQUEST_CONTEXT, CYCLE_ID)
    )

    book = batch.normalized[0]
    assert [level.price for level in book.bids] == [Decimal(value) for value in range(99, 79, -1)]
    assert [level.price for level in book.asks] == [Decimal(value) for value in range(101, 121)]


def test_fetch_order_books_rejects_more_than_requested_order_limit() -> None:
    # Catches endpoint limit drift invalidating bounded aggregation work.
    bids = [complete_order(f"b-{index}", 200 - index) for index in range(101)]
    document = {
        "code": 200,
        "total_asks": 1,
        "asks": [complete_order("a-200", 202)],
        "total_bids": 101,
        "bids": bids,
    }
    adapter = book_adapter_for(document)

    with pytest.raises(ValueError, match="order limit"):
        asyncio.run(adapter.fetch_order_books(frozenset({Asset.BTC}), REQUEST_CONTEXT, CYCLE_ID))


def test_fetch_order_books_does_not_return_partial_batch_after_later_asset_failure() -> None:
    # Catches a multi-asset call silently succeeding after one requested book is invalid.
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return response(fixture_bytes("order_books.json"))
        if calls == 2:
            return response(fixture_bytes("order_book_orders.json"))
        return response(json.dumps(malformed_book("empty_side")).encode())

    adapter = make_adapter(handler, wall_times=[RECEIVED_1, RECEIVED_2, RECEIVED_2])

    with pytest.raises(ValueError, match="empty"):
        asyncio.run(
            adapter.fetch_order_books(frozenset({Asset.BTC, Asset.ETH}), REQUEST_CONTEXT, CYCLE_ID)
        )
    assert calls == 3
