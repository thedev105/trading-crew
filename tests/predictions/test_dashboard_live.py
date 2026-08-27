from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from threading import Event, Thread

import pytest
from pydantic import ValidationError

from polytrading.predictions.dashboard import PredictionDashboardBuilder
from polytrading.predictions.dashboard_live import (
    DashboardReset,
    DashboardRevision,
    DashboardRevisionBuffer,
    DashboardRevisionPublisher,
    changed_dashboard_domains,
    deterministic_dashboard_revision,
)
from polytrading.predictions.dashboard_models import DashboardDomain, PredictionDashboardSnapshot
from polytrading.predictions.storage.store import PredictionMarketStore

NOW = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
ALL_DOMAINS = tuple(DashboardDomain)


@pytest.fixture
def snapshot(tmp_path: Path) -> PredictionDashboardSnapshot:
    database = tmp_path / "predictions.duckdb"
    store = PredictionMarketStore(database)
    try:
        return PredictionDashboardBuilder(store, database).build(NOW)
    finally:
        store.close()


def _revision(snapshot: PredictionDashboardSnapshot, seed: int) -> PredictionDashboardSnapshot:
    values = snapshot.model_dump(mode="python", exclude={"revision_id"})
    values["recipes"] = snapshot.recipes.model_copy(
        update={"recipes": (*snapshot.recipes.recipes, f"observer-status-{seed}")}
    )
    return PredictionDashboardSnapshot.finalize(**values)


def test_deterministic_revision_is_canonical_and_omits_revision_id(
    snapshot: PredictionDashboardSnapshot,
) -> None:
    assert deterministic_dashboard_revision(snapshot) == snapshot.revision_id
    with pytest.raises(ValidationError, match="revision"):
        snapshot.model_copy(update={"revision_id": "f" * 64})


def test_changed_domains_are_stable_sorted_and_match_additive_legacy_mapping(
    snapshot: PredictionDashboardSnapshot,
) -> None:
    changed = PredictionDashboardSnapshot.finalize(
        **{
            **snapshot.model_dump(mode="python", exclude={"revision_id"}),
            "recipes": snapshot.recipes.model_copy(
                update={"recipes": (*snapshot.recipes.recipes, "observer-status")}
            ),
            "live_ledger": snapshot.live_ledger.model_copy(update={"posting_count": 1}),
            "evidence_counts": snapshot.evidence_counts.model_copy(update={"counts": {"x": 1}}),
        }
    )
    assert changed_dashboard_domains(snapshot, changed) == (
        DashboardDomain.OVERVIEW,
        DashboardDomain.LEDGER,
        DashboardDomain.EVIDENCE,
    )


def test_dashboard_revision_is_strict_immutable_metadata_only(
    snapshot: PredictionDashboardSnapshot,
) -> None:
    current = _revision(snapshot, 2)
    revision = DashboardRevision.from_snapshots(
        snapshot, current, emitted_at=NOW + timedelta(seconds=1)
    )
    assert set(revision.model_dump()) == {
        "schema_version",
        "event_id",
        "revision_id",
        "as_of",
        "emitted_at",
        "changed_domains",
    }
    with pytest.raises(ValidationError):
        revision.event_id = "5"
    with pytest.raises(ValidationError):
        DashboardRevision.model_validate(
            {**revision.model_dump(mode="python"), "changed_domains": ["ledger"]}, strict=True
        )


def test_first_publication_marks_all_domains_and_ids_are_monotonic(
    snapshot: PredictionDashboardSnapshot,
) -> None:
    buffer = DashboardRevisionBuffer(capacity=3, clock=lambda: NOW)
    first = buffer.publish(_revision(snapshot, 7))
    second = buffer.publish(_revision(snapshot, 8))
    assert first is not None and second is not None
    assert first.event_id == "1"
    assert first.changed_domains == ALL_DOMAINS
    assert second.event_id == "2"


def test_duplicate_revision_is_suppressed_without_consuming_an_event_id(
    snapshot: PredictionDashboardSnapshot,
) -> None:
    buffer = DashboardRevisionBuffer(capacity=3, clock=lambda: NOW)
    assert buffer.publish(_revision(snapshot, 3)) is not None
    assert buffer.publish(_revision(snapshot, 3)) is None
    next_revision = buffer.publish(_revision(snapshot, 4))
    assert next_revision is not None
    assert next_revision.event_id == "2"


def test_missing_cursor_gets_newest_revision_and_known_cursor_resumes_after_it(
    snapshot: PredictionDashboardSnapshot,
) -> None:
    buffer = DashboardRevisionBuffer(capacity=3, clock=lambda: NOW)
    first = buffer.publish(_revision(snapshot, 1))
    newest = buffer.publish(_revision(snapshot, 2))
    assert first is not None and newest is not None
    assert buffer.event_after(None) == newest
    assert buffer.event_after(first.event_id) == newest
    assert buffer.event_after(newest.event_id) is None


