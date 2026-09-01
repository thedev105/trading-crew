from __future__ import annotations

import io
import json
from base64 import b64encode
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid5

import pytest

from polytrading.predictions.domain import PredictionVenue
from polytrading.predictions.execution.models import (
    ExecutionIntent,
    ExecutionOperation,
    ImmediateOrderType,
    _intent_fingerprint,
    canonical_execution_hash,
    deterministic_intent_id,
)
from polytrading.predictions.pilot.capabilities import (
    MutationEvidence,
    PilotCapabilityIssuer,
    SignedMutationEvidence,
    SignerKillDirective,
)
from polytrading.predictions.pilot.execution_port import (
    CoordinatorExecutionPort,
    ExecutionEvidence,
)
from polytrading.predictions.pilot.models import PILOT_CEILINGS
from polytrading.predictions.pilot.runtime import _mutation_evidence_for
from polytrading.predictions.pilot.selector import FrozenPilotPlan, PilotAccountState, PilotLeg
from polytrading.predictions.pilot.signer_link import (
    SignerLinkError,
    SignerLinkVenuePort,
    describe_identity,
)
from polytrading.predictions.polymarket_execution.ipc import (
    SanitizedOperationResult,
    SignedEnvelopeResult,
    SignerCapabilityProof,
    SignerKillPayload,
    SignerKillResult,
    SignerResponse,
    canonical_response_bytes,
    parse_signer_request,
    write_frame,
)
from polytrading.predictions.polymarket_execution.protocol import (
    POLYMARKET_PILOT_PROTOCOL_VERSION,
)
from polytrading.predictions.polymarket_execution.routes import (
    AllowanceEntry,
    BalanceAllowancePayload,
    CancellationPayload,
    OrderAckPayload,
    OrdersReadPayload,
    RestCode,
    RouteKey,
    TradesReadPayload,
)
from tests.predictions.pilot_helpers import (
    ACCOUNT_FINGERPRINT,
    CAPABILITY_ID,
    WALLET_FINGERPRINT,
    signer_capability_grant,
)
from tests.predictions.test_execution_authority import ELIGIBLE_MANIFEST

NOW = datetime(2026, 8, 30, 12, tzinfo=UTC)
MANIFEST_DIGEST = canonical_execution_hash(ELIGIBLE_MANIFEST)
SIGNER_PROOF = SignerCapabilityProof(
    grant=signer_capability_grant(account_fingerprint=ACCOUNT_FINGERPRINT, now=NOW),
    signature=b64encode(b"test-signature"),
)
MUTATION_EVIDENCE = SignedMutationEvidence(
    evidence=MutationEvidence(
        schema_version=1,
        manifest=ELIGIBLE_MANIFEST,
        manifest_record_hash=MANIFEST_DIGEST,
        account_fingerprint=ACCOUNT_FINGERPRINT,
        reconciliation_hash="8" * 64,
        reconciliation_observed_at=NOW,
        geoblock_allowed=True,
        geoblock_evidence_hash="9" * 64,
        geoblock_expires_at=NOW + timedelta(minutes=1),
        account_scope_evidence_hash="a" * 64,
        account_scope_expires_at=NOW + timedelta(minutes=1),
        kill_engaged=False,
        operator_present=True,
        plan_digest=SIGNER_PROOF.grant.plan_hash,
        authority_digest=SIGNER_PROOF.grant.digest,
        requested_notional=Decimal("4"),
        capital_after=Decimal("4"),
        position_after=Decimal("4"),
        loss_after=Decimal("0"),
        issued_at=NOW,
        expires_at=NOW + timedelta(seconds=5),
    ),
    signature=b64encode(b"test-evidence-signature"),
)


def mutation_evidence_for(
    target: ExecutionIntent,
    proof: SignerCapabilityProof,
) -> SignedMutationEvidence:
    return SignedMutationEvidence(
        evidence=MUTATION_EVIDENCE.evidence.model_copy(
            update={
                "account_fingerprint": target.account_fingerprint,
                "plan_digest": target.capability_fingerprint,
                "authority_digest": proof.grant.digest,
                "requested_notional": target.maximum_spend or Decimal("0"),
            }
        ),
        signature=b64encode(MUTATION_EVIDENCE.signature),
    )


