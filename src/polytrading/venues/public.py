from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Protocol
from uuid import UUID

from polytrading.domain.models import (
    Asset,
    FundingObservation,
    InstrumentSpec,
    Level2BookSnapshot,
    MarketSnapshot,
    RawEnvelope,
    Venue,
)

type NormalizedRecord = InstrumentSpec | FundingObservation | MarketSnapshot | Level2BookSnapshot


class AdapterBatchIntegrityError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class AdapterWarning:
    code: str
    venue: Venue
    endpoint: str
    symbol: str
    message: str

    def __post_init__(self) -> None:
        for field_name in ("code", "endpoint", "symbol", "message"):
            if not isinstance(getattr(self, field_name), str):
                raise TypeError(f"{field_name} must be a string")
        if not isinstance(self.venue, Venue):
            raise TypeError("venue must be a Venue")


@dataclass(frozen=True)
class AdapterBatch:
    raw: tuple[RawEnvelope, ...]
    normalized: tuple[NormalizedRecord, ...]
    warnings: tuple[AdapterWarning, ...] = ()


def validate_adapter_batch(batch: AdapterBatch) -> None:
    for raw in batch.raw:
        actual_hash = sha256(raw.payload_json.encode("utf-8")).hexdigest()
        if raw.source_hash != actual_hash:
            raise AdapterBatchIntegrityError(
                "raw_source_hash_mismatch",
                "raw source hash does not match exact UTF-8 payload",
            )
    raw_lineage = {(item.venue, item.source_hash) for item in batch.raw}
    for item in batch.normalized:
        if type(item) not in (
            InstrumentSpec,
            FundingObservation,
            MarketSnapshot,
            Level2BookSnapshot,
        ):
            raise TypeError(f"unsupported normalized record type: {type(item).__name__}")
        if (item.venue, item.source_hash) not in raw_lineage:
            raise AdapterBatchIntegrityError(
                "normalized_lineage_mismatch",
                "normalized lineage must reference a same-venue raw source hash in its batch",
            )


class PublicVenueAdapter(Protocol):
    venue: Venue

    async def fetch_instruments(
        self, assets: frozenset[Asset], observed_at: datetime
    ) -> AdapterBatch:
        """Fetch public point-in-time instrument specifications."""
        ...

    async def fetch_funding_history(
        self, asset: Asset, start: datetime, end: datetime, observed_at: datetime
    ) -> AdapterBatch:
        """Fetch public realized funding observations in the closed interval."""
        ...

    async def fetch_market_snapshots(
        self, assets: frozenset[Asset], observed_at: datetime
    ) -> AdapterBatch:
        """Fetch public top-of-book, mark, index, and open-interest snapshots."""
        ...

    async def fetch_order_books(
        self, assets: frozenset[Asset], observed_at: datetime, cycle_id: UUID
    ) -> AdapterBatch:
        """Fetch public 20-level book snapshots for one collection cycle."""
        ...
