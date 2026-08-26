"""Conservative Polymarket order-heartbeat state classification."""

from __future__ import annotations

from dataclasses import dataclass
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


@dataclass(frozen=True, slots=True)
class HeartbeatState:
    status: Literal["INITIAL", "CONFIRMED", "UNCERTAIN"]
    observed_at: datetime | None
    heartbeat_id: str | None
    evidence_hashes: tuple[Sha256, ...]
    kill_reason: Literal["HEARTBEAT_CANCELLATION_UNCERTAIN"] | None
    required_reads: tuple[RouteKey, ...]

    def __post_init__(self) -> None:
        if self.status not in {"INITIAL", "CONFIRMED", "UNCERTAIN"}:
            raise ValueError("HEARTBEAT_STATE_INVALID") from None
        if (
            type(self.evidence_hashes) is not tuple
            or any(
                type(value) is not str
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
                for value in self.evidence_hashes
            )
            or self.evidence_hashes != tuple(sorted(set(self.evidence_hashes)))
        ):
            raise ValueError("HEARTBEAT_STATE_INVALID") from None
        if self.heartbeat_id is not None and (
            type(self.heartbeat_id) is not str
            or not 1 <= len(self.heartbeat_id) <= 256
            or any(
                ord(character) < 0x20 or ord(character) > 0x7E for character in self.heartbeat_id
            )
        ):
            raise ValueError("HEARTBEAT_STATE_INVALID") from None
        if self.observed_at is not None and (
            type(self.observed_at) is not datetime
            or normalize_utc_timestamp(self.observed_at) != self.observed_at
        ):
            raise ValueError("HEARTBEAT_STATE_INVALID") from None
        if self.status == "INITIAL":
            if any(
                value
                for value in (
                    self.observed_at,
                    self.heartbeat_id,
                    self.evidence_hashes,
                    self.kill_reason,
                    self.required_reads,
                )
            ):
                raise ValueError("HEARTBEAT_STATE_INVALID") from None
        elif self.status == "CONFIRMED":
            if (
                self.observed_at is None
                or not self.heartbeat_id
                or self.kill_reason is not None
                or self.required_reads
            ):
                raise ValueError("HEARTBEAT_STATE_INVALID") from None
        elif (
            self.observed_at is None
            or self.kill_reason != "HEARTBEAT_CANCELLATION_UNCERTAIN"
            or self.required_reads != _RECOVERY_READS
        ):
            raise ValueError("HEARTBEAT_STATE_INVALID") from None

    @classmethod
    def initial(cls) -> HeartbeatState:
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
        return cls(
            status="CONFIRMED",
            observed_at=normalize_utc_timestamp(observed_at),
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
        return cls(
            status="UNCERTAIN",
            observed_at=normalize_utc_timestamp(observed_at),
            heartbeat_id=previous_heartbeat_id,
            evidence_hashes=tuple(sorted(set(evidence_hashes))),
            kill_reason="HEARTBEAT_CANCELLATION_UNCERTAIN",
            required_reads=_RECOVERY_READS,
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
    evidence_hashes = tuple(
        value for value in (result.raw_body_hash, result.request_body_hash) if value is not None
    )
    if result.code is RestCode.HEARTBEAT_ACCEPTED:
        if not isinstance(result.payload, HeartbeatAckPayload):
            raise ValueError("HEARTBEAT_RESULT_INVALID") from None
        return HeartbeatState.confirmed(
            observed_at=observed_at,
            heartbeat_id=result.payload.heartbeat_id,
            evidence_hashes=evidence_hashes,
        )
    return HeartbeatState.uncertain(
        observed_at=observed_at,
        previous_heartbeat_id=previous.heartbeat_id,
        evidence_hashes=evidence_hashes,
    )


__all__ = [
    "HeartbeatState",
    "classify_heartbeat",
    "recovery_reads_after_heartbeat_failure",
]
