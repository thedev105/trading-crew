from decimal import Decimal

import pytest
from pydantic import ValidationError

from polytrading.predictions.proofs_models import APPROVED_PROOF_TEMPLATES, ProofArtifact
from tests.predictions.proof_helpers import (
    HASH,
    equivalence_matrix,
    excluded_state,
    proof_artifact,
    terminal_state,
)


def test_proof_ready_with_valid_fields_round_trips() -> None:
    record = proof_artifact()
    assert record.status == "proof_ready"
    restored = ProofArtifact.model_validate_json(record.model_dump_json())
    assert restored == record


def test_every_approved_template_is_usable_proof_ready() -> None:
    for template in APPROVED_PROOF_TEMPLATES:
        if template == "cross_venue_equivalence@1":
            record = proof_artifact(template=template, equivalence_matrix=equivalence_matrix())
        else:
            record = proof_artifact(template=template)
        assert record.template == template


def test_unapproved_template_cannot_be_proof_ready() -> None:
    with pytest.raises(ValidationError, match="template"):
        proof_artifact(template="unapproved_template@1")


def test_proof_ready_forbids_a_rejection_reason() -> None:
    with pytest.raises(ValidationError, match="rejection_reason"):
        proof_artifact(rejection_reason="MISSING_ATTESTATION")


def test_proof_ready_requires_at_least_one_terminal_state() -> None:
    with pytest.raises(ValidationError, match="terminal state"):
        proof_artifact(terminal_states=())


def test_proof_ready_requires_minimum_basket_payout() -> None:
    with pytest.raises(ValidationError, match="basket payout"):
        proof_artifact(minimum_basket_payout=None)


def test_proof_ready_requires_maximum_basket_payout() -> None:
    with pytest.raises(ValidationError, match="basket payout"):
        proof_artifact(maximum_basket_payout=None)


def test_rejected_requires_a_rejection_reason() -> None:
    with pytest.raises(ValidationError, match="rejection_reason"):
        proof_artifact(
            status="rejected",
            rejection_reason=None,
            terminal_states=(),
            minimum_basket_payout=None,
            maximum_basket_payout=None,
        )


def test_insufficient_evidence_requires_a_rejection_reason() -> None:
    with pytest.raises(ValidationError, match="rejection_reason"):
        proof_artifact(
            status="insufficient_evidence",
            rejection_reason=None,
            terminal_states=(),
            minimum_basket_payout=None,
            maximum_basket_payout=None,
        )


def test_rejected_forbids_a_minimum_basket_payout() -> None:
    with pytest.raises(ValidationError, match="basket payout"):
        proof_artifact(
            status="rejected",
            rejection_reason="MISSING_ATTESTATION",
            terminal_states=(),
            minimum_basket_payout=Decimal("1"),
            maximum_basket_payout=None,
        )


def test_rejected_forbids_a_maximum_basket_payout() -> None:
    with pytest.raises(ValidationError, match="basket payout"):
        proof_artifact(
            status="rejected",
            rejection_reason="MISSING_ATTESTATION",
            terminal_states=(),
            minimum_basket_payout=None,
            maximum_basket_payout=Decimal("1"),
        )


def test_rejected_with_no_basket_payout_is_valid() -> None:
    record = proof_artifact(
        status="rejected",
        rejection_reason="OUTCOME_SET_NOT_EXHAUSTIVE",
        terminal_states=(),
        minimum_basket_payout=None,
        maximum_basket_payout=None,
    )
    assert record.status == "rejected"
    assert record.rejection_reason == "OUTCOME_SET_NOT_EXHAUSTIVE"


def test_insufficient_evidence_with_missing_attestation_is_valid() -> None:
    record = proof_artifact(
        status="insufficient_evidence",
        rejection_reason="MISSING_ATTESTATION",
        terminal_states=(),
        minimum_basket_payout=None,
        maximum_basket_payout=None,
    )
    assert record.rejection_reason == "MISSING_ATTESTATION"


def test_equivalence_matrix_required_for_cross_venue_equivalence_template() -> None:
    with pytest.raises(ValidationError, match="equivalence_matrix"):
        proof_artifact(template="cross_venue_equivalence@1", equivalence_matrix=None)


def test_equivalence_matrix_forbidden_for_non_cross_venue_template() -> None:
    with pytest.raises(ValidationError, match="equivalence_matrix"):
        proof_artifact(
            template="binary_complement@1",
            equivalence_matrix=equivalence_matrix(),
        )


def test_equivalence_matrix_present_with_cross_venue_equivalence_is_valid() -> None:
    record = proof_artifact(
        template="cross_venue_equivalence@1", equivalence_matrix=equivalence_matrix()
    )
    assert record.equivalence_matrix is not None
    assert record.equivalence_matrix.resolution_sources == "compatible"


def test_equivalence_matrix_rejects_an_invalid_dimension_value() -> None:
    with pytest.raises(ValidationError):
        equivalence_matrix(resolution_sources="maybe")


def test_invalidation_conditions_must_include_the_rule_version_change_literal() -> None:
    with pytest.raises(ValidationError, match="invalidation_conditions"):
        proof_artifact(invalidation_conditions=("some other condition",))


def test_invalidation_conditions_may_include_additional_conditions() -> None:
    record = proof_artifact(
        invalidation_conditions=(
            "any participating rule_version change",
            "a manual review revocation",
        )
    )
    assert "any participating rule_version change" in record.invalidation_conditions


def test_source_hashes_must_be_sorted_and_unique() -> None:
    with pytest.raises(ValidationError, match="source_hashes"):
        proof_artifact(source_hashes=(HASH, HASH))


def test_source_hashes_out_of_order_is_rejected() -> None:
    high = "b" * 64
    low = "a" * 64
    with pytest.raises(ValidationError, match="source_hashes"):
        proof_artifact(source_hashes=(high, low))


def test_terminal_states_must_share_the_same_leg_payout_length() -> None:
    with pytest.raises(ValidationError, match="leg payout"):
        proof_artifact(
            terminal_states=(
                terminal_state(state_id="a", leg_payouts=(Decimal("1"), Decimal("0"))),
                terminal_state(state_id="b", leg_payouts=(Decimal("1"),)),
            )
        )


def test_terminal_state_ids_must_be_unique_within_a_proof() -> None:
    with pytest.raises(ValidationError, match="state_id"):
        proof_artifact(
            terminal_states=(
                terminal_state(state_id="dup", leg_payouts=(Decimal("1"), Decimal("0"))),
                terminal_state(state_id="dup", leg_payouts=(Decimal("0"), Decimal("1"))),
            )
        )


def test_terminal_state_rejects_a_negative_leg_payout() -> None:
    with pytest.raises(ValidationError):
        terminal_state(leg_payouts=(Decimal("-1"),))


def test_terminal_state_rejects_an_empty_state_id() -> None:
    with pytest.raises(ValidationError):
        terminal_state(state_id="")


def test_excluded_state_round_trips() -> None:
    record = excluded_state()
    assert record.exclusion_reason


def test_record_is_frozen() -> None:
    record = proof_artifact()
    with pytest.raises(ValidationError):
        record.template = "different"  # type: ignore[misc]


def test_rejects_unknown_extra_fields() -> None:
    with pytest.raises(ValidationError):
        proof_artifact(unexpected_field="nope")
