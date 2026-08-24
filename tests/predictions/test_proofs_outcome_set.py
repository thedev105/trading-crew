from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest

from polytrading.predictions.candidates_models import RelationshipType
from polytrading.predictions.domain import PredictionVenue
from polytrading.predictions.proofs import compile_proof
from tests.predictions.attestation_helpers import rule_attestation
from tests.predictions.candidate_helpers import HASH, candidate_relationship, leg
from tests.predictions.domain_helpers import rule_version

NOW = datetime(2026, 8, 15, 12, tzinfo=UTC)
REVIEW_IDENTITY = "reviewer@example.test"

_MEMBER_MARKET_IDS = ("0xmember-a", "0xmember-b", "0xmember-c")
_MEMBER_RULE_VERSION_IDS = (
    UUID("00000000-0000-0000-0000-000000007001"),
    UUID("00000000-0000-0000-0000-000000007002"),
    UUID("00000000-0000-0000-0000-000000007003"),
)
_MEMBER_ATTESTATION_IDS = (
    UUID("00000000-0000-0000-0000-000000007101"),
    UUID("00000000-0000-0000-0000-000000007102"),
    UUID("00000000-0000-0000-0000-000000007103"),
)


def _member_legs(n: int = 3, **shared_overrides):
    return tuple(
        leg(
            market_id=_MEMBER_MARKET_IDS[i],
            outcome_index=None,
            outcome_token_id=None,
            rule_version_id=_MEMBER_RULE_VERSION_IDS[i],
            **shared_overrides,
        )
        for i in range(n)
    )


def _candidate(n: int = 3, **overrides):
    values = {
        "relationship_type": RelationshipType.EXHAUSTIVE_OUTCOME_SET,
        "legs": _member_legs(n),
    }
    values.update(overrides)
    return candidate_relationship(**values)


def _member_attestation(i: int, **overrides):
    values = {
        "attestation_id": _MEMBER_ATTESTATION_IDS[i],
        "rule_version_id": _MEMBER_RULE_VERSION_IDS[i],
        "market_id": _MEMBER_MARKET_IDS[i],
        "venue": PredictionVenue.POLYMARKET,
        "rule_source_hash": HASH,
    }
    values.update(overrides)
    return rule_attestation(**values)


def _attestations(n: int = 3, per_member_overrides: dict[int, dict] | None = None):
    per_member_overrides = per_member_overrides or {}
    result = {}
    for i in range(n):
        attestation = _member_attestation(i, **per_member_overrides.get(i, {}))
        result[_MEMBER_RULE_VERSION_IDS[i]] = attestation
    return result


def _member_rule_version(i: int, **overrides):
    values = {
        "rule_version_id": _MEMBER_RULE_VERSION_IDS[i],
        "market_id": _MEMBER_MARKET_IDS[i],
        "venue": PredictionVenue.POLYMARKET,
        "source_hash": HASH,
    }
    values.update(overrides)
    return rule_version(**values)


def _rule_versions(n: int = 3, exclude: set[int] | None = None):
    exclude = exclude or set()
    return {
        _MEMBER_RULE_VERSION_IDS[i]: _member_rule_version(i) for i in range(n) if i not in exclude
    }


