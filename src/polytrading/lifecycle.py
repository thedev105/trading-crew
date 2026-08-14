from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager

Cleanup = Callable[[], object]
AsyncCleanup = Callable[[], Awaitable[object]]
CleanupErrorFactory = Callable[[], "OwnedResourceCleanupError"]


class OwnedResourceCleanupError(RuntimeError):
    """An owned resource failed during cleanup after the body succeeded."""

    def __init__(self, message: str = "OWNED_RESOURCE_CLEANUP_ERROR") -> None:
        super().__init__(message)


class CleanupRegistry:
    """Cleanup callbacks registered in resource ownership order."""

    def __init__(self) -> None:
        self._callbacks: list[Cleanup] = []

    def add(self, callback: Cleanup) -> None:
        self._callbacks.append(callback)

    def run(self) -> BaseException | None:
        first_error: BaseException | None = None
        for callback in self._callbacks:
            try:
                callback()
            except BaseException as error:
                if first_error is None:
                    first_error = error
        return first_error


class AsyncCleanupRegistry:
    """Async cleanup callbacks registered in resource ownership order."""

    def __init__(self) -> None:
        self._callbacks: list[AsyncCleanup] = []

    def add(self, callback: AsyncCleanup) -> None:
        self._callbacks.append(callback)

    async def run(self) -> BaseException | None:
        async def capture(callback: AsyncCleanup) -> BaseException | None:
            try:
                await callback()
            except BaseException as error:
                return error
            return None

        errors = await asyncio.gather(*(capture(callback) for callback in self._callbacks))
        return next((error for error in errors if error is not None), None)


@contextmanager
def owned_resource_cleanup(
    *, marker_factory: CleanupErrorFactory = OwnedResourceCleanupError
) -> Iterator[CleanupRegistry]:
    """Preserve an active primary while completing every registered cleanup."""

    registry = CleanupRegistry()
    active_error: BaseException | None = None
    try:
        try:
            yield registry
        except BaseException as error:
            active_error = error
            raise
    finally:
        cleanup_error = registry.run()
        if active_error is None and cleanup_error is not None:
            raise marker_factory() from cleanup_error


@asynccontextmanager
async def async_owned_resource_cleanup(
    *, marker_factory: CleanupErrorFactory = OwnedResourceCleanupError
) -> AsyncIterator[AsyncCleanupRegistry]:
    """Async companion preserving the same primary and first-cause semantics."""

    registry = AsyncCleanupRegistry()
    active_error: BaseException | None = None
    try:
        try:
            yield registry
        except BaseException as error:
            active_error = error
            raise
    finally:
        cleanup_error = await registry.run()
        if active_error is None and cleanup_error is not None:
            raise marker_factory() from cleanup_error


def cleanup_error_cause(error: BaseException) -> BaseException:
    """Return the raw cause only for the known ordinary cleanup marker hierarchy."""

    if isinstance(error, OwnedResourceCleanupError) and error.__cause__ is not None:
        return error.__cause__
    return error
