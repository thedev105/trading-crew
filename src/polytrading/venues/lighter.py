from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation

import httpx

from polytrading.domain.models import (
    Asset,
    InstrumentKind,
    InstrumentSpec,
    RawEnvelope,
    Venue,
    normalize_utc_timestamp,
)
from polytrading.venues.public import AdapterBatch
from polytrading.venues.recorder import make_raw_envelope

_BASE_URL = "https://mainnet.zklighter.elliot.ai"
_MARKETS_ENDPOINT = "/api/v1/orderBooks"
_SOURCE_VERSION = "mainnet-v1-public"
_SYMBOL_BY_ASSET = {
    Asset.BTC: "BTC",
    Asset.ETH: "ETH",
    Asset.SOL: "SOL",
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
            venue=Venue.LIGHTER,
            payload=self.payload,
            endpoint=self.endpoint,
            source_version=_SOURCE_VERSION,
            venue_timestamp=None,
            monotonic_started_ns=self.monotonic_started_ns,
            monotonic_completed_ns=self.monotonic_completed_ns,
            observed_at=self.observed_at,
        )


class LighterPublicAdapter:
    venue = Venue.LIGHTER

    def __init__(
        self,
        client: httpx.AsyncClient,
        wall_clock: Callable[[], datetime],
        monotonic_ns: Callable[[], int],
    ) -> None:
        self._client = client
        self._wall_clock = wall_clock
        self._monotonic_ns = monotonic_ns

    async def fetch_instruments(
        self, assets: frozenset[Asset], observed_at: datetime
    ) -> AdapterBatch:
        _require_collection_context(observed_at)
        received, selected = await self._resolve_markets(assets)
        raw = received.raw_envelope()
        instruments = tuple(
            _instrument_spec(asset, symbol, row, received.observed_at, raw.source_hash)
            for asset, symbol, _market_id, row in selected
        )
        return AdapterBatch(raw=(raw,), normalized=instruments)

    async def _resolve_markets(
        self, assets: frozenset[Asset]
    ) -> tuple[
        _ReceivedResponse,
        tuple[tuple[Asset, str, int, Mapping[str, object]], ...],
    ]:
        received = await self._get(_MARKETS_ENDPOINT, {"filter": "perp"})
        rows = _require_list(
            _require_key(received.document, "order_books", "market response"),
            "order_books",
        )
        by_symbol: dict[str, Mapping[str, object]] = {}
        for value in rows:
            row = _require_mapping(value, "market row")
            symbol = _require_string(row, "symbol", "market row")
            if symbol in by_symbol:
                raise ValueError(f"market response contains duplicate symbol {symbol!r}")
            by_symbol[symbol] = row

        selected: list[tuple[Asset, str, int, Mapping[str, object]]] = []
        missing: list[str] = []
        selected_market_ids: set[int] = set()
        for asset in sorted(assets, key=lambda item: item.value):
            symbol = _SYMBOL_BY_ASSET[asset]
            row = by_symbol.get(symbol)
            if row is None:
                missing.append(symbol)
                continue
            market_type = _require_string(row, "market_type", f"market {symbol}")
            if market_type != "perp":
                raise ValueError(f"Lighter market {symbol!r} is not a perpetual market")
            status = _require_string(row, "status", f"market {symbol}")
            if status != "active":
                raise ValueError(f"Lighter market {symbol!r} is not active")
            market_id = _require_integer(row, "market_id", f"market {symbol}")
            if market_id < 0:
                raise ValueError("market_id must be nonnegative")
            if market_id in selected_market_ids:
                raise ValueError("requested Lighter markets must have unique market_id values")
            selected_market_ids.add(market_id)
            selected.append((asset, symbol, market_id, row))
        if missing:
            raise ValueError(f"market response is missing requested symbols: {','.join(missing)}")
        return received, tuple(selected)

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
            raise ValueError(f"Lighter endpoint {endpoint} response is not valid UTF-8") from error
        try:
            value = json.loads(decoded, object_pairs_hook=_unique_json_object)
        except json.JSONDecodeError as error:
            raise ValueError(f"Lighter endpoint {endpoint} response is not valid JSON") from error
        document = _require_mapping(value, f"Lighter endpoint {endpoint} response")
        code = _require_integer(document, "code", f"Lighter endpoint {endpoint} response")
        if code != 200:
            raise ValueError(f"Lighter endpoint {endpoint} returned API code {code}")
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


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Lighter response contains a duplicate JSON object key")
        result[key] = value
    return result


def _instrument_spec(
    asset: Asset,
    symbol: str,
    row: Mapping[str, object],
    observed_at: datetime,
    source_hash: str,
) -> InstrumentSpec:
    size_decimals = _require_decimal_count(row, "supported_size_decimals", symbol)
    price_decimals = _require_decimal_count(row, "supported_price_decimals", symbol)
    multiplier_value = row.get("multiplier", "1")
    multiplier = _parse_decimal(multiplier_value, f"market {symbol} multiplier")
    min_notional = _require_decimal(row, "min_quote_amount", f"market {symbol}")
    if multiplier <= 0 or min_notional <= 0:
        raise ValueError("instrument multiplier and minimum notional must be positive")
    return InstrumentSpec(
        schema_version=1,
        instrument_id=f"lighter:{symbol}",
        venue=Venue.LIGHTER,
        symbol=symbol,
        asset=asset,
        kind=InstrumentKind.LINEAR_PERPETUAL,
        contract_multiplier=multiplier,
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
        min_notional=min_notional,
        quantity_step=Decimal(1).scaleb(-size_decimals),
        price_tick=Decimal(1).scaleb(-price_decimals),
        is_inverse=False,
        is_prelaunch=False,
        observed_at=observed_at,
        source_hash=source_hash,
    )


def _require_decimal_count(row: Mapping[str, object], key: str, symbol: str) -> int:
    value = _require_integer(row, key, f"market {symbol}")
    if not 0 <= value <= 18:
        raise ValueError(f"market {symbol} {key} must be between 0 and 18")
    return value


def _require_key(row: Mapping[str, object], key: str, context: str) -> object:
    if key not in row:
        raise ValueError(f"{context} is missing {key}")
    return row[key]


def _require_mapping(value: object, context: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{context} must be a mapping")
    return value


def _require_list(value: object, context: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{context} must be a list")
    return value


def _require_string(row: Mapping[str, object], key: str, context: str) -> str:
    value = _require_key(row, key, context)
    if not isinstance(value, str):
        raise ValueError(f"{context} {key} must be a string")
    return value


def _require_integer(row: Mapping[str, object], key: str, context: str) -> int:
    value = _require_key(row, key, context)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{context} {key} must be an integer")
    return value


def _parse_decimal(value: object, context: str) -> Decimal:
    if not isinstance(value, str):
        raise ValueError(f"{context} must be a decimal string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise ValueError(f"{context} must be a decimal string") from error
    if not parsed.is_finite():
        raise ValueError(f"{context} must be finite")
    return parsed


def _require_decimal(row: Mapping[str, object], key: str, context: str) -> Decimal:
    return _parse_decimal(_require_key(row, key, context), f"{context} {key}")