def test_happy_path_emits_proof_ready_with_expected_states_and_bounds() -> None:
    candidate = _candidate()
    attestations = _attestations()
    rule_versions = _rule_versions()

    artifact = compile_proof(
        candidate,
        rule_versions,
        attestations,
        as_of=NOW,
        review_identity=REVIEW_IDENTITY,
    )

    assert artifact.status == "proof_ready"
    assert artifact.rejection_reason is None
    assert artifact.template == "exhaustive_outcome_set@1"
    assert artifact.compiler_version == "1"
    assert {state.state_id for state in artifact.terminal_states} == {
        "member_0_wins",
        "member_1_wins",
        "member_2_wins",
    }
    for state in artifact.terminal_states:
        assert len(state.leg_payouts) == len(candidate.legs)

    # All three members attest identical winner/loser values, so every terminal state
    # sums to winner + 2*loser -- min and max collapse to the same bound.
    winner = attestations[_MEMBER_RULE_VERSION_IDS[0]].winner_payout_per_share
    loser = attestations[_MEMBER_RULE_VERSION_IDS[0]].loser_payout_per_share
    expected_sum = winner + 2 * loser
    assert artifact.minimum_basket_payout == expected_sum
    assert artifact.maximum_basket_payout == expected_sum

    state_0 = next(s for s in artifact.terminal_states if s.state_id == "member_0_wins")
    assert state_0.leg_payouts == (winner, loser, loser)

    assert artifact.rule_version_ids == tuple(cl.rule_version_id for cl in candidate.legs)
    assert artifact.source_hashes == (HASH,)
    assert artifact.excluded_states == ()
    assert artifact.equivalence_matrix is None
    assert artifact.candidate_id == candidate.candidate_id
    assert artifact.review_identity == REVIEW_IDENTITY
    assert artifact.information_cutoff == NOW
    assert artifact.observed_at == NOW
    assert "any participating rule_version change" in artifact.invalidation_conditions

    # One ProofAssumption per member, each citing that member's own attestation.
    assert len(artifact.assumptions) == 3
    assumption_attestation_ids = {a.attestation_id for a in artifact.assumptions}
    assert assumption_attestation_ids == set(_MEMBER_ATTESTATION_IDS)


def test_per_member_distinct_payouts_use_each_members_own_attestation() -> None:
    candidate = _candidate()
    attestations = _attestations(
        per_member_overrides={
            0: {"winner_payout_per_share": Decimal("1"), "loser_payout_per_share": Decimal("0")},
            1: {
                "winner_payout_per_share": Decimal("0.9"),
                "loser_payout_per_share": Decimal("0.1"),
            },
            2: {
                "winner_payout_per_share": Decimal("0.8"),
                "loser_payout_per_share": Decimal("0.2"),
            },
        }
    )
    rule_versions = _rule_versions()

    artifact = compile_proof(
        candidate,
        rule_versions,
        attestations,
        as_of=NOW,
        review_identity=REVIEW_IDENTITY,
    )

    assert artifact.status == "proof_ready"
    state_0 = next(s for s in artifact.terminal_states if s.state_id == "member_0_wins")
    state_1 = next(s for s in artifact.terminal_states if s.state_id == "member_1_wins")
    state_2 = next(s for s in artifact.terminal_states if s.state_id == "member_2_wins")

    assert state_0.leg_payouts == (Decimal("1"), Decimal("0.1"), Decimal("0.2"))
    assert state_1.leg_payouts == (Decimal("0"), Decimal("0.9"), Decimal("0.2"))
    assert state_2.leg_payouts == (Decimal("0"), Decimal("0.1"), Decimal("0.8"))

    assert artifact.minimum_basket_payout == Decimal("0.9")
    assert artifact.maximum_basket_payout == Decimal("1.3")


def test_missing_attestation_yields_insufficient_evidence() -> None:
    candidate = _candidate()
    attestations = _attestations()
    del attestations[_MEMBER_RULE_VERSION_IDS[1]]
    rule_versions = _rule_versions()

    artifact = compile_proof(
        candidate,
        rule_versions,
        attestations,
        as_of=NOW,
        review_identity=REVIEW_IDENTITY,
    )

    assert artifact.status == "insufficient_evidence"
    assert artifact.rejection_reason == "MISSING_ATTESTATION"
    assert artifact.terminal_states == ()
    assert artifact.minimum_basket_payout is None
    assert artifact.maximum_basket_payout is None
    assert artifact.source_hashes == (HASH,)


