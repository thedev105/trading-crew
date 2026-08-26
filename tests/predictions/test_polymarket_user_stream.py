import copy
import json
import pickle
from datetime import UTC, datetime, timedelta, tzinfo
from hashlib import sha256
from threading import Event, Thread

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


class _HostileTimezone(tzinfo):
    def utcoffset(self, value: datetime | None) -> timedelta:
        del value
        raise RuntimeError("public-time-control-canary")

    def dst(self, value: datetime | None) -> timedelta:
        del value
        return timedelta(0)


HOSTILE_TIME = datetime(2026, 8, 25, 16, tzinfo=_HostileTimezone())


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


@pytest.mark.parametrize("maker_order_count", (0, 1, 2))
def test_maker_trade_never_binds_the_counterparty_or_owner_derived_order(
    maker_order_count: int,
) -> None:
    wire = json.loads(_trade_frame("MATCHED"))
    maker_order = wire["maker_orders"][0]
    owner_canary = "maker-owner-control-canary"
    wire.update(
        trader_side="MAKER",
        taker_order_id="counterparty-taker-order",
        owner=owner_canary,
        trade_owner=owner_canary,
        maker_orders=[
            {
                **maker_order,
                "order_id": f"account-maker-order-{index}",
                "owner": owner_canary,
            }
            for index in range(maker_order_count)
        ],
    )
    frame = json.dumps(wire, separators=(",", ":"), sort_keys=True).encode()

    event = parse_user_event(frame, receipt_time=NOW)

    assert isinstance(event, VenueTradeEvent)
    assert event.venue_order_id is None
    rendered = event.model_dump_json()
    assert "counterparty-taker-order" not in rendered
    assert "account-maker-order" not in rendered
    assert owner_canary not in rendered


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


def test_private_wire_models_deny_construct_and_revalidate_copy_bypasses() -> None:
    wire = user_stream_module._validated_wire(_trade_frame("MATCHED"))

    with pytest.raises(
        ValueError,
        match=r"^USER_STREAM_WIRE_CONSTRUCTION_INVALID$",
    ):
        type(wire).model_construct(
            event_type="not-trade",
            owner=object(),
            trade_owner=object(),
        )
    for update in (
        {"event_type": "not-trade"},
        {"owner": object()},
        {"maker_orders": []},
    ):
        with pytest.raises(ValueError):
            wire.model_copy(update=update)
    copied = wire.model_copy()
    assert type(copied) is type(wire)
    assert copied == wire


def test_private_wire_models_deny_post_definition_subclasses() -> None:
    with pytest.raises(TypeError, match=r"^USER_STREAM_WIRE_NOT_SUBCLASSABLE$"):

        class ForkedTradeWire(user_stream_module._TradeWire):
            pass


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


def test_regressing_gap_clock_installs_recovery_before_later_events() -> None:
    ticks = iter((10.0, 5.0, 11.0))
    parser = UserStreamParser(
        connected_at=NOW,
        monotonic=lambda: next(ticks),
        max_event_identities=10,
    )

    parser.on_gap(observed_at=NOW + timedelta(seconds=1))

    assert parser.health.status == "RECOVERY_REQUIRED"
    assert parser.health.kill_reason == "USER_STREAM_PROTOCOL_ERROR"
    assert parser.health.monotonic_at == 10.0
    with pytest.raises(UserStreamProtocolError, match=r"^USER_STREAM_PROTOCOL_ERROR$"):
        parser.parse(_trade_frame("MATCHED"), receipt_time=NOW + timedelta(seconds=2))


