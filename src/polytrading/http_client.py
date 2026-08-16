from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import httpx

_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})


class RetryingTransport(httpx.AsyncBaseTransport):
    """Retry a bounded set of public HTTP statuses with deterministic backoff."""

    def __init__(
        self,
        transport: httpx.AsyncBaseTransport,
        *,
        max_attempts: int = 3,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int):
            raise TypeError("max_attempts must be an integer")
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        self._transport = transport
        self._max_attempts = max_attempts
        self._sleep = sleep

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        for attempt in range(1, self._max_attempts + 1):
            response = await self._transport.handle_async_request(request)
            if response.status_code not in _RETRYABLE_STATUSES or attempt == self._max_attempts:
                return response
            await response.aclose()
            await self._sleep(0.25 * 2 ** (attempt - 1))
        raise AssertionError("positive retry budget must return a response")

    async def aclose(self) -> None:
        await self._transport.aclose()


def make_public_http_client(
    *, transport: httpx.AsyncBaseTransport | None = None
) -> httpx.AsyncClient:
    base_transport = transport or httpx.AsyncHTTPTransport()
    return httpx.AsyncClient(
        headers={
            "Accept-Encoding": "identity",
            "User-Agent": "polytrading/0.1 public-market-research",
        },
        timeout=httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=30.0),
        transport=RetryingTransport(base_transport),
    )
