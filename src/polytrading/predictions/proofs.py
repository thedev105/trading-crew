from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid5

from polytrading.predictions.attestations import RuleAttestation
from polytrading.predictions.candidates_models import CandidateRelationship, RelationshipType
from polytrading.predictions.domain import RuleVersion, Sha256
from polytrading.predictions.proofs_models import (
    ExcludedState,
    ProofArtifact,
    ProofAssumption,
    ProofRejectionReason,
    ProofStatus,
    TerminalState,
)

# Fixed namespace for deterministic proof identity (UUIDv5). Generated once via uuid4()
# and pinned here as a literal -- never regenerate this value, or every previously
# derived proof_id would silently change identity, breaking append-idempotent persistence.
_PROOF_IDENTITY_NAMESPACE = UUID("0073fffa-f33c-466c-bff1-83a1ab710ae7")

# Placeholder used only while building an artifact's content prior to deriving its real
# proof_id from that content; never observable outside this module.
_PLACEHOLDER_PROOF_ID = UUID(int=0)

_RULE_VERSION_CHANGE_CONDITION = "any participating rule_version change"

_BINARY_COMPLEMENT_TEMPLATE = "binary_complement@1"
_BINARY_COMPLEMENT_COMPILER_VERSION = "1"


def compile_proof(
    candidate: CandidateRelationship,
    rule_versions: Mapping[UUID, RuleVersion],
    attestations: Mapping[UUID, RuleAttestation],
    *,
    as_of: datetime,
    review_identity: str,
) -> ProofArtifact:
    """Compile a deterministic payoff proof for one candidate relationship.

    Pure function: no I/O, no clock reads. ``as_of`` is the caller-supplied evaluation
    time, used verbatim for both ``observed_at`` and ``information_cutoff``.
    ``rule_versions`` is keyed by ``rule_version_id`` and represents the caller's current
    registry state (i.e. only each market's presently-effective rule version); a
    candidate leg's ``rule_version_id`` missing from this mapping means that leg's rule
    version has since been superseded. ``attestations`` is keyed by ``rule_version_id``.
    """
    if candidate.relationship_type is RelationshipType.BINARY_COMPLEMENT:
        return _compile_binary_complement(
            candidate, rule_versions, attestations, as_of=as_of, review_identity=review_identity
        )
    raise NotImplementedError(
        "proof compilation for relationship_type="
        f"{candidate.relationship_type!r} is not yet implemented"
    )


