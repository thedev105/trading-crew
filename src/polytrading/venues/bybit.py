from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from itertools import pairwise
from uuid import UUID

import httpx

from polytrading.domain.models import (
    Asset,
    BookLevel,
    FundingObservation,
    InstrumentKind,
    InstrumentSpec,
    Level2BookSnapshot,
    MarketSnapshot,
    RawEnvelope,
    Venue,
    normalize_utc_timestamp,
)
from polytrading.registry.instruments import InstrumentRegistry
from polytrading.venues.public import AdapterBatch, AdapterWarning
from polytrading.venues.recorder import make_raw_envelope

_BASE_URL = "https://api.bybit.com"
_INSTRUMENTS_ENDPOINT = "/v5/market/instruments-info"
_TICKERS_ENDPOINT = "/v5/market/tickers"
_FUNDING_ENDPOINT = "/v5/market/funding/history"
_ORDERBOOK_ENDPOINT = "/v5/market/orderbook"
_SOURCE_VERSION = "v5-public-market"
_INSTRUMENT_PAGE_LIMIT = 1000
_FUNDING_PAGE_LIMIT = 200
_MAX_INSTRUMENT_PAGES = 1000
_MILLISECONDS_PER_SECOND = 1000
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_SYMBOL_BY_ASSET = {
    Asset.BTC: "BTCUSDT",
    Asset.ETH: "ETHUSDT",
    Asset.SOL: "SOLUSDT",
}
_ASSET_BY_SYMBOL = {symbol: asset for asset, symbol in _SYMBOL_BY_ASSET.items()}


class PaginationStalledError(RuntimeError):
    """Raised when a finite Bybit pagination loop cannot make progress."""


class VenueResponseError(RuntimeError):
    """A sanitized non-zero Bybit response code."""

    def __init__(self, endpoint: str, code: int) -> None:
        self.endpoint = endpoint
        self.code = code
        super().__init__(f"Bybit endpoint {endpoint} returned code {code}")


@dataclass(frozen=True)
class _ReceivedResponse:
    endpoint: str
    payload: bytes
    document: Mapping[str, object]
    observed_at: datetime
    venue_timestamp: datetime
    monotonic_started_ns: int
    monotonic_completed_ns: int

    def raw_envelope(self) -> RawEnvelope:
        return make_raw_envelope(
            venue=Venue.BYBIT,
            payload=self.payload,
            endpoint=self.endpoint,
            source_version=_SOURCE_VERSION,
            venue_timestamp=self.venue_timestamp,
            monotonic_started_ns=self.monotonic_started_ns,
            monotonic_completed_ns=self.monotonic_completed_ns,
            observed_at=self.observed_at,
        )


@dataclass(frozen=True)
class _BookPayload:
    effective_at: datetime
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]
    update_id: int
    cross_sequence: int


