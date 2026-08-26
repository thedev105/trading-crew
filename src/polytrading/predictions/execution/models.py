from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256
from typing import Annotated, Literal
from uuid import UUID, uuid5

from pydantic import Field, StringConstraints, field_validator, model_validator

from polytrading.predictions.domain import (
    PredictionRecord,
    PredictionVenue,
    Sha256,
    normalize_utc_timestamp,
)

NonEmptyString = Annotated[str, StringConstraints(min_length=1)]
PositiveDecimal = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
NonNegativeDecimal = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
NonNegativeInt = Annotated[int, Field(ge=0)]
_INTENT_NAMESPACE = UUID("b59d5b2a-94e6-4a5e-b184-327132349d5e")
_PUBLIC_ORDER_ADDRESS_FIELDS = frozenset({"maker", "signer"})
_PUBLIC_ORDER_BYTES32_FIELDS = frozenset({"builder", "metadata"})
_PUBLIC_ORDER_STRING_INTEGER_FIELDS = frozenset(
    {"expiration", "makerAmount", "takerAmount", "timestamp", "tokenId"}
)
_PUBLIC_ORDER_JSON_INTEGER_FIELDS = frozenset({"salt", "signatureType"})
_PUBLIC_ORDER_FIELDS = (
    _PUBLIC_ORDER_ADDRESS_FIELDS
    | _PUBLIC_ORDER_BYTES32_FIELDS
    | _PUBLIC_ORDER_STRING_INTEGER_FIELDS
    | _PUBLIC_ORDER_JSON_INTEGER_FIELDS
    | {"side"}
)
_EVM_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}")
_BYTES32 = re.compile(r"0x[0-9a-fA-F]{64}")
_ASCII_INTEGER = re.compile(r"[0-9]+")


class ImmediateOrderType(StrEnum):
    FAK = "FAK"
    FOK = "FOK"


class ExecutionOperation(StrEnum):
    SIGN_ORDER = "SIGN_ORDER"
    SUBMIT_ORDER = "SUBMIT_ORDER"
    CANCEL_ORDER = "CANCEL_ORDER"
    HEARTBEAT = "HEARTBEAT"
    READ_ORDERS = "READ_ORDERS"
    READ_TRADES = "READ_TRADES"
    READ_ACCOUNT = "READ_ACCOUNT"


class VenueOrderState(StrEnum):
    PLANNED = "PLANNED"
    SIGNED = "SIGNED"
    SUBMITTING = "SUBMITTING"
    ACK_LIVE_UNEXPECTED = "ACK_LIVE_UNEXPECTED"
    ACK_MATCHED = "ACK_MATCHED"
    ACK_DELAYED = "ACK_DELAYED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_PENDING = "CANCEL_PENDING"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"
    RECONCILED = "RECONCILED"


class VenueTradeState(StrEnum):
    MATCHED_NOT_BROADCASTED = "MATCHED_NOT_BROADCASTED"
    MATCHED = "MATCHED"
    MINED = "MINED"
    CONFIRMED = "CONFIRMED"
    RETRYING = "RETRYING"
    FAILED = "FAILED"


def _json_default(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (UUID, StrEnum)):
        return str(value)
    if isinstance(value, datetime):
        return normalize_utc_timestamp(value).isoformat()
    raise TypeError(f"cannot canonically encode {type(value).__name__}")


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        default=_json_default,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def canonical_execution_hash(value: PredictionRecord | Mapping[str, object]) -> Sha256:
    payload = value.model_dump(mode="json") if isinstance(value, PredictionRecord) else value
    return sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _sorted_unique(value: tuple[Sha256, ...], field_name: str) -> tuple[Sha256, ...]:
    if value != tuple(sorted(set(value))):
        raise ValueError(f"{field_name} must be sorted and unique")
    return value


class _ExecutionRecord(PredictionRecord):
    lineage_hashes: tuple[Sha256, ...] = ()

    @field_validator("lineage_hashes")
    @classmethod
    def _validate_lineage(cls, value: tuple[Sha256, ...]) -> tuple[Sha256, ...]:
        return _sorted_unique(value, "lineage_hashes")