def _compile_binary_complement(
    candidate: CandidateRelationship,
    rule_versions: Mapping[UUID, RuleVersion],
    attestations: Mapping[UUID, RuleAttestation],
    *,
    as_of: datetime,
    review_identity: str,
) -> ProofArtifact:
    """Implements the ``binary_complement@1`` template.

    Both legs are one market's two outcomes (outcome_index 0 and 1), so the basket is
    one share of each outcome. Terminal states model exactly one outcome winning at a
    time; a possible void resolution is either excluded (refund_at_cost) or modeled as
    a third terminal state (resolve_to_rules_price); anything unmodeled -- an unknown
    void behavior, or a possible tie -- rejects rather than guesses.

    A candidate whose legs don't actually fit this shape (not exactly 2 legs, or legs
    spanning two markets/rule versions) is a structural integrity error, not a research
    outcome to reject or defer -- ``CandidateRelationship`` itself doesn't enforce
    per-relationship-type leg shape, so this compiler raises rather than silently
    reading only ``legs[0]``/``legs[1]`` and dropping the rest.
    """
    legs = candidate.legs
    if (
        len(legs) != 2
        or legs[0].market_id != legs[1].market_id
        or legs[0].rule_version_id != legs[1].rule_version_id
    ):
        raise ValueError(
            "a binary_complement candidate must have exactly 2 legs sharing one "
            f"market's market_id and rule_version_id; got legs={legs!r}"
        )

    rule_version_ids = tuple(leg.rule_version_id for leg in legs)
    market_rule_version_id = legs[0].rule_version_id

    attestation = attestations.get(market_rule_version_id)
    if attestation is None:
        return _finalize(
            _terminal_artifact(
                candidate,
                status="insufficient_evidence",
                rejection_reason="MISSING_ATTESTATION",
                rule_version_ids=rule_version_ids,
                source_hashes=_sorted_unique_hashes(leg.rule_source_hash for leg in legs),
                as_of=as_of,
                review_identity=review_identity,
            )
        )

    source_hashes = _sorted_unique_hashes(
        [leg.rule_source_hash for leg in legs] + [attestation.rule_source_hash]
    )

    if not attestation.outcome_set_exhaustive:
        return _finalize(
            _terminal_artifact(
                candidate,
                status="rejected",
                rejection_reason="OUTCOME_SET_NOT_EXHAUSTIVE",
                rule_version_ids=rule_version_ids,
                source_hashes=source_hashes,
                as_of=as_of,
                review_identity=review_identity,
            )
        )

    if not all(leg.rule_version_id in rule_versions for leg in legs):
        return _finalize(
            _terminal_artifact(
                candidate,
                status="rejected",
                rejection_reason="RULE_VERSION_CHANGED",
                rule_version_ids=rule_version_ids,
                source_hashes=source_hashes,
                as_of=as_of,
                review_identity=review_identity,
            )
        )

    if attestation.tie_possible:
        return _finalize(
            _terminal_artifact(
                candidate,
                status="rejected",
                rejection_reason="TIE_UNMODELED",
                rule_version_ids=rule_version_ids,
                source_hashes=source_hashes,
                as_of=as_of,
                review_identity=review_identity,
            )
        )

    winner = attestation.winner_payout_per_share
    loser = attestation.loser_payout_per_share
    market_id = legs[0].market_id

    terminal_states = [
        TerminalState(
            state_id="outcome_0_wins",
            description=(
                f"{market_id}: outcome index 0 resolves as the winning outcome; "
                "outcome index 1 resolves as the losing outcome."
            ),
            leg_payouts=(winner, loser),
        ),
        TerminalState(
            state_id="outcome_1_wins",
            description=(
                f"{market_id}: outcome index 1 resolves as the winning outcome; "
                "outcome index 0 resolves as the losing outcome."
            ),
            leg_payouts=(loser, winner),
        ),
    ]
    excluded_states: list[ExcludedState] = []

    if attestation.void_or_invalid_possible:
        if attestation.void_behavior == "unknown":
            return _finalize(
                _terminal_artifact(
                    candidate,
                    status="rejected",
                    rejection_reason="VOID_BEHAVIOR_UNKNOWN",
                    rule_version_ids=rule_version_ids,
                    source_hashes=source_hashes,
                    as_of=as_of,
                    review_identity=review_identity,
                )
            )
        if attestation.void_behavior == "refund_at_cost":
            excluded_states.append(
                ExcludedState(
                    description=f"{market_id}: resolves void or invalid.",
                    exclusion_reason=(
                        "a void resolution refunds cost at par per the attested rules, "
                        "so it carries no basket payout risk and is excluded rather "
                        "than modeled as a terminal state"
                    ),
                    attestation_id=attestation.attestation_id,
                )
            )
        elif attestation.void_behavior == "resolve_to_rules_price":
            terminal_states.append(
                TerminalState(
                    state_id="void",
                    description=(
                        f"{market_id}: resolves void and settles both legs at the "
                        "attested rules (loser) price."
                    ),
                    leg_payouts=(loser, loser),
                )
            )

    payout_sums: list[Decimal] = [sum(state.leg_payouts) for state in terminal_states]

    assumption = ProofAssumption(
        claim=(
            f"{market_id} is an exhaustive binary complement: exactly one of outcome "
            "index 0 / outcome index 1 wins, per the attested rules"
        ),
        attestation_id=attestation.attestation_id,
        supporting_spans=attestation.supporting_spans,
    )

    artifact = ProofArtifact(
        schema_version=1,
        proof_id=_PLACEHOLDER_PROOF_ID,
        candidate_id=candidate.candidate_id,
        template=_BINARY_COMPLEMENT_TEMPLATE,
        compiler_version=_BINARY_COMPLEMENT_COMPILER_VERSION,
        status="proof_ready",
        rejection_reason=None,
        terminal_states=tuple(terminal_states),
        minimum_basket_payout=min(payout_sums),
        maximum_basket_payout=max(payout_sums),
        assumptions=(assumption,),
        excluded_states=tuple(excluded_states),
        equivalence_matrix=None,
        rule_version_ids=rule_version_ids,
        source_hashes=source_hashes,
        review_identity=review_identity,
        invalidation_conditions=(_RULE_VERSION_CHANGE_CONDITION,),
        information_cutoff=as_of,
        observed_at=as_of,
    )
    return _finalize(artifact)


def _terminal_artifact(
    candidate: CandidateRelationship,
    *,
    status: ProofStatus,
    rejection_reason: ProofRejectionReason,
    rule_version_ids: tuple[UUID, ...],
    source_hashes: tuple[Sha256, ...],
    as_of: datetime,
    review_identity: str,
) -> ProofArtifact:
    """Build a non-``proof_ready`` (``rejected``/``insufficient_evidence``) artifact."""
    return ProofArtifact(
        schema_version=1,
        proof_id=_PLACEHOLDER_PROOF_ID,
        candidate_id=candidate.candidate_id,
        template=_BINARY_COMPLEMENT_TEMPLATE,
        compiler_version=_BINARY_COMPLEMENT_COMPILER_VERSION,
        status=status,
        rejection_reason=rejection_reason,
        terminal_states=(),
        minimum_basket_payout=None,
        maximum_basket_payout=None,
        assumptions=(),
        excluded_states=(),
        equivalence_matrix=None,
        rule_version_ids=rule_version_ids,
        source_hashes=source_hashes,
        review_identity=review_identity,
        invalidation_conditions=(_RULE_VERSION_CHANGE_CONDITION,),
        information_cutoff=as_of,
        observed_at=as_of,
    )


def _sorted_unique_hashes(hashes: Iterable[Sha256]) -> tuple[Sha256, ...]:
    return tuple(sorted(set(hashes)))


def _finalize(artifact: ProofArtifact) -> ProofArtifact:
    """Derive and stamp ``proof_id`` from the artifact's own content.

    ``proof_id = uuid5(fixed namespace, canonical JSON of the artifact's content
    excluding proof_id)``, mirroring ``candidates_models.deterministic_candidate_id``'s
    style: two identical compilations always produce byte-identical artifacts, so
    persisting a proof is append-idempotent.
    """
    content = artifact.model_dump(mode="json", exclude={"proof_id"})
    canonical = json.dumps(content, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    proof_id = uuid5(_PROOF_IDENTITY_NAMESPACE, canonical)
    return artifact.model_copy(update={"proof_id": proof_id})
