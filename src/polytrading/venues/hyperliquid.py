from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
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
from polytrading.venues.public import AdapterBatch
from polytrading.venues.recorder import make_raw_envelope

_INFO_URL = "https://api.hyperliquid.xyz/info"
_ENDPOINT = "/info"
_SOURCE_VERSION = "public-info-v1"
_FUNDING_PAGE_LIMIT = 500
_FUNDING_INTERVAL_MILLISECONDS = 3_600_000
_MILLISECONDS_PER_SECOND = 1_000
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class PaginationStalledError(RuntimeError):
    """Raised when a funding-history page cannot advance the request cursor."""


@dataclass(frozen=True)
class _ReceivedResponse:
    payload: bytes
    document: object
    observed_at: datetime
    monotonic_started_ns: int
    monotonic_completed_ns: int

    def raw_envelope(self, *, venue_timestamp: datetime | None) -> RawEnvelope:
        return make_raw_envelope(
            venue=Venue.HYPERLIQUID,
            payload=self.payload,
            endpoint=_ENDPOINT,
            source_version=_SOURCE_VERSION,
            venue_timestamp=venue_timestamp,
            monotonic_started_ns=self.monotonic_started_ns,
            monotonic_completed_ns=self.monotonic_completed_ns,
            observed_at=self.observed_at,
        )


@dataclass(frozen=True)
class _L2Payload:
    effective_at: datetime
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]


