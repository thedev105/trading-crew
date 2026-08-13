from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from uuid import UUID

from polytrading.domain.models import Asset, Venue
from polytrading.venues.funding_cycle_models import (
    FUNDING_CYCLE_PROTOCOL_VERSION,
    FUNDING_CYCLE_WARNINGS,
    FundingCaptureOutcome,
    FundingCollectionCycle,
    FundingCycleItem,
    FundingCycleStatus,
    InstrumentCaptureOutcome,
)

HEALTH_AS_OF = datetime(2026, 8, 14, 17, 6, tzinfo=UTC)
LATEST_BOUNDARY = datetime(2026, 8, 14, 17, tzinfo=UTC)


def _source_hash(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def funding_cycle(
    cycle_end: datetime,
    status: FundingCycleStatus,
    *,
    cycle_int: int,
    completed_offset: timedelta = timedelta(minutes=2),
) -> FundingCollectionCycle:
    cycle_id = UUID(int=cycle_int)
    label = f"{cycle_end.isoformat()}:{status.value}:{cycle_id}"
    bybit_instrument_hash = _source_hash(f"{label}:bybit:instrument")
    bybit_funding_hash = _source_hash(f"{label}:bybit:funding")
    hyperliquid_instrument_hash = _source_hash(f"{label}:hyperliquid:instrument")
    hyperliquid_funding_hash = _source_hash(f"{label}:hyperliquid:funding")

    if status is FundingCycleStatus.LATE:
        request_started_at = cycle_end + timedelta(minutes=6)
        request_completed_at = max(
            cycle_end + completed_offset,
            request_started_at,
        )
        items = tuple(
            FundingCycleItem(
                schema_version=1,
                venue=venue,
                asset=Asset.BTC,
                symbol="BTCUSDT" if venue is Venue.BYBIT else "BTC",
                instrument_outcome=InstrumentCaptureOutcome.LATE_NOT_COLLECTED,
                funding_outcome=FundingCaptureOutcome.LATE_NOT_COLLECTED,
                instrument_observed_at=None,
                funding_effective_at=None,
                funding_observed_at=None,
                instrument_source_hashes=(),
                funding_source_hashes=(),
                reason_codes=("COLLECTION_WINDOW_MISSED",),
            )
            for venue in (Venue.BYBIT, Venue.HYPERLIQUID)
        )
    else:
        request_started_at = cycle_end + timedelta(seconds=30)
        request_completed_at = cycle_end + completed_offset
        bybit_degraded = status is FundingCycleStatus.DEGRADED
        items = (
            FundingCycleItem(
                schema_version=1,
                venue=Venue.BYBIT,
                asset=Asset.BTC,
                symbol="BTCUSDT",
                instrument_outcome=InstrumentCaptureOutcome.CAPTURED,
                funding_outcome=(
                    FundingCaptureOutcome.BOOTSTRAP_REQUIRED
                    if bybit_degraded
                    else FundingCaptureOutcome.NO_SETTLEMENT
                ),
                instrument_observed_at=cycle_end + timedelta(minutes=1),
                funding_effective_at=None,
                funding_observed_at=(None if bybit_degraded else cycle_end + timedelta(minutes=1)),
                instrument_source_hashes=(bybit_instrument_hash,),
                funding_source_hashes=() if bybit_degraded else (bybit_funding_hash,),
                reason_codes=(("BYBIT_INSTRUMENT_BOOTSTRAP_REQUIRED",) if bybit_degraded else ()),
            ),
            FundingCycleItem(
                schema_version=1,
                venue=Venue.HYPERLIQUID,
                asset=Asset.BTC,
                symbol="BTC",
                instrument_outcome=InstrumentCaptureOutcome.CAPTURED,
                funding_outcome=FundingCaptureOutcome.CAPTURED,
                instrument_observed_at=cycle_end + timedelta(minutes=1),
                funding_effective_at=cycle_end,
                funding_observed_at=cycle_end + timedelta(minutes=1),
                instrument_source_hashes=(hyperliquid_instrument_hash,),
                funding_source_hashes=(hyperliquid_funding_hash,),
                reason_codes=(),
            ),
        )

    source_hashes = tuple(
        sorted(
            {
                source_hash
                for item in items
                for source_hash in (
                    *item.instrument_source_hashes,
                    *item.funding_source_hashes,
                )
            }
        )
    )
    return FundingCollectionCycle(
        schema_version=1,
        protocol_version=FUNDING_CYCLE_PROTOCOL_VERSION,
        cycle_id=cycle_id,
        cycle_end=cycle_end,
        assets=(Asset.BTC,),
        venues=(Venue.BYBIT, Venue.HYPERLIQUID),
        request_started_at=request_started_at,
        request_completed_at=request_completed_at,
        items=items,
        status=status,
        source_hashes=source_hashes,
        warnings=FUNDING_CYCLE_WARNINGS,
    )