class BybitPublicAdapter:
    venue = Venue.BYBIT

    def __init__(
        self,
        client: httpx.AsyncClient,
        wall_clock: Callable[[], datetime],
        monotonic_ns: Callable[[], int],
        *,
        instrument_registry: InstrumentRegistry,
        max_funding_pages: int = 10_000,
    ) -> None:
        if isinstance(max_funding_pages, bool) or not isinstance(max_funding_pages, int):
            raise TypeError("max_funding_pages must be an integer")
        if max_funding_pages <= 0:
            raise ValueError("max_funding_pages must be positive")
        self._client = client
        self._wall_clock = wall_clock
        self._monotonic_ns = monotonic_ns
        self._instrument_registry = instrument_registry
        self._max_funding_pages = max_funding_pages

    async def fetch_instruments(
        self, assets: frozenset[Asset], observed_at: datetime
    ) -> AdapterBatch:
        _require_collection_context(observed_at)
        cursor: str | None = None
        seen_cursors: set[str] = set()
        seen_symbols: set[str] = set()
        raws: list[RawEnvelope] = []
        instruments: list[InstrumentSpec] = []

        for _page_number in range(_MAX_INSTRUMENT_PAGES):
            params: dict[str, str | int] = {
                "category": "linear",
                "limit": _INSTRUMENT_PAGE_LIMIT,
            }
            if cursor is not None:
                params["cursor"] = cursor
            received = await self._get(_INSTRUMENTS_ENDPOINT, params)
            raw = received.raw_envelope()
            raws.append(raw)
            result = _result_mapping(received.document, _INSTRUMENTS_ENDPOINT)
            _require_category(result, _INSTRUMENTS_ENDPOINT)
            rows = _result_list(result, _INSTRUMENTS_ENDPOINT)

            for value in rows:
                row = _require_mapping(value, "instrument row")
                symbol = _require_string(row, "symbol", "instrument row")
                if symbol in seen_symbols:
                    raise ValueError("instrument response contains duplicate symbols")
                seen_symbols.add(symbol)
                asset = _ASSET_BY_SYMBOL.get(symbol)
                if asset is None or asset not in assets:
                    continue
                instrument = _parse_instrument(row, asset, received.observed_at, raw.source_hash)
                if instrument is not None:
                    instruments.append(instrument)

            next_cursor = _require_string(result, "nextPageCursor", "instrument result")
            if not next_cursor:
                break
            if next_cursor in seen_cursors:
                raise PaginationStalledError("instrument cursor made no progress")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        else:
            raise PaginationStalledError("instrument cursor page budget exhausted")

        instruments.sort(key=lambda item: item.symbol)
        return AdapterBatch(raw=tuple(raws), normalized=tuple(instruments))

    async def fetch_market_snapshots(
        self, assets: frozenset[Asset], observed_at: datetime
    ) -> AdapterBatch:
        _require_collection_context(observed_at)
        received = await self._get(_TICKERS_ENDPOINT, {"category": "linear"})
        raw = received.raw_envelope()
        result = _result_mapping(received.document, _TICKERS_ENDPOINT)
        _require_category(result, _TICKERS_ENDPOINT)
        rows = _result_list(result, _TICKERS_ENDPOINT)
        tickers: dict[str, Mapping[str, object]] = {}
        for value in rows:
            row = _require_mapping(value, "ticker row")
            symbol = _require_string(row, "symbol", "ticker row")
            if symbol in tickers:
                raise ValueError("ticker response contains duplicate symbols")
            for field in ("bid1Price", "ask1Price", "markPrice", "indexPrice", "openInterest"):
                _require_decimal(row, field, f"{symbol} ticker row")
            tickers[symbol] = row

        snapshots: list[MarketSnapshot] = []
        warnings: list[AdapterWarning] = []
        for asset in sorted(assets, key=lambda item: item.value):
            symbol = _SYMBOL_BY_ASSET[asset]
            ticker = tickers.get(symbol)
            if ticker is None:
                warnings.append(
                    AdapterWarning(
                        code="ticker_missing",
                        venue=self.venue,
                        endpoint=_TICKERS_ENDPOINT,
                        symbol=symbol,
                        message="ticker missing for requested symbol",
                    )
                )
                continue
            snapshots.append(
                MarketSnapshot(
                    schema_version=1,
                    venue=self.venue,
                    symbol=symbol,
                    asset=asset,
                    bid=_require_decimal(ticker, "bid1Price", f"{symbol} ticker row"),
                    ask=_require_decimal(ticker, "ask1Price", f"{symbol} ticker row"),
                    mark=_require_decimal(ticker, "markPrice", f"{symbol} ticker row"),
                    index=_require_decimal(ticker, "indexPrice", f"{symbol} ticker row"),
                    open_interest=_require_decimal(ticker, "openInterest", f"{symbol} ticker row"),
                    effective_at=received.venue_timestamp,
                    observed_at=received.observed_at,
                    source_hash=raw.source_hash,
                )
            )
        return AdapterBatch(raw=(raw,), normalized=tuple(snapshots), warnings=tuple(warnings))

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
        start_ms = _ceil_epoch_milliseconds(normalized_start)
        end_ms = _floor_epoch_milliseconds(normalized_end)
        if start_ms > end_ms:
            return AdapterBatch(raw=(), normalized=())

        symbol = _SYMBOL_BY_ASSET[asset]
        cursor_end = end_ms
        previous_earliest: int | None = None
        raws: list[RawEnvelope] = []
        observations: dict[int, tuple[FundingObservation, Decimal, Decimal]] = {}
        for _request_number in range(self._max_funding_pages):
            received = await self._get(
                _FUNDING_ENDPOINT,
                {
                    "category": "linear",
                    "symbol": symbol,
                    "startTime": start_ms,
                    "endTime": cursor_end,
                    "limit": _FUNDING_PAGE_LIMIT,
                },
            )
            raw = received.raw_envelope()
            raws.append(raw)
            result = _result_mapping(received.document, _FUNDING_ENDPOINT)
            _require_category(result, _FUNDING_ENDPOINT)
            rows = _result_list(result, _FUNDING_ENDPOINT)
            if not rows:
                break
            if len(rows) > _FUNDING_PAGE_LIMIT:
                raise ValueError("funding history page exceeds the 200-row public limit")

            earliest: int | None = None
            for value in rows:
                row = _require_mapping(value, "funding history row")
                row_symbol = _require_string(row, "symbol", "funding history row")
                if row_symbol != symbol:
                    raise ValueError(
                        f"funding history row symbol {row_symbol!r} does not match {symbol!r}"
                    )
                timestamp_ms = _require_millisecond_string(
                    row, "fundingRateTimestamp", "funding history row"
                )
                if timestamp_ms < start_ms or timestamp_ms > end_ms:
                    raise ValueError("funding history row is outside requested range")
                rate = _require_decimal(row, "fundingRate", "funding history row")
                earliest = timestamp_ms if earliest is None else min(earliest, timestamp_ms)
                effective_at = _datetime_from_milliseconds(timestamp_ms)
                spec = self._instrument_registry.require_as_of(self.venue, symbol, effective_at)
                if spec.asset is not asset:
                    raise ValueError("historical instrument asset does not match funding asset")
                interval = spec.funding_interval_hours
                existing = observations.get(timestamp_ms)
                if existing is not None:
                    if existing[1] != rate or existing[2] != interval:
                        raise ValueError("conflicting duplicate funding observation")
                    continue
                observations[timestamp_ms] = (
                    FundingObservation(
                        schema_version=1,
                        venue=self.venue,
                        symbol=symbol,
                        asset=asset,
                        rate=rate,
                        interval_hours=interval,
                        effective_at=effective_at,
                        observed_at=received.observed_at,
                        source_hash=raw.source_hash,
                    ),
                    rate,
                    interval,
                )

            if earliest is None:
                raise ValueError("non-empty funding history page contains no rows")
            if earliest <= start_ms:
                break
            if previous_earliest is not None and earliest >= previous_earliest:
                raise PaginationStalledError("funding history page made no timestamp progress")
            next_end = earliest - 1
            if next_end >= cursor_end:
                raise PaginationStalledError("funding history page made no timestamp progress")
            previous_earliest = earliest
            cursor_end = next_end
        else:
            raise PaginationStalledError("funding history request budget exhausted")

        ordered = tuple(observations[key][0] for key in sorted(observations))
        return AdapterBatch(raw=tuple(raws), normalized=ordered)

    async def fetch_order_books(
        self,
        assets: frozenset[Asset],
        observed_at: datetime,
        cycle_id: UUID,
    ) -> AdapterBatch:
        _require_collection_context(observed_at)
        raws: list[RawEnvelope] = []
        books: list[Level2BookSnapshot] = []
        seen_sequences: set[tuple[int, int]] = set()

        for asset in sorted(assets, key=lambda item: item.value):
            symbol = _SYMBOL_BY_ASSET[asset]
            received = await self._get(
                _ORDERBOOK_ENDPOINT,
                {"category": "linear", "symbol": symbol, "limit": 20},
            )
            raw = received.raw_envelope()
            result = _result_mapping(received.document, _ORDERBOOK_ENDPOINT)
            parsed = _parse_book(result, symbol, received.observed_at)
            sequence_pair = (parsed.update_id, parsed.cross_sequence)
            if sequence_pair in seen_sequences:
                raise ValueError("repeated order book sequence within one fetch cycle")
            seen_sequences.add(sequence_pair)
            raws.append(raw)
            books.append(
                Level2BookSnapshot(
                    schema_version=1,
                    cycle_id=cycle_id,
                    venue=self.venue,
                    symbol=symbol,
                    asset=asset,
                    bids=parsed.bids,
                    asks=parsed.asks,
                    depth_limit=20,
                    sequence=(f"u={parsed.update_id};seq={parsed.cross_sequence}"),
                    effective_at=parsed.effective_at,
                    observed_at=received.observed_at,
                    source_hash=raw.source_hash,
                )
            )
        return AdapterBatch(raw=tuple(raws), normalized=tuple(books))

    async def _get(self, endpoint: str, params: Mapping[str, str | int]) -> _ReceivedResponse:
        monotonic_started_ns = self._monotonic_ns()
        response = await self._client.get(f"{_BASE_URL}{endpoint}", params=params)
        payload = response.content
        monotonic_completed_ns = self._monotonic_ns()
        observed_at = normalize_utc_timestamp(self._wall_clock())
        response.raise_for_status()
        try:
            value = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("Bybit response is not valid UTF-8 JSON") from error
        document = _require_mapping(value, "Bybit response")
        code = _require_integer(document, "retCode", "Bybit response")
        if code != 0:
            raise VenueResponseError(endpoint, code)
        timestamp_ms = _require_integer(document, "time", "Bybit response")
        if timestamp_ms < 0:
            raise ValueError("Bybit response time must be non-negative milliseconds")
        venue_timestamp = _datetime_from_milliseconds(timestamp_ms)
        if venue_timestamp > observed_at:
            raise ValueError("Bybit response time is after response receipt")
        return _ReceivedResponse(
            endpoint=endpoint,
            payload=payload,
            document=document,
            observed_at=observed_at,
            venue_timestamp=venue_timestamp,
            monotonic_started_ns=monotonic_started_ns,
            monotonic_completed_ns=monotonic_completed_ns,
        )


