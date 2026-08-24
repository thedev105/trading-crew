from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest

from polytrading.predictions.attestations import RuleAttestation
from polytrading.predictions.candidates_models import RelationshipType
from polytrading.predictions.domain import PredictionVenue
from polytrading.predictions.proofs import _equivalence_terminal_states, compile_proof
from tests.predictions.attestation_helpers import rule_attestation
from tests.predictions.candidate_helpers import HASH, candidate_relationship, leg
from tests.predictions.domain_helpers import rule_version

NOW = datetime(2026, 8, 15, 12, tzinfo=UTC)
REVIEW_IDENTITY = "reviewer@example.test"

MARKET_A = "poly-market-a"
MARKET_B = "kalshi-market-b"
RULE_VERSION_ID_A = UUID("00000000-0000-0000-0000-000000009001")
RULE_VERSION_ID_B = UUID("00000000-0000-0000-0000-000000009002")
ATTESTATION_ID_A = UUID("00000000-0000-0000-0000-000000009101")
ATTESTATION_ID_B = UUID("00000000-0000-0000-0000-000000009102")

FIXTURE_PATH = (
    Path(__file__).resolve().parent.parent / "fixtures" / "predictions" / "hard_negatives.json"
)


def _legs(
    market_a: str = MARKET_A,
    market_b: str = MARKET_B,
    venue_a: PredictionVenue = PredictionVenue.POLYMARKET,
    venue_b: PredictionVenue = PredictionVenue.KALSHI,
    rule_version_id_a: UUID = RULE_VERSION_ID_A,
    rule_version_id_b: UUID = RULE_VERSION_ID_B,
):
    leg_a = leg(
        venue=venue_a,
        market_id=market_a,
        outcome_index=None,
        outcome_token_id=None,
        rule_version_id=rule_version_id_a,
        rule_source_hash=HASH,
    )
    leg_b = leg(
        venue=venue_b,
        market_id=market_b,
        outcome_index=None,
        outcome_token_id=None,
        rule_version_id=rule_version_id_b,
        rule_source_hash=HASH,
    )
    return (leg_a, leg_b)


def _candidate(legs=None, **overrides):
    values: dict[str, Any] = {
        "relationship_type": RelationshipType.CROSS_VENUE_EQUIVALENCE,
        "legs": legs if legs is not None else _legs(),
        "propositions": (),
    }
    values.update(overrides)
    return candidate_relationship(**values)


def _attestation_a(**overrides: Any) -> RuleAttestation:
    values: dict[str, Any] = {
        "attestation_id": ATTESTATION_ID_A,
        "rule_version_id": RULE_VERSION_ID_A,
        "market_id": MARKET_A,
        "venue": PredictionVenue.POLYMARKET,
        "rule_source_hash": HASH,
        "payout_unit": "usdc_1_per_share",
        "winner_payout_per_share": Decimal("1"),
        "loser_payout_per_share": Decimal("0"),
        "outcome_set_exhaustive": True,
        "void_or_invalid_possible": False,
        "void_behavior": "unknown",
        "tie_possible": False,
        "tie_behavior": None,
        "resolution_source_attested": "https://example.test/rules",
        "deadline_utc": None,
        "threshold_text": None,
        "threshold_inclusive": None,
    }
    values.update(overrides)
    return rule_attestation(**values)


def _attestation_b(**overrides: Any) -> RuleAttestation:
    values: dict[str, Any] = {
        "attestation_id": ATTESTATION_ID_B,
        "rule_version_id": RULE_VERSION_ID_B,
        "market_id": MARKET_B,
        "venue": PredictionVenue.KALSHI,
        "rule_source_hash": HASH,
        "payout_unit": "usdc_1_per_share",
        "winner_payout_per_share": Decimal("1"),
        "loser_payout_per_share": Decimal("0"),
        "outcome_set_exhaustive": True,
        "void_or_invalid_possible": False,
        "void_behavior": "unknown",
        "tie_possible": False,
        "tie_behavior": None,
        "resolution_source_attested": "https://example.test/rules",
        "deadline_utc": None,
        "threshold_text": None,
        "threshold_inclusive": None,
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
            venue=PredictionVenue.KALSHI,
            source_hash=HASH,
        )
    return result


