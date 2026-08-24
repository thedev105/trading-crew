from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import StringConstraints, model_validator

from polytrading.predictions.domain import NonNegativeDecimal, PredictionRecord, Sha256
from polytrading.predictions.propositions import PropositionSpan

NonEmptyString = Annotated[str, StringConstraints(min_length=1)]

# Spec section 6.5: the only templates a compiler may emit a proof_ready artifact
# under. A candidate whose compiled template falls outside this set can only be
# rejected (TEMPLATE_NOT_APPROVED), never proof_ready -- enforced below.
APPROVED_PROOF_TEMPLATES: frozenset[str] = frozenset(
    {
        "binary_complement@1",
        "exhaustive_outcome_set@1",
        "logical_implication@1",
        "cross_venue_equivalence@1",
    }
)

# The literal invalidation condition every proof artifact must declare: a proof is
# derived from specific rule_version identities, so any of those rule versions
# changing invalidates the proof regardless of any other cited condition.
_RULE_VERSION_CHANGE_CONDITION = "any participating rule_version change"

EquivalenceDimensionResult = Literal["compatible", "incompatible", "unknown"]

ProofStatus = Literal["proof_ready", "rejected", "insufficient_evidence"]

ProofRejectionReason = Literal[
    "MISSING_ATTESTATION",
    "OUTCOME_SET_NOT_EXHAUSTIVE",
    "VOID_BEHAVIOR_UNKNOWN",
    "TIE_UNMODELED",
    "IMPLICATION_INVALID",
    "EQUIVALENCE_DIMENSION_UNKNOWN",
    "EQUIVALENCE_DIMENSION_INCOMPATIBLE",
    "TEMPLATE_NOT_APPROVED",
    "RULE_VERSION_CHANGED",
    "PROPOSITIONS_NOT_EXTRACTED",
]


class TerminalState(PredictionRecord):
    """One mutually-exclusive resolution outcome of a proof's candidate legs.

    ``leg_payouts`` carries exactly one payout per candidate leg, in leg order; a
    ``ProofArtifact`` requires every one of its terminal states to agree on that
    length (see ``ProofArtifact._require_consistent_proof``).
    """

    state_id: NonEmptyString
    description: str
    leg_payouts: tuple[NonNegativeDecimal, ...]


class ProofAssumption(PredictionRecord):
    claim: str
    attestation_id: UUID
    supporting_spans: tuple[PropositionSpan, ...]


class ExcludedState(PredictionRecord):
    description: str
    exclusion_reason: str
    attestation_id: UUID


class EquivalenceMatrix(PredictionRecord):
    """A per-dimension compatibility verdict over the Engine-D equivalence dimensions.

    Field names are exactly the eight increment-2 unresolved-field names
    (``scout_bridge._ENGINE_D_UNRESOLVED_FIELDS``) so a cross-venue-equivalence
    compiler resolving one of those unresolved fields writes its verdict under the
    identical name.
    """

    proposition_threshold_inclusivity: EquivalenceDimensionResult
    observation_period_timezone: EquivalenceDimensionResult
    resolution_sources: EquivalenceDimensionResult
    void_dispute_behavior: EquivalenceDimensionResult
    outcome_completeness: EquivalenceDimensionResult
    denomination_collateral_rounding: EquivalenceDimensionResult
    settlement_finality_timing: EquivalenceDimensionResult
    venue_access_custody_rules: EquivalenceDimensionResult
    basis_attestation_ids: tuple[UUID, ...]


class ProofArtifact(PredictionRecord):
    """A deterministic, append-only proof compiled for one candidate relationship.

    A proof is either ``proof_ready`` (a fully bounded basket payout with cited
    terminal states) or not (``rejected``/``insufficient_evidence``, carrying a
    typed ``rejection_reason`` and no payout bounds) -- never a partial mix of the
    two shapes (spec section 6.5).
    """

    schema_version: Literal[1]
    proof_id: UUID
    candidate_id: UUID
    template: str
    compiler_version: str
    status: ProofStatus
    rejection_reason: ProofRejectionReason | None
    terminal_states: tuple[TerminalState, ...]
    minimum_basket_payout: Decimal | None
    maximum_basket_payout: Decimal | None
    assumptions: tuple[ProofAssumption, ...]
    excluded_states: tuple[ExcludedState, ...]
    equivalence_matrix: EquivalenceMatrix | None
    rule_version_ids: tuple[UUID, ...]
    source_hashes: tuple[Sha256, ...]
    review_identity: str
    invalidation_conditions: tuple[str, ...]
    information_cutoff: datetime
    observed_at: datetime

    @model_validator(mode="after")
    def _require_consistent_proof(self) -> ProofArtifact:
        if self.status == "proof_ready":
            if self.rejection_reason is not None:
                raise ValueError("a proof_ready artifact must not carry a rejection_reason")
            if not self.terminal_states:
                raise ValueError("a proof_ready artifact requires at least one terminal state")
            if self.minimum_basket_payout is None or self.maximum_basket_payout is None:
                raise ValueError(
                    "a proof_ready artifact requires both a minimum and maximum basket payout"
                )
            if self.template not in APPROVED_PROOF_TEMPLATES:
                raise ValueError(
                    "a proof_ready artifact's template must be one of the approved proof "
                    f"templates: {sorted(APPROVED_PROOF_TEMPLATES)}"
                )
        else:
            if self.rejection_reason is None:
                raise ValueError(f"a {self.status} artifact requires a rejection_reason")
            if self.minimum_basket_payout is not None or self.maximum_basket_payout is not None:
                raise ValueError(f"a {self.status} artifact must not carry a basket payout bound")

        payout_lengths = {len(state.leg_payouts) for state in self.terminal_states}
        if len(payout_lengths) > 1:
            raise ValueError("every terminal state's leg payout tuple must be the same length")

        state_ids = [state.state_id for state in self.terminal_states]
        if len(state_ids) != len(set(state_ids)):
            raise ValueError("terminal state_id values must be unique within a proof")

        if (self.template == "cross_venue_equivalence@1") != (self.equivalence_matrix is not None):
            raise ValueError(
                "equivalence_matrix is required if and only if template is "
                "'cross_venue_equivalence@1'"
            )

        if _RULE_VERSION_CHANGE_CONDITION not in self.invalidation_conditions:
            raise ValueError(
                f"invalidation_conditions must include {_RULE_VERSION_CHANGE_CONDITION!r}"
            )

        if tuple(sorted(set(self.source_hashes))) != self.source_hashes:
            raise ValueError("source_hashes must be sorted and unique")

        return self
