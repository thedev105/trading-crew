import asyncio
import errno
import multiprocessing
import sys
from pathlib import Path

import pytest

import polytrading.trial.writer_lease as writer_lease
from polytrading.trial.writer_lease import (
    WriterLeaseUnavailable,
    database_writer_lease,
    writer_lease_path,
)


class _RecordingLockHandle:
    def __init__(
        self,
        events: list[str],
        *,
        close_error: BaseException | None = None,
    ) -> None:
        self._events = events
        self._close_error = close_error

    def close(self) -> None:
        self._events.append("close")
        if self._close_error is not None:
            raise self._close_error


class _RecordingLockPath:
    def __init__(self, events: list[str], handle: _RecordingLockHandle) -> None:
        self._events = events
        self._handle = handle

    def open(self, mode: str) -> _RecordingLockHandle:
        assert mode == "a+b"
        self._events.append("open")
        return self._handle


def _install_recording_lock(
    monkeypatch: pytest.MonkeyPatch,
    *,
    close_error: BaseException | None = None,
    unlock_error: BaseException | None = None,
) -> list[str]:
    events: list[str] = []
    handle = _RecordingLockHandle(events, close_error=close_error)
    lock_path = _RecordingLockPath(events, handle)

    def try_lock(candidate: object) -> bool:
        assert candidate is handle
        events.append("lock")
        return True

    def unlock(candidate: object) -> None:
        assert candidate is handle
        events.append("unlock")
        if unlock_error is not None:
            raise unlock_error

    monkeypatch.setattr(writer_lease, "writer_lease_path", lambda _database: lock_path)
    monkeypatch.setattr(writer_lease, "_try_lock", try_lock)
    monkeypatch.setattr(writer_lease, "_unlock_if_held", unlock)
    return events


def _hold_writer_lease(database: Path, acquired, release) -> None:
    with database_writer_lease(database, timeout_seconds=0):
        acquired.put(True)
        release.wait(5)


def test_writer_lease_lives_beside_the_database(tmp_path: Path) -> None:
    database = tmp_path / "trial.duckdb"

    assert writer_lease_path(database) == tmp_path / "trial.duckdb.writer.lock"


def test_second_writer_times_out_without_entering(tmp_path: Path) -> None:
    database = tmp_path / "trial.duckdb"

    with (
        database_writer_lease(database, timeout_seconds=0),
        pytest.raises(WriterLeaseUnavailable, match="database writer lease is busy"),
        database_writer_lease(database, timeout_seconds=0),
    ):
        raise AssertionError("contended lease entered")


def test_writer_lease_releases_after_body_failure(tmp_path: Path) -> None:
    database = tmp_path / "trial.duckdb"

    with (
        pytest.raises(RuntimeError, match="boom"),
        database_writer_lease(database, timeout_seconds=0),
    ):
        raise RuntimeError("boom")

    with database_writer_lease(database, timeout_seconds=0):
        pass


@pytest.mark.parametrize(
    ("primary", "cleanup_phase"),
    [
        (RuntimeError("primary body failure"), "unlock"),
        (asyncio.CancelledError("exact cancellation"), "unlock"),
        (RuntimeError("primary body failure"), "close"),
        (RuntimeError("primary body failure"), "unlock_base_exception"),
    ],
    ids=(
        "body-plus-unlock",
        "cancellation-plus-unlock",
        "body-plus-close",
        "body-plus-unlock-base-exception",
    ),
)
def test_writer_lease_cleanup_does_not_replace_exact_primary_and_all_phases_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    primary: BaseException,
    cleanup_phase: str,
) -> None:
    cleanup_error: BaseException = (
        asyncio.CancelledError("hostile cleanup cancellation")
        if cleanup_phase == "unlock_base_exception"
        else OSError("hostile /private/lease.lock?token=secret")
    )
    events = _install_recording_lock(
        monkeypatch,
        unlock_error=cleanup_error if cleanup_phase.startswith("unlock") else None,
        close_error=cleanup_error if cleanup_phase == "close" else None,
    )

    with (
        pytest.raises(BaseException) as captured,
        database_writer_lease(tmp_path / "trial.duckdb", timeout_seconds=0),
    ):
        raise primary

    assert captured.value is primary
    assert events == ["open", "lock", "unlock", "close"]


def test_writer_lease_first_cleanup_failure_wins_but_later_close_is_attempted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unlock_error = asyncio.CancelledError("hostile first unlock cancellation")
    close_error = RuntimeError("hostile later close failure")
    events = _install_recording_lock(
        monkeypatch,
        unlock_error=unlock_error,
        close_error=close_error,
    )

    with (
        pytest.raises(BaseException) as captured,
        database_writer_lease(tmp_path / "trial.duckdb", timeout_seconds=0),
    ):
        pass

    assert type(captured.value).__name__ == "WriterLeaseCleanupError"
    assert str(captured.value) == "DATABASE_WRITER_LEASE_CLEANUP_ERROR"
    assert captured.value.__cause__ is unlock_error
    assert events == ["open", "lock", "unlock", "close"]


