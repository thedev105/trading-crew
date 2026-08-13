from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Literal

from polytrading.ai.evaluate import (
    BooleanCaseResult,
    EvaluationRequest,
    FailureExample,
    GateStatus,
    SemanticEvaluation,
    evaluation_request_hash,
)
from polytrading.ai.metrics import MetricResult, MutationCaseResult
from polytrading.ai.models import ModelCard
from polytrading.domain.models import StrictRecord
from polytrading.research.models import ExperimentRecord


class ReportHashes(StrictRecord):
    manifest_hash: str
    code_hash: str
    experiment_hash: str
    model_hashes: dict[str, str]
    feature_hashes: dict[str, str]
    prompt_hashes: dict[str, str]


class ReportCorpus(StrictRecord):
    counts: dict[str, int]
    split_counts: dict[str, int]
    template_counts: dict[str, int]
    adversarial_counts: dict[str, int]


class ClassGDependency(StrictRecord):
    status: Literal["BLOCKED_BY_DEPENDENCY"]
    dependency: Literal["deterministic payoff compiler and graph"]


class SemanticScoutReport(StrictRecord):
    schema_version: Literal[1]
    overall_status: Literal["RESEARCH_ONLY_NOT_PROMOTABLE"]
    evaluation_basis: Literal["synthetic_fixture_self_consistency", "adjudicated_gold"]
    semantic_gate_status: GateStatus
    hashes: ReportHashes
    model_versions: tuple[str, ...]
    corpus: ReportCorpus
    metrics: tuple[MetricResult, ...]
    failure_examples: tuple[FailureExample, ...]
    malformed_case_results: tuple[BooleanCaseResult, ...]
    hostile_case_results: tuple[BooleanCaseResult, ...]
    mutation_case_results: tuple[MutationCaseResult, ...]
    inference_cost_usd: Decimal
    class_g_false_eligibility: ClassGDependency


def build_semantic_report(
    evaluation: SemanticEvaluation,
    request: EvaluationRequest,
    experiment: ExperimentRecord,
    model_cards: tuple[ModelCard, ...],
) -> SemanticScoutReport:
    if (
        evaluation.request_hash != evaluation_request_hash(request)
        or evaluation.split != request.split
    ):
        raise ValueError("evaluation does not match the supplied request")
    if experiment.experiment_id != request.experiment_id:
        raise ValueError("experiment does not match the supplied request")
    requested = {(item.model_id, item.version): item for item in request.model_versions}
    selected_cards = tuple(
        card for card in model_cards if (card.model_id, card.version) in requested
    )
    if len(selected_cards) != len(requested):
        raise ValueError("report model cards do not cover every evaluated model version")
    model_hashes: dict[str, str] = {}
    feature_hashes: dict[str, str] = {}
    prompt_hashes: dict[str, str] = {}
    versions: list[str] = []
    for card in sorted(selected_cards, key=lambda item: (item.model_id, item.version)):
        identity = f"{card.model_id}@{card.version}"
        versions.append(identity)
        model_hashes[identity] = _hash_json(card.model_dump(mode="json"))
        reference = requested[(card.model_id, card.version)]
        feature_hashes[identity] = _hash_text(reference.feature_version)
        prompt_hashes[identity] = _hash_text(reference.prompt_version or "none")
    return SemanticScoutReport(
        schema_version=1,
        overall_status="RESEARCH_ONLY_NOT_PROMOTABLE",
        evaluation_basis=request.evaluation_basis,
        semantic_gate_status=evaluation.gate_status,
        hashes=ReportHashes(
            manifest_hash=request.manifest.dataset_id,
            code_hash=_hash_text(request.code_revision),
            experiment_hash=_hash_json(experiment.model_dump(mode="json")),
            model_hashes=model_hashes,
            feature_hashes=feature_hashes,
            prompt_hashes=prompt_hashes,
        ),
        model_versions=tuple(versions),
        corpus=ReportCorpus(
            counts=dict(sorted(request.manifest.counts.items())),
            split_counts=dict(sorted(request.split_counts.items())),
            template_counts=dict(sorted(request.manifest.rule_template_counts.items())),
            adversarial_counts=dict(sorted(request.manifest.adversarial_tag_counts.items())),
        ),
        metrics=evaluation.metrics,
        failure_examples=evaluation.failure_examples,
        malformed_case_results=evaluation.malformed_case_results,
        hostile_case_results=evaluation.hostile_case_results,
        mutation_case_results=evaluation.mutation_case_results,
        inference_cost_usd=Decimal(0),
        class_g_false_eligibility=ClassGDependency(
            status="BLOCKED_BY_DEPENDENCY",
            dependency="deterministic payoff compiler and graph",
        ),
    )


