import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest

from polytrading.predictions.execution.authority import AuthorityDecision
from polytrading.predictions.execution.models import (
    VenueOrderEvent,
    VenueOrderState,
    VenueTradeEvent,
    VenueTradeState,
)
from polytrading.predictions.polymarket_execution import user_stream as user_stream_module
from polytrading.predictions.polymarket_execution.protocol import bundled_fixture_path
from polytrading.predictions.polymarket_execution.routes import RouteKey
from polytrading.predictions.polymarket_execution.secrets import SecretMaterial
from polytrading.predictions.polymarket_execution.user_stream import (
    MAX_USER_STREAM_MESSAGE_BYTES,
    UserStreamHealth,
    UserStreamParser,
    UserStreamProtocolError,
    parse_user_event,
    recovery_reads_after_stream_gap,
)

NOW = datetime(2026, 8, 25, 16, tzinfo=UTC)


def _trade_frame(status: str = "RETRYING") -> bytes:
    vectors = json.loads(
        (bundled_fixture_path() / "event_vectors_v1.json").read_text(encoding="utf-8")
    )
    vector = {
        **vectors["user_trade_event"],
        "asset_id": "217426",
        "maker_address": "0x" + "11" * 20,
        "status": status,
    }
    vector["maker_orders"] = [
        {
            **item,
            "asset_id": "217426",
            "maker_address": "0x" + "22" * 20,
        }
        for item in vector["maker_orders"]
    ]
    return json.dumps(vector, separators=(",", ":"), sort_keys=True).encode()


def _changed_trade_frame(**updates: object) -> bytes:
    vector = json.loads(_trade_frame())
    vector.update(updates)
    return json.dumps(vector, separators=(",", ":"), sort_keys=True).encode()


def _order_frame(
    event_type: str,
    *,
    status: str,
    original_size: str = "10",
    size_matched: str = "0",
) -> bytes:
    vectors = json.loads(
        (bundled_fixture_path() / "event_vectors_v1.json").read_text(encoding="utf-8")
    )
    vector = {
        **vectors["user_order_event"],
        "asset_id": "217426",
        "maker_address": "0x" + "11" * 20,
        "original_size": original_size,
        "size_matched": size_matched,
        "status": status,
        "type": event_type,
    }
    return json.dumps(vector, separators=(",", ":"), sort_keys=True).encode()


def test_trade_retrying_is_nonterminal() -> None:
    event = parse_user_event(_trade_frame(), receipt_time=NOW)

    assert isinstance(event, VenueTradeEvent)
    assert event.normalized_state is VenueTradeState.RETRYING
    assert event.terminal is False


@pytest.mark.parametrize(
    ("wire_state", "expected_state", "terminal"),
    (
        ("MATCHED_NOT_BROADCASTED", VenueTradeState.MATCHED_NOT_BROADCASTED, False),
        ("MATCHED", VenueTradeState.MATCHED, False),
        ("MINED", VenueTradeState.MINED, False),
        ("CONFIRMED", VenueTradeState.CONFIRMED, True),
        ("RETRYING", VenueTradeState.RETRYING, False),
        ("FAILED", VenueTradeState.FAILED, True),
    ),
)
def test_all_six_frozen_trade_states_are_preserved_without_secret_owners(
    wire_state: str,
    expected_state: VenueTradeState,
    terminal: bool,
) -> None:
    frame = _trade_frame(wire_state)

    event = parse_user_event(frame, receipt_time=NOW)

    assert event.normalized_state is expected_state
    assert event.original_venue_state == wire_state
    assert event.terminal is terminal
    assert event.raw_event_hash == sha256(frame).hexdigest()
    assert event.venue_timestamp == datetime(2026, 6, 29, 17, 15, 57, 257000, tzinfo=UTC)
    assert event.received_at == NOW
    assert event.sequence_number is None
    assert event.intent_id is None
    assert "owner" not in event.model_dump(mode="json")


