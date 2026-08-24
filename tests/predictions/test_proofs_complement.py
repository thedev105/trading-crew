from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from polytrading.predictions.candidates_models import RelationshipType
from polytrading.predictions.domain import PredictionVenue
from polytrading.predictions.proofs import compile_proof
from tests.predictions.attestation_helpers import rule_attestation
from tests.predictions.candidate_helpers import HASH, RULE_VERSION_ID, candidate_relationship, leg
from tests.predictions.domain_helpers import rule_version

NOW = datetime(2026, 8, 15, 12, tzinfo=UTC)
REVIEW_IDENTITY = "reviewer@example.test"


def _candidate(**overrides):
    return candidate_relationship(**overrides)


def _attestation(**overrides):
    values = {
        "rule_version_id": RULE_VERSION_ID,
        "market_id": "0xcondition",
        "venue": PredictionVenue.POLYMARKET,
        "rule_source_hash": HASH,
    }
    values.update(overrides)
    return rule_attestation(**values)


def _rule_versions(**overrides):
    values = {
        "rule_version_id": RULE_VERSION_ID,
        "market_id": "0xcondition",
        "venue": PredictionVenue.POLYMARKET,
        "source_hash": HASH,
    }
    values.update(overrides)
    return {RULE_VERSION_ID: rule_version(**values)}


def test_happy_path_emits_proof_ready_with_expected_states_and_bounds() -> None:
    candidate = _candidate()
    attestation = _attestation()
    rule_versions = _rule_versions()

    artifact = compile_proof(
        candidate,
        rule_versions,
        {RULE_VERSION_ID: attestation},
        as_of=NOW,
        review_identity=REVIEW_IDENTITY,
    )

    assert artifact.status == "proof_ready"
    assert artifact.rejection_reason is None
    assert artifact.template == "binary_complement@1"
    assert artifact.compiler_version == "1"
    assert {state.state_id for state in artifact.terminal_states} == {
        "outcome_0_wins",
        "outcome_1_wins",
    }
    for state in artifact.terminal_states:
        assert len(state.leg_payouts) == len(candidate.legs)
    winner = attestation.winner_payout_per_share
    loser = attestation.loser_payout_per_share
    assert artifact.minimum_basket_payout == winner + loser
    assert artifact.maximum_basket_payout == winner + loser
    assert artifact.rule_version_ids == tuple(cl.rule_version_id for cl in candidate.legs)
    assert artifact.source_hashes == (HASH,)
    assert artifact.excluded_states == ()
    assert artifact.equivalence_matrix is None
    assert artifact.candidate_id == candidate.candidate_id
    assert artifact.review_identity == REVIEW_IDENTITY
    assert artifact.information_cutoff == NOW
    assert artifact.observed_at == NOW
    assert "any participating rule_version change" in artifact.invalidation_conditions
    assert len(artifact.assumptions) >= 1
    assert artifact.assumptions[0].attestation_id == attestation.attestation_id
    assert artifact.assumptions[0].supporting_spans == attestation.supporting_spans


def test_missing_attestation_yields_insufficient_evidence() -> None:
    candidate = _candidate()
    rule_versions = _rule_versions()

    artifact = compile_proof(
        candidate,
        rule_versions,
        {},
        as_of=NOW,
        review_identity=REVIEW_IDENTITY,
    )

    assert artifact.status == "insufficient_evidence"
    assert artifact.rejection_reason == "MISSING_ATTESTATION"
    assert artifact.terminal_states == ()
    assert artifact.minimum_basket_payout is None
    assert artifact.maximum_basket_payout is None
    assert artifact.source_hashes == (HASH,)


def test_non_exhaustive_outcome_set_is_rejected() -> None:
    candidate = _candidate()
    attestation = _attestation(outcome_set_exhaustive=False)
    rule_versions = _rule_versions()

    artifact = compile_proof(
        candidate,
        rule_versions,
        {RULE_VERSION_ID: attestation},
        as_of=NOW,
        review_identity=REVIEW_IDENTITY,
    )

    assert artifact.status == "rejected"
    assert artifact.rejection_reason == "OUTCOME_SET_NOT_EXHAUSTIVE"
    assert artifact.terminal_states == ()
    assert artifact.minimum_basket_payout is None


def test_void_behavior_unknown_is_rejected() -> None:
    candidate = _candidate()
    attestation = _attestation(void_or_invalid_possible=True, void_behavior="unknown")
    rule_versions = _rule_versions()

    artifact = compile_proof(
        candidate,
        rule_versions,
        {RULE_VERSION_ID: attestation},
        as_of=NOW,
        review_identity=REVIEW_IDENTITY,
    )

    assert artifact.status == "rejected"
    assert artifact.rejection_reason == "VOID_BEHAVIOR_UNKNOWN"


def test_tie_possible_is_rejected_as_unmodeled() -> None:
    candidate = _candidate()
    attestation = _attestation(tie_possible=True, tie_behavior="split evenly")
    rule_versions = _rule_versions()

    artifact = compile_proof(
        candidate,
        rule_versions,
        {RULE_VERSION_ID: attestation},
        as_of=NOW,
        review_identity=REVIEW_IDENTITY,
    )

    assert artifact.status == "rejected"
    assert artifact.rejection_reason == "TIE_UNMODELED"


def test_changed_rule_version_is_rejected() -> None:
    candidate = _candidate()
    attestation = _attestation()

    artifact = compile_proof(
        candidate,
        {},  # current registry state no longer contains this rule version
        {RULE_VERSION_ID: attestation},
        as_of=NOW,
        review_identity=REVIEW_IDENTITY,
    )

    assert artifact.status == "rejected"
    assert artifact.rejection_reason == "RULE_VERSION_CHANGED"


