from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Literal
from uuid import UUID, uuid5

from polytrading.predictions.attestations import RuleAttestation
from polytrading.predictions.candidates_models import CandidateRelationship, RelationshipType
from polytrading.predictions.domain import RuleVersion, Sha256
from polytrading.predictions.proofs_models import (
    EquivalenceDimensionResult,
    EquivalenceMatrix,
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

_LOGICAL_IMPLICATION_TEMPLATE = "logical_implication@1"
_LOGICAL_IMPLICATION_COMPILER_VERSION = "1"

_CROSS_VENUE_EQUIVALENCE_TEMPLATE = "cross_venue_equivalence@1"
_CROSS_VENUE_EQUIVALENCE_COMPILER_VERSION = "1"

# Predicate strings this template recognizes as threshold "direction" families: an
# "up" bound (>=, >) is satisfied by values at-or-above/strictly-above the threshold;
# a "down" bound (<=, <) is satisfied by values at-or-below/strictly-below it. Any
# other predicate string on a threshold proposition isn't a family this template's
# strictness comparison understands, so it can't be checked deterministically.
_THRESHOLD_UP_PREDICATES = frozenset({">=", ">"})
_THRESHOLD_DOWN_PREDICATES = frozenset({"<=", "<"})


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
    if candidate.relationship_type is RelationshipType.LOGICAL_IMPLICATION:
        return _compile_logical_implication(
            candidate, rule_versions, attestations, as_of=as_of, review_identity=review_identity
        )
    if candidate.relationship_type is RelationshipType.CROSS_VENUE_EQUIVALENCE:
        return _compile_cross_venue_equivalence(
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


def _compile_logical_implication(
    candidate: CandidateRelationship,
    rule_versions: Mapping[UUID, RuleVersion],
    attestations: Mapping[UUID, RuleAttestation],
    *,
    as_of: datetime,
    review_identity: str,
) -> ProofArtifact:
    """Implements the ``logical_implication@1`` template.

    Leg 0 is the NO side of proposition A's market; leg 1 is the YES side of
    proposition B's market -- two distinct markets, one venue. The basket is
    NO(A) + YES(B); given a deterministically-verified implication A => B, the only
    combination that can never occur is "A true, B false" (``a_without_b``), which the
    implication excludes rather than models as a terminal state. Because leg 0 rides
    the NO side of A's market, v1 treats each attestation's winner/loser payouts as
    side-symmetric: A's attestation's ``winner_payout_per_share`` is paid to whichever
    side of A's market actually wins (here, NO(A) when A resolves false), and
    correspondingly for B's attestation and the YES(B) leg.

    Implication validity is checked deterministically over both legs' typed
    propositions and attestations -- never inferred from natural language. Template
    v1 only understands two proposition kinds (``threshold`` and ``deadline``); any
    other kind, or either proposition not ``status=="extracted"``, is insufficient
    evidence (the fact simply isn't usable yet), as is an unparsable threshold
    ``value``. Everything else that breaks the implication -- a kind, subject,
    resolution-source, or (for deadlines) ``threshold_text`` mismatch; an attested
    ``threshold_inclusive`` of ``None``; a deadline-kind attestation with
    ``deadline_utc`` of ``None``; an unrecognized or mismatched threshold predicate
    direction; or a bound/deadline ordering that doesn't support A => B -- rejects as
    ``IMPLICATION_INVALID``: in each of those cases the proposition and its
    attestation both exist, but the specific fact this template needs was never
    attested (or contradicts the other leg's), which is a different failure mode than
    missing evidence.

    Rule-version currency, tie, and void checks apply the same semantics as the other
    templates, evaluated over both legs' attestations independently: a void or tie
    condition on either leg is enough to affect the whole proof, since the basket
    holds both legs simultaneously. A void member with ``refund_at_cost`` contributes
    its own excluded state; ``resolve_to_rules_price`` contributes one combined
    ``void`` terminal state in which each leg pays its own attestation's
    ``loser_payout_per_share`` (never the other leg's).

    A candidate whose legs don't fit this shape (not exactly 2 legs, or legs sharing
    one market_id) is a structural integrity error, as is a candidate whose
    ``propositions`` don't line up one-per-leg -- raised rather than silently
    building mismatched per-leg state.
    """
    legs = candidate.legs
    if len(legs) != 2 or legs[0].market_id == legs[1].market_id:
        raise ValueError(
            "a logical_implication candidate must have exactly 2 legs from 2 "
            f"distinct markets; got legs={legs!r}"
        )
    if len(candidate.propositions) != len(legs):
        raise ValueError(
            "a logical_implication candidate requires exactly one TypedProposition "
            "per leg (propositions[i] belongs to legs[i]); got "
            f"propositions={candidate.propositions!r} legs={legs!r}"
        )

    leg_a, leg_b = legs
    prop_a, prop_b = candidate.propositions
    rule_version_ids = (leg_a.rule_version_id, leg_b.rule_version_id)

    attestation_a = attestations.get(leg_a.rule_version_id)
    attestation_b = attestations.get(leg_b.rule_version_id)
    if attestation_a is None or attestation_b is None:
        return _finalize(
            _terminal_artifact(
                candidate,
                template=_LOGICAL_IMPLICATION_TEMPLATE,
                compiler_version=_LOGICAL_IMPLICATION_COMPILER_VERSION,
                status="insufficient_evidence",
                rejection_reason="MISSING_ATTESTATION",
                rule_version_ids=rule_version_ids,
                source_hashes=_sorted_unique_hashes(leg.rule_source_hash for leg in legs),
                as_of=as_of,
                review_identity=review_identity,
            )
        )

    source_hashes = _sorted_unique_hashes(
        [leg.rule_source_hash for leg in legs]
        + [attestation_a.rule_source_hash, attestation_b.rule_source_hash]
    )

    def _reject(reason: ProofRejectionReason, *, status: ProofStatus = "rejected") -> ProofArtifact:
        return _finalize(
            _terminal_artifact(
                candidate,
                template=_LOGICAL_IMPLICATION_TEMPLATE,
                compiler_version=_LOGICAL_IMPLICATION_COMPILER_VERSION,
                status=status,
                rejection_reason=reason,
                rule_version_ids=rule_version_ids,
                source_hashes=source_hashes,
                as_of=as_of,
                review_identity=review_identity,
            )
        )

    if (
        prop_a.kind not in ("threshold", "deadline")
        or prop_a.status != "extracted"
        or prop_b.kind not in ("threshold", "deadline")
        or prop_b.status != "extracted"
    ):
        return _reject("PROPOSITIONS_NOT_EXTRACTED", status="insufficient_evidence")

    if prop_a.kind != prop_b.kind:
        return _reject("IMPLICATION_INVALID")

    if prop_a.subject != prop_b.subject:
        return _reject("IMPLICATION_INVALID")

    if attestation_a.resolution_source_attested != attestation_b.resolution_source_attested:
        return _reject("IMPLICATION_INVALID")

    if prop_a.kind == "threshold":
        # Thresholds additionally require both legs to attest the same resolution
        # deadline, so the implication holds over the same observation window.
        if attestation_a.deadline_utc != attestation_b.deadline_utc:
            return _reject("IMPLICATION_INVALID")

        value_a = _parse_decimal(prop_a.value)
        value_b = _parse_decimal(prop_b.value)
        if value_a is None or value_b is None:
            return _reject("PROPOSITIONS_NOT_EXTRACTED", status="insufficient_evidence")

        if attestation_a.threshold_inclusive is None or attestation_b.threshold_inclusive is None:
            return _reject("IMPLICATION_INVALID")

        direction_a = _threshold_direction(prop_a.predicate)
        direction_b = _threshold_direction(prop_b.predicate)
        if direction_a is None or direction_b is None or direction_a != direction_b:
            return _reject("IMPLICATION_INVALID")

        if not _threshold_implies(
            value_a,
            attestation_a.threshold_inclusive,
            value_b,
            attestation_b.threshold_inclusive,
            direction_a,
        ):
            return _reject("IMPLICATION_INVALID")
    else:  # prop_a.kind == "deadline"
        if attestation_a.threshold_text != attestation_b.threshold_text:
            return _reject("IMPLICATION_INVALID")
        if attestation_a.deadline_utc is None or attestation_b.deadline_utc is None:
            return _reject("IMPLICATION_INVALID")
        if attestation_a.deadline_utc > attestation_b.deadline_utc:
            return _reject("IMPLICATION_INVALID")

    if not all(leg.rule_version_id in rule_versions for leg in legs):
        return _reject("RULE_VERSION_CHANGED")

    if attestation_a.tie_possible or attestation_b.tie_possible:
        return _reject("TIE_UNMODELED")

    excluded_states: list[ExcludedState] = []
    model_combined_void_state = False

    void_attestations = [a for a in (attestation_a, attestation_b) if a.void_or_invalid_possible]
    if void_attestations:
        void_behaviors = {a.void_behavior for a in void_attestations}
        if "unknown" in void_behaviors or len(void_behaviors) > 1:
            return _reject("VOID_BEHAVIOR_UNKNOWN")
        (void_behavior,) = void_behaviors
        if void_behavior == "refund_at_cost":
            for attestation in void_attestations:
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

    winner_a = attestation_a.winner_payout_per_share
    loser_a = attestation_a.loser_payout_per_share
    winner_b = attestation_b.winner_payout_per_share
    loser_b = attestation_b.loser_payout_per_share
    market_a = leg_a.market_id
    market_b = leg_b.market_id

    terminal_states: list[TerminalState] = [
        TerminalState(
            state_id="neither",
            description=(
                f"{market_a}: proposition A resolves false (NO(A) wins); {market_b}: "
                "proposition B resolves false (YES(B) loses)."
            ),
            leg_payouts=(winner_a, loser_b),
        ),
        TerminalState(
            state_id="b_only",
            description=(
                f"{market_a}: proposition A resolves false (NO(A) wins); {market_b}: "
                "proposition B resolves true (YES(B) wins)."
            ),
            leg_payouts=(winner_a, winner_b),
        ),
        TerminalState(
            state_id="both",
            description=(
                f"{market_a}: proposition A resolves true (NO(A) loses); {market_b}: "
                "proposition B resolves true (YES(B) wins)."
            ),
            leg_payouts=(loser_a, winner_b),
        ),
    ]

    if model_combined_void_state:
        terminal_states.append(
            TerminalState(
                state_id="void",
                description=(
                    f"{market_a} and {market_b} both resolve void together; each leg "
                    "settles at its own attested rules (loser) price."
                ),
                leg_payouts=(loser_a, loser_b),
            )
        )

    # The a_without_b combination (A true, B false) is excluded because the verified
    # implication makes it logically impossible, not because of a void condition --
    # this excluded state is unconditional on the proof being proof_ready.
    excluded_states.insert(
        0,
        ExcludedState(
            description=(
                f"{market_a}: proposition A resolves true; {market_b}: proposition B "
                "resolves false."
            ),
            exclusion_reason=(
                "proposition A deterministically implies proposition B per the "
                "attested rules, so A resolving true with B resolving false is a "
                "logically impossible combination, not modeled as a terminal state"
            ),
            attestation_id=attestation_a.attestation_id,
        ),
    )

    assumption = ProofAssumption(
        claim=(
            f"{market_a}'s proposition ({prop_a.subject!r} {prop_a.predicate} "
            f"{prop_a.value}) deterministically implies {market_b}'s proposition "
            f"({prop_b.subject!r} {prop_b.predicate} {prop_b.value}), per the "
            "attested rules"
        ),
        attestation_id=attestation_a.attestation_id,
        supporting_spans=prop_a.supporting_spans + prop_b.supporting_spans,
    )

    payout_sums: list[Decimal] = [sum(state.leg_payouts) for state in terminal_states]

    artifact = ProofArtifact(
        schema_version=1,
        proof_id=_PLACEHOLDER_PROOF_ID,
        candidate_id=candidate.candidate_id,
        template=_LOGICAL_IMPLICATION_TEMPLATE,
        compiler_version=_LOGICAL_IMPLICATION_COMPILER_VERSION,
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


def _compile_cross_venue_equivalence(
    candidate: CandidateRelationship,
    rule_versions: Mapping[UUID, RuleVersion],
    attestations: Mapping[UUID, RuleAttestation],
    *,
    as_of: datetime,
    review_identity: str,
) -> ProofArtifact:
    """Implements the ``cross_venue_equivalence@1`` template (Engine D).

    Leg 0 and leg 1 are each one market on a distinct venue, independently attested
    (unlike ``binary_complement``, they never share a ``rule_version_id``). Rather than
    directly modeling a payoff table, this template's real work is compiling an
    ``EquivalenceMatrix``: an 8-dimension compatibility verdict comparing the two legs'
    attestations field-by-field, reusing the identical field names as increment 2's
    ``scout_bridge._ENGINE_D_UNRESOLVED_FIELDS`` -- a cross-venue nomination starts
    with all 8 fields unresolved, and this compiler is the only thing permitted to
    narrow them.

    Every dimension must independently be derived as ``"compatible"``,
    ``"incompatible"``, or ``"unknown"`` from only the two attestations' own fields --
    never inferred from anything else, and never guessed when the underlying fact
    was never attested. Two dimensions (``settlement_finality_timing``,
    ``venue_access_custody_rules``) have no attested basis anywhere in v1's
    ``RuleAttestation`` model, so they are unconditionally ``"unknown"``: this means
    ``proof_ready`` is UNREACHABLE for this template in v1 -- every compiled artifact
    rejects, at minimum on those two dimensions. That is the spec-correct fail-closed
    outcome (equivalence across venues is a strong claim; v1 simply doesn't yet attest
    enough to support it), not a bug to soften.

    Rejection precedence when the matrix carries more than one non-``"compatible"``
    dimension: ``"incompatible"`` wins over ``"unknown"`` for the artifact's single
    ``rejection_reason`` -- a proven divergence is a stronger, more specific finding
    than an absence of evidence, so ``EQUIVALENCE_DIMENSION_INCOMPATIBLE`` is reported
    whenever any dimension is incompatible, even if others are also unknown. Only when
    no dimension is incompatible (but at least one is unknown, which given the two
    always-unknown dimensions is every artifact in v1) does the artifact reject as
    ``EQUIVALENCE_DIMENSION_UNKNOWN``.

    Within the ``void_dispute_behavior`` dimension specifically, "needed but unknown"
    is checked before equality: either leg attesting ``void_or_invalid_possible=True``
    with ``void_behavior=="unknown"`` makes that dimension ``"unknown"`` regardless of
    whether the other leg's void tuple matches, since the fact this dimension needs
    (how a void resolves) was never actually attested on that leg.

    Were ``proof_ready`` ever reachable (a future increment attesting the two
    currently-unknown dimensions), the basket is buying opposite sides across the two
    equivalent legs -- YES on leg 0, NO on leg 1 -- yielding exactly two terminal
    states (the proposition resolving true or false, identically on both venues since
    they're proven equivalent): no excluded states, and no separate void/tie payoff
    modeling, since a "compatible" ``void_dispute_behavior`` dimension already
    guarantees the two legs' void/tie facts agree.

    Check order: missing attestation (either leg) is checked first, before rule-version
    currency, before the matrix can even be built -- an all-``"unknown"`` matrix is
    still emitted (using whichever attestation IDs exist as its basis) since
    ``ProofArtifact`` requires a matrix on every status for this template, never only
    on ``proof_ready``.

    A candidate whose legs don't fit this shape (not exactly 2 legs) is a structural
    integrity error, not a research outcome -- raised rather than silently reading only
    ``legs[0]``/``legs[1]``. Legs spanning at least two distinct venues is already
    enforced by ``CandidateRelationship`` itself.
    """
    legs = candidate.legs
    if len(legs) != 2:
        raise ValueError(
            "a cross_venue_equivalence candidate must have exactly 2 legs (spanning "
            f"two distinct venues); got legs={legs!r}"
        )
    leg_a, leg_b = legs
    rule_version_ids = (leg_a.rule_version_id, leg_b.rule_version_id)

    attestation_a = attestations.get(leg_a.rule_version_id)
    attestation_b = attestations.get(leg_b.rule_version_id)
    if attestation_a is None or attestation_b is None:
        basis_ids = tuple(
            sorted(
                attestation.attestation_id
                for attestation in (attestation_a, attestation_b)
                if attestation is not None
            )
        )
        return _finalize(
            _terminal_artifact(
                candidate,
                template=_CROSS_VENUE_EQUIVALENCE_TEMPLATE,
                compiler_version=_CROSS_VENUE_EQUIVALENCE_COMPILER_VERSION,
                status="insufficient_evidence",
                rejection_reason="MISSING_ATTESTATION",
                rule_version_ids=rule_version_ids,
                source_hashes=_sorted_unique_hashes(leg.rule_source_hash for leg in legs),
                as_of=as_of,
                review_identity=review_identity,
                equivalence_matrix=_all_unknown_equivalence_matrix(basis_ids),
            )
        )

    source_hashes = _sorted_unique_hashes(
        [leg.rule_source_hash for leg in legs]
        + [attestation_a.rule_source_hash, attestation_b.rule_source_hash]
    )
    basis_ids = tuple(sorted((attestation_a.attestation_id, attestation_b.attestation_id)))

    if not all(leg.rule_version_id in rule_versions for leg in legs):
        return _finalize(
            _terminal_artifact(
                candidate,
                template=_CROSS_VENUE_EQUIVALENCE_TEMPLATE,
                compiler_version=_CROSS_VENUE_EQUIVALENCE_COMPILER_VERSION,
                status="rejected",
                rejection_reason="RULE_VERSION_CHANGED",
                rule_version_ids=rule_version_ids,
                source_hashes=source_hashes,
                as_of=as_of,
                review_identity=review_identity,
                equivalence_matrix=_all_unknown_equivalence_matrix(basis_ids),
            )
        )

    matrix = _build_equivalence_matrix(attestation_a, attestation_b, basis_ids)
    dimensions = (
        matrix.proposition_threshold_inclusivity,
        matrix.observation_period_timezone,
        matrix.resolution_sources,
        matrix.void_dispute_behavior,
        matrix.outcome_completeness,
        matrix.denomination_collateral_rounding,
        matrix.settlement_finality_timing,
        matrix.venue_access_custody_rules,
    )

    def _reject(reason: ProofRejectionReason) -> ProofArtifact:
        return _finalize(
            _terminal_artifact(
                candidate,
                template=_CROSS_VENUE_EQUIVALENCE_TEMPLATE,
                compiler_version=_CROSS_VENUE_EQUIVALENCE_COMPILER_VERSION,
                status="rejected",
                rejection_reason=reason,
                rule_version_ids=rule_version_ids,
                source_hashes=source_hashes,
                as_of=as_of,
                review_identity=review_identity,
                equivalence_matrix=matrix,
            )
        )

    # Incompatible wins over unknown: a proven divergence is a stronger finding than
    # an absence of evidence (see docstring).
    if "incompatible" in dimensions:
        return _reject("EQUIVALENCE_DIMENSION_INCOMPATIBLE")
    if "unknown" in dimensions:
        return _reject("EQUIVALENCE_DIMENSION_UNKNOWN")

    # Unreachable in v1 (settlement_finality_timing and venue_access_custody_rules are
    # always "unknown" above), retained so a future increment that attests those two
    # dimensions doesn't need to touch this branch. Split into a pure helper
    # (_equivalence_terminal_states) so the payoff-table shape has direct unit test
    # coverage today, despite being unreachable through compile_proof itself in v1.
    market_a = leg_a.market_id
    market_b = leg_b.market_id
    terminal_states = _equivalence_terminal_states(attestation_a, attestation_b, market_a, market_b)
    payout_sums: list[Decimal] = [sum(state.leg_payouts) for state in terminal_states]

    assumption = ProofAssumption(
        claim=(
            f"{market_a} (leg 0) and {market_b} (leg 1) attest an identical proposition "
            "across venues per every equivalence dimension"
        ),
        attestation_id=attestation_a.attestation_id,
        supporting_spans=attestation_a.supporting_spans + attestation_b.supporting_spans,
    )

    artifact = ProofArtifact(
        schema_version=1,
        proof_id=_PLACEHOLDER_PROOF_ID,
        candidate_id=candidate.candidate_id,
        template=_CROSS_VENUE_EQUIVALENCE_TEMPLATE,
        compiler_version=_CROSS_VENUE_EQUIVALENCE_COMPILER_VERSION,
        status="proof_ready",
        rejection_reason=None,
        terminal_states=terminal_states,
        minimum_basket_payout=min(payout_sums),
        maximum_basket_payout=max(payout_sums),
        assumptions=(assumption,),
        excluded_states=(),
        equivalence_matrix=matrix,
        rule_version_ids=rule_version_ids,
        source_hashes=source_hashes,
        review_identity=review_identity,
        invalidation_conditions=(_RULE_VERSION_CHANGE_CONDITION,),
        information_cutoff=as_of,
        observed_at=as_of,
    )
    return _finalize(artifact)


def _equivalence_terminal_states(
    attestation_a: RuleAttestation,
    attestation_b: RuleAttestation,
    market_a: str,
    market_b: str,
) -> tuple[TerminalState, TerminalState]:
    """The two-state payoff table for a fully-``"compatible"`` equivalence matrix.

    Basket = YES(leg 0) + NO(leg 1) ("opposite sides"): since the two legs' proposition
    is proven equivalent, exactly one of these two states can occur. Pure and total
    over any two attestations -- the caller (``_compile_cross_venue_equivalence``) is
    responsible for only invoking this once every matrix dimension is ``"compatible"``,
    which in v1 never actually happens (see that function's docstring), so this helper
    exists to give the payoff-table shape direct unit test coverage today, ahead of any
    future increment that makes the ``proof_ready`` path reachable.
    """
    return (
        TerminalState(
            state_id="proposition_true",
            description=(
                f"{market_a} and {market_b}: the shared proposition resolves true on "
                "both venues (proven equivalent); YES(leg 0) wins, NO(leg 1) loses."
            ),
            leg_payouts=(
                attestation_a.winner_payout_per_share,
                attestation_b.loser_payout_per_share,
            ),
        ),
        TerminalState(
            state_id="proposition_false",
            description=(
                f"{market_a} and {market_b}: the shared proposition resolves false on "
                "both venues (proven equivalent); NO(leg 1) wins, YES(leg 0) loses."
            ),
            leg_payouts=(
                attestation_a.loser_payout_per_share,
                attestation_b.winner_payout_per_share,
            ),
        ),
    )


def _build_equivalence_matrix(
    attestation_a: RuleAttestation,
    attestation_b: RuleAttestation,
    basis_attestation_ids: tuple[UUID, ...],
) -> EquivalenceMatrix:
    return EquivalenceMatrix(
        proposition_threshold_inclusivity=_threshold_inclusivity_dimension(
            attestation_a, attestation_b
        ),
        observation_period_timezone=_deadline_dimension(attestation_a, attestation_b),
        resolution_sources=_resolution_sources_dimension(attestation_a, attestation_b),
        void_dispute_behavior=_void_dispute_behavior_dimension(attestation_a, attestation_b),
        outcome_completeness=_outcome_completeness_dimension(attestation_a, attestation_b),
        denomination_collateral_rounding=_denomination_dimension(attestation_a, attestation_b),
        settlement_finality_timing="unknown",
        venue_access_custody_rules="unknown",
        basis_attestation_ids=basis_attestation_ids,
    )


def _all_unknown_equivalence_matrix(
    basis_attestation_ids: tuple[UUID, ...],
) -> EquivalenceMatrix:
    return EquivalenceMatrix(
        proposition_threshold_inclusivity="unknown",
        observation_period_timezone="unknown",
        resolution_sources="unknown",
        void_dispute_behavior="unknown",
        outcome_completeness="unknown",
        denomination_collateral_rounding="unknown",
        settlement_finality_timing="unknown",
        venue_access_custody_rules="unknown",
        basis_attestation_ids=basis_attestation_ids,
    )


def _threshold_inclusivity_dimension(
    attestation_a: RuleAttestation, attestation_b: RuleAttestation
) -> EquivalenceDimensionResult:
    if attestation_a.threshold_text is None or attestation_b.threshold_text is None:
        return "unknown"
    if (
        attestation_a.threshold_text == attestation_b.threshold_text
        and attestation_a.threshold_inclusive == attestation_b.threshold_inclusive
    ):
        return "compatible"
    return "incompatible"


def _deadline_dimension(
    attestation_a: RuleAttestation, attestation_b: RuleAttestation
) -> EquivalenceDimensionResult:
    if attestation_a.deadline_utc is None or attestation_b.deadline_utc is None:
        return "unknown"
    if attestation_a.deadline_utc == attestation_b.deadline_utc:
        return "compatible"
    return "incompatible"


def _resolution_sources_dimension(
    attestation_a: RuleAttestation, attestation_b: RuleAttestation
) -> EquivalenceDimensionResult:
    if attestation_a.resolution_source_attested == attestation_b.resolution_source_attested:
        return "compatible"
    return "incompatible"


def _void_dispute_behavior_dimension(
    attestation_a: RuleAttestation, attestation_b: RuleAttestation
) -> EquivalenceDimensionResult:
    # "Needed but unknown" is checked before equality: either leg attesting a possible
    # void with an unknown behavior means this dimension's underlying fact was never
    # actually attested on that leg, regardless of what the other leg says.
    if (attestation_a.void_or_invalid_possible and attestation_a.void_behavior == "unknown") or (
        attestation_b.void_or_invalid_possible and attestation_b.void_behavior == "unknown"
    ):
        return "unknown"
    tuple_a = (
        attestation_a.void_or_invalid_possible,
        attestation_a.void_behavior,
        attestation_a.tie_possible,
        attestation_a.tie_behavior,
    )
    tuple_b = (
        attestation_b.void_or_invalid_possible,
        attestation_b.void_behavior,
        attestation_b.tie_possible,
        attestation_b.tie_behavior,
    )
    return "compatible" if tuple_a == tuple_b else "incompatible"


def _outcome_completeness_dimension(
    attestation_a: RuleAttestation, attestation_b: RuleAttestation
) -> EquivalenceDimensionResult:
    if attestation_a.outcome_set_exhaustive and attestation_b.outcome_set_exhaustive:
        return "compatible"
    return "incompatible"


def _denomination_dimension(
    attestation_a: RuleAttestation, attestation_b: RuleAttestation
) -> EquivalenceDimensionResult:
    if (
        attestation_a.payout_unit == attestation_b.payout_unit
        and attestation_a.winner_payout_per_share == attestation_b.winner_payout_per_share
        and attestation_a.loser_payout_per_share == attestation_b.loser_payout_per_share
    ):
        return "compatible"
    return "incompatible"


def _parse_decimal(value: str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(value)
    except InvalidOperation:
        return None


def _threshold_direction(predicate: str) -> Literal["up", "down"] | None:
    if predicate in _THRESHOLD_UP_PREDICATES:
        return "up"
    if predicate in _THRESHOLD_DOWN_PREDICATES:
        return "down"
    return None


def _threshold_implies(
    value_a: Decimal,
    inclusive_a: bool,
    value_b: Decimal,
    inclusive_b: bool,
    direction: Literal["up", "down"],
) -> bool:
    """True iff proposition A's threshold bound deterministically implies B's.

    Modeling each bound as an interval on the real line -- ``[value, inf)`` when
    inclusive or ``(value, inf)`` when exclusive for an "up" bound, mirrored for
    "down" -- A implies B iff A's interval is a subset of B's. That collapses to
    comparing the two threshold values, tie-broken by A's own inclusivity only when
    both propositions share the same threshold value and B's bound is exclusive (an
    exclusive B excludes its own boundary value, so A can only be a subset there if A
    excludes it too).
    """
    if direction == "up":
        if inclusive_b:
            return value_a >= value_b
        return value_a > value_b or (value_a == value_b and not inclusive_a)
    if inclusive_b:
        return value_a <= value_b
    return value_a < value_b or (value_a == value_b and not inclusive_a)


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
    equivalence_matrix: EquivalenceMatrix | None = None,
) -> ProofArtifact:
    """Build a non-``proof_ready`` (``rejected``/``insufficient_evidence``) artifact.

    ``equivalence_matrix`` is only ever non-``None`` for the ``cross_venue_equivalence@1``
    template: ``ProofArtifact`` requires a matrix on every status for that template,
    including rejection (see ``ProofArtifact._require_consistent_proof``), while every
    other template never carries one.
    """
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
        equivalence_matrix=equivalence_matrix,
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