# ---------------------------------------------------------------------------
# Structural guard
# ---------------------------------------------------------------------------


def test_wrong_leg_count_is_a_structural_error() -> None:
    third_leg = leg(
        venue=PredictionVenue.LIMITLESS,
        market_id="limitless-market-c",
        outcome_index=None,
        outcome_token_id=None,
        rule_version_id=RULE_VERSION_ID_A,
        rule_source_hash=HASH,
    )
    candidate = _candidate(legs=(*_legs(), third_leg))
    attestations = _attestations()
    rule_versions = _rule_versions()

    with pytest.raises(ValueError, match="exactly 2 legs"):
        compile_proof(
            candidate, rule_versions, attestations, as_of=NOW, review_identity=REVIEW_IDENTITY
        )


# ---------------------------------------------------------------------------
# proof_ready is unreachable in v1: the "most compatible possible" pair
# ---------------------------------------------------------------------------


def test_most_compatible_pair_still_rejects_as_equivalence_dimension_unknown() -> None:
    """Every attestable fact matches across both legs, yet v1 can never reach
    proof_ready: settlement_finality_timing and venue_access_custody_rules have no
    attested basis and are always "unknown", so the matrix's last two dimensions can
    never be anything else. This is the spec-correct fail-closed outcome (not softened).
    """
    candidate = _candidate()
    attestations = _attestations(
        {
            RULE_VERSION_ID_A: _attestation_a(
                resolution_source_attested="https://shared.test/rules",
                threshold_text="BTC/USD spot price at or above $100,000.00",
                threshold_inclusive=True,
                deadline_utc=NOW,
            ),
            RULE_VERSION_ID_B: _attestation_b(
                resolution_source_attested="https://shared.test/rules",
                threshold_text="BTC/USD spot price at or above $100,000.00",
                threshold_inclusive=True,
                deadline_utc=NOW,
            ),
        }
    )
    rule_versions = _rule_versions()

    artifact = compile_proof(
        candidate, rule_versions, attestations, as_of=NOW, review_identity=REVIEW_IDENTITY
    )

    assert artifact.status == "rejected"
    assert artifact.rejection_reason == "EQUIVALENCE_DIMENSION_UNKNOWN"
    assert artifact.template == "cross_venue_equivalence@1"
    assert artifact.compiler_version == "1"
    assert artifact.terminal_states == ()
    assert artifact.minimum_basket_payout is None
    assert artifact.maximum_basket_payout is None

    matrix = artifact.equivalence_matrix
    assert matrix is not None
    assert matrix.proposition_threshold_inclusivity == "compatible"
    assert matrix.observation_period_timezone == "compatible"
    assert matrix.resolution_sources == "compatible"
    assert matrix.void_dispute_behavior == "compatible"
    assert matrix.outcome_completeness == "compatible"
    assert matrix.denomination_collateral_rounding == "compatible"
    assert matrix.settlement_finality_timing == "unknown"
    assert matrix.venue_access_custody_rules == "unknown"
    assert matrix.basis_attestation_ids == tuple(sorted((ATTESTATION_ID_A, ATTESTATION_ID_B)))


# ---------------------------------------------------------------------------
# Missing attestation / rule version currency
# ---------------------------------------------------------------------------


def test_missing_attestation_on_either_leg_yields_insufficient_evidence() -> None:
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
    matrix = artifact.equivalence_matrix
    assert matrix is not None
    for dimension in (
        matrix.proposition_threshold_inclusivity,
        matrix.observation_period_timezone,
        matrix.resolution_sources,
        matrix.void_dispute_behavior,
        matrix.outcome_completeness,
        matrix.denomination_collateral_rounding,
        matrix.settlement_finality_timing,
        matrix.venue_access_custody_rules,
    ):
        assert dimension == "unknown"
    assert matrix.basis_attestation_ids == (ATTESTATION_ID_A,)


def test_both_attestations_missing_yields_empty_basis_ids() -> None:
    candidate = _candidate()
    rule_versions = _rule_versions()

    artifact = compile_proof(
        candidate, rule_versions, {}, as_of=NOW, review_identity=REVIEW_IDENTITY
    )

    assert artifact.status == "insufficient_evidence"
    assert artifact.rejection_reason == "MISSING_ATTESTATION"
    assert artifact.equivalence_matrix is not None
    assert artifact.equivalence_matrix.basis_attestation_ids == ()


