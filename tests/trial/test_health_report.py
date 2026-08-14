import json
from pathlib import Path

from polytrading.trial.health import LighterDydxTrialHealthAuditor
from polytrading.trial.health_models import TRIAL_HEALTH_WARNINGS
from polytrading.trial.health_report import render_trial_health_json, render_trial_health_text
from tests.trial.test_health import AS_OF, seed_complete_trial_hours


def test_health_json_is_canonical_and_preserves_model_order(tmp_path: Path) -> None:
    store = seed_complete_trial_hours(tmp_path, hours=2)
    report = LighterDydxTrialHealthAuditor(store).audit(AS_OF, 2)
    rendered = render_trial_health_json(report)
    document = json.loads(rendered)

    assert rendered == json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True)
    assert document["as_of"] == "2026-08-14T07:06:00Z"
    assert document["assets"][0]["training_funding_coverage"] == "0"
    assert [item["asset"] for item in document["assets"]] == ["BTC", "ETH", "SOL"]
    assert [item["cycle_end"] for item in document["recent_boundaries"]] == [
        "2026-08-14T06:00:00Z",
        "2026-08-14T07:00:00Z",
    ]
    assert document["warnings"] == list(TRIAL_HEALTH_WARNINGS)
    assert [item["asset"] for item in document["economics"]] == ["BTC", "ETH", "SOL"]
    assert all(item["available"] is False for item in document["economics"])
    assert document["recent_boundaries"][0]["failed_book_attempt_count"] == 0
    assert document["recent_boundaries"][0]["skewed_book_attempt_count"] == 0
    store.close()


def test_health_text_is_complete_research_only_and_non_authorizing(tmp_path: Path) -> None:
    store = seed_complete_trial_hours(tmp_path, hours=2)
    report = LighterDydxTrialHealthAuditor(store).audit(AS_OF, 2)
    rendered = render_trial_health_text(report)

    assert "status: COLLECTING" in rendered
    assert "cutoff: 2026-08-14T07:06:00Z" in rendered
    assert "trial start: 2026-08-14T06:00:00Z" in rendered
    assert "elapsed/target hours: 2/2160" in rendered
    assert "collection-only projection assuming complete future boundaries" in rendered
    for asset in ("BTC", "ETH", "SOL"):
        assert f"{asset} coverage: training=" in rendered
        assert f"{asset} current 168:" in rendered
        assert f"{asset} depth:" in rendered
    assert "recent gaps: none" in rendered
    assert "dossier evidence:" in rendered
    assert "fee evidence:" in rendered
    assert "operator policy: not assessed" in rendered
    assert "BTC economics: unavailable" in rendered
    assert "failed_books=0 skewed_books=0" in rendered
    for warning in TRIAL_HEALTH_WARNINGS:
        assert rendered.count(warning) == 1
    lowered = rendered.lower()
    for forbidden in ("place orders", "open positions", "recommended fee tier"):
        assert forbidden not in lowered
    store.close()
