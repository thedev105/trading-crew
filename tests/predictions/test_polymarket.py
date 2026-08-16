import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
from pydantic import ValidationError

from polytrading.predictions.adapter import PredictionCollectionGateError
from polytrading.predictions.domain import (
    MarketRecord,
    PredictionBookSnapshot,
    PredictionFeeRate,
    PredictionVenue,
    RuleVersion,
    TradeRecord,
)
from polytrading.predictions.manifest import AdapterImplementationState
from polytrading.predictions.polymarket import (
    PaginationStalledError,
    PolymarketAdapter,
    PolymarketBookContinuity,
)
from tests.predictions.manifest_helpers import venue_manifest

FIXTURES = Path("tests/fixtures/predictions/polymarket")
NOW = datetime(2026, 8, 16, 12, tzinfo=UTC)
CYCLE_ID = UUID("00000000-0000-0000-0000-000000002001")


class SequenceClock:
    def __init__(self, values: list[Any]) -> None:
        self._values = iter(values)

    def __call__(self) -> Any:
        return next(self._values)


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def response(payload: bytes, status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code, content=payload, headers={"content-type": "application/json"}
    )


def make_adapter(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    wall_times: list[datetime] | None = None,
    monotonic_times: list[int] | None = None,
    max_market_pages: int = 1000,
) -> PolymarketAdapter:
    return PolymarketAdapter(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        wall_clock=SequenceClock(wall_times or [NOW] * 20),
        monotonic_ns=SequenceClock(monotonic_times or list(range(100, 20_000, 100))),
        max_market_pages=max_market_pages,
    )


def empty_markets_page(request: httpx.Request) -> httpx.Response:
    return response(b"[]")


def test_fetch_markets_normalizes_gamma_page_into_market_and_rule_version() -> None:
    pages = iter([fixture_bytes("gamma_markets_page_1.json"), b"[]"])

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/markets"
        return response(next(pages))

    adapter = make_adapter(handler)
    batch = asyncio.run(adapter.fetch_markets(information_cutoff=NOW))

    markets = [item for item in batch.normalized if isinstance(item, MarketRecord)]
    rule_versions = [item for item in batch.normalized if isinstance(item, RuleVersion)]
    assert len(markets) == 2
    assert len(rule_versions) == 2

    market = markets[0]
    assert market.venue is PredictionVenue.POLYMARKET
    assert market.market_id == "0xa467b14d51f01b957109d9cbb1d6c124fab2a089d52ed8f471d23c2812e743b7"
    assert market.outcomes == ("Yes", "No")
    assert market.outcome_token_ids is not None
    assert len(market.outcome_token_ids) == 2
    assert market.negative_risk is False
    assert market.order_book_enabled is True

    rule_version = next(item for item in rule_versions if item.market_id == market.market_id)
    assert rule_version.market_id == market.market_id
    assert rule_version.outcomes == market.outcomes


def test_fetch_markets_stops_pagination_on_a_short_page() -> None:
    pages = iter([fixture_bytes("gamma_markets_page_1.json")])
    requests: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(dict(request.url.params))
        return response(next(pages))

    adapter = make_adapter(handler)
    asyncio.run(adapter.fetch_markets(information_cutoff=NOW))

    assert len(requests) == 1
    assert requests[0]["offset"] == "0"


def test_fetch_markets_rejects_duplicate_condition_ids_on_one_page() -> None:
    document = json.loads(fixture_bytes("gamma_markets_page_1.json"))
    document[1]["conditionId"] = document[0]["conditionId"]
    payload = json.dumps(document).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        return response(payload)

    adapter = make_adapter(handler)
    with pytest.raises(ValueError, match="duplicate conditionId"):
        asyncio.run(adapter.fetch_markets(information_cutoff=NOW))


def test_fetch_markets_rejects_mismatched_token_and_outcome_counts() -> None:
    document = json.loads(fixture_bytes("gamma_markets_page_1.json"))
    document[0]["clobTokenIds"] = json.dumps(["only-one-token"])
    payload = json.dumps(document).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        return response(payload)

    adapter = make_adapter(handler)
    with pytest.raises((ValueError, ValidationError), match="align"):
        asyncio.run(adapter.fetch_markets(information_cutoff=NOW))


