from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from polytrading.predictions.pilot.models import PresenceState
from polytrading.predictions.pilot.presence import (
    HEARTBEAT_INTERVAL,
    MAXIMUM_HEARTBEAT_SILENCE,
    PresenceMonitor,
)
from polytrading.predictions.pilot.presence_macos import MacOSPresenceSource

NOW = datetime(2026, 8, 29, 12, tzinfo=UTC)


class FakePresenceSource:
    """Injected native signals and a controllable monotonic clock; no real machine is touched."""

    def __init__(self, *, signals: list[str] | None = None, monotonic_ns: int = 0) -> None:
        self.signals = signals or []
        self._monotonic = monotonic_ns

    def drain(self) -> tuple[str, ...]:
        drained = tuple(self.signals)
        self.signals.clear()
        return drained  # type: ignore[return-value]

    def monotonic_ns(self) -> int:
        return self._monotonic

    def advance(self, seconds: float) -> None:
        self._monotonic += int(seconds * 1_000_000_000)


def monitor(**overrides: object) -> tuple[PresenceMonitor, FakePresenceSource, list[str]]:
    source = overrides.pop("source", None) or FakePresenceSource()
    killed: list[str] = []
    return (
        PresenceMonitor(
            source=source,
            started_at=NOW,
            on_kill=killed.append,  # type: ignore[arg-type]
        ),
        source,
        killed,
    )


def test_a_two_second_heartbeat_keeps_the_operator_present() -> None:
    monitored, _source, killed = monitor()

    for step in range(1, 6):
        decision = monitored.record_browser_heartbeat(NOW + HEARTBEAT_INTERVAL * step)
        assert decision.state is PresenceState.PRESENT

    assert killed == []


def test_two_missed_heartbeats_engage_the_kill() -> None:
    monitored, _source, killed = monitor()

    decision = monitored.record_browser_heartbeat(NOW + timedelta(seconds=4.5))

    assert decision.state is PresenceState.TERMINAL
    assert decision.kill_reason == "HEARTBEAT_MISSED"
    assert killed == ["HEARTBEAT_MISSED"]


def test_five_seconds_of_silence_engages_the_kill_without_a_heartbeat() -> None:
    monitored, _source, killed = monitor()

    decision = monitored.evaluate(NOW + MAXIMUM_HEARTBEAT_SILENCE)

    assert decision.kill_reason == "HEARTBEAT_SILENT"
    assert killed == ["HEARTBEAT_SILENT"]


def test_a_screen_lock_kills_immediately() -> None:
    source = FakePresenceSource(signals=["SCREEN_LOCKED"])
    monitored, _source, killed = monitor(source=source)

    decision = monitored.record_native_state(NOW)

    assert decision.state is PresenceState.TERMINAL
    assert decision.kill_reason == "SCREEN_LOCKED"
    assert killed == ["SCREEN_LOCKED"]


def test_a_sleep_signal_kills_immediately() -> None:
    source = FakePresenceSource(signals=["SYSTEM_SLEPT"])
    monitored, _source, _killed = monitor(source=source)

    assert monitored.record_native_state(NOW).kill_reason == "SYSTEM_SLEPT"


def test_a_monotonic_jump_is_classified_as_sleep_before_any_other_decision() -> None:
    source = FakePresenceSource()
    monitored, _source, killed = monitor(source=source)
    source.advance(30)

    decision = monitored.record_native_state(NOW + timedelta(seconds=30))

    assert decision.kill_reason == "SYSTEM_SLEPT"
    assert killed == ["SYSTEM_SLEPT"]


def test_a_short_monotonic_step_is_not_a_sleep() -> None:
    source = FakePresenceSource()
    monitored, _source, killed = monitor(source=source)
    source.advance(1)

    assert monitored.record_native_state(NOW + timedelta(seconds=1)).state is PresenceState.PRESENT
    assert killed == []


def test_a_closed_page_ends_presence() -> None:
    monitored, _source, _killed = monitor()

    assert monitored.record_page_closed(NOW).kill_reason == "PAGE_CLOSED"


def test_presence_never_recovers_on_its_own() -> None:
    monitored, source, _killed = monitor(source=FakePresenceSource(signals=["SCREEN_LOCKED"]))
    monitored.record_native_state(NOW)
    source.signals.append("SCREEN_UNLOCKED")

    assert monitored.record_native_state(NOW + timedelta(seconds=1)).state is PresenceState.TERMINAL
    assert (
        monitored.record_browser_heartbeat(NOW + timedelta(seconds=2)).state
        is PresenceState.TERMINAL
    )


