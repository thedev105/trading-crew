from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

import httpx

from polytrading.predictions.adapter import (
    PredictionAdapterBatch,
    PredictionAdapterWarning,
    PredictionCollectionGateError,
)
from polytrading.predictions.domain import (
    MarketRecord,
    PredictionBookLevel,
    PredictionBookSnapshot,
    PredictionRawEnvelope,
    PredictionVenue,
    RuleVersion,
    TradeRecord,
    rule_relevant_version_id,
)
from polytrading.predictions.manifest import VenueManifest, evaluate_collection_gate

_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
_MARKETS_ENDPOINT = "/markets"
_HISTORICAL_CUTOFF_ENDPOINT = "/historical/cutoff"
_ORDERBOOK_ENDPOINT_TEMPLATE = "/markets/{ticker}/orderbook"
_TRADES_ENDPOINT = "/markets/trades"
_HISTORICAL_TRADES_ENDPOINT = "/historical/trades"
_SOURCE_VERSION = "trade-api-v2"
_PAGE_LIMIT = 1000  # Kalshi's documented maximum for GET /markets.
_MAX_PAGES = 1000
# Unpaced back-to-back large-page (limit=1000) requests against Kalshi's live public
# API were observed (2026-08-16) to progressively stall into read timeouts partway
# through an ~80-page sweep, consistent with server-side rate-limiting; this pause is a
# deliberate courtesy between successful page fetches, not a workaround for an error.
_PAGE_PAUSE_SECONDS = 0.5
# Kalshi's status *filter* values (unopened/open/paused/closed/settled, per its API
# reference) differ from the literal strings its /markets response body actually returns
# for the "status" field (initialized/active/inactive/determined/finalized, confirmed
# against the live API on 2026-08-16); "closed" and "settled" map to "determined" and
# "finalized" respectively, and neither response literal is ever the string "closed".
_CLOSED_STATUSES = frozenset({"determined", "finalized"})


class PaginationStalledError(RuntimeError):
    """Raised when a finite Kalshi cursor pagination loop cannot make progress."""


def _require_collection_context(observed_at: datetime) -> None:
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("collection context must use an aware UTC timestamp")


