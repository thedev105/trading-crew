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

from polytrading.domain.models import Asset, InstrumentKind
from polytrading.venues.hyperliquid import (
    HyperliquidPublicAdapter,
    PaginationStalledError,
)

FIXTURES = Path(__file__).parents[1] / "fixtures" / "hyperliquid"
START = datetime(2026, 8, 11, 12, tzinfo=UTC)
END = datetime(2026, 8, 11, 13, tzinfo=UTC)
REQUEST_CONTEXT = datetime(2026, 8, 12, 10, tzinfo=UTC)
RECEIVED_1 = datetime(2026, 8, 12, 10, 0, 1, tzinfo=UTC)
RECEIVED_2 = datetime(2026, 8, 12, 10, 0, 2, tzinfo=UTC)
CYCLE_ID = UUID("00000000-0000-0000-0000-000000000601")


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
) -> HyperliquidPublicAdapter:
    return HyperliquidPublicAdapter(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        wall_clock=SequenceClock(wall_times),
        monotonic_ns=SequenceClock(monotonic_times or list(range(100, 1000, 100))),
    )


def decode_request(request: httpx.Request) -> dict[str, Any]:
    assert request.method == "POST"
    assert str(request.url) == "https://api.hyperliquid.xyz/info"
    return json.loads(request.content)


def response(payload: bytes) -> httpx.Response:
    return httpx.Response(200, content=payload, headers={"content-type": "application/json"})


def test_fetch_instruments_uses_meta_contract_and_keeps_unknown_compatibility_fields_none() -> None:
    # Catches a wrong endpoint/body, guessed compatibility metadata, or zip-truncated universe.
    payload = fixture_bytes("meta_and_asset_ctxs.json")
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(decode_request(request))
        return response(payload)

    adapter = make_adapter(
        handler,
        wall_times=[RECEIVED_1],
        monotonic_times=[1_000_000, 2_500_000],
    )
    batch = asyncio.run(
        adapter.fetch_instruments(
            frozenset({Asset.BTC, Asset.ETH, Asset.SOL}), REQUEST_CONTEXT
        )
    )

    assert requests == [{"type": "metaAndAssetCtxs"}]
    assert len(batch.raw) == 1
    assert batch.raw[0].payload_json == payload.decode()
    assert batch.raw[0].source_hash == hashlib.sha256(payload).hexdigest()
    assert batch.raw[0].observed_at == RECEIVED_1
    assert batch.raw[0].received_monotonic_ns == 2_500_000
    assert batch.raw[0].request_latency_ms == Decimal("1.5")
    assert [item.asset for item in batch.normalized] == [Asset.BTC, Asset.ETH, Asset.SOL]
    assert [item.quantity_step for item in batch.normalized] == [
        Decimal("0.00001"),
        Decimal("0.0001"),
        Decimal("0.01"),
    ]
    for item in batch.normalized:
        assert item.instrument_id == f"hyperliquid:{item.symbol}"
        assert item.symbol == item.asset.value
        assert item.kind is InstrumentKind.LINEAR_PERPETUAL
        assert item.contract_multiplier == Decimal(1)
        assert item.collateral_asset == "USDC"
        assert item.pnl_asset == "USDC"
        assert item.funding_interval_hours == Decimal(1)
        assert item.observed_at == RECEIVED_1
        assert item.source_hash == batch.raw[0].source_hash
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
        assert item.price_tick is None


def test_fetch_instruments_rejects_universe_context_length_mismatch() -> None:
    # Catches silent zip truncation when Hyperliquid metadata arrays lose alignment.
    document = json.loads(fixture_bytes("meta_and_asset_ctxs.json"))
    document[1].pop()
    adapter = make_adapter(
        lambda request: response(json.dumps(document).encode()), wall_times=[RECEIVED_1]
    )

    with pytest.raises(ValueError, match="align"):
        asyncio.run(adapter.fetch_instruments(frozenset({Asset.BTC}), REQUEST_CONTEXT))


