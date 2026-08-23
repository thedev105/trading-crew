import json
from pathlib import Path

from polytrading.predictions.domain import PredictionVenue
from polytrading.predictions.health import PredictionHealthAuditor
from polytrading.predictions.health_report import (
    render_prediction_health_json,
    render_prediction_health_text,
)
from polytrading.predictions.manifest import AdapterImplementationState
from polytrading.predictions.storage.store import PredictionMarketStore
from tests.predictions.domain_helpers import NOW
from tests.predictions.manifest_helpers import venue_manifest


def _report(tmp_path: Path):
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    store.append_venue_manifest(
        venue_manifest(
            venue=PredictionVenue.POLYMARKET,
            implementation_state=AdapterImplementationState.READ_ONLY,
        )
    )
    return PredictionHealthAuditor(store).audit(NOW)


def test_json_renderer_is_canonical_sorted_and_byte_stable(tmp_path: Path) -> None:
    report = _report(tmp_path)
    first = render_prediction_health_json(report)
    second = render_prediction_health_json(report)
    assert first == second
    parsed = json.loads(first)
    assert parsed["as_of"] == "2026-08-15T12:00:00Z"
    assert {venue["venue"] for venue in parsed["venues"]} == {
        "polymarket",
        "kalshi",
        "limitless",
    }


def test_text_renderer_includes_every_venue_and_all_warnings(tmp_path: Path) -> None:
    report = _report(tmp_path)
    text = render_prediction_health_text(report)
    assert "polymarket" in text
    assert "kalshi" in text
    assert "limitless" in text
    for warning in report.warnings:
        assert warning in text


def test_renderers_never_claim_approval_or_returns(tmp_path: Path) -> None:
    report = _report(tmp_path)
    combined = render_prediction_health_json(report) + render_prediction_health_text(report)
    lowered = combined.casefold()
    assert "approved" not in lowered
    assert "guaranteed" not in lowered
    assert "profit" not in lowered