def intent(**overrides: Any) -> ExecutionIntent:
    fields: dict[str, Any] = {
        "schema_version": 1,
        "intent_id": UUID(int=0),
        "plan_id": uuid5(UUID("2b0f0a54-1f1d-4f8f-9a3f-6f7f8b3a51c2"), "plan"),
        "leg_sequence": 0,
        "venue": PredictionVenue.POLYMARKET,
        "token_id": "217426",
        "side": "buy",
        "limit_price": Decimal("0.40"),
        "tick_size": Decimal("0.01"),
        "exchange_kind": "standard",
        "base_size": Decimal("10"),
        "maximum_spend": Decimal("4.00"),
        "order_type": ImmediateOrderType.FAK,
        "fee_rate_bps_cap": 0,
        "rounding_mode": "ROUND_DOWN",
        "account_fingerprint": ACCOUNT_FINGERPRINT,
        "capability_fingerprint": "b" * 64,
        "created_at": NOW,
        "deadline": NOW + timedelta(seconds=30),
        "protocol_version": POLYMARKET_PILOT_PROTOCOL_VERSION,
        "intent_fingerprint": "0" * 64,
    }
    fields.update(overrides)
    draft = ExecutionIntent.model_construct(**fields)
    fields["intent_fingerprint"] = _intent_fingerprint(draft)
    fields["intent_id"] = deterministic_intent_id(ExecutionIntent.model_construct(**fields))
    return ExecutionIntent.model_validate(fields)


class FakeSignerChannel:
    """A framed, in-memory stand-in for the signer sidecar on the other end of the pipe."""

    def __init__(self, *, codes: list[str] | None = None, ok: bool = True) -> None:
        self.requests: list[Any] = []
        self.codes = codes or []
        self.ok = ok
        self._responses = io.BytesIO()
        self._read_at = 0

    # request side ------------------------------------------------------------------
    def write(self, payload: bytes) -> int:
        return len(payload)

    def flush(self) -> None:
        return None

    def answer(self, frame: bytes) -> None:
        request = parse_signer_request(frame)
        self.requests.append(request)
        if request.operation is ExecutionOperation.SIGNER_KILL:
            response = (
                SignerResponse.accepted(
                    request.request_id,
                    SignerKillResult(
                        operation=ExecutionOperation.SIGNER_KILL,
                        result_code="SIGNER_KILL_ENGAGED",
                    ),
                )
                if self.ok
                else SignerResponse.rejected(request.request_id, "EXECUTION_KILL_ENGAGED")
            )
        elif request.operation is ExecutionOperation.SIGN_ORDER:
            response = SignerResponse.accepted(
                request.request_id,
                SignedEnvelopeResult(
                    operation=ExecutionOperation.SIGN_ORDER,
                    envelope=_envelope(request.payload.intent),
                ),
            )
        else:
            operation = request.operation.value
            code = self.codes.pop(0) if self.codes else _default_code(operation)
            public_payload = _payload_for(operation, code)
            response = (
                SignerResponse.accepted(
                    request.request_id,
                    SanitizedOperationResult(
                        operation=request.operation,
                        result_code=code,
                        evidence_hashes=_evidence_for(request.operation.value),
                        raw_body_hash="e" * 64,
                        request_body_hash=(
                            "f" * 64
                            if request.operation.value in {"SUBMIT_ORDER", "CANCEL_ORDER"}
                            else None
                        ),
                        venue_order_id=(
                            "order-1"
                            if request.operation.value in {"SUBMIT_ORDER", "CANCEL_ORDER"}
                            and code not in {RestCode.ORDER_OUTCOME_UNKNOWN, RestCode.AUTH_REJECTED}
                            else None
                        ),
                        route=_route_for(request.operation.value),
                        observed_at=NOW,
                        attempts=1,
                        **_flags_for(operation, code, public_payload),
                        public_payload=public_payload,
                    ),
                )
                if self.ok
                else SignerResponse.rejected(request.request_id, "EXECUTION_KILL_ENGAGED")
            )
        payload = canonical_response_bytes(response)
        position = self._responses.tell()
        self._responses.seek(0, io.SEEK_END)
        write_frame(self._responses, payload)
        self._responses.seek(position)