def test_regressing_invalid_frame_clock_installs_recovery_before_later_events() -> None:
    ticks = iter((10.0, 5.0, 11.0))
    parser = UserStreamParser(
        connected_at=NOW,
        monotonic=lambda: next(ticks),
        max_event_identities=10,
    )

    with pytest.raises(UserStreamProtocolError, match=r"^USER_STREAM_PROTOCOL_ERROR$"):
        parser.parse(b"{", receipt_time=NOW + timedelta(seconds=1))

    assert parser.health.status == "RECOVERY_REQUIRED"
    assert parser.health.kill_reason == "USER_STREAM_PROTOCOL_ERROR"
    assert parser.health.monotonic_at == 10.0
    with pytest.raises(UserStreamProtocolError, match=r"^USER_STREAM_PROTOCOL_ERROR$"):
        parser.parse(_trade_frame("MATCHED"), receipt_time=NOW + timedelta(seconds=2))


def test_constructor_clock_exception_is_one_context_free_task9_error() -> None:
    def failing_clock() -> float:
        raise RuntimeError("constructor-clock-control-canary")

    captured: UserStreamProtocolError | None = None
    try:
        UserStreamParser(
            connected_at=NOW,
            monotonic=failing_clock,
            max_event_identities=10,
        )
    except UserStreamProtocolError as error:
        captured = error

    assert captured is not None
    assert str(captured) == "USER_STREAM_PROTOCOL_ERROR"
    assert "constructor-clock-control-canary" not in str(captured)
    assert captured.__cause__ is None
    assert captured.__context__ is None


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


def test_disconnect_reads_before_reconnect_remain_closed_and_are_idempotent() -> None:
    disconnected = UserStreamHealth.connected(NOW, monotonic_at=0).on_disconnect(
        NOW + timedelta(seconds=1),
        monotonic_at=1,
    )
    reads = (
        RouteKey.READ_OPEN_ORDERS,
        RouteKey.READ_TRADES,
        RouteKey.READ_BALANCE_ALLOWANCE,
    )

    reads_first = disconnected.on_authoritative_reads_completed(
        reads,
        observed_at=NOW + timedelta(seconds=2),
        monotonic_at=2,
    )
    duplicate_reads = reads_first.on_authoritative_reads_completed(
        reads,
        observed_at=NOW + timedelta(seconds=3),
        monotonic_at=3,
    )

    assert reads_first.status == duplicate_reads.status == "RECOVERY_REQUIRED"
    assert reads_first.kill_reason == duplicate_reads.kill_reason == "USER_STREAM_DISCONNECTED"
    recovered = duplicate_reads.on_reconnect(
        NOW + timedelta(seconds=4),
        monotonic_at=4,
    )
    assert recovered.status == "CONNECTED"
    assert recovered.kill_reason is None
    assert (
        recovered.on_reconnect(
            NOW + timedelta(seconds=5),
            monotonic_at=5,
        )
        is recovered
    )


def test_disconnect_reconnect_before_reads_remains_closed_and_is_idempotent() -> None:
    disconnected = UserStreamHealth.connected(NOW, monotonic_at=0).on_disconnect(
        NOW + timedelta(seconds=1),
        monotonic_at=1,
    )

    reconnect_first = disconnected.on_reconnect(
        NOW + timedelta(seconds=2),
        monotonic_at=2,
    )
    duplicate_reconnect = reconnect_first.on_reconnect(
        NOW + timedelta(seconds=3),
        monotonic_at=3,
    )

    assert reconnect_first.status == duplicate_reconnect.status == "RECOVERY_REQUIRED"
    recovered = duplicate_reconnect.on_authoritative_reads_completed(
        (
            RouteKey.READ_OPEN_ORDERS,
            RouteKey.READ_TRADES,
            RouteKey.READ_BALANCE_ALLOWANCE,
        ),
        observed_at=NOW + timedelta(seconds=4),
        monotonic_at=4,
    )
    assert recovered.status == "CONNECTED"
    assert recovered.kill_reason is None
    assert (
        recovered.on_authoritative_reads_completed(
            (
                RouteKey.READ_OPEN_ORDERS,
                RouteKey.READ_TRADES,
                RouteKey.READ_BALANCE_ALLOWANCE,
            ),
            observed_at=NOW + timedelta(seconds=5),
            monotonic_at=5,
        )
        is recovered
    )


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