def test_fetch_funding_history_paginates_deduplicates_and_preserves_page_evidence() -> None:
    # Catches wrong request bounds, lost raw pages, duplicate emissions, or backdated receipt time.
    pages = iter(
        [
            fixture_bytes("funding_history_page_1.json"),
            fixture_bytes("funding_history_page_2.json"),
        ]
    )
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(decode_request(request))
        return response(next(pages))

    adapter = make_adapter(
        handler,
        wall_times=[RECEIVED_1, RECEIVED_2],
        monotonic_times=[100, 200, 300, 450],
    )
    batch = asyncio.run(
        adapter.fetch_funding_history(Asset.BTC, START, END, REQUEST_CONTEXT)
    )

    assert requests == [
        {
            "type": "fundingHistory",
            "coin": "BTC",
            "startTime": 1786449600000,
            "endTime": 1786453200000,
        },
        {
            "type": "fundingHistory",
            "coin": "BTC",
            "startTime": 1786449600001,
            "endTime": 1786453200000,
        },
    ]
    assert [raw.payload_json for raw in batch.raw] == [
        fixture_bytes("funding_history_page_1.json").decode(),
        fixture_bytes("funding_history_page_2.json").decode(),
    ]
    assert [item.effective_at for item in batch.normalized] == [START, END]
    assert [item.rate for item in batch.normalized] == [
        Decimal("0.0000125"),
        Decimal("-0.0000030"),
    ]
    assert [item.observed_at for item in batch.normalized] == [RECEIVED_1, RECEIVED_2]
    assert [item.source_hash for item in batch.normalized] == [
        batch.raw[0].source_hash,
        batch.raw[1].source_hash,
    ]
    assert all(item.interval_hours == Decimal(1) for item in batch.normalized)


def test_fetch_funding_history_stops_on_empty_page() -> None:
    # Catches a paginator that continues requesting after the documented empty sentinel.
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        decode_request(request)
        calls += 1
        return response(b"[]\n")

    adapter = make_adapter(handler, wall_times=[RECEIVED_1])
    batch = asyncio.run(
        adapter.fetch_funding_history(Asset.BTC, START, END, REQUEST_CONTEXT)
    )

    assert calls == 1
    assert len(batch.raw) == 1
    assert batch.normalized == ()


def test_fetch_funding_history_rejects_conflicting_duplicate() -> None:
    # Catches nondeterministic overwrite when immutable funding identity repeats with a new rate.
    first = fixture_bytes("funding_history_page_1.json")
    conflict = json.dumps(
        [
            {
                "coin": "BTC",
                "fundingRate": "0.999",
                "premium": "0.000042",
                "time": 1786449600000,
            },
            {
                "coin": "BTC",
                "fundingRate": "0.0001",
                "premium": "0.0001",
                "time": 1786453200000,
            },
        ]
    ).encode()
    pages = iter([first, conflict])
    adapter = make_adapter(
        lambda request: response(next(pages)), wall_times=[RECEIVED_1, RECEIVED_2]
    )

    with pytest.raises(ValueError, match="conflicting duplicate"):
        asyncio.run(adapter.fetch_funding_history(Asset.BTC, START, END, REQUEST_CONTEXT))


def test_fetch_funding_history_rejects_rows_outside_closed_range() -> None:
    # Catches acceptance of server rows that do not belong to the requested evidence window.
    payload = json.dumps(
        [
            {
                "coin": "BTC",
                "fundingRate": "0.0001",
                "premium": "0.0001",
                "time": 1786449599999,
            }
        ]
    ).encode()
    adapter = make_adapter(lambda request: response(payload), wall_times=[RECEIVED_1])

    with pytest.raises(ValueError, match="outside requested range"):
        asyncio.run(adapter.fetch_funding_history(Asset.BTC, START, END, REQUEST_CONTEXT))


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ({"coin": "BTC"}, "must be a list"),
        ([{"coin": "ETH", "fundingRate": "0.0001", "time": 1786449600000}], "coin"),
        ([{"coin": "BTC", "fundingRate": "bad", "time": 1786449600000}], "decimal"),
        ([{"coin": "BTC", "fundingRate": "0.0001"}], "time"),
        (
            [
                {"coin": "BTC", "fundingRate": "0.0001", "time": 1786449600000}
                for _ in range(501)
            ],
            "500-row",
        ),
    ],
)
def test_fetch_funding_history_fails_closed_on_malformed_page(
    document: object, message: str
) -> None:
    # Catches malformed public rows being accepted as funding evidence.
    payload = json.dumps(document).encode()
    adapter = make_adapter(lambda request: response(payload), wall_times=[RECEIVED_1])

    with pytest.raises(ValueError, match=message):
        asyncio.run(adapter.fetch_funding_history(Asset.BTC, START, END, REQUEST_CONTEXT))


