from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import model_validator

from polytrading.predictions.domain import PredictionRecord, PredictionVenue, Sha256


class AdapterImplementationState(StrEnum):
    WATCHLIST = "WATCHLIST"
    READ_ONLY = "READ_ONLY"
    SHADOW = "SHADOW"
    LIVE_DISABLED = "LIVE_DISABLED"
    LIVE_ELIGIBLE = "LIVE_ELIGIBLE"


UseStatus = Literal["permitted", "restricted", "unknown"]
JurisdictionReviewStatus = Literal["UNREVIEWED", "BLOCKED", "ELIGIBILITY_REVIEWED"]

ManifestGateReason = Literal[
    "MANIFEST_NOT_FOUND",
    "COLLECTION_NOT_PERMITTED",
    "AUTOMATED_USE_RESTRICTED",
    "JURISDICTION_BLOCKED",
    "JURISDICTION_UNREVIEWED",
    "LIVE_NOT_ELIGIBLE",
]

_COLLECTIBLE_STATES = (
    AdapterImplementationState.READ_ONLY,
    AdapterImplementationState.SHADOW,
    AdapterImplementationState.LIVE_DISABLED,
    AdapterImplementationState.LIVE_ELIGIBLE,
)


class VenueManifest(PredictionRecord):
    schema_version: Literal[1]
    venue: PredictionVenue
    underlying_exchange: str | None
    is_independent_liquidity: bool
    official_sources: tuple[str, ...]
    public_capability: bool
    authenticated_demo_capability: bool
    authenticated_live_capability: bool
    data_retention_status: UseStatus
    automated_use_status: UseStatus
    commercial_use_status: UseStatus
    redistribution_status: UseStatus
    model_training_status: UseStatus
    implementation_state: AdapterImplementationState
    jurisdiction_review_status: JurisdictionReviewStatus
    review_identity: str
    reviewed_at: datetime
    source_hashes: tuple[Sha256, ...]
    invalidation_conditions: tuple[str, ...]

    @model_validator(mode="after")
    def _require_nonempty_sources_and_canonical_hashes(self) -> VenueManifest:
        if not self.official_sources:
            raise ValueError("venue manifest must cite at least one official source")
        if not self.source_hashes:
            raise ValueError("venue manifest must cite at least one source hash")
        if tuple(sorted(set(self.source_hashes))) != self.source_hashes:
            raise ValueError("venue manifest source hashes must be sorted and unique")
        return self


class ManifestGateDecision(PredictionRecord):
    allowed: bool
    reason: ManifestGateReason | None
    venue: PredictionVenue
    manifest_source_hashes: tuple[Sha256, ...]


def evaluate_collection_gate(
    manifest: VenueManifest | None, *, venue: PredictionVenue
) -> ManifestGateDecision:
    if manifest is None:
        return ManifestGateDecision(
            allowed=False,
            reason="MANIFEST_NOT_FOUND",
            venue=venue,
            manifest_source_hashes=(),
        )
    if manifest.automated_use_status != "permitted":
        return ManifestGateDecision(
            allowed=False,
            reason="AUTOMATED_USE_RESTRICTED",
            venue=venue,
            manifest_source_hashes=manifest.source_hashes,
        )
    if manifest.implementation_state not in _COLLECTIBLE_STATES:
        return ManifestGateDecision(
            allowed=False,
            reason="COLLECTION_NOT_PERMITTED",
            venue=venue,
            manifest_source_hashes=manifest.source_hashes,
        )
    return ManifestGateDecision(
        allowed=True,
        reason=None,
        venue=venue,
        manifest_source_hashes=manifest.source_hashes,
    )


def evaluate_execution_gate(
    manifest: VenueManifest | None, *, venue: PredictionVenue
) -> ManifestGateDecision:
    collection = evaluate_collection_gate(manifest, venue=venue)
    if not collection.allowed:
        return collection
    assert manifest is not None
    if manifest.jurisdiction_review_status != "ELIGIBILITY_REVIEWED":
        reason: ManifestGateReason = (
            "JURISDICTION_BLOCKED"
            if manifest.jurisdiction_review_status == "BLOCKED"
            else "JURISDICTION_UNREVIEWED"
        )
        return ManifestGateDecision(
            allowed=False,
            reason=reason,
            venue=venue,
            manifest_source_hashes=manifest.source_hashes,
        )
    if manifest.implementation_state != AdapterImplementationState.LIVE_ELIGIBLE:
        return ManifestGateDecision(
            allowed=False,
            reason="LIVE_NOT_ELIGIBLE",
            venue=venue,
            manifest_source_hashes=manifest.source_hashes,
        )
    return ManifestGateDecision(
        allowed=True,
        reason=None,
        venue=venue,
        manifest_source_hashes=manifest.source_hashes,
    )
