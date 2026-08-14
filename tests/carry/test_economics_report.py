import json
import re

from polytrading.carry.economics_models import RESEARCH_WARNING, CandidateEconomicsReport
from polytrading.carry.economics_report import render_economics_json, render_economics_text
from tests.carry.test_economics import evaluate_bundle, passing_bundle


def test_canonical_json_round_trips_with_strings_sorted_keys_and_utc_z() -> None:
    report = evaluate_bundle(passing_bundle())

    rendered = render_economics_json(report)
    parsed = CandidateEconomicsReport.model_validate_json(rendered)

    assert parsed == report
    assert rendered == render_economics_json(report)
    assert rendered.startswith('{"asset":')
    assert '"assigned_capital_usd":"' in rendered
    assert '"evaluated_at":"2026-08-13T17:00:07Z"' in rendered
    assert json.loads(rendered)["reason_codes"] == []


def test_text_contains_all_audit_sections_without_action_copy() -> None:
    report = evaluate_bundle(passing_bundle())

    rendered = render_economics_text(report)

    assert RESEARCH_WARNING in rendered
    for expected in (
        "Decision: SHADOW_CANDIDATE",
        "Direction: short_lighter_long_dydx",
        "Coverage:",
        "Assigned capital:",
        "Entry slippage:",
        "Forced exit cost:",
        "Taker fees:",
        "Operational cost:",
        "Latency reserve:",
        "7 days:",
        "14 days:",
        "28 days:",
        "Stress loss:",
        "Modeled drawdown:",
        "Modeled liquidation:",
        "Reasons: none",
    ):
        assert expected in rendered
    prohibited = re.compile(
        r"\b(buy|sell|enter|execute|guaranteed|expected profit)\b", re.IGNORECASE
    )
    assert prohibited.search(rendered) is None


def test_text_sorts_reasons_and_withholds_unavailable_economics() -> None:
    report = evaluate_bundle(passing_bundle()).model_copy(
        update={
            "decision": "INSUFFICIENT_EVIDENCE",
            "reason_codes": ("Z_MISSING", "A_MISSING"),
            "direction": None,
            "short_venue": None,
            "long_venue": None,
            "economics": None,
        }
    )

    rendered = render_economics_text(report)

    assert "Direction: unavailable" in rendered
    assert "Assigned capital: unavailable" in rendered
    assert "Reasons: A_MISSING, Z_MISSING" in rendered
