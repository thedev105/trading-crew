from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
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
    PredictionFeeRate,
    PredictionRawEnvelope,
    PredictionVenue,
    RuleVersion,
    TradeRecord,
    rule_relevant_version_id,
)
from polytrading.predictions.manifest import VenueManifest, evaluate_collection_gate

_GAMMA_BASE_URL = "https://gamma-api.polymarket.com"
_CLOB_BASE_URL = "https://clob.polymarket.com"
_DATA_BASE_URL = "https://data-api.polymarket.com"
_MARKETS_ENDPOINT = "/markets"
_BOOK_ENDPOINT = "/book"
_FEE_RATE_ENDPOINT = "/fee-rate"
_TRADES_ENDPOINT = "/trades"
_SOURCE_VERSION = "gamma-clob-data-v1"
_PAGE_LIMIT = 500
_MAX_PAGES = 1000
_FEE_BASIS_POINTS_DIVISOR = Decimal(10_000)


class PaginationStalledError(RuntimeError):
    """Raised when a finite Gamma pagination loop cannot make progress."""


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


def _require_bool(mapping: Mapping[str, object], key: str, label: str) -> bool:
    value = mapping.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"{label} key {key!r} must be a boolean")
    return value


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


def _parse_inner_json_array(mapping: Mapping[str, object], key: str, label: str) -> list[object]:
    raw_value = mapping.get(key)
    if not isinstance(raw_value, str):
        raise ValueError(f"{label} key {key!r} must be a JSON-encoded string")
    try:
        parsed = json.loads(raw_value)
    except json.JSONDecodeError as error:
        raise ValueError(f"{label} key {key!r} is not valid JSON") from error
    if not isinstance(parsed, list):
        raise ValueError(f"{label} key {key!r} must decode to a JSON array")
    return parsed


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
            event_id=uuid5(
                NAMESPACE_URL,
                f"polymarket:{self.endpoint}:{self.received_monotonic_ns}",
            ),
            venue=PredictionVenue.POLYMARKET,
            endpoint=self.endpoint,
            venue_timestamp=None,
            observed_at=self.observed_at,
            received_monotonic_ns=self.received_monotonic_ns,
            request_latency_ms=self.latency_ms,
            source_version=_SOURCE_VERSION,
            payload_json=payload_text,
            source_hash=sha256(payload_text.encode("utf-8")).hexdigest(),
        )


