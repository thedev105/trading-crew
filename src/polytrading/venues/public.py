from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
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

type NormalizedRecord = (
    InstrumentSpec | FundingObservation | MarketSnapshot | Level2BookSnapshot
)


@dataclass(frozen=True)
class AdapterBatch:
    raw: tuple[RawEnvelope, ...]
    normalized: tuple[NormalizedRecord, ...]


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
