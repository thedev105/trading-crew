import pytest
from pydantic import ValidationError

from polytrading.predictions.propositions import PropositionSpan, TypedProposition

HASH = "a" * 64


def _span(**overrides: object) -> PropositionSpan:
    values: dict[str, object] = {
        "start_char": 10,
        "end_char": 20,
        "exact_text": "at least $100,000",
        "rule_source_hash": HASH,
    }
    values.update(overrides)
    return PropositionSpan(**values)


def test_extracted_proposition_requires_supporting_spans() -> None:
    with pytest.raises(ValidationError, match="supporting"):
        TypedProposition(
            schema_version=1,
            kind="threshold",
            subject="BTC price",
            predicate=">=",
            value="100000",
            status="extracted",
            supporting_spans=(),
        )


def test_unknown_proposition_forbids_value_and_spans() -> None:
    with pytest.raises(ValidationError):
        TypedProposition(
            schema_version=1,
            kind="deadline",
            subject="event",
            predicate="resolves_by",
            value="2026-12-31",
            status="unknown",
            supporting_spans=(),
        )


def test_unknown_proposition_forbids_spans_even_without_value() -> None:
    with pytest.raises(ValidationError):
        TypedProposition(
            schema_version=1,
            kind="deadline",
            subject="event",
            predicate="resolves_by",
            value=None,
            status="unknown",
            supporting_spans=(_span(),),
        )


def test_span_bounds_must_be_nonempty_and_ordered() -> None:
    with pytest.raises(ValidationError):
        PropositionSpan(start_char=5, end_char=5, exact_text="x", rule_source_hash=HASH)


def test_span_start_must_be_nonnegative() -> None:
    with pytest.raises(ValidationError):
        PropositionSpan(start_char=-1, end_char=5, exact_text="x", rule_source_hash=HASH)


def test_span_requires_nonempty_exact_text() -> None:
    with pytest.raises(ValidationError):
        _span(exact_text="")


def test_span_requires_sha256_rule_source_hash() -> None:
    with pytest.raises(ValidationError):
        _span(rule_source_hash="not-a-hash")


def test_valid_extracted_proposition_round_trips_through_json() -> None:
    proposition = TypedProposition(
        schema_version=1,
        kind="threshold",
        subject="BTC price",
        predicate=">=",
        value="100000",
        status="extracted",
        supporting_spans=(_span(),),
    )
    restored = TypedProposition.model_validate_json(proposition.model_dump_json())
    assert restored == proposition


def test_unknown_proposition_with_no_value_or_spans_is_valid() -> None:
    proposition = TypedProposition(
        schema_version=1,
        kind="scope",
        subject="event",
        predicate="applies_to",
        value=None,
        status="unknown",
        supporting_spans=(),
    )
    assert proposition.status == "unknown"


def test_proposition_forbids_extra_fields() -> None:
    with pytest.raises(ValidationError):
        TypedProposition(
            schema_version=1,
            kind="scope",
            subject="event",
            predicate="applies_to",
            value=None,
            status="unknown",
            supporting_spans=(),
            unexpected=1,
        )