def test_writer_lease_close_only_failure_is_retained_as_cleanup_cause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    close_error = OSError("hostile close-only /private/lease.lock")
    events = _install_recording_lock(monkeypatch, close_error=close_error)

    with (
        pytest.raises(BaseException) as captured,
        database_writer_lease(tmp_path / "trial.duckdb", timeout_seconds=0),
    ):
        pass

    assert type(captured.value).__name__ == "WriterLeaseCleanupError"
    assert str(captured.value) == "DATABASE_WRITER_LEASE_CLEANUP_ERROR"
    assert captured.value.__cause__ is close_error
    assert events == ["open", "lock", "unlock", "close"]


@pytest.mark.parametrize(
    ("timeout_seconds", "poll_seconds", "message"),
    [
        (True, 0.05, "writer lease timeout"),
        (-0.01, 0.05, "writer lease timeout"),
        (float("nan"), 0.05, "writer lease timeout"),
        (float("inf"), 0.05, "writer lease timeout"),
        (0, True, "writer lease poll interval"),
        (0, -0.01, "writer lease poll interval"),
        (0, 0, "writer lease poll interval"),
        (0, float("nan"), "writer lease poll interval"),
        (0, float("inf"), "writer lease poll interval"),
    ],
)
def test_writer_lease_rejects_nonfinite_or_invalid_intervals(
    tmp_path: Path, timeout_seconds: float, poll_seconds: float, message: str
) -> None:
    with (
        pytest.raises(ValueError, match=message),
        database_writer_lease(
            tmp_path / "trial.duckdb",
            timeout_seconds=timeout_seconds,
            poll_seconds=poll_seconds,
        ),
    ):
        pass


def test_writer_lease_retries_only_until_its_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monotonic_values = iter((10.0, 10.0, 10.04, 10.11))
    sleeps: list[float] = []
    monkeypatch.setattr(writer_lease, "_try_lock", lambda handle: False)

    with (
        pytest.raises(WriterLeaseUnavailable, match="database writer lease is busy"),
        database_writer_lease(
            tmp_path / "trial.duckdb",
            timeout_seconds=0.1,
            poll_seconds=0.05,
            monotonic=lambda: next(monotonic_values),
            sleep=sleeps.append,
        ),
    ):
        pass

    assert sleeps == [0.05, 0.05]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX flock behavior")
@pytest.mark.parametrize("lock_errno", [errno.EACCES, errno.EAGAIN])
def test_posix_contention_errnos_follow_bounded_lease_handling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lock_errno: int
) -> None:
    def raise_lock_error(file_descriptor: int, operation: int) -> None:
        del file_descriptor
        if operation == writer_lease.fcntl.LOCK_UN:
            return
        raise OSError(lock_errno, "simulated flock contention")

    monkeypatch.setattr(writer_lease.fcntl, "flock", raise_lock_error)

    with (
        pytest.raises(WriterLeaseUnavailable, match="database writer lease is busy"),
        database_writer_lease(tmp_path / "trial.duckdb", timeout_seconds=0),
    ):
        raise AssertionError("contended lease entered")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX flock behavior")
def test_posix_unexpected_lock_errno_is_propagated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def raise_lock_error(file_descriptor: int, operation: int) -> None:
        del file_descriptor, operation
        raise OSError(errno.EIO, "simulated flock failure")

    monkeypatch.setattr(writer_lease.fcntl, "flock", raise_lock_error)

    with (
        pytest.raises(OSError) as exception,
        database_writer_lease(tmp_path / "trial.duckdb", timeout_seconds=0),
    ):
        pass

    assert exception.value.errno == errno.EIO


def test_writer_lease_excludes_a_second_process_and_releases_afterward(tmp_path: Path) -> None:
    database = tmp_path / "trial.duckdb"
    context = multiprocessing.get_context("spawn")
    acquired = context.Queue()
    release = context.Event()
    process = context.Process(target=_hold_writer_lease, args=(database, acquired, release))
    process.start()
    try:
        assert acquired.get(timeout=5) is True
        with (
            pytest.raises(WriterLeaseUnavailable, match="database writer lease is busy"),
            database_writer_lease(database, timeout_seconds=0),
        ):
            raise AssertionError("contended lease entered")
    finally:
        release.set()
        process.join(5)
        if process.is_alive():
            process.terminate()
            process.join(5)

    assert process.exitcode == 0
    with database_writer_lease(database, timeout_seconds=0):
        pass
