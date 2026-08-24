import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from polytrading.ai.corpus import CorpusManifest
from polytrading.ai.evaluate import (
    _THRESHOLDS,
    BooleanCaseResult,
    EvaluationRequest,
    FieldEvaluationCase,
    ModelVersionRef,
    SemanticEvaluator,
)
from polytrading.ai.extraction import RegexRuleExtractor
from polytrading.ai.metrics import MutationCaseResult, RelationshipMetricCase
from polytrading.ai.models import ModelCard
from polytrading.research.models import (
    EvaluationWindow,
    ExperimentRecord,
    SuccessCriterion,
)

NOW = datetime(2026, 8, 12, 12, tzinfo=UTC)
EXPERIMENT_ID = UUID("00000000-0000-7000-8000-000000000801")
DATASET_ID = "a" * 64


def family_hash(*families: str) -> str:
    payload = (json.dumps(sorted(families), separators=(",", ":")) + "\n").encode()
    return hashlib.sha256(payload).hexdigest()


def manifest(**overrides: object) -> CorpusManifest:
    values: dict[str, object] = {
        "schema_version": 1,
        "dataset_id": DATASET_ID,
        "created_at": NOW,
        "information_cutoff": NOW,
        "file_hashes": {
            "contracts": "b" * 64,
            "relationships": "c" * 64,
            "labels": "d" * 64,
            "reviews": "e" * 64,
        },
        "split_family_hashes": {
            "train": family_hash("family-train"),
            "validation": family_hash("family-validation"),
            "test": family_hash("family-test"),
        },
        "counts": {"contracts": 3, "relationships": 1, "labels": 3, "reviews": 0},
        "rule_template_counts": {"binary_threshold": 3},
        "adversarial_tag_counts": {"prompt_injection": 1},
        "review_completion": {"complete": 0, "unresolved": 4},
        "frozen": True,
    }
    values.update(overrides)
    return CorpusManifest(**values)


def experiment(**overrides: object) -> ExperimentRecord:
    values: dict[str, object] = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "hypothesis": "offline semantic scout can reduce review while failing closed",
        "feature_allowlist": ("semantic-tfidf-char35", "rule-regex-baseline"),
        "parameters": (),
        "evaluation_window": EvaluationWindow(
            starts_at=NOW,
            ends_at=NOW + timedelta(days=7),
        ),
        "benchmark": "synthetic deterministic baseline",
        "success_criteria": (
            SuccessCriterion(
                metric="malformed_fail_closed_rate", operator="gte", threshold=Decimal(1)
            ),
        ),
        "code_revision": "code-revision-1",
        "data_cutoff": NOW,
        "fee_version": "not-applicable",
        "trial_family_id": "semantic-scout-v1",
    }
    values.update(overrides)
    return ExperimentRecord(**values)


def model_card(**overrides: object) -> ModelCard:
    values: dict[str, object] = {
        "schema_version": 1,
        "model_id": "rule-regex-baseline",
        "version": "1.0.0",
        "owner": "research",
        "intended_use": "offline deterministic evaluation",
        "prohibited_uses": (
            "trade_approval",
            "order_submission",
            "risk_limit_changes",
            "credential_access",
        ),
        "authority": "research_only",
        "implementation_kind": "deterministic_baseline",
        "training_cutoff": None,
        "prompt_version": "regex-v1",
        "feature_version": "feature-v1",
        "validation_dataset_hash": DATASET_ID,
        "status": "draft",
        "approved_at": None,
        "expires_at": None,
    }
    values.update(overrides)
    return ModelCard(**values)


def request(split: str = "train", **overrides: object) -> EvaluationRequest:
    text = "BTC resolves above $100."
    fields = RegexRuleExtractor().extract(text).fields
    values: dict[str, object] = {
        "schema_version": 1,
        "manifest": manifest(),
        "experiment_id": EXPERIMENT_ID,
        "trial_family_id": "semantic-scout-v1",
        "split": split,
        "retrieval_top_k": 50,
        "code_revision": "code-revision-1",
        "model_versions": (
            ModelVersionRef(
                model_id="rule-regex-baseline",
                version="1.0.0",
                feature_version="feature-v1",
                prompt_version="regex-v1",
            ),
        ),
        "split_event_families": {
            "train": ("family-train",),
            "validation": ("family-validation",),
            "test": ("family-test",),
        },
        "split_counts": {"train": 1, "validation": 1, "test": 1},
        "field_cases": (
            FieldEvaluationCase(
                contract_id=f"{split}-contract",
                canonical_text=text,
                expected_fields=fields,
                actual_fields=fields,
            ),
        ),
        "relationship_cases": (
            RelationshipMetricCase(
                relationship_id=f"{split}-relationship",
                known_positive=True,
                retrieved=True,
            ),
        ),
        "malformed_case_results": (BooleanCaseResult(case_id="malformed-1", passed=True),),
        "hostile_case_results": (BooleanCaseResult(case_id="hostile-1", passed=True),),
        "mutation_case_results": tuple(
            MutationCaseResult(case_id=group, group=group, invalidated=True)
            for group in ("operator", "timestamp", "oracle", "fallback")
        ),
        "retrieval_candidate_count": 10,
        "routed_manual_count": 2,
        "payoff_compiler_results": None,
    }
    values.update(overrides)
    return EvaluationRequest(**values)


