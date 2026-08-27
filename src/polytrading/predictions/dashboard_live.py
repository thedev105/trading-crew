"""Bounded metadata-only notifications for immutable prediction dashboards."""

from __future__ import annotations

import json
import re
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime
from hashlib import sha256
from threading import Condition, Event, Lock, Thread, current_thread
from typing import Annotated, Literal

from pydantic import Field, field_validator

from polytrading.predictions.dashboard_models import (
    DashboardDomain,
    PredictionDashboardSnapshot,
)
from polytrading.predictions.domain import PredictionRecord, Sha256, normalize_utc_timestamp

EventId = Annotated[str, Field(pattern=r"^(0|[1-9][0-9]*)$")]
_EVENT_ID = re.compile(r"^(0|[1-9][0-9]*)$")
_PRODUCTION_CAPACITY = 128
_DOMAIN_FIELDS: dict[DashboardDomain, tuple[str, ...]] = {
    DashboardDomain.OVERVIEW: ("health", "recipes"),
    DashboardDomain.MARKETS: (
        "markets",
        "books",
        "candidates",
        "proofs",
        "scans",
        "opportunities",
    ),
    DashboardDomain.EXECUTION: ("execution_readiness", "execution_timeline"),
    DashboardDomain.LEDGER: ("shadow", "live_ledger"),
    DashboardDomain.EVIDENCE: ("evidence_counts", "evidence_status"),
}


class DashboardRevision(PredictionRecord):
    schema_version: Literal[1]
    event_id: EventId
    revision_id: Sha256
    as_of: datetime
    emitted_at: datetime
    changed_domains: tuple[DashboardDomain, ...]

    @field_validator("emitted_at")
    @classmethod
    def _emitted_at_is_utc(cls, value: datetime) -> datetime:
        return normalize_utc_timestamp(value)

    @field_validator("changed_domains")
    @classmethod
    def _domains_are_ordered_unique(
        cls, value: tuple[DashboardDomain, ...]
    ) -> tuple[DashboardDomain, ...]:
        expected = tuple(domain for domain in DashboardDomain if domain in value)
        if value != expected:
            raise ValueError("changed_domains must be sorted and unique")
        return value

    @classmethod
    def from_snapshots(
        cls,
        previous: PredictionDashboardSnapshot | None,
        current: PredictionDashboardSnapshot,
        *,
        emitted_at: datetime,
        event_id: str = "0",
    ) -> DashboardRevision:
        domains = (
            tuple(DashboardDomain)
            if previous is None
            else changed_dashboard_domains(previous, current)
        )
        return cls(
            schema_version=1,
            event_id=event_id,
            revision_id=current.revision_id,
            as_of=current.as_of,
            emitted_at=emitted_at,
            changed_domains=domains,
        )


class DashboardReset(PredictionRecord):
    schema_version: Literal[1]
    event_id: EventId
    latest_revision_id: Sha256
    emitted_at: datetime
    reason: Literal["CURSOR_NOT_AVAILABLE"]

    @field_validator("emitted_at")
    @classmethod
    def _emitted_at_is_utc(cls, value: datetime) -> datetime:
        return normalize_utc_timestamp(value)


DashboardEvent = DashboardRevision | DashboardReset


