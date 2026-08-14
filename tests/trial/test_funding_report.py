from __future__ import annotations

import json

from polytrading.trial.funding_models import TRIAL_FUNDING_WARNINGS
from polytrading.trial.funding_report import (
    render_trial_funding_json,
    render_trial_funding_text,
)
from tests.trial.funding_helpers import CYCLE_ID, trial_funding_cycle


def test_trial_funding_json_is_canonical_and_machine_readable() -> None:
    cycle = trial_funding_cycle()

    rendered = render_trial_funding_json(cycle)
    payload = json.loads(rendered)

    assert rendered == json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    assert payload["cycle_id"] == str(CYCLE_ID)
    assert payload["cycle_end"] == "2026-08-14T07:00:00Z"
    assert payload["request_started_at"] == "2026-08-14T07:00:10Z"
    assert payload["request_completed_at"] == "2026-08-14T07:00:20Z"
    assert [(item["venue"], item["asset"]) for item in payload["items"]] == [
        ("dydx", "BTC"),
        ("dydx", "ETH"),
        ("dydx", "SOL"),
        ("lighter", "BTC"),
        ("lighter", "ETH"),
        ("lighter", "SOL"),
    ]
    assert tuple(payload["warnings"]) == TRIAL_FUNDING_WARNINGS


def test_trial_funding_text_has_stable_boundary_attempt_items_and_exact_warnings() -> None:
    rendered = render_trial_funding_text(trial_funding_cycle())
    lines = rendered.splitlines()

    assert lines[:2] == [
        "Lighter-dYdX prospective funding cycle v1 | "
        "boundary=2026-08-14T07:00:00Z | status=complete",
        "Attempt: started=2026-08-14T07:00:10Z | completed=2026-08-14T07:00:20Z",
    ]
    assert lines[2:8] == [
        f"{venue} {asset} | instrument=captured | funding=captured | reasons=none"
        for venue in ("dydx", "lighter")
        for asset in ("BTC", "ETH", "SOL")
    ]
    assert lines[-4:] == ["", *TRIAL_FUNDING_WARNINGS]


def test_trial_funding_renderers_make_no_execution_or_return_claim() -> None:
    rendered = render_trial_funding_text(trial_funding_cycle()) + render_trial_funding_json(
        trial_funding_cycle()
    )

    for prohibited in ("TRADE", "APPROVED", "LIVE_ELIGIBLE"):
        assert prohibited not in rendered
    for claim in ("guaranteed return", "expected profit", "profitable"):
        assert claim not in rendered.lower()
