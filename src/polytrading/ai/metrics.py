from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import model_validator

from polytrading.ai.models import RuleFieldSet
from polytrading.domain.models import StrictRecord

MetricStatus = Literal["PASS", "FAIL", "NOT_MEASURABLE"]
MutationGroup = Literal["operator", "timestamp", "oracle", "fallback"]
_MUTATION_GROUPS: tuple[MutationGroup, ...] = (
    "operator",
    "timestamp",
    "oracle",
    "fallback",
)


class MetricResult(StrictRecord):
    name: str
    numerator: int
    denominator: int
    value: Decimal | None
    threshold: Decimal
    status: MetricStatus

    @model_validator(mode="after")
    def require_consistent_ratio(self) -> MetricResult:
        if self.numerator < 0 or self.denominator < 0 or self.numerator > self.denominator:
            raise ValueError("metric counts must satisfy 0 <= numerator <= denominator")
        if self.denominator == 0:
            if self.value is not None or self.status != "NOT_MEASURABLE":
                raise ValueError("zero-denominator metric must be NOT_MEASURABLE")
        else:
            expected = Decimal(self.numerator) / Decimal(self.denominator)
            if self.value != expected:
                raise ValueError("metric value must equal its exact count ratio")
            expected_status = "PASS" if expected >= self.threshold else "FAIL"
            if self.status != expected_status:
                raise ValueError("metric status must use exact greater-than-or-equal threshold")
        return self


class RelationshipMetricCase(StrictRecord):
    relationship_id: str
    known_positive: bool
    retrieved: bool


class MutationCaseResult(StrictRecord):
    case_id: str
    group: MutationGroup
    invalidated: bool


def ratio_metric(
    name: str,
    numerator: int,
    denominator: int,
    *,
    threshold: Decimal,
) -> MetricResult:
    if denominator == 0:
        return MetricResult(
            name=name,
            numerator=numerator,
            denominator=denominator,
            value=None,
            threshold=threshold,
            status="NOT_MEASURABLE",
        )
    value = Decimal(numerator) / Decimal(denominator)
    return MetricResult(
        name=name,
        numerator=numerator,
        denominator=denominator,
        value=value,
        threshold=threshold,
        status="PASS" if value >= threshold else "FAIL",
    )


def critical_field_exact_match(
    pairs: tuple[tuple[RuleFieldSet, RuleFieldSet], ...],
    *,
    threshold: Decimal,
) -> MetricResult:
    numerator = 0
    denominator = 0
    for expected, actual in pairs:
        for field_name in RuleFieldSet.model_fields:
            expected_field = getattr(expected, field_name)
            actual_field = getattr(actual, field_name)
            denominator += 1
            if expected_field.status != actual_field.status:
                continue
            if expected_field.status == "unknown" or expected_field.value == actual_field.value:
                numerator += 1
    return ratio_metric(
        "critical_field_exact_match",
        numerator,
        denominator,
        threshold=threshold,
    )


def candidate_recall(
    cases: tuple[RelationshipMetricCase, ...], *, threshold: Decimal
) -> MetricResult:
    positives = tuple(case for case in cases if case.known_positive)
    retrieved = sum(case.retrieved for case in positives)
    return ratio_metric(
        "candidate_recall",
        retrieved,
        len(positives),
        threshold=threshold,
    )


def span_validity(results: tuple[bool, ...], *, threshold: Decimal) -> MetricResult:
    return ratio_metric(
        "span_validity",
        sum(results),
        len(results),
        threshold=threshold,
    )


def fail_closed_rate(name: str, results: tuple[bool, ...], *, threshold: Decimal) -> MetricResult:
    return ratio_metric(name, sum(results), len(results), threshold=threshold)


def mutation_invalidation_metrics(
    cases: tuple[MutationCaseResult, ...], *, threshold: Decimal
) -> tuple[MetricResult, ...]:
    metrics: list[MetricResult] = []
    for group in _MUTATION_GROUPS:
        group_cases = tuple(case for case in cases if case.group == group)
        metrics.append(
            ratio_metric(
                f"mutation_invalidation_{group}",
                sum(case.invalidated for case in group_cases),
                len(group_cases),
                threshold=threshold,
            )
        )
    return tuple(metrics)


def review_reduction(
    retrieval_candidate_count: int,
    routed_manual_count: int,
    *,
    threshold: Decimal,
) -> MetricResult:
    if retrieval_candidate_count < 0 or not 0 <= routed_manual_count <= retrieval_candidate_count:
        raise ValueError("manual routing count must be within retrieval candidate count")
    return ratio_metric(
        "review_reduction",
        retrieval_candidate_count - routed_manual_count,
        retrieval_candidate_count,
        threshold=threshold,
    )