@pytest.mark.parametrize(
    ("wire_type", "status", "size_matched", "expected_state", "terminal"),
    (
        ("PLACEMENT", "LIVE", "0", VenueOrderState.ACK_LIVE_UNEXPECTED, False),
        ("UPDATE", "LIVE", "4.5", VenueOrderState.PARTIALLY_FILLED, False),
        ("UPDATE", "MATCHED", "10", VenueOrderState.FILLED, True),
        ("CANCELLATION", "CANCELED", "4.5", VenueOrderState.CANCELLED, True),
    ),
)
def test_order_type_status_and_size_map_only_to_the_ruled_states(
    wire_type: str,
    status: str,
    size_matched: str,
    expected_state: VenueOrderState,
    terminal: bool,
) -> None:
    frame = _order_frame(wire_type, status=status, size_matched=size_matched)

    event = parse_user_event(frame, receipt_time=NOW)

    assert isinstance(event, VenueOrderEvent)
    assert event.normalized_state is expected_state
    assert event.original_venue_state == status
    assert event.terminal is terminal
    assert event.raw_event_hash == sha256(frame).hexdigest()
    assert event.venue_timestamp == datetime(2026, 6, 29, 17, 15, 57, 257000, tzinfo=UTC)
    assert event.sequence_number is None
    assert event.intent_id is None
    assert "owner" not in event.model_dump(mode="json")


@pytest.mark.parametrize(
    "frame",
    (
        _trade_frame().replace(b"{", b'{"event_type":"trade",', 1),
        _trade_frame().replace(b'"bucket_index":0', b'"bucket_index":NaN'),
    ),
    ids=("duplicate-key", "nonfinite-constant"),
)
def test_ambiguous_json_is_rejected_with_one_context_free_code(frame: bytes) -> None:
    captured: UserStreamProtocolError | None = None
    try:
        parse_user_event(frame, receipt_time=NOW)
    except UserStreamProtocolError as error:
        captured = error

    assert captured is not None
    assert str(captured) == "USER_STREAM_PROTOCOL_ERROR"
    assert repr(captured) == "UserStreamProtocolError('USER_STREAM_PROTOCOL_ERROR')"
    assert captured.__cause__ is None
    assert captured.__context__ is None


@pytest.mark.parametrize(
    ("wire_type", "status", "size_matched"),
    (
        ("PLACEMENT", "MATCHED", "0"),
        ("PLACEMENT", "LIVE", "1"),
        ("UPDATE", "LIVE", "0"),
        ("UPDATE", "LIVE", "11"),
        ("UPDATE", "CANCELED", "4.5"),
        ("CANCELLATION", "LIVE", "4.5"),
        ("CANCELLATION", "CANCELED", "11"),
        ("UPDATE", "DELAYED", "4.5"),
        ("UPDATE", "UNMATCHED", "4.5"),
    ),
)
def test_every_other_order_combination_fails_closed_without_exception_context(
    wire_type: str,
    status: str,
    size_matched: str,
) -> None:
    captured: UserStreamProtocolError | None = None
    try:
        parse_user_event(
            _order_frame(wire_type, status=status, size_matched=size_matched),
            receipt_time=NOW,
        )
    except UserStreamProtocolError as error:
        captured = error

    assert captured is not None
    assert str(captured) == "USER_STREAM_PROTOCOL_ERROR"
    assert captured.__cause__ is None
    assert captured.__context__ is None


@pytest.mark.parametrize(
    "frame",
    (
        "not-bytes",
        bytearray(b"{}"),
        b"",
        b"{",
        b"[]",
        b"x" * (MAX_USER_STREAM_MESSAGE_BYTES + 1),
        _changed_trade_frame(status="UNKNOWN"),
        _changed_trade_frame(owner=7),
        _changed_trade_frame(unreviewed_field="value"),
        _changed_trade_frame(trade_owner="owner-control-canary\n"),
    ),
    ids=(
        "string",
        "bytearray",
        "empty",
        "malformed",
        "top-level-array",
        "oversized",
        "unknown-state",
        "wrong-owner-type",
        "unknown-field",
        "invalid-secret-owner",
    ),
)
def test_malformed_unknown_oversized_or_nonbytes_frames_fail_closed(frame: object) -> None:
    captured: UserStreamProtocolError | None = None
    try:
        parse_user_event(frame, receipt_time=NOW)  # type: ignore[arg-type]
    except UserStreamProtocolError as error:
        captured = error

    assert captured is not None
    assert str(captured) == "USER_STREAM_PROTOCOL_ERROR"
    assert "owner-control-canary" not in str(captured)
    assert captured.__cause__ is None
    assert captured.__context__ is None