def test_changed_rule_version_is_rejected() -> None:
    candidate = _candidate()
    attestations = _attestations()
    rule_versions = _rule_versions(exclude={"b"})

    artifact = compile_proof(
        candidate, rule_versions, attestations, as_of=NOW, review_identity=REVIEW_IDENTITY
    )

    assert artifact.status == "rejected"
    assert artifact.rejection_reason == "RULE_VERSION_CHANGED"
    matrix = artifact.equivalence_matrix
    assert matrix is not None
    assert matrix.proposition_threshold_inclusivity == "unknown"
    assert matrix.basis_attestation_ids == tuple(sorted((ATTESTATION_ID_A, ATTESTATION_ID_B)))


# ---------------------------------------------------------------------------
# Per-dimension derivation
# ---------------------------------------------------------------------------


def test_threshold_text_none_on_either_leg_is_unknown() -> None:
    candidate = _candidate()
    attestations = _attestations()  # both threshold_text=None by default
    rule_versions = _rule_versions()

    artifact = compile_proof(
        candidate, rule_versions, attestations, as_of=NOW, review_identity=REVIEW_IDENTITY
    )

    assert artifact.status == "rejected"
    assert artifact.equivalence_matrix.proposition_threshold_inclusivity == "unknown"


def test_threshold_inclusivity_mismatch_is_incompatible() -> None:
    candidate = _candidate()
    attestations = _attestations(
        {
            RULE_VERSION_ID_A: _attestation_a(
                threshold_text="at or above $100,000", threshold_inclusive=True
            ),
            RULE_VERSION_ID_B: _attestation_b(
                threshold_text="at or above $100,000", threshold_inclusive=False
            ),
        }
    )
    rule_versions = _rule_versions()

    artifact = compile_proof(
        candidate, rule_versions, attestations, as_of=NOW, review_identity=REVIEW_IDENTITY
    )

    assert artifact.status == "rejected"
    assert artifact.rejection_reason == "EQUIVALENCE_DIMENSION_INCOMPATIBLE"
    assert artifact.equivalence_matrix.proposition_threshold_inclusivity == "incompatible"


def test_threshold_text_mismatch_is_incompatible() -> None:
    candidate = _candidate()
    attestations = _attestations(
        {
            RULE_VERSION_ID_A: _attestation_a(
                threshold_text="at or above $100,000", threshold_inclusive=True
            ),
            RULE_VERSION_ID_B: _attestation_b(
                threshold_text="strictly above $100,000", threshold_inclusive=True
            ),
        }
    )
    rule_versions = _rule_versions()

    artifact = compile_proof(
        candidate, rule_versions, attestations, as_of=NOW, review_identity=REVIEW_IDENTITY
    )

    assert artifact.status == "rejected"
    assert artifact.rejection_reason == "EQUIVALENCE_DIMENSION_INCOMPATIBLE"
    assert artifact.equivalence_matrix.proposition_threshold_inclusivity == "incompatible"


def test_deadline_utc_none_on_either_leg_is_unknown() -> None:
    candidate = _candidate()
    attestations = _attestations()  # both deadline_utc=None by default
    rule_versions = _rule_versions()

    artifact = compile_proof(
        candidate, rule_versions, attestations, as_of=NOW, review_identity=REVIEW_IDENTITY
    )

    assert artifact.status == "rejected"
    assert artifact.equivalence_matrix.observation_period_timezone == "unknown"


def test_deadline_utc_mismatch_is_incompatible() -> None:
    candidate = _candidate()
    attestations = _attestations(
        {
            RULE_VERSION_ID_A: _attestation_a(deadline_utc=NOW),
            RULE_VERSION_ID_B: _attestation_b(deadline_utc=NOW + timedelta(hours=5)),
        }
    )
    rule_versions = _rule_versions()

    artifact = compile_proof(
        candidate, rule_versions, attestations, as_of=NOW, review_identity=REVIEW_IDENTITY
    )

    assert artifact.status == "rejected"
    assert artifact.rejection_reason == "EQUIVALENCE_DIMENSION_INCOMPATIBLE"
    assert artifact.equivalence_matrix.observation_period_timezone == "incompatible"