@pytest.mark.parametrize(
    ("monotonic_at", "expected"),
    ((9.999, False), (10.0, True), (10.001, True)),
)
def test_ping_due_uses_the_ruled_exact_cadence_boundary(
    monotonic_at: float,
    expected: bool,
) -> None:
    connected = UserStreamHealth.connected(NOW, monotonic_at=0)

    assert connected.ping_due(monotonic_at) is expected


@pytest.mark.parametrize(
    ("monotonic_at", "expected_kill"),
    (
        (9.999, "USER_STREAM_PROTOCOL_ERROR"),
        (10.0, None),
        (10.001, "USER_STREAM_PING_MISSED"),
    ),
)
def test_ping_send_uses_the_ruled_exact_cadence_boundary(
    monotonic_at: float,
    expected_kill: str | None,
) -> None:
    connected = UserStreamHealth.connected(NOW, monotonic_at=0)

    result = connected.on_ping_sent(
        b"PING",
        observed_at=NOW + timedelta(seconds=monotonic_at),
        monotonic_at=monotonic_at,
    )

    assert result.kill_reason == expected_kill


@pytest.mark.parametrize(
    ("monotonic_at", "expected_kill"),
    ((9.999, None), (10.0, None), (10.001, "USER_STREAM_PING_MISSED")),
)
def test_ping_timer_and_event_observation_share_the_ruled_boundary(
    monotonic_at: float,
    expected_kill: str | None,
) -> None:
    connected = UserStreamHealth.connected(NOW, monotonic_at=0)

    timer = connected.check_deadlines(
        observed_at=NOW + timedelta(seconds=monotonic_at),
        monotonic_at=monotonic_at,
    )
    event = connected.on_event_observed(
        observed_at=NOW + timedelta(seconds=monotonic_at),
        monotonic_at=monotonic_at,
    )

    assert timer.kill_reason == event.kill_reason == expected_kill


@pytest.mark.parametrize(
    ("monotonic_at", "expected_kill"),
    (
        (19.999, None),
        (20.0, "USER_STREAM_PONG_MISSED"),
        (20.001, "USER_STREAM_PONG_MISSED"),
    ),
)
def test_pong_receive_uses_the_ruled_strict_deadline_boundary(
    monotonic_at: float,
    expected_kill: str | None,
) -> None:
    waiting = UserStreamHealth.connected(NOW, monotonic_at=0).on_ping_sent(
        b"PING",
        observed_at=NOW + timedelta(seconds=10),
        monotonic_at=10,
    )

    result = waiting.on_pong(
        b"PONG",
        observed_at=NOW + timedelta(seconds=monotonic_at),
        monotonic_at=monotonic_at,
    )

    assert result.kill_reason == expected_kill


@pytest.mark.parametrize(
    ("monotonic_at", "expected_kill"),
    (
        (19.999, None),
        (20.0, "USER_STREAM_PONG_MISSED"),
        (20.001, "USER_STREAM_PONG_MISSED"),
    ),
)
def test_pong_timer_and_event_observation_share_the_ruled_boundary(
    monotonic_at: float,
    expected_kill: str | None,
) -> None:
    waiting = UserStreamHealth.connected(NOW, monotonic_at=0).on_ping_sent(
        b"PING",
        observed_at=NOW + timedelta(seconds=10),
        monotonic_at=10,
    )

    timer = waiting.check_deadlines(
        observed_at=NOW + timedelta(seconds=monotonic_at),
        monotonic_at=monotonic_at,
    )
    event = waiting.on_event_observed(
        observed_at=NOW + timedelta(seconds=monotonic_at),
        monotonic_at=monotonic_at,
    )

    assert timer.kill_reason == event.kill_reason == expected_kill