def test_event_identity_is_deterministic_and_raw_evidence_bound() -> None:
    frame = _trade_frame("MATCHED")

    first = parse_user_event(frame, receipt_time=NOW)
    second = parse_user_event(frame, receipt_time=NOW + timedelta(seconds=1))
    changed = parse_user_event(
        _changed_trade_frame(timestamp="1782753357258"),
        receipt_time=NOW,
    )

    assert isinstance(first, VenueTradeEvent)
    assert isinstance(second, VenueTradeEvent)
    assert isinstance(changed, VenueTradeEvent)
    assert first.trade_event_id == second.trade_event_id
    assert first.trade_event_id != changed.trade_event_id


def test_identity_retention_is_bounded_without_evicting_duplicate_evidence() -> None:
    parser = UserStreamParser(
        connected_at=NOW,
        monotonic=lambda: 0.0,
        max_event_identities=1,
    )
    first = _trade_frame("MATCHED")
    assert parser.parse(first, receipt_time=NOW) is not None
    assert parser.parse(first, receipt_time=NOW) is None

    with pytest.raises(UserStreamProtocolError, match=r"^USER_STREAM_PROTOCOL_ERROR$"):
        parser.parse(
            _changed_trade_frame(id="trade-2", timestamp="1782753357258"),
            receipt_time=NOW + timedelta(seconds=1),
        )

    assert parser.health.kill_reason == "USER_STREAM_PROTOCOL_ERROR"
    assert "identities=1" in repr(parser)


