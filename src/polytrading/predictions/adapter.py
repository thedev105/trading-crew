from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Protocol
from uuid import UUID

from polytrading.predictions.domain import (
    MarketRecord,
    PredictionBookSnapshot,
    PredictionFeeRate,
    PredictionNormalizedRecord,
    PredictionRawEnvelope,
    PredictionVenue,
    RuleVersion,
    TradeRecord,
)
from polytrading.predictions.manifest import VenueManifest


class PredictionCollectionGateError(RuntimeError):
    """Raised when a venue manifest does not permit the requested collection."""


class PredictionAdapterBatchIntegrityError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class PredictionAdapterWarning:
    code: str
    venue: PredictionVenue
    endpoint: str
    market_id: str
    message: str

    def __post_init__(self) -> None:
        for field_name in ("code", "endpoint", "market_id", "message"):
            if not isinstance(getattr(self, field_name), str):
                raise TypeError(f"{field_name} must be a string")
        if not isinstance(self.venue, PredictionVenue):
            raise TypeError("venue must be a PredictionVenue")


@dataclass(frozen=True)
class PredictionAdapterBatch:
    raw: tuple[PredictionRawEnvelope, ...]
    normalized: tuple[PredictionNormalizedRecord, ...]
    warnings: tuple[PredictionAdapterWarning, ...] = ()


def validate_prediction_adapter_batch(batch: PredictionAdapterBatch) -> None:
    for raw in batch.raw:
        actual_hash = sha256(raw.payload_json.encode("utf-8")).hexdigest()
        if raw.source_hash != actual_hash:
            raise PredictionAdapterBatchIntegrityError(
                "raw_source_hash_mismatch",
                "raw source hash does not match exact UTF-8 payload",
            )
    raw_lineage = {(item.venue, item.source_hash) for item in batch.raw}
    for item in batch.normalized:
        source_hashed_types = RuleVersion | TradeRecord | PredictionBookSnapshot | PredictionFeeRate
        if isinstance(item, MarketRecord):
            lineage_hash = item.raw_hash
        elif isinstance(item, source_hashed_types):
            lineage_hash = item.source_hash
        else:
            raise TypeError(f"unsupported normalized record type: {type(item).__name__}")
        if (item.venue, lineage_hash) not in raw_lineage:
            raise PredictionAdapterBatchIntegrityError(
                "normalized_lineage_mismatch",
                "normalized lineage must reference a same-venue raw source hash in its batch",
            )


class PredictionVenueAdapter(Protocol):
    venue: PredictionVenue

    async def fetch_manifest_gated(self, manifest: VenueManifest) -> None:
        """Raise PredictionCollectionGateError if the manifest disallows collection."""
        ...

    async def fetch_markets(self, *, information_cutoff: datetime) -> PredictionAdapterBatch:
        """Fetch public markets and their current rule version."""
        ...

    async def fetch_book_snapshot(
        self,
        market_id: str,
        outcome_token_id: str | None,
        observed_at: datetime,
        cycle_id: UUID,
    ) -> PredictionAdapterBatch:
        """Fetch a public executable book snapshot."""
        ...

    async def fetch_trades(
        self, market_id: str, start: datetime, end: datetime, observed_at: datetime
    ) -> PredictionAdapterBatch:
        """Fetch public trades in the closed interval."""
        ...

    async def fetch_fee_rate(
        self, market_id: str | None, observed_at: datetime
    ) -> PredictionAdapterBatch:
        """Fetch the current public fee rate."""
        ...
