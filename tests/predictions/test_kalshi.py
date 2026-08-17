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

from polytrading.predictions.adapter import PredictionCollectionGateError
from polytrading.predictions.domain import (
    MarketRecord,
    PredictionBookSnapshot,
    PredictionVenue,
    RuleVersion,
    TradeRecord,
)
from polytrading.predictions.kalshi import KalshiAdapter, PaginationStalledError
from polytrading.predictions.manifest import AdapterImplementationState
from tests.predictions.manifest_helpers import venue_manifest

FIXTURES = Path("tests/fixtures/predictions/kalshi")
NOW = datetime(2026, 8, 16, 12, tzinfo=UTC)
CYCLE_ID = UUID("00000000-0000-0000-0000-000000003001")


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


async def _no_pause(_seconds: float) -> None:
    return None


def make_adapter(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    wall_times: list[datetime] | None = None,
    monotonic_times: list[int] | None = None,
    max_pages: int = 1000,
) -> KalshiAdapter:
    return KalshiAdapter(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        wall_clock=SequenceClock(wall_times or [NOW] * 40),
        monotonic_ns=SequenceClock(monotonic_times or list(range(100, 40_000, 100))),
        max_pages=max_pages,
        sleep=_no_pause,
    )


def empty_response(request: httpx.Request) -> httpx.Response:
    return response(b'{"cursor": "", "markets": []}')


def test_fetch_markets_normalizes_market_row_into_market_and_rule_version() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/markets")
        return response(fixture_bytes("markets_page_1.json"))

    adapter = make_adapter(handler)
    batch = asyncio.run(adapter.fetch_markets(information_cutoff=NOW))

    markets = [item for item in batch.normalized if isinstance(item, MarketRecord)]
    rule_versions = [item for item in batch.normalized if isinstance(item, RuleVersion)]
    assert len(markets) == 2
    assert len(rule_versions) == 2

    market = markets[0]
    assert market.venue is PredictionVenue.KALSHI
    assert market.market_id == "KXHIGHNY-26AUG16-T85"
    assert market.outcomes == ("yes", "no")
    assert market.outcome_token_ids is None
    assert market.negative_risk is None
    assert market.active is True
    assert market.closed is False

    rule_version = next(item for item in rule_versions if item.market_id == market.market_id)
    assert "New York City" in rule_version.description


def test_fetch_markets_status_maps_to_active_and_closed_flags() -> None:
    document = json.loads(fixture_bytes("markets_page_1.json"))
    document["markets"][0]["status"] = "finalized"

    def handler(request: httpx.Request) -> httpx.Response:
        return response(json.dumps(document).encode())

    adapter = make_adapter(handler)
    batch = asyncio.run(adapter.fetch_markets(information_cutoff=NOW))
    market = next(
        item
        for item in batch.normalized
        if isinstance(item, MarketRecord) and item.market_id == document["markets"][0]["ticker"]
    )
    assert market.active is False
    assert market.closed is True


def test_fetch_markets_falls_back_to_subtitle_when_title_is_null() -> None:
    document = json.loads(fixture_bytes("markets_page_1.json"))
    document["markets"][0]["title"] = None
    document["markets"][0]["subtitle"] = "Kimi:: Moonshot"

    def handler(request: httpx.Request) -> httpx.Response:
        return response(json.dumps(document).encode())

    adapter = make_adapter(handler)
    batch = asyncio.run(adapter.fetch_markets(information_cutoff=NOW))
    market = next(
        item
        for item in batch.normalized
        if isinstance(item, MarketRecord) and item.market_id == document["markets"][0]["ticker"]
    )
    assert market.question == "Kimi:: Moonshot"


def test_fetch_markets_paginates_via_cursor_and_stops_on_empty_cursor() -> None:
    page_one = json.loads(fixture_bytes("markets_page_1.json"))
    page_one["cursor"] = "next-page-token"
    requests: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(dict(request.url.params))
        if "cursor" not in request.url.params:
            return response(json.dumps(page_one).encode())
        return response(b'{"cursor": "", "markets": []}')

    adapter = make_adapter(handler)
    asyncio.run(adapter.fetch_markets(information_cutoff=NOW))

    assert len(requests) == 2
    assert requests[1]["cursor"] == "next-page-token"


def test_fetch_markets_requests_open_status_and_excludes_multivariate_events() -> None:
    requests: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(dict(request.url.params))
        return response(fixture_bytes("markets_page_1.json"))

    adapter = make_adapter(handler)
    asyncio.run(adapter.fetch_markets(information_cutoff=NOW))

    assert requests[0]["status"] == "open"
    assert requests[0]["mve_filter"] == "exclude"


