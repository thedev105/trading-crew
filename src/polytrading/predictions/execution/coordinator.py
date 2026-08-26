"""Fail-closed, offline Polymarket execution lifecycle coordination.

The coordinator owns persistence ordering and lifecycle decisions.  It owns no credential,
authenticated header, raw venue frame, arbitrary request, or transport implementation.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from hashlib import sha256
from itertools import pairwise
from threading import Lock
from types import MappingProxyType
from typing import Literal, Protocol, Self
from uuid import UUID, uuid5

from pydantic import ConfigDict, field_validator, model_validator

from polytrading.predictions.domain import PredictionRecord, Sha256, normalize_utc_timestamp
from polytrading.predictions.execution.authority import (
    AuthorityContext,
    verify_mutation_authority,
)
from polytrading.predictions.execution.kill_switch import KillState
from polytrading.predictions.execution.models import (
    ActivationEvidence,
    ExecutionIntent,
    ExecutionOperation,
    KillSwitchEvent,
    LiveExecutionPlan,
    SignedOrderEnvelope,
    VenueOrderEvent,
    VenueOrderState,
    VenueTradeEvent,
    VenueTradeState,
)
from polytrading.predictions.polymarket_execution.heartbeat import HeartbeatState
from polytrading.predictions.polymarket_execution.rest import RestResult
from polytrading.predictions.polymarket_execution.routes import (
    BalanceAllowancePayload,
    CancellationPayload,
    OrderAckPayload,
    OrderReadPayload,
    OrdersReadPayload,
    RestCode,
    RouteKey,
    TradesReadPayload,
)
from polytrading.predictions.polymarket_execution.user_stream import UserStreamHealth
from polytrading.predictions.storage.store import ConflictingRecordError, PredictionMarketStore

_COORDINATOR_NAMESPACE = UUID("9fa51eb6-2bba-46b6-9a68-fb768d408dd8")
_BASE_RECOVERY_READS = (
    RouteKey.READ_OPEN_ORDERS,
    RouteKey.READ_TRADES,
    RouteKey.READ_BALANCE_ALLOWANCE,
)


class CoordinatorCode(StrEnum):
    PREPARED = "PREPARED"
    PREFLIGHT_REFUSED = "PREFLIGHT_REFUSED"
    PREFLIGHT_EVIDENCE_INVALID = "PREFLIGHT_EVIDENCE_INVALID"
    PREFLIGHT_EVIDENCE_STALE = "PREFLIGHT_EVIDENCE_STALE"
    PREFLIGHT_IDENTITY_MISMATCH = "PREFLIGHT_IDENTITY_MISMATCH"
    PREFLIGHT_PLAN_BOUNDS_MISMATCH = "PREFLIGHT_PLAN_BOUNDS_MISMATCH"
    AUTHORITY_DENIED = "AUTHORITY_DENIED"
    EXECUTION_KILL_ENGAGED = "EXECUTION_KILL_ENGAGED"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    DUPLICATE_INTENT = "DUPLICATE_INTENT"
    INTENT_COLLISION = "INTENT_COLLISION"
    ENVELOPE_COLLISION = "ENVELOPE_COLLISION"
    SIGNER_FAILED = "SIGNER_FAILED"
    SUBMITTED = "SUBMITTED"
    ORDER_EVENT_APPLIED = "ORDER_EVENT_APPLIED"
    ORDER_EVENT_CONTRADICTION = "ORDER_EVENT_CONTRADICTION"
    RECOVERY_COMPLETE = "RECOVERY_COMPLETE"
    RECOVERY_BLOCKED = "RECOVERY_BLOCKED"


class PreflightRefusalCode(StrEnum):
    PROOF_MISSING = "PROOF_MISSING"
    PROOF_STALE = "PROOF_STALE"
    ECONOMICS_MISSING = "ECONOMICS_MISSING"
    ECONOMICS_STALE = "ECONOMICS_STALE"
    BOOK_MISSING = "BOOK_MISSING"
    BOOK_STALE = "BOOK_STALE"
    FEE_MISSING = "FEE_MISSING"
    FEE_STALE = "FEE_STALE"
    ACCOUNT_MISSING = "ACCOUNT_MISSING"
    ACCOUNT_STALE = "ACCOUNT_STALE"
    BALANCE_MISSING = "BALANCE_MISSING"
    BALANCE_STALE = "BALANCE_STALE"
    ALLOWANCE_MISSING = "ALLOWANCE_MISSING"
    ALLOWANCE_STALE = "ALLOWANCE_STALE"
    GEOBLOCK_MISSING = "GEOBLOCK_MISSING"
    GEOBLOCK_STALE = "GEOBLOCK_STALE"
    MANIFEST_INVALID = "MANIFEST_INVALID"
    ACTIVATION_INVALID = "ACTIVATION_INVALID"
    RISK_INVALID = "RISK_INVALID"
    PROTOCOL_INVALID = "PROTOCOL_INVALID"
    CAPABILITY_INVALID = "CAPABILITY_INVALID"
    SIGNER_UNAVAILABLE = "SIGNER_UNAVAILABLE"


class PostFillDecision(StrEnum):
    CONTINUE_FROZEN_PLAN = "CONTINUE_FROZEN_PLAN"
    FROZEN_UNWIND = "FROZEN_UNWIND"
    HALT_EXPOSED = "HALT_EXPOSED"


_KILL_REASONS = frozenset(
    {
        CoordinatorCode.AUTHORITY_DENIED.value,
        CoordinatorCode.DUPLICATE_INTENT.value,
        CoordinatorCode.ENVELOPE_COLLISION.value,
        CoordinatorCode.INTENT_COLLISION.value,
        CoordinatorCode.ORDER_EVENT_CONTRADICTION.value,
        CoordinatorCode.RECOVERY_BLOCKED.value,
        CoordinatorCode.SIGNER_FAILED.value,
        PostFillDecision.HALT_EXPOSED.value,
        RestCode.AUTH_REJECTED.value,
        RestCode.AUTH_REQUEST_BUILD_FAILED.value,
        RestCode.CANCEL_NOT_CONFIRMED.value,
        RestCode.CANCEL_OUTCOME_UNKNOWN.value,
        RestCode.ORDER_ACK_DELAYED.value,
        RestCode.ORDER_ACK_LIVE_UNEXPECTED.value,
        RestCode.ORDER_ACK_UNMATCHED.value,
        RestCode.ORDER_OUTCOME_UNKNOWN.value,
        RestCode.RATE_LIMITED.value,
        RestCode.PROTOCOL_RESPONSE_INVALID.value,
        RestCode.TRANSPORT_UNAVAILABLE.value,
        RestCode.READ_FAILED.value,
        "ORDER_ENVELOPE_INVALID",
        "ORDER_ENVELOPE_MISMATCH",
        "USER_STREAM_DISCONNECTED",
        "USER_STREAM_PROTOCOL_ERROR",
        "USER_STREAM_PING_MISSED",
        "USER_STREAM_PONG_MISSED",
        "HEARTBEAT_CANCELLATION_UNCERTAIN",
        "SETTLEMENT_MATCHED_NOT_BROADCASTED",
        "SETTLEMENT_MATCHED",
        "SETTLEMENT_MINED",
        "SETTLEMENT_RETRYING",
        "SETTLEMENT_FAILED",
        "RECONCILIATION_INCOMPLETE",
    }
)


def _closed_kill_reason(value: str | None) -> str | None:
    if value is not None and value not in _KILL_REASONS:
        raise ValueError("coordinator kill reason is not a closed code")
    return value


class _CoordinatorRecord(PredictionRecord):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        hide_input_in_errors=True,
    )

    @classmethod
    def model_construct(cls, _fields_set: set[str] | None = None, **values: object) -> Self:
        del _fields_set, values
        raise ValueError("COORDINATOR_MODEL_INVALID") from None

    def model_copy(
        self,
        *,
        update: dict[str, object] | None = None,
        deep: bool = False,
    ) -> Self:
        del deep
        values = self.model_dump(mode="python")
        if update is not None:
            values.update(update)
        return type(self).model_validate(values, strict=True)


class PreflightRefusal(_CoordinatorRecord):
    code: PreflightRefusalCode
    observed_at: datetime
    evidence_hashes: tuple[Sha256, ...]

    @field_validator("observed_at")
    @classmethod
    def _observed_at_utc(cls, value: datetime) -> datetime:
        return normalize_utc_timestamp(value)

    @field_validator("evidence_hashes")
    @classmethod
    def _sorted_evidence(cls, value: tuple[Sha256, ...]) -> tuple[Sha256, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("preflight evidence hashes must be sorted and unique")
        return value


class PreflightEvidence(_CoordinatorRecord):
    """Sanitized proof that the injected preflight validators completed."""

    schema_version: Literal[1]
    plan: LiveExecutionPlan
    activation_evidence: ActivationEvidence
    proof_artifact_hash: Sha256
    economics_report_hash: Sha256
    book_snapshot_ids: tuple[UUID, ...]
    fee_evidence_ids: tuple[UUID, ...]
    account_evidence_hash: Sha256
    balance_evidence_hash: Sha256
    allowance_evidence_hash: Sha256
    geoblock_evidence_hash: Sha256
    manifest_hash: Sha256
    risk_policy_hash: Sha256
    protocol_hash: Sha256
    protocol_version: str
    capability_fingerprint: Sha256
    signer_account_fingerprint: Sha256
    signer_healthy: bool
    fee_deadline: datetime
    balance_deadline: datetime
    allowance_deadline: datetime
    manifest_deadline: datetime
    activation_deadline: datetime
    risk_deadline: datetime
    protocol_deadline: datetime
    capability_deadline: datetime
    evidence_hashes: tuple[Sha256, ...]

    @field_validator(
        "fee_deadline",
        "balance_deadline",
        "allowance_deadline",
        "manifest_deadline",
        "activation_deadline",
        "risk_deadline",
        "protocol_deadline",
        "capability_deadline",
    )
    @classmethod
    def _deadline_utc(cls, value: datetime) -> datetime:
        return normalize_utc_timestamp(value)

    @field_validator("protocol_version")
    @classmethod
    def _protocol_version_bounded(cls, value: str) -> str:
        if type(value) is not str or not 1 <= len(value) <= 128 or not value.isascii():
            raise ValueError("preflight protocol version is invalid")
        return value

    @field_validator("book_snapshot_ids", "fee_evidence_ids")
    @classmethod
    def _nonempty_unique_ids(cls, value: tuple[UUID, ...]) -> tuple[UUID, ...]:
        if not value or len(value) != len(set(value)):
            raise ValueError("preflight evidence identities must be nonempty and unique")
        return value

    @field_validator("evidence_hashes")
    @classmethod
    def _sorted_evidence(cls, value: tuple[Sha256, ...]) -> tuple[Sha256, ...]:
        if not value or value != tuple(sorted(set(value))):
            raise ValueError("preflight evidence hashes must be nonempty, sorted, and unique")
        return value

    @model_validator(mode="after")
    def _activation_deadline_matches(self) -> PreflightEvidence:
        if self.activation_evidence.expires_at != self.activation_deadline:
            raise ValueError("preflight activation deadline does not match evidence")
        return self


class PreparationResult(_CoordinatorRecord):
    code: CoordinatorCode
    plan_id: UUID | None
    intent_id: UUID
    refusal_code: PreflightRefusalCode | None
    evidence_hashes: tuple[Sha256, ...]

    @field_validator("evidence_hashes")
    @classmethod
    def _sorted_evidence(cls, value: tuple[Sha256, ...]) -> tuple[Sha256, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("coordinator evidence hashes must be sorted and unique")
        return value

    @model_validator(mode="after")
    def _closed_result(self) -> PreparationResult:
        if self.code is CoordinatorCode.PREPARED:
            if self.plan_id is None or self.refusal_code is not None:
                raise ValueError("prepared result is invalid")
        elif self.refusal_code is not None and self.code is not CoordinatorCode.PREFLIGHT_REFUSED:
            raise ValueError("refusal code does not match coordinator code")
        return self


class SubmissionResult(_CoordinatorRecord):
    code: CoordinatorCode
    intent_id: UUID
    event: VenueOrderEvent | None
    kill_reason: str | None
    post_fill_decision: PostFillDecision | None = None

    @field_validator("kill_reason")
    @classmethod
    def _kill_reason_closed(cls, value: str | None) -> str | None:
        return _closed_kill_reason(value)

    @property
    def state(self) -> VenueOrderState | None:
        return None if self.event is None else self.event.normalized_state

    @model_validator(mode="after")
    def _closed_submission(self) -> SubmissionResult:
        if self.code is CoordinatorCode.SUBMITTED and self.event is None:
            raise ValueError("submitted result requires an order event")
        if self.post_fill_decision is not None and self.event is None:
            raise ValueError("post-fill decision requires an order event")
        return self


class RecoveryReport(_CoordinatorRecord):
    """Closed startup/account recovery report; populated by the recovery slice."""

    code: CoordinatorCode
    account_fingerprint: Sha256
    reads: tuple[RouteKey, ...]
    recovered_intent_ids: tuple[UUID, ...]
    blocked_intent_ids: tuple[UUID, ...]
    submit_attempts: Literal[0]
    kill_reason: str | None

    @field_validator("kill_reason")
    @classmethod
    def _kill_reason_closed(cls, value: str | None) -> str | None:
        return _closed_kill_reason(value)

    @field_validator("reads")
    @classmethod
    def _closed_reads(cls, value: tuple[RouteKey, ...]) -> tuple[RouteKey, ...]:
        suffix = value[len(_BASE_RECOVERY_READS) :]
        if (
            value[: len(_BASE_RECOVERY_READS)] != _BASE_RECOVERY_READS
            or any(route is not RouteKey.READ_ORDER for route in suffix)
            or len(suffix) > 10_000
        ):
            raise ValueError("recovery report reads are invalid")
        return value

    @model_validator(mode="after")
    def _closed_report(self) -> RecoveryReport:
        if self.code not in {
            CoordinatorCode.RECOVERY_COMPLETE,
            CoordinatorCode.RECOVERY_BLOCKED,
        }:
            raise ValueError("recovery report code is invalid")
        if (
            self.recovered_intent_ids != tuple(sorted(set(self.recovered_intent_ids)))
            or self.blocked_intent_ids != tuple(sorted(set(self.blocked_intent_ids)))
            or set(self.recovered_intent_ids) & set(self.blocked_intent_ids)
            or (self.code is CoordinatorCode.RECOVERY_COMPLETE and self.blocked_intent_ids)
        ):
            raise ValueError("recovery report intent identities are invalid")
        return self


class PreflightPort(Protocol):
    def validate(
        self,
        intent: ExecutionIntent,
        now: datetime,
    ) -> PreflightEvidence | PreflightRefusal: ...

    def revalidate_after_fill(
        self,
        plan: LiveExecutionPlan,
        intent: ExecutionIntent,
        event: VenueOrderEvent,
        now: datetime,
    ) -> PostFillDecision: ...


class SignerPort(Protocol):
    def sign(
        self,
        intent: ExecutionIntent,
        evidence: PreflightEvidence,
    ) -> SignedOrderEnvelope: ...

    def submit(
        self,
        intent: ExecutionIntent,
        envelope: SignedOrderEnvelope,
        evidence: PreflightEvidence,
    ) -> RestResult: ...

    def cancel(
        self,
        intent: ExecutionIntent,
        envelope: SignedOrderEnvelope,
        venue_order_id: str,
        evidence: PreflightEvidence,
    ) -> RestResult: ...


class AccountReadPort(Protocol):
    account_fingerprint: Sha256

    def read_open_orders(self) -> RestResult: ...

    def read_trades(self) -> RestResult: ...

    def read_balance_allowance(self) -> RestResult: ...

    def read_order(self, venue_order_id: str) -> RestResult: ...


class CoordinatorAuthorityPort(Protocol):
    def snapshot(
        self,
        intent: ExecutionIntent,
        evidence: PreflightEvidence,
        operation: ExecutionOperation,
        now: datetime,
    ) -> AuthorityContext: ...


class ExecutionCoordinator:
    """Append-only execution state machine with no production activation path."""

    __slots__ = (
        "_account_fingerprint",
        "_account_reader",
        "_active_submission_claims",
        "_authority",
        "_clock",
        "_initialized",
        "_last_received_at",
        "_preflight",
        "_prepared",
        "_recovering",
        "_signer",
        "_store",
        "_submission_claim_lock",
        "_submission_claims",
        "_test_only_kill_state",
    )

    def __init__(
        self,
        *,
        store: PredictionMarketStore,
        preflight: PreflightPort,
        signer: SignerPort,
        account_reader: AccountReadPort,
        authority: CoordinatorAuthorityPort,
        account_fingerprint: Sha256,
        clock: object,
        test_only_kill_state: KillState | None = None,
    ) -> None:
        try:
            object.__getattribute__(self, "_initialized")
        except AttributeError:
            pass
        else:
            raise ValueError("COORDINATOR_ALREADY_INITIALIZED") from None
        if (
            type(store) is not PredictionMarketStore
            or type(account_fingerprint) is not str
            or len(account_fingerprint) != 64
            or any(character not in "0123456789abcdef" for character in account_fingerprint)
            or not callable(clock)
            or (test_only_kill_state is not None and type(test_only_kill_state) is not KillState)
        ):
            raise ValueError("COORDINATOR_INITIALIZATION_INVALID") from None
        object.__setattr__(self, "_initialized", True)
        object.__setattr__(self, "_store", store)
        object.__setattr__(self, "_preflight", preflight)
        object.__setattr__(self, "_signer", signer)
        object.__setattr__(self, "_account_reader", account_reader)
        object.__setattr__(self, "_authority", authority)
        object.__setattr__(self, "_account_fingerprint", account_fingerprint)
        object.__setattr__(self, "_clock", clock)
        object.__setattr__(self, "_test_only_kill_state", test_only_kill_state)
        object.__setattr__(self, "_prepared", MappingProxyType({}))
        object.__setattr__(self, "_submission_claim_lock", Lock())
        object.__setattr__(self, "_submission_claims", set())
        object.__setattr__(self, "_active_submission_claims", set())
        object.__setattr__(self, "_recovering", False)
        object.__setattr__(self, "_last_received_at", None)

    def __delattr__(self, name: str) -> None:
        del name
        raise AttributeError("COORDINATOR_IMMUTABLE") from None

    def __setattr__(self, name: str, value: object) -> None:
        del name, value
        raise AttributeError("COORDINATOR_IMMUTABLE") from None

    def __init_subclass__(cls, **kwargs: object) -> None:
        del cls, kwargs
        raise TypeError("COORDINATOR_NOT_SUBCLASSABLE") from None

    def __copy__(self) -> ExecutionCoordinator:
        raise TypeError("COORDINATOR_STATE_COPY_DENIED") from None

    def __deepcopy__(self, memo: dict[int, object]) -> ExecutionCoordinator:
        del memo
        raise TypeError("COORDINATOR_STATE_COPY_DENIED") from None

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("COORDINATOR_STATE_COPY_DENIED") from None

    @property
    def new_intents_blocked(self) -> bool:
        """Report the durable fail-closed admission state without clearing it."""

        return self._recovering or self._killed(self._now())

    def _now(self) -> datetime:
        invalid = False
        observed: object = None
        try:
            observed = self._clock()  # type: ignore[operator]
            if type(observed) is not datetime:
                invalid = True
            else:
                observed = normalize_utc_timestamp(observed)
        except Exception:
            invalid = True
        if invalid or type(observed) is not datetime:
            raise ValueError("COORDINATOR_CLOCK_INVALID") from None
        return observed.astimezone(UTC)

    def _killed(self, now: datetime) -> bool:
        if self._store.verified_kill_switch_events(self._account_fingerprint, now):
            return True
        state = self._test_only_kill_state
        return True if state is None else state.engaged

    def _received_at(self, now: datetime) -> datetime:
        previous = self._last_received_at
        received_at = (
            now if previous is None or now > previous else previous + timedelta(microseconds=1)
        )
        object.__setattr__(self, "_last_received_at", received_at)
        return received_at

    @staticmethod
    def _safe_hash(*values: object) -> Sha256:
        payload = "\x1f".join(str(value) for value in values).encode("utf-8")
        return sha256(payload).hexdigest()

    @staticmethod
    def _synthetic_order_id(intent: ExecutionIntent) -> str:
        return f"intent:{intent.intent_id}"

    def _order_event(
        self,
        intent: ExecutionIntent,
        *,
        state: VenueOrderState,
        original_state: str,
        source_channel: str,
        venue_order_id: str,
        raw_event_hash: Sha256,
        now: datetime,
        lineage_hashes: tuple[Sha256, ...] = (),
    ) -> VenueOrderEvent:
        received_at = self._received_at(now)
        event_id = uuid5(
            _COORDINATOR_NAMESPACE,
            "|".join(
                (
                    str(intent.intent_id),
                    venue_order_id,
                    state.value,
                    original_state,
                    source_channel,
                    raw_event_hash,
                    received_at.isoformat(),
                )
            ),
        )
        return VenueOrderEvent(
            schema_version=1,
            event_id=event_id,
            venue="polymarket",
            raw_event_hash=raw_event_hash,
            source_channel=source_channel,
            venue_order_id=venue_order_id,
            intent_id=intent.intent_id,
            original_venue_state=original_state,
            normalized_state=state,
            terminal=state
            in {
                VenueOrderState.FILLED,
                VenueOrderState.CANCELLED,
                VenueOrderState.REJECTED,
                VenueOrderState.RECONCILED,
            },
            venue_timestamp=None,
            received_at=received_at,
            sequence_number=None,
            protocol_version=intent.protocol_version,
            lineage_hashes=lineage_hashes,
        )

    def _engage_kill(
        self,
        *,
        trigger: str,
        intent: ExecutionIntent | None,
        venue_order_id: str | None,
        now: datetime,
        evidence_hashes: tuple[Sha256, ...] = (),
    ) -> str:
        existing = self._store.verified_kill_switch_events(self._account_fingerprint, now)
        if existing:
            return existing[0].trigger
        event = KillSwitchEvent(
            schema_version=1,
            kill_event_id=uuid5(
                _COORDINATOR_NAMESPACE,
                f"kill|{self._account_fingerprint}|{trigger}|"
                f"{None if intent is None else intent.intent_id}",
            ),
            trigger=trigger,
            scope=self._account_fingerprint,
            source_intent_id=None if intent is None else intent.intent_id,
            source_order_id=venue_order_id,
            prior_state=False,
            occurred_at=now,
            clearance_evidence_hashes=(),
            lineage_hashes=tuple(sorted(set(evidence_hashes))),
        )
        self._store.append_kill_switch_event(event)
        return trigger

    def _authority_allowed(
        self,
        intent: ExecutionIntent,
        evidence: PreflightEvidence,
        operation: ExecutionOperation,
        now: datetime,
    ) -> bool:
        invalid = False
        context: object = None
        try:
            context = self._authority.snapshot(intent, evidence, operation, now)
        except Exception:
            invalid = True
        return (
            not invalid
            and type(context) is AuthorityContext
            and verify_mutation_authority(context, operation).allowed
        )

    @staticmethod
    def _result(
        code: CoordinatorCode,
        intent: ExecutionIntent,
        *,
        plan_id: UUID | None = None,
        refusal_code: PreflightRefusalCode | None = None,
        evidence_hashes: tuple[Sha256, ...] = (),
    ) -> PreparationResult:
        return PreparationResult(
            code=code,
            plan_id=plan_id,
            intent_id=intent.intent_id,
            refusal_code=refusal_code,
            evidence_hashes=evidence_hashes,
        )

    def _evidence_code(
        self,
        intent: ExecutionIntent,
        evidence: PreflightEvidence,
        now: datetime,
    ) -> CoordinatorCode | None:
        plan = evidence.plan
        if (
            intent.account_fingerprint != self._account_fingerprint
            or plan.venue != intent.venue
            or plan.plan_id != intent.plan_id
            or plan.account_fingerprint != intent.account_fingerprint
            or plan.capability_fingerprint != intent.capability_fingerprint
            or evidence.capability_fingerprint != intent.capability_fingerprint
            or evidence.signer_account_fingerprint != intent.account_fingerprint
            or evidence.protocol_version != intent.protocol_version
            or evidence.proof_artifact_hash != plan.proof_artifact_hash
            or evidence.economics_report_hash != plan.economics_report_hash
            or evidence.book_snapshot_ids != plan.book_snapshot_ids
            or evidence.fee_evidence_ids != plan.fee_evidence_ids
            or evidence.manifest_hash != plan.manifest_hash
            or evidence.risk_policy_hash != plan.risk_policy_hash
            or evidence.protocol_hash != plan.protocol_hash
            or evidence.activation_evidence.capability_digest != intent.capability_fingerprint
            or evidence.activation_evidence.manifest_digest != plan.manifest_hash
            or evidence.activation_evidence.verifier_result is not True
            or evidence.signer_healthy is not True
            or evidence.evidence_hashes
            != tuple(
                sorted(
                    {
                        evidence.account_evidence_hash,
                        evidence.balance_evidence_hash,
                        evidence.allowance_evidence_hash,
                        evidence.geoblock_evidence_hash,
                    }
                )
            )
        ):
            return CoordinatorCode.PREFLIGHT_IDENTITY_MISMATCH
        if (
            intent.leg_sequence >= len(plan.token_ids)
            or intent.token_id != plan.token_ids[intent.leg_sequence]
            or intent.order_type is not plan.leg_order_types[intent.leg_sequence]
            or intent.limit_price != plan.limit_prices[intent.leg_sequence]
            or intent.fee_rate_bps_cap > plan.fee_rate_bps_caps[intent.leg_sequence]
            or (
                plan.maximum_size is not None
                and intent.base_size is not None
                and intent.base_size > plan.maximum_size
            )
            or (
                plan.maximum_spend is not None
                and intent.maximum_spend is not None
                and intent.maximum_spend > plan.maximum_spend
            )
        ):
            return CoordinatorCode.PREFLIGHT_PLAN_BOUNDS_MISMATCH
        deadlines = (
            plan.book_deadline,
            plan.proof_deadline,
            plan.economics_deadline,
            plan.account_deadline,
            plan.geoblock_deadline,
            evidence.fee_deadline,
            evidence.balance_deadline,
            evidence.allowance_deadline,
            evidence.manifest_deadline,
            evidence.activation_deadline,
            evidence.risk_deadline,
            evidence.protocol_deadline,
            evidence.capability_deadline,
        )
        if (
            not intent.created_at <= now < intent.deadline
            or plan.observed_at > now
            or plan.information_cutoff > now
            or evidence.activation_evidence.verified_at > now
            or any(deadline <= now for deadline in deadlines)
        ):
            return CoordinatorCode.PREFLIGHT_EVIDENCE_STALE
        return None

    @staticmethod
    def _canonical_evidence(evidence: object) -> tuple[bytes, Sha256] | None:
        if type(evidence) is not PreflightEvidence:
            return None
        try:
            validated = PreflightEvidence.model_validate(
                evidence.model_dump(mode="python"),
                strict=True,
            )
            payload = validated.model_dump_json().encode("utf-8")
            replay = PreflightEvidence.model_validate_json(payload, strict=True)
        except Exception:
            return None
        if replay != validated:
            return None
        return payload, sha256(payload).hexdigest()

    def _prepared_evidence(self, intent_id: UUID) -> PreflightEvidence | None:
        snapshot = self._prepared.get(intent_id)
        if (
            type(snapshot) is not tuple
            or len(snapshot) != 2
            or type(snapshot[0]) is not bytes
            or type(snapshot[1]) is not str
            or sha256(snapshot[0]).hexdigest() != snapshot[1]
        ):
            return None
        try:
            return PreflightEvidence.model_validate_json(snapshot[0], strict=True)
        except Exception:
            return None

    def _fresh_mutation_evidence(
        self,
        intent: ExecutionIntent,
        operation: ExecutionOperation,
    ) -> tuple[PreflightEvidence | None, datetime, CoordinatorCode | None]:
        now = self._now()
        if self._killed(now):
            return None, now, CoordinatorCode.EXECUTION_KILL_ENGAGED
        evidence = self._prepared_evidence(intent.intent_id)
        if evidence is None:
            return None, now, CoordinatorCode.PREFLIGHT_EVIDENCE_INVALID
        evidence_code = self._evidence_code(intent, evidence, now)
        if evidence_code is not None:
            return None, now, evidence_code
        if not self._authority_allowed(intent, evidence, operation, now):
            return None, now, CoordinatorCode.AUTHORITY_DENIED

        # Treat the authority callback as hostile: sample and reconstruct again after it returns.
        now = self._now()
        if self._killed(now):
            return None, now, CoordinatorCode.EXECUTION_KILL_ENGAGED
        evidence = self._prepared_evidence(intent.intent_id)
        if evidence is None:
            return None, now, CoordinatorCode.PREFLIGHT_EVIDENCE_INVALID
        evidence_code = self._evidence_code(intent, evidence, now)
        if evidence_code is not None:
            return None, now, evidence_code
        return evidence, now, None

    def prepare(self, intent: ExecutionIntent) -> PreparationResult:
        """Validate all preflight evidence and durably persist plan plus intent."""

        now = self._now()
        if self._recovering:
            return self._result(CoordinatorCode.RECOVERY_REQUIRED, intent)
        if self._killed(now):
            return self._result(CoordinatorCode.EXECUTION_KILL_ENGAGED, intent)
        if intent.intent_id in self._prepared:
            evidence = self._prepared_evidence(intent.intent_id)
            if evidence is None:
                return self._result(CoordinatorCode.PREFLIGHT_EVIDENCE_INVALID, intent)
            return self._result(
                CoordinatorCode.PREPARED,
                intent,
                plan_id=evidence.plan.plan_id,
                evidence_hashes=evidence.evidence_hashes,
            )
        try:
            persisted_intent = self._store.verified_execution_intent(intent.intent_id)
        except ConflictingRecordError:
            return self._result(CoordinatorCode.INTENT_COLLISION, intent)
        if persisted_intent is not None:
            return self._result(CoordinatorCode.DUPLICATE_INTENT, intent)
        invalid = False
        outcome: object = None
        try:
            outcome = self._preflight.validate(intent, now)
        except Exception:
            invalid = True
        if invalid:
            return self._result(CoordinatorCode.PREFLIGHT_EVIDENCE_INVALID, intent)
        if type(outcome) is PreflightRefusal:
            return self._result(
                CoordinatorCode.PREFLIGHT_REFUSED,
                intent,
                refusal_code=outcome.code,
                evidence_hashes=outcome.evidence_hashes,
            )
        snapshot = self._canonical_evidence(outcome)
        if snapshot is None:
            return self._result(CoordinatorCode.PREFLIGHT_EVIDENCE_INVALID, intent)
        evidence = PreflightEvidence.model_validate_json(snapshot[0], strict=True)
        evidence_code = self._evidence_code(intent, evidence, now)
        if evidence_code is not None:
            return self._result(
                evidence_code,
                intent,
                plan_id=evidence.plan.plan_id,
                evidence_hashes=evidence.evidence_hashes,
            )
        if not self._authority_allowed(
            intent,
            evidence,
            ExecutionOperation.SIGN_ORDER,
            now,
        ):
            return self._result(CoordinatorCode.AUTHORITY_DENIED, intent)
        evidence = PreflightEvidence.model_validate_json(snapshot[0], strict=True)
        evidence_code = self._evidence_code(intent, evidence, self._now())
        if evidence_code is not None:
            return self._result(
                evidence_code,
                intent,
                plan_id=evidence.plan.plan_id,
                evidence_hashes=evidence.evidence_hashes,
            )
        try:
            with self._store.transaction() as transaction:
                transaction.append_live_execution_plan(evidence.plan)
                transaction.append_execution_intent(intent)
        except ConflictingRecordError:
            return self._result(
                CoordinatorCode.INTENT_COLLISION,
                intent,
                plan_id=evidence.plan.plan_id,
                evidence_hashes=evidence.evidence_hashes,
            )
        object.__setattr__(
            self,
            "_prepared",
            MappingProxyType({**self._prepared, intent.intent_id: snapshot}),
        )
        return self._result(
            CoordinatorCode.PREPARED,
            intent,
            plan_id=evidence.plan.plan_id,
            evidence_hashes=evidence.evidence_hashes,
        )

    def _submission_result(
        self,
        code: CoordinatorCode,
        intent: ExecutionIntent,
        *,
        event: VenueOrderEvent | None = None,
        kill_reason: str | None = None,
        post_fill_decision: PostFillDecision | None = None,
    ) -> SubmissionResult:
        return SubmissionResult(
            code=code,
            intent_id=intent.intent_id,
            event=event,
            kill_reason=kill_reason,
            post_fill_decision=post_fill_decision,
        )

    @staticmethod
    def _legal_order_transition(
        previous: VenueOrderState,
        current: VenueOrderState,
    ) -> bool:
        legal: dict[VenueOrderState, frozenset[VenueOrderState]] = {
            VenueOrderState.ACK_MATCHED: frozenset(
                {
                    VenueOrderState.PARTIALLY_FILLED,
                    VenueOrderState.FILLED,
                    VenueOrderState.CANCEL_PENDING,
                    VenueOrderState.CANCELLED,
                    VenueOrderState.REJECTED,
                    VenueOrderState.UNKNOWN,
                    VenueOrderState.RECONCILED,
                }
            ),
            VenueOrderState.ACK_DELAYED: frozenset(
                {
                    VenueOrderState.ACK_MATCHED,
                    VenueOrderState.PARTIALLY_FILLED,
                    VenueOrderState.FILLED,
                    VenueOrderState.REJECTED,
                    VenueOrderState.UNKNOWN,
                    VenueOrderState.RECONCILED,
                }
            ),
            VenueOrderState.ACK_LIVE_UNEXPECTED: frozenset(
                {
                    VenueOrderState.PARTIALLY_FILLED,
                    VenueOrderState.FILLED,
                    VenueOrderState.CANCEL_PENDING,
                    VenueOrderState.CANCELLED,
                    VenueOrderState.REJECTED,
                    VenueOrderState.UNKNOWN,
                    VenueOrderState.RECONCILED,
                }
            ),
            VenueOrderState.PARTIALLY_FILLED: frozenset(
                {
                    VenueOrderState.PARTIALLY_FILLED,
                    VenueOrderState.FILLED,
                    VenueOrderState.CANCEL_PENDING,
                    VenueOrderState.CANCELLED,
                    VenueOrderState.UNKNOWN,
                    VenueOrderState.RECONCILED,
                }
            ),
            VenueOrderState.CANCEL_PENDING: frozenset(
                {
                    VenueOrderState.CANCELLED,
                    VenueOrderState.FILLED,
                    VenueOrderState.PARTIALLY_FILLED,
                    VenueOrderState.UNKNOWN,
                    VenueOrderState.RECONCILED,
                }
            ),
            VenueOrderState.UNKNOWN: frozenset(
                {
                    VenueOrderState.ACK_MATCHED,
                    VenueOrderState.PARTIALLY_FILLED,
                    VenueOrderState.FILLED,
                    VenueOrderState.CANCEL_PENDING,
                    VenueOrderState.CANCELLED,
                    VenueOrderState.REJECTED,
                    VenueOrderState.RECONCILED,
                }
            ),
        }
        return current in legal.get(previous, frozenset())

    @staticmethod
    def _known_venue_order_ids(events: tuple[VenueOrderEvent, ...]) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    event.venue_order_id
                    for event in events
                    if not event.venue_order_id.startswith("intent:")
                }
            )
        )

    def _contradictory_order_event(
        self,
        intent: ExecutionIntent,
        incoming: VenueOrderEvent,
        known_order_ids: tuple[str, ...],
        now: datetime,
    ) -> SubmissionResult:
        venue_order_id = (
            known_order_ids[0] if len(known_order_ids) == 1 else self._synthetic_order_id(intent)
        )
        event = self._order_event(
            intent,
            state=VenueOrderState.UNKNOWN,
            original_state=CoordinatorCode.ORDER_EVENT_CONTRADICTION.value,
            source_channel="coordinator",
            venue_order_id=venue_order_id,
            raw_event_hash=incoming.raw_event_hash,
            now=now,
            lineage_hashes=(incoming.raw_event_hash,),
        )
        self._store.append_venue_order_event(event)
        kill_reason = self._engage_kill(
            trigger=CoordinatorCode.ORDER_EVENT_CONTRADICTION.value,
            intent=intent,
            venue_order_id=(known_order_ids[0] if len(known_order_ids) == 1 else None),
            now=now,
            evidence_hashes=(incoming.raw_event_hash,),
        )
        return self._submission_result(
            CoordinatorCode.ORDER_EVENT_CONTRADICTION,
            intent,
            event=event,
            kill_reason=kill_reason,
        )

    def apply_order_event(
        self,
        intent: ExecutionIntent,
        incoming: VenueOrderEvent,
    ) -> SubmissionResult:
        """Append one strictly correlated authoritative order fact."""

        now = self._now()
        try:
            event = VenueOrderEvent.model_validate(
                incoming.model_dump(mode="python"),
                strict=True,
            )
        except Exception:
            event = None
        first_fill_claimed = (
            event is not None
            and event.normalized_state in {VenueOrderState.PARTIALLY_FILLED, VenueOrderState.FILLED}
            and self._store.claim_execution_first_fill(intent.intent_id)
        )
        try:
            history = self._store.verified_venue_order_events_for_intent(
                intent.intent_id,
                datetime.max.replace(tzinfo=UTC),
            )
        except ConflictingRecordError:
            history = ()
        known_order_ids = self._known_venue_order_ids(history)
        previous = history[-1] if history else None
        invalid = (
            event is None
            or previous is None
            or len(known_order_ids) != 1
            or event.venue_order_id != known_order_ids[0]
            or event.venue != intent.venue
            or event.protocol_version != intent.protocol_version
            or event.intent_id not in {None, intent.intent_id}
            or event.received_at <= previous.received_at
            or (
                previous.sequence_number is not None
                and event.sequence_number is not None
                and event.sequence_number <= previous.sequence_number
            )
            or (
                previous.venue_timestamp is not None
                and event.venue_timestamp is not None
                and event.venue_timestamp <= previous.venue_timestamp
            )
            or not self._legal_order_transition(
                previous.normalized_state,
                event.normalized_state,
            )
            or (
                intent.order_type.value == "FOK"
                and event.normalized_state is VenueOrderState.PARTIALLY_FILLED
            )
        )
        if invalid or event is None:
            contradiction_source = incoming if type(incoming) is VenueOrderEvent else previous
            if contradiction_source is None:
                raise ValueError("ORDER_EVENT_INVALID") from None
            return self._contradictory_order_event(
                intent,
                contradiction_source,
                known_order_ids,
                now,
            )

        correlated = VenueOrderEvent(
            schema_version=1,
            event_id=uuid5(
                _COORDINATOR_NAMESPACE,
                f"correlated|{intent.intent_id}|{event.event_id}",
            ),
            venue=event.venue,
            raw_event_hash=event.raw_event_hash,
            source_channel=event.source_channel,
            venue_order_id=event.venue_order_id,
            intent_id=intent.intent_id,
            original_venue_state=event.original_venue_state,
            normalized_state=event.normalized_state,
            terminal=event.terminal,
            venue_timestamp=event.venue_timestamp,
            received_at=event.received_at,
            sequence_number=event.sequence_number,
            protocol_version=event.protocol_version,
            lineage_hashes=tuple(sorted(set((*event.lineage_hashes, event.raw_event_hash)))),
        )
        self._store.append_venue_order_event(correlated)
        first_fill = (
            correlated.normalized_state
            in {
                VenueOrderState.PARTIALLY_FILLED,
                VenueOrderState.FILLED,
            }
            and first_fill_claimed
            and not any(
                prior.normalized_state in {VenueOrderState.PARTIALLY_FILLED, VenueOrderState.FILLED}
                for prior in history
            )
        )
        decision = None
        kill_reason = None
        if first_fill:
            evidence = self._prepared_evidence(intent.intent_id)
            invalid_decision = evidence is None
            outcome: object = None
            if evidence is not None:
                try:
                    outcome = self._preflight.revalidate_after_fill(
                        evidence.plan,
                        intent,
                        correlated,
                        now,
                    )
                except Exception:
                    invalid_decision = True
            if type(outcome) is not PostFillDecision:
                invalid_decision = True
            decision = PostFillDecision.HALT_EXPOSED if invalid_decision else outcome
            if decision is PostFillDecision.HALT_EXPOSED:
                kill_reason = self._engage_kill(
                    trigger=PostFillDecision.HALT_EXPOSED.value,
                    intent=intent,
                    venue_order_id=correlated.venue_order_id,
                    now=now,
                    evidence_hashes=(correlated.raw_event_hash,),
                )
        return self._submission_result(
            CoordinatorCode.ORDER_EVENT_APPLIED,
            intent,
            event=correlated,
            kill_reason=kill_reason,
            post_fill_decision=decision,
        )

    def _account_intents(
        self,
        account_fingerprint: Sha256,
        as_of: datetime,
    ) -> tuple[ExecutionIntent, ...]:
        intents: dict[UUID, ExecutionIntent] = {}
        for plan in self._store.verified_live_execution_plans_for_account(
            account_fingerprint,
            as_of,
        ):
            for intent in self._store.verified_execution_intent_history_for_plan(
                plan.plan_id,
                as_of,
            ):
                if (
                    intent.plan_id != plan.plan_id
                    or intent.account_fingerprint != account_fingerprint
                    or intent.account_fingerprint != plan.account_fingerprint
                    or intent.venue != plan.venue
                    or intent.capability_fingerprint != plan.capability_fingerprint
                ):
                    raise ConflictingRecordError("plan-linked intent identity mismatch")
                intents[intent.intent_id] = intent
        return tuple(
            sorted(
                intents.values(),
                key=lambda intent: (intent.created_at, intent.intent_id),
            )
        )

    def _recovery_evidence(
        self,
        intent: ExecutionIntent,
        now: datetime,
    ) -> PreflightEvidence | None:
        outcome: object = None
        try:
            outcome = self._preflight.validate(intent, now)
        except Exception:
            return None
        if (
            type(outcome) is not PreflightEvidence
            or self._evidence_code(intent, outcome, now) is not None
        ):
            return None
        snapshot = self._canonical_evidence(outcome)
        if snapshot is None:
            return None
        evidence = PreflightEvidence.model_validate_json(snapshot[0], strict=True)
        if self._evidence_code(intent, evidence, now) is not None:
            return None
        object.__setattr__(
            self,
            "_prepared",
            MappingProxyType({**self._prepared, intent.intent_id: snapshot}),
        )
        return evidence

    @staticmethod
    def _order_read_fill_state(
        intent: ExecutionIntent,
        item: OrderReadPayload,
    ) -> VenueOrderState | None:
        try:
            original = Decimal(item.original_size)
            matched = Decimal(item.size_matched)
            price = Decimal(item.price)
        except InvalidOperation:
            return None
        if (
            item.asset_id != intent.token_id
            or item.side != intent.side.upper()
            or price != intent.limit_price
            or item.order_type.upper() != intent.order_type.value
            or original <= 0
            or matched < 0
            or matched > original
            or (intent.base_size is not None and original != intent.base_size)
        ):
            return None
        if matched == original:
            return VenueOrderState.FILLED
        if matched > 0:
            return VenueOrderState.PARTIALLY_FILLED
        return None

    def _append_recovered_order_state(
        self,
        intent: ExecutionIntent,
        state: VenueOrderState,
        venue_order_id: str,
        evidence_hash: Sha256,
        now: datetime,
    ) -> bool:
        if self._recovery_evidence(intent, now) is None:
            return False
        incoming = self._order_event(
            intent,
            state=state,
            original_state=f"RECOVERY_{state.value}",
            source_channel="recovery_read",
            venue_order_id=venue_order_id,
            raw_event_hash=evidence_hash,
            now=now,
            lineage_hashes=(evidence_hash,),
        )
        result = self.apply_order_event(intent, incoming)
        return result.code is CoordinatorCode.ORDER_EVENT_APPLIED

    def _recover_from_order_read(
        self,
        intent: ExecutionIntent,
        venue_order_id: str,
        payload: OrdersReadPayload,
        evidence_hash: Sha256,
        now: datetime,
    ) -> bool:
        matches = tuple(item for item in payload.items if item.id == venue_order_id)
        if len(matches) != 1:
            return False
        state = self._order_read_fill_state(intent, matches[0])
        return state is not None and self._append_recovered_order_state(
            intent,
            state,
            venue_order_id,
            evidence_hash,
            now,
        )

    def _recover_from_trade_read(
        self,
        intent: ExecutionIntent,
        venue_order_id: str,
        payload: TradesReadPayload,
        evidence_hash: Sha256,
        now: datetime,
    ) -> bool:
        try:
            envelope = self._store.verified_signed_order_envelope(intent.intent_id)
        except ConflictingRecordError:
            return False
        if (
            envelope is None
            or envelope.intent_id != intent.intent_id
            or envelope.intent_fingerprint != intent.intent_fingerprint
            or envelope.protocol_version != intent.protocol_version
        ):
            return False
        try:
            public_order = json.loads(envelope.canonical_order_json)
            signer_address = public_order["signer"]
        except (KeyError, TypeError, json.JSONDecodeError):
            return False
        if (
            type(signer_address) is not str
            or public_order.get("maker") != signer_address
            or public_order.get("tokenId") != intent.token_id
            or public_order.get("side") != intent.side.upper()
        ):
            return False

        items = payload.items
        try:
            item_order = tuple((int(item.last_update), item.id) for item in items)
        except ValueError:
            return False
        if item_order != tuple(sorted(item_order)) or len({item.id for item in items}) != len(
            items
        ):
            return False

        correlations = []
        seen_trade_ids: set[str] = set()
        total_matched = Decimal("0")
        for trade in items:
            matching_maker_orders = tuple(
                maker_order
                for maker_order in trade.maker_orders
                if maker_order.order_id == venue_order_id
            )
            taker_match = trade.trader_side == "TAKER" and trade.taker_order_id == venue_order_id
            maker_match = trade.trader_side == "MAKER" and bool(matching_maker_orders)
            if not taker_match and not maker_match:
                continue
            if trade.id in seen_trade_ids or taker_match == maker_match:
                return False
            try:
                matched = Decimal(trade.size)
                price = Decimal(trade.price)
                fee_rate_bps = int(trade.fee_rate_bps)
                match_time = int(trade.match_time)
                last_update = int(trade.last_update)
            except (InvalidOperation, ValueError):
                return False
            if (
                trade.status != VenueTradeState.CONFIRMED.value
                or trade.transaction_hash is None
                or trade.asset_id != intent.token_id
                or trade.side != intent.side.upper()
                or price != intent.limit_price
                or fee_rate_bps > intent.fee_rate_bps_cap
                or matched <= 0
                or intent.base_size is None
                or match_time > last_update
            ):
                return False
            if maker_match:
                if len(matching_maker_orders) != 1:
                    return False
                maker_order = matching_maker_orders[0]
                try:
                    maker_matched = Decimal(maker_order.matched_amount)
                    maker_price = Decimal(maker_order.price)
                    maker_fee_rate_bps = int(maker_order.fee_rate_bps)
                except (InvalidOperation, ValueError):
                    return False
                if (
                    trade.taker_order_id == venue_order_id
                    or maker_order.asset_id != intent.token_id
                    or maker_order.side != intent.side.upper()
                    or maker_order.maker_address.casefold() != signer_address.casefold()
                    or trade.maker_address.casefold() != signer_address.casefold()
                    or maker_order.maker_address.casefold() != trade.maker_address.casefold()
                    or maker_price != intent.limit_price
                    or maker_price != price
                    or maker_fee_rate_bps != fee_rate_bps
                    or maker_fee_rate_bps > intent.fee_rate_bps_cap
                    or maker_matched != matched
                    or maker_order.outcome != trade.outcome
                ):
                    return False
            elif matching_maker_orders:
                return False
            seen_trade_ids.add(trade.id)
            total_matched += matched
            correlations.append(trade)
        if not correlations or intent.base_size is None or total_matched > intent.base_size:
            return False
        trade_events = tuple(
            VenueTradeEvent(
                schema_version=1,
                trade_event_id=uuid5(
                    _COORDINATOR_NAMESPACE,
                    f"recovery-trade|{intent.intent_id}|{trade.id}|{venue_order_id}",
                ),
                venue="polymarket",
                raw_event_hash=evidence_hash,
                source_channel="recovery_read",
                venue_trade_id=trade.id,
                venue_order_id=venue_order_id,
                intent_id=intent.intent_id,
                original_venue_state=trade.status,
                normalized_state=VenueTradeState.CONFIRMED,
                terminal=True,
                venue_timestamp=None,
                received_at=self._received_at(now),
                sequence_number=None,
                protocol_version=intent.protocol_version,
                lineage_hashes=(evidence_hash,),
            )
            for trade in correlations
        )
        try:
            with self._store.transaction() as transaction:
                for trade_event in trade_events:
                    transaction.append_venue_trade_event(trade_event)
        except ConflictingRecordError:
            return False
        state = (
            VenueOrderState.FILLED
            if total_matched == intent.base_size
            else VenueOrderState.PARTIALLY_FILLED
        )
        return self._append_recovered_order_state(
            intent,
            state,
            venue_order_id,
            evidence_hash,
            now,
        )

    def _recover_cancellation(
        self,
        intent: ExecutionIntent,
        venue_order_id: str,
        now: datetime,
    ) -> tuple[bool, bool, str | None]:
        try:
            history = self._store.verified_venue_order_events_for_intent(
                intent.intent_id,
                datetime.max.replace(tzinfo=UTC),
            )
        except ConflictingRecordError:
            return False, False, CoordinatorCode.ORDER_EVENT_CONTRADICTION.value
        acknowledgements = tuple(
            event
            for event in history
            if event.original_venue_state == RestCode.CANCEL_ACKNOWLEDGED.value
        )
        acknowledgement = acknowledgements[0] if len(acknowledgements) == 1 else None
        if acknowledgements and (
            acknowledgement is None
            or acknowledgement.venue != intent.venue
            or acknowledgement.intent_id != intent.intent_id
            or acknowledgement.venue_order_id != venue_order_id
            or acknowledgement.normalized_state is not VenueOrderState.CANCEL_PENDING
            or acknowledgement.terminal
            or acknowledgement.protocol_version != intent.protocol_version
            or acknowledgement.source_channel != "recovery_cancel_ack"
            or acknowledgement.raw_event_hash not in acknowledgement.lineage_hashes
        ):
            return False, False, CoordinatorCode.ORDER_EVENT_CONTRADICTION.value

        if acknowledgement is None:
            try:
                envelope = self._store.verified_signed_order_envelope(intent.intent_id)
            except ConflictingRecordError:
                return False, False, CoordinatorCode.ENVELOPE_COLLISION.value
            evidence = self._recovery_evidence(intent, self._now())
            if envelope is None or evidence is None:
                return False, False, CoordinatorCode.RECOVERY_BLOCKED.value
            authority_now = self._now()
            if self._evidence_code(
                intent, evidence, authority_now
            ) is not None or not self._authority_allowed(
                intent,
                evidence,
                ExecutionOperation.CANCEL_ORDER,
                authority_now,
            ):
                return False, False, CoordinatorCode.AUTHORITY_DENIED.value
            # Reconstruct and revalidate after the injected authority callback.
            authority_now = self._now()
            evidence = self._prepared_evidence(intent.intent_id)
            if evidence is None or self._evidence_code(intent, evidence, authority_now) is not None:
                return False, False, CoordinatorCode.RECOVERY_BLOCKED.value
            outcome: object = None
            try:
                outcome = self._signer.cancel(
                    intent,
                    envelope,
                    venue_order_id,
                    evidence,
                )
            except Exception:
                return False, False, RestCode.CANCEL_OUTCOME_UNKNOWN.value
            outcome_now = self._now()
            if type(outcome) is RestResult:
                try:
                    outcome = RestResult.model_validate(
                        outcome.model_dump(mode="python"),
                        strict=True,
                    )
                except Exception:
                    outcome = None
            if (
                type(outcome) is not RestResult
                or outcome.route is not RouteKey.CANCEL_ORDER
                or outcome.code is not RestCode.CANCEL_ACKNOWLEDGED
                or type(outcome.payload) is not CancellationPayload
                or outcome.payload.order_id != venue_order_id
                or outcome.payload.confirmation_required is not True
                or outcome.observed_at > outcome_now
                or (outcome.raw_body_hash is None and outcome.request_body_hash is None)
            ):
                reason = (
                    outcome.code.value
                    if type(outcome) is RestResult and outcome.route is RouteKey.CANCEL_ORDER
                    else RestCode.CANCEL_OUTCOME_UNKNOWN.value
                )
                return False, False, reason
            lineage_hashes = tuple(
                sorted(
                    {
                        value
                        for value in (outcome.raw_body_hash, outcome.request_body_hash)
                        if value is not None
                    }
                )
            )
            received_at = self._received_at(outcome_now)
            acknowledgement = VenueOrderEvent(
                schema_version=1,
                event_id=uuid5(
                    _COORDINATOR_NAMESPACE,
                    "|".join(
                        (
                            "cancel-ack",
                            str(intent.intent_id),
                            venue_order_id,
                            *lineage_hashes,
                        )
                    ),
                ),
                venue=intent.venue,
                raw_event_hash=outcome.raw_body_hash or outcome.request_body_hash,
                source_channel="recovery_cancel_ack",
                venue_order_id=venue_order_id,
                intent_id=intent.intent_id,
                original_venue_state=RestCode.CANCEL_ACKNOWLEDGED.value,
                normalized_state=VenueOrderState.CANCEL_PENDING,
                terminal=False,
                venue_timestamp=None,
                received_at=received_at,
                sequence_number=None,
                protocol_version=intent.protocol_version,
                lineage_hashes=lineage_hashes,
            )
            try:
                self._store.append_venue_order_event(acknowledgement)
            except ConflictingRecordError:
                return False, False, CoordinatorCode.ORDER_EVENT_CONTRADICTION.value

        previous_received_at = self._last_received_at
        if previous_received_at is None or previous_received_at < acknowledgement.received_at:
            object.__setattr__(self, "_last_received_at", acknowledgement.received_at)
        confirmation: object = None
        try:
            confirmation = self._account_reader.read_order(venue_order_id)
        except Exception:
            return False, True, RestCode.CANCEL_NOT_CONFIRMED.value
        if (
            not self._valid_recovery_read(
                confirmation,
                RouteKey.READ_ORDER,
                OrderReadPayload,
                now,
            )
            or type(confirmation) is not RestResult
            or type(confirmation.payload) is not OrderReadPayload
            or confirmation.payload.id != venue_order_id
            or confirmation.payload.status != "CANCELED"
            or confirmation.payload.asset_id != intent.token_id
            or confirmation.payload.side != intent.side.upper()
            or Decimal(confirmation.payload.price) != intent.limit_price
            or confirmation.raw_body_hash is None
        ):
            reason = (
                confirmation.code.value
                if type(confirmation) is RestResult and confirmation.kill_required
                else RestCode.CANCEL_NOT_CONFIRMED.value
            )
            return False, True, reason
        incoming = self._order_event(
            intent,
            state=VenueOrderState.CANCELLED,
            original_state="CANCELED",
            source_channel="recovery_read",
            venue_order_id=venue_order_id,
            raw_event_hash=confirmation.raw_body_hash,
            now=now,
            lineage_hashes=tuple(
                sorted({*acknowledgement.lineage_hashes, confirmation.raw_body_hash})
            ),
        )
        return (
            self.apply_order_event(intent, incoming).code is CoordinatorCode.ORDER_EVENT_APPLIED,
            True,
            None,
        )

    @staticmethod
    def _valid_recovery_read(
        result: object,
        route: RouteKey,
        payload_type: type[object],
        now: datetime,
    ) -> bool:
        return (
            type(result) is RestResult
            and result.route is route
            and result.code is RestCode.READ_OK
            and type(result.payload) is payload_type
            and result.observed_at <= now
            and result.recovery_required is False
            and result.kill_required is False
        )

    def recover_account(
        self,
        account_fingerprint: Sha256,
        *,
        stream_health: UserStreamHealth | None = None,
        heartbeat_state: HeartbeatState | None = None,
    ) -> RecoveryReport:
        """Perform the three authoritative reads; never retry submission."""

        return self._run_recovery(
            account_fingerprint,
            stream_health=stream_health,
            heartbeat_state=heartbeat_state,
            startup=False,
        )

    def _run_recovery(
        self,
        account_fingerprint: Sha256,
        *,
        stream_health: UserStreamHealth | None,
        heartbeat_state: HeartbeatState | None,
        startup: bool,
    ) -> RecoveryReport:
        try:
            reader_account = self._account_reader.account_fingerprint
        except Exception:
            reader_account = None
        if (
            account_fingerprint != self._account_fingerprint
            or type(reader_account) is not str
            or reader_account != account_fingerprint
        ):
            raise ValueError("RECOVERY_ACCOUNT_MISMATCH") from None
        if self._recovering:
            raise ValueError("RECOVERY_ALREADY_ACTIVE") from None
        object.__setattr__(self, "_recovering", True)
        try:
            return self._recover_account(
                account_fingerprint,
                stream_health=stream_health,
                heartbeat_state=heartbeat_state,
                startup=startup,
            )
        finally:
            object.__setattr__(self, "_recovering", False)

    def _recover_account(
        self,
        account_fingerprint: Sha256,
        *,
        stream_health: UserStreamHealth | None,
        heartbeat_state: HeartbeatState | None,
        startup: bool,
    ) -> RecoveryReport:

        now = self._now()
        read_routes = list(_BASE_RECOVERY_READS)
        outcomes: list[object] = []
        for operation in (
            self._account_reader.read_open_orders,
            self._account_reader.read_trades,
            self._account_reader.read_balance_allowance,
        ):
            try:
                outcomes.append(operation())
            except Exception:
                outcomes.append(None)

        reads_valid = all(
            self._valid_recovery_read(result, route, payload_type, now)
            for result, route, payload_type in zip(
                outcomes,
                read_routes,
                (OrdersReadPayload, TradesReadPayload, BalanceAllowancePayload),
                strict=True,
            )
        )
        orders_result = outcomes[0] if reads_valid else None
        trades_result = outcomes[1] if reads_valid else None
        read_failure_reason = next(
            (
                outcome.code.value
                for outcome in outcomes
                if type(outcome) is RestResult and outcome.kill_required
            ),
            CoordinatorCode.RECOVERY_BLOCKED.value,
        )
        store_scan_failed = False
        try:
            intents = self._account_intents(account_fingerprint, now)
        except Exception:
            intents = ()
            reads_valid = False
            store_scan_failed = True

        uncertainty_reason = None
        if stream_health is not None:
            if (
                type(stream_health) is UserStreamHealth
                and stream_health.status == "RECOVERY_REQUIRED"
            ):
                uncertainty_reason = stream_health.kill_reason
            elif type(stream_health) is not UserStreamHealth:
                uncertainty_reason = CoordinatorCode.RECOVERY_BLOCKED.value
        if heartbeat_state is not None:
            if type(heartbeat_state) is HeartbeatState and heartbeat_state.status == "UNCERTAIN":
                uncertainty_reason = heartbeat_state.kill_reason
            elif type(heartbeat_state) is not HeartbeatState:
                uncertainty_reason = CoordinatorCode.RECOVERY_BLOCKED.value

        forced_reasons: dict[UUID, str] = {}
        for intent in intents:
            try:
                trade_history = self._store.verified_venue_trade_events_for_intent(
                    intent.intent_id,
                    now,
                )
            except ConflictingRecordError:
                forced_reasons[intent.intent_id] = CoordinatorCode.RECOVERY_BLOCKED.value
                continue
            if startup:
                try:
                    order_history = self._store.verified_venue_order_events_for_intent(
                        intent.intent_id,
                        now,
                    )
                except ConflictingRecordError:
                    forced_reasons[intent.intent_id] = CoordinatorCode.RECOVERY_BLOCKED.value
                    continue
                if not order_history or order_history[-1].normalized_state in {
                    VenueOrderState.PARTIALLY_FILLED,
                    VenueOrderState.FILLED,
                }:
                    forced_reasons[intent.intent_id] = CoordinatorCode.RECOVERY_BLOCKED.value
                    continue
            unresolved_trade = next(
                (
                    event
                    for event in trade_history
                    if event.normalized_state
                    in {
                        VenueTradeState.MATCHED_NOT_BROADCASTED,
                        VenueTradeState.MATCHED,
                        VenueTradeState.MINED,
                        VenueTradeState.RETRYING,
                        VenueTradeState.FAILED,
                    }
                ),
                None,
            )
            trade_identities = [event.venue_trade_id for event in trade_history]
            trade_sequences = [
                event.sequence_number
                for event in trade_history
                if event.sequence_number is not None
            ]
            trade_contradiction = len(trade_identities) != len(set(trade_identities)) or any(
                current <= previous for previous, current in pairwise(trade_sequences)
            )
            if trade_contradiction:
                forced_reasons[intent.intent_id] = CoordinatorCode.RECOVERY_BLOCKED.value
            elif unresolved_trade is not None:
                forced_reasons[intent.intent_id] = (
                    f"SETTLEMENT_{unresolved_trade.normalized_state.value}"
                )
            elif uncertainty_reason is not None:
                try:
                    order_history = self._store.verified_venue_order_events_for_intent(
                        intent.intent_id,
                        now,
                    )
                except ConflictingRecordError:
                    forced_reasons[intent.intent_id] = CoordinatorCode.RECOVERY_BLOCKED.value
                    continue
                if order_history and not order_history[-1].terminal:
                    forced_reasons[intent.intent_id] = uncertainty_reason
        account_block_reason = CoordinatorCode.RECOVERY_BLOCKED.value if store_scan_failed else None
        if startup:
            try:
                reconciliations = self._store.verified_live_reconciliations_for_account(
                    account_fingerprint,
                    now,
                )
                postings = self._store.verified_live_ledger_postings_for_account(
                    account_fingerprint,
                    now,
                )
            except Exception:
                reconciliations = ()
                postings = ()
                account_block_reason = CoordinatorCode.RECOVERY_BLOCKED.value
            if reconciliations and not reconciliations[-1].complete:
                account_block_reason = "RECONCILIATION_INCOMPLETE"
                for intent in intents:
                    forced_reasons.setdefault(intent.intent_id, account_block_reason)
            complete_reconciliations = tuple(
                reconciliation for reconciliation in reconciliations if reconciliation.complete
            )
            latest_complete = complete_reconciliations[-1] if complete_reconciliations else None
            posting_ids = {posting.posting_id for posting in postings}
            reconciliation_references_postings = bool(
                latest_complete is not None and latest_complete.expected_posting_ids
            )
            if (postings or reconciliation_references_postings) and (
                latest_complete is None
                or any(posting.occurred_at > latest_complete.observed_at for posting in postings)
                or set(latest_complete.expected_posting_ids) != posting_ids
            ):
                account_block_reason = CoordinatorCode.RECOVERY_BLOCKED.value
                for intent in intents:
                    forced_reasons.setdefault(intent.intent_id, account_block_reason)
        if forced_reasons:
            first_forced = next(intent for intent in intents if intent.intent_id in forced_reasons)
            self._engage_kill(
                trigger=forced_reasons[first_forced.intent_id],
                intent=first_forced,
                venue_order_id=None,
                now=now,
            )
        elif account_block_reason is not None:
            self._engage_kill(
                trigger=account_block_reason,
                intent=None,
                venue_order_id=None,
                now=now,
            )

        recovered: list[UUID] = []
        blocked: list[UUID] = []
        mutation_failure_reason = None
        unresolved_states = {
            VenueOrderState.SUBMITTING,
            VenueOrderState.UNKNOWN,
            VenueOrderState.ACK_DELAYED,
            VenueOrderState.ACK_LIVE_UNEXPECTED,
            VenueOrderState.CANCEL_PENDING,
        }
        for intent in intents:
            if intent.intent_id in forced_reasons:
                blocked.append(intent.intent_id)
                continue
            try:
                history = self._store.verified_venue_order_events_for_intent(
                    intent.intent_id,
                    now,
                )
            except ConflictingRecordError:
                blocked.append(intent.intent_id)
                continue
            latest = history[-1] if history else None
            if latest is None or latest.normalized_state not in unresolved_states:
                continue
            known_order_ids = self._known_venue_order_ids(history)
            if not reads_valid or len(known_order_ids) != 1:
                blocked.append(intent.intent_id)
                continue
            venue_order_id = known_order_ids[0]
            assert type(orders_result) is RestResult
            assert type(orders_result.payload) is OrdersReadPayload
            assert type(trades_result) is RestResult
            assert type(trades_result.payload) is TradesReadPayload
            order_hash = orders_result.raw_body_hash
            trade_hash = trades_result.raw_body_hash
            if latest.normalized_state is VenueOrderState.CANCEL_PENDING:
                resolved, confirmation_attempted, cancellation_reason = self._recover_cancellation(
                    intent,
                    venue_order_id,
                    now,
                )
                if mutation_failure_reason is None:
                    mutation_failure_reason = cancellation_reason
                if confirmation_attempted:
                    read_routes.append(RouteKey.READ_ORDER)
            else:
                resolved = order_hash is not None and self._recover_from_order_read(
                    intent,
                    venue_order_id,
                    orders_result.payload,
                    order_hash,
                    now,
                )
            if not resolved:
                resolved = latest.normalized_state is not VenueOrderState.CANCEL_PENDING and (
                    trade_hash is not None
                    and self._recover_from_trade_read(
                        intent,
                        venue_order_id,
                        trades_result.payload,
                        trade_hash,
                        now,
                    )
                )
            (recovered if resolved else blocked).append(intent.intent_id)

        kill_reason = None
        if blocked:
            first_intent = next(intent for intent in intents if intent.intent_id == blocked[0])
            kill_reason = self._engage_kill(
                trigger=mutation_failure_reason or read_failure_reason,
                intent=first_intent,
                venue_order_id=None,
                now=now,
            )
        elif not reads_valid or account_block_reason is not None:
            kill_reason = account_block_reason or CoordinatorCode.RECOVERY_BLOCKED.value
        existing_kills = self._store.verified_kill_switch_events(
            account_fingerprint,
            now,
        )
        if existing_kills:
            kill_reason = existing_kills[0].trigger
        code = (
            CoordinatorCode.RECOVERY_BLOCKED
            if blocked or not reads_valid or account_block_reason is not None
            else CoordinatorCode.RECOVERY_COMPLETE
        )
        return RecoveryReport(
            code=code,
            account_fingerprint=account_fingerprint,
            reads=tuple(read_routes),
            recovered_intent_ids=tuple(sorted(recovered)),
            blocked_intent_ids=tuple(sorted(blocked)),
            submit_attempts=0,
            kill_reason=kill_reason,
        )

    def recover_on_startup(self, account_fingerprint: Sha256) -> RecoveryReport:
        """Scan database-complete persisted history before admitting new work."""

        return self._run_recovery(
            account_fingerprint,
            stream_health=None,
            heartbeat_state=None,
            startup=True,
        )

    def _unknown_after_port_failure(
        self,
        intent: ExecutionIntent,
        *,
        trigger: str,
        now: datetime,
    ) -> SubmissionResult:
        event = self._order_event(
            intent,
            state=VenueOrderState.UNKNOWN,
            original_state=trigger,
            source_channel="coordinator",
            venue_order_id=self._synthetic_order_id(intent),
            raw_event_hash=self._safe_hash(trigger, intent.intent_id),
            now=now,
        )
        self._store.append_venue_order_event(event)
        kill_reason = self._engage_kill(
            trigger=trigger,
            intent=intent,
            venue_order_id=None,
            now=now,
        )
        return self._submission_result(
            CoordinatorCode.SIGNER_FAILED,
            intent,
            event=event,
            kill_reason=kill_reason,
        )

    def _classify_submit_result(
        self,
        intent: ExecutionIntent,
        result: RestResult,
        now: datetime,
    ) -> SubmissionResult:
        payload = result.payload if type(result.payload) is OrderAckPayload else None
        venue_order_id = (
            payload.order_id if payload is not None else self._synthetic_order_id(intent)
        )
        state = {
            RestCode.ORDER_ACK_MATCHED: VenueOrderState.ACK_MATCHED,
            RestCode.ORDER_ACK_DELAYED: VenueOrderState.ACK_DELAYED,
            RestCode.ORDER_ACK_LIVE_UNEXPECTED: VenueOrderState.ACK_LIVE_UNEXPECTED,
            RestCode.ORDER_ACK_UNMATCHED: VenueOrderState.UNKNOWN,
            RestCode.ORDER_OUTCOME_UNKNOWN: VenueOrderState.UNKNOWN,
            RestCode.AUTH_REJECTED: VenueOrderState.UNKNOWN,
            RestCode.AUTH_REQUEST_BUILD_FAILED: VenueOrderState.REJECTED,
        }[result.code]
        evidence_hashes = tuple(
            sorted(
                {
                    value
                    for value in (result.raw_body_hash, result.request_body_hash)
                    if value is not None
                }
            )
        )
        event = self._order_event(
            intent,
            state=state,
            original_state=result.code.value,
            source_channel="rest",
            venue_order_id=venue_order_id,
            raw_event_hash=(
                result.raw_body_hash
                or result.request_body_hash
                or self._safe_hash(result.code.value, intent.intent_id)
            ),
            now=now,
            lineage_hashes=evidence_hashes,
        )
        self._store.append_venue_order_event(event)
        kill_reason = None
        if result.kill_required:
            kill_reason = self._engage_kill(
                trigger=result.code.value,
                intent=intent,
                venue_order_id=payload.order_id if payload is not None else None,
                now=now,
                evidence_hashes=evidence_hashes,
            )
        return self._submission_result(
            CoordinatorCode.SUBMITTED,
            intent,
            event=event,
            kill_reason=kill_reason,
        )

    def submit_intent(self, intent: ExecutionIntent) -> SubmissionResult:
        """Claim permanently, then sign and submit at most once for this coordinator."""

        with self._submission_claim_lock:
            already_claimed = intent.intent_id in self._submission_claims
            active = intent.intent_id in self._active_submission_claims
            if not already_claimed:
                self._submission_claims.add(intent.intent_id)
                self._active_submission_claims.add(intent.intent_id)
        if already_claimed:
            if active:
                return self._submission_result(CoordinatorCode.DUPLICATE_INTENT, intent)
            now = self._now()
            kill_reason = self._engage_kill(
                trigger=CoordinatorCode.DUPLICATE_INTENT.value,
                intent=intent,
                venue_order_id=None,
                now=now,
            )
            return self._submission_result(
                CoordinatorCode.DUPLICATE_INTENT,
                intent,
                kill_reason=kill_reason,
            )
        if not self._store.claim_execution_intent_submission(intent.intent_id):
            with self._submission_claim_lock:
                self._active_submission_claims.discard(intent.intent_id)
            return self._submission_result(CoordinatorCode.DUPLICATE_INTENT, intent)
        try:
            return self._submit_claimed_intent(intent)
        finally:
            with self._submission_claim_lock:
                self._active_submission_claims.discard(intent.intent_id)

    def _submit_claimed_intent(self, intent: ExecutionIntent) -> SubmissionResult:
        """Execute one already-claimed append-only submission lifecycle."""

        now = self._now()
        try:
            latest = self._store.latest_order_state(intent.intent_id)
        except ConflictingRecordError:
            kill_reason = self._engage_kill(
                trigger=CoordinatorCode.ORDER_EVENT_CONTRADICTION.value,
                intent=intent,
                venue_order_id=None,
                now=now,
            )
            return self._submission_result(
                CoordinatorCode.ORDER_EVENT_CONTRADICTION,
                intent,
                kill_reason=kill_reason,
            )
        if latest is not None:
            kill_reason = self._engage_kill(
                trigger=CoordinatorCode.DUPLICATE_INTENT.value,
                intent=intent,
                venue_order_id=None,
                now=now,
            )
            return self._submission_result(
                CoordinatorCode.DUPLICATE_INTENT,
                intent,
                kill_reason=kill_reason,
            )
        evidence = self._prepared_evidence(intent.intent_id)
        if evidence is None:
            prepared = self.prepare(intent)
            if prepared.code is not CoordinatorCode.PREPARED:
                kill_reason = None
                if prepared.code in {
                    CoordinatorCode.DUPLICATE_INTENT,
                    CoordinatorCode.INTENT_COLLISION,
                }:
                    kill_reason = self._engage_kill(
                        trigger=prepared.code.value,
                        intent=intent,
                        venue_order_id=None,
                        now=now,
                    )
                return self._submission_result(
                    prepared.code,
                    intent,
                    kill_reason=kill_reason,
                )
            evidence = self._prepared_evidence(intent.intent_id)
            if evidence is None:
                return self._submission_result(
                    CoordinatorCode.PREFLIGHT_EVIDENCE_INVALID,
                    intent,
                )
        try:
            persisted_envelope = self._store.verified_signed_order_envelope(intent.intent_id)
        except ConflictingRecordError:
            persisted_envelope = None
            envelope_corrupted = True
        else:
            envelope_corrupted = False
        if persisted_envelope is not None or envelope_corrupted:
            kill_reason = self._engage_kill(
                trigger=CoordinatorCode.ENVELOPE_COLLISION.value,
                intent=intent,
                venue_order_id=None,
                now=now,
            )
            return self._submission_result(
                CoordinatorCode.ENVELOPE_COLLISION,
                intent,
                kill_reason=kill_reason,
            )
        evidence, now, boundary_code = self._fresh_mutation_evidence(
            intent,
            ExecutionOperation.SIGN_ORDER,
        )
        if boundary_code is not None or evidence is None:
            kill_reason = None
            if boundary_code is CoordinatorCode.AUTHORITY_DENIED:
                kill_reason = self._engage_kill(
                    trigger=CoordinatorCode.AUTHORITY_DENIED.value,
                    intent=intent,
                    venue_order_id=None,
                    now=now,
                )
            return self._submission_result(
                boundary_code or CoordinatorCode.PREFLIGHT_EVIDENCE_INVALID,
                intent,
                kill_reason=kill_reason,
            )
        sign_failed = False
        envelope: object = None
        try:
            envelope = self._signer.sign(intent, evidence)
        except Exception:
            sign_failed = True
        if sign_failed or type(envelope) is not SignedOrderEnvelope:
            return self._unknown_after_port_failure(
                intent,
                trigger=CoordinatorCode.SIGNER_FAILED.value,
                now=now,
            )
        try:
            envelope = SignedOrderEnvelope.model_validate(
                envelope.model_dump(mode="python"),
                strict=True,
            )
        except Exception:
            return self._unknown_after_port_failure(
                intent,
                trigger="ORDER_ENVELOPE_INVALID",
                now=now,
            )
        if (
            envelope.intent_id != intent.intent_id
            or envelope.intent_fingerprint != intent.intent_fingerprint
            or envelope.protocol_version != intent.protocol_version
        ):
            return self._unknown_after_port_failure(
                intent,
                trigger="ORDER_ENVELOPE_MISMATCH",
                now=now,
            )
        now = self._now()
        submitting = self._order_event(
            intent,
            state=VenueOrderState.SUBMITTING,
            original_state=VenueOrderState.SUBMITTING.value,
            source_channel="coordinator",
            venue_order_id=self._synthetic_order_id(intent),
            raw_event_hash=envelope.exact_body_hash,
            now=now,
            lineage_hashes=(envelope.order_fingerprint,),
        )
        try:
            with self._store.transaction() as transaction:
                envelope_appended = transaction.append_signed_order_envelope(envelope)
                event_appended = transaction.append_venue_order_event(submitting)
                if not envelope_appended or not event_appended:
                    raise ConflictingRecordError("submission boundary already exists")
        except ConflictingRecordError:
            kill_reason = self._engage_kill(
                trigger=CoordinatorCode.ENVELOPE_COLLISION.value,
                intent=intent,
                venue_order_id=None,
                now=now,
            )
            return self._submission_result(
                CoordinatorCode.ENVELOPE_COLLISION,
                intent,
                kill_reason=kill_reason,
            )
        evidence, now, boundary_code = self._fresh_mutation_evidence(
            intent,
            ExecutionOperation.SUBMIT_ORDER,
        )
        if boundary_code is not None or evidence is None:
            kill_reason = None
            if boundary_code is CoordinatorCode.AUTHORITY_DENIED:
                kill_reason = self._engage_kill(
                    trigger=CoordinatorCode.AUTHORITY_DENIED.value,
                    intent=intent,
                    venue_order_id=None,
                    now=now,
                )
            return self._submission_result(
                boundary_code or CoordinatorCode.PREFLIGHT_EVIDENCE_INVALID,
                intent,
                event=submitting,
                kill_reason=kill_reason,
            )
        transport_failed = False
        result: object = None
        try:
            result = self._signer.submit(intent, envelope, evidence)
        except Exception:
            transport_failed = True
        now = self._now()
        if type(result) is RestResult:
            try:
                result = RestResult.model_validate(
                    result.model_dump(mode="python"),
                    strict=True,
                )
            except Exception:
                result = None
        if (
            transport_failed
            or type(result) is not RestResult
            or (type(result) is RestResult and result.route is not RouteKey.SUBMIT_ORDER)
            or (type(result) is RestResult and result.observed_at > now)
        ):
            return self._unknown_after_port_failure(
                intent,
                trigger="ORDER_OUTCOME_UNKNOWN",
                now=now,
            )
        return self._classify_submit_result(intent, result, now)


__all__ = [
    "AccountReadPort",
    "CoordinatorAuthorityPort",
    "CoordinatorCode",
    "ExecutionCoordinator",
    "PostFillDecision",
    "PreflightEvidence",
    "PreflightPort",
    "PreflightRefusal",
    "PreflightRefusalCode",
    "PreparationResult",
    "RecoveryReport",
    "SignerPort",
    "SubmissionResult",
]