def test_void_refund_at_cost_adds_excluded_state_not_a_terminal_state() -> None:
    candidate = _candidate()
    attestation = _attestation(void_or_invalid_possible=True, void_behavior="refund_at_cost")
    rule_versions = _rule_versions()

    artifact = compile_proof(
        candidate,
        rule_versions,
        {RULE_VERSION_ID: attestation},
        as_of=NOW,
        review_identity=REVIEW_IDENTITY,
    )

    assert artifact.status == "proof_ready"
    assert {state.state_id for state in artifact.terminal_states} == {
        "outcome_0_wins",
        "outcome_1_wins",
    }
    assert len(artifact.excluded_states) == 1
    assert artifact.excluded_states[0].attestation_id == attestation.attestation_id


def test_void_resolve_to_rules_price_adds_modeled_void_terminal_state() -> None:
    candidate = _candidate()
    attestation = _attestation(
        void_or_invalid_possible=True,
        void_behavior="resolve_to_rules_price",
        winner_payout_per_share=Decimal("1"),
        loser_payout_per_share=Decimal("0.5"),
    )
    rule_versions = _rule_versions()

    artifact = compile_proof(
        candidate,
        rule_versions,
        {RULE_VERSION_ID: attestation},
        as_of=NOW,
        review_identity=REVIEW_IDENTITY,
    )

    assert artifact.status == "proof_ready"
    assert {state.state_id for state in artifact.terminal_states} == {
        "outcome_0_wins",
        "outcome_1_wins",
        "void",
    }
    void_state = next(s for s in artifact.terminal_states if s.state_id == "void")
    assert void_state.leg_payouts == (Decimal("0.5"), Decimal("0.5"))
    assert artifact.minimum_basket_payout == Decimal("1.0")
    assert artifact.maximum_basket_payout == Decimal("1.5")
    assert artifact.excluded_states == ()


def test_compile_proof_is_deterministic_and_idempotent() -> None:
    candidate = _candidate()
    attestation = _attestation()
    rule_versions = _rule_versions()

    first = compile_proof(
        candidate,
        rule_versions,
        {RULE_VERSION_ID: attestation},
        as_of=NOW,
        review_identity=REVIEW_IDENTITY,
    )
    second = compile_proof(
        candidate,
        rule_versions,
        {RULE_VERSION_ID: attestation},
        as_of=NOW,
        review_identity=REVIEW_IDENTITY,
    )

    assert first == second
    assert first.proof_id == second.proof_id


def test_unimplemented_relationship_type_raises_not_implemented_error() -> None:
    # CROSS_VENUE_EQUIVALENCE is the only relationship_type without a compiler yet
    # (Task 9); it requires legs spanning at least two distinct venues.
    candidate = _candidate(
        relationship_type=RelationshipType.CROSS_VENUE_EQUIVALENCE,
        legs=(leg(), leg(venue=PredictionVenue.KALSHI, market_id="0xother", outcome_index=1)),
    )

    with pytest.raises(NotImplementedError):
        compile_proof(
            candidate,
            {},
            {},
            as_of=NOW,
            review_identity=REVIEW_IDENTITY,
        )


def test_three_leg_complement_candidate_is_a_structural_error() -> None:
    # A malformed candidate: BINARY_COMPLEMENT with 3 legs of the same market. Silently
    # reading only legs[0]/legs[1] would drop legs[2] and still emit 2-tuple
    # leg_payouts, violating the "leg_payouts length == len(candidate.legs)" invariant
    # this compiler owns -- so this must raise, not reject or ignore.
    candidate = _candidate(legs=(leg(), leg(outcome_index=1), leg(outcome_index=2)))
    attestation = _attestation()
    rule_versions = _rule_versions()

    with pytest.raises(ValueError, match="exactly 2 legs"):
        compile_proof(
            candidate,
            rule_versions,
            {RULE_VERSION_ID: attestation},
            as_of=NOW,
            review_identity=REVIEW_IDENTITY,
        )


def test_cross_market_two_leg_complement_candidate_is_a_structural_error() -> None:
    # A malformed candidate: 2 legs, same venue (satisfies CandidateRelationship's own
    # validator), but from two different markets. Treating legs[0]/legs[1] as one
    # market's two outcomes here would silently apply leg 0's market's attestation to
    # an unrelated market -- must raise rather than guess.
    candidate = _candidate(
        legs=(leg(), leg(outcome_index=1, market_id="0xother", outcome_token_id="222"))
    )
    attestation = _attestation()
    rule_versions = _rule_versions()

    with pytest.raises(ValueError, match="market_id"):
        compile_proof(
            candidate,
            rule_versions,
            {RULE_VERSION_ID: attestation},
            as_of=NOW,
            review_identity=REVIEW_IDENTITY,
        )


def test_missing_attestation_wins_over_changed_rule_version() -> None:
    # Locks in the documented check order: MISSING_ATTESTATION is checked before the
    # RULE_VERSION_CHANGED cross-check, so when both conditions hold simultaneously the
    # attestation check wins.
    candidate = _candidate()

    artifact = compile_proof(
        candidate,
        {},  # rule version also absent from current registry state
        {},  # no attestation at all
        as_of=NOW,
        review_identity=REVIEW_IDENTITY,
    )

    assert artifact.status == "insufficient_evidence"
    assert artifact.rejection_reason == "MISSING_ATTESTATION"