def test_one_member_non_exhaustive_rejects_whole_group() -> None:
    candidate = _candidate()
    attestations = _attestations(per_member_overrides={2: {"outcome_set_exhaustive": False}})
    rule_versions = _rule_versions()

    artifact = compile_proof(
        candidate,
        rule_versions,
        attestations,
        as_of=NOW,
        review_identity=REVIEW_IDENTITY,
    )

    assert artifact.status == "rejected"
    assert artifact.rejection_reason == "OUTCOME_SET_NOT_EXHAUSTIVE"
    assert artifact.terminal_states == ()
    assert artifact.minimum_basket_payout is None


def test_one_member_changed_rule_version_rejects_whole_group() -> None:
    candidate = _candidate()
    attestations = _attestations()
    rule_versions = _rule_versions(exclude={1})

    artifact = compile_proof(
        candidate,
        rule_versions,
        attestations,
        as_of=NOW,
        review_identity=REVIEW_IDENTITY,
    )

    assert artifact.status == "rejected"
    assert artifact.rejection_reason == "RULE_VERSION_CHANGED"


def test_one_member_tie_possible_rejects_whole_group() -> None:
    candidate = _candidate()
    attestations = _attestations(
        per_member_overrides={1: {"tie_possible": True, "tie_behavior": "split evenly"}}
    )
    rule_versions = _rule_versions()

    artifact = compile_proof(
        candidate,
        rule_versions,
        attestations,
        as_of=NOW,
        review_identity=REVIEW_IDENTITY,
    )

    assert artifact.status == "rejected"
    assert artifact.rejection_reason == "TIE_UNMODELED"


def test_one_member_void_behavior_unknown_rejects_whole_group() -> None:
    candidate = _candidate()
    attestations = _attestations(
        per_member_overrides={0: {"void_or_invalid_possible": True, "void_behavior": "unknown"}}
    )
    rule_versions = _rule_versions()

    artifact = compile_proof(
        candidate,
        rule_versions,
        attestations,
        as_of=NOW,
        review_identity=REVIEW_IDENTITY,
    )

    assert artifact.status == "rejected"
    assert artifact.rejection_reason == "VOID_BEHAVIOR_UNKNOWN"


def test_void_refund_at_cost_adds_one_excluded_state_per_void_member() -> None:
    candidate = _candidate()
    attestations = _attestations(
        per_member_overrides={
            0: {"void_or_invalid_possible": True, "void_behavior": "refund_at_cost"},
            2: {"void_or_invalid_possible": True, "void_behavior": "refund_at_cost"},
        }
    )
    rule_versions = _rule_versions()

    artifact = compile_proof(
        candidate,
        rule_versions,
        attestations,
        as_of=NOW,
        review_identity=REVIEW_IDENTITY,
    )

    assert artifact.status == "proof_ready"
    assert {state.state_id for state in artifact.terminal_states} == {
        "member_0_wins",
        "member_1_wins",
        "member_2_wins",
    }
    assert len(artifact.excluded_states) == 2
    excluded_attestation_ids = {e.attestation_id for e in artifact.excluded_states}
    assert excluded_attestation_ids == {
        _MEMBER_ATTESTATION_IDS[0],
        _MEMBER_ATTESTATION_IDS[2],
    }