def test_resolution_source_mismatch_is_incompatible() -> None:
    candidate = _candidate()
    attestations = _attestations(
        {
            RULE_VERSION_ID_A: _attestation_a(resolution_source_attested="AP race call"),
            RULE_VERSION_ID_B: _attestation_b(resolution_source_attested="DDHQ race call"),
        }
    )
    rule_versions = _rule_versions()

    artifact = compile_proof(
        candidate, rule_versions, attestations, as_of=NOW, review_identity=REVIEW_IDENTITY
    )

    assert artifact.status == "rejected"
    assert artifact.rejection_reason == "EQUIVALENCE_DIMENSION_INCOMPATIBLE"
    assert artifact.equivalence_matrix.resolution_sources == "incompatible"


def test_void_behavior_needed_but_unknown_on_either_leg_is_unknown() -> None:
    candidate = _candidate()
    attestations = _attestations(
        {
            RULE_VERSION_ID_A: _attestation_a(
                void_or_invalid_possible=True, void_behavior="unknown"
            ),
            RULE_VERSION_ID_B: _attestation_b(
                void_or_invalid_possible=True, void_behavior="unknown"
            ),
        }
    )
    rule_versions = _rule_versions()

    artifact = compile_proof(
        candidate, rule_versions, attestations, as_of=NOW, review_identity=REVIEW_IDENTITY
    )

    assert artifact.status == "rejected"
    assert artifact.equivalence_matrix.void_dispute_behavior == "unknown"


def test_void_dispute_behavior_mismatch_is_incompatible() -> None:
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
    assert artifact.rejection_reason == "EQUIVALENCE_DIMENSION_INCOMPATIBLE"
    assert artifact.equivalence_matrix.void_dispute_behavior == "incompatible"


def test_void_dispute_behavior_matching_refund_at_cost_is_compatible() -> None:
    candidate = _candidate()
    attestations = _attestations(
        {
            RULE_VERSION_ID_A: _attestation_a(
                void_or_invalid_possible=True, void_behavior="refund_at_cost"
            ),
            RULE_VERSION_ID_B: _attestation_b(
                void_or_invalid_possible=True, void_behavior="refund_at_cost"
            ),
        }
    )
    rule_versions = _rule_versions()

    artifact = compile_proof(
        candidate, rule_versions, attestations, as_of=NOW, review_identity=REVIEW_IDENTITY
    )

    assert artifact.equivalence_matrix.void_dispute_behavior == "compatible"


def test_outcome_completeness_either_false_is_incompatible() -> None:
    candidate = _candidate()
    attestations = _attestations({RULE_VERSION_ID_A: _attestation_a(outcome_set_exhaustive=False)})
    rule_versions = _rule_versions()

    artifact = compile_proof(
        candidate, rule_versions, attestations, as_of=NOW, review_identity=REVIEW_IDENTITY
    )

    assert artifact.status == "rejected"
    assert artifact.rejection_reason == "EQUIVALENCE_DIMENSION_INCOMPATIBLE"
    assert artifact.equivalence_matrix.outcome_completeness == "incompatible"


def test_denomination_payout_unit_mismatch_is_incompatible() -> None:
    candidate = _candidate()
    attestations = _attestations(
        {RULE_VERSION_ID_B: _attestation_b(payout_unit="usd_1_per_contract")}
    )
    rule_versions = _rule_versions()

    artifact = compile_proof(
        candidate, rule_versions, attestations, as_of=NOW, review_identity=REVIEW_IDENTITY
    )

    assert artifact.status == "rejected"
    assert artifact.rejection_reason == "EQUIVALENCE_DIMENSION_INCOMPATIBLE"
    assert artifact.equivalence_matrix.denomination_collateral_rounding == "incompatible"


def test_denomination_payout_value_mismatch_is_incompatible() -> None:
    candidate = _candidate()
    attestations = _attestations(
        {RULE_VERSION_ID_B: _attestation_b(loser_payout_per_share=Decimal("0.1"))}
    )
    rule_versions = _rule_versions()

    artifact = compile_proof(
        candidate, rule_versions, attestations, as_of=NOW, review_identity=REVIEW_IDENTITY
    )

    assert artifact.status == "rejected"
    assert artifact.rejection_reason == "EQUIVALENCE_DIMENSION_INCOMPATIBLE"
    assert artifact.equivalence_matrix.denomination_collateral_rounding == "incompatible"