def test_unknown_or_evicted_cursor_gets_reset_to_latest_identity(
    snapshot: PredictionDashboardSnapshot,
) -> None:
    buffer = DashboardRevisionBuffer(capacity=2, clock=lambda: NOW)
    evicted = buffer.publish(_revision(snapshot, 1))
    buffer.publish(_revision(snapshot, 2))
    latest = buffer.publish(_revision(snapshot, 3))
    assert evicted is not None and latest is not None
    for cursor in (evicted.event_id, "999"):
        reset = buffer.event_after(cursor)
        assert isinstance(reset, DashboardReset)
        assert reset.event_id == latest.event_id
        assert reset.latest_revision_id == latest.revision_id
        assert reset.reason == "CURSOR_NOT_AVAILABLE"


def test_slow_subscriber_is_coalesced_to_newest_not_given_an_unbounded_backlog(
    snapshot: PredictionDashboardSnapshot,
) -> None:
    buffer = DashboardRevisionBuffer(capacity=8, clock=lambda: NOW)
    first = buffer.publish(_revision(snapshot, 1))
    for seed in range(2, 8):
        latest = buffer.publish(_revision(snapshot, seed))
    assert first is not None and latest is not None
    assert buffer.event_after(first.event_id) == latest
    assert buffer.buffered_count <= 8


def test_buffer_retains_only_metadata_and_defensively_copies_caller_collections(
    snapshot: PredictionDashboardSnapshot,
) -> None:
    buffer = DashboardRevisionBuffer(capacity=3, clock=lambda: NOW)
    revision = buffer.publish(snapshot)
    assert revision is not None
    assert not any(
        isinstance(value, PredictionDashboardSnapshot) for value in vars(buffer).values()
    )
    dumped = revision.model_dump(mode="json")
    forbidden = {"markets", "posting_count", "account_fingerprint", "raw_payload"}
    assert forbidden.isdisjoint(dumped)


def test_buffer_independently_rejects_a_stale_snapshot_revision(
    snapshot: PredictionDashboardSnapshot,
) -> None:
    stale = object.__new__(PredictionDashboardSnapshot)
    object.__setattr__(stale, "__dict__", dict(snapshot.__dict__))
    object.__setattr__(stale, "__pydantic_fields_set__", snapshot.__pydantic_fields_set__)
    object.__setattr__(stale, "__pydantic_extra__", snapshot.__pydantic_extra__)
    object.__setattr__(stale, "__pydantic_private__", snapshot.__pydantic_private__)
    object.__setattr__(stale, "revision_id", "f" * 64)

    with pytest.raises(ValueError, match="revision"):
        DashboardRevisionBuffer(capacity=2, clock=lambda: NOW).publish(stale)


def test_capacity_and_cursor_inputs_fail_closed(snapshot: PredictionDashboardSnapshot) -> None:
    with pytest.raises(ValueError, match="capacity"):
        DashboardRevisionBuffer(capacity=0)
    buffer = DashboardRevisionBuffer(capacity=1, clock=lambda: NOW)
    buffer.publish(snapshot)
    with pytest.raises(ValueError, match="event ID"):
        buffer.event_after("not-decimal")


def test_waiter_wakes_for_publication_and_shutdown(snapshot: PredictionDashboardSnapshot) -> None:
    buffer = DashboardRevisionBuffer(capacity=2, clock=lambda: NOW)
    assert buffer.wait_for_event(None, timeout=0) is None
    expected = buffer.publish(snapshot)
    assert buffer.wait_for_event(None, timeout=0) == expected
    buffer.close()
    assert buffer.wait_for_event(expected.event_id if expected else None, timeout=0) is None


def test_publisher_poll_once_suppresses_duplicates_and_uses_injected_clock(
    snapshot: PredictionDashboardSnapshot,
) -> None:
    snapshots = iter((_revision(snapshot, 1), _revision(snapshot, 1), _revision(snapshot, 2)))
    buffer = DashboardRevisionBuffer(capacity=4, clock=lambda: NOW)
    publisher = DashboardRevisionPublisher(
        snapshot_factory=lambda: next(snapshots),
        revision_buffer=buffer,
        interval_seconds=0.25,
        clock=lambda: NOW + timedelta(seconds=3),
    )
    assert publisher.poll_once() is not None
    assert publisher.poll_once() is None
    last = publisher.poll_once()
    assert last is not None and last.emitted_at == NOW + timedelta(seconds=3)
    publisher.close()


