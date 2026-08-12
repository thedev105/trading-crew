from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from polytrading.carry.normalize import compare_latest_funding, funding_spread
from polytrading.domain.models import Asset, FundingObservation, InstrumentSpec, Venue
from tests.domain.factories import NOW, SOURCE_HASH, instrument_spec


def funding_observation(
    *,
    venue: Venue,
    symbol: str,
    rate: Decimal,
    interval_hours: Decimal,
    asset: Asset = Asset.BTC,
    effective_at: datetime = NOW,
    observed_at: datetime = NOW,
) -> FundingObservation:
    return FundingObservation(
        schema_version=1,
        venue=venue,
        symbol=symbol,
        asset=asset,
        rate=rate,
        interval_hours=interval_hours,
        effective_at=effective_at,
        observed_at=observed_at,
        source_hash=SOURCE_HASH,
    )


def paired_instrument(*, venue: Venue, symbol: str, asset: Asset = Asset.BTC) -> InstrumentSpec:
    return instrument_spec(
        instrument_id=f"{venue.value}:{symbol}", venue=venue, symbol=symbol, asset=asset
    )


def test_positive_funding_assigns_the_higher_hourly_rate_to_short() -> None:
    bybit = funding_observation(
        venue=Venue.BYBIT,
        symbol="BTCUSDT",
        rate=Decimal("0.0008"),
        interval_hours=Decimal("8"),
    )
    hyperliquid = funding_observation(
        venue=Venue.HYPERLIQUID,
        symbol="BTC",
        rate=Decimal("0.0002"),
        interval_hours=Decimal("1"),
    )

    diagnostic = compare_latest_funding(
        bybit,
        paired_instrument(venue=Venue.BYBIT, symbol="BTCUSDT"),
        hyperliquid,
        paired_instrument(venue=Venue.HYPERLIQUID, symbol="BTC"),
        NOW,
    )

    assert diagnostic.long_venue is Venue.BYBIT
    assert diagnostic.short_venue is Venue.HYPERLIQUID
    assert diagnostic.long_hourly_rate == Decimal("0.0001")
    assert diagnostic.short_hourly_rate == Decimal("0.0002")
    assert diagnostic.hourly_spread == Decimal("0.0001")
    assert diagnostic.diagnostic_annualized_spread == Decimal("0.8760")
    assert diagnostic.forecast_status == "not_evaluated"


def test_negative_funding_rates_preserve_their_signs() -> None:
    long = funding_observation(
        venue=Venue.BYBIT,
        symbol="BTCUSDT",
        rate=Decimal("-0.0008"),
        interval_hours=Decimal("8"),
    )
    short = funding_observation(
        venue=Venue.HYPERLIQUID,
        symbol="BTC",
        rate=Decimal("-0.00005"),
        interval_hours=Decimal("1"),
    )

    diagnostic = compare_latest_funding(
        long,
        paired_instrument(venue=Venue.BYBIT, symbol="BTCUSDT"),
        short,
        paired_instrument(venue=Venue.HYPERLIQUID, symbol="BTC"),
        NOW,
    )

    assert diagnostic.long_hourly_rate == Decimal("-0.0001")
    assert diagnostic.short_hourly_rate == Decimal("-0.00005")
    assert diagnostic.hourly_spread == Decimal("0.00005")


def test_equal_hourly_rates_choose_long_by_venue_then_symbol() -> None:
    hyperliquid = funding_observation(
        venue=Venue.HYPERLIQUID,
        symbol="BTC",
        rate=Decimal("0.0001"),
        interval_hours=Decimal("1"),
    )
    bybit = funding_observation(
        venue=Venue.BYBIT,
        symbol="BTCUSDT",
        rate=Decimal("0.0008"),
        interval_hours=Decimal("8"),
    )

    diagnostic = compare_latest_funding(
        hyperliquid,
        paired_instrument(venue=Venue.HYPERLIQUID, symbol="BTC"),
        bybit,
        paired_instrument(venue=Venue.BYBIT, symbol="BTCUSDT"),
        NOW,
    )

    assert diagnostic.long_venue is Venue.BYBIT
    assert diagnostic.long_symbol == "BTCUSDT"
    assert diagnostic.short_venue is Venue.HYPERLIQUID
    assert diagnostic.hourly_spread == Decimal("0")


