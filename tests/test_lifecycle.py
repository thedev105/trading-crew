from __future__ import annotations

import asyncio

import pytest

from polytrading.lifecycle import (
    OwnedResourceCleanupError,
    async_owned_resource_cleanup,
    cleanup_error_cause,
    owned_resource_cleanup,
)


def test_sync_cleanup_preserves_exact_primary_and_attempts_every_phase() -> None:
    primary = asyncio.CancelledError("primary token=primary")
    cleanup_errors = (
        asyncio.CancelledError("cleanup token=first"),
        KeyboardInterrupt("cleanup token=second"),
    )
    events: list[str] = []

    def fail(index: int) -> None:
        events.append(f"cleanup-{index}")
        raise cleanup_errors[index]

    with (
        pytest.raises(BaseException) as captured,
        owned_resource_cleanup() as cleanup,
    ):
        cleanup.add(lambda: fail(0))
        cleanup.add(lambda: fail(1))
        events.append("body")
        raise primary

    assert captured.value is primary
    assert events == ["body", "cleanup-0", "cleanup-1"]


def test_sync_cleanup_wraps_first_base_exception_and_attempts_every_phase() -> None:
    cleanup_errors = (
        asyncio.CancelledError("cleanup token=first"),
        KeyboardInterrupt("cleanup token=second"),
    )
    events: list[str] = []

    def fail(index: int) -> None:
        events.append(f"cleanup-{index}")
        raise cleanup_errors[index]

    with (
        pytest.raises(Exception, match=r"^OWNED_RESOURCE_CLEANUP_ERROR$") as captured,
        owned_resource_cleanup() as cleanup,
    ):
        cleanup.add(lambda: fail(0))
        cleanup.add(lambda: fail(1))
        events.append("body")

    assert captured.value.__cause__ is cleanup_errors[0]
    assert events == ["body", "cleanup-0", "cleanup-1"]


def test_sync_cleanup_runs_successful_callbacks_without_a_marker() -> None:
    events: list[str] = []

    with owned_resource_cleanup() as cleanup:
        cleanup.add(lambda: events.append("cleanup-0"))
        cleanup.add(lambda: events.append("cleanup-1"))
        events.append("body")

    assert events == ["body", "cleanup-0", "cleanup-1"]


def test_sync_cleanup_preserves_exact_ordinary_primary() -> None:
    primary = RuntimeError("primary token=primary")
    cleanup_error = KeyboardInterrupt("cleanup token=cleanup")
    events: list[str] = []

    def cleanup_callback() -> None:
        events.append("cleanup")
        raise cleanup_error

    with (
        pytest.raises(BaseException) as captured,
        owned_resource_cleanup() as cleanup,
    ):
        cleanup.add(cleanup_callback)
        events.append("body")
        raise primary

    assert captured.value is primary
    assert events == ["body", "cleanup"]


def test_cleanup_error_cause_unwraps_only_the_known_marker() -> None:
    raw_cause = OSError("hostile token=secret")
    marker = OwnedResourceCleanupError()
    marker.__cause__ = raw_cause
    unrelated = RuntimeError("ordinary body")
    unrelated.__cause__ = raw_cause

    assert cleanup_error_cause(marker) is raw_cause
    assert cleanup_error_cause(unrelated) is unrelated


def test_async_cleanup_preserves_exact_primary_and_closes_every_resource() -> None:
    primary = asyncio.CancelledError("primary token=primary")
    cleanup_errors = (
        asyncio.CancelledError("cleanup token=first"),
        KeyboardInterrupt("cleanup token=second"),
    )
    events: list[str] = []

    async def fail(index: int) -> None:
        events.append(f"cleanup-{index}")
        raise cleanup_errors[index]

    async def exercise() -> BaseException:
        with pytest.raises(BaseException) as captured:
            async with async_owned_resource_cleanup() as cleanup:
                cleanup.add(lambda: fail(0))
                cleanup.add(lambda: fail(1))
                events.append("body")
                raise primary
        return captured.value

    assert asyncio.run(exercise()) is primary
    assert events == ["body", "cleanup-0", "cleanup-1"]


def test_async_cleanup_uses_registration_order_and_awaits_every_resource() -> None:
    first = asyncio.CancelledError("cleanup token=first")
    second = KeyboardInterrupt("cleanup token=second")
    events: list[str] = []

    async def fail_first(second_started: asyncio.Event) -> None:
        events.append("cleanup-0-start")
        await second_started.wait()
        events.append("cleanup-0-fail")
        raise first

    async def fail_second(second_started: asyncio.Event) -> None:
        events.append("cleanup-1-start")
        second_started.set()
        events.append("cleanup-1-fail")
        raise second

    async def exercise() -> BaseException:
        second_started = asyncio.Event()
        with pytest.raises(Exception, match=r"^OWNED_RESOURCE_CLEANUP_ERROR$") as captured:
            async with async_owned_resource_cleanup() as cleanup:
                cleanup.add(lambda: fail_first(second_started))
                cleanup.add(lambda: fail_second(second_started))
                events.append("body")
        return captured.value

    captured = asyncio.run(exercise())
    assert captured.__cause__ is first
    assert events == [
        "body",
        "cleanup-0-start",
        "cleanup-1-start",
        "cleanup-1-fail",
        "cleanup-0-fail",
    ]


def test_async_cleanup_runs_successful_callbacks_without_a_marker() -> None:
    events: list[str] = []

    async def record(value: str) -> None:
        events.append(value)

    async def exercise() -> None:
        async with async_owned_resource_cleanup() as cleanup:
            cleanup.add(lambda: record("cleanup-0"))
            cleanup.add(lambda: record("cleanup-1"))
            events.append("body")

    asyncio.run(exercise())
    assert events == ["body", "cleanup-0", "cleanup-1"]


def test_async_cleanup_preserves_exact_ordinary_primary() -> None:
    primary = RuntimeError("primary token=primary")
    cleanup_error = asyncio.CancelledError("cleanup token=cleanup")
    events: list[str] = []

    async def cleanup_callback() -> None:
        events.append("cleanup")
        raise cleanup_error

    async def exercise() -> BaseException:
        with pytest.raises(BaseException) as captured:
            async with async_owned_resource_cleanup() as cleanup:
                cleanup.add(cleanup_callback)
                events.append("body")
                raise primary
        return captured.value

    assert asyncio.run(exercise()) is primary
    assert events == ["body", "cleanup"]