def test_settlement_finality_timing_is_always_unknown_in_v1() -> None:
    candidate = _candidate()
    attestations = _attestations()
    rule_versions = _rule_versions()

    artifact = compile_proof(
        candidate, rule_versions, attestations, as_of=NOW, review_identity=REVIEW_IDENTITY
    )

    assert artifact.equivalence_matrix.settlement_finality_timing == "unknown"


def test_venue_access_custody_rules_is_always_unknown_in_v1() -> None:
    candidate = _candidate()
    attestations = _attestations()
    rule_versions = _rule_versions()

    artifact = compile_proof(
        candidate, rule_versions, attestations, as_of=NOW, review_identity=REVIEW_IDENTITY
    )

    assert artifact.equivalence_matrix.venue_access_custody_rules == "unknown"


def test_incompatible_wins_over_unknown_in_aggregate_rejection_reason() -> None:
    """threshold_text is None on both legs (unknown), AND resolution_source_attested
    diverges (incompatible). Both are present in the matrix simultaneously; the
    RULING is that incompatible wins the aggregate rejection reason.
    """
    candidate = _candidate()
    attestations = _attestations(
        {
            RULE_VERSION_ID_A: _attestation_a(resolution_source_attested="AP race call"),
            RULE_VERSION_ID_B: _attestation_b(resolution_source_attested="DDHQ race call"),
        }
    )
    rule_versions = _rule_versions()

    artifact = compile_proof(
        candidate, rule_versions, attestations, as_of=NOW, review_identity=REVIEW_IDENTITY
    )

    assert artifact.equivalence_matrix.proposition_threshold_inclusivity == "unknown"
    assert artifact.equivalence_matrix.resolution_sources == "incompatible"
    assert artifact.rejection_reason == "EQUIVALENCE_DIMENSION_INCOMPATIBLE"


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


# ---------------------------------------------------------------------------
# _equivalence_terminal_states: the proof_ready payoff-table helper. compile_proof
# can never actually reach this branch in v1 (settlement_finality_timing and
# venue_access_custody_rules are always "unknown"), so this pure helper is unit
# tested directly to give the payoff-table shape coverage ahead of any future
# increment that makes proof_ready reachable.
# ---------------------------------------------------------------------------


def test_equivalence_terminal_states_hand_computed_two_state_table() -> None:
    attestation_a = _attestation_a(
        winner_payout_per_share=Decimal("1"), loser_payout_per_share=Decimal("0.2")
    )
    attestation_b = _attestation_b(
        winner_payout_per_share=Decimal("1"), loser_payout_per_share=Decimal("0.3")
    )

    states = _equivalence_terminal_states(attestation_a, attestation_b, MARKET_A, MARKET_B)

    assert [state.state_id for state in states] == ["proposition_true", "proposition_false"]
    proposition_true, proposition_false = states
    # YES(leg 0) + NO(leg 1): true -> leg 0 wins, leg 1 loses; false -> the reverse.
    assert proposition_true.leg_payouts == (Decimal("1"), Decimal("0.3"))
    assert proposition_false.leg_payouts == (Decimal("0.2"), Decimal("1"))

    payout_sums = [sum(state.leg_payouts) for state in states]
    assert min(payout_sums) == Decimal("1.2")  # 0.2 + 1
    assert max(payout_sums) == Decimal("1.3")  # 1 + 0.3


def test_equivalence_terminal_states_descriptions_cite_both_markets() -> None:
    attestation_a = _attestation_a()
    attestation_b = _attestation_b()

    states = _equivalence_terminal_states(attestation_a, attestation_b, MARKET_A, MARKET_B)

    for state in states:
        assert MARKET_A in state.description
        assert MARKET_B in state.description
    assert len(states) == len({state.state_id for state in states})  # unique state_ids


# ---------------------------------------------------------------------------
# Hard negatives (spec section 15.2): every fixture pair, attested faithfully to its
# rule texts, must reject with its expected divergent matrix dimension flagged
# incompatible.
# ---------------------------------------------------------------------------