def _require_collection_context(observed_at: datetime) -> None:
    normalize_utc_timestamp(observed_at)


def _parse_instrument(
    row: Mapping[str, object], asset: Asset, observed_at: datetime, source_hash: str
) -> InstrumentSpec | None:
    contract_type = _require_string(row, "contractType", "instrument row")
    status = _require_string(row, "status", "instrument row")
    is_prelaunch = _require_boolean(row, "isPreListing", "instrument row")
    if contract_type != "LinearPerpetual" or status != "Trading" or is_prelaunch:
        return None
    symbol = _require_string(row, "symbol", "instrument row")
    if _require_string(row, "baseCoin", "instrument row") != asset.value:
        raise ValueError("instrument baseCoin does not match requested asset")
    if _require_string(row, "quoteCoin", "instrument row") != "USDT":
        raise ValueError("linear perpetual quoteCoin must be USDT")
    settle_coin = _require_string(row, "settleCoin", "instrument row")
    funding_minutes = _require_integer(row, "fundingInterval", "instrument row")
    if funding_minutes <= 0:
        raise ValueError("fundingInterval must be positive minutes")
    price_filter = _require_mapping(
        _require_key(row, "priceFilter", "instrument row"), "instrument priceFilter"
    )
    lot_filter = _require_mapping(
        _require_key(row, "lotSizeFilter", "instrument row"),
        "instrument lotSizeFilter",
    )
    upper = _require_decimal(row, "upperFundingRate", "instrument row")
    lower = _require_decimal(row, "lowerFundingRate", "instrument row")
    funding_cap = upper if upper >= 0 and lower == -upper else None
    return InstrumentSpec(
        schema_version=1,
        instrument_id=f"bybit:{symbol}",
        venue=Venue.BYBIT,
        symbol=symbol,
        asset=asset,
        kind=InstrumentKind.LINEAR_PERPETUAL,
        contract_multiplier=Decimal(1),
        index_family=None,
        oracle_family=None,
        mark_method=None,
        liquidation_method=None,
        collateral_asset=settle_coin,
        pnl_asset=settle_coin,
        funding_formula_id=None,
        funding_cap=funding_cap,
        funding_interval_hours=Decimal(funding_minutes) / Decimal(60),
        funding_payment_offset_minutes=None,
        min_notional=_require_decimal(lot_filter, "minNotionalValue", "instrument lotSizeFilter"),
        quantity_step=_require_decimal(lot_filter, "qtyStep", "instrument lotSizeFilter"),
        price_tick=_require_decimal(price_filter, "tickSize", "instrument priceFilter"),
        is_inverse=False,
        is_prelaunch=False,
        observed_at=observed_at,
        source_hash=source_hash,
    )


