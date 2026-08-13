from hashlib import sha256

import pytest

from polytrading.ai.extraction import RegexRuleExtractor
from polytrading.ai.models import CriticalField, RuleFieldSet, SourceSpan
from polytrading.ai.spans import (
    SourceHashMismatchError,
    SourceSpanBoundsError,
    SourceSpanTextMismatchError,
    validate_rule_fields,
    validate_span,
)


def source_span(text: str, exact_text: str, *, start: int | None = None) -> SourceSpan:
    offset = text.index(exact_text) if start is None else start
    return SourceSpan(
        start_char=offset,
        end_char=offset + len(exact_text),
        exact_text=exact_text,
        canonical_text_hash=sha256(text.encode("utf-8")).hexdigest(),
    )


def test_validate_span_accepts_only_the_exact_source_slice() -> None:
    text = "BTC resolves above $100."

    assert validate_span(source_span(text, "$100"), text) is None

    with pytest.raises(SourceHashMismatchError):
        validate_span(source_span(text, "$100"), text.replace("$100", "$101"))
    with pytest.raises(SourceSpanBoundsError):
        validate_span(
            SourceSpan(
                start_char=18,
                end_char=200,
                exact_text="$100",
                canonical_text_hash=sha256(text.encode("utf-8")).hexdigest(),
            ),
            text,
        )
    with pytest.raises(SourceSpanTextMismatchError):
        validate_span(
            SourceSpan(
                start_char=18,
                end_char=22,
                exact_text="$999",
                canonical_text_hash=sha256(text.encode("utf-8")).hexdigest(),
            ),
            text,
        )


def test_validate_rule_fields_rejects_the_complete_set_on_one_invalid_span() -> None:
    text = "BTC resolves above $100."
    unknown = CriticalField(status="unknown", value=None, supporting_spans=())
    values = {field: unknown for field in RuleFieldSet.model_fields}
    values["threshold"] = CriticalField(
        status="known",
        value="100",
        supporting_spans=(source_span(text, "$100"),),
    )
    values["operator"] = CriticalField(
        status="known",
        value=">",
        supporting_spans=(
            SourceSpan(
                start_char=13,
                end_char=18,
                exact_text="below",
                canonical_text_hash=sha256(text.encode("utf-8")).hexdigest(),
            ),
        ),
    )
    fields = RuleFieldSet(**values)

    with pytest.raises(SourceSpanTextMismatchError):
        validate_rule_fields(fields, text)

    assert fields.threshold.status == "known"
    assert fields.operator.value == ">"


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (">=", ">"),
        ("16:00", "17:00"),
        ("Coinbase", "Kraken"),
        ("use Kraken instead", "use Bitstamp instead"),
    ],
)
def test_mutating_any_critical_source_clause_invalidates_old_fields(old: str, new: str) -> None:
    text = (
        "BTC-USD resolves if the closing price according to Coinbase on 2026-08-12 "
        "at 16:00 UTC is >= $100. If Coinbase is unavailable, use Kraken instead."
    )
    result = RegexRuleExtractor().extract(text)
    mutated = text.replace(old, new, 1)

    assert any(field.status == "known" for _, field in result.fields)
    with pytest.raises(SourceHashMismatchError):
        validate_rule_fields(result.fields, mutated)
