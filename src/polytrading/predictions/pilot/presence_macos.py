"""macOS screen-lock and sleep signals behind the presence port.

The adapter subscribes to the distributed notifications macOS already publishes for screen lock
and to the monotonic clock for sleep detection. It observes only: it never locks, sleeps, or
wakes the machine, and it holds no secret.
"""

from __future__ import annotations

import sys
import threading
import time
from collections import deque
from typing import Any, Final

from polytrading.predictions.pilot.presence import NativeSignal

_OBSERVED_NOTIFICATIONS: Final = (
    "com.apple.screenIsLocked",
    "com.apple.screenIsUnlocked",
    "com.apple.screensaver.didstart",
    "com.apple.screensaver.didstop",
)
_CF_STRING_ENCODING_UTF8: Final = 0x08000100
# CFNotificationSuspensionBehaviorDeliverImmediately
_DELIVER_IMMEDIATELY: Final = 4

_LOCK_NOTIFICATIONS = {
    "com.apple.screenIsLocked": "SCREEN_LOCKED",
    "com.apple.screenIsUnlocked": "SCREEN_UNLOCKED",
    "com.apple.screensaver.didstart": "SCREEN_LOCKED",
    "com.apple.screensaver.didstop": "SCREEN_UNLOCKED",
}


class MacOSPresenceSource:
    """Buffers native signals for the monitor to drain on its own schedule."""

    __slots__ = ("_bridge", "_pending", "_platform", "_thread")

    def __init__(self, *, platform: str = sys.platform) -> None:
        self._platform = platform
        self._pending: deque[NativeSignal] = deque(maxlen=64)
        self._bridge: _CoreFoundationBridge | None = None
        self._thread: threading.Thread | None = None

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

    def start(self, *, bridge: _CoreFoundationBridge | None = None) -> None:
        """Subscribe to the lock notifications on a background run loop.

        Observing is all this does: it registers for four names, never posts a notification, and
        never locks, sleeps, or wakes the machine.
        """
        if not self.supported:
            raise RuntimeError("PRESENCE_SOURCE_UNSUPPORTED")
        if self._thread is not None:
            return
        self._bridge = bridge or _CoreFoundationBridge()
        self._bridge.observe(_OBSERVED_NOTIFICATIONS, self.publish)
        self._thread = threading.Thread(target=self._bridge.run, name="pilot-presence", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the run loop and drop every observer this source registered."""
        bridge, thread = self._bridge, self._thread
        self._bridge, self._thread = None, None
        if bridge is not None:
            bridge.stop()
        if thread is not None:
            thread.join(timeout=2)


class _CoreFoundationBridge:
    """A four-symbol ctypes binding to the distributed notification center."""

    __slots__ = ("_callback", "_core", "_loop", "_started")

    def __init__(self, library: Any | None = None) -> None:
        self._core = library or _load_core_foundation()
        self._callback: Any | None = None
        self._loop: Any | None = None
        self._started = threading.Event()

    def observe(self, names: tuple[str, ...], publish: Any) -> None:
        import ctypes

        prototype = ctypes.CFUNCTYPE(
            None,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        )

        def _deliver(_center: int, _observer: int, name: int, _object: int, _info: int) -> None:
            publish(self._text(name))

        # The callback is retained here for the life of the bridge: C holds a bare pointer.
        self._callback = prototype(_deliver)
        center = self._core.CFNotificationCenterGetDistributedCenter()
        for name in names:
            self._core.CFNotificationCenterAddObserver(
                center,
                None,
                self._callback,
                self._string(name),
                None,
                _DELIVER_IMMEDIATELY,
            )

    def run(self) -> None:
        self._loop = self._core.CFRunLoopGetCurrent()
        self._started.set()
        self._core.CFRunLoopRun()

    def stop(self) -> None:
        self._started.wait(timeout=2)
        center = self._core.CFNotificationCenterGetDistributedCenter()
        self._core.CFNotificationCenterRemoveEveryObserver(center, None)
        if self._loop is not None:
            self._core.CFRunLoopStop(self._loop)

    def _string(self, value: str) -> Any:
        return self._core.CFStringCreateWithCString(
            None, value.encode("utf-8"), _CF_STRING_ENCODING_UTF8
        )

    def _text(self, reference: int) -> str:
        import ctypes

        buffer = ctypes.create_string_buffer(256)
        if not self._core.CFStringGetCString(
            reference, buffer, len(buffer), _CF_STRING_ENCODING_UTF8
        ):
            return ""
        return buffer.value.decode("utf-8", errors="ignore")


def _load_core_foundation() -> Any:
    import ctypes
    import ctypes.util

    path = ctypes.util.find_library("CoreFoundation")
    if path is None:
        raise RuntimeError("PRESENCE_SOURCE_UNSUPPORTED")
    core = ctypes.cdll.LoadLibrary(path)
    core.CFNotificationCenterGetDistributedCenter.restype = ctypes.c_void_p
    core.CFStringCreateWithCString.restype = ctypes.c_void_p
    core.CFStringCreateWithCString.argtypes = [
        ctypes.c_void_p,
        ctypes.c_char_p,
        ctypes.c_uint32,
    ]
    core.CFRunLoopGetCurrent.restype = ctypes.c_void_p
    core.CFStringGetCString.restype = ctypes.c_bool
    core.CFStringGetCString.argtypes = [
        ctypes.c_void_p,
        ctypes.c_char_p,
        ctypes.c_long,
        ctypes.c_uint32,
    ]
    return core


__all__ = ["MacOSPresenceSource"]