def _parse_book(
    result: Mapping[str, object], expected_symbol: str, observed_at: datetime
) -> _BookPayload:
    symbol = _require_string(result, "s", "order book result")
    if symbol != expected_symbol:
        raise ValueError(f"order book symbol {symbol!r} does not match {expected_symbol!r}")
    result_timestamp = _require_integer(result, "ts", "order book result")
    cts = _require_integer(result, "cts", "order book result")
    if result_timestamp < 0 or cts < 0:
        raise ValueError("order book timestamps must be non-negative milliseconds")
    if _datetime_from_milliseconds(result_timestamp) > observed_at:
        raise ValueError("order book result timestamp is after response receipt")
    effective_at = _datetime_from_milliseconds(cts)
    if effective_at > observed_at:
        raise ValueError("order book cts is after response receipt")
    bids = _parse_book_side(_require_key(result, "b", "order book result"), "bids")
    asks = _parse_book_side(_require_key(result, "a", "order book result"), "asks")
    if any(left.price <= right.price for left, right in pairwise(bids)):
        raise ValueError("book bids must be in strictly descending price order")
    if any(left.price >= right.price for left, right in pairwise(asks)):
        raise ValueError("book asks must be in strictly ascending price order")
    if bids[0].price >= asks[0].price:
        raise ValueError("book top of book must not cross")
    update_id = _require_integer(result, "u", "order book result")
    cross_sequence = _require_integer(result, "seq", "order book result")
    if update_id < 0 or cross_sequence < 0:
        raise ValueError("order book sequence values must be non-negative")
    return _BookPayload(
        effective_at=effective_at,
        bids=bids,
        asks=asks,
        update_id=update_id,
        cross_sequence=cross_sequence,
    )


