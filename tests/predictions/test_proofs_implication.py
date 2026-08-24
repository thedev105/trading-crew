from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from polytrading.predictions.candidates_models import RelationshipType
from polytrading.predictions.domain import PredictionVenue
from polytrading.predictions.proofs import compile_proof
from polytrading.predictions.propositions import PropositionSpan, TypedProposition
from tests.predictions.attestation_helpers import rule_attestation
from tests.predictions.candidate_helpers import HASH, candidate_relationship, leg
from tests.predictions.domain_helpers import rule_version

NOW = datetime(2026, 8, 15, 12, tzinfo=UTC)
REVIEW_IDENTITY = "reviewer@example.test"

MARKET_A = "0xmarket-a"
MARKET_B = "0xmarket-b"
RULE_VERSION_ID_A = UUID("00000000-0000-0000-0000-000000008001")
RULE_VERSION_ID_B = UUID("00000000-0000-0000-0000-000000008002")
ATTESTATION_ID_A = UUID("00000000-0000-0000-0000-000000008101")
ATTESTATION_ID_B = UUID("00000000-0000-0000-0000-000000008102")


def _span(**overrides) -> PropositionSpan:
    values = {
        "start_char": 0,
        "end_char": 10,
        "exact_text": "at least $100,000",
        "rule_source_hash": HASH,
    }
    values.update(overrides)
    return PropositionSpan(**values)


def _proposition(**overrides) -> TypedProposition:
    values = {
        "schema_version": 1,
        "kind": "threshold",
        "subject": "BTC price",
        "predicate": ">=",
        "value": "110000",
        "status": "extracted",
        "supporting_spans": (_span(),),
    }
    values.update(overrides)
    return TypedProposition(**values)


def _legs(**overrides):
    leg_a = leg(
        market_id=MARKET_A,
        outcome_index=1,  # NO side
        outcome_token_id="a-no",
        rule_version_id=RULE_VERSION_ID_A,
        rule_source_hash=HASH,
    )
    leg_b = leg(
        market_id=MARKET_B,
        outcome_index=0,  # YES side
        outcome_token_id="b-yes",
        rule_version_id=RULE_VERSION_ID_B,
        rule_source_hash=HASH,
    )
    return (leg_a, leg_b)


def _candidate(propositions=None, **overrides):
    if propositions is None:
        propositions = (
            _proposition(),
            _proposition(
                value="100000", supporting_spans=(_span(exact_text="at least $100,000 b"),)
            ),
        )
    values = {
        "relationship_type": RelationshipType.LOGICAL_IMPLICATION,
        "legs": _legs(),
        "propositions": propositions,
    }
    values.update(overrides)
    return candidate_relationship(**values)


def _attestation_a(**overrides):
    values = {
        "attestation_id": ATTESTATION_ID_A,
        "rule_version_id": RULE_VERSION_ID_A,
        "market_id": MARKET_A,
        "venue": PredictionVenue.POLYMARKET,
        "rule_source_hash": HASH,
        "winner_payout_per_share": Decimal("1"),
        "loser_payout_per_share": Decimal("0"),
        "resolution_source_attested": "https://example.test/rules",
        "deadline_utc": None,
        "threshold_text": "at least $100,000",
        "threshold_inclusive": True,
    }
    values.update(overrides)
    return rule_attestation(**values)


def _attestation_b(**overrides):
    values = {
        "attestation_id": ATTESTATION_ID_B,
        "rule_version_id": RULE_VERSION_ID_B,
        "market_id": MARKET_B,
        "venue": PredictionVenue.POLYMARKET,
        "rule_source_hash": HASH,
        "winner_payout_per_share": Decimal("1"),
        "loser_payout_per_share": Decimal("0"),
        "resolution_source_attested": "https://example.test/rules",
        "deadline_utc": None,
        "threshold_text": "at least $100,000",
        "threshold_inclusive": True,
    }
    values.update(overrides)
    return rule_attestation(**values)


def _attestations(overrides: dict | None = None):
    result = {RULE_VERSION_ID_A: _attestation_a(), RULE_VERSION_ID_B: _attestation_b()}
    if overrides:
        result.update(overrides)
    return result


