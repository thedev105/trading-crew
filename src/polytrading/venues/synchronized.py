from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal, Protocol
from uuid import UUID, uuid4

from pydantic import field_validator, model_validator

from polytrading.domain.models import (
    Asset,
    Level2BookSnapshot,
    StrictRecord,
    Venue,
    normalize_utc_timestamp,
)
from polytrading.venues.public import AdapterBatch, PublicVenueAdapter
from polytrading.venues.recorder import PublicRecordStore, append_normalized


class BookCollectionCycle(StrictRecord):
    schema_version: Literal[1]
    cycle_id: UUID
    assets: tuple[Asset, ...]
    venues: tuple[Venue, ...]
    request_started_at: datetime
    request_completed_at: datetime
    effective_timestamps: tuple[datetime, ...]
    max_effective_skew_ms: Decimal
    status: Literal["complete", "failed", "skew_exceeds_research_target"]
    failure_codes: tuple[str, ...]
    source_hashes: tuple[str, ...]

    @field_validator("assets")
    @classmethod
    def canonicalize_assets(cls, values: tuple[Asset, ...]) -> tuple[Asset, ...]:
        return tuple(sorted(set(values), key=lambda item: item.value))

    @field_validator("venues")
    @classmethod
    def canonicalize_venues(cls, values: tuple[Venue, ...]) -> tuple[Venue, ...]:
        return tuple(sorted(set(values), key=lambda item: item.value))

    @field_validator("request_started_at", "request_completed_at")
    @classmethod
    def require_request_timestamp_utc(cls, value: datetime) -> datetime:
        return normalize_utc_timestamp(value)

    @field_validator("effective_timestamps")
    @classmethod
    def require_effective_timestamps_utc(
        cls, values: tuple[datetime, ...]
    ) -> tuple[datetime, ...]:
        return tuple(sorted(normalize_utc_timestamp(value) for value in values))

    @field_validator("max_effective_skew_ms")
    @classmethod
    def require_nonnegative_skew(cls, value: Decimal) -> Decimal:
        if value < 0:
            raise ValueError("effective timestamp skew must be nonnegative")
        return value

    @model_validator(mode="after")
    def require_ordered_request_window(self) -> BookCollectionCycle:
        if self.request_completed_at < self.request_started_at:
            raise ValueError("request completion must not precede request start")
        return self


class BookCollectionStore(PublicRecordStore, Protocol):
    def append_book_collection_cycle(self, record: BookCollectionCycle) -> bool: ...


class SynchronizedBookCollector:
    def __init__(
        self,
        store: BookCollectionStore,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        cycle_id_factory: Callable[[], UUID] = uuid4,
        research_skew_target_ms: Decimal = Decimal(1_000),
    ) -> None:
        self._store = store
        self._clock = clock
        self._cycle_id_factory = cycle_id_factory
        self._research_skew_target_ms = research_skew_target_ms

    async def collect_once(
        self,
        adapters: Iterable[PublicVenueAdapter],
        assets: frozenset[Asset],
        observed_at: datetime,
    ) -> BookCollectionCycle:
        requested_assets = frozenset(assets)
        if not requested_assets:
            raise ValueError("at least one asset is required")
        ordered_adapters = tuple(sorted(adapters, key=lambda adapter: adapter.venue.value))
        if not ordered_adapters:
            raise ValueError("at least one public adapter is required")
        adapter_venues = tuple(adapter.venue for adapter in ordered_adapters)
        if len(set(adapter_venues)) != len(adapter_venues):
            raise ValueError("public adapters must have unique venues")

        cycle_id = self._cycle_id_factory()
        request_started_at = normalize_utc_timestamp(self._clock())
        results = await asyncio.gather(
            *(
                adapter.fetch_order_books(requested_assets, observed_at, cycle_id)
                for adapter in ordered_adapters
            ),
            return_exceptions=True,
        )
        request_completed_at = normalize_utc_timestamp(self._clock())

        successful_batches: list[tuple[Venue, AdapterBatch]] = []
        failure_codes: list[str] = []
        for adapter, result in zip(ordered_adapters, results, strict=True):
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, BaseException):
                failure_codes.append(f"{adapter.venue.value}:{type(result).__name__}")
                continue
            batch_failure = self._validate_book_batch(
                result, adapter.venue, requested_assets, cycle_id
            )
            if batch_failure is not None:
                failure_codes.append(f"{adapter.venue.value}:{batch_failure}")
            successful_batches.append((adapter.venue, result))

        raw_records = tuple(
            sorted(
                (raw for _, batch in successful_batches for raw in batch.raw),
                key=lambda raw: (
                    raw.venue.value,
                    raw.endpoint,
                    raw.source_hash,
                    str(raw.event_id),
                ),
            )
        )
        books = tuple(
            sorted(
                (
                    record
                    for _, batch in successful_batches
                    for record in batch.normalized
                    if type(record) is Level2BookSnapshot
                ),
                key=lambda record: (record.venue.value, record.asset.value, record.symbol),
            )
        )
        effective_timestamps = tuple(record.effective_at for record in books)
        max_effective_skew_ms = _max_timestamp_skew_ms(effective_timestamps)
        if failure_codes:
            status = "failed"
        elif max_effective_skew_ms > self._research_skew_target_ms:
            status = "skew_exceeds_research_target"
        else:
            status = "complete"
        cycle = BookCollectionCycle(
            schema_version=1,
            cycle_id=cycle_id,
            assets=tuple(requested_assets),
            venues=adapter_venues,
            request_started_at=request_started_at,
            request_completed_at=request_completed_at,
            effective_timestamps=effective_timestamps,
            max_effective_skew_ms=max_effective_skew_ms,
            status=status,
            failure_codes=tuple(sorted(failure_codes)),
            source_hashes=tuple(raw.source_hash for raw in raw_records),
        )

        with self._store.transaction() as transaction:
            for raw in raw_records:
                transaction.append_raw(raw)
            if cycle.status != "failed":
                for book in books:
                    append_normalized(transaction, book)
            transaction.append_book_collection_cycle(cycle)
        return cycle

    @staticmethod
    def _validate_book_batch(
        batch: AdapterBatch,
        venue: Venue,
        requested_assets: frozenset[Asset],
        cycle_id: UUID,
    ) -> str | None:
        books = batch.normalized
        if any(type(record) is not Level2BookSnapshot for record in books):
            return "invalid_normalized_record"
        typed_books = tuple(record for record in books if type(record) is Level2BookSnapshot)
        if any(record.venue is not venue for record in typed_books):
            return "venue_mismatch"
        if any(record.cycle_id != cycle_id for record in typed_books):
            return "cycle_id_mismatch"
        returned_assets = tuple(record.asset for record in typed_books)
        if (
            len(returned_assets) != len(requested_assets)
            or set(returned_assets) != requested_assets
        ):
            return "asset_coverage_mismatch"
        return None


def _max_timestamp_skew_ms(values: tuple[datetime, ...]) -> Decimal:
    if len(values) < 2:
        return Decimal(0)
    elapsed: timedelta = max(values) - min(values)
    elapsed_microseconds = (
        (elapsed.days * 86_400 + elapsed.seconds) * 1_000_000 + elapsed.microseconds
    )
    return Decimal(elapsed_microseconds) / Decimal(1_000)
