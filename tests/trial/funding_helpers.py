from datetime import UTC, datetime, timedelta
from uuid import UUID

from polytrading.domain.models import Asset, Venue
from polytrading.trial.funding_models import (
    TRIAL_FUNDING_PROTOCOL_VERSION,
    TRIAL_FUNDING_WARNINGS,
    LighterDydxFundingCycle,
    LighterDydxFundingItem,
    TrialFundingCycleStatus,
    TrialFundingOutcome,
    TrialInstrumentOutcome,
)

CYCLE_END = datetime(2026, 8, 14, 7, tzinfo=UTC)
CYCLE_ID = UUID("00000000-0000-0000-0000-000000000901")


def trial_funding_item(
    *,
    venue: Venue = Venue.DYDX,
    asset: Asset = Asset.BTC,
    cycle_end: datetime = CYCLE_END,
) -> LighterDydxFundingItem:
    symbol = f"{asset.value}-USD" if venue is Venue.DYDX else asset.value
    return LighterDydxFundingItem(
        schema_version=1,
        venue=venue,
        asset=asset,
        symbol=symbol,
        instrument_outcome=TrialInstrumentOutcome.CAPTURED,
        funding_outcome=TrialFundingOutcome.CAPTURED,
        instrument_observed_at=cycle_end + timedelta(seconds=11),
        funding_effective_at=cycle_end,
        funding_observed_at=cycle_end + timedelta(seconds=12),
        instrument_source_hashes=("1" * 64,),
        funding_source_hashes=("2" * 64,),
        reason_codes=(),
    )


def trial_funding_cycle(
    *,
    cycle_id: UUID = CYCLE_ID,
    cycle_end: datetime = CYCLE_END,
    request_started_at: datetime | None = None,
    request_completed_at: datetime | None = None,
    items: tuple[LighterDydxFundingItem, ...] | None = None,
    status: TrialFundingCycleStatus = TrialFundingCycleStatus.COMPLETE,
) -> LighterDydxFundingCycle:
    selected = (
        tuple(
            trial_funding_item(venue=venue, asset=asset, cycle_end=cycle_end)
            for venue in (Venue.DYDX, Venue.LIGHTER)
            for asset in (Asset.BTC, Asset.ETH, Asset.SOL)
        )
        if items is None
        else items
    )
    return LighterDydxFundingCycle(
        schema_version=1,
        protocol_version=TRIAL_FUNDING_PROTOCOL_VERSION,
        cycle_id=cycle_id,
        cycle_end=cycle_end,
        assets=(Asset.BTC, Asset.ETH, Asset.SOL),
        venues=(Venue.DYDX, Venue.LIGHTER),
        request_started_at=request_started_at or cycle_end + timedelta(seconds=10),
        request_completed_at=request_completed_at or cycle_end + timedelta(seconds=20),
        items=selected,
        status=status,
        source_hashes=tuple(
            sorted(
                {
                    value
                    for item in selected
                    for value in (*item.instrument_source_hashes, *item.funding_source_hashes)
                }
            )
        ),
        warnings=TRIAL_FUNDING_WARNINGS,
    )
