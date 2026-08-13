from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

import httpx

from polytrading.domain.models import (
    Asset,
    FundingObservation,
    InstrumentKind,
    InstrumentSpec,
    RawEnvelope,
    Venue,
    normalize_utc_timestamp,
)
from polytrading.venues.public import AdapterBatch, AdapterWarning
from polytrading.venues.recorder import make_raw_envelope

_BASE_URL = "https://indexer.dydx.trade"
_MARKETS_ENDPOINT = "/v4/perpetualMarkets"
_FUNDING_ENDPOINT_PREFIX = "/v4/historicalFunding"
_SOURCE_VERSION = "indexer-v4-public"
_FUNDING_PAGE_LIMIT = 100
_SYMBOL_BY_ASSET = {
    Asset.BTC: "BTC-USD",
    Asset.ETH: "ETH-USD",
    Asset.SOL: "SOL-USD",
}


@dataclass(frozen=True)
class _ReceivedResponse:
    endpoint: str
    payload: bytes
    document: Mapping[str, object]
    observed_at: datetime
    monotonic_started_ns: int
    monotonic_completed_ns: int

    def raw_envelope(self) -> RawEnvelope:
        return make_raw_envelope(
            venue=Venue.DYDX,
            payload=self.payload,
            endpoint=self.endpoint,
            source_version=_SOURCE_VERSION,
            venue_timestamp=None,
            monotonic_started_ns=self.monotonic_started_ns,
            monotonic_completed_ns=self.monotonic_completed_ns,
            observed_at=self.observed_at,
        )


class PaginationStalledError(RuntimeError):
    """Raised when finite dYdX funding pagination cannot move backward."""


