from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from polytrading.predictions.execution.kill_switch import (
    ExecutionAuthorityError,
    KillState,
    derive_kill_state,
    require_mutation_allowed,
)
from polytrading.predictions.execution.models import KillSwitchEvent

NOW = datetime(2026, 8, 25, 16, tzinfo=UTC)


def kill_event(*, event_id: str, occurred_at: datetime, prior_state: bool) -> KillSwitchEvent:
    return KillSwitchEvent.model_validate(
        {
            "schema_version": 1,
            "kill_event_id": UUID(event_id),
            "trigger": "operator-request",
            "scope": "account:" + "a" * 64,
            "source_intent_id": None,
            "source_order_id": None,
            "prior_state": prior_state,
            "occurred_at": occurred_at,
            "clearance_evidence_hashes": (),
        }
    )


def test_production_without_events_starts_killed() -> None:
    assert derive_kill_state((), production=True) == KillState(engaged=True, latest_event=None)


def test_nonproduction_derivation_does_not_create_a_clearance_path() -> None:
    assert derive_kill_state((), production=False) == KillState(engaged=True, latest_event=None)


def test_latest_append_only_event_is_retained_and_state_remains_engaged() -> None:
    first = kill_event(
        event_id="11111111-1111-4111-8111-111111111111",
        occurred_at=NOW,
        prior_state=False,
    )
    latest = kill_event(
        event_id="22222222-2222-4222-8222-222222222222",
        occurred_at=NOW + timedelta(seconds=1),
        prior_state=True,
    )
    assert derive_kill_state((first, latest), production=True) == KillState(True, latest)


@pytest.mark.parametrize(
    "events",
    [
        (
            kill_event(
                event_id="22222222-2222-4222-8222-222222222222",
                occurred_at=NOW + timedelta(seconds=1),
                prior_state=True,
            ),
            kill_event(
                event_id="11111111-1111-4111-8111-111111111111",
                occurred_at=NOW,
                prior_state=False,
            ),
        ),
        (
            kill_event(
                event_id="11111111-1111-4111-8111-111111111111",
                occurred_at=NOW,
                prior_state=False,
            ),
            kill_event(
                event_id="11111111-1111-4111-8111-111111111111",
                occurred_at=NOW,
                prior_state=False,
            ),
        ),
    ],
)
def test_malformed_kill_history_fails_closed(events: tuple[KillSwitchEvent, ...]) -> None:
    with pytest.raises(ExecutionAuthorityError, match=r"^KILL_EVENT_HISTORY_INVALID$"):
        derive_kill_state(events, production=True)


def test_engaged_kill_raises_only_a_stable_sanitized_code() -> None:
    with pytest.raises(ExecutionAuthorityError) as raised:
        require_mutation_allowed(KillState(engaged=True, latest_event=None))
    assert str(raised.value) == "EXECUTION_KILL_ENGAGED"


def test_explicit_test_fixture_clear_state_allows_mutation() -> None:
    require_mutation_allowed(KillState(engaged=False, latest_event=None))