def _load_pairs() -> list[dict[str, Any]]:
    payload = json.loads(FIXTURE_PATH.read_text())
    return payload["pairs"]


def _hn_rule_version_id(market_id: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"hard-negative-equivalence:{market_id}")


def _hn_attestation_id(market_id: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"hard-negative-equivalence-attestation:{market_id}")


# Faithful attestation overrides per pair_id, hand-transcribed from each fixture
# member's own rule_text -- never derived from the text automatically, matching the
# rest of the codebase's stance that only a human reviewer produces attestation
# content. Each entry supplies (overrides_a, overrides_b); fields not mentioned keep
# the shared baseline (same resolution_source_attested/outcome_set_exhaustive/payout
# unless the field under test IS that dimension).
_BASELINE = {
    "outcome_set_exhaustive": True,
    "void_or_invalid_possible": False,
    "void_behavior": "unknown",
    "tie_possible": False,
    "tie_behavior": None,
    "payout_unit": "usdc_1_per_share",
    "winner_payout_per_share": Decimal("1"),
    "loser_payout_per_share": Decimal("0"),
    "deadline_utc": None,
    "threshold_text": None,
    "threshold_inclusive": None,
}


def _hn_overrides() -> dict[str, tuple[dict[str, Any], dict[str, Any]]]:
    shared_resolution_source = "Coinbase BTC-USD spot index"
    return {
        "btc-100k-threshold-inclusivity": (
            {
                "resolution_source_attested": shared_resolution_source,
                "threshold_text": "BTC/USD spot price relative to $100,000.00",
                "threshold_inclusive": True,
                "deadline_utc": datetime(2027, 1, 1, 4, 59, 59, tzinfo=UTC),
            },
            {
                "resolution_source_attested": shared_resolution_source,
                "threshold_text": "BTC/USD spot price relative to $100,000.00",
                "threshold_inclusive": False,
                "deadline_utc": datetime(2027, 1, 1, 4, 59, 59, tzinfo=UTC),
            },
        ),
        "fomc-march-2026-deadline-timezone": (
            {
                "resolution_source_attested": "Federal Reserve FOMC press release",
                "deadline_utc": datetime(2026, 3, 20, 4, 59, 59, tzinfo=UTC),
            },
            {
                "resolution_source_attested": "Federal Reserve FOMC press release",
                "deadline_utc": datetime(2026, 3, 19, 23, 59, 59, tzinfo=UTC),
            },
        ),
        "ga-senate-2026-resolution-source": (
            {"resolution_source_attested": "Associated Press race call"},
            {"resolution_source_attested": "Decision Desk HQ race call"},
        ),
        "nyc-temp-july4-observation-window": (
            {
                "resolution_source_attested": "National Weather Service Central Park station",
                "deadline_utc": datetime(2026, 7, 5, 3, 59, 59, tzinfo=UTC),
            },
            {
                "resolution_source_attested": "National Weather Service Central Park station",
                "deadline_utc": datetime(2026, 7, 5, 10, 59, 59, tzinfo=UTC),
            },
        ),
        "fomc-march-2026-same-underlying-exchange-frontend": (
            {"resolution_source_attested": "Federal Reserve FOMC press release"},
            {"resolution_source_attested": "Federal Reserve FOMC press release"},
        ),
        "btc-100k-subset-superset-scope": (
            {
                "resolution_source_attested": shared_resolution_source,
                "threshold_text": "spot price above $100,000.00 at any point during 2026",
                "threshold_inclusive": False,
                "deadline_utc": datetime(2027, 1, 1, 4, 59, 59, tzinfo=UTC),
            },
            {
                "resolution_source_attested": shared_resolution_source,
                "threshold_text": "closing price above $100,000.00 on December 31, 2026",
                "threshold_inclusive": False,
                "deadline_utc": datetime(2027, 1, 1, 4, 59, 59, tzinfo=UTC),
            },
        ),
    }