class LiveExecutionPlan(_ExecutionRecord):
    schema_version: Literal[1]
    plan_id: UUID
    proposal_id: UUID
    candidate_id: UUID
    proof_artifact_hash: Sha256
    economics_report_hash: Sha256
    venue: Literal[PredictionVenue.POLYMARKET]
    account_fingerprint: Sha256
    book_snapshot_ids: tuple[UUID, ...]
    fee_evidence_ids: tuple[UUID, ...]
    information_cutoff: datetime
    token_ids: tuple[NonEmptyString, ...]
    leg_order_types: tuple[ImmediateOrderType, ...]
    maximum_size: PositiveDecimal | None
    maximum_spend: PositiveDecimal | None
    limit_prices: tuple[PositiveDecimal, ...]
    fee_rate_bps_caps: tuple[NonNegativeInt, ...]
    assigned_capital: PositiveDecimal
    incomplete_exposure_reserve: NonNegativeDecimal
    risk_policy_hash: Sha256
    manifest_hash: Sha256
    eligibility_hash: Sha256
    protocol_hash: Sha256
    capability_fingerprint: Sha256
    book_deadline: datetime
    proof_deadline: datetime
    economics_deadline: datetime
    account_deadline: datetime
    geoblock_deadline: datetime
    kill_conditions: tuple[NonEmptyString, ...]
    unwind_conditions: tuple[NonEmptyString, ...]
    plan_fingerprint: Sha256
    observed_at: datetime

    @field_validator(
        "information_cutoff",
        "book_deadline",
        "proof_deadline",
        "economics_deadline",
        "account_deadline",
        "geoblock_deadline",
        "observed_at",
    )
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return normalize_utc_timestamp(value)

    @model_validator(mode="after")
    def _validate_plan(self) -> LiveExecutionPlan:
        count = len(self.token_ids)
        if not count:
            raise ValueError("plan must contain at least one Polymarket token")
        if not (
            len(self.leg_order_types)
            == len(self.limit_prices)
            == len(self.fee_rate_bps_caps)
            == count
        ):
            raise ValueError("per-leg plan fields must align with token_ids")
        if self.maximum_size is None and self.maximum_spend is None:
            raise ValueError("plan must define maximum_size or maximum_spend")
        if not self.kill_conditions or not self.unwind_conditions:
            raise ValueError("plan must declare kill and unwind conditions")
        deadlines = (
            self.book_deadline,
            self.proof_deadline,
            self.economics_deadline,
            self.account_deadline,
            self.geoblock_deadline,
        )
        if any(deadline <= self.observed_at for deadline in deadlines):
            raise ValueError("plan freshness deadlines must be after observed_at")
        return self


class ExecutionIntent(_ExecutionRecord):
    schema_version: Literal[1]
    intent_id: UUID
    plan_id: UUID
    leg_sequence: NonNegativeInt
    venue: Literal[PredictionVenue.POLYMARKET]
    token_id: NonEmptyString
    side: Literal["buy", "sell"]
    limit_price: PositiveDecimal
    tick_size: PositiveDecimal
    exchange_kind: Literal["standard", "negative_risk"]
    base_size: PositiveDecimal | None
    maximum_spend: PositiveDecimal | None
    order_type: ImmediateOrderType
    fee_rate_bps_cap: NonNegativeInt
    rounding_mode: NonEmptyString
    account_fingerprint: Sha256
    capability_fingerprint: Sha256
    created_at: datetime
    deadline: datetime
    protocol_version: NonEmptyString
    intent_fingerprint: Sha256

    @field_validator("created_at", "deadline")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return normalize_utc_timestamp(value)

    @model_validator(mode="after")
    def _validate_intent(self) -> ExecutionIntent:
        if self.base_size is None and self.maximum_spend is None:
            raise ValueError("intent must define base_size or maximum_spend")
        if self.deadline <= self.created_at:
            raise ValueError("deadline must be after created_at")
        if self.intent_fingerprint != _intent_fingerprint(self):
            raise ValueError("intent fingerprint does not match intent content")
        if self.intent_id != deterministic_intent_id(self):
            raise ValueError("intent_id does not match canonical intent content")
        return self


def _intent_content(intent: ExecutionIntent) -> dict[str, object]:
    payload = intent.model_dump(mode="json")
    payload.pop("intent_id", None)
    payload.pop("intent_fingerprint", None)
    return payload


def _intent_fingerprint(intent: ExecutionIntent) -> Sha256:
    return sha256(_canonical_json(_intent_content(intent)).encode("utf-8")).hexdigest()


def deterministic_intent_id(intent: ExecutionIntent) -> UUID:
    payload = intent.model_dump(mode="json")
    payload.pop("intent_id", None)
    return uuid5(_INTENT_NAMESPACE, _canonical_json(payload))