class HyperliquidPublicAdapter:
    venue = Venue.HYPERLIQUID

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
        received = await self._post_info({"type": "metaAndAssetCtxs"})
        raw = received.raw_envelope(venue_timestamp=None)
        universe, contexts = _parse_meta_and_contexts(received.document)
        selected = _select_universe(universe, contexts, assets)
        instruments = tuple(
            _instrument_spec(entry, context, asset, received.observed_at, raw.source_hash)
            for entry, context, asset in selected
        )
        return AdapterBatch(raw=(raw,), normalized=instruments)

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

        maximum_rows = (end_ms - start_ms) // _FUNDING_INTERVAL_MILLISECONDS + 1
        request_budget = (maximum_rows + _FUNDING_PAGE_LIMIT - 1) // _FUNDING_PAGE_LIMIT + 1
        cursor = start_ms
        raws: list[RawEnvelope] = []
        observations: dict[int, tuple[FundingObservation, Decimal]] = {}

        for _request_number in range(request_budget):
            received = await self._post_info(
                {
                    "type": "fundingHistory",
                    "coin": asset.value,
                    "startTime": cursor,
                    "endTime": end_ms,
                }
            )
            raw = received.raw_envelope(venue_timestamp=None)
            raws.append(raw)
            rows = _require_list(received.document, "funding history response")
            if not rows:
                break
            if len(rows) > _FUNDING_PAGE_LIMIT:
                raise ValueError("funding history page exceeds the 500-row public limit")

            max_returned_time: int | None = None
            for row in rows:
                mapping = _require_mapping(row, "funding history row")
                coin = _require_string(mapping, "coin", "funding history row")
                if coin != asset.value:
                    raise ValueError(
                        f"funding history row coin {coin!r} does not match {asset.value!r}"
                    )
                timestamp_ms = _require_integer(mapping, "time", "funding history row")
                if timestamp_ms < start_ms or timestamp_ms > end_ms:
                    raise ValueError("funding history row is outside requested range")
                rate = _require_decimal(mapping, "fundingRate", "funding history row")
                max_returned_time = (
                    timestamp_ms
                    if max_returned_time is None
                    else max(max_returned_time, timestamp_ms)
                )
                existing = observations.get(timestamp_ms)
                if existing is not None:
                    if existing[1] != rate:
                        raise ValueError("conflicting duplicate funding observation")
                    continue
                observations[timestamp_ms] = (
                    FundingObservation(
                        schema_version=1,
                        venue=self.venue,
                        symbol=asset.value,
                        asset=asset,
                        rate=rate,
                        interval_hours=Decimal(1),
                        effective_at=_datetime_from_milliseconds(timestamp_ms),
                        observed_at=received.observed_at,
                        source_hash=raw.source_hash,
                    ),
                    rate,
                )

            if max_returned_time is None:
                raise ValueError("non-empty funding history page contains no rows")
            if max_returned_time >= end_ms:
                break
            next_cursor = max_returned_time + 1
            if next_cursor <= cursor:
                raise PaginationStalledError("funding history page made no timestamp progress")
            cursor = next_cursor
        else:
            raise PaginationStalledError("funding history request budget exhausted")

        ordered = tuple(observations[key][0] for key in sorted(observations))
        return AdapterBatch(raw=tuple(raws), normalized=ordered)

    async def fetch_market_snapshots(
        self, assets: frozenset[Asset], observed_at: datetime
    ) -> AdapterBatch:
        """Build quotes from meta contexts plus L2, preserving both exact raw responses.

        ``MarketSnapshot.source_hash`` identifies the primary meta/context response that
        supplies mark, index, and open interest. Each snapshot is nevertheless a batch-level
        derivation: the matching L2 raw in the same ``AdapterBatch`` supplies bid and ask.
        The schema has no multi-source lineage field, so no composite envelope or hash is made.
        """
        _require_collection_context(observed_at)
        meta_response = await self._post_info({"type": "metaAndAssetCtxs"})
        meta_raw = meta_response.raw_envelope(venue_timestamp=None)
        universe, contexts = _parse_meta_and_contexts(meta_response.document)
        selected = _select_universe(universe, contexts, assets)
        selected_assets = {asset for _entry, _context, asset in selected}
        missing = assets - selected_assets
        if missing:
            names = ", ".join(sorted(asset.value for asset in missing))
            raise ValueError(f"requested assets missing from metadata universe: {names}")

        raws = [meta_raw]
        snapshots: list[MarketSnapshot] = []
        for _entry, context, asset in selected:
            l2_response = await self._post_info(
                {"type": "l2Book", "coin": asset.value, "nSigFigs": None}
            )
            l2 = _parse_l2_payload(l2_response.document, asset, l2_response.observed_at)
            l2_raw = l2_response.raw_envelope(venue_timestamp=l2.effective_at)
            raws.append(l2_raw)
            snapshots.append(
                MarketSnapshot(
                    schema_version=1,
                    venue=self.venue,
                    symbol=asset.value,
                    asset=asset,
                    bid=l2.bids[0].price,
                    ask=l2.asks[0].price,
                    mark=_require_decimal(context, "markPx", "asset context"),
                    index=_require_decimal(context, "oraclePx", "asset context"),
                    open_interest=_require_decimal(
                        context, "openInterest", "asset context"
                    ),
                    effective_at=l2.effective_at,
                    observed_at=l2_response.observed_at,
                    source_hash=meta_raw.source_hash,
                )
            )
        return AdapterBatch(raw=tuple(raws), normalized=tuple(snapshots))

    async def fetch_order_books(
        self,
        assets: frozenset[Asset],
        observed_at: datetime,
        cycle_id: UUID,
    ) -> AdapterBatch:
        _require_collection_context(observed_at)
        raws: list[RawEnvelope] = []
        books: list[Level2BookSnapshot] = []
        for asset in sorted(assets, key=lambda item: item.value):
            received = await self._post_info(
                {"type": "l2Book", "coin": asset.value, "nSigFigs": None}
            )
            l2 = _parse_l2_payload(received.document, asset, received.observed_at)
            raw = received.raw_envelope(venue_timestamp=l2.effective_at)
            raws.append(raw)
            books.append(
                Level2BookSnapshot(
                    schema_version=1,
                    cycle_id=cycle_id,
                    venue=self.venue,
                    symbol=asset.value,
                    asset=asset,
                    bids=l2.bids,
                    asks=l2.asks,
                    depth_limit=20,
                    sequence=None,
                    effective_at=l2.effective_at,
                    observed_at=received.observed_at,
                    source_hash=raw.source_hash,
                )
            )
        return AdapterBatch(raw=tuple(raws), normalized=tuple(books))

    async def _post_info(self, body: dict[str, object]) -> _ReceivedResponse:
        monotonic_started_ns = self._monotonic_ns()
        response = await self._client.post(_INFO_URL, json=body)
        payload = response.content
        monotonic_completed_ns = self._monotonic_ns()
        observed_at = normalize_utc_timestamp(self._wall_clock())
        response.raise_for_status()
        try:
            document = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("Hyperliquid response is not valid UTF-8 JSON") from error
        return _ReceivedResponse(
            payload=payload,
            document=document,
            observed_at=observed_at,
            monotonic_started_ns=monotonic_started_ns,
            monotonic_completed_ns=monotonic_completed_ns,
        )


def _require_collection_context(observed_at: datetime) -> None:
    normalize_utc_timestamp(observed_at)


def _parse_meta_and_contexts(
    document: object,
) -> tuple[list[Mapping[str, object]], list[Mapping[str, object]]]:
    outer = _require_list(document, "metaAndAssetCtxs response")
    if len(outer) != 2:
        raise ValueError("metaAndAssetCtxs response must contain metadata and contexts")
    metadata = _require_mapping(outer[0], "metadata")
    universe_values = _require_list(
        _require_key(metadata, "universe", "metadata"), "metadata universe"
    )
    context_values = _require_list(outer[1], "asset contexts")
    if len(universe_values) != len(context_values):
        raise ValueError("metadata universe and asset contexts must align one-to-one")
    universe = [
        _require_mapping(value, f"metadata universe entry {index}")
        for index, value in enumerate(universe_values)
    ]
    contexts = [
        _require_mapping(value, f"asset context {index}")
        for index, value in enumerate(context_values)
    ]
    names = [_require_string(entry, "name", "metadata universe entry") for entry in universe]
    if len(names) != len(set(names)):
        raise ValueError("metadata universe contains duplicate coin names")
    return universe, contexts