def test_void_resolve_to_rules_price_adds_one_combined_void_terminal_state() -> None:
    candidate = _candidate()
    attestations = _attestations(
        per_member_overrides={
            0: {
                "void_or_invalid_possible": True,
                "void_behavior": "resolve_to_rules_price",
                "winner_payout_per_share": Decimal("1"),
                "loser_payout_per_share": Decimal("0.1"),
            },
            1: {
                "winner_payout_per_share": Decimal("1"),
                "loser_payout_per_share": Decimal("0.2"),
            },
            2: {
                "winner_payout_per_share": Decimal("1"),
                "loser_payout_per_share": Decimal("0.3"),
            },
        }
    )
    rule_versions = _rule_versions()

    artifact = compile_proof(
        candidate,
        rule_versions,
        attestations,
        as_of=NOW,
        review_identity=REVIEW_IDENTITY,
    )

    assert artifact.status == "proof_ready"
    assert {state.state_id for state in artifact.terminal_states} == {
        "member_0_wins",
        "member_1_wins",
        "member_2_wins",
        "void",
    }
    void_state = next(s for s in artifact.terminal_states if s.state_id == "void")
    # Every leg -- not only the void-flagged member -- pays its own attestation's
    # loser_payout_per_share in the single combined void state.
    assert void_state.leg_payouts == (Decimal("0.1"), Decimal("0.2"), Decimal("0.3"))
    assert artifact.excluded_states == ()


def test_mixed_void_behaviors_across_members_is_rejected() -> None:
    candidate = _candidate()
    attestations = _attestations(
        per_member_overrides={
            0: {"void_or_invalid_possible": True, "void_behavior": "refund_at_cost"},
            1: {"void_or_invalid_possible": True, "void_behavior": "resolve_to_rules_price"},
        }
    )
    rule_versions = _rule_versions()

    artifact = compile_proof(
        candidate,
        rule_versions,
        attestations,
        as_of=NOW,
        review_identity=REVIEW_IDENTITY,
    )

    assert artifact.status == "rejected"
    assert artifact.rejection_reason == "VOID_BEHAVIOR_UNKNOWN"


def test_compile_proof_is_deterministic_and_idempotent() -> None:
    candidate = _candidate()
    attestations = _attestations()
    rule_versions = _rule_versions()

    first = compile_proof(
        candidate,
        rule_versions,
        attestations,
        as_of=NOW,
        review_identity=REVIEW_IDENTITY,
    )
    second = compile_proof(
        candidate,
        rule_versions,
        attestations,
        as_of=NOW,
        review_identity=REVIEW_IDENTITY,
    )

    assert first == second
    assert first.proof_id == second.proof_id


def test_duplicate_market_id_across_legs_is_a_structural_error() -> None:
    # A malformed candidate: two legs share the same market_id, which can never happen
    # from the increment-2 generator (it emits one leg per distinct member market) --
    # treating them as two different members would double-count or silently merge state.
    candidate = _candidate(
        legs=(
            leg(market_id=_MEMBER_MARKET_IDS[0], outcome_index=None, outcome_token_id=None),
            leg(
                market_id=_MEMBER_MARKET_IDS[0],
                outcome_index=None,
                outcome_token_id=None,
                rule_version_id=_MEMBER_RULE_VERSION_IDS[1],
            ),
            leg(market_id=_MEMBER_MARKET_IDS[2], outcome_index=None, outcome_token_id=None),
        )
    )
    attestations = _attestations()
    rule_versions = _rule_versions()

    with pytest.raises(ValueError, match="distinct member market"):
        compile_proof(
            candidate,
            rule_versions,
            attestations,
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
        {},  # rule versions also absent from the current registry state
        {},  # no attestations at all
        as_of=NOW,
        review_identity=REVIEW_IDENTITY,
    )

    assert artifact.status == "insufficient_evidence"
    assert artifact.rejection_reason == "MISSING_ATTESTATION"


def test_exhaustive_check_wins_over_rule_version_change() -> None:
    # Locks in the documented check order: OUTCOME_SET_NOT_EXHAUSTIVE is checked before
    # RULE_VERSION_CHANGED.
    candidate = _candidate()
    attestations = _attestations(per_member_overrides={0: {"outcome_set_exhaustive": False}})

    artifact = compile_proof(
        candidate,
        {},  # every rule version absent from the current registry state too
        attestations,
        as_of=NOW,
        review_identity=REVIEW_IDENTITY,
    )

    assert artifact.status == "rejected"
    assert artifact.rejection_reason == "OUTCOME_SET_NOT_EXHAUSTIVE"
