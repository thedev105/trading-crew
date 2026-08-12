from datetime import datetime

from polytrading.domain.models import InstrumentSpec, Venue, normalize_utc_timestamp
from polytrading.registry import MissingPointInTimeRecordError
from polytrading.storage.store import DuckDBStore


class InstrumentRegistry:
    def __init__(self, store: DuckDBStore) -> None:
        self._store = store

    def record(self, spec: InstrumentSpec) -> bool:
        return self._store.append_instrument(spec)

    def as_of(self, venue: Venue, symbol: str, as_of: datetime) -> InstrumentSpec | None:
        return self._store.latest_instrument_as_of(venue, symbol, normalize_utc_timestamp(as_of))

    def require_as_of(self, venue: Venue, symbol: str, as_of: datetime) -> InstrumentSpec:
        normalized_as_of = normalize_utc_timestamp(as_of)
        record = self.as_of(venue, symbol, normalized_as_of)
        if record is None:
            raise MissingPointInTimeRecordError(
                ("instrument", venue.value, symbol), normalized_as_of
            )
        return record