class SignedOrderEnvelope(_ExecutionRecord):
    schema_version: Literal[1]
    intent_id: UUID
    intent_fingerprint: Sha256
    protocol_version: NonEmptyString
    salt: NonNegativeInt
    signature_type: NonNegativeInt
    public_signature: Annotated[str, Field(pattern=r"^0x[0-9a-fA-F]+$")]
    domain_fingerprint: Sha256
    exact_body_hash: Sha256
    order_fingerprint: Sha256
    signer_version: NonEmptyString
    canonical_order_json: NonEmptyString
    exchange_fingerprint: Sha256 | None = None

    @field_validator("canonical_order_json")
    @classmethod
    def _public_canonical_order(cls, value: str) -> str:
        try:
            payload = json.loads(value)
        except json.JSONDecodeError as error:
            raise ValueError("canonical_order_json must contain JSON") from error
        if not isinstance(payload, dict):
            raise ValueError("canonical_order_json must contain an object")
        if value != _canonical_json(payload):
            raise ValueError("canonical_order_json must use canonical JSON")
        if set(payload) != _PUBLIC_ORDER_FIELDS:
            raise ValueError("canonical_order_json must contain only public order fields")
        for field in _PUBLIC_ORDER_ADDRESS_FIELDS:
            address = payload[field]
            if not isinstance(address, str) or _EVM_ADDRESS.fullmatch(address) is None:
                raise ValueError(f"canonical_order_json {field} must be an EVM address")
        for field in _PUBLIC_ORDER_BYTES32_FIELDS:
            value_bytes = payload[field]
            if not isinstance(value_bytes, str) or _BYTES32.fullmatch(value_bytes) is None:
                raise ValueError(f"canonical_order_json {field} must be bytes32")
        for field in _PUBLIC_ORDER_STRING_INTEGER_FIELDS:
            number = payload[field]
            if not isinstance(number, str) or _ASCII_INTEGER.fullmatch(number) is None:
                raise ValueError(f"canonical_order_json {field} must be an integer string")
        for field in _PUBLIC_ORDER_JSON_INTEGER_FIELDS:
            number = payload[field]
            if type(number) is not int or number < 0:
                raise ValueError(f"canonical_order_json {field} must be a nonnegative integer")
        if payload["side"] not in {"BUY", "SELL"}:
            raise ValueError("canonical_order_json side must be BUY or SELL")
        return value

    @model_validator(mode="after")
    def _intent_fingerprint_not_placeholder(self) -> SignedOrderEnvelope:
        if self.intent_fingerprint == "0" * 64:
            raise ValueError("intent fingerprint must match the source intent")
        return self


class VenueOrderEvent(_ExecutionRecord):
    schema_version: Literal[1]
    event_id: UUID
    venue: Literal[PredictionVenue.POLYMARKET]
    raw_event_hash: Sha256
    source_channel: NonEmptyString
    venue_order_id: NonEmptyString
    intent_id: UUID | None
    original_venue_state: NonEmptyString
    normalized_state: VenueOrderState
    terminal: bool
    venue_timestamp: datetime | None
    received_at: datetime
    sequence_number: NonNegativeInt | None
    protocol_version: NonEmptyString

    @field_validator("venue_timestamp", "received_at")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else normalize_utc_timestamp(value)

    @model_validator(mode="after")
    def _validate_terminal(self) -> VenueOrderEvent:
        terminal_states = {
            VenueOrderState.FILLED,
            VenueOrderState.CANCELLED,
            VenueOrderState.REJECTED,
            VenueOrderState.RECONCILED,
        }
        if self.terminal != (self.normalized_state in terminal_states):
            raise ValueError("terminal must match the normalized order state")
        return self


class VenueTradeEvent(_ExecutionRecord):
    schema_version: Literal[1]
    trade_event_id: UUID
    venue: Literal[PredictionVenue.POLYMARKET]
    raw_event_hash: Sha256
    source_channel: NonEmptyString
    venue_trade_id: NonEmptyString
    venue_order_id: NonEmptyString | None
    intent_id: UUID | None
    original_venue_state: NonEmptyString
    normalized_state: VenueTradeState
    terminal: bool
    venue_timestamp: datetime | None
    received_at: datetime
    sequence_number: NonNegativeInt | None
    protocol_version: NonEmptyString

    @field_validator("venue_timestamp", "received_at")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else normalize_utc_timestamp(value)

    @model_validator(mode="after")
    def _validate_terminal(self) -> VenueTradeEvent:
        terminal_states = {VenueTradeState.CONFIRMED, VenueTradeState.FAILED}
        if self.terminal != (self.normalized_state in terminal_states):
            raise ValueError("terminal must match the normalized trade state")
        return self


class ActivationEvidence(_ExecutionRecord):
    schema_version: Literal[1]
    activation_evidence_id: UUID
    capability_digest: Sha256
    manifest_digest: Sha256
    verifier_result: bool
    verified_at: datetime
    expires_at: datetime | None
    rejection_codes: tuple[NonEmptyString, ...] = ()

    @field_validator("verified_at", "expires_at")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else normalize_utc_timestamp(value)

    @model_validator(mode="after")
    def _validate_result(self) -> ActivationEvidence:
        if self.verifier_result and self.rejection_codes:
            raise ValueError("accepted activation evidence cannot contain rejection codes")
        if not self.verifier_result and not self.rejection_codes:
            raise ValueError("rejected activation evidence requires a stable rejection code")
        if self.expires_at is not None and self.expires_at <= self.verified_at:
            raise ValueError("activation expiration must be after verification")
        return self


