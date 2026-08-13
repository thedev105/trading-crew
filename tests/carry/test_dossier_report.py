import json

from polytrading.carry.dossier import evaluate_dossier, load_bundled_dossier
from polytrading.carry.dossier_report import render_dossier_json, render_dossier_text


def test_dossier_json_is_deterministic_and_preserves_complete_evidence() -> None:
    report = evaluate_dossier(load_bundled_dossier())

    first = render_dossier_json(report)
    second = render_dossier_json(report)
    document = json.loads(first)

    assert first == second
    assert first == json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True)
    assert document["observed_at"] == "2026-08-13T12:00:00Z"
    assert document["status"] == "ineligible"
    assert document["primary_reason_code"] == "quanto_structure_excluded"
    assert len(document["sources"]) == 13
    assert len(document["checks"]) == 14


def test_dossier_text_names_primary_blocker_and_every_check() -> None:
    rendered = render_dossier_text(evaluate_dossier(load_bundled_dossier()))
    lines = rendered.splitlines()

    assert lines[:3] == [
        "RESEARCH ONLY — NOT A TRADE RECOMMENDATION",
        "status=ineligible | pair=hyperliquid->dydx | assets=BTC,ETH,SOL",
        "primary_blocker=quanto_structure_excluded",
    ]
    check_lines = [line for line in lines if line.startswith("check=")]
    assert len(check_lines) == 14
    assert check_lines[1].startswith(
        "check=payoff_and_quote | judgment=blocking | reason=quanto_structure_excluded"
    )
    assert "technically quanto" in check_lines[1]
    assert lines[-1] == "No cost model or trading authority exists."
