from datetime import UTC, datetime, timedelta, tzinfo

import pytest

from polytrading.predictions.polymarket_execution.heartbeat import (
    HeartbeatState,
    classify_heartbeat,
    recovery_reads_after_heartbeat_failure,
)
from polytrading.predictions.polymarket_execution.rest import RestResult, sanitize_venue_error
from polytrading.predictions.polymarket_execution.routes import (
    HeartbeatAckPayload,
    RestCode,
    RouteKey,
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


class _FailAfterTimezone(tzinfo):
    def __init__(self, allowed_calls: int) -> None:
        self._allowed_calls = allowed_calls
        self.calls = 0

    def utcoffset(self, value: datetime | None) -> timedelta:
        del value
        self.calls += 1
        if self.calls > self._allowed_calls:
            raise RuntimeError(f"stateful-time-control-canary-{self.calls}")
        return timedelta(0)

    def dst(self, value: datetime | None) -> timedelta:
        del value
        return timedelta(0)


def _fail_after_time(allowed_calls: int) -> tuple[datetime, _FailAfterTimezone]:
    timezone = _FailAfterTimezone(allowed_calls)
    return datetime(2026, 8, 25, 16, tzinfo=timezone), timezone


def test_accepted_heartbeat_confirms_only_the_returned_identifier() -> None:
    previous = HeartbeatState.initial()
    result = RestResult(
        route=RouteKey.HEARTBEAT,
        code=RestCode.HEARTBEAT_ACCEPTED,
        observed_at=NOW,
        raw_body_hash="a" * 64,
        request_body_hash="b" * 64,
        attempts=1,
        recovery_required=False,
        kill_required=False,
        payload=HeartbeatAckPayload(
            kind="HEARTBEAT_ACK",
            heartbeat_id="heartbeat-1",
        ),
    )

    state = classify_heartbeat(previous, result, NOW)

    assert state.status == "CONFIRMED"
    assert state.heartbeat_id == "heartbeat-1"
    assert state.kill_reason is None


@pytest.mark.parametrize(
    ("raw_body_hash", "request_body_hash", "expected_evidence"),
    (
        (None, "b" * 64, ("b" * 64,)),
        ("a" * 64, "a" * 64, ("a" * 64,)),
    ),
    ids=("absent-raw-hash", "equal-request-and-raw-hashes"),
)
def test_every_valid_accepted_heartbeat_evidence_shape_confirms(
    raw_body_hash: str | None,
    request_body_hash: str,
    expected_evidence: tuple[str, ...],
) -> None:
    result = RestResult(
        route=RouteKey.HEARTBEAT,
        code=RestCode.HEARTBEAT_ACCEPTED,
        observed_at=NOW,
        raw_body_hash=raw_body_hash,
        request_body_hash=request_body_hash,
        attempts=1,
        recovery_required=False,
        kill_required=False,
        payload=HeartbeatAckPayload(
            kind="HEARTBEAT_ACK",
            heartbeat_id="heartbeat-valid-evidence-shape",
        ),
    )

    state = classify_heartbeat(HeartbeatState.initial(), result, NOW)

    assert state.status == "CONFIRMED"
    assert state.heartbeat_id == "heartbeat-valid-evidence-shape"
    assert state.evidence_hashes == expected_evidence


def _accepted_result(heartbeat_id: str = "heartbeat-previous") -> RestResult:
    return RestResult(
        route=RouteKey.HEARTBEAT,
        code=RestCode.HEARTBEAT_ACCEPTED,
        observed_at=NOW,
        raw_body_hash="a" * 64,
        request_body_hash="b" * 64,
        attempts=1,
        recovery_required=False,
        kill_required=False,
        payload=HeartbeatAckPayload(
            kind="HEARTBEAT_ACK",
            heartbeat_id=heartbeat_id,
        ),
    )


def _failed_result(code: RestCode) -> RestResult:
    build_failed = code is RestCode.AUTH_REQUEST_BUILD_FAILED
    mismatch = code is RestCode.HEARTBEAT_ID_MISMATCH
    return RestResult(
        route=RouteKey.HEARTBEAT,
        code=code,
        observed_at=NOW,
        raw_body_hash=None if build_failed else "c" * 64,
        request_body_hash=None if build_failed else "d" * 64,
        attempts=0 if build_failed else 1,
        recovery_required=True,
        kill_required=not mismatch,
        payload=(
            HeartbeatAckPayload(
                kind="HEARTBEAT_ACK",
                heartbeat_id="heartbeat-expected-by-venue",
            )
            if mismatch
            else None
        ),
    )


@pytest.mark.parametrize(
    "code",
    (
        RestCode.HEARTBEAT_ID_MISMATCH,
        RestCode.HEARTBEAT_OUTCOME_UNKNOWN,
        RestCode.AUTH_REJECTED,
        RestCode.AUTH_REQUEST_BUILD_FAILED,
    ),
)
def test_every_nonaccepted_task8_heartbeat_result_is_cancellation_uncertainty(
    code: RestCode,
) -> None:
    previous = classify_heartbeat(HeartbeatState.initial(), _accepted_result(), NOW)

    state = classify_heartbeat(previous, _failed_result(code), NOW)

    assert state.status == "UNCERTAIN"
    assert state.heartbeat_id == "heartbeat-previous"
    assert state.kill_reason == "HEARTBEAT_CANCELLATION_UNCERTAIN"
    assert (
        state.required_reads
        == recovery_reads_after_heartbeat_failure()
        == (
            RouteKey.READ_OPEN_ORDERS,
            RouteKey.READ_TRADES,
            RouteKey.READ_BALANCE_ALLOWANCE,
        )
    )


@pytest.mark.parametrize(
    "code",
    (
        RestCode.HEARTBEAT_ID_MISMATCH,
        RestCode.HEARTBEAT_OUTCOME_UNKNOWN,
        RestCode.AUTH_REJECTED,
        RestCode.AUTH_REQUEST_BUILD_FAILED,
    ),
)
def test_later_accepted_heartbeat_cannot_clear_each_failure_family(
    code: RestCode,
) -> None:
    confirmed = classify_heartbeat(
        HeartbeatState.initial(),
        _accepted_result("heartbeat-before-uncertainty"),
        NOW,
    )
    uncertain = classify_heartbeat(confirmed, _failed_result(code), NOW)

    later = classify_heartbeat(
        uncertain,
        _accepted_result("heartbeat-after-uncertainty"),
        NOW,
    )

    assert later.status == "UNCERTAIN"
    assert later.heartbeat_id == "heartbeat-before-uncertainty"
    assert later.kill_reason == "HEARTBEAT_CANCELLATION_UNCERTAIN"
    assert later.required_reads == (
        RouteKey.READ_OPEN_ORDERS,
        RouteKey.READ_TRADES,
        RouteKey.READ_BALANCE_ALLOWANCE,
    )


@pytest.mark.parametrize(
    "reads",
    (
        (RouteKey.READ_OPEN_ORDERS, RouteKey.READ_TRADES),
        (
            RouteKey.READ_OPEN_ORDERS,
            RouteKey.READ_TRADES,
            RouteKey.READ_TRADES,
        ),
        (
            RouteKey.READ_TRADES,
            RouteKey.READ_OPEN_ORDERS,
            RouteKey.READ_BALANCE_ALLOWANCE,
        ),
        (
            RouteKey.READ_OPEN_ORDERS,
            RouteKey.READ_TRADES,
            RouteKey.READ_BALANCE_ALLOWANCE,
            RouteKey.READ_ORDER,
        ),
    ),
    ids=("incomplete", "duplicate", "wrong-order", "additional"),
)
def test_heartbeat_recovery_rejects_any_nonexact_read_tuple(
    reads: tuple[RouteKey, ...],
) -> None:
    confirmed = classify_heartbeat(
        HeartbeatState.initial(),
        _accepted_result("heartbeat-before-uncertainty"),
        NOW,
    )
    uncertain = classify_heartbeat(
        confirmed,
        _failed_result(RestCode.HEARTBEAT_OUTCOME_UNKNOWN),
        NOW,
    )

    with pytest.raises(ValueError, match=r"^HEARTBEAT_RECOVERY_READS_INCOMPLETE$"):
        uncertain.on_authoritative_reads_completed(reads, observed_at=NOW)


def test_exact_heartbeat_recovery_precedes_a_new_confirmation() -> None:
    confirmed = classify_heartbeat(
        HeartbeatState.initial(),
        _accepted_result("heartbeat-before-uncertainty"),
        NOW,
    )
    uncertain = classify_heartbeat(
        confirmed,
        _failed_result(RestCode.HEARTBEAT_OUTCOME_UNKNOWN),
        NOW,
    )

    recovered = uncertain.on_authoritative_reads_completed(
        (
            RouteKey.READ_OPEN_ORDERS,
            RouteKey.READ_TRADES,
            RouteKey.READ_BALANCE_ALLOWANCE,
        ),
        observed_at=NOW,
    )

    assert recovered.status == "RECOVERED"
    assert recovered.heartbeat_id == "heartbeat-before-uncertainty"
    assert recovered.kill_reason is None
    assert recovered.required_reads == ()
    after_recovery = classify_heartbeat(
        recovered,
        _accepted_result("heartbeat-after-recovery"),
        NOW,
    )
    assert after_recovery.status == "CONFIRMED"
    assert after_recovery.heartbeat_id == "heartbeat-after-recovery"


def test_heartbeat_state_rejects_unreviewed_status_even_with_plausible_fields() -> None:
    with pytest.raises(ValueError, match=r"^HEARTBEAT_STATE_INVALID$"):
        HeartbeatState(
            status="UNREVIEWED",  # type: ignore[arg-type]
            observed_at=NOW,
            heartbeat_id="heartbeat-previous",
            evidence_hashes=(),
            kill_reason="HEARTBEAT_CANCELLATION_UNCERTAIN",
            required_reads=(
                RouteKey.READ_OPEN_ORDERS,
                RouteKey.READ_TRADES,
                RouteKey.READ_BALANCE_ALLOWANCE,
            ),
        )


@pytest.mark.parametrize(
    ("heartbeat_id", "evidence_hashes"),
    (
        ("heartbeat-control-canary", ("not-a-sha256",)),
        ("", ("a" * 64,)),
        ("heartbeat-control-canary\n", ("a" * 64,)),
    ),
    ids=("invalid-evidence-hash", "empty-identifier", "nonprintable-identifier"),
)
def test_confirmed_heartbeat_state_rejects_unvalidated_public_evidence(
    heartbeat_id: str,
    evidence_hashes: tuple[str, ...],
) -> None:
    with pytest.raises(ValueError, match=r"^HEARTBEAT_STATE_INVALID$"):
        HeartbeatState(
            status="CONFIRMED",
            observed_at=NOW,
            heartbeat_id=heartbeat_id,
            evidence_hashes=evidence_hashes,
            kill_reason=None,
            required_reads=(),
        )


@pytest.mark.parametrize(
    "updates",
    (
        {"required_reads": []},
        {"evidence_hashes": ("a" * 64, "b" * 64, "c" * 64)},
        {"evidence_hashes": ["a" * 64, "b" * 64]},
    ),
    ids=("mutable-read-list", "unbounded-evidence", "mutable-evidence-list"),
)
def test_heartbeat_direct_constructor_rejects_mutable_or_unbounded_state(
    updates: dict[str, object],
) -> None:
    fields: dict[str, object] = {
        "status": "CONFIRMED",
        "observed_at": NOW,
        "heartbeat_id": "heartbeat-confirmed",
        "evidence_hashes": ("a" * 64, "b" * 64),
        "kill_reason": None,
        "required_reads": (),
    }
    fields.update(updates)

    with pytest.raises(ValueError, match=r"^HEARTBEAT_STATE_INVALID$"):
        HeartbeatState(**fields)  # type: ignore[arg-type]


def test_heartbeat_state_denies_subclass_state_variants() -> None:
    with pytest.raises(TypeError, match=r"^HEARTBEAT_STATE_NOT_SUBCLASSABLE$"):

        class ForkedHeartbeatState(HeartbeatState):
            pass


def test_heartbeat_factories_and_transitions_return_strict_immutable_values() -> None:
    confirmed = classify_heartbeat(
        HeartbeatState.initial(),
        _accepted_result("heartbeat-before-uncertainty"),
        NOW,
    )
    uncertain = classify_heartbeat(
        confirmed,
        _failed_result(RestCode.HEARTBEAT_OUTCOME_UNKNOWN),
        NOW,
    )
    recovered = uncertain.on_authoritative_reads_completed(
        (
            RouteKey.READ_OPEN_ORDERS,
            RouteKey.READ_TRADES,
            RouteKey.READ_BALANCE_ALLOWANCE,
        ),
        observed_at=NOW,
    )

    for state in (confirmed, uncertain, recovered):
        assert type(state) is HeartbeatState
        assert type(state.evidence_hashes) is tuple
        assert type(state.required_reads) is tuple
        assert len(state.evidence_hashes) <= 2


def test_heartbeat_state_initialization_is_one_shot_for_every_alias() -> None:
    uncertain = HeartbeatState.uncertain(
        observed_at=NOW,
        previous_heartbeat_id="heartbeat-before-uncertainty",
        evidence_hashes=("a" * 64,),
    )
    alias = uncertain
    captured: ValueError | None = None

    try:
        uncertain.__init__(
            status="RECOVERED",
            observed_at=NOW,
            heartbeat_id="heartbeat-before-uncertainty",
            evidence_hashes=("a" * 64,),
            kill_reason=None,
            required_reads=(),
        )
    except ValueError as error:
        captured = error

    assert alias.status == "UNCERTAIN"
    assert alias.kill_reason == "HEARTBEAT_CANCELLATION_UNCERTAIN"
    assert captured is not None
    assert str(captured) == "HEARTBEAT_STATE_INVALID"
    assert captured.__cause__ is None
    assert captured.__context__ is None


def test_heartbeat_factory_and_recovery_hide_hostile_timezone_callbacks() -> None:
    uncertain = HeartbeatState.uncertain(
        observed_at=NOW,
        previous_heartbeat_id="heartbeat-before-uncertainty",
        evidence_hashes=("a" * 64,),
    )
    operations = (
        lambda: HeartbeatState.confirmed(
            observed_at=HOSTILE_TIME,
            heartbeat_id="heartbeat-confirmed",
            evidence_hashes=("a" * 64, "b" * 64),
        ),
        lambda: uncertain.on_authoritative_reads_completed(
            (
                RouteKey.READ_OPEN_ORDERS,
                RouteKey.READ_TRADES,
                RouteKey.READ_BALANCE_ALLOWANCE,
            ),
            observed_at=HOSTILE_TIME,
        ),
    )

    for operation in operations:
        captured: ValueError | None = None
        try:
            operation()
        except ValueError as error:
            captured = error

        assert captured is not None
        assert str(captured) == "HEARTBEAT_STATE_INVALID"
        assert "public-time-control-canary" not in str(captured)
        assert captured.__cause__ is None
        assert captured.__context__ is None
        assert uncertain.status == "UNCERTAIN"


def test_heartbeat_classifier_hides_hostile_timezone_callbacks() -> None:
    captured: ValueError | None = None

    try:
        classify_heartbeat(HeartbeatState.initial(), _accepted_result(), HOSTILE_TIME)
    except ValueError as error:
        captured = error

    assert captured is not None
    assert str(captured) == "HEARTBEAT_RESULT_INVALID"
    assert "public-time-control-canary" not in str(captured)
    assert captured.__cause__ is None
    assert captured.__context__ is None


@pytest.mark.parametrize("allowed_calls", (2, 3))
def test_heartbeat_direct_constructor_discards_stateful_timezone_after_normalization(
    allowed_calls: int,
) -> None:
    observed_at, timezone = _fail_after_time(allowed_calls)

    state = HeartbeatState(
        status="CONFIRMED",
        observed_at=observed_at,
        heartbeat_id="heartbeat-confirmed",
        evidence_hashes=("a" * 64,),
        kill_reason=None,
        required_reads=(),
    )

    assert state.observed_at == NOW
    assert state.observed_at.tzinfo is UTC
    assert timezone.calls == 2


@pytest.mark.parametrize("allowed_calls", (0, 1))
def test_heartbeat_direct_constructor_sanitizes_stateful_timezone_normalization_failure(
    allowed_calls: int,
) -> None:
    observed_at, _ = _fail_after_time(allowed_calls)
    captured: ValueError | None = None

    try:
        HeartbeatState(
            status="CONFIRMED",
            observed_at=observed_at,
            heartbeat_id="heartbeat-confirmed",
            evidence_hashes=("a" * 64,),
            kill_reason=None,
            required_reads=(),
        )
    except ValueError as error:
        captured = error

    assert captured is not None
    assert type(captured) is ValueError
    assert str(captured) == "HEARTBEAT_STATE_INVALID"
    assert "stateful-time-control-canary" not in str(captured)
    assert captured.__cause__ is None
    assert captured.__context__ is None


@pytest.mark.parametrize("allowed_calls", (2, 3))
def test_heartbeat_factories_transitions_and_classifier_store_only_canonical_stateful_time(
    allowed_calls: int,
) -> None:
    uncertain = HeartbeatState.uncertain(
        observed_at=NOW,
        previous_heartbeat_id="heartbeat-before-uncertainty",
        evidence_hashes=("a" * 64,),
    )
    operations = (
        lambda observed_at: HeartbeatState.confirmed(
            observed_at=observed_at,
            heartbeat_id="heartbeat-confirmed",
            evidence_hashes=("a" * 64,),
        ),
        lambda observed_at: HeartbeatState.uncertain(
            observed_at=observed_at,
            previous_heartbeat_id="heartbeat-before-uncertainty",
            evidence_hashes=("a" * 64,),
        ),
        lambda observed_at: HeartbeatState.recovered(
            observed_at=observed_at,
            heartbeat_id="heartbeat-before-uncertainty",
            evidence_hashes=("a" * 64,),
        ),
        lambda observed_at: uncertain.on_authoritative_reads_completed(
            (
                RouteKey.READ_OPEN_ORDERS,
                RouteKey.READ_TRADES,
                RouteKey.READ_BALANCE_ALLOWANCE,
            ),
            observed_at=observed_at,
        ),
        lambda observed_at: classify_heartbeat(
            HeartbeatState.initial(),
            _accepted_result(),
            observed_at,
        ),
    )

    for operation in operations:
        observed_at, timezone = _fail_after_time(allowed_calls)
        state = operation(observed_at)
        assert state.observed_at == NOW
        assert state.observed_at is not None
        assert state.observed_at.tzinfo is UTC
        assert timezone.calls == 2


@pytest.mark.parametrize(
    "failure",
    (
        {"status_code": 429},
        {"status_code": 503},
        {"protocol_invalid": True, "raw_body_hash": "e" * 64},
        {},
    ),
    ids=("rate-limited", "server-unavailable", "malformed", "transport-unavailable"),
)
def test_rate_malformed_and_transport_families_never_imply_cancellation(
    failure: dict[str, object],
) -> None:
    result = sanitize_venue_error(
        route=RouteKey.HEARTBEAT,
        observed_at=NOW,
        attempts=1,
        request_body_hash="d" * 64,
        **failure,
    )

    state = classify_heartbeat(HeartbeatState.initial(), result, NOW)

    assert result.code is RestCode.HEARTBEAT_OUTCOME_UNKNOWN
    assert state.status == "UNCERTAIN"
    assert state.kill_reason == "HEARTBEAT_CANCELLATION_UNCERTAIN"
    assert state.required_reads == (
        RouteKey.READ_OPEN_ORDERS,
        RouteKey.READ_TRADES,
        RouteKey.READ_BALANCE_ALLOWANCE,
    )
