from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import model_validator

from polytrading.ai.corpus import CorpusManifest, Split
from polytrading.ai.metrics import (
    MetricResult,
    MutationCaseResult,
    RelationshipMetricCase,
    candidate_recall,
    critical_field_exact_match,
    fail_closed_rate,
    mutation_invalidation_metrics,
    review_reduction,
    span_validity,
)
from polytrading.ai.models import ModelCard, NonEmptyString, RuleFieldSet
from polytrading.ai.spans import SourceSpanValidationError, validate_span
from polytrading.domain.models import StrictRecord
from polytrading.research.models import ExperimentRecord

GateStatus = Literal[
    "PASS",
    "FAIL",
    "NOT_EVALUATED",
    "FAIL_CLOSED_BREACH",
    "BLOCKED_BY_DEPENDENCY",
]
_THRESHOLDS = {
    "critical_field_exact_match": Decimal("0.95"),
    "candidate_recall": Decimal("0.90"),
    "span_validity": Decimal("1"),
    "malformed_fail_closed_rate": Decimal("1"),
    "hostile_fail_closed_rate": Decimal("1"),
    "mutation_invalidation": Decimal("1"),
    "review_reduction": Decimal("0.50"),
}


class ModelVersionRef(StrictRecord):
    model_id: NonEmptyString
    version: NonEmptyString
    feature_version: NonEmptyString
    prompt_version: NonEmptyString | None


class BooleanCaseResult(StrictRecord):
    case_id: NonEmptyString
    passed: bool


class FieldEvaluationCase(StrictRecord):
    contract_id: NonEmptyString
    canonical_text: str
    expected_fields: RuleFieldSet
    actual_fields: RuleFieldSet


class PayoffCompilerResults(StrictRecord):
    compiler_hash: NonEmptyString
    graph_hash: NonEmptyString
    all_cases_proved: bool


class EvaluationRequest(StrictRecord):
    schema_version: Literal[1]
    manifest: CorpusManifest
    experiment_id: UUID
    trial_family_id: NonEmptyString
    split: Split
    retrieval_top_k: int
    code_revision: NonEmptyString
    model_versions: tuple[ModelVersionRef, ...]
    split_event_families: dict[Split, tuple[NonEmptyString, ...]]
    split_counts: dict[Split, int]
    field_cases: tuple[FieldEvaluationCase, ...]
    relationship_cases: tuple[RelationshipMetricCase, ...]
    malformed_case_results: tuple[BooleanCaseResult, ...]
    hostile_case_results: tuple[BooleanCaseResult, ...]
    mutation_case_results: tuple[MutationCaseResult, ...]
    retrieval_candidate_count: int
    routed_manual_count: int
    payoff_compiler_results: PayoffCompilerResults | None

    @model_validator(mode="after")
    def require_complete_nonnegative_configuration(self) -> EvaluationRequest:
        if self.retrieval_top_k < 1:
            raise ValueError("retrieval top-k must be positive")
        if set(self.split_event_families) != {"train", "validation", "test"}:
            raise ValueError("event families must define every split")
        if set(self.split_counts) != {"train", "validation", "test"}:
            raise ValueError("split counts must define every split")
        if any(count < 0 for count in self.split_counts.values()):
            raise ValueError("split counts must be nonnegative")
        if self.retrieval_candidate_count < 0:
            raise ValueError("retrieval candidate count must be nonnegative")
        if not 0 <= self.routed_manual_count <= self.retrieval_candidate_count:
            raise ValueError("manual routing count must be within candidate count")
        identities = {(item.model_id, item.version) for item in self.model_versions}
        if not self.model_versions or len(identities) != len(self.model_versions):
            raise ValueError("model versions must be nonempty and unique")
        return self


class FailureExample(StrictRecord):
    kind: NonEmptyString
    item_id: NonEmptyString
    detail: NonEmptyString
    abstained: bool


class SemanticEvaluation(StrictRecord):
    request_hash: NonEmptyString
    split: Split
    metrics: tuple[MetricResult, ...]
    gate_status: GateStatus
    failure_examples: tuple[FailureExample, ...]
    malformed_case_results: tuple[BooleanCaseResult, ...]
    hostile_case_results: tuple[BooleanCaseResult, ...]
    mutation_case_results: tuple[MutationCaseResult, ...]
    payoff_compiler_results: PayoffCompilerResults | None

    def metric(self, name: str) -> MetricResult:
        matches = tuple(metric for metric in self.metrics if metric.name == name)
        if len(matches) != 1:
            raise KeyError(name)
        return matches[0]


class EvaluationAttempt(StrictRecord):
    sequence: int
    request_hash: NonEmptyString
    experiment_id: UUID
    trial_family_id: NonEmptyString
    split: Split
    accepted: bool
    failure_reason: str | None