def evaluator(card: ModelCard | None = None) -> SemanticEvaluator:
    return SemanticEvaluator((experiment(),), (card or model_card(),))


def run_through_test(service: SemanticEvaluator, **test_overrides: object):
    service.run(request("train"))
    service.run(request("validation"))
    return service.run(request("test", **test_overrides))


def test_critical_field_gate_requires_995_per_thousand() -> None:
    assert _THRESHOLDS["critical_field_exact_match"] == Decimal("0.995")


def test_synthetic_fixture_corpus_does_not_pass_the_raised_gate() -> None:
    evaluation = run_through_test(evaluator())

    assert evaluation.gate_status != "PASS"


def test_test_gate_remains_blocked_when_all_measurable_metrics_pass_without_payoff() -> None:
    result = run_through_test(evaluator())

    assert all(metric.status == "PASS" for metric in result.metrics)
    assert result.gate_status == "BLOCKED_BY_DEPENDENCY"


def test_metric_below_threshold_yields_fail() -> None:
    service = evaluator()
    service.run(request("train"))
    service.run(request("validation"))
    relationship_cases = (
        RelationshipMetricCase(relationship_id="miss", known_positive=True, retrieved=False),
    )

    result = service.run(request("test", relationship_cases=relationship_cases))

    assert result.metric("candidate_recall").status == "FAIL"
    assert result.gate_status == "FAIL"


def test_validation_without_untouched_test_is_not_evaluated() -> None:
    service = evaluator()
    service.run(request("train"))

    result = service.run(request("validation"))

    assert result.gate_status == "NOT_EVALUATED"


@pytest.mark.parametrize("breach", ["span", "malformed"])
def test_invalid_span_or_malformed_acceptance_is_fail_closed_breach(breach: str) -> None:
    service = evaluator()
    service.run(request("train"))
    service.run(request("validation"))
    overrides: dict[str, object] = {}
    if breach == "span":
        base = request("test").field_cases[0]
        changed_text = base.canonical_text.replace("$100", "$101")
        overrides["field_cases"] = (
            FieldEvaluationCase(
                contract_id=base.contract_id,
                canonical_text=changed_text,
                expected_fields=base.expected_fields,
                actual_fields=base.actual_fields,
            ),
        )
    else:
        overrides["malformed_case_results"] = (
            BooleanCaseResult(case_id="accepted-malformed", passed=False),
        )

    result = service.run(request("test", **overrides))

    assert result.gate_status == "FAIL_CLOSED_BREACH"


def test_rejects_family_overlap_changed_versions_and_unregistered_trial_family() -> None:
    overlapping = request(
        "train",
        split_event_families={
            "train": ("shared",),
            "validation": ("validation",),
            "test": ("shared",),
        },
    )
    changed_feature = request(
        "train",
        model_versions=(
            ModelVersionRef(
                model_id="rule-regex-baseline",
                version="1.0.0",
                feature_version="changed",
                prompt_version="regex-v1",
            ),
        ),
    )
    wrong_trial = request("train", trial_family_id="unregistered")

    for invalid in (overlapping, changed_feature, wrong_trial):
        service = evaluator()
        with pytest.raises(ValueError):
            service.run(invalid)
        assert service.attempts[-1].accepted is False


def test_rejects_model_card_validated_on_same_untouched_test_and_second_test_run() -> None:
    leaked_card = model_card(
        status="validated",
        approved_at=NOW,
        expires_at=NOW + timedelta(days=30),
    )
    leaked = evaluator(leaked_card)
    leaked.run(request("train"))
    leaked.run(request("validation"))
    with pytest.raises(ValueError, match="untouched test"):
        leaked.run(request("test"))

    service = evaluator()
    run_through_test(service)
    with pytest.raises(ValueError, match="already evaluated"):
        service.run(request("test"))


def test_rejects_split_counts_or_families_that_do_not_match_frozen_manifest() -> None:
    bad_counts = request("train", split_counts={"train": 1, "validation": 1, "test": 0})
    bad_families = request(
        "train",
        split_event_families={
            "train": ("different-train",),
            "validation": ("family-validation",),
            "test": ("family-test",),
        },
    )

    for invalid in (bad_counts, bad_families):
        with pytest.raises(ValueError):
            evaluator().run(invalid)
