"""Append-only execution kill state and its one reviewed clearance input.

Clearance is never automatic and never derived here: the pilot's operator ceremony produces an
append-only clearance event, and this module only replays it. An empty production history stays
killed, and a clearance older than the newest kill clears nothing.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from polytrading.predictions.execution.models import KillSwitchEvent


class KillClearance(Protocol):
    """The shape of one reviewed clearance event, without importing the pilot package."""

    @property
    def kill_event_id(self) -> UUID: ...

    @property
    def occurred_at(self) -> datetime: ...

    @property
    def result(self) -> object: ...


class ExecutionAuthorityError(RuntimeError):
    """A sanitized, stable authority rejection safe for boundary responses."""


@dataclass(frozen=True)
class KillState:
    engaged: bool
    latest_event: KillSwitchEvent | None


def _validate_chronological_unique_events(events: Sequence[KillSwitchEvent]) -> None:
    event_ids = tuple(event.kill_event_id for event in events)
    ordering = tuple((event.occurred_at, event.kill_event_id) for event in events)
    if len(event_ids) != len(set(event_ids)) or ordering != tuple(sorted(ordering)):
        raise ExecutionAuthorityError("KILL_EVENT_HISTORY_INVALID")


def derive_kill_state(
    events: Sequence[KillSwitchEvent],
    *,
    production: bool,
    clearances: Sequence[KillClearance] = (),
) -> KillState:
    """Derive an engaged state from append-only events; an empty production state is killed."""

    if production and not events:
        return KillState(engaged=True, latest_event=None)
    _validate_chronological_unique_events(events)
    latest_event = events[-1] if events else None
    if latest_event is None or not clearances:
        return KillState(engaged=True, latest_event=latest_event)
    cleared = [
        clearance
        for clearance in clearances
        if clearance.kill_event_id == latest_event.kill_event_id
        and clearance.occurred_at >= latest_event.occurred_at
        and getattr(clearance.result, "value", clearance.result) == "CLEARED"
    ]
    return KillState(engaged=not cleared, latest_event=latest_event)


def require_mutation_allowed(state: KillState) -> None:
    if state.engaged:
        raise ExecutionAuthorityError("EXECUTION_KILL_ENGAGED")
