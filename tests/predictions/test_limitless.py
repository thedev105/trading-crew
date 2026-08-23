import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from polytrading.predictions.adapter import (
    PredictionCollectionGateError,
    validate_prediction_adapter_batch,
)
from polytrading.predictions.domain import MarketRecord, PredictionVenue, RuleVersion
from polytrading.predictions.limitless import LimitlessAdapter, PaginationStalledError
from polytrading.predictions.manifest import AdapterImplementationState
from tests.predictions.manifest_helpers import venue_manifest

FIXTURES = Path("tests/fixtures/predictions/limitless")
NOW = datetime(2026, 8, 16, 12, tzinfo=UTC)
CYCLE_ID = UUID("00000000-0000-0000-0000-000000004001")


class SequenceClock:
    def __init__(self, values: list[Any]) -> None:
        self._values = iter(values)

    def __call__(self) -> Any:
        return next(self._values)


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def fixture_document() -> dict[str, Any]:
    return json.loads(fixture_bytes("markets_active_page_1.json"))


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
) -> LimitlessAdapter:
    return LimitlessAdapter(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        utc_now=SequenceClock(wall_times or [NOW] * 20),
        monotonic_ns=SequenceClock(monotonic_times or list(range(100, 20_000, 100))),
        max_market_pages=max_market_pages,
    )


def empty_markets_page(request: httpx.Request) -> httpx.Response:
    return response(b'{"data": [], "totalMarketsCount": 0}')


def test_fetch_markets_persists_raw_before_normalized_lineage() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/markets/active"
        return response(fixture_bytes("markets_active_page_1.json"))

    adapter = make_adapter(handler)
    batch = asyncio.run(adapter.fetch_markets(information_cutoff=NOW))

    validate_prediction_adapter_batch(batch)
    assert all(record.venue is PredictionVenue.LIMITLESS for record in batch.raw)
    markets = [item for item in batch.normalized if isinstance(item, MarketRecord)]
    rule_versions = [item for item in batch.normalized if isinstance(item, RuleVersion)]
    assert len(markets) == 2
    assert len(rule_versions) == 2


def test_clob_market_is_order_book_enabled_and_negative_risk_round_trips() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return response(fixture_bytes("markets_active_page_1.json"))

    adapter = make_adapter(handler)
    batch = asyncio.run(adapter.fetch_markets(information_cutoff=NOW))

    clob = next(
        item
        for item in batch.normalized
        if isinstance(item, MarketRecord) and item.market_id == "clob-market-1"
    )
    assert clob.order_book_enabled is True
    assert clob.outcomes == ("Yes", "No")
    assert clob.outcome_token_ids is not None
    assert len(clob.outcome_token_ids) == 2
    assert clob.negative_risk is True
    assert clob.event_id == "negrisk-group-42"
    assert clob.restricted is None
    clob_warning_codes = {w.code for w in batch.warnings if w.market_id == "clob-market-1"}
    assert clob_warning_codes == {"limitless_restricted_unknown"}


def test_restricted_is_unknown_not_defaulted_and_warns_for_every_market() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return response(fixture_bytes("markets_active_page_1.json"))

    adapter = make_adapter(handler)
    batch = asyncio.run(adapter.fetch_markets(information_cutoff=NOW))

    markets = [item for item in batch.normalized if isinstance(item, MarketRecord)]
    assert markets
    assert all(market.restricted is None for market in markets)
    for market in markets:
        assert any(
            w.code == "limitless_restricted_unknown" and w.market_id == market.market_id
            for w in batch.warnings
        )


def test_amm_market_is_not_order_book_enabled_and_warns() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return response(fixture_bytes("markets_active_page_1.json"))

    adapter = make_adapter(handler)
    batch = asyncio.run(adapter.fetch_markets(information_cutoff=NOW))

    amm = next(
        m for m in batch.normalized if isinstance(m, MarketRecord) and m.market_id == "amm-market-1"
    )
    assert amm.order_book_enabled is False
    assert any(
        w.code == "limitless_amm_market" and w.market_id == "amm-market-1" for w in batch.warnings
    )


