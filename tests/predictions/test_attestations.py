import pytest
from pydantic import ValidationError

from tests.predictions.attestation_helpers import HASH, rule_attestation, supporting_span


def test_valid_attestation_round_trips() -> None:
    attestation = rule_attestation()
    assert attestation.rule_source_hash == HASH
    assert len(attestation.supporting_spans) == 1


def test_rejects_empty_supporting_spans() -> None:
    with pytest.raises(ValidationError, match="supporting span"):
        rule_attestation(supporting_spans=())


def test_rejects_a_span_bound_to_a_different_rule_source_hash() -> None:
    mismatched_span = supporting_span(rule_source_hash="b" * 64)
    with pytest.raises(ValidationError, match="rule_source_hash"):
        rule_attestation(supporting_spans=(mismatched_span,))


def test_tie_possible_requires_a_tie_behavior() -> None:
    with pytest.raises(ValidationError, match="tie_behavior"):
        rule_attestation(tie_possible=True, tie_behavior=None)


def test_tie_possible_with_a_tie_behavior_is_valid() -> None:
    attestation = rule_attestation(tie_possible=True, tie_behavior="split_evenly")
    assert attestation.tie_behavior == "split_evenly"


def test_tie_impossible_allows_a_none_tie_behavior() -> None:
    attestation = rule_attestation(tie_possible=False, tie_behavior=None)
    assert attestation.tie_behavior is None


def test_void_or_invalid_possible_permits_unknown_void_behavior() -> None:
    # unknown is representable at the model layer even though proof compilers reject it.
    attestation = rule_attestation(void_or_invalid_possible=True, void_behavior="unknown")
    assert attestation.void_behavior == "unknown"


def test_rejects_a_non_positive_winner_payout() -> None:
    with pytest.raises(ValidationError):
        rule_attestation(winner_payout_per_share="0")


def test_rejects_a_negative_loser_payout() -> None:
    with pytest.raises(ValidationError):
        rule_attestation(loser_payout_per_share="-1")


def test_rejects_an_empty_review_identity() -> None:
    with pytest.raises(ValidationError):
        rule_attestation(review_identity="")


def test_rejects_a_non_utc_reviewed_at() -> None:
    from datetime import datetime, timedelta, timezone

    naive = datetime(2026, 8, 15, 12)
    with pytest.raises(ValidationError):
        rule_attestation(reviewed_at=naive)
    aware_non_utc = datetime(2026, 8, 15, 12, tzinfo=timezone(timedelta(hours=5)))
    attestation = rule_attestation(reviewed_at=aware_non_utc)
    assert attestation.reviewed_at.utcoffset() == timedelta(0)


def test_record_is_frozen() -> None:
    attestation = rule_attestation()
    with pytest.raises(ValidationError):
        attestation.market_id = "different"  # type: ignore[misc]


def test_rejects_unknown_extra_fields() -> None:
    with pytest.raises(ValidationError):
        rule_attestation(unexpected_field="nope")
