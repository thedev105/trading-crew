"""Operator presence: a two-second browser heartbeat plus native sleep and lock signals.

Presence is a kill input, not a convenience: two missed heartbeats, five seconds of silence, a
screen lock, or a monotonic jump that looks like sleep all engage the kill state immediately.
Nothing here trusts a clock or a counter supplied by the browser.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Literal, Protocol

from polytrading.predictions.pilot.models import PilotRecord, PresenceState, UtcTimestamp

HEARTBEAT_INTERVAL = timedelta(seconds=2)
MAXIMUM_MISSED_HEARTBEATS = 2
MAXIMUM_HEARTBEAT_SILENCE = timedelta(seconds=5)
# A monotonic gap larger than the heartbeat budget is a sleep, whatever the wall clock says.
MAXIMUM_MONOTONIC_GAP = MAXIMUM_HEARTBEAT_SILENCE

PresenceKillReason = Literal[
    "HEARTBEAT_MISSED",
    "HEARTBEAT_SILENT",
    "SCREEN_LOCKED",
    "SYSTEM_SLEPT",
    "PAGE_CLOSED",
]
NativeSignal = Literal["SCREEN_LOCKED", "SCREEN_UNLOCKED", "SYSTEM_SLEPT", "SYSTEM_WOKE"]


class PresenceDecision(PilotRecord):
    state: PresenceState
    kill_reason: PresenceKillReason | None
    observed_at: UtcTimestamp


class NativePresenceSource(Protocol):
    """The operating-system half of presence, injected so tests never lock a real machine."""

    def drain(self) -> tuple[NativeSignal, ...]: ...

    def monotonic_ns(self) -> int: ...


class PresenceMonitor:
    """Derives one presence verdict from browser heartbeats and native signals."""

    def __init__(
        self,
        *,
        source: NativePresenceSource,
        started_at: datetime,
        on_kill: Callable[[PresenceKillReason], None] | None = None,
    ) -> None:
        self._source = source
        self._last_heartbeat = started_at
        self._last_monotonic = source.monotonic_ns()
        self._state = PresenceState.PRESENT
        self._kill_reason: PresenceKillReason | None = None
        self._on_kill = on_kill

    @property
    def state(self) -> PresenceState:
        return self._state

    @property
    def kill_reason(self) -> PresenceKillReason | None:
        return self._kill_reason

    def record_browser_heartbeat(self, now: datetime) -> PresenceDecision:
        """Accept one heartbeat; a late one is itself the evidence that presence was lost."""

        if self._state is PresenceState.TERMINAL:
            return self._decision(now)
        silence = now - self._last_heartbeat
        if silence >= MAXIMUM_HEARTBEAT_SILENCE:
            return self._kill("HEARTBEAT_SILENT", now)
        if silence > HEARTBEAT_INTERVAL * MAXIMUM_MISSED_HEARTBEATS:
            return self._kill("HEARTBEAT_MISSED", now)
        self._last_heartbeat = now
        return self._decision(now)

    def record_native_state(self, now: datetime) -> PresenceDecision:
        """Drain native signals and classify a monotonic jump as sleep before anything else."""

        if self._state is PresenceState.TERMINAL:
            return self._decision(now)
        observed = self._source.monotonic_ns()
        gap = timedelta(microseconds=(observed - self._last_monotonic) / 1000)
        self._last_monotonic = observed
        if gap > MAXIMUM_MONOTONIC_GAP:
            return self._kill("SYSTEM_SLEPT", now)
        for signal in self._source.drain():
            if signal == "SCREEN_LOCKED":
                return self._kill("SCREEN_LOCKED", now)
            if signal == "SYSTEM_SLEPT":
                return self._kill("SYSTEM_SLEPT", now)
        return self._decision(now)

    def record_page_closed(self, now: datetime) -> PresenceDecision:
        return self._kill("PAGE_CLOSED", now)

    def evaluate(self, now: datetime) -> PresenceDecision:
        """Verdict without a heartbeat: silence alone ends presence."""

        if self._state is PresenceState.TERMINAL:
            return self._decision(now)
        if now - self._last_heartbeat >= MAXIMUM_HEARTBEAT_SILENCE:
            return self._kill("HEARTBEAT_SILENT", now)
        if now - self._last_heartbeat > HEARTBEAT_INTERVAL * MAXIMUM_MISSED_HEARTBEATS:
            return self._kill("HEARTBEAT_MISSED", now)
        return self._decision(now)

    def _kill(self, reason: PresenceKillReason, now: datetime) -> PresenceDecision:
        self._state = PresenceState.TERMINAL
        self._kill_reason = reason
        if self._on_kill is not None:
            self._on_kill(reason)
        return self._decision(now)

    def _decision(self, now: datetime) -> PresenceDecision:
        return PresenceDecision(state=self._state, kill_reason=self._kill_reason, observed_at=now)


__all__ = [
    "HEARTBEAT_INTERVAL",
    "MAXIMUM_HEARTBEAT_SILENCE",
    "MAXIMUM_MISSED_HEARTBEATS",
    "NativePresenceSource",
    "NativeSignal",
    "PresenceDecision",
    "PresenceKillReason",
    "PresenceMonitor",
]
