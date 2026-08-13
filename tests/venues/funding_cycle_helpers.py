from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from polytrading.domain.models import (
    Asset,
    FundingObservation,
    InstrumentKind,
    InstrumentSpec,
    RawEnvelope,
    Venue,
)
from polytrading.venues.public import AdapterBatch

CYCLE_END = datetime(2026, 8, 13, 17, tzinfo=UTC)
ASSETS = frozenset({Asset.BTC, Asset.ETH, Asset.SOL})


def raw_envelope(
    venue: Venue,
    *,
    label: str,
    observed_at: datetime,
    event_int: int,
) -> RawEnvelope:
    payload = f'{{"venue":"{venue.value}","label":"{label}"}}'
    return RawEnvelope(
        schema_version=1,
        event_id=UUID(int=event_int),
        venue=venue,
        endpoint=f"/{venue.value}/public/{label}",
        venue_timestamp=None,
        observed_at=observed_at,
        received_monotonic_ns=event_int,
        request_latency_ms=Decimal("1"),
        source_version="fixture-v1",
        payload_json=payload,
        source_hash=hashlib.sha256(payload.encode()).hexdigest(),
    )


def instrument_spec(
    venue: Venue,
    asset: Asset,
    *,
    observed_at: datetime,
    source_hash: str,
) -> InstrumentSpec:
    symbol = f"{asset.value}USDT" if venue is Venue.BYBIT else asset.value
    return InstrumentSpec(
        schema_version=1,
        instrument_id=f"{venue.value}:{symbol}:linear_perpetual",
        venue=venue,
        symbol=symbol,
        asset=asset,
        kind=InstrumentKind.LINEAR_PERPETUAL,
        contract_multiplier=Decimal("1"),
        index_family=None,
        oracle_family=None,
        mark_method=None,
        liquidation_method=None,
        collateral_asset="USDT" if venue is Venue.BYBIT else "USDC",
        pnl_asset="USDT" if venue is Venue.BYBIT else "USDC",
        funding_formula_id=None,
        funding_cap=None,
        funding_interval_hours=Decimal("8") if venue is Venue.BYBIT else Decimal("1"),
        funding_payment_offset_minutes=None,
        min_notional=Decimal("5"),
        quantity_step=Decimal("0.001"),
        price_tick=Decimal("0.1"),
        is_inverse=False,
        is_prelaunch=False,
        observed_at=observed_at,
        source_hash=source_hash,
    )


def funding_observation(
    venue: Venue,
    asset: Asset,
    *,
    effective_at: datetime,
    observed_at: datetime,
    source_hash: str,
) -> FundingObservation:
    return FundingObservation(
        schema_version=1,
        venue=venue,
        symbol=f"{asset.value}USDT" if venue is Venue.BYBIT else asset.value,
        asset=asset,
        rate=Decimal("0.0001"),
        interval_hours=Decimal("8") if venue is Venue.BYBIT else Decimal("1"),
        effective_at=effective_at,
        observed_at=observed_at,
        source_hash=source_hash,
    )


def instrument_batch(
    venue: Venue,
    assets: frozenset[Asset],
    *,
    observed_at: datetime,
    event_int: int,
) -> AdapterBatch:
    raw = raw_envelope(
        venue,
        label="instruments",
        observed_at=observed_at,
        event_int=event_int,
    )
    return AdapterBatch(
        raw=(raw,),
        normalized=tuple(
            instrument_spec(
                venue,
                asset,
                observed_at=observed_at,
                source_hash=raw.source_hash,
            )
            for asset in sorted(assets, key=lambda item: item.value)
        ),
    )


def funding_batch(
    venue: Venue,
    asset: Asset,
    *,
    effective_at: datetime,
    observed_at: datetime,
    event_int: int,
    include_record: bool = True,
) -> AdapterBatch:
    raw = raw_envelope(
        venue,
        label=f"funding-{asset.value}",
        observed_at=observed_at,
        event_int=event_int,
    )
    normalized = (
        (
            funding_observation(
                venue,
                asset,
                effective_at=effective_at,
                observed_at=observed_at,
                source_hash=raw.source_hash,
            ),
        )
        if include_record
        else ()
    )
    return AdapterBatch(raw=(raw,), normalized=normalized)


class FakeFundingAdapter:
    def __init__(
        self,
        venue: Venue,
        instrument_result: AdapterBatch | BaseException,
        funding_results: dict[Asset, AdapterBatch | BaseException],
    ) -> None:
        self.venue = venue
        self.instrument_result = instrument_result
        self.funding_results = funding_results
        self.instrument_calls: list[tuple[frozenset[Asset], datetime]] = []
        self.funding_calls: list[tuple[Asset, datetime, datetime, datetime]] = []

    async def fetch_instruments(
        self, assets: frozenset[Asset], observed_at: datetime
    ) -> AdapterBatch:
        self.instrument_calls.append((assets, observed_at))
        if isinstance(self.instrument_result, BaseException):
            raise self.instrument_result
        return self.instrument_result

    async def fetch_funding_history(
        self,
        asset: Asset,
        start: datetime,
        end: datetime,
        observed_at: datetime,
    ) -> AdapterBatch:
        self.funding_calls.append((asset, start, end, observed_at))
        result = self.funding_results[asset]
        if isinstance(result, BaseException):
            raise result
        return result

    async def fetch_market_snapshots(self, *args: object, **kwargs: object) -> AdapterBatch:
        raise AssertionError("unexpected adapter method")

    async def fetch_order_books(self, *args: object, **kwargs: object) -> AdapterBatch:
        raise AssertionError("unexpected adapter method")


class SequenceClock:
    def __init__(self, *values: datetime) -> None:
        self._values = iter(values)

    def __call__(self) -> datetime:
        return next(self._values)