def test_fetch_funding_history_rejects_page_without_timestamp_progress() -> None:
    # Catches infinite/redundant pagination when the endpoint repeats the same page.
    payload = fixture_bytes("funding_history_page_1.json")
    adapter = make_adapter(
        lambda request: response(payload), wall_times=[RECEIVED_1, RECEIVED_2]
    )

    with pytest.raises(PaginationStalledError, match="progress"):
        asyncio.run(adapter.fetch_funding_history(Asset.BTC, START, END, REQUEST_CONTEXT))


def test_fetch_order_books_uses_l2_contract_and_preserves_counts_and_timestamp() -> None:
    # Catches side inversion, dropped order counts, sequence invention, or wrong L2 request shape.
    payload = fixture_bytes("l2_book.json")
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(decode_request(request))
        return response(payload)

    adapter = make_adapter(handler, wall_times=[RECEIVED_1], monotonic_times=[25, 75])
    batch = asyncio.run(
        adapter.fetch_order_books(frozenset({Asset.BTC}), REQUEST_CONTEXT, CYCLE_ID)
    )

    assert requests == [{"type": "l2Book", "coin": "BTC", "nSigFigs": None}]
    assert len(batch.raw) == 1
    assert batch.raw[0].payload_json == payload.decode()
    book = batch.normalized[0]
    assert book.symbol == "BTC"
    assert book.asset is Asset.BTC
    assert book.cycle_id == CYCLE_ID
    assert book.depth_limit == 20
    assert book.sequence is None
    assert book.effective_at == datetime(2026, 8, 11, 13, 0, 0, 123000, tzinfo=UTC)
    assert book.observed_at == RECEIVED_1
    assert [level.price for level in book.bids] == [
        Decimal("118999.0"),
        Decimal("118998.0"),
        Decimal("118997.0"),
    ]
    assert [level.order_count for level in book.bids] == [7, 3, 11]
    assert [level.order_count for level in book.asks] == [5, 4, 9]
    assert book.source_hash == batch.raw[0].source_hash


def malformed_book(**overrides: Any) -> bytes:
    document = json.loads(fixture_bytes("l2_book.json"))
    document.update(overrides)
    return json.dumps(document).encode()


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (malformed_book(levels=[[], [{"px": "119001", "sz": "1", "n": 1}]]), "bids"),
        (malformed_book(levels=[[{"px": "118999", "sz": "1", "n": 1}], []]), "asks"),
        (
            malformed_book(
                levels=[
                    [{"px": "119002", "sz": "1", "n": 1}],
                    [{"px": "119001", "sz": "1", "n": 1}],
                ]
            ),
            "cross",
        ),
        (
            malformed_book(
                levels=[
                    [
                        {"px": "118999", "sz": "1", "n": 1},
                        {"px": "118999", "sz": "2", "n": 2},
                    ],
                    [{"px": "119001", "sz": "1", "n": 1}],
                ]
            ),
            "descending",
        ),
        (
            malformed_book(
                levels=[
                    [
                        {"px": "118998", "sz": "1", "n": 1},
                        {"px": "118999", "sz": "2", "n": 2},
                    ],
                    [{"px": "119001", "sz": "1", "n": 1}],
                ]
            ),
            "descending",
        ),
        (
            malformed_book(
                levels=[
                    [{"px": "118999", "sz": "1", "n": 1}],
                    [
                        {"px": "119002", "sz": "1", "n": 1},
                        {"px": "119001", "sz": "2", "n": 2},
                    ],
                ]
            ),
            "ascending",
        ),
        (
            malformed_book(
                levels=[
                    [{"px": str(119000 - index), "sz": "1", "n": 1} for index in range(21)],
                    [{"px": "119001", "sz": "1", "n": 1}],
                ]
            ),
            "20",
        ),
        (malformed_book(coin="ETH"), "coin"),
        (malformed_book(time=9999999999999), "receipt"),
        (malformed_book(levels="bad"), "must be a list"),
        (malformed_book(levels=[[], [], []]), "bid and ask"),
        (
            malformed_book(
                levels=[
                    [{"px": "not-a-number", "sz": "1", "n": 1}],
                    [{"px": "119001", "sz": "1", "n": 1}],
                ]
            ),
            "decimal",
        ),
        (
            malformed_book(
                levels=[
                    [{"px": "118999", "sz": "1"}],
                    [{"px": "119001", "sz": "1", "n": 1}],
                ]
            ),
            "required key 'n'",
        ),
        (
            malformed_book(
                levels=[
                    [{"px": "118999", "sz": "1", "n": "1"}],
                    [{"px": "119001", "sz": "1", "n": 1}],
                ]
            ),
            "integer",
        ),
    ],
)
def test_fetch_order_books_fails_closed_on_malformed_payload(
    payload: bytes, message: str
) -> None:
    # Catches acceptance of structurally impossible or untrustworthy book evidence.
    adapter = make_adapter(lambda request: response(payload), wall_times=[RECEIVED_1])

    with pytest.raises(ValueError, match=message):
        asyncio.run(
            adapter.fetch_order_books(frozenset({Asset.BTC}), REQUEST_CONTEXT, CYCLE_ID)
        )