def test_fetch_markets_rejects_malformed_inner_json_array() -> None:
    document = json.loads(fixture_bytes("gamma_markets_page_1.json"))
    document[0]["outcomes"] = "not-json"
    payload = json.dumps(document).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        return response(payload)

    adapter = make_adapter(handler)
    with pytest.raises(ValueError, match="not valid JSON"):
        asyncio.run(adapter.fetch_markets(information_cutoff=NOW))


def test_fetch_markets_pagination_terminates_within_bound() -> None:
    template = json.loads(fixture_bytes("gamma_markets_page_1.json"))[0]

    def full_page(offset: int) -> bytes:
        page = []
        for index in range(500):
            row = dict(template)
            row["conditionId"] = f"0x{offset + index:064x}"
            row["clobTokenIds"] = json.dumps(
                [f"token-{offset + index}-a", f"token-{offset + index}-b"]
            )
            page.append(row)
        return json.dumps(page).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params["offset"])
        return response(full_page(offset))

    adapter = make_adapter(
        handler,
        wall_times=[NOW] * 20,
        monotonic_times=list(range(100, 20_000, 100)),
        max_market_pages=3,
    )
    with pytest.raises(PaginationStalledError):
        asyncio.run(adapter.fetch_markets(information_cutoff=NOW))


def test_fetch_book_snapshot_reverses_venue_sides_into_canonical_order() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/book"
        return response(fixture_bytes("clob_book.json"))

    adapter = make_adapter(handler)
    batch = asyncio.run(
        adapter.fetch_book_snapshot(
            "0xa467b14d51f01b957109d9cbb1d6c124fab2a089d52ed8f471d23c2812e743b7",
            "32338220190071351435772801779725302244575775216413325951443816017994629993401",
            NOW,
            CYCLE_ID,
        )
    )

    book = next(item for item in batch.normalized if isinstance(item, PredictionBookSnapshot))
    assert book.bids[0].price > book.bids[-1].price
    assert book.asks[0].price < book.asks[-1].price
    assert book.bids[0].price < book.asks[0].price
    assert book.sequence == "b7348d8ace6fccaf60b998d819e338ae41e5f55d"


def test_fetch_book_snapshot_requires_an_outcome_token() -> None:
    adapter = make_adapter(empty_markets_page)
    with pytest.raises(ValueError, match="outcome token"):
        asyncio.run(adapter.fetch_book_snapshot("market", None, NOW, CYCLE_ID))


def test_fetch_fee_rate_derives_maker_zero_and_documented_taker_conversion() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/fee-rate"
        return response(fixture_bytes("fee_rate.json"))

    adapter = make_adapter(handler)
    batch = asyncio.run(adapter.fetch_fee_rate("some-token-id", NOW))

    fee = next(item for item in batch.normalized if isinstance(item, PredictionFeeRate))
    assert fee.maker_rate == Decimal(0)
    assert fee.taker_rate == Decimal("0.1")
    assert any(
        warning.code == "POLYMARKET_FEE_RATE_IS_FLAT_REFERENCE_NOT_APPLIED_CURVE"
        for warning in batch.warnings
    )


def test_fetch_fee_rate_requires_a_market_identifier() -> None:
    adapter = make_adapter(empty_markets_page)
    with pytest.raises(ValueError, match="requires a token/market identifier"):
        asyncio.run(adapter.fetch_fee_rate(None, NOW))


def test_fetch_trades_filters_by_market_and_range() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/trades"
        return response(fixture_bytes("data_trades.json"))

    adapter = make_adapter(handler)
    batch = asyncio.run(
        adapter.fetch_trades(
            "0xa467b14d51f01b957109d9cbb1d6c124fab2a089d52ed8f471d23c2812e743b7",
            datetime(2026, 8, 12, tzinfo=UTC),
            NOW,
            NOW,
        )
    )

    trades = [item for item in batch.normalized if isinstance(item, TradeRecord)]
    assert len(trades) == 3
    assert {trade.side for trade in trades} == {"buy", "sell"}