class PolymarketAdapter:
    venue = PredictionVenue.POLYMARKET

    def __init__(
        self,
        client: httpx.AsyncClient,
        wall_clock: Callable[[], datetime],
        monotonic_ns: Callable[[], int],
        *,
        max_market_pages: int = _MAX_PAGES,
    ) -> None:
        if isinstance(max_market_pages, bool) or not isinstance(max_market_pages, int):
            raise TypeError("max_market_pages must be an integer")
        if max_market_pages <= 0:
            raise ValueError("max_market_pages must be positive")
        self._client = client
        self._wall_clock = wall_clock
        self._monotonic_ns = monotonic_ns
        self._max_market_pages = max_market_pages

    async def fetch_manifest_gated(self, manifest: VenueManifest) -> None:
        decision = evaluate_collection_gate(manifest, venue=self.venue)
        if not decision.allowed:
            raise PredictionCollectionGateError(decision.reason or "COLLECTION_NOT_PERMITTED")

    async def _get(
        self, base_url: str, endpoint: str, params: Mapping[str, object]
    ) -> _ReceivedResponse:
        started_ns = self._monotonic_ns()
        response = await self._client.get(f"{base_url}{endpoint}", params=params)
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

    async def fetch_markets(self, *, information_cutoff: datetime) -> PredictionAdapterBatch:
        raws: list[PredictionRawEnvelope] = []
        normalized: list[MarketRecord | RuleVersion] = []
        seen_ids: set[str] = set()
        offset = 0
        for _page_number in range(self._max_market_pages):
            received = await self._get(
                _GAMMA_BASE_URL,
                _MARKETS_ENDPOINT,
                {"limit": _PAGE_LIMIT, "offset": offset, "active": "true", "closed": "false"},
            )
            raw = received.raw_envelope()
            raws.append(raw)
            rows = _require_list(received.document, "markets page")
            for value in rows:
                row = _require_mapping(value, "market row")
                condition_id = _require_string(row, "conditionId", "market row")
                if condition_id in seen_ids:
                    raise ValueError("markets page contains a duplicate conditionId")
                seen_ids.add(condition_id)
                market, rule_version = _parse_market_row(
                    row, information_cutoff=information_cutoff, raw=raw
                )
                normalized.append(market)
                normalized.append(rule_version)
            if len(rows) < _PAGE_LIMIT:
                break
            offset += _PAGE_LIMIT
        else:
            raise PaginationStalledError("Gamma markets pagination did not terminate")
        return PredictionAdapterBatch(raw=tuple(raws), normalized=tuple(normalized))

    async def fetch_book_snapshot(
        self,
        market_id: str,
        outcome_token_id: str | None,
        observed_at: datetime,
        cycle_id: UUID,
    ) -> PredictionAdapterBatch:
        _require_collection_context(observed_at)
        if outcome_token_id is None:
            raise ValueError("Polymarket book snapshots require an outcome token ID")
        received = await self._get(_CLOB_BASE_URL, _BOOK_ENDPOINT, {"token_id": outcome_token_id})
        raw = received.raw_envelope()
        document = _require_mapping(received.document, "book response")
        snapshot = _parse_book_document(
            document,
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
        received = await self._get(
            _DATA_BASE_URL, _TRADES_ENDPOINT, {"market": market_id, "limit": _PAGE_LIMIT}
        )
        raw = received.raw_envelope()
        rows = _require_list(received.document, "trades response")
        normalized: list[TradeRecord] = []
        for value in rows:
            row = _require_mapping(value, "trade row")
            if _require_string(row, "conditionId", "trade row") != market_id:
                continue
            trade = _parse_trade_row(
                row, raw_source_hash=raw.source_hash, observed_at=received.observed_at
            )
            if start <= trade.effective_at <= end:
                normalized.append(trade)
        return PredictionAdapterBatch(raw=(raw,), normalized=tuple(normalized))

    async def fetch_fee_rate(
        self, market_id: str | None, observed_at: datetime
    ) -> PredictionAdapterBatch:
        _require_collection_context(observed_at)
        if market_id is None:
            raise ValueError("Polymarket fee rate requires a token/market identifier")
        received = await self._get(_CLOB_BASE_URL, _FEE_RATE_ENDPOINT, {"token_id": market_id})
        raw = received.raw_envelope()
        document = _require_mapping(received.document, "fee-rate response")
        base_fee = document.get("base_fee")
        if isinstance(base_fee, bool) or not isinstance(base_fee, int):
            raise ValueError("fee-rate key 'base_fee' must be an integer")
        # base_fee is documented in basis points; Polymarket's own fee documentation and
        # community reports (Polymarket/py-clob-client#326) note the *applied* taker fee
        # actually follows a price-dependent curve, not this flat rate. This flat value is
        # the venue's own reference figure, not proof of the exact fee a fill will incur.
        taker_rate = Decimal(base_fee) / _FEE_BASIS_POINTS_DIVISOR
        fee_rate = PredictionFeeRate(
            schema_version=1,
            venue=PredictionVenue.POLYMARKET,
            market_id=market_id,
            maker_rate=Decimal(0),
            taker_rate=taker_rate,
            observed_at=received.observed_at,
            source_hash=raw.source_hash,
        )
        warning = PredictionAdapterWarning(
            code="POLYMARKET_FEE_RATE_IS_FLAT_REFERENCE_NOT_APPLIED_CURVE",
            venue=PredictionVenue.POLYMARKET,
            endpoint=_FEE_RATE_ENDPOINT,
            market_id=market_id,
            message=(
                "base_fee is a flat basis-point reference; Polymarket's applied taker fee "
                "follows a price-dependent curve that this value does not represent exactly"
            ),
        )
        return PredictionAdapterBatch(raw=(raw,), normalized=(fee_rate,), warnings=(warning,))


def _parse_market_row(
    row: Mapping[str, object], *, information_cutoff: datetime, raw: PredictionRawEnvelope
) -> tuple[MarketRecord, RuleVersion]:
    condition_id = _require_string(row, "conditionId", "market row")
    question = _require_string(row, "question", "market row")
    outcomes_raw = _parse_inner_json_array(row, "outcomes", "market row")
    outcomes = tuple(str(item) for item in outcomes_raw)
    if not outcomes:
        raise ValueError("market row must declare at least one outcome")
    token_ids_raw = _parse_inner_json_array(row, "clobTokenIds", "market row")
    outcome_token_ids = tuple(str(item) for item in token_ids_raw)
    if len(outcome_token_ids) != len(outcomes):
        raise ValueError("clobTokenIds must align one-to-one with outcomes")

    active = _require_bool(row, "active", "market row")
    closed = _require_bool(row, "closed", "market row")
    restricted = _require_bool(row, "restricted", "market row")
    order_book_enabled = _require_bool(row, "enableOrderBook", "market row")
    negative_risk = _require_bool(row, "negRisk", "market row")

    start_at = _optional_datetime(row.get("startDate"))
    end_at = _optional_datetime(row.get("endDate"))
    resolution_source_raw = row.get("resolutionSource")
    resolution_source = resolution_source_raw or None
    description_raw = row.get("description")
    description = description_raw if isinstance(description_raw, str) else ""

    rule_version_id = rule_relevant_version_id(
        PredictionVenue.POLYMARKET,
        condition_id,
        question,
        description,
        resolution_source,
        outcomes,
        end_at,
    )
    market = MarketRecord(
        schema_version=1,
        market_id=condition_id,
        venue=PredictionVenue.POLYMARKET,
        underlying_exchange=None,
        event_id=_first_event_id(row.get("events")),
        question=question,
        slug=row.get("slug") if isinstance(row.get("slug"), str) else None,
        outcomes=outcomes,
        outcome_token_ids=outcome_token_ids,
        negative_risk=negative_risk,
        active=active,
        closed=closed,
        restricted=restricted,
        order_book_enabled=order_book_enabled,
        start_at=start_at,
        end_at=end_at,
        resolution_source=resolution_source,
        rule_version_id=rule_version_id,
        information_cutoff=information_cutoff,
        source_url=f"{_GAMMA_BASE_URL}{_MARKETS_ENDPOINT}",
        retrieved_at=information_cutoff,
        raw_hash=raw.source_hash,
        normalized_hash=raw.source_hash,
    )
    rule_version = RuleVersion(
        schema_version=1,
        rule_version_id=rule_version_id,
        market_id=condition_id,
        venue=PredictionVenue.POLYMARKET,
        question=question,
        description=description,
        resolution_source=resolution_source,
        outcomes=outcomes,
        superseded_rule_version_id=None,
        effective_at=information_cutoff,
        source_hash=raw.source_hash,
    )
    return market, rule_version


def _first_event_id(value: object) -> str | None:
    if not isinstance(value, list) or not value:
        return None
    first = value[0]
    if not isinstance(first, dict):
        return None
    event_id = first.get("id")
    return event_id if isinstance(event_id, str) else None


def _optional_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _parse_book_document(
    document: Mapping[str, object],
    *,
    market_id: str,
    outcome_token_id: str,
    cycle_id: UUID,
    observed_at: datetime,
    source_hash: str,
) -> PredictionBookSnapshot:
    timestamp_ms = _require_string(document, "timestamp", "book response")
    effective_at = datetime.fromtimestamp(int(timestamp_ms) / 1000, tz=UTC)
    book_hash = _require_string(document, "hash", "book response")
    raw_bids = _require_list(document.get("bids"), "bids")
    raw_asks = _require_list(document.get("asks"), "asks")
    # Polymarket's public /book response lists bids ascending (worst first) and asks
    # descending (worst first) -- the opposite convention from this domain's book model,
    # which requires bids descending / asks ascending (best price first on both sides).
    bids = tuple(reversed([_parse_book_level(item) for item in raw_bids]))
    asks = tuple(reversed([_parse_book_level(item) for item in raw_asks]))
    return PredictionBookSnapshot(
        schema_version=1,
        cycle_id=cycle_id,
        venue=PredictionVenue.POLYMARKET,
        market_id=market_id,
        outcome_token_id=outcome_token_id,
        bids=bids,
        asks=asks,
        sequence=book_hash,
        effective_at=effective_at,
        observed_at=observed_at,
        source_hash=source_hash,
    )


def _parse_book_level(value: object) -> PredictionBookLevel:
    row = _require_mapping(value, "book level")
    return PredictionBookLevel(
        price=_require_decimal_string(row.get("price"), "book level price"),
        size=_require_decimal_string(row.get("size"), "book level size"),
    )


class PolymarketBookContinuity:
    """Tracks whether one outcome-token's CLOB WebSocket book state can still be trusted.

    Every ``book`` market-channel event is a full authoritative snapshot carrying the same
    ``hash`` fingerprint field as the public REST ``/book`` response (spec section 6.3 requires
    periodic reconciliation against an independent REST snapshot). This class does not attempt
    to reimplement Polymarket's undocumented ``price_change`` delta-application algorithm: a
    ``price_change`` event, a heartbeat timeout, or a disconnect all simply invalidate the
    maintained state until the next full ``book`` event or REST reconciliation restores it.
    Persisted book evidence is still produced only via ``PolymarketAdapter.fetch_book_snapshot``;
    this class is a freshness/gap signal, not an alternate persistence path.
    """

    def __init__(self) -> None:
        self._latest_hash: str | None = None
        self._valid = False

    @property
    def is_valid(self) -> bool:
        return self._valid

    @property
    def latest_hash(self) -> str | None:
        return self._latest_hash if self._valid else None

    def handle_message(self, message: Mapping[str, object], *, outcome_token_id: str) -> None:
        event_type = message.get("event_type")
        if event_type == "book":
            if message.get("asset_id") != outcome_token_id:
                return
            self._latest_hash = _require_string(message, "hash", "market channel book event")
            self._valid = True
        elif event_type in ("price_change", "tick_size_change"):
            self._valid = False
        else:
            raise ValueError(f"unsupported market channel event_type: {event_type!r}")

    def invalidate(self) -> None:
        self._valid = False

    def reconcile_with_rest(self, rest_book_hash: str) -> bool:
        """Confirm the maintained WS state against an independent REST snapshot's hash.

        Returns True only when a valid WS state exists and its hash matches; any mismatch
        invalidates the WS state so a stale or diverged book is never reported as current.
        """
        if not self._valid or self._latest_hash != rest_book_hash:
            self._valid = False
            return False
        return True


def _parse_trade_row(
    row: Mapping[str, object], *, raw_source_hash: str, observed_at: datetime
) -> TradeRecord:
    condition_id = _require_string(row, "conditionId", "trade row")
    asset_id = _require_string(row, "asset", "trade row")
    tx_hash = _require_string(row, "transactionHash", "trade row")
    price = row.get("price")
    size = row.get("size")
    if isinstance(price, bool) or not isinstance(price, int | float):
        raise ValueError("trade row 'price' must be numeric")
    if isinstance(size, bool) or not isinstance(size, int | float):
        raise ValueError("trade row 'size' must be numeric")
    timestamp = row.get("timestamp")
    if isinstance(timestamp, bool) or not isinstance(timestamp, int):
        raise ValueError("trade row 'timestamp' must be an integer")
    side_raw = _require_string(row, "side", "trade row").lower()
    if side_raw not in ("buy", "sell"):
        raise ValueError("trade row 'side' must be BUY or SELL")
    effective_at = datetime.fromtimestamp(timestamp, tz=UTC)
    if effective_at > observed_at:
        raise ValueError("trade timestamp is after response receipt")
    return TradeRecord(
        schema_version=1,
        venue=PredictionVenue.POLYMARKET,
        market_id=condition_id,
        outcome_token_id=asset_id,
        trade_id=tx_hash,
        price=Decimal(str(price)),
        size=Decimal(str(size)),
        side=side_raw,
        effective_at=effective_at,
        observed_at=observed_at,
        source_hash=raw_source_hash,
    )