def _require_mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _require_list(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")
    return value


def _require_string(mapping: Mapping[str, object], key: str, label: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str):
        raise ValueError(f"{label} key {key!r} must be a string")
    return value


def _optional_string(mapping: Mapping[str, object], key: str) -> str | None:
    value = mapping.get(key)
    return value if isinstance(value, str) and value else None


def _require_decimal_string(value: object, label: str) -> Decimal:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a decimal string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise ValueError(f"{label} must be a valid decimal string") from error
    if not parsed.is_finite():
        raise ValueError(f"{label} must be finite")
    return parsed


def _parse_datetime(value: object, label: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty timestamp string")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _optional_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@dataclass(frozen=True)
class _ReceivedResponse:
    endpoint: str
    payload: bytes
    document: Any
    observed_at: datetime
    received_monotonic_ns: int
    latency_ms: Decimal

    def raw_envelope(self) -> PredictionRawEnvelope:
        payload_text = self.payload.decode("utf-8")
        return PredictionRawEnvelope(
            schema_version=1,
            event_id=uuid5(NAMESPACE_URL, f"kalshi:{self.endpoint}:{self.received_monotonic_ns}"),
            venue=PredictionVenue.KALSHI,
            endpoint=self.endpoint,
            venue_timestamp=None,
            observed_at=self.observed_at,
            received_monotonic_ns=self.received_monotonic_ns,
            request_latency_ms=self.latency_ms,
            source_version=_SOURCE_VERSION,
            payload_json=payload_text,
            source_hash=sha256(payload_text.encode("utf-8")).hexdigest(),
        )


@dataclass(frozen=True)
class _HistoricalCutoff:
    market_settled_ts: datetime
    trades_created_ts: datetime


class KalshiAdapter:
    venue = PredictionVenue.KALSHI

    def __init__(
        self,
        client: httpx.AsyncClient,
        wall_clock: Callable[[], datetime],
        monotonic_ns: Callable[[], int],
        *,
        max_pages: int = _MAX_PAGES,
        page_pause_seconds: float = _PAGE_PAUSE_SECONDS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if isinstance(max_pages, bool) or not isinstance(max_pages, int):
            raise TypeError("max_pages must be an integer")
        if max_pages <= 0:
            raise ValueError("max_pages must be positive")
        self._client = client
        self._wall_clock = wall_clock
        self._monotonic_ns = monotonic_ns
        self._max_pages = max_pages
        self._page_pause_seconds = page_pause_seconds
        self._sleep = sleep

    async def fetch_manifest_gated(self, manifest: VenueManifest) -> None:
        decision = evaluate_collection_gate(manifest, venue=self.venue)
        if not decision.allowed:
            raise PredictionCollectionGateError(decision.reason or "COLLECTION_NOT_PERMITTED")

    async def _get(self, endpoint: str, params: Mapping[str, object]) -> _ReceivedResponse:
        started_ns = self._monotonic_ns()
        response = await self._client.get(f"{_BASE_URL}{endpoint}", params=params)
        completed_ns = self._monotonic_ns()
        response.raise_for_status()
        observed_at = self._wall_clock()
        document = response.json()
        return _ReceivedResponse(
            endpoint=endpoint,
            payload=response.content,
            document=document,
            observed_at=observed_at,
            received_monotonic_ns=completed_ns,
            latency_ms=Decimal(completed_ns - started_ns) / Decimal(1_000_000),
        )

    async def _fetch_historical_cutoff(self) -> tuple[_HistoricalCutoff, PredictionRawEnvelope]:
        received = await self._get(_HISTORICAL_CUTOFF_ENDPOINT, {})
        raw = received.raw_envelope()
        document = _require_mapping(received.document, "historical cutoff response")
        cutoff = _HistoricalCutoff(
            market_settled_ts=_parse_datetime(
                document.get("market_settled_ts"), "market_settled_ts"
            ),
            trades_created_ts=_parse_datetime(
                document.get("trades_created_ts"), "trades_created_ts"
            ),
        )
        return cutoff, raw

    async def fetch_markets(self, *, information_cutoff: datetime) -> PredictionAdapterBatch:
        raws: list[PredictionRawEnvelope] = []
        normalized: list[MarketRecord | RuleVersion] = []
        seen_tickers: set[str] = set()
        cursor = ""
        for page_number in range(self._max_pages):
            if page_number > 0:
                await self._sleep(self._page_pause_seconds)
            # Kalshi's /markets with no status filter returns every market ever listed
            # (its entire history, including short-lived recurring markets), which is far
            # larger than _max_pages * _PAGE_LIMIT and would always stall pagination.
            # Scope each collection run to the currently open/tradeable universe; a
            # historical backfill of closed/settled markets is a separate, deliberate
            # operation, not this method's default. mve_filter=exclude drops
            # auto-generated combinatorial multivariate-event (parlay) markets
            # server-side: confirmed 2026-08-16 that these otherwise dominate the open
            # universe by several orders of magnitude (~300k+ open items vs. ~83k
            # non-combinatorial) and have no analog on other venues this system
            # compares against.
            params: dict[str, object] = {
                "limit": _PAGE_LIMIT,
                "status": "open",
                "mve_filter": "exclude",
            }
            if cursor:
                params["cursor"] = cursor
            received = await self._get(_MARKETS_ENDPOINT, params)
            raw = received.raw_envelope()
            raws.append(raw)
            document = _require_mapping(received.document, "markets response")
            rows = _require_list(document.get("markets"), "markets response markets")
            for value in rows:
                row = _require_mapping(value, "market row")
                if row.get("mve_collection_ticker"):
                    # Belt-and-suspenders against the mve_filter=exclude request param
                    # above, in case that server-side filter is ever incomplete. The
                    # raw page is still persisted above either way; only normalization
                    # is scoped to non-combinatorial markets.
                    continue
                ticker = _require_string(row, "ticker", "market row")
                if ticker in seen_tickers:
                    raise ValueError("markets page contains a duplicate ticker")
                seen_tickers.add(ticker)
                market, rule_version = _parse_market_row(
                    row, information_cutoff=information_cutoff, raw=raw
                )
                normalized.append(market)
                normalized.append(rule_version)
            cursor = document.get("cursor") or ""
            if not cursor or not rows:
                break
        else:
            raise PaginationStalledError("Kalshi markets pagination did not terminate")
        return PredictionAdapterBatch(raw=tuple(raws), normalized=tuple(normalized))

    async def fetch_book_snapshot(
        self,
        market_id: str,
        outcome_token_id: str | None,
        observed_at: datetime,
        cycle_id: UUID,
    ) -> PredictionAdapterBatch:
        _require_collection_context(observed_at)
        if outcome_token_id not in ("yes", "no"):
            raise ValueError("Kalshi book snapshots require an outcome_token_id of 'yes' or 'no'")
        received = await self._get(_ORDERBOOK_ENDPOINT_TEMPLATE.format(ticker=market_id), {})
        raw = received.raw_envelope()
        document = _require_mapping(received.document, "orderbook response")
        orderbook = _require_mapping(document.get("orderbook_fp"), "orderbook_fp")
        snapshot = _parse_orderbook(
            orderbook,
            market_id=market_id,
            outcome_token_id=outcome_token_id,
            cycle_id=cycle_id,
            observed_at=received.observed_at,
            source_hash=raw.source_hash,
        )
        return PredictionAdapterBatch(raw=(raw,), normalized=(snapshot,))

    async def fetch_trades(
        self, market_id: str, start: datetime, end: datetime, observed_at: datetime
    ) -> PredictionAdapterBatch:
        _require_collection_context(observed_at)
        if end < start:
            raise ValueError("trade range end must not precede start")
        cutoff, cutoff_raw = await self._fetch_historical_cutoff()
        raws: list[PredictionRawEnvelope] = [cutoff_raw]
        normalized: list[TradeRecord] = []
        # A trade created at or before trades_created_ts is only available from the
        # historical partition; a trade created after it is only available live. Routing by
        # this single documented boundary (rather than duplicating logic per range) is what
        # keeps the join from ever double-counting or dropping a boundary-adjacent trade.
        if start <= cutoff.trades_created_ts:
            historical_end = min(end, cutoff.trades_created_ts)
            raw, trades = await self._fetch_trades_page(
                _HISTORICAL_TRADES_ENDPOINT, market_id, start, historical_end
            )
            raws.append(raw)
            normalized.extend(trades)
        if end > cutoff.trades_created_ts:
            live_start = max(start, cutoff.trades_created_ts)
            raw, trades = await self._fetch_trades_page(
                _TRADES_ENDPOINT, market_id, live_start, end
            )
            raws.append(raw)
            normalized.extend(trades)
        return PredictionAdapterBatch(raw=tuple(raws), normalized=tuple(normalized))

    async def _fetch_trades_page(
        self, endpoint: str, market_id: str, start: datetime, end: datetime
    ) -> tuple[PredictionRawEnvelope, list[TradeRecord]]:
        received = await self._get(
            endpoint,
            {
                "ticker": market_id,
                "limit": _PAGE_LIMIT,
                "min_ts": int(start.timestamp()),
                "max_ts": int(end.timestamp()),
            },
        )
        raw = received.raw_envelope()
        document = _require_mapping(received.document, "trades response")
        rows = _require_list(document.get("trades"), "trades response trades")
        trades = [
            _parse_trade_row(
                _require_mapping(value, "trade row"),
                raw_source_hash=raw.source_hash,
                observed_at=received.observed_at,
            )
            for value in rows
        ]
        return raw, [trade for trade in trades if start <= trade.effective_at <= end]

    async def fetch_fee_rate(
        self, market_id: str | None, observed_at: datetime
    ) -> PredictionAdapterBatch:
        _require_collection_context(observed_at)
        # Kalshi documents a per-category fee schedule rather than exposing a live public
        # fee-rate endpoint; fabricating a rate here would misrepresent it as observed
        # evidence. Collection therefore records no fee-rate evidence and reports why.
        warning = PredictionAdapterWarning(
            code="KALSHI_FEE_RATE_ENDPOINT_UNAVAILABLE",
            venue=PredictionVenue.KALSHI,
            endpoint="",
            market_id=market_id or "",
            message=(
                "Kalshi documents fees as a published per-category schedule; no live public "
                "fee-rate endpoint exists to collect as point-in-time evidence"
            ),
        )
        return PredictionAdapterBatch(raw=(), normalized=(), warnings=(warning,))


def _parse_market_row(
    row: Mapping[str, object], *, information_cutoff: datetime, raw: PredictionRawEnvelope
) -> tuple[MarketRecord, RuleVersion]:
    ticker = _require_string(row, "ticker", "market row")
    # A minority of markets grouped under a multi-candidate event (e.g. "which model
    # will be top-ranked") carry the human-readable question on the event, not the
    # per-market row, leaving "title" null; "subtitle" carries the market-specific
    # candidate text in that case (confirmed 2026-08-16: 1 of ~79.8k open markets).
    title = _optional_string(row, "title") or _require_string(row, "subtitle", "market row")
    status = _require_string(row, "status", "market row")
    active = status == "active"
    closed = status in _CLOSED_STATUSES
    outcomes = ("yes", "no")
    end_at = _optional_datetime(row.get("close_time"))
    description_parts = [
        part
        for part in (row.get("rules_primary"), row.get("rules_secondary"))
        if isinstance(part, str) and part
    ]
    description = "\n\n".join(description_parts)
    rule_version_id = rule_relevant_version_id(
        PredictionVenue.KALSHI, ticker, title, description, None, outcomes, end_at
    )

    market = MarketRecord(
        schema_version=1,
        market_id=ticker,
        venue=PredictionVenue.KALSHI,
        underlying_exchange=None,
        event_id=_optional_string(row, "event_ticker"),
        question=title,
        slug=None,
        outcomes=outcomes,
        outcome_token_ids=None,
        negative_risk=None,
        active=active,
        closed=closed,
        restricted=False,
        order_book_enabled=True,
        start_at=_optional_datetime(row.get("open_time")),
        end_at=end_at,
        resolution_source=None,
        rule_version_id=rule_version_id,
        information_cutoff=information_cutoff,
        source_url=f"{_BASE_URL}{_MARKETS_ENDPOINT}",
        retrieved_at=information_cutoff,
        raw_hash=raw.source_hash,
        normalized_hash=raw.source_hash,
    )
    rule_version = RuleVersion(
        schema_version=1,
        rule_version_id=rule_version_id,
        market_id=ticker,
        venue=PredictionVenue.KALSHI,
        question=title,
        description=description,
        resolution_source=None,
        outcomes=outcomes,
        superseded_rule_version_id=None,
        effective_at=information_cutoff,
        source_hash=raw.source_hash,
    )
    return market, rule_version


def _parse_price_ladder(value: object, label: str) -> list[PredictionBookLevel]:
    rows = _require_list(value, label)
    levels: list[PredictionBookLevel] = []
    for entry in rows:
        pair = _require_list(entry, f"{label} entry")
        if len(pair) != 2:
            raise ValueError(f"{label} entry must be a [price, size] pair")
        levels.append(
            PredictionBookLevel(
                price=_require_decimal_string(pair[0], f"{label} price"),
                size=_require_decimal_string(pair[1], f"{label} size"),
            )
        )
    return levels


def _parse_orderbook(
    orderbook: Mapping[str, object],
    *,
    market_id: str,
    outcome_token_id: str,
    cycle_id: UUID,
    observed_at: datetime,
    source_hash: str,
) -> PredictionBookSnapshot:
    yes_bids = _parse_price_ladder(orderbook.get("yes_dollars") or [], "yes_dollars")
    no_bids = _parse_price_ladder(orderbook.get("no_dollars") or [], "no_dollars")
    # Kalshi exposes only resting bids on each side. A bid to buy the opposite outcome at
    # price p is equivalent to an offer to sell this outcome at (1 - p); ladders are stored
    # ascending by their own price, so the derived side must be reversed to stay ascending.
    if outcome_token_id == "yes":
        bids = sorted(yes_bids, key=lambda level: level.price, reverse=True)
        asks = sorted(
            (
                PredictionBookLevel(price=Decimal(1) - level.price, size=level.size)
                for level in no_bids
            ),
            key=lambda level: level.price,
        )
    else:
        bids = sorted(no_bids, key=lambda level: level.price, reverse=True)
        asks = sorted(
            (
                PredictionBookLevel(price=Decimal(1) - level.price, size=level.size)
                for level in yes_bids
            ),
            key=lambda level: level.price,
        )
    return PredictionBookSnapshot(
        schema_version=1,
        cycle_id=cycle_id,
        venue=PredictionVenue.KALSHI,
        market_id=market_id,
        outcome_token_id=outcome_token_id,
        bids=tuple(bids),
        asks=tuple(asks),
        sequence=None,
        effective_at=observed_at,
        observed_at=observed_at,
        source_hash=source_hash,
    )


def _parse_trade_row(
    row: Mapping[str, object], *, raw_source_hash: str, observed_at: datetime
) -> TradeRecord:
    ticker = _require_string(row, "ticker", "trade row")
    trade_id = _require_string(row, "trade_id", "trade row")
    taker_side = _require_string(row, "taker_side", "trade row")
    if taker_side not in ("yes", "no"):
        raise ValueError("trade row 'taker_side' must be yes or no")
    price_key = "yes_price_dollars" if taker_side == "yes" else "no_price_dollars"
    price = _require_decimal_string(row.get(price_key), f"trade row {price_key}")
    count = row.get("count_fp")
    if not isinstance(count, str):
        raise ValueError("trade row 'count_fp' must be a decimal string")
    size = _require_decimal_string(count, "trade row count_fp")
    effective_at = _parse_datetime(row.get("created_time"), "trade row created_time")
    if effective_at > observed_at:
        raise ValueError("trade timestamp is after response receipt")
    # taker_book_side names which resting side the taker executed against: hitting the bid
    # side means the taker sold the outcome named by taker_side; lifting the ask side means
    # the taker bought it.
    taker_book_side = _require_string(row, "taker_book_side", "trade row")
    if taker_book_side == "bid":
        side = "sell"
    elif taker_book_side == "ask":
        side = "buy"
    else:
        raise ValueError("trade row 'taker_book_side' must be bid or ask")
    return TradeRecord(
        schema_version=1,
        venue=PredictionVenue.KALSHI,
        market_id=ticker,
        outcome_token_id=taker_side,
        trade_id=trade_id,
        price=price,
        size=size,
        side=side,
        effective_at=effective_at,
        observed_at=observed_at,
        source_hash=raw_source_hash,
    )