def _rule_versions(exclude: set[str] | None = None):
    exclude = exclude or set()
    result = {}
    if "a" not in exclude:
        result[RULE_VERSION_ID_A] = rule_version(
            rule_version_id=RULE_VERSION_ID_A,
            market_id=MARKET_A,
            venue=PredictionVenue.POLYMARKET,
            source_hash=HASH,
        )
    if "b" not in exclude:
        result[RULE_VERSION_ID_B] = rule_version(
            rule_version_id=RULE_VERSION_ID_B,
            market_id=MARKET_B,
            venue=PredictionVenue.POLYMARKET,
            source_hash=HASH,
        )
    return result


def test_nested_threshold_happy_path_emits_proof_ready_with_expected_states() -> None:
    # A: BTC price >= 110000 (leg 0, NO side); B: BTC price >= 100000 (leg 1, YES side).
    # A implies B (110000 >= 100000), so a_without_b is excluded.
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
    assert artifact.template == "logical_implication@1"
    assert artifact.compiler_version == "1"
    assert {state.state_id for state in artifact.terminal_states} == {
        "neither",
        "b_only",
        "both",
    }
    for state in artifact.terminal_states:
        assert len(state.leg_payouts) == 2

    winner_a = attestations[RULE_VERSION_ID_A].winner_payout_per_share
    loser_a = attestations[RULE_VERSION_ID_A].loser_payout_per_share
    winner_b = attestations[RULE_VERSION_ID_B].winner_payout_per_share
    loser_b = attestations[RULE_VERSION_ID_B].loser_payout_per_share

    neither = next(s for s in artifact.terminal_states if s.state_id == "neither")
    b_only = next(s for s in artifact.terminal_states if s.state_id == "b_only")
    both = next(s for s in artifact.terminal_states if s.state_id == "both")
    assert neither.leg_payouts == (winner_a, loser_b)
    assert b_only.leg_payouts == (winner_a, winner_b)
    assert both.leg_payouts == (loser_a, winner_b)

    # Hand-computed: winner=1, loser=0 for both attestations.
    # neither = 1+0=1, b_only = 1+1=2, both = 0+1=1
    assert artifact.minimum_basket_payout == Decimal("1")
    assert artifact.maximum_basket_payout == Decimal("2")

    assert len(artifact.excluded_states) == 1
    excluded = artifact.excluded_states[0]
    assert excluded.attestation_id in (ATTESTATION_ID_A, ATTESTATION_ID_B)

    assert len(artifact.assumptions) == 1
    assumption = artifact.assumptions[0]
    assert len(assumption.supporting_spans) == 2

    assert artifact.rule_version_ids == (RULE_VERSION_ID_A, RULE_VERSION_ID_B)
    assert artifact.source_hashes == (HASH,)
    assert artifact.equivalence_matrix is None
    assert artifact.candidate_id == candidate.candidate_id
    assert artifact.review_identity == REVIEW_IDENTITY
    assert "any participating rule_version change" in artifact.invalidation_conditions


def test_strict_a_implies_inclusive_b_at_same_value_is_valid() -> None:
    # A: price > 100000 (exclusive); B: price >= 100000 (inclusive). Same value: valid.
    candidate = _candidate(
        propositions=(
            _proposition(predicate=">", value="100000"),
            _proposition(predicate=">=", value="100000"),
        )
    )
    attestations = _attestations(
        {
            RULE_VERSION_ID_A: _attestation_a(threshold_inclusive=False),
            RULE_VERSION_ID_B: _attestation_b(threshold_inclusive=True),
        }
    )
    rule_versions = _rule_versions()

    artifact = compile_proof(
        candidate, rule_versions, attestations, as_of=NOW, review_identity=REVIEW_IDENTITY
    )

    assert artifact.status == "proof_ready"


def test_inclusive_a_does_not_imply_strict_b_at_same_value() -> None:
    # A: price >= 100000 (inclusive); B: price > 100000 (exclusive). Same value: invalid,
    # since price==100000 satisfies A but not B.
    candidate = _candidate(
        propositions=(
            _proposition(predicate=">=", value="100000"),
            _proposition(predicate=">", value="100000"),
        )
    )
    attestations = _attestations(
        {
            RULE_VERSION_ID_A: _attestation_a(threshold_inclusive=True),
            RULE_VERSION_ID_B: _attestation_b(threshold_inclusive=False),
        }
    )
    rule_versions = _rule_versions()

    artifact = compile_proof(
        candidate, rule_versions, attestations, as_of=NOW, review_identity=REVIEW_IDENTITY
    )

    assert artifact.status == "rejected"
    assert artifact.rejection_reason == "IMPLICATION_INVALID"


