import multiprocessing
from pathlib import Path

import pytest

import polytrading.trial.writer_lease as writer_lease
from polytrading.trial.writer_lease import (
    WriterLeaseUnavailable,
    database_writer_lease,
    writer_lease_path,
)


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
