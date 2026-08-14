import math
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

__all__ = ["WriterLeaseUnavailable", "database_writer_lease", "writer_lease_path"]


class WriterLeaseUnavailable(RuntimeError):
    """Raised when another process currently holds a database writer lease."""


def writer_lease_path(database_path: Path) -> Path:
    return database_path.with_name(f"{database_path.name}.writer.lock")


@contextmanager
def database_writer_lease(
    database_path: Path,
    *,
    timeout_seconds: float,
    poll_seconds: float = 0.05,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> Iterator[None]:
    timeout = _finite_nonnegative(timeout_seconds, "writer lease timeout")
    poll = _finite_positive(poll_seconds, "writer lease poll interval")
    deadline = monotonic() + timeout
    handle = writer_lease_path(database_path).open("a+b")
    locked = False
    try:
        while not _try_lock(handle):
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise WriterLeaseUnavailable("database writer lease is busy")
            sleep(min(poll, remaining))
        locked = True
        yield
    finally:
        try:
            if locked:
                _unlock_if_held(handle)
        finally:
            handle.close()


def _finite_nonnegative(value: float, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be finite and nonnegative")
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{label} must be finite and nonnegative")
    return float(value)


def _finite_positive(value: float, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be finite and positive")
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{label} must be finite and positive")
    return float(value)


def _try_lock(handle: BinaryIO) -> bool:
    if sys.platform == "win32":
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
    else:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
    return True


def _unlock_if_held(handle: BinaryIO) -> None:
    if sys.platform == "win32":
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