@pytest.mark.parametrize(
    ("notification", "expected"),
    [
        ("com.apple.screenIsLocked", ("SCREEN_LOCKED",)),
        ("com.apple.screensaver.didstart", ("SCREEN_LOCKED",)),
        ("com.apple.screenIsUnlocked", ("SCREEN_UNLOCKED",)),
        ("com.apple.unrelated.notification", ()),
    ],
)
def test_the_macos_source_maps_only_the_reviewed_notifications(
    notification: str, expected: tuple[str, ...]
) -> None:
    source = MacOSPresenceSource(platform="darwin")
    source.publish(notification)

    assert source.drain() == expected


def test_the_macos_source_reports_whether_this_platform_is_supported() -> None:
    assert MacOSPresenceSource(platform="darwin").supported is True
    assert MacOSPresenceSource(platform="linux").supported is False


def test_the_macos_source_uses_a_monotonic_clock() -> None:
    source = MacOSPresenceSource(platform="darwin")

    first = source.monotonic_ns()
    second = source.monotonic_ns()

    assert second >= first


# -- the macOS notification bridge -----------------------------------------------------------


class FakeCoreFoundation:
    """A stand-in for CoreFoundation: records the subscription, runs no real loop."""

    def __init__(self) -> None:
        self.observed: list[str] = []
        self.last_string = b""
        self.removed = 0
        self.stopped = 0
        self.ran = False
        self._callback = None

    def CFNotificationCenterGetDistributedCenter(self) -> int:
        return 1

    def CFStringCreateWithCString(self, _allocator, value: bytes, _encoding) -> bytes:
        self.last_string = value
        return value

    def CFNotificationCenterAddObserver(
        self, _center, _observer, callback, name: bytes, _object, _behaviour
    ) -> None:
        self._callback = callback
        self.observed.append(name.decode("utf-8"))

    def CFNotificationCenterRemoveEveryObserver(self, _center, _observer) -> None:
        self.removed += 1

    def CFRunLoopGetCurrent(self) -> int:
        return 2

    def CFRunLoopRun(self) -> None:
        self.ran = True

    def CFRunLoopStop(self, _loop) -> None:
        self.stopped += 1

    def CFStringGetCString(self, _reference, buffer, _length, _encoding) -> bool:
        buffer.value = self.last_string
        return True


def test_the_bridge_subscribes_to_exactly_the_four_lock_notifications() -> None:
    from polytrading.predictions.pilot.presence_macos import _CoreFoundationBridge

    core = FakeCoreFoundation()
    published: list[str] = []
    bridge = _CoreFoundationBridge(library=core)

    bridge.observe(
        (
            "com.apple.screenIsLocked",
            "com.apple.screenIsUnlocked",
            "com.apple.screensaver.didstart",
            "com.apple.screensaver.didstop",
        ),
        published.append,
    )

    assert core.observed == [
        "com.apple.screenIsLocked",
        "com.apple.screenIsUnlocked",
        "com.apple.screensaver.didstart",
        "com.apple.screensaver.didstop",
    ]


def test_starting_and_stopping_the_source_registers_and_releases_observers() -> None:
    core = FakeCoreFoundation()
    from polytrading.predictions.pilot.presence_macos import _CoreFoundationBridge

    source = MacOSPresenceSource(platform="darwin")
    source.start(bridge=_CoreFoundationBridge(library=core))
    source.stop()

    assert core.observed
    assert core.ran is True
    assert core.removed == 1
    assert core.stopped == 1


def test_an_unsupported_platform_never_subscribes() -> None:
    source = MacOSPresenceSource(platform="linux")

    with pytest.raises(RuntimeError, match="PRESENCE_SOURCE_UNSUPPORTED"):
        source.start()


def test_the_bridge_binds_the_real_core_foundation_symbols() -> None:
    import sys

    from polytrading.predictions.pilot.presence_macos import _load_core_foundation

    if sys.platform != "darwin":
        pytest.skip("CoreFoundation exists only on macOS")

    core = _load_core_foundation()

    assert core.CFNotificationCenterGetDistributedCenter() != 0
    for symbol in (
        "CFNotificationCenterAddObserver",
        "CFNotificationCenterRemoveEveryObserver",
        "CFRunLoopRun",
        "CFRunLoopStop",
        "CFStringGetCString",
    ):
        assert getattr(core, symbol) is not None


def test_a_lock_notification_reaches_the_monitor_as_a_kill() -> None:
    core = FakeCoreFoundation()
    from polytrading.predictions.pilot.presence_macos import _CoreFoundationBridge

    source = MacOSPresenceSource(platform="darwin")
    bridge = _CoreFoundationBridge(library=core)
    bridge.observe(("com.apple.screenIsLocked",), source.publish)
    monitored, _source, killed = monitor(source=source)

    # Exactly what the run loop would do when macOS posts the notification.
    core._callback(1, None, b"com.apple.screenIsLocked", None, None)

    assert monitored.record_native_state(NOW).kill_reason == "SCREEN_LOCKED"
    assert killed == ["SCREEN_LOCKED"]