def test_fetch_markets_skips_combinatorial_rows_but_keeps_the_raw_page() -> None:
    document = json.loads(fixture_bytes("markets_page_1.json"))
    document["markets"][0]["mve_collection_ticker"] = "KXMVECROSSCATEGORY-SHARD1-R"

    def handler(request: httpx.Request) -> httpx.Response:
        return response(json.dumps(document).encode())

    adapter = make_adapter(handler)
    batch = asyncio.run(adapter.fetch_markets(information_cutoff=NOW))

    market_ids = {item.market_id for item in batch.normalized if isinstance(item, MarketRecord)}
    assert document["markets"][0]["ticker"] not in market_ids
    assert document["markets"][1]["ticker"] in market_ids
    assert len(batch.raw) == 1
    assert document["markets"][0]["ticker"] in batch.raw[0].payload_json


def test_fetch_markets_pauses_between_pages() -> None:
    page_one = json.loads(fixture_bytes("markets_page_1.json"))
    page_one["cursor"] = "next-page-token"
    pauses: list[float] = []

    async def recording_pause(seconds: float) -> None:
        pauses.append(seconds)

    def handler(request: httpx.Request) -> httpx.Response:
        if "cursor" not in request.url.params:
            return response(json.dumps(page_one).encode())
        return response(b'{"cursor": "", "markets": []}')

    adapter = KalshiAdapter(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        wall_clock=SequenceClock([NOW] * 40),
        monotonic_ns=SequenceClock(list(range(100, 40_000, 100))),
        sleep=recording_pause,
    )
    asyncio.run(adapter.fetch_markets(information_cutoff=NOW))

    assert pauses == [adapter._page_pause_seconds]


def test_fetch_markets_rejects_duplicate_tickers() -> None:
    document = json.loads(fixture_bytes("markets_page_1.json"))
    document["markets"][1]["ticker"] = document["markets"][0]["ticker"]

    def handler(request: httpx.Request) -> httpx.Response:
        return response(json.dumps(document).encode())

    adapter = make_adapter(handler)
    with pytest.raises(ValueError, match="duplicate ticker"):
        asyncio.run(adapter.fetch_markets(information_cutoff=NOW))


def test_fetch_markets_pagination_terminates_within_bound() -> None:
    call_count = {"n": 0}

    def counting_handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        document = json.loads(fixture_bytes("markets_page_1.json"))
        document["cursor"] = f"cursor-{call_count['n']}"
        for row in document["markets"]:
            row["ticker"] = f"{row['ticker']}-{call_count['n']}"
        return response(json.dumps(document).encode())

    adapter = make_adapter(
        counting_handler,
        wall_times=[NOW] * 20,
        monotonic_times=list(range(100, 20_000, 100)),
        max_pages=3,
    )
    with pytest.raises(PaginationStalledError):
        asyncio.run(adapter.fetch_markets(information_cutoff=NOW))


def test_fetch_book_snapshot_derives_asks_from_the_opposite_sides_bids() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/orderbook")
        return response(fixture_bytes("orderbook.json"))

    adapter = make_adapter(handler)
    batch = asyncio.run(adapter.fetch_book_snapshot("KXHIGHNY-26AUG16-T78", "yes", NOW, CYCLE_ID))

    book = next(item for item in batch.normalized if isinstance(item, PredictionBookSnapshot))
    assert book.outcome_token_id == "yes"
    assert book.bids[0].price == Decimal("0.05")
    assert book.asks[0].price == Decimal("0.95")
    assert book.bids[0].price < book.asks[0].price


def test_fetch_book_snapshot_no_side_mirrors_yes_side() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return response(fixture_bytes("orderbook.json"))

    adapter = make_adapter(handler)
    batch = asyncio.run(adapter.fetch_book_snapshot("KXHIGHNY-26AUG16-T78", "no", NOW, CYCLE_ID))
    book = next(item for item in batch.normalized if isinstance(item, PredictionBookSnapshot))
    assert book.outcome_token_id == "no"
    assert book.bids[0].price < book.asks[0].price


def test_fetch_book_snapshot_rejects_an_invalid_outcome_token() -> None:
    adapter = make_adapter(empty_response)
    with pytest.raises(ValueError, match="'yes' or 'no'"):
        asyncio.run(adapter.fetch_book_snapshot("market", "maybe", NOW, CYCLE_ID))


