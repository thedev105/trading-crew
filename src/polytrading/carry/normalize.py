from datetime import datetime
from decimal import Decimal

from polytrading.carry.compatibility import compare_contracts
from polytrading.carry.models import FundingSpreadDiagnostic
from polytrading.domain.models import FundingObservation, InstrumentSpec, normalize_utc_timestamp

_HOURS_PER_YEAR = Decimal(8760)


def funding_spread(
    long_observation: FundingObservation, short_observation: FundingObservation
) -> Decimal:
    """Return the signed hourly difference: short funding less long funding."""
    return short_observation.hourly_rate - long_observation.hourly_rate


def compare_latest_funding(
    first_observation: FundingObservation,
    first_instrument: InstrumentSpec,
    second_observation: FundingObservation,
    second_instrument: InstrumentSpec,
    as_of: datetime,
) -> FundingSpreadDiagnostic:
    """Normalize two point-in-time funding observations into a research diagnostic."""
    normalized_as_of = normalize_utc_timestamp(as_of)
    _require_current_and_aligned(first_observation, first_instrument, normalized_as_of)
    _require_current_and_aligned(second_observation, second_instrument, normalized_as_of)
    if first_observation.asset != second_observation.asset:
        raise ValueError("funding observations and instruments must align")
    if first_observation.venue == second_observation.venue:
        raise ValueError("funding comparison requires distinct venues")

    ordered = sorted(
        ((first_observation, first_instrument), (second_observation, second_instrument)),
        key=lambda pair: (pair[0].hourly_rate, pair[0].venue.value, pair[0].symbol),
    )
    (long_observation, long_instrument), (short_observation, short_instrument) = ordered
    hourly_spread = funding_spread(long_observation, short_observation)

    return FundingSpreadDiagnostic(
        schema_version=1,
        asset=long_observation.asset,
        long_venue=long_observation.venue,
        long_symbol=long_observation.symbol,
        short_venue=short_observation.venue,
        short_symbol=short_observation.symbol,
        long_hourly_rate=long_observation.hourly_rate,
        short_hourly_rate=short_observation.hourly_rate,
        hourly_spread=hourly_spread,
        diagnostic_annualized_spread=hourly_spread * _HOURS_PER_YEAR,
        as_of=normalized_as_of,
        compatibility=compare_contracts(long_instrument, short_instrument),
    )


def _require_current_and_aligned(
    observation: FundingObservation, instrument: InstrumentSpec, as_of: datetime
) -> None:
    if observation.effective_at > as_of:
        raise ValueError("funding effective_at must not be after as_of")
    if observation.observed_at > as_of:
        raise ValueError("funding observed_at must not be after as_of")
    if instrument.observed_at > as_of:
        raise ValueError("instrument observed_at must not be after as_of")
    if (
        observation.asset != instrument.asset
        or observation.venue != instrument.venue
        or observation.symbol != instrument.symbol
    ):
        raise ValueError("funding observations and instruments must align")