class RealSignerChannel(FakeSignerChannel):
    """Frames requests through the production signer service instead of a canned response."""

    def __init__(self, service: Any) -> None:
        super().__init__()
        self._service = service

    def answer(self, frame: bytes) -> None:
        request = parse_signer_request(frame)
        self.requests.append(request)
        response = self._service.handle(request)
        position = self._responses.tell()
        self._responses.seek(0, io.SEEK_END)
        write_frame(self._responses, canonical_response_bytes(response))
        self._responses.seek(position)


def _flags_for(operation: str, code: RestCode, payload: object | None) -> dict[str, bool]:
    """Take the recovery/kill flags from the route contract itself, not from a guess."""
    from polytrading.predictions.polymarket_execution.routes import expected_route_result_flags

    recovery, kill = expected_route_result_flags(
        route=_route_for(operation), code=code, payload=payload
    )
    return {"recovery_required": recovery, "kill_required": kill}


def _evidence_for(operation: str) -> tuple[str, ...]:
    if operation in {"SUBMIT_ORDER", "CANCEL_ORDER"}:
        return tuple(sorted({"e" * 64, "f" * 64}))
    return ("e" * 64,)


_ACK_STATUS = {
    RestCode.ORDER_ACK_MATCHED: "matched",
    RestCode.ORDER_ACK_DELAYED: "delayed",
    RestCode.ORDER_ACK_LIVE_UNEXPECTED: "live",
    RestCode.ORDER_ACK_UNMATCHED: "unmatched",
}


def _payload_for(operation: str, code: RestCode) -> object | None:
    if code in {
        RestCode.ORDER_ACK_MATCHED,
        RestCode.ORDER_ACK_DELAYED,
        RestCode.ORDER_ACK_LIVE_UNEXPECTED,
        RestCode.ORDER_ACK_UNMATCHED,
    }:
        return OrderAckPayload(
            kind="ORDER_ACK",
            order_id="order-1",
            status=_ACK_STATUS[code],
            making_amount="1",
            taking_amount="2",
            transaction_hashes=(),
            trade_ids=(),
        )
    if code is RestCode.CANCEL_ACKNOWLEDGED:
        return CancellationPayload(
            kind="CANCELLATION", order_id="order-1", confirmation_required=True
        )
    if code is RestCode.READ_OK:
        if operation == "READ_ORDERS":
            return OrdersReadPayload(kind="ORDERS_READ", items=())
        if operation == "READ_TRADES":
            return TradesReadPayload(kind="TRADES_READ", items=())
        return BalanceAllowancePayload(
            kind="BALANCE_ALLOWANCE",
            balance="200",
            allowances=(AllowanceEntry(address="0x" + "11" * 20, amount="200"),),
        )
    return None


def _route_for(operation: str) -> RouteKey:
    return {
        "SUBMIT_ORDER": RouteKey.SUBMIT_ORDER,
        "CANCEL_ORDER": RouteKey.CANCEL_ORDER,
        "READ_ORDERS": RouteKey.READ_OPEN_ORDERS,
        "READ_TRADES": RouteKey.READ_TRADES,
        "READ_ACCOUNT": RouteKey.READ_BALANCE_ALLOWANCE,
    }[operation]


def _default_code(operation: str) -> RestCode:
    return {
        "SUBMIT_ORDER": RestCode.ORDER_ACK_MATCHED,
        "CANCEL_ORDER": RestCode.CANCEL_ACKNOWLEDGED,
        "READ_ORDERS": RestCode.READ_OK,
        "READ_TRADES": RestCode.READ_OK,
        "READ_ACCOUNT": RestCode.READ_OK,
    }[operation]


class _RequestStream(io.RawIOBase):
    """Captures each written frame and asks the fake signer to answer it immediately."""

    def __init__(self, channel: FakeSignerChannel) -> None:
        self._channel = channel
        self._buffer = bytearray()

    def write(self, payload: bytes) -> int:  # type: ignore[override]
        self._buffer.extend(payload)
        while len(self._buffer) >= 4:
            size = int.from_bytes(self._buffer[:4], "big")
            if len(self._buffer) < 4 + size:
                break
            frame = bytes(self._buffer[4 : 4 + size])
            del self._buffer[: 4 + size]
            self._channel.answer(frame)
        return len(payload)

    def flush(self) -> None:
        return None


