"""Conservative Polymarket order-heartbeat state classification."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from polytrading.predictions.domain import Sha256, normalize_utc_timestamp
from polytrading.predictions.polymarket_execution.rest import RestResult
from polytrading.predictions.polymarket_execution.routes import (
    HeartbeatAckPayload,
    RestCode,
    RouteKey,
)

_RECOVERY_READS = (
    RouteKey.READ_OPEN_ORDERS,
    RouteKey.READ_TRADES,
    RouteKey.READ_BALANCE_ALLOWANCE,
)


def _normalize_heartbeat_datetime(value: object, *, error_code: str) -> datetime:
    invalid = type(value) is not datetime
    normalized: datetime | None = None
    if not invalid:
        try:
            normalized = normalize_utc_timestamp(value)
        except Exception:
            invalid = True
    if invalid or normalized is None:
        raise ValueError(error_code) from None
    return normalized


@dataclass(frozen=True, slots=True, init=False)
class HeartbeatState:
    status: Literal["INITIAL", "CONFIRMED", "UNCERTAIN", "RECOVERED"]
    observed_at: datetime | None
    heartbeat_id: str | None
    evidence_hashes: tuple[Sha256, ...]
    kill_reason: Literal["HEARTBEAT_CANCELLATION_UNCERTAIN"] | None
    required_reads: tuple[RouteKey, ...]
    _initialized: bool = field(init=False, repr=False, compare=False)

    def __init__(
        self,
        status: Literal["INITIAL", "CONFIRMED", "UNCERTAIN", "RECOVERED"],
        observed_at: datetime | None,
        heartbeat_id: str | None,
        evidence_hashes: tuple[Sha256, ...],
        kill_reason: Literal["HEARTBEAT_CANCELLATION_UNCERTAIN"] | None,
        required_reads: tuple[RouteKey, ...],
    ) -> None:
        initialized = False
        try:
            object.__getattribute__(self, "_initialized")
        except AttributeError:
            initialized = False
        else:
            initialized = True
        if initialized:
            raise ValueError("HEARTBEAT_STATE_INVALID") from None
        normalized_observed_at = (
            None
            if observed_at is None
            else _normalize_heartbeat_datetime(
                observed_at,
                error_code="HEARTBEAT_STATE_INVALID",
            )
        )
        object.__setattr__(self, "_initialized", True)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "observed_at", normalized_observed_at)
        object.__setattr__(self, "heartbeat_id", heartbeat_id)
        object.__setattr__(self, "evidence_hashes", evidence_hashes)
        object.__setattr__(self, "kill_reason", kill_reason)
        object.__setattr__(self, "required_reads", required_reads)
        self.__post_init__()

    def __init_subclass__(cls, **kwargs: object) -> None:
        del cls, kwargs
        raise TypeError("HEARTBEAT_STATE_NOT_SUBCLASSABLE") from None

    def __post_init__(self) -> None:
        invalid = (
            type(self) is not HeartbeatState
            or type(self.status) is not str
            or type(self.observed_at) not in {datetime, type(None)}
            or type(self.heartbeat_id) not in {str, type(None)}
            or type(self.evidence_hashes) is not tuple
            or any(type(value) is not str for value in self.evidence_hashes)
            or type(self.kill_reason) not in {str, type(None)}
            or type(self.required_reads) is not tuple
            or any(type(route) is not RouteKey for route in self.required_reads)
        )
        if not invalid:
            invalid = (
                self.status not in {"INITIAL", "CONFIRMED", "UNCERTAIN", "RECOVERED"}
                or len(self.evidence_hashes) > 2
                or any(
                    len(value) != 64
                    or any(character not in "0123456789abcdef" for character in value)
                    for value in self.evidence_hashes
                )
                or self.evidence_hashes != tuple(sorted(set(self.evidence_hashes)))
                or (
                    self.heartbeat_id is not None
                    and (
                        not 1 <= len(self.heartbeat_id) <= 256
                        or any(
                            ord(character) < 0x20 or ord(character) > 0x7E
                            for character in self.heartbeat_id
                        )
                    )
                )
            )
        if not invalid and self.status == "INITIAL":
            invalid = (
                self.observed_at is not None
                or self.heartbeat_id is not None
                or self.evidence_hashes != ()
                or self.kill_reason is not None
                or self.required_reads != ()
            )
        elif not invalid and self.status == "CONFIRMED":
            invalid = (
                self.observed_at is None
                or self.heartbeat_id is None
                or len(self.evidence_hashes) not in {1, 2}
                or self.kill_reason is not None
                or self.required_reads != ()
            )
        elif not invalid and self.status == "RECOVERED":
            invalid = (
                self.observed_at is None
                or self.kill_reason is not None
                or self.required_reads != ()
            )
        elif not invalid:
            invalid = (
                self.observed_at is None
                or self.kill_reason != "HEARTBEAT_CANCELLATION_UNCERTAIN"
                or self.required_reads != _RECOVERY_READS
            )
        if invalid:
            raise ValueError("HEARTBEAT_STATE_INVALID") from None

    @classmethod
    def initial(cls) -> HeartbeatState:
        if cls is not HeartbeatState:
            raise TypeError("HEARTBEAT_STATE_NOT_SUBCLASSABLE") from None
        return cls(
            status="INITIAL",
            observed_at=None,
            heartbeat_id=None,
            evidence_hashes=(),
            kill_reason=None,
            required_reads=(),
        )

    @classmethod
    def confirmed(
        cls,
        *,
        observed_at: datetime,
        heartbeat_id: str,
        evidence_hashes: tuple[Sha256, ...],
    ) -> HeartbeatState:
        if cls is not HeartbeatState or type(evidence_hashes) is not tuple:
            raise ValueError("HEARTBEAT_STATE_INVALID") from None
        return cls(
            status="CONFIRMED",
            observed_at=_normalize_heartbeat_datetime(
                observed_at,
                error_code="HEARTBEAT_STATE_INVALID",
            ),
            heartbeat_id=heartbeat_id,
            evidence_hashes=tuple(sorted(set(evidence_hashes))),
            kill_reason=None,
            required_reads=(),
        )

    @classmethod
    def uncertain(
        cls,
        *,
        observed_at: datetime,
        previous_heartbeat_id: str | None,
        evidence_hashes: tuple[Sha256, ...],
    ) -> HeartbeatState:
        if cls is not HeartbeatState or type(evidence_hashes) is not tuple:
            raise ValueError("HEARTBEAT_STATE_INVALID") from None
        return cls(
            status="UNCERTAIN",
            observed_at=_normalize_heartbeat_datetime(
                observed_at,
                error_code="HEARTBEAT_STATE_INVALID",
            ),
            heartbeat_id=previous_heartbeat_id,
            evidence_hashes=tuple(sorted(set(evidence_hashes))),
            kill_reason="HEARTBEAT_CANCELLATION_UNCERTAIN",
            required_reads=_RECOVERY_READS,
        )

    @classmethod
    def recovered(
        cls,
        *,
        observed_at: datetime,
        heartbeat_id: str | None,
        evidence_hashes: tuple[Sha256, ...],
    ) -> HeartbeatState:
        if cls is not HeartbeatState or type(evidence_hashes) is not tuple:
            raise ValueError("HEARTBEAT_STATE_INVALID") from None
        return cls(
            status="RECOVERED",
            observed_at=_normalize_heartbeat_datetime(
                observed_at,
                error_code="HEARTBEAT_STATE_INVALID",
            ),
            heartbeat_id=heartbeat_id,
            evidence_hashes=tuple(sorted(set(evidence_hashes))),
            kill_reason=None,
            required_reads=(),
        )

    def on_authoritative_reads_completed(
        self,
        reads: tuple[RouteKey, ...],
        *,
        observed_at: datetime,
    ) -> HeartbeatState:
        if self.status != "UNCERTAIN" or reads != _RECOVERY_READS:
            raise ValueError("HEARTBEAT_RECOVERY_READS_INCOMPLETE") from None
        return HeartbeatState.recovered(
            observed_at=observed_at,
            heartbeat_id=self.heartbeat_id,
            evidence_hashes=self.evidence_hashes,
        )


def recovery_reads_after_heartbeat_failure() -> tuple[RouteKey, ...]:
    return _RECOVERY_READS


def classify_heartbeat(
    previous: HeartbeatState,
    result: RestResult,
    observed_at: datetime,
) -> HeartbeatState:
    """Confirm only one strict Task 8 HEARTBEAT_ACCEPTED result."""
    if type(previous) is not HeartbeatState or type(result) is not RestResult:
        raise ValueError("HEARTBEAT_RESULT_INVALID") from None
    if result.route is not RouteKey.HEARTBEAT:
        raise ValueError("HEARTBEAT_RESULT_INVALID") from None
    normalized_observed_at = _normalize_heartbeat_datetime(
        observed_at,
        error_code="HEARTBEAT_RESULT_INVALID",
    )
    evidence_hashes = tuple(
        value for value in (result.raw_body_hash, result.request_body_hash) if value is not None
    )
    if result.code is RestCode.HEARTBEAT_ACCEPTED:
        accepted_evidence = tuple(sorted(set(evidence_hashes)))
        if type(result.payload) is not HeartbeatAckPayload or len(accepted_evidence) not in {1, 2}:
            raise ValueError("HEARTBEAT_RESULT_INVALID") from None
        if previous.status == "UNCERTAIN":
            return previous
        return HeartbeatState.confirmed(
            observed_at=normalized_observed_at,
            heartbeat_id=result.payload.heartbeat_id,
            evidence_hashes=accepted_evidence,
        )
    return HeartbeatState.uncertain(
        observed_at=normalized_observed_at,
        previous_heartbeat_id=previous.heartbeat_id,
        evidence_hashes=evidence_hashes,
    )


__all__ = [
    "HeartbeatState",
    "classify_heartbeat",
    "recovery_reads_after_heartbeat_failure",
]