def test_publisher_shutdown_interrupts_injected_wait_and_joins_thread(
    snapshot: PredictionDashboardSnapshot,
) -> None:
    entered_wait = Event()
    calls = 0

    def snapshot_factory() -> PredictionDashboardSnapshot:
        nonlocal calls
        calls += 1
        return snapshot

    def controlled_wait(stop: Event, interval: float) -> bool:
        assert interval == 60
        entered_wait.set()
        return stop.wait(interval)

    publisher = DashboardRevisionPublisher(
        snapshot_factory=snapshot_factory,
        revision_buffer=DashboardRevisionBuffer(capacity=2, clock=lambda: NOW),
        interval_seconds=60,
        clock=lambda: NOW,
    )
    publisher._wait = controlled_wait
    publisher.start()
    assert entered_wait.wait(1)
    publisher.close()
    assert calls == 1
    assert publisher.running is False


def test_publisher_recovers_after_one_snapshot_factory_failure(
    snapshot: PredictionDashboardSnapshot,
) -> None:
    published = Event()
    calls = 0
    buffer = DashboardRevisionBuffer(capacity=2, clock=lambda: NOW)

    def snapshot_factory() -> PredictionDashboardSnapshot:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("private-key authorization signed-body account ip")
        published.set()
        return snapshot

    publisher = DashboardRevisionPublisher(
        snapshot_factory=snapshot_factory,
        revision_buffer=buffer,
        interval_seconds=0.01,
        clock=lambda: NOW,
    )
    publisher.start()
    try:
        assert published.wait(1)
        assert buffer.wait_for_event(None, timeout=1) is not None
        assert publisher.running is True
        assert publisher.failure_code is None
    finally:
        publisher.close()


def test_publisher_close_is_bounded_for_blocked_factory_then_joins_after_release(
    snapshot: PredictionDashboardSnapshot,
) -> None:
    entered = Event()
    release = Event()
    close_returned = Event()

    def blocked_factory() -> PredictionDashboardSnapshot:
        entered.set()
        release.wait()
        return snapshot

    publisher = DashboardRevisionPublisher(
        snapshot_factory=blocked_factory,
        revision_buffer=DashboardRevisionBuffer(capacity=2, clock=lambda: NOW),
        interval_seconds=60,
        clock=lambda: NOW,
        shutdown_timeout_seconds=0.05,
    )
    publisher.start()
    assert entered.wait(1)
    closer = Thread(target=lambda: (publisher.close(), close_returned.set()), daemon=True)
    closer.start()
    returned_within_bound = close_returned.wait(0.25)
    release.set()
    assert close_returned.wait(1)
    publisher.close()
    closer.join(1)

    assert returned_within_bound
    assert publisher.failure_code == "SHUTDOWN_TIMEOUT"
    assert publisher.running is False


def test_publisher_close_before_start_and_repeated_close_are_deterministic(
    snapshot: PredictionDashboardSnapshot,
) -> None:
    buffer = DashboardRevisionBuffer(capacity=2, clock=lambda: NOW)
    publisher = DashboardRevisionPublisher(
        snapshot_factory=lambda: snapshot,
        revision_buffer=buffer,
        interval_seconds=1,
        clock=lambda: NOW,
    )

    publisher.close()
    publisher.close()
    assert buffer.closed
    assert publisher.running is False
    with pytest.raises(RuntimeError, match="closed"):
        publisher.start()


@pytest.mark.parametrize(
    ("seam", "expected"),
    [("clock", "CLOCK_UNAVAILABLE"), ("wait", "WAIT_UNAVAILABLE")],
)
def test_publisher_hostile_seams_map_to_fixed_failure_state(
    seam: str,
    expected: str,
    snapshot: PredictionDashboardSnapshot,
) -> None:
    publisher = DashboardRevisionPublisher(
        snapshot_factory=lambda: snapshot,
        revision_buffer=DashboardRevisionBuffer(capacity=2, clock=lambda: NOW),
        interval_seconds=0.01,
        clock=(lambda: (_ for _ in ()).throw(RuntimeError("raw clock canary")))
        if seam == "clock"
        else (lambda: NOW),
    )
    if seam == "wait":
        publisher._wait = lambda _stop, _interval: (_ for _ in ()).throw(
            RuntimeError("raw wait canary")
        )
    publisher.start()
    for _ in range(100):
        if publisher.failure_code is not None:
            break
        Event().wait(0.01)
    publisher.close()

    assert publisher.failure_code == expected


def test_reset_is_strict_and_contains_no_snapshot_values() -> None:
    reset = DashboardReset(
        schema_version=1,
        event_id="4",
        latest_revision_id=sha256(b"latest").hexdigest(),
        emitted_at=NOW,
        reason="CURSOR_NOT_AVAILABLE",
    )
    assert set(reset.model_dump()) == {
        "schema_version",
        "event_id",
        "latest_revision_id",
        "emitted_at",
        "reason",
    }
