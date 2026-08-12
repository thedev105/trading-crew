from datetime import datetime
from decimal import Decimal
from typing import Literal

from polytrading.domain.models import FeeSchedule, Venue, normalize_utc_timestamp
from polytrading.registry import MissingPointInTimeRecordError
from polytrading.storage.store import DuckDBStore


class FeeRegistry:
    def __init__(self, store: DuckDBStore) -> None:
        self._store = store

    def record(self, schedule: FeeSchedule) -> bool:
        return self._store.append_fee_schedule(schedule)

    def as_of(self, venue: Venue, tier_name: str, as_of: datetime) -> FeeSchedule | None:
        return self._store.latest_fee_as_of(
            venue, tier_name, normalize_utc_timestamp(as_of)
        )

    def require_as_of(self, venue: Venue, tier_name: str, as_of: datetime) -> FeeSchedule:
        normalized_as_of = normalize_utc_timestamp(as_of)
        schedule = self.as_of(venue, tier_name, normalized_as_of)
        if schedule is None:
            raise MissingPointInTimeRecordError(
                ("fee", venue.value, tier_name), normalized_as_of
            )
        return schedule

    def calculate(
        self,
        venue: Venue,
        tier_name: str,
        liquidity: Literal["maker", "taker"],
        notional: Decimal,
        as_of: datetime,
    ) -> Decimal:
        if notional < 0:
            raise ValueError("notional must be non-negative")
        schedule = self.require_as_of(venue, tier_name, as_of)
        if notional == 0:
            return Decimal(0)
        if liquidity == "maker":
            rate = schedule.maker_rate
        elif liquidity == "taker":
            rate = schedule.taker_rate
        else:
            raise ValueError("liquidity must be 'maker' or 'taker'")
        return notional * rate