def account_state(payload: Mapping[str, object]) -> PilotAccountState:
    del payload
    return PilotAccountState.model_validate(
        {
            "account_fingerprint": ACCOUNT_FINGERPRINT,
            "wallet_fingerprint": WALLET_FINGERPRINT,
            "collateral_usd": Decimal("200"),
            "allowance_usd": Decimal("200"),
            "kill_engaged": False,
            "observed_at": NOW,
        },
        strict=True,
    )


def port(channel: FakeSignerChannel, **overrides: Any) -> SignerLinkVenuePort:
    arguments: dict[str, Any] = {
        "request_stream": _RequestStream(channel),
        "response_stream": channel._responses,
        "account_fingerprint": ACCOUNT_FINGERPRINT,
        "manifest_digest": MANIFEST_DIGEST,
        "clock": lambda: NOW,
        "account_reader": account_state,
        "signed_envelope": _envelope,
        "proof_for": lambda capability_id: {CAPABILITY_ID: SIGNER_PROOF}[capability_id],
        "mutation_evidence": lambda intent, operation, proof: mutation_evidence_for(
            intent, proof
        ),
        "kill_directive": lambda capability_ids: SignerKillDirective(
            capability_ids=tuple(sorted(capability_ids, key=str)),
            issued_at=NOW,
            signature=b64encode(b"test-kill-signature"),
        ),
    }
    arguments.update(overrides)
    return SignerLinkVenuePort(**arguments)


def _envelope(target: ExecutionIntent) -> Any:
    from polytrading.predictions.execution.models import SignedOrderEnvelope
    from tests.predictions.execution_helpers import public_unsigned_order_json

    order = json.loads(public_unsigned_order_json())
    return SignedOrderEnvelope(
        schema_version=1,
        intent_id=target.intent_id,
        intent_fingerprint=target.intent_fingerprint,
        protocol_version=POLYMARKET_PILOT_PROTOCOL_VERSION,
        salt=int(order["salt"]),
        signature_type=int(order["signatureType"]),
        public_signature="0x" + "ab" * 65,
        domain_fingerprint="1" * 64,
        exact_body_hash="2" * 64,
        order_fingerprint="3" * 64,
        signer_version="eth-account==0.13.7",
        canonical_order_json=json.dumps(order, separators=(",", ":"), sort_keys=True),
        exchange_fingerprint="4" * 64,
    )


def test_a_submission_speaks_the_signer_protocol_under_the_pilot_checkpoint() -> None:
    channel = FakeSignerChannel()
    target = intent(capability_fingerprint=SIGNER_PROOF.grant.plan_hash)

    outcome = port(channel).submit(target, CAPABILITY_ID)

    request = channel.requests[0]
    assert request.protocol_version == POLYMARKET_PILOT_PROTOCOL_VERSION
    assert request.operation.value == "SUBMIT_ORDER"
    assert request.intent_id == target.intent_id
    assert request.account_fingerprint == ACCOUNT_FINGERPRINT
    assert outcome.state == "FILLED"
    assert outcome.notional_usd == Decimal("4.00")
    assert outcome.venue_order_id == "order-1"


def test_a_cancellation_uses_the_cancel_operation() -> None:
    channel = FakeSignerChannel()

    outcome = port(channel).cancel(
        intent(capability_fingerprint=SIGNER_PROOF.grant.plan_hash), CAPABILITY_ID
    )

    assert channel.requests[0].operation.value == "CANCEL_ORDER"
    assert outcome.state == "FILLED"