class KillSwitchEvent(_ExecutionRecord):
    schema_version: Literal[1]
    kill_event_id: UUID
    trigger: NonEmptyString
    scope: NonEmptyString
    source_intent_id: UUID | None
    source_order_id: NonEmptyString | None
    prior_state: bool
    occurred_at: datetime
    clearance_evidence_hashes: tuple[Sha256, ...] = ()

    @field_validator("occurred_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return normalize_utc_timestamp(value)

    @field_validator("clearance_evidence_hashes")
    @classmethod
    def _clearance_hashes(cls, value: tuple[Sha256, ...]) -> tuple[Sha256, ...]:
        return _sorted_unique(value, "clearance_evidence_hashes")


class LiveLedgerPosting(_ExecutionRecord):
    schema_version: Literal[1]
    posting_id: UUID
    account_fingerprint: Sha256
    intent_id: UUID | None
    venue_order_id: NonEmptyString | None
    venue_trade_id: NonEmptyString | None
    settlement_hash: Sha256 | None
    fee_hash: Sha256 | None
    balance_evidence_hashes: tuple[Sha256, ...]
    debit_account: NonEmptyString
    credit_account: NonEmptyString
    asset_id: NonEmptyString
    debit_amount: NonNegativeDecimal
    credit_amount: NonNegativeDecimal
    occurred_at: datetime

    @field_validator("occurred_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return normalize_utc_timestamp(value)

    @field_validator("balance_evidence_hashes")
    @classmethod
    def _balance_hashes(cls, value: tuple[Sha256, ...]) -> tuple[Sha256, ...]:
        return _sorted_unique(value, "balance_evidence_hashes")

    @model_validator(mode="after")
    def _one_nonzero_side(self) -> LiveLedgerPosting:
        if (self.debit_amount > 0) == (self.credit_amount > 0):
            raise ValueError("exactly one ledger posting side must be nonzero")
        return self


class LiveReconciliation(_ExecutionRecord):
    schema_version: Literal[1]
    reconciliation_id: UUID
    account_fingerprint: Sha256
    observed_at: datetime
    complete: bool
    differences: tuple[NonEmptyString, ...]
    evidence_hashes: tuple[Sha256, ...]
    next_action: NonEmptyString | None
    venue_order_hashes: tuple[Sha256, ...] = ()
    venue_trade_hashes: tuple[Sha256, ...] = ()
    balance_hashes: tuple[Sha256, ...] = ()
    allowance_hashes: tuple[Sha256, ...] = ()
    expected_posting_ids: tuple[UUID, ...] = ()

    @field_validator("observed_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return normalize_utc_timestamp(value)

    @field_validator(
        "evidence_hashes",
        "venue_order_hashes",
        "venue_trade_hashes",
        "balance_hashes",
        "allowance_hashes",
    )
    @classmethod
    def _hashes(cls, value: tuple[Sha256, ...]) -> tuple[Sha256, ...]:
        return _sorted_unique(value, "reconciliation hashes")

    @model_validator(mode="after")
    def _validate_complete(self) -> LiveReconciliation:
        if self.complete and self.differences:
            raise ValueError("complete reconciliation cannot contain unexplained differences")
        if self.complete and self.next_action is not None:
            raise ValueError("complete reconciliation cannot require a next action")
        if not self.complete and not self.differences:
            raise ValueError("incomplete reconciliation requires unexplained differences")
        if not self.complete and self.next_action is None:
            raise ValueError("incomplete reconciliation requires a next action")
        return self


class ProtocolConformanceResult(_ExecutionRecord):
    schema_version: Literal[1]
    conformance_result_id: UUID
    fixture_hashes: tuple[Sha256, ...]
    source_hashes: tuple[Sha256, ...]
    implementation_revision: NonEmptyString
    executed_checks: tuple[NonEmptyString, ...]
    result: NonEmptyString
    observed_at: datetime
    failure_fingerprints: tuple[Sha256, ...] = ()

    @field_validator("observed_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return normalize_utc_timestamp(value)

    @field_validator("fixture_hashes", "source_hashes", "failure_fingerprints")
    @classmethod
    def _hashes(cls, value: tuple[Sha256, ...]) -> tuple[Sha256, ...]:
        return _sorted_unique(value, "conformance hashes")

    @field_validator("executed_checks")
    @classmethod
    def _checks(cls, value: tuple[NonEmptyString, ...]) -> tuple[NonEmptyString, ...]:
        if not value or len(set(value)) != len(value):
            raise ValueError("executed_checks must be nonempty and unique")
        return value