def render_report_json(report: SemanticScoutReport) -> str:
    return (
        json.dumps(
            report.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def render_report_markdown(report: SemanticScoutReport) -> str:
    lines = [
        "# Offline semantic scout evaluation",
        "",
        f"Overall status: `{report.overall_status}`",
        f"Evaluation basis: `{report.evaluation_basis}`",
        f"Semantic gate: `{report.semantic_gate_status}`",
        "",
        "## Identity",
        "",
        f"- Manifest: `{report.hashes.manifest_hash}`",
        f"- Code: `{report.hashes.code_hash}`",
        f"- Experiment: `{report.hashes.experiment_hash}`",
        "- Models:",
    ]
    for identity in report.model_versions:
        lines.extend(
            [
                f"  - `{identity}` model: `{report.hashes.model_hashes[identity]}`",
                f"  - `{identity}` feature: `{report.hashes.feature_hashes[identity]}`",
                f"  - `{identity}` prompt: `{report.hashes.prompt_hashes[identity]}`",
            ]
        )
    lines.extend(
        [
            "",
            "## Corpus",
            "",
            f"- Counts: `{_compact_json(report.corpus.counts)}`",
            f"- Split counts: `{_compact_json(report.corpus.split_counts)}`",
            f"- Templates: `{_compact_json(report.corpus.template_counts)}`",
            f"- Adversarial tags: `{_compact_json(report.corpus.adversarial_counts)}`",
            "",
            "## Metrics",
            "",
            "| Metric | Numerator | Denominator | Exact value | Threshold | Status |",
            "|---|---:|---:|---:|---:|---|",
        ]
    )
    for metric in report.metrics:
        value = "NOT_MEASURABLE" if metric.value is None else str(metric.value)
        lines.append(
            f"| {metric.name} | {metric.numerator} | {metric.denominator} | "
            f"{value} | {metric.threshold} | {metric.status} |"
        )
    lines.extend(["", "## Failure examples", ""])
    if report.failure_examples:
        for failure in report.failure_examples:
            abstained = str(failure.abstained).lower()
            lines.append(f"- `{failure.item_id}` — {failure.kind}; abstained={abstained}")
    else:
        lines.append("- None in this evaluated fixture.")
    lines.extend(["", "## Fail-closed cases", ""])
    for case in report.malformed_case_results:
        lines.append(f"- malformed:{case.case_id}={_case_status(case.passed)}")
    for case in report.hostile_case_results:
        lines.append(f"- hostile:{case.case_id}={_case_status(case.passed)}")
    for case in report.mutation_case_results:
        lines.append(f"- mutation:{case.group}:{case.case_id}={_case_status(case.invalidated)}")
    lines.extend(
        [
            "",
            "## Authority boundary",
            "",
            "- In-repository baseline inference cost: `USD 0`.",
            "- Class G false-eligibility: `BLOCKED_BY_DEPENDENCY`.",
            "- Missing dependency: `deterministic payoff compiler and graph`.",
            "- This report grants no proposal, risk, credential, or execution authority.",
            "",
        ]
    )
    return "\n".join(lines)


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _hash_json(value: object) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return _hash_text(canonical)


def _compact_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _case_status(passed: bool) -> str:
    return "PASS" if passed else "FAIL"