@pytest.mark.parametrize(
    ("code", "state"),
    [
        (RestCode.ORDER_OUTCOME_UNKNOWN, "UNKNOWN"),
        (RestCode.ORDER_ACK_DELAYED, "PARTIALLY_FILLED"),
        (RestCode.ORDER_ACK_UNMATCHED, "REJECTED"),
        (RestCode.AUTH_REJECTED, "REJECTED"),
    ],
)
def test_every_venue_answer_maps_to_one_lifecycle_state(code: RestCode, state: str) -> None:
    channel = FakeSignerChannel(codes=[code])

    outcome = port(channel).submit(
        intent(capability_fingerprint=SIGNER_PROOF.grant.plan_hash), CAPABILITY_ID
    )

    assert outcome.state == state
    if state != "FILLED":
        assert outcome.notional_usd == Decimal("0")


def test_a_refused_request_raises_the_signers_own_code() -> None:
    channel = FakeSignerChannel(ok=False)

    with pytest.raises(SignerLinkError) as raised:
        port(channel).submit(
            intent(capability_fingerprint=SIGNER_PROOF.grant.plan_hash), CAPABILITY_ID
        )

    assert raised.value.code == "EXECUTION_KILL_ENGAGED"


def test_an_unbound_sidecar_startup_failure_preserves_its_code() -> None:
    response_stream = io.BytesIO()
    write_frame(
        response_stream,
        canonical_response_bytes(
            SignerResponse.rejected(None, "IPC_SERVICE_INITIALIZATION_FAILED")
        ),
    )
    response_stream.seek(0)

    with pytest.raises(SignerLinkError) as raised:
        describe_identity(io.BytesIO(), response_stream, clock=lambda: NOW)

    assert raised.value.code == "IPC_SERVICE_INITIALIZATION_FAILED"


def test_an_account_read_returns_authoritative_state() -> None:
    channel = FakeSignerChannel()

    state = port(channel).account_state()

    assert channel.requests[0].operation.value == "READ_ACCOUNT"
    assert state.collateral_usd == Decimal("200")


def test_positions_are_read_per_tracked_token_and_never_remembered() -> None:
    channel = FakeSignerChannel()
    linked = port(
        channel,
        tracked_tokens=("217426",),
        position_reader=lambda payload: Decimal("10"),
    )

    positions = linked.positions()

    assert positions == {"217426": Decimal("10")}
    assert channel.requests[0].payload.asset_type == "CONDITIONAL"
    assert channel.requests[0].payload.token_id == "217426"


def test_no_tracked_token_means_no_claimed_position() -> None:
    channel = FakeSignerChannel()

    assert port(channel).positions() == {}
    assert channel.requests == []


def test_orders_exposes_only_the_typed_sanitized_open_order_read() -> None:
    channel = FakeSignerChannel()

    result = port(channel).orders()

    request = channel.requests[0]
    assert request.operation is ExecutionOperation.READ_ORDERS
    assert request.payload.venue_order_id is None
    assert type(result) is SanitizedOperationResult
    assert type(result.public_payload) is OrdersReadPayload


def test_trades_exposes_only_the_typed_sanitized_trade_read() -> None:
    channel = FakeSignerChannel()

    result = port(channel).trades()

    request = channel.requests[0]
    assert request.operation is ExecutionOperation.READ_TRADES
    assert type(result) is SanitizedOperationResult
    assert type(result.public_payload) is TradesReadPayload


def test_submit_serializes_the_matching_public_proof() -> None:
    channel = FakeSignerChannel()
    target = intent(capability_fingerprint=SIGNER_PROOF.grant.plan_hash)

    port(channel).submit(target, CAPABILITY_ID)

    request = channel.requests[0]
    assert request.authority_proof is not None
    assert request.authority_proof.grant.capability_id == CAPABILITY_ID
    assert request.capability_digest == target.capability_fingerprint
    assert request.authority_digest == SIGNER_PROOF.grant.digest


def test_submit_serializes_matching_short_lived_mutation_evidence() -> None:
    channel = FakeSignerChannel()
    target = intent(capability_fingerprint=SIGNER_PROOF.grant.plan_hash)

    port(channel).submit(target, CAPABILITY_ID)

    request = channel.requests[0]
    assert request.mutation_evidence == MUTATION_EVIDENCE
    assert request.deadline == MUTATION_EVIDENCE.evidence.expires_at