def test_incomplete_market_is_skipped_with_warning_not_defaulted() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return response(fixture_bytes("markets_active_page_1.json"))

    adapter = make_adapter(handler)
    batch = asyncio.run(adapter.fetch_markets(information_cutoff=NOW))

    market_ids = {item.market_id for item in batch.normalized if isinstance(item, MarketRecord)}
    assert len(market_ids) == 2
    assert any(w.code == "limitless_market_incomplete" for w in batch.warnings)


def test_book_trade_fee_endpoints_are_explicitly_not_collected() -> None:
    adapter = make_adapter(empty_markets_page)

    with pytest.raises(NotImplementedError, match="limitless_endpoint_not_collected"):
        asyncio.run(adapter.fetch_book_snapshot("m", None, NOW, uuid4()))
    with pytest.raises(NotImplementedError, match="limitless_endpoint_not_collected"):
        asyncio.run(adapter.fetch_trades("m", NOW, NOW, NOW))
    with pytest.raises(NotImplementedError, match="limitless_endpoint_not_collected"):
        asyncio.run(adapter.fetch_fee_rate("m", NOW))


def test_fetch_markets_pagination_follows_to_a_second_page_and_stops() -> None:
    document = fixture_document()
    template = document["data"][0]

    def full_page(page: int) -> bytes:
        rows = []
        for index in range(25):
            row = dict(template)
            row["conditionId"] = f"clob-market-page{page}-{index}"
            rows.append(row)
        return json.dumps({"data": rows, "totalMarketsCount": 26}).encode()

    requests: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(dict(request.url.params))
        page = int(request.url.params["page"])
        if page == 1:
            return response(full_page(1))
        return response(json.dumps({"data": [template], "totalMarketsCount": 26}).encode())

    adapter = make_adapter(
        handler, wall_times=[NOW] * 60, monotonic_times=list(range(100, 60_000, 100))
    )
    batch = asyncio.run(adapter.fetch_markets(information_cutoff=NOW))

    assert [request["page"] for request in requests] == ["1", "2"]
    assert len(batch.raw) == 2


def test_fetch_markets_pagination_terminates_within_bound() -> None:
    template = fixture_document()["data"][0]

    def full_page(offset: int) -> bytes:
        rows = []
        for index in range(25):
            row = dict(template)
            row["conditionId"] = f"clob-market-{offset + index}"
            rows.append(row)
        return json.dumps({"data": rows, "totalMarketsCount": 10_000}).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        return response(full_page(page * 25))

    adapter = make_adapter(
        handler,
        wall_times=[NOW] * 20,
        monotonic_times=list(range(100, 20_000, 100)),
        max_market_pages=3,
    )
    with pytest.raises(PaginationStalledError):
        asyncio.run(adapter.fetch_markets(information_cutoff=NOW))


def test_fetch_markets_rejects_malformed_json_with_no_partial_output() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return response(b"not json at all")

    adapter = make_adapter(handler)
    with pytest.raises(Exception):  # noqa: B017 - malformed body must fail closed
        asyncio.run(adapter.fetch_markets(information_cutoff=NOW))


def test_fetch_markets_rejects_duplicate_identifiers_on_one_page() -> None:
    document = fixture_document()
    document["data"][1]["conditionId"] = document["data"][0]["conditionId"]

    def handler(request: httpx.Request) -> httpx.Response:
        return response(json.dumps(document).encode())

    adapter = make_adapter(handler)
    with pytest.raises(ValueError, match="duplicate"):
        asyncio.run(adapter.fetch_markets(information_cutoff=NOW))


def test_fetch_manifest_gated_rejects_watchlisted_venue_before_any_request() -> None:
    def reject_network(request: httpx.Request) -> httpx.Response:
        raise AssertionError("gated collection must not open a network request")

    adapter = make_adapter(reject_network)
    manifest = venue_manifest(
        venue=PredictionVenue.LIMITLESS,
        implementation_state=AdapterImplementationState.WATCHLIST,
    )
    with pytest.raises(PredictionCollectionGateError):
        asyncio.run(adapter.fetch_manifest_gated(manifest))


def test_fetch_manifest_gated_allows_read_only_venue() -> None:
    adapter = make_adapter(empty_markets_page)
    manifest = venue_manifest(
        venue=PredictionVenue.LIMITLESS,
        implementation_state=AdapterImplementationState.READ_ONLY,
    )
    asyncio.run(adapter.fetch_manifest_gated(manifest))