def test_a_bound_looser_than_b_bound_rejects() -> None:
    # A: price >= 90000 (looser); B: price >= 100000 (stricter). A does not imply B.
    candidate = _candidate(
        propositions=(
            _proposition(predicate=">=", value="90000"),
            _proposition(predicate=">=", value="100000"),
        )
    )
    attestations = _attestations()
    rule_versions = _rule_versions()

    artifact = compile_proof(
        candidate, rule_versions, attestations, as_of=NOW, review_identity=REVIEW_IDENTITY
    )

    assert artifact.status == "rejected"
    assert artifact.rejection_reason == "IMPLICATION_INVALID"


def test_mismatched_direction_rejects() -> None:
    # A: price >= 110000 (upward); B: price <= 100000 (downward). Not comparable.
    candidate = _candidate(
        propositions=(
            _proposition(predicate=">=", value="110000"),
            _proposition(predicate="<=", value="100000"),
        )
    )
    attestations = _attestations()
    rule_versions = _rule_versions()

    artifact = compile_proof(
        candidate, rule_versions, attestations, as_of=NOW, review_identity=REVIEW_IDENTITY
    )

    assert artifact.status == "rejected"
    assert artifact.rejection_reason == "IMPLICATION_INVALID"


def test_threshold_inclusive_none_rejects_as_implication_invalid() -> None:
    candidate = _candidate()
    attestations = _attestations({RULE_VERSION_ID_A: _attestation_a(threshold_inclusive=None)})
    rule_versions = _rule_versions()

    artifact = compile_proof(
        candidate, rule_versions, attestations, as_of=NOW, review_identity=REVIEW_IDENTITY
    )

    assert artifact.status == "rejected"
    assert artifact.rejection_reason == "IMPLICATION_INVALID"


def test_subject_mismatch_rejects() -> None:
    candidate = _candidate(
        propositions=(
            _proposition(subject="BTC price"),
            _proposition(subject="ETH price", value="100000"),
        )
    )
    attestations = _attestations()
    rule_versions = _rule_versions()

    artifact = compile_proof(
        candidate, rule_versions, attestations, as_of=NOW, review_identity=REVIEW_IDENTITY
    )

    assert artifact.status == "rejected"
    assert artifact.rejection_reason == "IMPLICATION_INVALID"


def test_resolution_source_mismatch_rejects() -> None:
    candidate = _candidate()
    attestations = _attestations(
        {RULE_VERSION_ID_B: _attestation_b(resolution_source_attested="https://other.test/rules")}
    )
    rule_versions = _rule_versions()

    artifact = compile_proof(
        candidate, rule_versions, attestations, as_of=NOW, review_identity=REVIEW_IDENTITY
    )

    assert artifact.status == "rejected"
    assert artifact.rejection_reason == "IMPLICATION_INVALID"


def test_threshold_deadline_mismatch_rejects() -> None:
    candidate = _candidate()
    attestations = _attestations(
        {RULE_VERSION_ID_B: _attestation_b(deadline_utc=NOW + timedelta(days=1))}
    )
    rule_versions = _rule_versions()

    artifact = compile_proof(
        candidate, rule_versions, attestations, as_of=NOW, review_identity=REVIEW_IDENTITY
    )

    assert artifact.status == "rejected"
    assert artifact.rejection_reason == "IMPLICATION_INVALID"


def test_kind_mismatch_rejects() -> None:
    candidate = _candidate(
        propositions=(
            _proposition(kind="threshold"),
            _proposition(
                kind="deadline",
                predicate="resolves_by",
                value="2026-12-31T00:00:00Z",
            ),
        )
    )
    attestations = _attestations()
    rule_versions = _rule_versions()

    artifact = compile_proof(
        candidate, rule_versions, attestations, as_of=NOW, review_identity=REVIEW_IDENTITY
    )

    assert artifact.status == "rejected"
    assert artifact.rejection_reason == "IMPLICATION_INVALID"