def test_runtime_envelope_keeps_reconciliation_and_account_scope_hashes_distinct() -> None:
    issuer = PilotCapabilityIssuer(key_id="test-launch")
    target = intent(capability_fingerprint=SIGNER_PROOF.grant.plan_hash)
    current = ExecutionEvidence(
        manifest=ELIGIBLE_MANIFEST,
        account=account_state({}),
        reconciliation_hash="8" * 64,
        reconciliation_observed_at=NOW,
        geoblock_allowed=True,
        geoblock_evidence_hash="9" * 64,
        geoblock_expires_at=NOW + timedelta(minutes=1),
        account_scope_evidence_hash="a" * 64,
        account_scope_expires_at=NOW + timedelta(minutes=1),
        kill_engaged=False,
        operator_present=True,
    )
    try:
        signed = _mutation_evidence_for(
            issuer=issuer,
            intent=target,
            operation=ExecutionOperation.SUBMIT_ORDER,
            proof=SIGNER_PROOF,
            current_evidence=lambda: current,
            clock=lambda: NOW,
        )
    finally:
        issuer.close()

    assert signed.evidence.reconciliation_hash == "8" * 64
    assert signed.evidence.account_scope_evidence_hash == "a" * 64


def test_production_link_signs_then_submits_on_the_same_typed_channel() -> None:
    channel = FakeSignerChannel()
    target = intent(capability_fingerprint=SIGNER_PROOF.grant.plan_hash)

    outcome = port(channel, signed_envelope=None).submit(target, CAPABILITY_ID)

    assert [request.operation for request in channel.requests] == [
        ExecutionOperation.SIGN_ORDER,
        ExecutionOperation.SUBMIT_ORDER,
    ]
    assert channel.requests[1].payload.envelope == _envelope(target)
    assert outcome.state == "FILLED"


def test_submit_serializes_a_coordinator_intent_under_its_matching_grant() -> None:
    frozen = FrozenPilotPlan.model_validate(
        {
            "schema_version": 1,
            "proof_id": UUID("00000000-0000-0000-0000-000000006001"),
            "proposal_id": UUID("70000000-0000-0000-0000-000000000001"),
            "account_fingerprint": ACCOUNT_FINGERPRINT,
            "wallet_fingerprint": WALLET_FINGERPRINT,
            "legs": (
                PilotLeg(
                    leg_index=0,
                    outcome_token_id="217426",
                    side="buy",
                    limit_price=Decimal("0.40"),
                    size=Decimal("10"),
                    order_type=ImmediateOrderType.FAK,
                ),
            ),
            "recovery_branches": (),
            "gross_notional_usd": Decimal("4"),
            "deployed_capital_usd": Decimal("4"),
            "worst_case_incomplete_loss_usd": Decimal("0"),
            "effective_limits": PILOT_CEILINGS,
            "evidence_hashes": ("e" * 64,),
            "deadline": NOW + timedelta(seconds=30),
            "information_cutoff": NOW,
        },
        strict=True,
    )
    coordinator = CoordinatorExecutionPort(
        store=object(),  # type: ignore[arg-type]
        signer=object(),  # type: ignore[arg-type]
        verifier=object(),  # type: ignore[arg-type]
        grants={},
        evidence=lambda: object(),  # type: ignore[return-value]
        clock=lambda: NOW,
    )
    target = coordinator._intent_for(frozen, frozen.legs[0])
    proof = SIGNER_PROOF.model_copy(
        update={"grant": SIGNER_PROOF.grant.model_copy(update={"plan_hash": frozen.plan_hash})}
    )
    channel = FakeSignerChannel()

    port(channel, proof_for=lambda capability_id: {CAPABILITY_ID: proof}[capability_id]).submit(
        target, CAPABILITY_ID
    )

    request = channel.requests[0]
    assert request.capability_digest == target.capability_fingerprint
    assert request.authority_digest == proof.grant.digest
    assert request.authority_proof == proof


