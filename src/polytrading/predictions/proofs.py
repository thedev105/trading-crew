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

_EXHAUSTIVE_OUTCOME_SET_TEMPLATE = "exhaustive_outcome_set@1"
_EXHAUSTIVE_OUTCOME_SET_COMPILER_VERSION = "1"


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
    if candidate.relationship_type is RelationshipType.EXHAUSTIVE_OUTCOME_SET:
        return _compile_exhaustive_outcome_set(
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
                template=_BINARY_COMPLEMENT_TEMPLATE,
                compiler_version=_BINARY_COMPLEMENT_COMPILER_VERSION,
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
                template=_BINARY_COMPLEMENT_TEMPLATE,
                compiler_version=_BINARY_COMPLEMENT_COMPILER_VERSION,
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
                template=_BINARY_COMPLEMENT_TEMPLATE,
                compiler_version=_BINARY_COMPLEMENT_COMPILER_VERSION,
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
                template=_BINARY_COMPLEMENT_TEMPLATE,
                compiler_version=_BINARY_COMPLEMENT_COMPILER_VERSION,
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
                    template=_BINARY_COMPLEMENT_TEMPLATE,
                    compiler_version=_BINARY_COMPLEMENT_COMPILER_VERSION,
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


def _compile_exhaustive_outcome_set(
    candidate: CandidateRelationship,
    rule_versions: Mapping[UUID, RuleVersion],
    attestations: Mapping[UUID, RuleAttestation],
    *,
    as_of: datetime,
    review_identity: str,
) -> ProofArtifact:
    """Implements the ``exhaustive_outcome_set@1`` template.

    Each leg is one member market of a single venue-native event group (the
    increment-2 generator emits exactly one leg per member, ``outcome_index=None``),
    so the basket is buying every member's YES side. Terminal states model exactly one
    member winning at a time (``member_i_wins``): leg ``i`` pays its own attestation's
    ``winner_payout_per_share`` and every other leg ``j`` pays leg ``j``'s own
    attestation's ``loser_payout_per_share`` -- unlike ``binary_complement``, each
    member may attest different payout values, so every leg is always priced from its
    *own* attestation, never another member's.

    The group-level exhaustiveness claim ("exactly one member wins") is only as good
    as *every* member's attestation independently affirming it, so this requires an
    attestation for every leg (any missing -> insufficient_evidence) and every one of
    them to carry ``outcome_set_exhaustive=True`` (any False -> rejected); the
    resulting ``ProofAssumption`` set carries one assumption per member, each citing
    that member's own attestation, rather than a single pooled assumption.

    A void or tie condition on any single member invalidates the whole group's
    mutual-exclusivity model, not just that member, so:
    - any member with ``tie_possible`` rejects the whole proof (no group-level split
      model is attempted);
    - a member with ``void_behavior="unknown"`` rejects the whole proof;
    - a member with ``void_behavior="refund_at_cost"`` contributes its own excluded
      state (one per such member), citing that member's attestation;
    - a member with ``void_behavior="resolve_to_rules_price"`` contributes to *one*
      combined group-level ``void`` terminal state, modeling the whole group voiding
      together, in which every leg (not only the void-flagged members) pays its own
      attestation's ``loser_payout_per_share`` -- this single combined state is
      template v1's simplification: it cannot represent one member voiding while its
      siblings still resolve normally, only "the whole group is void together" or
      "the whole group resolves normally";
    - members disagreeing on void behavior (some ``refund_at_cost``, some
      ``resolve_to_rules_price``) can't be reconciled into that one combined state, so
      that mix also rejects as ``VOID_BEHAVIOR_UNKNOWN``.

    A candidate whose legs don't actually fit this shape (fewer than 2 legs, or two
    legs sharing one member's market_id) is a structural integrity error, not a
    research outcome to reject or defer -- raised rather than silently building
    mismatched per-leg state.
    """
    legs = candidate.legs
    market_ids = tuple(leg.market_id for leg in legs)
    if len(legs) < 2 or len(set(market_ids)) != len(market_ids):
        raise ValueError(
            "an exhaustive_outcome_set candidate must have at least 2 legs, each from "
            f"a distinct member market; got legs={legs!r}"
        )

    rule_version_ids = tuple(leg.rule_version_id for leg in legs)

    member_attestations = [attestations.get(leg.rule_version_id) for leg in legs]
    if any(attestation is None for attestation in member_attestations):
        return _finalize(
            _terminal_artifact(
                candidate,
                template=_EXHAUSTIVE_OUTCOME_SET_TEMPLATE,
                compiler_version=_EXHAUSTIVE_OUTCOME_SET_COMPILER_VERSION,
                status="insufficient_evidence",
                rejection_reason="MISSING_ATTESTATION",
                rule_version_ids=rule_version_ids,
                source_hashes=_sorted_unique_hashes(leg.rule_source_hash for leg in legs),
                as_of=as_of,
                review_identity=review_identity,
            )
        )
    # mypy/type-narrowing: every element is non-None past the guard above.
    member_attestations_present = [a for a in member_attestations if a is not None]

    source_hashes = _sorted_unique_hashes(
        [leg.rule_source_hash for leg in legs]
        + [attestation.rule_source_hash for attestation in member_attestations_present]
    )

    if not all(attestation.outcome_set_exhaustive for attestation in member_attestations_present):
        return _finalize(
            _terminal_artifact(
                candidate,
                template=_EXHAUSTIVE_OUTCOME_SET_TEMPLATE,
                compiler_version=_EXHAUSTIVE_OUTCOME_SET_COMPILER_VERSION,
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
                template=_EXHAUSTIVE_OUTCOME_SET_TEMPLATE,
                compiler_version=_EXHAUSTIVE_OUTCOME_SET_COMPILER_VERSION,
                status="rejected",
                rejection_reason="RULE_VERSION_CHANGED",
                rule_version_ids=rule_version_ids,
                source_hashes=source_hashes,
                as_of=as_of,
                review_identity=review_identity,
            )
        )

    if any(attestation.tie_possible for attestation in member_attestations_present):
        return _finalize(
            _terminal_artifact(
                candidate,
                template=_EXHAUSTIVE_OUTCOME_SET_TEMPLATE,
                compiler_version=_EXHAUSTIVE_OUTCOME_SET_COMPILER_VERSION,
                status="rejected",
                rejection_reason="TIE_UNMODELED",
                rule_version_ids=rule_version_ids,
                source_hashes=source_hashes,
                as_of=as_of,
                review_identity=review_identity,
            )
        )

    void_members = [a for a in member_attestations_present if a.void_or_invalid_possible]
    excluded_states: list[ExcludedState] = []
    model_combined_void_state = False

    if void_members:
        void_behaviors = {a.void_behavior for a in void_members}
        if "unknown" in void_behaviors or len(void_behaviors) > 1:
            return _finalize(
                _terminal_artifact(
                    candidate,
                    template=_EXHAUSTIVE_OUTCOME_SET_TEMPLATE,
                    compiler_version=_EXHAUSTIVE_OUTCOME_SET_COMPILER_VERSION,
                    status="rejected",
                    rejection_reason="VOID_BEHAVIOR_UNKNOWN",
                    rule_version_ids=rule_version_ids,
                    source_hashes=source_hashes,
                    as_of=as_of,
                    review_identity=review_identity,
                )
            )
        (void_behavior,) = void_behaviors
        if void_behavior == "refund_at_cost":
            for attestation in void_members:
                excluded_states.append(
                    ExcludedState(
                        description=f"{attestation.market_id}: resolves void or invalid.",
                        exclusion_reason=(
                            "a void resolution refunds cost at par per the attested "
                            "rules, so it carries no basket payout risk and is "
                            "excluded rather than modeled as a terminal state"
                        ),
                        attestation_id=attestation.attestation_id,
                    )
                )
        else:  # void_behavior == "resolve_to_rules_price"
            model_combined_void_state = True

    terminal_states: list[TerminalState] = []
    for i, winning_leg in enumerate(legs):
        leg_payouts = tuple(
            member_attestations_present[i].winner_payout_per_share
            if j == i
            else member_attestations_present[j].loser_payout_per_share
            for j in range(len(legs))
        )
        terminal_states.append(
            TerminalState(
                state_id=f"member_{i}_wins",
                description=(
                    f"{winning_leg.market_id}: member index {i} resolves as the winning "
                    "outcome; every other member resolves as losing."
                ),
                leg_payouts=leg_payouts,
            )
        )

    if model_combined_void_state:
        terminal_states.append(
            TerminalState(
                state_id="void",
                description=(
                    "the group resolves void together; every member settles at its "
                    "own attested rules (loser) price. Template v1 models group-wide "
                    "void as a single combined state and cannot represent one member "
                    "voiding while its siblings resolve normally."
                ),
                leg_payouts=tuple(
                    attestation.loser_payout_per_share
                    for attestation in member_attestations_present
                ),
            )
        )

    payout_sums: list[Decimal] = [sum(state.leg_payouts) for state in terminal_states]

    assumptions = tuple(
        ProofAssumption(
            claim=(
                f"{member_leg.market_id} is one exhaustive, mutually-exclusive member "
                "of this venue-native outcome set, per the attested rules"
            ),
            attestation_id=member_attestations_present[i].attestation_id,
            supporting_spans=member_attestations_present[i].supporting_spans,
        )
        for i, member_leg in enumerate(legs)
    )

    artifact = ProofArtifact(
        schema_version=1,
        proof_id=_PLACEHOLDER_PROOF_ID,
        candidate_id=candidate.candidate_id,
        template=_EXHAUSTIVE_OUTCOME_SET_TEMPLATE,
        compiler_version=_EXHAUSTIVE_OUTCOME_SET_COMPILER_VERSION,
        status="proof_ready",
        rejection_reason=None,
        terminal_states=tuple(terminal_states),
        minimum_basket_payout=min(payout_sums),
        maximum_basket_payout=max(payout_sums),
        assumptions=assumptions,
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
    template: str,
    compiler_version: str,
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
        template=template,
        compiler_version=compiler_version,
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
