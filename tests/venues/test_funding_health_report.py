from __future__ import annotations

import json
from datetime import datetime, timedelta

from polytrading.venues.funding_cycle_models import FundingCollectionCycle, FundingCycleStatus
from polytrading.venues.funding_health import FundingCollectionHealthAuditor
from polytrading.venues.funding_health_models import (
    FUNDING_HEALTH_WARNINGS,
    FundingCollectionHealthReport,
)
from polytrading.venues.funding_health_report import (
    render_funding_health_json,
    render_funding_health_text,
)
from tests.venues.funding_health_helpers import (
    HEALTH_AS_OF,
    LATEST_BOUNDARY,
    funding_cycle,
)


class History:
    def __init__(self) -> None:
        first = LATEST_BOUNDARY - timedelta(hours=2)
        self.cycles = (
            funding_cycle(first, FundingCycleStatus.COMPLETE, cycle_int=1),
            funding_cycle(first + timedelta(hours=1), FundingCycleStatus.DEGRADED, cycle_int=2),
            funding_cycle(LATEST_BOUNDARY, FundingCycleStatus.LATE, cycle_int=3),
        )

    def funding_collection_cycles_between(
        self, _start: datetime, _end: datetime
    ) -> tuple[FundingCollectionCycle, ...]:
        return self.cycles


def mixed_report() -> FundingCollectionHealthReport:
    return FundingCollectionHealthAuditor(History()).audit(HEALTH_AS_OF, 3)


def test_json_is_byte_stable_canonical_and_preserves_exact_types() -> None:
    report = mixed_report()

    rendered = render_funding_health_json(report)
    payload = json.loads(rendered)

    assert rendered == render_funding_health_json(report)
    assert rendered == json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    assert payload["as_of"] == "2026-08-14T17:06:00Z"
    assert payload["latest_auditable_boundary"] == "2026-08-14T17:00:00Z"
    assert payload["status"] == "critical"
    assert payload["complete_coverage"] == "0.3333333333333333333333333333"
    assert [item["cycle_end"] for item in payload["boundaries"]] == [
        "2026-08-14T15:00:00Z",
        "2026-08-14T16:00:00Z",
        "2026-08-14T17:00:00Z",
    ]
    assert [item["status"] for item in payload["boundaries"]] == [
        "complete",
        "degraded",
        "late",
    ]
    assert payload["boundaries"][0]["selected_cycle_id"] == ("00000000-0000-0000-0000-000000000001")
    assert payload["warnings"] == list(FUNDING_HEALTH_WARNINGS)


def test_text_is_stable_explicit_and_ends_with_exact_warnings() -> None:
    rendered = render_funding_health_text(mixed_report())

    assert rendered == render_funding_health_text(mixed_report())
    assert rendered.splitlines()[:3] == [
        "Funding collection health v1 | 2026-08-14T17:06:00Z | critical",
        "Boundaries: 2026-08-14T15:00:00Z..2026-08-14T17:00:00Z | hours=3",
        ("Coverage: 1/3 (0.3333333333333333333333333333) | current_complete_streak=0"),
    ]
    assert rendered.splitlines()[3:6] == [
        (
            "2026-08-14T15:00:00Z | complete | attempts=1 | "
            "complete/degraded/late=1/0/0 | "
            "selected=00000000-0000-0000-0000-000000000001 | reasons=none"
        ),
        (
            "2026-08-14T16:00:00Z | degraded | attempts=1 | "
            "complete/degraded/late=0/1/0 | "
            "selected=00000000-0000-0000-0000-000000000002 | "
            "reasons=BOUNDARY_DEGRADED_ONLY"
        ),
        (
            "2026-08-14T17:00:00Z | late | attempts=1 | "
            "complete/degraded/late=0/0/1 | "
            "selected=00000000-0000-0000-0000-000000000003 | "
            "reasons=BOUNDARY_LATE_ONLY"
        ),
    ]
    assert rendered.endswith("\n".join(FUNDING_HEALTH_WARNINGS))


def test_outputs_do_not_claim_strategy_or_execution_authority() -> None:
    outputs = (
        render_funding_health_json(mixed_report()),
        render_funding_health_text(mixed_report()),
    )

    for rendered in outputs:
        lowered = rendered.casefold()
        for forbidden in (
            "recommended",
            "expected profit",
            "live_eligible",
            "api key",
            "private key",
            "place order",
        ):
            assert forbidden not in lowered
