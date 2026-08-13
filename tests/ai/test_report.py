import json

import pytest

from polytrading.ai.evaluate import SemanticEvaluator
from polytrading.ai.report import (
    build_semantic_report,
    render_report_json,
    render_report_markdown,
)
from tests.ai.test_evaluate import evaluator, experiment, request, run_through_test


def completed_evaluation():
    service: SemanticEvaluator = evaluator()
    return run_through_test(service)


def test_report_is_canonical_candid_and_byte_stable() -> None:
    evaluation = completed_evaluation()
    test_request = request("test")
    report = build_semantic_report(
        evaluation,
        test_request,
        experiment(),
        evaluator().model_cards,
    )

    first_json = render_report_json(report)
    second_json = render_report_json(report)
    first_markdown = render_report_markdown(report)
    second_markdown = render_report_markdown(report)
    parsed = json.loads(first_json)

    assert first_json == second_json
    assert first_markdown == second_markdown
    assert parsed["overall_status"] == "RESEARCH_ONLY_NOT_PROMOTABLE"
    assert parsed["semantic_gate_status"] == "BLOCKED_BY_DEPENDENCY"
    assert parsed["class_g_false_eligibility"] == {
        "dependency": "deterministic payoff compiler and graph",
        "status": "BLOCKED_BY_DEPENDENCY",
    }
    assert parsed["inference_cost_usd"] == "0"
    assert parsed["hashes"]["manifest_hash"] == "a" * 64
    assert parsed["hashes"]["code_hash"]
    assert parsed["hashes"]["experiment_hash"]
    assert parsed["hashes"]["model_hashes"]
    assert parsed["hashes"]["feature_hashes"]
    assert parsed["hashes"]["prompt_hashes"]
    assert parsed["corpus"]["counts"]["contracts"] == 3
    assert parsed["corpus"]["split_counts"] == {
        "test": 1,
        "train": 1,
        "validation": 1,
    }
    assert all("numerator" in metric and "denominator" in metric for metric in parsed["metrics"])
    assert "RESEARCH_ONLY_NOT_PROMOTABLE" in first_markdown
    assert "deterministic payoff compiler and graph" in first_markdown
    assert "rule-regex-baseline@1.0.0" in first_markdown
    assert "binary_threshold" in first_markdown
    assert "prompt_injection" in first_markdown
    assert "malformed-1" in first_markdown
    assert "hostile-1" in first_markdown
    assert "mutation:operator" in first_markdown
    lowered = (first_json + first_markdown).casefold()
    assert "projected profit" not in lowered
    assert "promotion recommendation" not in lowered


def test_report_does_not_hide_failure_ids_or_abstentions() -> None:
    service = evaluator()
    service.run(request("train"))
    service.run(request("validation"))
    base = request("test")
    field_case = base.field_cases[0]
    unknown = field_case.actual_fields.model_copy(
        update={
            name: value.model_copy(
                update={"status": "unknown", "value": None, "supporting_spans": ()}
            )
            for name, value in field_case.actual_fields
        }
    )
    failed_request = request(
        "test",
        field_cases=(field_case.model_copy(update={"actual_fields": unknown}),),
    )
    evaluation = service.run(failed_request)
    report = build_semantic_report(evaluation, failed_request, experiment(), service.model_cards)
    parsed = json.loads(render_report_json(report))

    assert any(
        failure["item_id"] == "test-contract" and failure["abstained"] is True
        for failure in parsed["failure_examples"]
    )


def test_report_rejects_mismatched_request_or_experiment() -> None:
    evaluation = completed_evaluation()

    with pytest.raises(ValueError, match="request"):
        build_semantic_report(
            evaluation,
            request("validation"),
            experiment(),
            evaluator().model_cards,
        )
