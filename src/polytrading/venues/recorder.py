from __future__ import annotations

from contextlib import AbstractContextManager
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
from typing import Protocol
from uuid import UUID, uuid4

from polytrading.domain.models import (
    FundingObservation,
    InstrumentSpec,
    Level2BookSnapshot,
    MarketSnapshot,
    RawEnvelope,
    Venue,
)
from polytrading.venues.public import AdapterBatch, NormalizedRecord


class PublicRecordStore(Protocol):
    def transaction(self) -> AbstractContextManager[PublicRecordStore]: ...

    def append_raw(self, record: RawEnvelope) -> bool: ...

    def append_instrument(self, record: InstrumentSpec) -> bool: ...

    def append_funding(self, record: FundingObservation) -> bool: ...

    def append_market_snapshot(self, record: MarketSnapshot) -> bool: ...

    def append_book_snapshot(self, record: Level2BookSnapshot) -> bool: ...


def make_raw_envelope(
    *,
    venue: Venue,
    payload: bytes,
    endpoint: str,
    source_version: str,
    venue_timestamp: datetime | None,
    monotonic_started_ns: int,
    monotonic_completed_ns: int,
    observed_at: datetime,
    event_id: UUID | None = None,
) -> RawEnvelope:
    payload_json = payload.decode("utf-8")
    elapsed_ns = monotonic_completed_ns - monotonic_started_ns
    if elapsed_ns < 0:
        raise ValueError("monotonic completion must not precede start")
    return RawEnvelope(
        schema_version=1,
        event_id=event_id or uuid4(),
        venue=venue,
        endpoint=endpoint,
        venue_timestamp=venue_timestamp,
        observed_at=observed_at,
        received_monotonic_ns=monotonic_completed_ns,
        request_latency_ms=Decimal(elapsed_ns) / Decimal(1_000_000),
        source_version=source_version,
        payload_json=payload_json,
        source_hash=sha256(payload).hexdigest(),
    )


def append_normalized(store: PublicRecordStore, record: NormalizedRecord) -> bool:
    if type(record) is InstrumentSpec:
        return store.append_instrument(record)
    if type(record) is FundingObservation:
        return store.append_funding(record)
    if type(record) is MarketSnapshot:
        return store.append_market_snapshot(record)
    if type(record) is Level2BookSnapshot:
        return store.append_book_snapshot(record)
    raise TypeError(f"unsupported normalized record type: {type(record).__name__}")


class PublicRecorder:
    def __init__(self, store: PublicRecordStore) -> None:
        self._store = store

    def record(self, batch: AdapterBatch) -> None:
        with self._store.transaction() as transaction:
            for raw in batch.raw:
                transaction.append_raw(raw)
            for normalized in batch.normalized:
                append_normalized(transaction, normalized)