class SemanticEvaluator:
    def __init__(
        self,
        registered_experiments: tuple[ExperimentRecord, ...],
        registered_model_cards: tuple[ModelCard, ...],
    ) -> None:
        self._experiments = {
            experiment.experiment_id: experiment for experiment in registered_experiments
        }
        if len(self._experiments) != len(registered_experiments):
            raise ValueError("registered experiment IDs must be unique")
        self._model_cards = {(card.model_id, card.version): card for card in registered_model_cards}
        if len(self._model_cards) != len(registered_model_cards):
            raise ValueError("registered model versions must be unique")
        self._attempts: list[EvaluationAttempt] = []
        self._completed_splits: dict[UUID, set[Split]] = {}

    @property
    def attempts(self) -> tuple[EvaluationAttempt, ...]:
        return tuple(self._attempts)

    @property
    def model_cards(self) -> tuple[ModelCard, ...]:
        return tuple(
            self._model_cards[key]
            for key in sorted(self._model_cards, key=lambda item: (item[0], item[1]))
        )

    def run(self, request: EvaluationRequest) -> SemanticEvaluation:
        request_hash = evaluation_request_hash(request)
        try:
            self._validate_request(request)
            result = _evaluate(request, request_hash)
        except Exception as error:
            self._record_attempt(request, request_hash, False, str(error))
            raise
        self._record_attempt(request, request_hash, True, None)
        self._completed_splits.setdefault(request.experiment_id, set()).add(request.split)
        return result

    def _record_attempt(
        self,
        request: EvaluationRequest,
        request_hash: str,
        accepted: bool,
        failure_reason: str | None,
    ) -> None:
        self._attempts.append(
            EvaluationAttempt(
                sequence=len(self._attempts) + 1,
                request_hash=request_hash,
                experiment_id=request.experiment_id,
                trial_family_id=request.trial_family_id,
                split=request.split,
                accepted=accepted,
                failure_reason=failure_reason,
            )
        )

    def _validate_request(self, request: EvaluationRequest) -> None:
        experiment = self._experiments.get(request.experiment_id)
        if experiment is None:
            raise ValueError("experiment ID is not registered")
        if experiment.trial_family_id != request.trial_family_id:
            raise ValueError("trial family ID is not registered for the experiment")
        if experiment.code_revision != request.code_revision:
            raise ValueError("evaluation code revision changed after registration")
        if not request.manifest.frozen:
            raise ValueError("evaluation requires a frozen corpus manifest")
        if request.manifest.information_cutoff > experiment.data_cutoff:
            raise ValueError("corpus information cutoff exceeds registered experiment cutoff")
        train = set(request.split_event_families["train"])
        test = set(request.split_event_families["test"])
        if train.intersection(test):
            raise ValueError("train and test event families overlap")
        if sum(request.split_counts.values()) != request.manifest.counts.get("contracts"):
            raise ValueError("split counts do not match frozen manifest contract count")
        for split, families in request.split_event_families.items():
            if len(families) != len(set(families)):
                raise ValueError(f"{split} event families must be unique")
            if _family_hash(families) != request.manifest.split_family_hashes[split]:
                raise ValueError(f"{split} event families do not match frozen manifest")

        completed = self._completed_splits.setdefault(request.experiment_id, set())
        if request.split == "validation" and "train" not in completed:
            raise ValueError("train diagnostics must run before validation")
        if request.split == "test" and "validation" not in completed:
            raise ValueError("validation must run before untouched test")
        if request.split == "test" and "test" in completed:
            raise ValueError("untouched test was already evaluated for this experiment")

        for reference in request.model_versions:
            card = self._model_cards.get((reference.model_id, reference.version))
            if card is None:
                raise ValueError("evaluation model version is not registered")
            if card.feature_version != reference.feature_version:
                raise ValueError("evaluation feature version changed after registration")
            if card.prompt_version != reference.prompt_version:
                raise ValueError("evaluation prompt version changed after registration")
            if (
                request.split == "test"
                and card.status == "validated"
                and card.validation_dataset_hash == request.manifest.dataset_id
            ):
                raise ValueError("model card was validated on the same untouched test output")