def test_a_real_signer_accepts_a_coordinator_intent_bound_to_the_frozen_plan() -> None:
    from polytrading.predictions.execution.models import canonical_execution_hash
    from polytrading.predictions.polymarket_execution import load_protocol_snapshot
    from polytrading.predictions.polymarket_execution.order import sign_order
    from tests.predictions.test_execution_authority import MANIFEST_HASH
    from tests.predictions.test_polymarket_order_signing import (
        ACCOUNT_FINGERPRINT as REAL_ACCOUNT_FINGERPRINT,
    )
    from tests.predictions.test_polymarket_order_signing import (
        PRIVATE_KEY,
    )
    from tests.predictions.test_polymarket_signer_ipc import (
        ISSUER,
        _capability_grant,
        _service,
    )
    from tests.predictions.test_polymarket_signer_ipc import (
        NOW as SIGNER_NOW,
    )

    frozen = FrozenPilotPlan.model_validate(
        {
            "schema_version": 1,
            "proof_id": UUID("00000000-0000-0000-0000-000000006001"),
            "proposal_id": UUID("70000000-0000-0000-0000-000000000001"),
            "account_fingerprint": REAL_ACCOUNT_FINGERPRINT,
            "wallet_fingerprint": WALLET_FINGERPRINT,
            "legs": (
                PilotLeg(
                    leg_index=0,
                    outcome_token_id="217426",
                    side="buy",
                    limit_price=Decimal("0.40"),
                    size=Decimal("10"),
                    order_type=ImmediateOrderType.FAK,
                ),
            ),
            "recovery_branches": (),
            "gross_notional_usd": Decimal("4"),
            "deployed_capital_usd": Decimal("4"),
            "worst_case_incomplete_loss_usd": Decimal("0"),
            "effective_limits": PILOT_CEILINGS,
            "evidence_hashes": ("e" * 64,),
            "deadline": SIGNER_NOW + timedelta(seconds=30),
            "information_cutoff": SIGNER_NOW,
        },
        strict=True,
    )
    coordinator = CoordinatorExecutionPort(
        store=object(),  # type: ignore[arg-type]
        signer=object(),  # type: ignore[arg-type]
        verifier=object(),  # type: ignore[arg-type]
        grants={},
        evidence=lambda: object(),  # type: ignore[return-value]
        clock=lambda: SIGNER_NOW,
    )
    target = coordinator._intent_for(frozen, frozen.legs[0])
    snapshot = load_protocol_snapshot(version=POLYMARKET_PILOT_PROTOCOL_VERSION)
    fixture_hash = canonical_execution_hash(
        {
            "version": snapshot.version,
            "fixtures": [item.model_dump(mode="json") for item in snapshot.fixture_hashes],
        }
    )
    base_grant = _capability_grant()
    grant = base_grant.model_copy(
        update={
            "plan_hash": frozen.plan_hash,
            "venue_binding": base_grant.venue_binding.model_copy(
                update={"protocol_fixture_hash": fixture_hash}
            ),
        }
    )
    proof = ISSUER.proof(grant)
    service = _service(now=SIGNER_NOW, snapshot=snapshot)
    channel = RealSignerChannel(service)
    try:
        outcome = port(
            channel,
            account_fingerprint=REAL_ACCOUNT_FINGERPRINT,
            manifest_digest=MANIFEST_HASH,
            clock=lambda: SIGNER_NOW,
            signed_envelope=lambda intent: sign_order(
                intent, PRIVATE_KEY, snapshot
            ),
                proof_for=lambda capability_id: {grant.capability_id: proof}[capability_id],
                mutation_evidence=None,
            ).submit(target, grant.capability_id)
    finally:
        service.close()

    request = channel.requests[0]
    assert outcome.state == "FILLED"
    assert request.capability_digest == frozen.plan_hash
    assert request.authority_digest == grant.digest
    assert request.authority_proof == proof


def test_engage_kill_sends_only_a_signed_kill_payload() -> None:
    channel = FakeSignerChannel()

    port(channel).engage_kill([CAPABILITY_ID])

    request = channel.requests[0]
    assert request.operation is ExecutionOperation.SIGNER_KILL
    assert isinstance(request.payload, SignerKillPayload)
    assert request.authority_proof is None


def test_engage_kill_raises_the_sanitized_rejection_code() -> None:
    channel = FakeSignerChannel(ok=False)

    with pytest.raises(SignerLinkError) as raised:
        port(channel).engage_kill([CAPABILITY_ID])

    assert raised.value.code == "EXECUTION_KILL_ENGAGED"