@pytest.mark.parametrize(
    ("frame_kind", "monotonic_at", "expected_kill"),
    (
        ("PING", 10.001, "USER_STREAM_PING_MISSED"),
        ("PONG", 20.0, "USER_STREAM_PONG_MISSED"),
        ("PONG", 20.001, "USER_STREAM_PONG_MISSED"),
    ),
)
def test_first_deadline_kill_is_absorbing_across_sequential_callback_orders(
    frame_kind: str,
    monotonic_at: float,
    expected_kill: str,
) -> None:
    initial = UserStreamHealth.connected(NOW, monotonic_at=0)
    if frame_kind == "PONG":
        initial = initial.on_ping_sent(
            b"PING",
            observed_at=NOW + timedelta(seconds=10),
            monotonic_at=10,
        )
    frame = b"PING" if frame_kind == "PING" else b"PONG"

    timer_first = initial.check_deadlines(
        observed_at=NOW + timedelta(seconds=monotonic_at),
        monotonic_at=monotonic_at,
    )
    timer_then_frame = (
        timer_first.on_ping_sent(
            frame,
            observed_at=NOW + timedelta(seconds=monotonic_at),
            monotonic_at=monotonic_at,
        )
        if frame_kind == "PING"
        else timer_first.on_pong(
            frame,
            observed_at=NOW + timedelta(seconds=monotonic_at),
            monotonic_at=monotonic_at,
        )
    )

    frame_first = (
        initial.on_ping_sent(
            frame,
            observed_at=NOW + timedelta(seconds=monotonic_at),
            monotonic_at=monotonic_at,
        )
        if frame_kind == "PING"
        else initial.on_pong(
            frame,
            observed_at=NOW + timedelta(seconds=monotonic_at),
            monotonic_at=monotonic_at,
        )
    )
    frame_then_timer = frame_first.check_deadlines(
        observed_at=NOW + timedelta(seconds=monotonic_at),
        monotonic_at=monotonic_at,
    )

    assert timer_first.kill_reason == frame_first.kill_reason == expected_kill
    assert timer_then_frame.kill_reason == frame_then_timer.kill_reason == expected_kill
    assert timer_then_frame is timer_first
    assert frame_then_timer is frame_first