def deterministic_dashboard_revision(snapshot: PredictionDashboardSnapshot) -> Sha256:
    canonical = json.dumps(
        snapshot.model_dump(mode="json", exclude={"revision_id"}),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return sha256(canonical).hexdigest()


def changed_dashboard_domains(
    previous: PredictionDashboardSnapshot,
    current: PredictionDashboardSnapshot,
) -> tuple[DashboardDomain, ...]:
    previous_fingerprints = _domain_fingerprints(previous)
    current_fingerprints = _domain_fingerprints(current)
    return tuple(
        domain
        for domain in DashboardDomain
        if previous_fingerprints[domain] != current_fingerprints[domain]
    )


def _domain_fingerprints(
    snapshot: PredictionDashboardSnapshot,
) -> dict[DashboardDomain, Sha256]:
    document = snapshot.model_dump(mode="json")
    fingerprints = {}
    for domain, fields in _DOMAIN_FIELDS.items():
        projection = {field: _without_cutoff(document[field]) for field in fields}
        canonical = json.dumps(
            projection,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        fingerprints[domain] = sha256(canonical).hexdigest()
    return fingerprints


def _without_cutoff(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: _without_cutoff(item)
            for key, item in value.items()
            if key not in {"as_of", "revision_id"}
        }
    if isinstance(value, list):
        return [_without_cutoff(item) for item in value]
    return value


class DashboardRevisionBuffer:
    """Fixed-capacity cursor buffer that retains revision metadata, never snapshots."""

    def __init__(
        self,
        capacity: int = _PRODUCTION_CAPACITY,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if type(capacity) is not int or capacity < 1:
            raise ValueError("capacity must be a positive integer")
        self._capacity = capacity
        self._clock = clock or (lambda: datetime.now(UTC))
        self._events: deque[DashboardRevision] = deque(maxlen=capacity)
        self._condition = Condition(Lock())
        self._next_event_id = 1
        self._latest_revision_id: str | None = None
        self._latest_domain_fingerprints: dict[DashboardDomain, Sha256] | None = None
        self._closed = False

    @property
    def buffered_count(self) -> int:
        with self._condition:
            return len(self._events)

    @property
    def closed(self) -> bool:
        with self._condition:
            return self._closed

    def publish(
        self,
        snapshot: PredictionDashboardSnapshot,
        *,
        emitted_at: datetime | None = None,
    ) -> DashboardRevision | None:
        fingerprints = _domain_fingerprints(snapshot)
        with self._condition:
            if self._closed:
                raise RuntimeError("dashboard revision buffer is closed")
            if snapshot.revision_id == self._latest_revision_id:
                return None
            domains = (
                tuple(DashboardDomain)
                if self._latest_domain_fingerprints is None
                else tuple(
                    domain
                    for domain in DashboardDomain
                    if self._latest_domain_fingerprints[domain] != fingerprints[domain]
                )
            )
            revision = DashboardRevision(
                schema_version=1,
                event_id=str(self._next_event_id),
                revision_id=snapshot.revision_id,
                as_of=snapshot.as_of,
                emitted_at=normalize_utc_timestamp(emitted_at or self._clock()),
                changed_domains=domains,
            )
            self._events.append(revision)
            self._next_event_id += 1
            self._latest_revision_id = snapshot.revision_id
            self._latest_domain_fingerprints = dict(fingerprints)
            self._condition.notify_all()
            return revision

    def event_after(self, last_event_id: str | None) -> DashboardEvent | None:
        with self._condition:
            return self._event_after_locked(last_event_id)

    def _event_after_locked(self, last_event_id: str | None) -> DashboardEvent | None:
        if last_event_id is not None and _EVENT_ID.fullmatch(last_event_id) is None:
            raise ValueError("Last-Event-ID must be a decimal event ID")
        if not self._events:
            return None
        latest = self._events[-1]
        if last_event_id is None:
            return latest
        if last_event_id == latest.event_id:
            return None
        if any(event.event_id == last_event_id for event in self._events):
            return latest
        return DashboardReset(
            schema_version=1,
            event_id=latest.event_id,
            latest_revision_id=latest.revision_id,
            emitted_at=normalize_utc_timestamp(self._clock()),
            reason="CURSOR_NOT_AVAILABLE",
        )

    def wait_for_event(
        self, last_event_id: str | None, *, timeout: float | None
    ) -> DashboardEvent | None:
        if timeout is not None and timeout < 0:
            raise ValueError("timeout must be nonnegative")
        with self._condition:
            available = self._event_after_locked(last_event_id)
            if available is not None or self._closed or timeout == 0:
                return available
            self._condition.wait(timeout)
            return self._event_after_locked(last_event_id)

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()


WaitFunction = Callable[[Event, float], bool]


class DashboardRevisionPublisher:
    """Own one polling thread and publish only immutable notification metadata."""

    def __init__(
        self,
        *,
        snapshot_factory: Callable[[], PredictionDashboardSnapshot],
        revision_buffer: DashboardRevisionBuffer,
        interval_seconds: float,
        clock: Callable[[], datetime] | None = None,
        wait: WaitFunction | None = None,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self._snapshot_factory = snapshot_factory
        self._buffer = revision_buffer
        self._interval_seconds = interval_seconds
        self._clock = clock or (lambda: datetime.now(UTC))
        self._wait = wait or (lambda stop, interval: stop.wait(interval))
        self._stop = Event()
        self._lifecycle_lock = Lock()
        self._thread: Thread | None = None
        self._closed = False
        self._failure_code: str | None = None

    @property
    def running(self) -> bool:
        with self._lifecycle_lock:
            return self._thread is not None and self._thread.is_alive()

    @property
    def failure_code(self) -> str | None:
        return self._failure_code

    def poll_once(self) -> DashboardRevision | None:
        snapshot = self._snapshot_factory()
        return self._buffer.publish(snapshot, emitted_at=self._clock())

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._closed:
                raise RuntimeError("dashboard publisher is closed")
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = Thread(
                target=self._run,
                name="prediction-dashboard-publisher",
                daemon=True,
            )
            self._thread.start()

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                self.poll_once()
                if self._wait(self._stop, self._interval_seconds):
                    break
        except Exception:
            self._failure_code = "SNAPSHOT_UNAVAILABLE"
            self._stop.set()
            self._buffer.close()

    def close(self) -> None:
        with self._lifecycle_lock:
            self._closed = True
            self._stop.set()
            thread = self._thread
        if thread is not None and thread is not current_thread():
            thread.join()
        self._buffer.close()
