"""macOS screen-lock and sleep signals behind the presence port.

The adapter subscribes to the distributed notifications macOS already publishes for screen lock
and to the monotonic clock for sleep detection. It observes only: it never locks, sleeps, or
wakes the machine, and it holds no secret.
"""

from __future__ import annotations

import sys
import time
from collections import deque

from polytrading.predictions.pilot.presence import NativeSignal

_LOCK_NOTIFICATIONS = {
    "com.apple.screenIsLocked": "SCREEN_LOCKED",
    "com.apple.screenIsUnlocked": "SCREEN_UNLOCKED",
    "com.apple.screensaver.didstart": "SCREEN_LOCKED",
    "com.apple.screensaver.didstop": "SCREEN_UNLOCKED",
}


class MacOSPresenceSource:
    """Buffers native signals for the monitor to drain on its own schedule."""

    __slots__ = ("_pending", "_platform")

    def __init__(self, *, platform: str = sys.platform) -> None:
        self._platform = platform
        self._pending: deque[NativeSignal] = deque(maxlen=64)

    @property
    def supported(self) -> bool:
        return self._platform == "darwin"

    def publish(self, notification_name: str) -> None:
        """Record one macOS distributed notification, ignoring anything unrecognized."""
        signal = _LOCK_NOTIFICATIONS.get(notification_name)
        if signal is not None:
            self._pending.append(signal)  # type: ignore[arg-type]

    def drain(self) -> tuple[NativeSignal, ...]:
        drained = tuple(self._pending)
        self._pending.clear()
        return drained

    def monotonic_ns(self) -> int:
        """Wall-clock time never decides sleep; only the monotonic clock does."""
        return time.monotonic_ns()


__all__ = ["MacOSPresenceSource"]