def test_missed_ping_or_pong_deadline_requires_the_three_recovery_reads() -> None:
    missed_ping = UserStreamHealth.connected(NOW, monotonic_at=0).check_deadlines(
        observed_at=NOW + timedelta(seconds=10.001),
        monotonic_at=10.001,
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


@pytest.mark.parametrize(
    "updates",
    (
        {"required_reads": []},
        {"monotonic_at": 0},
        {"next_ping_at": 1e100},
        {"monotonic_at": 1e20, "next_ping_at": 1e20},
        {"pong_deadline_at": 1.0},
        {
            "status": "RECOVERY_REQUIRED",
            "kill_reason": "USER_STREAM_PROTOCOL_ERROR",
            "required_reads": (
                RouteKey.READ_OPEN_ORDERS,
                RouteKey.READ_TRADES,
                RouteKey.READ_BALANCE_ALLOWANCE,
            ),
            "pong_deadline_at": 5.0,
            "recovery_phase": "READS_REQUIRED",
        },
    ),
    ids=(
        "mutable-read-list",
        "non-float-monotonic",
        "ping-beyond-one-cadence",
        "unrepresentable-ping-cadence",
        "pong-does-not-match-cadence",
        "recovery-with-pending-pong",
    ),
)
def test_user_stream_health_direct_constructor_rejects_impossible_or_mutable_state(
    updates: dict[str, object],
) -> None:
    fields: dict[str, object] = {
        "status": "CONNECTED",
        "observed_at": NOW,
        "monotonic_at": 0.0,
        "kill_reason": None,
        "required_reads": (),
        "next_ping_at": 10.0,
        "pong_deadline_at": None,
        "recovery_phase": "NONE",
    }
    fields.update(updates)

    with pytest.raises(ValueError, match=r"^USER_STREAM_HEALTH_INVALID$"):
        UserStreamHealth(**fields)  # type: ignore[arg-type]


def test_user_stream_health_denies_subclass_state_variants() -> None:
    with pytest.raises(TypeError, match=r"^USER_STREAM_HEALTH_NOT_SUBCLASSABLE$"):

        class ForkedHealth(UserStreamHealth):
            pass


def test_user_stream_health_factories_and_transitions_return_strict_immutable_values() -> None:
    connected = UserStreamHealth.connected(NOW, monotonic_at=0)
    waiting = connected.on_ping_sent(
        b"PING",
        observed_at=NOW + timedelta(seconds=10),
        monotonic_at=10,
    )
    killed = waiting.on_disconnect(NOW + timedelta(seconds=11), monotonic_at=11)

    for health in (connected, waiting, killed):
        assert type(health) is UserStreamHealth
        assert type(health.monotonic_at) is float
        assert type(health.next_ping_at) is float
        assert type(health.required_reads) is tuple
    assert waiting.pong_deadline_at == waiting.next_ping_at == 20.0
    assert killed.pong_deadline_at is None


def test_user_stream_health_initialization_is_one_shot_for_every_alias() -> None:
    health = UserStreamHealth.connected(NOW, monotonic_at=0).on_disconnect(
        NOW + timedelta(seconds=1),
        monotonic_at=1,
    )
    alias = health
    captured: ValueError | None = None

    try:
        health.__init__(
            status="CONNECTED",
            observed_at=NOW + timedelta(seconds=2),
            monotonic_at=2.0,
            kill_reason=None,
            required_reads=(),
            next_ping_at=12.0,
            pong_deadline_at=None,
            recovery_phase="NONE",
        )
    except ValueError as error:
        captured = error

    assert alias.status == "RECOVERY_REQUIRED"
    assert alias.kill_reason == "USER_STREAM_DISCONNECTED"
    assert captured is not None
    assert str(captured) == "USER_STREAM_HEALTH_INVALID"
    assert captured.__cause__ is None
    assert captured.__context__ is None


@pytest.mark.parametrize("monotonic_at", (float(2**54 - 8), 1e20))
def test_connected_health_rejects_an_unrepresentable_ten_second_cadence(
    monotonic_at: float,
) -> None:
    with pytest.raises(ValueError, match=r"^USER_STREAM_HEALTH_INVALID$") as rejected:
        UserStreamHealth.connected(NOW, monotonic_at=monotonic_at)

    assert rejected.value.__cause__ is None
    assert rejected.value.__context__ is None


def test_cadence_arithmetic_failure_installs_recovery_at_float_boundary() -> None:
    monotonic_at = float(2**54 - 10)
    connected = UserStreamHealth.connected(NOW, monotonic_at=monotonic_at)

    assert connected.next_ping_at - connected.monotonic_at == 10.0
    failed = connected.on_ping_sent(
        b"PING",
        observed_at=NOW + timedelta(seconds=10),
        monotonic_at=connected.next_ping_at,
    )

    assert connected.status == "CONNECTED"
    assert failed.status == "RECOVERY_REQUIRED"
    assert failed.kill_reason == "USER_STREAM_PROTOCOL_ERROR"
    assert failed.required_reads == (
        RouteKey.READ_OPEN_ORDERS,
        RouteKey.READ_TRADES,
        RouteKey.READ_BALANCE_ALLOWANCE,
    )


def test_health_factory_and_transition_hide_hostile_timezone_callbacks() -> None:
    connected = UserStreamHealth.connected(NOW, monotonic_at=0)

    for operation in (
        lambda: UserStreamHealth.connected(HOSTILE_TIME, monotonic_at=0),
        lambda: connected.on_disconnect(HOSTILE_TIME, monotonic_at=1),
    ):
        captured: ValueError | None = None
        try:
            operation()
        except ValueError as error:
            captured = error

        assert captured is not None
        assert str(captured) == "USER_STREAM_HEALTH_INVALID"
        assert "public-time-control-canary" not in str(captured)
        assert captured.__cause__ is None
        assert captured.__context__ is None
        assert connected.status == "CONNECTED"


def test_parser_construction_hides_hostile_timezone_callbacks() -> None:
    captured: UserStreamProtocolError | None = None

    try:
        UserStreamParser(connected_at=HOSTILE_TIME, monotonic=lambda: 0.0)
    except UserStreamProtocolError as error:
        captured = error

    assert captured is not None
    assert str(captured) == "USER_STREAM_PROTOCOL_ERROR"
    assert "public-time-control-canary" not in str(captured)
    assert captured.__cause__ is None
    assert captured.__context__ is None


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


def test_private_signer_session_denies_state_copy_serialization_and_subclass_forks() -> None:
    secrets = SecretMaterial(
        bytearray(b"k" * 32),
        bytearray(b"api-key-canary"),
        bytearray(b"api-secret-canary"),
        bytearray(b"passphrase-canary"),
    )

    class RejectUnexpectedSend:
        def send_user_subscription(self, frame: bytes) -> None:
            del frame
            raise AssertionError("UNEXPECTED_SEND")

    session = user_stream_module._SignerUserStreamSession(
        secrets=secrets,
        transport=RejectUnexpectedSend(),
        read_guard=lambda observed_at: AuthorityDecision(True, None, ()),
    )

    for operation in (
        lambda: copy.copy(session),
        lambda: copy.deepcopy(session),
        lambda: pickle.dumps(session),
        session.__reduce__,
        lambda: session.__reduce_ex__(pickle.HIGHEST_PROTOCOL),
        session.__getstate__,
    ):
        with pytest.raises(UserStreamProtocolError, match=r"^USER_STREAM_PROTOCOL_ERROR$"):
            operation()
    with pytest.raises(AttributeError, match=r"^USER_SUBSCRIPTION_SESSION_IMMUTABLE$"):
        session._attempted = False
    for attribute in ("_transport", "_attempted", "not_a_session_attribute"):
        with pytest.raises(AttributeError, match=r"^USER_SUBSCRIPTION_SESSION_IMMUTABLE$"):
            delattr(session, attribute)
    with pytest.raises(TypeError, match=r"^USER_SUBSCRIPTION_SESSION_NOT_SUBCLASSABLE$"):

        class ForkedSession(user_stream_module._SignerUserStreamSession):
            pass

    secrets.close()


def test_subscription_owner_rejects_wrong_thread_calls_without_another_send() -> None:
    secrets = SecretMaterial(
        bytearray(b"k" * 32),
        bytearray(b"api-key-canary"),
        bytearray(b"api-secret-canary"),
        bytearray(b"passphrase-canary"),
    )
    frames: list[bytes] = []
    errors: list[BaseException] = []

    class RecordingTransport:
        def send_user_subscription(self, frame: bytes) -> None:
            frames.append(frame)

    session = user_stream_module._SignerUserStreamSession(
        secrets=secrets,
        transport=RecordingTransport(),
        read_guard=lambda observed_at: AuthorityDecision(True, None, ()),
    )
    evidence = session.open(observed_at=NOW)

    def open_session() -> None:
        try:
            session.open(observed_at=NOW)
        except BaseException as error:
            errors.append(error)

    first = Thread(target=open_session)
    second = Thread(target=open_session)
    first.start()
    second.start()
    first.join(timeout=2)
    second.join(timeout=2)

    assert not first.is_alive()
    assert not second.is_alive()
    assert len(frames) == 1
    assert evidence is session.open(observed_at=NOW + timedelta(seconds=1))
    assert len(errors) == 2
    assert all(type(error) is UserStreamProtocolError for error in errors)
    assert all(str(error) == "USER_STREAM_PROTOCOL_ERROR" for error in errors)
    secrets.close()


def test_cross_thread_subscription_reentry_fails_before_transport_waits() -> None:
    secrets = SecretMaterial(
        bytearray(b"k" * 32),
        bytearray(b"api-key-canary"),
        bytearray(b"api-secret-canary"),
        bytearray(b"passphrase-canary"),
    )
    callback_finished = Event()
    callback_finished_during_send: list[bool] = []
    callback_errors: list[BaseException] = []
    callback_threads: list[Thread] = []
    session: user_stream_module._SignerUserStreamSession

    class CrossThreadReentrantTransport:
        def send_user_subscription(self, frame: bytes) -> None:
            del frame

            def reenter() -> None:
                try:
                    session.open(observed_at=NOW)
                except BaseException as error:
                    callback_errors.append(error)
                finally:
                    callback_finished.set()

            callback = Thread(target=reenter)
            callback_threads.append(callback)
            callback.start()
            callback_finished_during_send.append(callback_finished.wait(timeout=0.2))

    session = user_stream_module._SignerUserStreamSession(
        secrets=secrets,
        transport=CrossThreadReentrantTransport(),
        read_guard=lambda observed_at: AuthorityDecision(True, None, ()),
    )

    evidence = session.open(observed_at=NOW)
    for callback in callback_threads:
        callback.join(timeout=2)

    assert evidence.frame_hash
    assert callback_finished_during_send == [True]
    assert len(callback_errors) == 1
    assert type(callback_errors[0]) is UserStreamProtocolError
    secrets.close()


def test_subscription_initialization_is_one_shot_after_success_or_failure() -> None:
    for transport_fails in (False, True):
        secrets = SecretMaterial(
            bytearray(b"k" * 32),
            bytearray(b"api-key-canary"),
            bytearray(b"api-secret-canary"),
            bytearray(b"passphrase-canary"),
        )
        send_calls = 0

        class OneOutcomeTransport:
            def send_user_subscription(
                self,
                frame: bytes,
                transport_fails: bool = transport_fails,
            ) -> None:
                del frame
                nonlocal send_calls
                send_calls += 1
                if transport_fails:
                    raise RuntimeError("subscription-reinit-canary")

        transport = OneOutcomeTransport()

        def guard(observed_at: datetime) -> AuthorityDecision:
            del observed_at
            return AuthorityDecision(True, None, ())

        session = user_stream_module._SignerUserStreamSession(
            secrets=secrets,
            transport=transport,
            read_guard=guard,
        )
        if transport_fails:
            with pytest.raises(UserStreamProtocolError):
                session.open(observed_at=NOW)
        else:
            session.open(observed_at=NOW)

        with pytest.raises(UserStreamProtocolError, match=r"^USER_STREAM_PROTOCOL_ERROR$"):
            session.__init__(secrets=secrets, transport=transport, read_guard=guard)
        if transport_fails:
            with pytest.raises(UserStreamProtocolError):
                session.open(observed_at=NOW)
        else:
            session.open(observed_at=NOW)

        assert send_calls == 1
        secrets.close()


def test_subscription_transport_reentrancy_cannot_send_a_second_frame() -> None:
    secrets = SecretMaterial(
        bytearray(b"k" * 32),
        bytearray(b"api-key-canary"),
        bytearray(b"api-secret-canary"),
        bytearray(b"passphrase-canary"),
    )
    send_calls = 0
    reentrant_errors: list[UserStreamProtocolError] = []
    session: user_stream_module._SignerUserStreamSession

    class ReentrantTransport:
        def send_user_subscription(self, frame: bytes) -> None:
            del frame
            nonlocal send_calls
            send_calls += 1
            try:
                session.open(observed_at=NOW)
            except UserStreamProtocolError as error:
                reentrant_errors.append(error)

    session = user_stream_module._SignerUserStreamSession(
        secrets=secrets,
        transport=ReentrantTransport(),
        read_guard=lambda observed_at: AuthorityDecision(True, None, ()),
    )

    first = session.open(observed_at=NOW)
    second = session.open(observed_at=NOW + timedelta(seconds=1))

    assert first is second
    assert send_calls == 1
    assert len(reentrant_errors) == 1
    assert str(reentrant_errors[0]) == "USER_STREAM_PROTOCOL_ERROR"
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