def test_fetch_trades_routes_entirely_historical_before_the_cutoff() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/historical/cutoff"):
            return response(fixture_bytes("historical_cutoff.json"))
        if request.url.path.endswith("/historical/trades"):
            return response(fixture_bytes("trades.json"))
        raise AssertionError(f"unexpected endpoint called: {request.url.path}")

    adapter = make_adapter(handler)
    cutoff = json.loads(fixture_bytes("historical_cutoff.json"))["trades_created_ts"]
    cutoff_dt = datetime.fromisoformat(cutoff.replace("Z", "+00:00"))
    batch = asyncio.run(
        adapter.fetch_trades(
            "KXHIGHNY-26AUG16-T85",
            cutoff_dt - timedelta(days=10),
            cutoff_dt - timedelta(days=5),
            cutoff_dt,
        )
    )
    assert any(path.endswith("/historical/trades") for path in calls)
    assert not any(path.endswith("/markets/trades") for path in calls)
    trades = [item for item in batch.normalized if isinstance(item, TradeRecord)]
    assert all(trade.effective_at <= cutoff_dt - timedelta(days=5) for trade in trades)


def test_fetch_trades_routes_entirely_live_after_the_cutoff() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/historical/cutoff"):
            return response(fixture_bytes("historical_cutoff.json"))
        if request.url.path.endswith("/markets/trades"):
            return response(fixture_bytes("trades.json"))
        raise AssertionError(f"unexpected endpoint called: {request.url.path}")

    adapter = make_adapter(handler)
    cutoff = json.loads(fixture_bytes("historical_cutoff.json"))["trades_created_ts"]
    cutoff_dt = datetime.fromisoformat(cutoff.replace("Z", "+00:00"))
    asyncio.run(
        adapter.fetch_trades(
            "KXHIGHNY-26AUG16-T85",
            cutoff_dt + timedelta(days=1),
            NOW,
            NOW,
        )
    )
    assert any(path.endswith("/markets/trades") for path in calls)
    assert not any(path.endswith("/historical/trades") for path in calls)


def test_fetch_trades_spanning_the_cutoff_queries_both_partitions_without_duplication() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/historical/cutoff"):
            return response(fixture_bytes("historical_cutoff.json"))
        return response(fixture_bytes("trades.json"))

    adapter = make_adapter(handler)
    cutoff = json.loads(fixture_bytes("historical_cutoff.json"))["trades_created_ts"]
    cutoff_dt = datetime.fromisoformat(cutoff.replace("Z", "+00:00"))
    batch = asyncio.run(
        adapter.fetch_trades(
            "KXHIGHNY-26AUG16-T85",
            cutoff_dt - timedelta(days=5),
            cutoff_dt + timedelta(days=5),
            cutoff_dt + timedelta(days=5),
        )
    )
    assert any(path.endswith("/historical/trades") for path in calls)
    assert any(path.endswith("/markets/trades") for path in calls)
    trades = [item for item in batch.normalized if isinstance(item, TradeRecord)]
    trade_ids = [trade.trade_id for trade in trades]
    assert len(trade_ids) == len(set(trade_ids))


def test_fetch_trades_rejects_end_before_start() -> None:
    adapter = make_adapter(empty_response)
    with pytest.raises(ValueError, match="must not precede start"):
        asyncio.run(adapter.fetch_trades("market", NOW, NOW - timedelta(hours=1), NOW))


def test_fetch_fee_rate_returns_empty_batch_with_a_structured_warning() -> None:
    adapter = make_adapter(empty_response)
    batch = asyncio.run(adapter.fetch_fee_rate("KXHIGHNY-26AUG16-T85", NOW))

    assert batch.raw == ()
    assert batch.normalized == ()
    assert any(warning.code == "KALSHI_FEE_RATE_ENDPOINT_UNAVAILABLE" for warning in batch.warnings)


def test_fetch_manifest_gated_rejects_watchlisted_venue_before_any_request() -> None:
    def reject_network(request: httpx.Request) -> httpx.Response:
        raise AssertionError("gated collection must not open a network request")

    adapter = make_adapter(reject_network)
    manifest = venue_manifest(
        venue=PredictionVenue.KALSHI, implementation_state=AdapterImplementationState.WATCHLIST
    )
    with pytest.raises(PredictionCollectionGateError):
        asyncio.run(adapter.fetch_manifest_gated(manifest))


def test_fetch_manifest_gated_allows_read_only_venue() -> None:
    adapter = make_adapter(empty_response)
    manifest = venue_manifest(
        venue=PredictionVenue.KALSHI, implementation_state=AdapterImplementationState.READ_ONLY
    )
    asyncio.run(adapter.fetch_manifest_gated(manifest))
