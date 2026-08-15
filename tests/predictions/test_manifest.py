import pytest
from pydantic import ValidationError

from polytrading.predictions.domain import PredictionVenue
from polytrading.predictions.manifest import (
    AdapterImplementationState,
    evaluate_collection_gate,
    evaluate_execution_gate,
)
from tests.predictions.manifest_helpers import SOURCE_HASH, venue_manifest


def test_watchlist_venue_never_permits_collection() -> None:
    manifest = venue_manifest(implementation_state=AdapterImplementationState.WATCHLIST)
    decision = evaluate_collection_gate(manifest, venue=PredictionVenue.POLYMARKET)
    assert decision.allowed is False
    assert decision.reason == "COLLECTION_NOT_PERMITTED"


def test_missing_manifest_fails_closed_before_any_request() -> None:
    decision = evaluate_collection_gate(None, venue=PredictionVenue.KALSHI)
    assert decision.allowed is False
    assert decision.reason == "MANIFEST_NOT_FOUND"
    assert decision.manifest_source_hashes == ()


def test_read_only_manifest_with_permitted_automated_use_allows_collection() -> None:
    manifest = venue_manifest(
        implementation_state=AdapterImplementationState.READ_ONLY,
        automated_use_status="permitted",
    )
    decision = evaluate_collection_gate(manifest, venue=manifest.venue)
    assert decision.allowed is True
    assert decision.reason is None
    assert decision.manifest_source_hashes == (SOURCE_HASH,)


@pytest.mark.parametrize("status", ["restricted", "unknown"])
def test_non_permitted_automated_use_rejects_collection(status: str) -> None:
    manifest = venue_manifest(automated_use_status=status)
    decision = evaluate_collection_gate(manifest, venue=manifest.venue)
    assert decision.allowed is False
    assert decision.reason == "AUTOMATED_USE_RESTRICTED"


def test_execution_gate_requires_live_eligible_and_reviewed_jurisdiction() -> None:
    manifest = venue_manifest(
        implementation_state=AdapterImplementationState.LIVE_DISABLED,
        jurisdiction_review_status="ELIGIBILITY_REVIEWED",
    )
    decision = evaluate_execution_gate(manifest, venue=manifest.venue)
    assert decision.allowed is False
    assert decision.reason == "LIVE_NOT_ELIGIBLE"


def test_execution_gate_rejects_unreviewed_jurisdiction_even_when_live_eligible() -> None:
    manifest = venue_manifest(
        implementation_state=AdapterImplementationState.LIVE_ELIGIBLE,
        jurisdiction_review_status="UNREVIEWED",
    )
    decision = evaluate_execution_gate(manifest, venue=manifest.venue)
    assert decision.allowed is False
    assert decision.reason == "JURISDICTION_UNREVIEWED"


def test_execution_gate_rejects_blocked_jurisdiction() -> None:
    manifest = venue_manifest(
        implementation_state=AdapterImplementationState.LIVE_ELIGIBLE,
        jurisdiction_review_status="BLOCKED",
    )
    decision = evaluate_execution_gate(manifest, venue=manifest.venue)
    assert decision.allowed is False
    assert decision.reason == "JURISDICTION_BLOCKED"


def test_execution_gate_passes_only_when_fully_eligible_and_reviewed() -> None:
    manifest = venue_manifest(
        implementation_state=AdapterImplementationState.LIVE_ELIGIBLE,
        jurisdiction_review_status="ELIGIBILITY_REVIEWED",
    )
    decision = evaluate_execution_gate(manifest, venue=manifest.venue)
    assert decision.allowed is True
    assert decision.reason is None


def test_execution_gate_fails_closed_on_missing_manifest() -> None:
    decision = evaluate_execution_gate(None, venue=PredictionVenue.KALSHI)
    assert decision.allowed is False
    assert decision.reason == "MANIFEST_NOT_FOUND"


def test_manifest_requires_nonempty_official_sources() -> None:
    with pytest.raises(ValidationError):
        venue_manifest(official_sources=())


def test_manifest_requires_sorted_unique_source_hashes() -> None:
    with pytest.raises(ValidationError):
        venue_manifest(source_hashes=())
    with pytest.raises(ValidationError):
        venue_manifest(source_hashes=("b" * 64, "a" * 64))
    with pytest.raises(ValidationError):
        venue_manifest(source_hashes=(SOURCE_HASH, SOURCE_HASH))