_EXPECTED_DIVERGENT_DIMENSION = {
    "btc-100k-threshold-inclusivity": "proposition_threshold_inclusivity",
    "fomc-march-2026-deadline-timezone": "observation_period_timezone",
    "ga-senate-2026-resolution-source": "resolution_sources",
    "nyc-temp-july4-observation-window": "observation_period_timezone",
    "btc-100k-subset-superset-scope": "proposition_threshold_inclusivity",
}

_FRONTEND_PAIR_ID = "fomc-march-2026-same-underlying-exchange-frontend"


def _build_hard_negative_case(pair: dict[str, Any]):
    market_a_id = pair["market_a"]["market_id"]
    market_b_id = pair["market_b"]["market_id"]
    venue_a = PredictionVenue(pair["market_a"]["venue"])
    venue_b = PredictionVenue(pair["market_b"]["venue"])
    rv_id_a = _hn_rule_version_id(market_a_id)
    rv_id_b = _hn_rule_version_id(market_b_id)
    att_id_a = _hn_attestation_id(market_a_id)
    att_id_b = _hn_attestation_id(market_b_id)

    legs = (
        leg(
            venue=venue_a,
            market_id=market_a_id,
            outcome_index=None,
            outcome_token_id=None,
            rule_version_id=rv_id_a,
            rule_source_hash=HASH,
        ),
        leg(
            venue=venue_b,
            market_id=market_b_id,
            outcome_index=None,
            outcome_token_id=None,
            rule_version_id=rv_id_b,
            rule_source_hash=HASH,
        ),
    )
    candidate = _candidate(legs=legs)

    overrides_a, overrides_b = _hn_overrides()[pair["pair_id"]]
    attestation_a = rule_attestation(
        **{
            **_BASELINE,
            "attestation_id": att_id_a,
            "rule_version_id": rv_id_a,
            "market_id": market_a_id,
            "venue": venue_a,
            "rule_source_hash": HASH,
            **overrides_a,
        }
    )
    attestation_b = rule_attestation(
        **{
            **_BASELINE,
            "attestation_id": att_id_b,
            "rule_version_id": rv_id_b,
            "market_id": market_b_id,
            "venue": venue_b,
            "rule_source_hash": HASH,
            **overrides_b,
        }
    )
    attestations = {rv_id_a: attestation_a, rv_id_b: attestation_b}
    rule_versions = {
        rv_id_a: rule_version(
            rule_version_id=rv_id_a, market_id=market_a_id, venue=venue_a, source_hash=HASH
        ),
        rv_id_b: rule_version(
            rule_version_id=rv_id_b, market_id=market_b_id, venue=venue_b, source_hash=HASH
        ),
    }
    return candidate, rule_versions, attestations


@pytest.mark.parametrize("pair", _load_pairs(), ids=lambda pair: pair["pair_id"])
def test_hard_negative_pairs_reject_with_expected_divergent_dimension(
    pair: dict[str, Any],
) -> None:
    candidate, rule_versions, attestations = _build_hard_negative_case(pair)

    artifact = compile_proof(
        candidate, rule_versions, attestations, as_of=NOW, review_identity=REVIEW_IDENTITY
    )

    assert artifact.status == "rejected"
    matrix = artifact.equivalence_matrix
    assert matrix is not None

    if pair["pair_id"] == _FRONTEND_PAIR_ID:
        # Not a genuine matrix dimension in v1: the underlying_exchange collision is
        # invisible to the attested facts, so this pair only rejects via the
        # always-unknown dimensions -- documenting that frontend detection is
        # increment-4+ work riding on venue manifests (spec section 6.1).
        assert artifact.rejection_reason in (
            "EQUIVALENCE_DIMENSION_UNKNOWN",
            "EQUIVALENCE_DIMENSION_INCOMPATIBLE",
        )
        first_six = (
            matrix.proposition_threshold_inclusivity,
            matrix.observation_period_timezone,
            matrix.resolution_sources,
            matrix.void_dispute_behavior,
            matrix.outcome_completeness,
            matrix.denomination_collateral_rounding,
        )
        assert not all(dimension == "compatible" for dimension in first_six)
        return

    expected_dimension = _EXPECTED_DIVERGENT_DIMENSION[pair["pair_id"]]
    assert artifact.rejection_reason == "EQUIVALENCE_DIMENSION_INCOMPATIBLE"
    assert getattr(matrix, expected_dimension) == "incompatible"