def test_unrecognized_kind_is_insufficient_evidence() -> None:
    candidate = _candidate(
        propositions=(
            _proposition(kind="scope", predicate="applies_to", value="foo"),
            _proposition(),
        )
    )
    attestations = _attestations()
    rule_versions = _rule_versions()

    artifact = compile_proof(
        candidate, rule_versions, attestations, as_of=NOW, review_identity=REVIEW_IDENTITY
    )

    assert artifact.status == "insufficient_evidence"
    assert artifact.rejection_reason == "PROPOSITIONS_NOT_EXTRACTED"


def test_unextracted_proposition_status_is_insufficient_evidence() -> None:
    candidate = _candidate(
        propositions=(
            _proposition(status="unknown", value=None, supporting_spans=()),
            _proposition(),
        )
    )
    attestations = _attestations()
    rule_versions = _rule_versions()

    artifact = compile_proof(
        candidate, rule_versions, attestations, as_of=NOW, review_identity=REVIEW_IDENTITY
    )

    assert artifact.status == "insufficient_evidence"
    assert artifact.rejection_reason == "PROPOSITIONS_NOT_EXTRACTED"


def test_unparsable_threshold_value_is_insufficient_evidence() -> None:
    candidate = _candidate(
        propositions=(
            _proposition(value="not-a-number"),
            _proposition(value="100000"),
        )
    )
    attestations = _attestations()
    rule_versions = _rule_versions()

    artifact = compile_proof(
        candidate, rule_versions, attestations, as_of=NOW, review_identity=REVIEW_IDENTITY
    )

    assert artifact.status == "insufficient_evidence"
    assert artifact.rejection_reason == "PROPOSITIONS_NOT_EXTRACTED"


def test_missing_attestation_yields_insufficient_evidence() -> None:
    candidate = _candidate()
    attestations = _attestations()
    del attestations[RULE_VERSION_ID_B]
    rule_versions = _rule_versions()

    artifact = compile_proof(
        candidate, rule_versions, attestations, as_of=NOW, review_identity=REVIEW_IDENTITY
    )

    assert artifact.status == "insufficient_evidence"
    assert artifact.rejection_reason == "MISSING_ATTESTATION"
    assert artifact.terminal_states == ()
    assert artifact.minimum_basket_payout is None


def test_changed_rule_version_is_rejected() -> None:
    candidate = _candidate()
    attestations = _attestations()
    rule_versions = _rule_versions(exclude={"b"})

    artifact = compile_proof(
        candidate, rule_versions, attestations, as_of=NOW, review_identity=REVIEW_IDENTITY
    )

    assert artifact.status == "rejected"
    assert artifact.rejection_reason == "RULE_VERSION_CHANGED"


def test_tie_possible_on_either_leg_rejects_as_unmodeled() -> None:
    candidate = _candidate()
    attestations = _attestations(
        {RULE_VERSION_ID_A: _attestation_a(tie_possible=True, tie_behavior="split evenly")}
    )
    rule_versions = _rule_versions()

    artifact = compile_proof(
        candidate, rule_versions, attestations, as_of=NOW, review_identity=REVIEW_IDENTITY
    )

    assert artifact.status == "rejected"
    assert artifact.rejection_reason == "TIE_UNMODELED"


def test_void_behavior_unknown_on_either_leg_rejects() -> None:
    candidate = _candidate()
    attestations = _attestations(
        {RULE_VERSION_ID_A: _attestation_a(void_or_invalid_possible=True, void_behavior="unknown")}
    )
    rule_versions = _rule_versions()

    artifact = compile_proof(
        candidate, rule_versions, attestations, as_of=NOW, review_identity=REVIEW_IDENTITY
    )

    assert artifact.status == "rejected"
    assert artifact.rejection_reason == "VOID_BEHAVIOR_UNKNOWN"


def test_mixed_void_behaviors_across_legs_rejects() -> None:
    candidate = _candidate()
    attestations = _attestations(
        {
            RULE_VERSION_ID_A: _attestation_a(
                void_or_invalid_possible=True, void_behavior="refund_at_cost"
            ),
            RULE_VERSION_ID_B: _attestation_b(
                void_or_invalid_possible=True, void_behavior="resolve_to_rules_price"
            ),
        }
    )
    rule_versions = _rule_versions()

    artifact = compile_proof(
        candidate, rule_versions, attestations, as_of=NOW, review_identity=REVIEW_IDENTITY
    )

    assert artifact.status == "rejected"
    assert artifact.rejection_reason == "VOID_BEHAVIOR_UNKNOWN"


