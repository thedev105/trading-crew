from decimal import Decimal

from polytrading.ai.metrics import (
    MutationCaseResult,
    RelationshipMetricCase,
    candidate_recall,
    critical_field_exact_match,
    fail_closed_rate,
    mutation_invalidation_metrics,
    ratio_metric,
    review_reduction,
    span_validity,
)
from polytrading.ai.models import CriticalField, RuleFieldSet, SourceSpan


def unknown_fields() -> RuleFieldSet:
    unknown = CriticalField(status="unknown", value=None, supporting_spans=())
    return RuleFieldSet(**{field: unknown for field in RuleFieldSet.model_fields})


def with_operator(fields: RuleFieldSet, value: str) -> RuleFieldSet:
    values = {name: field for name, field in fields}
    values["operator"] = CriticalField(
        status="known",
        value=value,
        supporting_spans=(
            SourceSpan(
                start_char=0,
                end_char=1,
                exact_text=">",
                canonical_text_hash="a" * 64,
            ),
        ),
    )
    return RuleFieldSet(**values)


def test_critical_field_exact_match_counts_status_and_exact_value_for_every_field() -> None:
    expected = with_operator(unknown_fields(), ">")
    correct = with_operator(unknown_fields(), ">")
    wrong = with_operator(unknown_fields(), ">=")

    perfect = critical_field_exact_match(((expected, correct),), threshold=Decimal("1"))
    imperfect = critical_field_exact_match(((expected, wrong),), threshold=Decimal("1"))

    assert perfect.numerator == len(RuleFieldSet.model_fields)
    assert perfect.denominator == len(RuleFieldSet.model_fields)
    assert perfect.value == Decimal(1)
    assert perfect.status == "PASS"
    assert imperfect.numerator == len(RuleFieldSet.model_fields) - 1
    assert imperfect.status == "FAIL"


def test_candidate_recall_counts_only_known_positive_relationships() -> None:
    metric = candidate_recall(
        (
            RelationshipMetricCase(
                relationship_id="positive-hit", known_positive=True, retrieved=True
            ),
            RelationshipMetricCase(
                relationship_id="positive-miss", known_positive=True, retrieved=False
            ),
            RelationshipMetricCase(
                relationship_id="known-negative", known_positive=False, retrieved=True
            ),
        ),
        threshold=Decimal("0.5"),
    )

    assert (metric.numerator, metric.denominator, metric.value, metric.status) == (
        1,
        2,
        Decimal("0.5"),
        "PASS",
    )


def test_span_malformed_mutation_and_review_metrics_use_exact_arithmetic() -> None:
    spans = span_validity((True, False, True), threshold=Decimal("0.6"))
    malformed = fail_closed_rate(
        "malformed_fail_closed_rate", (True, True, False), threshold=Decimal("1")
    )
    mutations = mutation_invalidation_metrics(
        tuple(
            MutationCaseResult(case_id=group, group=group, invalidated=True)
            for group in ("operator", "timestamp", "oracle", "fallback")
        ),
        threshold=Decimal("1"),
    )
    reduction = review_reduction(100, 25, threshold=Decimal("0.75"))

    assert spans.value == Decimal(2) / Decimal(3)
    assert malformed.value == Decimal(2) / Decimal(3)
    assert malformed.status == "FAIL"
    assert {metric.name for metric in mutations} == {
        "mutation_invalidation_operator",
        "mutation_invalidation_timestamp",
        "mutation_invalidation_oracle",
        "mutation_invalidation_fallback",
    }
    assert all(metric.value == Decimal(1) for metric in mutations)
    assert reduction.value == Decimal("0.75")
    assert (reduction.numerator, reduction.denominator) == (75, 100)


def test_zero_denominator_is_named_not_measurable_and_never_nan() -> None:
    metric = ratio_metric("empty", 0, 0, threshold=Decimal("0.9"))

    assert metric.status == "NOT_MEASURABLE"
    assert metric.value is None
    assert metric.numerator == 0
    assert metric.denominator == 0


def test_threshold_comparison_uses_unrounded_value_and_greater_than_or_equal() -> None:
    below = ratio_metric("recall", 8999, 10000, threshold=Decimal("0.9"))
    equal = ratio_metric("recall", 9, 10, threshold=Decimal("0.9"))

    assert below.value == Decimal("0.8999")
    assert below.status == "FAIL"
    assert equal.status == "PASS"
