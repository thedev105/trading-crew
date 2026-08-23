from uuid import UUID

import pytest
from pydantic import ValidationError

from polytrading.predictions.candidates_models import (
    CandidateDisposition,
    CandidateLeg,
    CandidateRelationship,
    RelationshipType,
    deterministic_candidate_id,
)
from polytrading.predictions.domain import PredictionVenue
from tests.predictions.candidate_helpers import (
    HASH,
    ai_provenance,
    candidate_relationship,
    deterministic_provenance,
    leg,
)


def test_ai_provenance_can_never_be_proof_ready() -> None:
    with pytest.raises(ValidationError, match="proof"):
        candidate_relationship(
            provenance=ai_provenance(), disposition=CandidateDisposition.PROOF_READY
        )


def test_ai_provenance_can_never_be_superseded() -> None:
    with pytest.raises(ValidationError, match="proof"):
        candidate_relationship(
            provenance=ai_provenance(),
            disposition=CandidateDisposition.SUPERSEDED,
            superseded_by_candidate_id=UUID("00000000-0000-0000-0000-000000009999"),
        )


def test_ai_provenance_may_be_quarantined_or_rejected() -> None:
    quarantined = candidate_relationship(
        provenance=ai_provenance(), disposition=CandidateDisposition.QUARANTINED
    )
    rejected = candidate_relationship(
        provenance=ai_provenance(), disposition=CandidateDisposition.REJECTED
    )
    assert quarantined.disposition is CandidateDisposition.QUARANTINED
    assert rejected.disposition is CandidateDisposition.REJECTED


def test_proof_ready_requires_review_and_no_unresolved_fields() -> None:
    with pytest.raises(ValidationError):
        candidate_relationship(
            provenance=deterministic_provenance(),
            disposition=CandidateDisposition.PROOF_READY,
            review_status="unreviewed",
        )


def test_proof_ready_requires_no_contradictions() -> None:
    with pytest.raises(ValidationError):
        candidate_relationship(
            provenance=deterministic_provenance(),
            disposition=CandidateDisposition.PROOF_READY,
            review_status="reviewed",
            contradictions=("conflicting resolution sources",),
        )


def test_proof_ready_requires_no_unresolved_fields() -> None:
    with pytest.raises(ValidationError):
        candidate_relationship(
            provenance=deterministic_provenance(),
            disposition=CandidateDisposition.PROOF_READY,
            review_status="reviewed",
            unresolved_fields=("outcome_token_id",),
        )


def test_proof_ready_with_review_and_no_gaps_is_valid() -> None:
    record = candidate_relationship(
        provenance=deterministic_provenance(),
        disposition=CandidateDisposition.PROOF_READY,
        review_status="reviewed",
    )
    assert record.disposition is CandidateDisposition.PROOF_READY


def test_superseded_requires_a_successor_candidate_id() -> None:
    with pytest.raises(ValidationError):
        candidate_relationship(
            disposition=CandidateDisposition.SUPERSEDED, superseded_by_candidate_id=None
        )


def test_superseded_by_candidate_id_forbidden_unless_superseded() -> None:
    with pytest.raises(ValidationError):
        candidate_relationship(
            disposition=CandidateDisposition.QUARANTINED,
            superseded_by_candidate_id=UUID("00000000-0000-0000-0000-000000009999"),
        )


def test_cross_venue_type_requires_two_distinct_venues() -> None:
    with pytest.raises(ValidationError, match="venue"):
        candidate_relationship(
            relationship_type=RelationshipType.CROSS_VENUE_EQUIVALENCE,
            legs=(leg(venue=PredictionVenue.POLYMARKET), leg(venue=PredictionVenue.POLYMARKET)),
        )


def test_cross_venue_type_with_two_distinct_venues_is_valid() -> None:
    record = candidate_relationship(
        relationship_type=RelationshipType.CROSS_VENUE_EQUIVALENCE,
        legs=(leg(venue=PredictionVenue.POLYMARKET), leg(venue=PredictionVenue.KALSHI)),
    )
    assert record.relationship_type is RelationshipType.CROSS_VENUE_EQUIVALENCE


