from datetime import UTC, datetime, timedelta
from decimal import Decimal

from polytrading.domain.models import Asset, FundingObservation, Venue


def at(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def funding_row(
    venue: Venue,
    effective_at: datetime,
    *,
    observed_at: datetime | None = None,
    rate: Decimal = Decimal("0.00002"),
    interval_hours: Decimal | None = None,
    asset: Asset = Asset.BTC,
    symbol: str | None = None,
    source_hash: str = "a" * 64,
) -> FundingObservation:
    return FundingObservation(
        schema_version=1,
        venue=venue,
        symbol=symbol or (f"{asset.value}USDT" if venue is Venue.BYBIT else asset.value),
        asset=asset,
        rate=rate,
        interval_hours=interval_hours or (Decimal(8) if venue is Venue.BYBIT else Decimal(1)),
        effective_at=effective_at,
        observed_at=observed_at or effective_at + timedelta(minutes=1),
        source_hash=source_hash,
    )


def complete_block(
    start: datetime,
    *,
    bybit_rate: Decimal = Decimal("0.00008"),
    hyperliquid_hourly_rate: Decimal = Decimal("0.00002"),
    observation_lag: timedelta = timedelta(minutes=1),
) -> tuple[FundingObservation, ...]:
    end = start + timedelta(hours=8)
    bybit = funding_row(
        Venue.BYBIT,
        end,
        observed_at=end + observation_lag,
        rate=bybit_rate,
    )
    hyperliquid = tuple(
        funding_row(
            Venue.HYPERLIQUID,
            start + timedelta(hours=hour),
            observed_at=start + timedelta(hours=hour) + observation_lag,
            rate=hyperliquid_hourly_rate,
        )
        for hour in range(1, 9)
    )
    return (bybit, *hyperliquid)
