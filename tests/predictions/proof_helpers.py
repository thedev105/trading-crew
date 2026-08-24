from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from polytrading.predictions.proofs_models import (
    EquivalenceMatrix,
    ExcludedState,
    ProofArtifact,
    ProofAssumption,
    TerminalState,
)
from tests.predictions.attestation_helpers import ATTESTATION_ID, supporting_span

NOW = datetime(2026, 8, 15, 12, tzinfo=UTC)
HASH = "a" * 64
CANDIDATE_ID = UUID("00000000-0000-0000-0000-000000003001")
RULE_VERSION_ID = UUID("00000000-0000-0000-0000-000000002001")
PROOF_ID = UUID("00000000-0000-0000-0000-000000006001")

INVALIDATION_CONDITIONS: tuple[str, ...] = ("any participating rule_version change",)


def terminal_state(**overrides: Any) -> TerminalState:
    values: dict[str, Any] = {
        "state_id": "state-a",
        "description": "leg A resolves YES, leg B resolves NO",
        "leg_payouts": (Decimal("1"), Decimal("0")),
    }
    values.update(overrides)
    return TerminalState(**values)


def proof_assumption(**overrides: Any) -> ProofAssumption:
    values: dict[str, Any] = {
        "claim": "the outcome set is exhaustive",
        "attestation_id": ATTESTATION_ID,
        "supporting_spans": (supporting_span(),),
    }
    values.update(overrides)
    return ProofAssumption(**values)


def excluded_state(**overrides: Any) -> ExcludedState:
    values: dict[str, Any] = {
        "description": "both legs resolve YES",
        "exclusion_reason": "mutually exclusive outcomes per attested rules",
        "attestation_id": ATTESTATION_ID,
    }
    values.update(overrides)
    return ExcludedState(**values)


def equivalence_matrix(**overrides: Any) -> EquivalenceMatrix:
    values: dict[str, Any] = {
        "proposition_threshold_inclusivity": "compatible",
        "observation_period_timezone": "compatible",
        "resolution_sources": "compatible",
        "void_dispute_behavior": "compatible",
        "outcome_completeness": "compatible",
        "denomination_collateral_rounding": "compatible",
        "settlement_finality_timing": "compatible",
        "venue_access_custody_rules": "compatible",
        "basis_attestation_ids": (ATTESTATION_ID,),
    }
    values.update(overrides)
    return EquivalenceMatrix(**values)


def proof_artifact(**overrides: Any) -> ProofArtifact:
    values: dict[str, Any] = {
        "schema_version": 1,
        "proof_id": PROOF_ID,
        "candidate_id": CANDIDATE_ID,
        "template": "binary_complement@1",
        "compiler_version": "1.0.0",
        "status": "proof_ready",
        "rejection_reason": None,
        "terminal_states": (terminal_state(),),
        "minimum_basket_payout": Decimal("1"),
        "maximum_basket_payout": Decimal("1"),
        "assumptions": (proof_assumption(),),
        "excluded_states": (),
        "equivalence_matrix": None,
        "rule_version_ids": (RULE_VERSION_ID,),
        "source_hashes": (HASH,),
        "review_identity": "reviewer@example.test",
        "invalidation_conditions": INVALIDATION_CONDITIONS,
        "information_cutoff": NOW,
        "observed_at": NOW,
    }
    values.update(overrides)
    return ProofArtifact(**values)