def _select_universe(
    universe: Sequence[Mapping[str, object]],
    contexts: Sequence[Mapping[str, object]],
    assets: frozenset[Asset],
) -> list[tuple[Mapping[str, object], Mapping[str, object], Asset]]:
    selected: list[tuple[Mapping[str, object], Mapping[str, object], Asset]] = []
    for index, entry in enumerate(universe):
        name = _require_string(entry, "name", "metadata universe entry")
        try:
            asset = Asset(name)
        except ValueError:
            continue
        if asset in assets:
            selected.append((entry, contexts[index], asset))
    return selected


def _instrument_spec(
    entry: Mapping[str, object],
    context: Mapping[str, object],
    asset: Asset,
    observed_at: datetime,
    source_hash: str,
) -> InstrumentSpec:
    size_decimals = _require_integer(entry, "szDecimals", "metadata universe entry")
    if size_decimals < 0:
        raise ValueError("szDecimals must be non-negative")
    # Context validation applies only after exact requested-asset selection. Unsupported or
    # unrequested contexts remain outside this adapter's evidence boundary.
    for field in ("markPx", "oraclePx", "funding", "openInterest"):
        _require_decimal(context, field, f"{asset.value} asset context")
    return InstrumentSpec(
        schema_version=1,
        instrument_id=f"hyperliquid:{asset.value}",
        venue=Venue.HYPERLIQUID,
        symbol=asset.value,
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
        quantity_step=Decimal(10) ** -size_decimals,
        price_tick=None,
        is_inverse=False,
        is_prelaunch=False,
        observed_at=observed_at,
        source_hash=source_hash,
    )


def _parse_l2_payload(
    document: object, expected_asset: Asset, observed_at: datetime
) -> _L2Payload:
    mapping = _require_mapping(document, "l2Book response")
    coin = _require_string(mapping, "coin", "l2Book response")
    if coin != expected_asset.value:
        raise ValueError(f"l2Book coin {coin!r} does not match {expected_asset.value!r}")
    timestamp_ms = _require_integer(mapping, "time", "l2Book response")
    if timestamp_ms < 0:
        raise ValueError("l2Book timestamp must be non-negative milliseconds")
    effective_at = _datetime_from_milliseconds(timestamp_ms)
    if effective_at > observed_at:
        raise ValueError("l2Book timestamp is after response receipt")
    sides = _require_list(_require_key(mapping, "levels", "l2Book response"), "l2Book levels")
    if len(sides) != 2:
        raise ValueError("l2Book levels must contain bid and ask sides")
    bids = _parse_book_side(sides[0], "bids")
    asks = _parse_book_side(sides[1], "asks")
    if any(left.price <= right.price for left, right in pairwise(bids)):
        raise ValueError("book bids must be in strictly descending price order")
    if any(left.price >= right.price for left, right in pairwise(asks)):
        raise ValueError("book asks must be in strictly ascending price order")
    if bids[0].price >= asks[0].price:
        raise ValueError("book top of book must not cross")
    return _L2Payload(effective_at=effective_at, bids=bids, asks=asks)


def _parse_book_side(value: object, label: str) -> tuple[BookLevel, ...]:
    rows = _require_list(value, f"l2Book {label}")
    if not rows:
        raise ValueError(f"l2Book {label} must not be empty")
    if len(rows) > 20:
        raise ValueError(f"l2Book {label} must contain no more than 20 levels")
    levels: list[BookLevel] = []
    for row in rows:
        mapping = _require_mapping(row, f"l2Book {label} level")
        order_count = _require_integer(mapping, "n", f"l2Book {label} level")
        if order_count <= 0:
            raise ValueError("l2Book order count must be positive")
        levels.append(
            BookLevel(
                price=_require_decimal(mapping, "px", f"l2Book {label} level"),
                quantity=_require_decimal(mapping, "sz", f"l2Book {label} level"),
                order_count=order_count,
            )
        )
    return tuple(levels)


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


def _require_integer(mapping: Mapping[str, object], key: str, label: str) -> int:
    value = _require_key(mapping, key, label)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} key {key!r} must be an integer")
    return value


def _require_decimal(mapping: Mapping[str, object], key: str, label: str) -> Decimal:
    value = _require_string(mapping, key, label)
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise ValueError(f"{label} key {key!r} must be a valid decimal string") from error
    if not parsed.is_finite():
        raise ValueError(f"{label} key {key!r} must be a finite decimal string")
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