def _evaluate(request: EvaluationRequest, request_hash: str) -> SemanticEvaluation:
    field_pairs = tuple((case.expected_fields, case.actual_fields) for case in request.field_cases)
    span_results, span_failures = _span_results(request.field_cases)
    metrics = (
        critical_field_exact_match(
            field_pairs,
            threshold=_THRESHOLDS["critical_field_exact_match"],
        ),
        candidate_recall(
            request.relationship_cases,
            threshold=_THRESHOLDS["candidate_recall"],
        ),
        span_validity(span_results, threshold=_THRESHOLDS["span_validity"]),
        fail_closed_rate(
            "malformed_fail_closed_rate",
            tuple(case.passed for case in request.malformed_case_results),
            threshold=_THRESHOLDS["malformed_fail_closed_rate"],
        ),
        fail_closed_rate(
            "hostile_fail_closed_rate",
            tuple(case.passed for case in request.hostile_case_results),
            threshold=_THRESHOLDS["hostile_fail_closed_rate"],
        ),
        *mutation_invalidation_metrics(
            request.mutation_case_results,
            threshold=_THRESHOLDS["mutation_invalidation"],
        ),
        review_reduction(
            request.retrieval_candidate_count,
            request.routed_manual_count,
            threshold=_THRESHOLDS["review_reduction"],
        ),
    )
    failures = _failure_examples(request, span_failures)
    malformed_breach = any(not case.passed for case in request.malformed_case_results)
    gate_status = _gate_status(
        metrics,
        split=request.split,
        span_breach=bool(span_failures),
        malformed_breach=malformed_breach,
        payoff=request.payoff_compiler_results,
    )
    return SemanticEvaluation(
        request_hash=request_hash,
        split=request.split,
        metrics=metrics,
        gate_status=gate_status,
        failure_examples=failures,
        malformed_case_results=request.malformed_case_results,
        hostile_case_results=request.hostile_case_results,
        mutation_case_results=request.mutation_case_results,
        payoff_compiler_results=request.payoff_compiler_results,
    )


def _span_results(
    cases: tuple[FieldEvaluationCase, ...],
) -> tuple[tuple[bool, ...], tuple[str, ...]]:
    results: list[bool] = []
    failed_contracts: set[str] = set()
    for case in cases:
        for _, field in case.actual_fields:
            if field.status == "unknown":
                continue
            valid = True
            for span in field.supporting_spans:
                try:
                    validate_span(span, case.canonical_text)
                except SourceSpanValidationError:
                    valid = False
            results.append(valid)
            if not valid:
                failed_contracts.add(case.contract_id)
    return tuple(results), tuple(sorted(failed_contracts))


def _failure_examples(
    request: EvaluationRequest, span_failures: tuple[str, ...]
) -> tuple[FailureExample, ...]:
    failures: list[FailureExample] = []
    for case in request.field_cases:
        mismatch = any(
            (
                getattr(case.expected_fields, name).status
                != getattr(case.actual_fields, name).status
                or getattr(case.expected_fields, name).value
                != getattr(case.actual_fields, name).value
            )
            for name in RuleFieldSet.model_fields
        )
        if mismatch:
            abstained = all(field.status == "unknown" for _, field in case.actual_fields)
            failures.append(
                FailureExample(
                    kind="critical_field_mismatch",
                    item_id=case.contract_id,
                    detail="expected and actual critical fields differ",
                    abstained=abstained,
                )
            )
    for contract_id in span_failures:
        failures.append(
            FailureExample(
                kind="source_span_invalid",
                item_id=contract_id,
                detail="one or more known fields failed exact source-span validation",
                abstained=False,
            )
        )
    for case in request.relationship_cases:
        if case.known_positive and not case.retrieved:
            failures.append(
                FailureExample(
                    kind="relationship_not_retrieved",
                    item_id=case.relationship_id,
                    detail="known positive relationship was absent from candidate set",
                    abstained=False,
                )
            )
    for kind, cases in (
        ("malformed_accepted", request.malformed_case_results),
        ("hostile_not_closed", request.hostile_case_results),
    ):
        for case in cases:
            if not case.passed:
                failures.append(
                    FailureExample(
                        kind=kind,
                        item_id=case.case_id,
                        detail="fail-closed case did not pass",
                        abstained=False,
                    )
                )
    for case in request.mutation_case_results:
        if not case.invalidated:
            failures.append(
                FailureExample(
                    kind=f"mutation_not_invalidated:{case.group}",
                    item_id=case.case_id,
                    detail="source mutation did not invalidate the prior artifact",
                    abstained=False,
                )
            )
    return tuple(sorted(failures, key=lambda item: (item.kind, item.item_id)))


def _gate_status(
    metrics: tuple[MetricResult, ...],
    *,
    split: Split,
    span_breach: bool,
    malformed_breach: bool,
    payoff: PayoffCompilerResults | None,
) -> GateStatus:
    if span_breach or malformed_breach:
        return "FAIL_CLOSED_BREACH"
    if any(metric.status == "FAIL" for metric in metrics):
        return "FAIL"
    if split != "test" or any(metric.status == "NOT_MEASURABLE" for metric in metrics):
        return "NOT_EVALUATED"
    if payoff is None:
        return "BLOCKED_BY_DEPENDENCY"
    return "PASS" if payoff.all_cases_proved else "FAIL"


def evaluation_request_hash(request: EvaluationRequest) -> str:
    canonical = json.dumps(
        request.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _family_hash(families: tuple[str, ...]) -> str:
    canonical = json.dumps(
        sorted(set(families)),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256((canonical + "\n").encode("utf-8")).hexdigest()