def test_fetch_market_snapshots_combines_aligned_context_with_public_l2_quote() -> None:
    # Catches the forbidden mark-as-bid/ask shortcut or a stubbed market snapshot method.
    meta = fixture_bytes("meta_and_asset_ctxs.json")
    book = fixture_bytes("l2_book.json")
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = decode_request(request)
        requests.append(body)
        return response(meta if body["type"] == "metaAndAssetCtxs" else book)

    adapter = make_adapter(handler, wall_times=[RECEIVED_1, RECEIVED_2])
    batch = asyncio.run(
        adapter.fetch_market_snapshots(frozenset({Asset.BTC}), REQUEST_CONTEXT)
    )

    assert requests == [
        {"type": "metaAndAssetCtxs"},
        {"type": "l2Book", "coin": "BTC", "nSigFigs": None},
    ]
    assert len(batch.raw) == 2
    assert batch.raw[0].payload_json == meta.decode()
    assert batch.raw[1].payload_json == book.decode()
    snapshot = batch.normalized[0]
    assert snapshot.bid == Decimal("118999.0")
    assert snapshot.ask == Decimal("119001.0")
    assert snapshot.mark == Decimal("119000.0")
    assert snapshot.index == Decimal("118995.0")
    assert snapshot.open_interest == Decimal("24567.89012")
    assert snapshot.effective_at == datetime(
        2026, 8, 11, 13, 0, 0, 123000, tzinfo=UTC
    )
    assert snapshot.observed_at == RECEIVED_2
    assert snapshot.source_hash == batch.raw[0].source_hash
    assert batch.raw[1].source_hash == hashlib.sha256(book).hexdigest()


def test_protocol_observed_at_must_be_aware_but_does_not_trigger_request() -> None:
    # Catches accepting ambiguous local time as collection context.
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return response(b"[]")

    adapter = make_adapter(handler, wall_times=[])

    with pytest.raises(ValueError, match="timezone-aware"):
        asyncio.run(
            adapter.fetch_instruments(
                frozenset({Asset.BTC}), datetime(2026, 8, 12, 10, 0, 0)
            )
        )
    assert called is False


def test_response_receipt_clocks_wrap_request_and_wall_clock_follows_body() -> None:
    # Catches moving the monotonic start/end or receipt wall clock outside the HTTP boundary.
    events: list[str] = []
    monotonic_values = iter([10, 25])

    def monotonic_ns() -> int:
        events.append("monotonic")
        return next(monotonic_values)

    def wall_clock() -> datetime:
        events.append("wall")
        return RECEIVED_1

    def handler(request: httpx.Request) -> httpx.Response:
        events.append("response-body")
        return response(b"[]")

    adapter = HyperliquidPublicAdapter(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        wall_clock=wall_clock,
        monotonic_ns=monotonic_ns,
    )

    batch = asyncio.run(
        adapter.fetch_funding_history(Asset.BTC, START, END, REQUEST_CONTEXT)
    )

    assert events == ["monotonic", "response-body", "monotonic", "wall"]
    assert batch.raw[0].received_monotonic_ns == 25
    assert batch.raw[0].observed_at == RECEIVED_1
