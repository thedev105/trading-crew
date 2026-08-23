from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID, uuid5

from pydantic import StringConstraints, field_validator, model_validator

from polytrading.predictions.domain import PredictionRecord, PredictionVenue, Sha256
from polytrading.predictions.propositions import TypedProposition

NonEmptyString = Annotated[str, StringConstraints(min_length=1)]

# Fixed namespace for deterministic candidate-relationship identity (UUIDv5). Generated
# once via uuid4() and pinned here as a literal -- never regenerate this value, or every
# previously derived candidate_id would silently change identity.
_CANDIDATE_IDENTITY_NAMESPACE = UUID("5d41aa1e-f680-43bf-b652-939bc0dd239a")


class RelationshipType(StrEnum):
    BINARY_COMPLEMENT = "binary_complement"
    EXHAUSTIVE_OUTCOME_SET = "exhaustive_outcome_set"
    LOGICAL_IMPLICATION = "logical_implication"
    CROSS_VENUE_EQUIVALENCE = "cross_venue_equivalence"


class CandidateDisposition(StrEnum):
    QUARANTINED = "quarantined"
    REJECTED = "rejected"
    PROOF_READY = "proof_ready"
    SUPERSEDED = "superseded"


class DeterministicProvenance(PredictionRecord):
    kind: Literal["deterministic"]
    generator: NonEmptyString
    generator_version: NonEmptyString
    code_revision: NonEmptyString


class AIProvenance(PredictionRecord):
    """Provenance for an AI-nominated candidate relationship.

    ``gate_status`` is always ``"PASS"``: a nomination may only be persisted once its
    backing evaluation has passed (Task 8 enforces the runtime check that produces
    this record in the first place).
    """

    kind: Literal["ai"]
    model_id: str
    model_version: str
    feature_version: str
    prompt_version: str | None
    evaluation_request_hash: Sha256
    gate_status: Literal["PASS"]


class CandidateLeg(PredictionRecord):
    venue: PredictionVenue
    market_id: str
    outcome_index: int | None
    outcome_token_id: str | None
    rule_version_id: UUID
    rule_source_hash: Sha256

    @field_validator("outcome_index")
    @classmethod
    def _require_nonnegative_outcome_index(cls, value: int | None) -> int | None:
        if value is not None and value < 0:
            raise ValueError("outcome_index must be non-negative when set")
        return value


class CandidateRelationship(PredictionRecord):
    """An append-only, evidence-backed candidate relationship between market legs.

    A candidate is nominated (deterministically or by an AI evaluation gate) and then
    reviewed; it is never mutated in place. Superseding a candidate means appending a
    new record and pointing the old one's ``superseded_by_candidate_id`` at the new
    identity.
    """

    schema_version: Literal[1]
    candidate_id: UUID
    trial_family_id: NonEmptyString
    relationship_type: RelationshipType
    legs: tuple[CandidateLeg, ...]
    information_cutoff: datetime
    observed_at: datetime
    provenance: DeterministicProvenance | AIProvenance
    propositions: tuple[TypedProposition, ...]
    unresolved_fields: tuple[str, ...]
    contradictions: tuple[str, ...]
    invalidation_conditions: tuple[str, ...]
    review_status: Literal["unreviewed", "in_review", "reviewed"]
    disposition: CandidateDisposition
    superseded_by_candidate_id: UUID | None

    @field_validator("legs")
    @classmethod
    def _require_at_least_two_legs(
        cls, value: tuple[CandidateLeg, ...]
    ) -> tuple[CandidateLeg, ...]:
        if len(value) < 2:
            raise ValueError("a candidate relationship requires at least 2 legs")
        return value

    @model_validator(mode="after")
    def _require_consistent_candidate(self) -> CandidateRelationship:
        if self.provenance.kind == "ai" and self.disposition not in (
            CandidateDisposition.QUARANTINED,
            CandidateDisposition.REJECTED,
        ):
            raise ValueError(
                "AI provenance can never produce a proof-ready or superseded candidate; "
                "an AI nomination may only be quarantined or rejected"
            )
        if self.disposition is CandidateDisposition.PROOF_READY and (
            self.review_status != "reviewed"
            or self.unresolved_fields != ()
            or self.contradictions != ()
        ):
            raise ValueError(
                "a proof-ready candidate requires review_status=='reviewed' and no "
                "unresolved fields or contradictions"
            )
        if (self.disposition is CandidateDisposition.SUPERSEDED) != (
            self.superseded_by_candidate_id is not None
        ):
            raise ValueError(
                "disposition=='superseded' must correspond exactly to a set "
                "superseded_by_candidate_id"
            )
        distinct_venues = {leg.venue for leg in self.legs}
        if self.relationship_type is RelationshipType.CROSS_VENUE_EQUIVALENCE:
            if len(distinct_venues) < 2:
                raise ValueError(
                    "a cross-venue-equivalence relationship requires legs spanning at "
                    "least two distinct venues"
                )
        elif len(distinct_venues) != 1:
            raise ValueError(
                f"a {self.relationship_type.value} relationship requires all legs to "
                "share a single venue"
            )
        return self


def deterministic_candidate_id(
    relationship_type: RelationshipType, legs: Sequence[CandidateLeg]
) -> UUID:
    """Derive a stable, leg-order-invariant identity for a candidate relationship.

    Same markets at the same rule versions always produce the same identity, so
    regenerating candidates is append-idempotent; a rule change (a different
    ``rule_version_id``) produces a new candidate.
    """
    identity_tuples = [
        (leg.venue.value, leg.market_id, leg.outcome_index, str(leg.rule_version_id))
        for leg in legs
    ]
    identity_tuples.sort(
        key=lambda item: (item[0], item[1], item[2] is None, item[2] or 0, item[3])
    )
    canonical = json.dumps(
        [relationship_type.value, identity_tuples],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return uuid5(_CANDIDATE_IDENTITY_NAMESPACE, canonical)