@given(
    long_rate=st.decimals(min_value=Decimal("-1"), max_value=Decimal("1"), places=6),
    short_rate=st.decimals(min_value=Decimal("-1"), max_value=Decimal("1"), places=6),
)
def test_swapping_funding_spread_legs_negates_the_spread(
    long_rate: Decimal, short_rate: Decimal
) -> None:
    # Catches an absolute-value or canonical-sorting implementation of the pure spread helper.
    long = funding_observation(
        venue=Venue.BYBIT,
        symbol="BTCUSDT",
        rate=long_rate,
        interval_hours=Decimal("1"),
    )
    short = funding_observation(
        venue=Venue.HYPERLIQUID,
        symbol="BTC",
        rate=short_rate,
        interval_hours=Decimal("1"),
    )

    assert funding_spread(long, short) == -funding_spread(short, long)


@pytest.mark.parametrize(
    ("observation", "instrument", "expected_message"),
    [
        (
            "future_effective",
            "current",
            "funding effective_at must not be after as_of",
        ),
        (
            "future_observed",
            "current",
            "funding observed_at must not be after as_of",
        ),
        (
            "current",
            "future_observed",
            "instrument observed_at must not be after as_of",
        ),
    ],
)
def test_future_known_records_are_rejected(
    observation: str, instrument: str, expected_message: str
) -> None:
    future = NOW + timedelta(seconds=1)
    bybit = funding_observation(
        venue=Venue.BYBIT,
        symbol="BTCUSDT",
        rate=Decimal("0.0001"),
        interval_hours=Decimal("1"),
        effective_at=future if observation == "future_effective" else NOW,
        observed_at=future if observation == "future_observed" else NOW,
    )
    bybit_instrument = paired_instrument(
        venue=Venue.BYBIT,
        symbol="BTCUSDT",
    ).model_copy(update={"observed_at": future if instrument == "future_observed" else NOW})

    with pytest.raises(ValueError, match=expected_message):
        compare_latest_funding(
            bybit,
            bybit_instrument,
            funding_observation(
                venue=Venue.HYPERLIQUID,
                symbol="BTC",
                rate=Decimal("0.0002"),
                interval_hours=Decimal("1"),
            ),
            paired_instrument(venue=Venue.HYPERLIQUID, symbol="BTC"),
            NOW,
        )


@pytest.mark.parametrize(
    ("observation_update", "instrument_update"),
    [
        ({"asset": Asset.ETH}, {"asset": Asset.ETH}),
        ({}, {"asset": Asset.ETH}),
        ({}, {"venue": Venue.HYPERLIQUID}),
        ({}, {"symbol": "BTC"}),
    ],
)
def test_funding_and_instrument_identity_must_match(
    observation_update: dict[str, object], instrument_update: dict[str, object]
) -> None:
    bybit = funding_observation(
        venue=Venue.BYBIT,
        symbol="BTCUSDT",
        rate=Decimal("0.0001"),
        interval_hours=Decimal("1"),
    ).model_copy(update=observation_update)
    bybit_instrument = paired_instrument(venue=Venue.BYBIT, symbol="BTCUSDT").model_copy(
        update=instrument_update
    )

    with pytest.raises(ValueError, match="funding observations and instruments must align"):
        compare_latest_funding(
            bybit,
            bybit_instrument,
            funding_observation(
                venue=Venue.HYPERLIQUID,
                symbol="BTC",
                rate=Decimal("0.0002"),
                interval_hours=Decimal("1"),
            ),
            paired_instrument(venue=Venue.HYPERLIQUID, symbol="BTC"),
            NOW,
        )


def test_as_of_must_be_timezone_aware() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        compare_latest_funding(
            funding_observation(
                venue=Venue.BYBIT,
                symbol="BTCUSDT",
                rate=Decimal("0.0001"),
                interval_hours=Decimal("1"),
            ),
            paired_instrument(venue=Venue.BYBIT, symbol="BTCUSDT"),
            funding_observation(
                venue=Venue.HYPERLIQUID,
                symbol="BTC",
                rate=Decimal("0.0002"),
                interval_hours=Decimal("1"),
            ),
            paired_instrument(venue=Venue.HYPERLIQUID, symbol="BTC"),
            datetime(2026, 8, 12, 12),
        )
