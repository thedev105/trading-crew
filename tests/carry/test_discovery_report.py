import json

from polytrading.carry.discovery import evaluate_discovery
from polytrading.carry.discovery_report import render_discovery_json, render_discovery_text
from polytrading.carry.dossier import (
    evaluate_dossier,
    load_bundled_dossiers,
)


def _bundled_discovery():
    return evaluate_discovery(tuple(evaluate_dossier(item) for item in load_bundled_dossiers()))


def test_discovery_json_is_deterministic_and_preserves_complete_reports() -> None:
    report = _bundled_discovery()

    first = render_discovery_json(report)
    second = render_discovery_json(report)
    document = json.loads(first)

    assert first == second
    assert first == json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True)
    assert document["observed_at"] == "2026-08-13T16:23:08Z"
    assert document["selected_dossier_id"] == "lighter-dydx-core-v1"
    assert document["counts"] == {
        "compatible": 0,
        "evidence_incomplete": 0,
        "ineligible": 1,
        "model_required": 1,
    }
    assert [item["dossier_id"] for item in document["candidates"]] == [
        "lighter-dydx-core-v1",
        "hyperliquid-dydx-core-v1",
    ]
    assert all(len(item["checks"]) == 14 for item in document["candidates"])


def test_discovery_text_names_selection_and_every_ranked_candidate() -> None:
    rendered = render_discovery_text(_bundled_discovery())
    lines = rendered.splitlines()

    assert lines[:2] == [
        "RESEARCH ONLY — NOT A TRADE RECOMMENDATION",
        (
            "selected=lighter-dydx-core-v1 | "
            "reason=best_nonblocking_complete_evidence | activation=not_authorized"
        ),
    ]
    candidate_lines = [line for line in lines if line.startswith("rank=")]
    assert len(candidate_lines) == 2
    assert "rank=1 | dossier=lighter-dydx-core-v1 | pair=lighter->dydx" in candidate_lines[0]
    assert "status=model_required" in candidate_lines[0]
    assert "blocking=0 | missing_evidence=0" in candidate_lines[0]
    assert "rank=2 | dossier=hyperliquid-dydx-core-v1" in candidate_lines[1]
    assert "primary=quanto_structure_excluded" in candidate_lines[1]
    assert lines[-1] == (
        "Next gate: collect public Lighter evidence and model costs; no trading authority exists."
    )