class DydxPublicAdapter:
    venue = Venue.DYDX

    def __init__(
        self,
        client: httpx.AsyncClient,
        wall_clock: Callable[[], datetime],
        monotonic_ns: Callable[[], int],
        *,
        max_funding_pages: int = 10_000,
    ) -> None:
        if isinstance(max_funding_pages, bool) or not isinstance(max_funding_pages, int):
            raise TypeError("maximum funding pages must be an integer")
        if max_funding_pages <= 0:
            raise ValueError("maximum funding pages must be positive")
        self._client = client
        self._wall_clock = wall_clock
        self._monotonic_ns = monotonic_ns
        self._max_funding_pages = max_funding_pages

    async def fetch_instruments(
        self, assets: frozenset[Asset], observed_at: datetime
    ) -> AdapterBatch:
        _require_collection_context(observed_at)
        received = await self._get(_MARKETS_ENDPOINT)
        raw = received.raw_envelope()
        selected = _select_requested_markets(received.document, assets)
        instruments = tuple(
            _instrument_spec(asset, symbol, row, received.observed_at, raw.source_hash)
            for asset, symbol, row in selected
        )
        return AdapterBatch(raw=(raw,), normalized=instruments)

    async def fetch_market_snapshots(
        self, assets: frozenset[Asset], observed_at: datetime
    ) -> AdapterBatch:
        _require_collection_context(observed_at)
        received = await self._get(_MARKETS_ENDPOINT)
        raw = received.raw_envelope()
        selected = _select_requested_markets(received.document, assets)
        warnings = tuple(
            AdapterWarning(
                code="DYDX_MARK_PRICE_UNAVAILABLE",
                venue=self.venue,
                endpoint=_MARKETS_ENDPOINT,
                symbol=symbol,
                message="dYdX public market evidence has no documented mark-price field",
            )
            for _asset, symbol, _row in selected
        )
        return AdapterBatch(raw=(raw,), normalized=(), warnings=warnings)

    async def fetch_funding_history(
        self,
        asset: Asset,
        start: datetime,
        end: datetime,
        observed_at: datetime,
    ) -> AdapterBatch:
        _require_collection_context(observed_at)
        normalized_start = normalize_utc_timestamp(start)
        normalized_end = normalize_utc_timestamp(end)
        if normalized_end < normalized_start:
            raise ValueError("funding range end must not precede start")

        possible_hourly_rows = (
            math.ceil((normalized_end - normalized_start) / timedelta(hours=1)) + 1
        )
        request_budget = min(possible_hourly_rows + 1, self._max_funding_pages)
        symbol = _SYMBOL_BY_ASSET[asset]
        endpoint = f"{_FUNDING_ENDPOINT_PREFIX}/{symbol}"
        cursor = normalized_end
        raws: list[RawEnvelope] = []
        seen_rates: dict[datetime, Decimal] = {}
        observations: dict[datetime, FundingObservation] = {}

        for _request_number in range(request_budget):
            received = await self._get(
                endpoint,
                {
                    "limit": _FUNDING_PAGE_LIMIT,
                    "effectiveBeforeOrAt": _format_query_timestamp(cursor),
                },
            )
            raw = received.raw_envelope()
            raws.append(raw)
            rows = _require_list(
                _require_key(received.document, "historicalFunding", "funding response"),
                "historicalFunding",
            )
            if not rows:
                break
            if len(rows) > _FUNDING_PAGE_LIMIT:
                raise ValueError("funding history page exceeds the requested 100-row limit")

            oldest: datetime | None = None
            for value in rows:
                row = _require_mapping(value, "funding history row")
                ticker = _require_string(row, "ticker", "funding history row")
                if ticker != symbol:
                    raise ValueError(f"funding history ticker {ticker!r} does not match {symbol!r}")
                rate = _require_decimal(row, "rate", "funding history row")
                effective_at = _parse_iso_timestamp(
                    _require_key(row, "effectiveAt", "funding history row"),
                    "funding history effectiveAt",
                )
                if effective_at > received.observed_at:
                    raise ValueError("funding effectiveAt is after response receipt")
                if effective_at > cursor:
                    raise PaginationStalledError(
                        "funding history row is newer than the backward cursor"
                    )
                oldest = effective_at if oldest is None else min(oldest, effective_at)
                existing_rate = seen_rates.get(effective_at)
                if existing_rate is not None:
                    if existing_rate != rate:
                        raise ValueError("conflicting duplicate funding observation")
                    continue
                seen_rates[effective_at] = rate
                if normalized_start <= effective_at <= normalized_end:
                    observations[effective_at] = FundingObservation(
                        schema_version=1,
                        venue=self.venue,
                        symbol=symbol,
                        asset=asset,
                        rate=rate,
                        interval_hours=Decimal(1),
                        effective_at=effective_at,
                        observed_at=received.observed_at,
                        source_hash=raw.source_hash,
                    )

            if oldest is None:
                raise ValueError("non-empty funding history page contains no rows")
            if oldest <= normalized_start:
                break
            next_cursor = oldest - timedelta(microseconds=1)
            if next_cursor >= cursor:
                raise PaginationStalledError("funding history cursor made no backward progress")
            cursor = next_cursor
        else:
            raise PaginationStalledError("funding history request budget exhausted")

        normalized = tuple(observations[key] for key in sorted(observations))
        return AdapterBatch(raw=tuple(raws), normalized=normalized)

    async def _get(
        self, endpoint: str, params: Mapping[str, str | int] | None = None
    ) -> _ReceivedResponse:
        monotonic_started_ns = self._monotonic_ns()
        response = await self._client.get(f"{_BASE_URL}{endpoint}", params=params)
        payload = response.content
        monotonic_completed_ns = self._monotonic_ns()
        observed_at = normalize_utc_timestamp(self._wall_clock())
        response.raise_for_status()
        try:
            decoded = payload.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError(f"dYdX endpoint {endpoint} response is not valid UTF-8") from error
        try:
            value = json.loads(decoded)
        except json.JSONDecodeError as error:
            raise ValueError(f"dYdX endpoint {endpoint} response is not valid JSON") from error
        document = _require_mapping(value, f"dYdX endpoint {endpoint} response")
        return _ReceivedResponse(
            endpoint=endpoint,
            payload=payload,
            document=document,
            observed_at=observed_at,
            monotonic_started_ns=monotonic_started_ns,
            monotonic_completed_ns=monotonic_completed_ns,
        )