def test_fetch_trades_rejects_a_trade_timestamp_after_receipt() -> None:
    document = json.loads(fixture_bytes("data_trades.json"))
    future = int(NOW.timestamp()) + 3600
    document[0]["timestamp"] = future

    def handler(request: httpx.Request) -> httpx.Response:
        return response(json.dumps(document).encode())

    adapter = make_adapter(handler)
    with pytest.raises(ValueError, match="after response receipt"):
        asyncio.run(
            adapter.fetch_trades(
                document[0]["conditionId"],
                datetime(2026, 8, 12, tzinfo=UTC),
                NOW,
                NOW,
            )
        )


def test_fetch_trades_rejects_end_before_start() -> None:
    adapter = make_adapter(empty_markets_page)
    with pytest.raises(ValueError, match="must not precede start"):
        asyncio.run(adapter.fetch_trades("market", NOW, NOW - timedelta(hours=1), NOW))


def test_fetch_manifest_gated_rejects_watchlisted_venue_before_any_request() -> None:
    def reject_network(request: httpx.Request) -> httpx.Response:
        raise AssertionError("gated collection must not open a network request")

    adapter = make_adapter(reject_network)
    manifest = venue_manifest(implementation_state=AdapterImplementationState.WATCHLIST)
    with pytest.raises(PredictionCollectionGateError):
        asyncio.run(adapter.fetch_manifest_gated(manifest))


def test_fetch_manifest_gated_allows_read_only_venue() -> None:
    adapter = make_adapter(empty_markets_page)
    manifest = venue_manifest(implementation_state=AdapterImplementationState.READ_ONLY)
    asyncio.run(adapter.fetch_manifest_gated(manifest))


def test_book_continuity_tracks_full_snapshot_events() -> None:
    continuity = PolymarketBookContinuity()
    assert continuity.is_valid is False

    continuity.handle_message(
        {"event_type": "book", "asset_id": "token-1", "hash": "hash-a"},
        outcome_token_id="token-1",
    )
    assert continuity.is_valid is True
    assert continuity.latest_hash == "hash-a"


def test_book_continuity_ignores_events_for_a_different_asset() -> None:
    continuity = PolymarketBookContinuity()
    continuity.handle_message(
        {"event_type": "book", "asset_id": "other-token", "hash": "hash-a"},
        outcome_token_id="token-1",
    )
    assert continuity.is_valid is False


def test_book_continuity_invalidates_on_price_change_and_tick_size_change() -> None:
    continuity = PolymarketBookContinuity()
    continuity.handle_message(
        {"event_type": "book", "asset_id": "token-1", "hash": "hash-a"},
        outcome_token_id="token-1",
    )
    continuity.handle_message({"event_type": "price_change"}, outcome_token_id="token-1")
    assert continuity.is_valid is False

    continuity.handle_message(
        {"event_type": "book", "asset_id": "token-1", "hash": "hash-b"},
        outcome_token_id="token-1",
    )
    continuity.handle_message({"event_type": "tick_size_change"}, outcome_token_id="token-1")
    assert continuity.is_valid is False


def test_book_continuity_rejects_unknown_event_type() -> None:
    continuity = PolymarketBookContinuity()
    with pytest.raises(ValueError, match="unsupported market channel event_type"):
        continuity.handle_message({"event_type": "unknown"}, outcome_token_id="token-1")


def test_book_continuity_reconciles_with_rest_hash() -> None:
    continuity = PolymarketBookContinuity()
    continuity.handle_message(
        {"event_type": "book", "asset_id": "token-1", "hash": "hash-a"},
        outcome_token_id="token-1",
    )

    assert continuity.reconcile_with_rest("hash-a") is True
    assert continuity.is_valid is True

    assert continuity.reconcile_with_rest("hash-b") is False
    assert continuity.is_valid is False


def test_book_continuity_reconcile_fails_closed_without_prior_state() -> None:
    continuity = PolymarketBookContinuity()
    assert continuity.reconcile_with_rest("anything") is False