def test_exact_duplicate_is_idempotent_but_conflicting_identity_kills() -> None:
    parser = UserStreamParser(
        connected_at=NOW,
        monotonic=lambda: 0.0,
        max_event_identities=10,
    )
    frame = _trade_frame("MATCHED")

    first = parser.parse(frame, receipt_time=NOW)
    duplicate = parser.parse(frame, receipt_time=NOW + timedelta(seconds=1))

    assert first is not None
    assert duplicate is None
    assert parser.health.kill_reason is None

    secret_canary = "conflicting-owner-canary"
    conflicting = json.loads(frame)
    conflicting["trade_owner"] = secret_canary
    conflicting_frame = json.dumps(
        conflicting,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    with pytest.raises(UserStreamProtocolError, match=r"^USER_STREAM_PROTOCOL_ERROR$"):
        parser.parse(conflicting_frame, receipt_time=NOW + timedelta(seconds=2))

    assert parser.health.kill_reason == "USER_STREAM_PROTOCOL_ERROR"
    assert parser.health.required_reads == (
        RouteKey.READ_OPEN_ORDERS,
        RouteKey.READ_TRADES,
        RouteKey.READ_BALANCE_ALLOWANCE,
    )
    assert secret_canary not in repr(parser)
    assert secret_canary not in repr(parser.health)


def test_older_wire_timestamp_for_same_kind_and_id_is_a_chronology_contradiction() -> None:
    parser = UserStreamParser(
        connected_at=NOW,
        monotonic=lambda: 0.0,
        max_event_identities=10,
    )
    parser.parse(_trade_frame("MATCHED"), receipt_time=NOW)

    with pytest.raises(UserStreamProtocolError, match=r"^USER_STREAM_PROTOCOL_ERROR$"):
        parser.parse(
            _changed_trade_frame(timestamp="1782753357256", status="MINED"),
            receipt_time=NOW + timedelta(seconds=1),
        )

    assert parser.health.kill_reason == "USER_STREAM_PROTOCOL_ERROR"


def test_valid_event_advances_health_without_resetting_fixed_ping_cadence() -> None:
    ticks = iter((0.0, 5.0))
    parser = UserStreamParser(
        connected_at=NOW,
        monotonic=lambda: next(ticks),
        max_event_identities=10,
    )

    event = parser.parse(_trade_frame("MATCHED"), receipt_time=NOW + timedelta(seconds=5))

    assert event is not None
    assert parser.health.monotonic_at == 5.0
    assert parser.health.next_ping_at == 10.0
    assert parser.health.pong_deadline_at is None


def test_transport_reported_unknown_gap_kills_before_more_events_can_emit() -> None:
    ticks = iter((0.0, 1.0))
    parser = UserStreamParser(
        connected_at=NOW,
        monotonic=lambda: next(ticks),
        max_event_identities=10,
    )

    parser.on_gap(observed_at=NOW + timedelta(seconds=1))

    assert parser.health.kill_reason == "USER_STREAM_PROTOCOL_ERROR"
    assert parser.health.required_reads == (
        RouteKey.READ_OPEN_ORDERS,
        RouteKey.READ_TRADES,
        RouteKey.READ_BALANCE_ALLOWANCE,
    )
    with pytest.raises(UserStreamProtocolError, match=r"^USER_STREAM_PROTOCOL_ERROR$"):
        parser.parse(_trade_frame("MATCHED"), receipt_time=NOW + timedelta(seconds=2))


@pytest.mark.parametrize("bad_clock", (float("nan"), RuntimeError("clock-canary")))
def test_gap_with_invalid_or_failed_clock_still_fails_closed(bad_clock: object) -> None:
    calls = 0

    def monotonic() -> float:
        nonlocal calls
        calls += 1
        if calls == 1:
            return 0.0
        if isinstance(bad_clock, Exception):
            raise bad_clock
        return bad_clock  # type: ignore[return-value]

    parser = UserStreamParser(
        connected_at=NOW,
        monotonic=monotonic,
        max_event_identities=10,
    )

    parser.on_gap(observed_at=NOW + timedelta(seconds=1))

    assert parser.health.kill_reason == "USER_STREAM_PROTOCOL_ERROR"
    assert parser.health.monotonic_at == 0.0


def test_disconnect_requires_authoritative_reads_before_resume() -> None:
    health = UserStreamHealth.connected(NOW).on_disconnect(NOW + timedelta(seconds=3))

    assert health.kill_reason == "USER_STREAM_DISCONNECTED"
    assert recovery_reads_after_stream_gap(health) == (
        RouteKey.READ_OPEN_ORDERS,
        RouteKey.READ_TRADES,
        RouteKey.READ_BALANCE_ALLOWANCE,
    )


def test_reconnect_cannot_resume_until_all_three_authoritative_reads_complete() -> None:
    disconnected = UserStreamHealth.connected(NOW, monotonic_at=0).on_disconnect(
        NOW + timedelta(seconds=3),
        monotonic_at=3,
    )

    reconnected = disconnected.on_reconnect(
        NOW + timedelta(seconds=4),
        monotonic_at=4,
    )

    assert reconnected.status == "RECOVERY_REQUIRED"
    assert reconnected.kill_reason == "USER_STREAM_DISCONNECTED"
    with pytest.raises(ValueError, match=r"^USER_STREAM_RECOVERY_READS_INCOMPLETE$"):
        reconnected.on_authoritative_reads_completed(
            (RouteKey.READ_OPEN_ORDERS, RouteKey.READ_TRADES),
            observed_at=NOW + timedelta(seconds=5),
            monotonic_at=5,
        )

    recovered = reconnected.on_authoritative_reads_completed(
        (
            RouteKey.READ_OPEN_ORDERS,
            RouteKey.READ_TRADES,
            RouteKey.READ_BALANCE_ALLOWANCE,
        ),
        observed_at=NOW + timedelta(seconds=5),
        monotonic_at=5,
    )

    assert recovered.status == "CONNECTED"
    assert recovered.kill_reason is None
    assert recovered.required_reads == ()
    assert recovered.next_ping_at == 15.0


def test_ping_pong_uses_one_fixed_ten_second_cadence() -> None:
    connected = UserStreamHealth.connected(NOW, monotonic_at=100)

    assert connected.ping_due(109.999) is False
    assert connected.ping_due(110) is True
    waiting = connected.on_ping_sent(
        b"PING",
        observed_at=NOW + timedelta(seconds=10),
        monotonic_at=110,
    )
    observed = waiting.on_event_observed(
        observed_at=NOW + timedelta(seconds=15),
        monotonic_at=115,
    )

    assert observed.pong_deadline_at == 120.0
    assert observed.next_ping_at == 120.0

    ponged = observed.on_pong(
        b"PONG",
        observed_at=NOW + timedelta(seconds=19),
        monotonic_at=119,
    )

    assert ponged.pong_deadline_at is None
    assert ponged.next_ping_at == 120.0
    assert ponged.kill_reason is None


def test_missed_ping_or_pong_deadline_requires_the_three_recovery_reads() -> None:
    missed_ping = UserStreamHealth.connected(NOW, monotonic_at=0).check_deadlines(
        observed_at=NOW + timedelta(seconds=10),
        monotonic_at=10,
    )
    waiting = UserStreamHealth.connected(NOW, monotonic_at=0).on_ping_sent(
        b"PING",
        observed_at=NOW + timedelta(seconds=10),
        monotonic_at=10,
    )
    missed_pong = waiting.check_deadlines(
        observed_at=NOW + timedelta(seconds=20),
        monotonic_at=20,
    )

    assert missed_ping.kill_reason == "USER_STREAM_PING_MISSED"
    assert missed_pong.kill_reason == "USER_STREAM_PONG_MISSED"
    assert (
        missed_ping.required_reads
        == missed_pong.required_reads
        == (
            RouteKey.READ_OPEN_ORDERS,
            RouteKey.READ_TRADES,
            RouteKey.READ_BALANCE_ALLOWANCE,
        )
    )


@pytest.mark.parametrize("bad_monotonic", (-1, float("nan"), float("inf"), 9.0))
def test_invalid_or_regressing_monotonic_observation_fails_closed(bad_monotonic: float) -> None:
    waiting = UserStreamHealth.connected(NOW, monotonic_at=10)

    failed = waiting.on_event_observed(
        observed_at=NOW + timedelta(seconds=1),
        monotonic_at=bad_monotonic,
    )

    assert failed.kill_reason == "USER_STREAM_PROTOCOL_ERROR"
    assert failed.required_reads == (
        RouteKey.READ_OPEN_ORDERS,
        RouteKey.READ_TRADES,
        RouteKey.READ_BALANCE_ALLOWANCE,
    )


@pytest.mark.parametrize(
    ("status", "kill_reason"),
    (
        ("UNREVIEWED", "USER_STREAM_PROTOCOL_ERROR"),
        ("RECOVERY_REQUIRED", "UNREVIEWED_REASON"),
    ),
)
def test_health_state_and_kill_reason_are_closed_constants(
    status: str,
    kill_reason: str,
) -> None:
    with pytest.raises(ValueError, match=r"^USER_STREAM_HEALTH_INVALID$"):
        UserStreamHealth(
            status=status,  # type: ignore[arg-type]
            observed_at=NOW,
            monotonic_at=0.0,
            kill_reason=kill_reason,  # type: ignore[arg-type]
            required_reads=(
                RouteKey.READ_OPEN_ORDERS,
                RouteKey.READ_TRADES,
                RouteKey.READ_BALANCE_ALLOWANCE,
            ),
            next_ping_at=10.0,
            pong_deadline_at=None,
        )


def test_private_signer_session_sends_exact_subscription_once_and_returns_only_hash() -> None:
    private_key = bytearray(b"k" * 32)
    api_key = bytearray(b"api-key-canary")
    api_secret = bytearray(b"api-secret-canary")
    passphrase = bytearray(b"passphrase-canary")
    secrets = SecretMaterial(private_key, api_key, api_secret, passphrase)
    frames: list[bytes] = []
    guard_calls: list[datetime] = []

    class FakeUserSubscriptionTransport:
        def send_user_subscription(self, frame: bytes) -> None:
            frames.append(frame)

    def read_guard(observed_at: datetime) -> AuthorityDecision:
        guard_calls.append(observed_at)
        return AuthorityDecision(True, None, ())

    session = user_stream_module._SignerUserStreamSession(
        secrets=secrets,
        transport=FakeUserSubscriptionTransport(),
        read_guard=read_guard,
    )
    first = session.open(observed_at=NOW)
    second = session.open(observed_at=NOW + timedelta(seconds=1))
    expected_frame = (
        b'{"auth":{"apiKey":"api-key-canary","passphrase":"passphrase-canary",'
        b'"secret":"api-secret-canary"},"type":"user"}'
    )

    assert len(frames) == 1
    if sha256(frames[0]).hexdigest() != sha256(expected_frame).hexdigest():
        raise AssertionError("USER_SUBSCRIPTION_FRAME_MISMATCH") from None
    assert guard_calls == [NOW]
    assert first == second
    assert first.frame_hash == sha256(expected_frame).hexdigest()
    assert first.protocol_version == "polymarket-clob-2026-08-25-v1"
    assert first.observed_at == NOW
    assert not hasattr(first, "frame")
    assert not hasattr(session, "send")
    assert not hasattr(session, "build")
    for canary in (api_key, api_secret, passphrase):
        assert bytes(canary) not in repr(first).encode()
        assert bytes(canary) not in repr(session).encode()
    secrets.close()


def test_uncertain_subscription_send_is_not_retried_or_reflected() -> None:
    secrets = SecretMaterial(
        bytearray(b"k" * 32),
        bytearray(b"api-key-canary"),
        bytearray(b"api-secret-canary"),
        bytearray(b"passphrase-canary"),
    )
    send_calls = 0

    class FailingTransport:
        def send_user_subscription(self, frame: bytes) -> None:
            nonlocal send_calls
            send_calls += 1
            if not frame:
                raise AssertionError("FRAME_REQUIRED")
            raise RuntimeError("subscription-transport-canary")

    session = user_stream_module._SignerUserStreamSession(
        secrets=secrets,
        transport=FailingTransport(),
        read_guard=lambda observed_at: AuthorityDecision(True, None, ()),
    )

    for _ in range(2):
        captured: UserStreamProtocolError | None = None
        try:
            session.open(observed_at=NOW)
        except UserStreamProtocolError as error:
            captured = error
        assert captured is not None
        assert str(captured) == "USER_STREAM_PROTOCOL_ERROR"
        assert "subscription-transport-canary" not in str(captured)
        assert captured.__cause__ is None
        assert captured.__context__ is None

    assert send_calls == 1
    secrets.close()


@pytest.mark.parametrize("guard_mode", ("denied", "wrong-type", "exception"))
def test_subscription_read_guard_fails_before_secret_frame_construction(
    guard_mode: str,
) -> None:
    secrets = SecretMaterial(
        bytearray(b"k" * 32),
        bytearray(b"api-key-canary"),
        bytearray(b"api-secret-canary"),
        bytearray(b"passphrase-canary"),
    )
    send_calls = 0

    class RejectUnexpectedSend:
        def send_user_subscription(self, frame: bytes) -> None:
            del frame
            nonlocal send_calls
            send_calls += 1

    def read_guard(observed_at: datetime) -> object:
        del observed_at
        if guard_mode == "exception":
            raise RuntimeError("read-guard-canary")
        if guard_mode == "wrong-type":
            return True
        return AuthorityDecision(False, "EXECUTION_KILL_ENGAGED", ())

    session = user_stream_module._SignerUserStreamSession(
        secrets=secrets,
        transport=RejectUnexpectedSend(),
        read_guard=read_guard,
    )

    with pytest.raises(UserStreamProtocolError, match=r"^USER_STREAM_PROTOCOL_ERROR$") as rejected:
        session.open(observed_at=NOW)

    assert send_calls == 0
    assert "read-guard-canary" not in str(rejected.value)
    assert rejected.value.__cause__ is None
    assert rejected.value.__context__ is None
    secrets.close()