def _parse_book_side(value: object, label: str) -> tuple[BookLevel, ...]:
    rows = _require_list(value, f"order book {label}")
    if not rows:
        raise ValueError(f"order book {label} must not be empty")
    if len(rows) > 20:
        raise ValueError(f"order book {label} must contain no more than 20 levels")
    levels: list[BookLevel] = []
    for row in rows:
        values = _require_list(row, f"order book {label} level")
        if len(values) != 2:
            raise ValueError(f"order book {label} level must contain price and quantity")
        levels.append(
            BookLevel(
                price=_decimal_from_value(values[0], f"order book {label} price"),
                quantity=_decimal_from_value(values[1], f"order book {label} quantity"),
                order_count=None,
            )
        )
    return tuple(levels)


def _result_mapping(document: Mapping[str, object], endpoint: str) -> Mapping[str, object]:
    return _require_mapping(_require_key(document, "result", endpoint), f"{endpoint} result")


def _result_list(result: Mapping[str, object], endpoint: str) -> list[object]:
    return _require_list(_require_key(result, "list", f"{endpoint} result"), f"{endpoint} list")


def _require_category(result: Mapping[str, object], endpoint: str) -> None:
    category = _require_string(result, "category", f"{endpoint} result")
    if category != "linear":
        raise ValueError(f"{endpoint} result category must be 'linear'")


def _require_key(mapping: Mapping[str, object], key: str, label: str) -> object:
    try:
        return mapping[key]
    except KeyError as error:
        raise ValueError(f"{label} is missing required key {key!r}") from error


def _require_mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _require_list(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")
    return value


def _require_string(mapping: Mapping[str, object], key: str, label: str) -> str:
    value = _require_key(mapping, key, label)
    if not isinstance(value, str):
        raise ValueError(f"{label} key {key!r} must be a string")
    return value


def _require_boolean(mapping: Mapping[str, object], key: str, label: str) -> bool:
    value = _require_key(mapping, key, label)
    if not isinstance(value, bool):
        raise ValueError(f"{label} key {key!r} must be a boolean")
    return value


def _require_integer(mapping: Mapping[str, object], key: str, label: str) -> int:
    value = _require_key(mapping, key, label)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} key {key!r} must be an integer")
    return value


def _require_millisecond_string(mapping: Mapping[str, object], key: str, label: str) -> int:
    value = _require_string(mapping, key, label)
    if not value or not value.isascii() or not value.isdigit():
        raise ValueError(f"{label} key {key!r} must be a non-negative millisecond string")
    return int(value)


def _require_decimal(mapping: Mapping[str, object], key: str, label: str) -> Decimal:
    return _decimal_from_value(_require_key(mapping, key, label), f"{label} key {key!r}")


def _decimal_from_value(value: object, label: str) -> Decimal:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise ValueError(f"{label} must be a valid decimal string") from error
    if not parsed.is_finite():
        raise ValueError(f"{label} must be a finite decimal string")
    return parsed


def _datetime_from_milliseconds(value: int) -> datetime:
    try:
        return _EPOCH + timedelta(milliseconds=value)
    except (OverflowError, OSError, ValueError) as error:
        raise ValueError("response timestamp is outside the supported datetime range") from error


def _floor_epoch_milliseconds(value: datetime) -> int:
    delta = value - _EPOCH
    return (
        delta.days * 86_400_000
        + delta.seconds * _MILLISECONDS_PER_SECOND
        + delta.microseconds // _MILLISECONDS_PER_SECOND
    )


def _ceil_epoch_milliseconds(value: datetime) -> int:
    floor = _floor_epoch_milliseconds(value)
    return floor + int(value.microsecond % _MILLISECONDS_PER_SECOND != 0)