def test_non_cross_venue_type_requires_single_venue() -> None:
    with pytest.raises(ValidationError, match="venue"):
        candidate_relationship(
            relationship_type=RelationshipType.BINARY_COMPLEMENT,
            legs=(leg(venue=PredictionVenue.POLYMARKET), leg(venue=PredictionVenue.KALSHI)),
        )


def test_candidate_relationship_requires_at_least_two_legs() -> None:
    with pytest.raises(ValidationError):
        candidate_relationship(legs=(leg(),))


def test_outcome_index_must_be_nonnegative_when_set() -> None:
    with pytest.raises(ValidationError):
        leg(outcome_index=-1)


def test_outcome_index_may_be_absent() -> None:
    assert leg(outcome_index=None).outcome_index is None


def test_candidate_relationship_forbids_extra_fields() -> None:
    base = candidate_relationship()
    with pytest.raises(ValidationError):
        CandidateRelationship(
            schema_version=base.schema_version,
            candidate_id=base.candidate_id,
            trial_family_id=base.trial_family_id,
            relationship_type=base.relationship_type,
            legs=base.legs,
            information_cutoff=base.information_cutoff,
            observed_at=base.observed_at,
            provenance=base.provenance,
            propositions=base.propositions,
            unresolved_fields=base.unresolved_fields,
            contradictions=base.contradictions,
            invalidation_conditions=base.invalidation_conditions,
            review_status=base.review_status,
            disposition=base.disposition,
            superseded_by_candidate_id=base.superseded_by_candidate_id,
            unexpected=1,
        )


def test_deterministic_candidate_id_is_stable_and_leg_order_invariant() -> None:
    leg_a = leg(outcome_index=0, outcome_token_id="111")
    leg_b = leg(outcome_index=1, outcome_token_id="222")

    forward = deterministic_candidate_id(RelationshipType.BINARY_COMPLEMENT, (leg_a, leg_b))
    reversed_order = deterministic_candidate_id(RelationshipType.BINARY_COMPLEMENT, (leg_b, leg_a))

    assert forward == reversed_order
    assert isinstance(forward, UUID)


def test_deterministic_candidate_id_changes_with_relationship_type() -> None:
    leg_a = leg(outcome_index=0)
    leg_b = leg(outcome_index=1)

    binary = deterministic_candidate_id(RelationshipType.BINARY_COMPLEMENT, (leg_a, leg_b))
    exhaustive = deterministic_candidate_id(RelationshipType.EXHAUSTIVE_OUTCOME_SET, (leg_a, leg_b))

    assert binary != exhaustive


def test_deterministic_candidate_id_changes_with_rule_version() -> None:
    leg_a = leg(outcome_index=0)
    leg_b = leg(outcome_index=1)
    revised_leg_b = leg(
        outcome_index=1, rule_version_id=UUID("00000000-0000-0000-0000-000000002999")
    )

    original = deterministic_candidate_id(RelationshipType.BINARY_COMPLEMENT, (leg_a, leg_b))
    revised = deterministic_candidate_id(RelationshipType.BINARY_COMPLEMENT, (leg_a, revised_leg_b))

    assert original != revised


def test_candidate_relationship_round_trips_through_json() -> None:
    record = candidate_relationship()
    restored = CandidateRelationship.model_validate_json(record.model_dump_json())
    assert restored == record


def test_leg_requires_sha256_rule_source_hash() -> None:
    with pytest.raises(ValidationError):
        leg(rule_source_hash="not-a-hash")


def test_leg_round_trips() -> None:
    original = leg()
    restored = CandidateLeg.model_validate_json(original.model_dump_json())
    assert restored == original


def test_leg_uses_valid_hash_constant() -> None:
    assert leg().rule_source_hash == HASH