def _require_collection_context(observed_at: datetime) -> None:
    normalize_utc_timestamp(observed_at)


def _select_requested_markets(
    document: Mapping[str, object], assets: frozenset[Asset]
) -> tuple[tuple[Asset, str, Mapping[str, object]], ...]:
    markets = _require_mapping(_require_key(document, "markets", "market response"), "markets")
    by_symbol: dict[str, Mapping[str, object]] = {}
    for key, value in markets.items():
        if not isinstance(key, str):
            raise ValueError("market ticker key must be a string")
        row = _require_mapping(value, f"market {key!r}")
        ticker = _require_string(row, "ticker", f"market {key!r}")
        if ticker != key:
            raise ValueError("market ticker field must match its mapping key")
        if ticker in by_symbol:
            raise ValueError("market response contains a duplicate ticker")
        by_symbol[ticker] = row

    selected: list[tuple[Asset, str, Mapping[str, object]]] = []
    missing: list[str] = []
    for asset in sorted(assets, key=lambda item: item.value):
        symbol = _SYMBOL_BY_ASSET[asset]
        row = by_symbol.get(symbol)
        if row is None:
            missing.append(symbol)
            continue
        if _require_string(row, "status", f"market {symbol}") != "ACTIVE":
            raise ValueError(f"requested market {symbol} must have ACTIVE status")
        selected.append((asset, symbol, row))
    if missing:
        raise ValueError(f"requested markets missing from response: {', '.join(missing)}")
    return tuple(selected)


def _instrument_spec(
    asset: Asset,
    symbol: str,
    row: Mapping[str, object],
    observed_at: datetime,
    source_hash: str,
) -> InstrumentSpec:
    return InstrumentSpec(
        schema_version=1,
        instrument_id=f"dydx:{symbol}",
        venue=Venue.DYDX,
        symbol=symbol,
        asset=asset,
        kind=InstrumentKind.LINEAR_PERPETUAL,
        contract_multiplier=Decimal(1),
        index_family=None,
        oracle_family=None,
        mark_method=None,
        liquidation_method=None,
        collateral_asset="USDC",
        pnl_asset="USDC",
        funding_formula_id=None,
        funding_cap=None,
        funding_interval_hours=Decimal(1),
        funding_payment_offset_minutes=None,
        min_notional=None,
        quantity_step=_require_positive_decimal(row, "stepSize", f"market {symbol}"),
        price_tick=_require_positive_decimal(row, "tickSize", f"market {symbol}"),
        is_inverse=False,
        is_prelaunch=False,
        observed_at=observed_at,
        source_hash=source_hash,
    )


def _require_key(mapping: Mapping[str, object], key: str, context: str) -> object:
    try:
        return mapping[key]
    except KeyError as error:
        raise ValueError(f"{context} is missing required key {key!r}") from error


def _require_mapping(value: object, context: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{context} must be a mapping")
    return value


def _require_string(mapping: Mapping[str, object], key: str, context: str) -> str:
    value = _require_key(mapping, key, context)
    if not isinstance(value, str):
        raise ValueError(f"{context} key {key!r} must be a string")
    return value


def _require_list(value: object, context: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{context} must be a list")
    return value


def _require_decimal(mapping: Mapping[str, object], key: str, context: str) -> Decimal:
    value = _require_string(mapping, key, context)
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise ValueError(f"{context} key {key!r} must be a decimal string") from error
    if not parsed.is_finite():
        raise ValueError(f"{context} key {key!r} must be finite")
    return parsed


def _parse_iso_timestamp(value: object, context: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{context} must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{context} must be a valid ISO-8601 timestamp") from error
    return normalize_utc_timestamp(parsed)


def _format_query_timestamp(value: datetime) -> str:
    return normalize_utc_timestamp(value).isoformat().replace("+00:00", "Z")


def _require_positive_decimal(mapping: Mapping[str, object], key: str, context: str) -> Decimal:
    parsed = _require_decimal(mapping, key, context)
    if parsed <= 0:
        raise ValueError(f"{context} key {key!r} must be positive")
    return parsed
