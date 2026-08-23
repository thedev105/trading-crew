from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from polytrading.predictions.candidates_models import (
    AIProvenance,
    CandidateDisposition,
    CandidateLeg,
    CandidateRelationship,
    DeterministicProvenance,
    RelationshipType,
)
from polytrading.predictions.domain import PredictionVenue

NOW = datetime(2026, 8, 15, 12, tzinfo=UTC)
HASH = "a" * 64
RULE_VERSION_ID = UUID("00000000-0000-0000-0000-000000002001")
CANDIDATE_ID = UUID("00000000-0000-0000-0000-000000003001")


def deterministic_provenance(**overrides: Any) -> DeterministicProvenance:
    values: dict[str, Any] = {
        "kind": "deterministic",
        "generator": "rule-matcher",
        "generator_version": "1.0.0",
        "code_revision": "abc123",
    }
    values.update(overrides)
    return DeterministicProvenance(**values)


def ai_provenance(**overrides: Any) -> AIProvenance:
    values: dict[str, Any] = {
        "kind": "ai",
        "model_id": "gpt-test",
        "model_version": "1",
        "feature_version": "1",
        "prompt_version": "1",
        "evaluation_request_hash": HASH,
        "gate_status": "PASS",
    }
    values.update(overrides)
    return AIProvenance(**values)


def leg(**overrides: Any) -> CandidateLeg:
    values: dict[str, Any] = {
        "venue": PredictionVenue.POLYMARKET,
        "market_id": "0xcondition",
        "outcome_index": 0,
        "outcome_token_id": "111",
        "rule_version_id": RULE_VERSION_ID,
        "rule_source_hash": HASH,
    }
    values.update(overrides)
    return CandidateLeg(**values)


def candidate_relationship(**overrides: Any) -> CandidateRelationship:
    values: dict[str, Any] = {
        "schema_version": 1,
        "candidate_id": CANDIDATE_ID,
        "trial_family_id": "family-1",
        "relationship_type": RelationshipType.BINARY_COMPLEMENT,
        "legs": (leg(), leg(outcome_index=1, outcome_token_id="222")),
        "information_cutoff": NOW,
        "observed_at": NOW,
        "provenance": deterministic_provenance(),
        "propositions": (),
        "unresolved_fields": (),
        "contradictions": (),
        "invalidation_conditions": (),
        "review_status": "unreviewed",
        "disposition": CandidateDisposition.QUARANTINED,
        "superseded_by_candidate_id": None,
    }
    values.update(overrides)
    return CandidateRelationship(**values)