def test_void_refund_at_cost_adds_excluded_state_per_leg() -> None:
    candidate = _candidate()
    attestations = _attestations(
        {
            RULE_VERSION_ID_A: _attestation_a(
                void_or_invalid_possible=True, void_behavior="refund_at_cost"
            )
        }
    )
    rule_versions = _rule_versions()

    artifact = compile_proof(
        candidate, rule_versions, attestations, as_of=NOW, review_identity=REVIEW_IDENTITY
    )

    assert artifact.status == "proof_ready"
    assert {state.state_id for state in artifact.terminal_states} == {
        "neither",
        "b_only",
        "both",
    }
    # 1 implication-exclusion state + 1 void-refund state.
    assert len(artifact.excluded_states) == 2
    void_excluded = [e for e in artifact.excluded_states if "void" in e.description]
    assert len(void_excluded) == 1
    assert void_excluded[0].attestation_id == ATTESTATION_ID_A


def test_void_resolve_to_rules_price_adds_combined_void_terminal_state() -> None:
    candidate = _candidate()
    attestations = _attestations(
        {
            RULE_VERSION_ID_A: _attestation_a(
                void_or_invalid_possible=True,
                void_behavior="resolve_to_rules_price",
                winner_payout_per_share=Decimal("1"),
                loser_payout_per_share=Decimal("0.1"),
            ),
            RULE_VERSION_ID_B: _attestation_b(
                winner_payout_per_share=Decimal("1"),
                loser_payout_per_share=Decimal("0.2"),
            ),
        }
    )
    rule_versions = _rule_versions()

    artifact = compile_proof(
        candidate, rule_versions, attestations, as_of=NOW, review_identity=REVIEW_IDENTITY
    )

    assert artifact.status == "proof_ready"
    assert {state.state_id for state in artifact.terminal_states} == {
        "neither",
        "b_only",
        "both",
        "void",
    }
    void_state = next(s for s in artifact.terminal_states if s.state_id == "void")
    assert void_state.leg_payouts == (Decimal("0.1"), Decimal("0.2"))
    assert len(artifact.excluded_states) == 1  # only the implication exclusion


def test_compile_proof_is_deterministic_and_idempotent() -> None:
    candidate = _candidate()
    attestations = _attestations()
    rule_versions = _rule_versions()

    first = compile_proof(
        candidate, rule_versions, attestations, as_of=NOW, review_identity=REVIEW_IDENTITY
    )
    second = compile_proof(
        candidate, rule_versions, attestations, as_of=NOW, review_identity=REVIEW_IDENTITY
    )

    assert first == second
    assert first.proof_id == second.proof_id


def test_wrong_leg_count_is_a_structural_error() -> None:
    candidate = _candidate(
        legs=(*_legs(), leg(market_id="0xmarket-c", rule_version_id=RULE_VERSION_ID_A)),
        propositions=(
            _proposition(),
            _proposition(value="100000"),
            _proposition(value="100000"),
        ),
    )
    attestations = _attestations()
    rule_versions = _rule_versions()

    with pytest.raises(ValueError, match="exactly 2 legs"):
        compile_proof(
            candidate, rule_versions, attestations, as_of=NOW, review_identity=REVIEW_IDENTITY
        )


def test_same_market_id_across_legs_is_a_structural_error() -> None:
    candidate = _candidate(
        legs=(
            leg(market_id=MARKET_A, rule_version_id=RULE_VERSION_ID_A),
            leg(market_id=MARKET_A, rule_version_id=RULE_VERSION_ID_B),
        )
    )
    attestations = _attestations()
    rule_versions = _rule_versions()

    with pytest.raises(ValueError, match="distinct markets"):
        compile_proof(
            candidate, rule_versions, attestations, as_of=NOW, review_identity=REVIEW_IDENTITY
        )


def test_propositions_not_one_per_leg_is_a_structural_error() -> None:
    candidate = _candidate(propositions=(_proposition(),))
    attestations = _attestations()
    rule_versions = _rule_versions()

    with pytest.raises(ValueError, match="one TypedProposition per leg"):
        compile_proof(
            candidate, rule_versions, attestations, as_of=NOW, review_identity=REVIEW_IDENTITY
        )
