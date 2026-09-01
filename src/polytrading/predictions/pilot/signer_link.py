"""The last hop: one framed request to the signer sidecar, and its sanitized answer back.

This module speaks the existing signer IPC and nothing else. It holds no secret, opens no socket,
and constructs no transport: the descriptors it writes to were inherited at launch, and the signer
on the other side re-verifies the capability, protocol, route set, and account for itself before
it touches the venue.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import BinaryIO
from uuid import UUID, uuid4

from polytrading.predictions.execution.models import ExecutionIntent, ExecutionOperation
from polytrading.predictions.pilot.capabilities import SignerKillDirective
from polytrading.predictions.pilot.selector import PilotAccountState
from polytrading.predictions.pilot.sessions import LegOutcome
from polytrading.predictions.polymarket_execution.ipc import (
    CancelOrderPayload,
    DescribeIdentityPayload,
    IdentityResult,
    ReadAccountPayload,
    ReadOrdersPayload,
    ReadTradesPayload,
    SanitizedOperationResult,
    SignerCapabilityProof,
    SignerKillPayload,
    SignerProtocolError,
    SignerRequest,
    SignerResponse,
    SubmitOrderPayload,
    canonical_request_bytes,
    read_frame,
    write_frame,
)
from polytrading.predictions.polymarket_execution.protocol import (
    POLYMARKET_PILOT_PROTOCOL_VERSION,
)

REQUEST_DEADLINE = timedelta(seconds=20)
IDENTITY_DEADLINE = timedelta(seconds=5)
_FILLED_CODES = frozenset({"SUBMIT_ORDER_OK", "ORDER_ACK_MATCHED"})
_REJECTED_CODES = frozenset({"ORDER_ACK_UNMATCHED", "AUTH_REJECTED", "PROTOCOL_RESPONSE_INVALID"})
_CANCELLED_CODES = frozenset({"CANCEL_ORDER_OK", "CANCEL_ACKNOWLEDGED"})


class SignerLinkError(ValueError):
    """A refused signer exchange, named by the signer's own stable code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class SignerLinkVenuePort:
    """Implements the executor's venue port over one inherited signer channel."""

    def __init__(
        self,
        *,
        request_stream: BinaryIO,
        response_stream: BinaryIO,
        account_fingerprint: str,
        manifest_digest: str,
        clock: Callable[[], datetime],
        account_reader: Callable[[Mapping[str, object]], PilotAccountState],
        signed_envelope: Callable[[ExecutionIntent], object],
        proof_for: Callable[[UUID], SignerCapabilityProof],
        kill_directive: Callable[[Iterable[UUID]], SignerKillDirective],
        tracked_tokens: tuple[str, ...] = (),
        position_reader: Callable[[Mapping[str, object]], Decimal] | None = None,
    ) -> None:
        self._request_stream = request_stream
        self._response_stream = response_stream
        self._account_fingerprint = account_fingerprint
        self._manifest_digest = manifest_digest
        self._clock = clock
        self._account_reader = account_reader
        self._signed_envelope = signed_envelope
        self._proof_for = proof_for
        self._kill_directive = kill_directive
        self._tracked_tokens = tracked_tokens
        self._position_reader = position_reader

    def submit(self, intent: ExecutionIntent, capability_id: UUID) -> LegOutcome:
        payload = SubmitOrderPayload(
            operation=ExecutionOperation.SUBMIT_ORDER,
            intent=intent,
            envelope=self._signed_envelope(intent),  # type: ignore[arg-type]
        )
        response = self._exchange(intent, ExecutionOperation.SUBMIT_ORDER, payload, capability_id)
        return self._outcome(intent, response)

    def cancel(self, intent: ExecutionIntent, capability_id: UUID) -> LegOutcome:
        venue_order_id = str(intent.intent_id)
        payload = CancelOrderPayload(
            operation=ExecutionOperation.CANCEL_ORDER, venue_order_id=venue_order_id
        )
        response = self._exchange(intent, ExecutionOperation.CANCEL_ORDER, payload, capability_id)
        return self._outcome(intent, response)

    def engage_kill(self, capability_ids: Iterable[UUID]) -> None:
        """Engage the signer-local kill switch using only a signed fixed directive."""
        payload = SignerKillPayload(
            operation=ExecutionOperation.SIGNER_KILL,
            directive=self._kill_directive(capability_ids),
        )
        self._exchange(None, ExecutionOperation.SIGNER_KILL, payload, None)

    def account_state(self) -> PilotAccountState:
        payload = ReadAccountPayload(
            operation=ExecutionOperation.READ_ACCOUNT,
            signature_type=0,
            asset_type="COLLATERAL",
            token_id=None,
        )
        response = self._exchange(None, ExecutionOperation.READ_ACCOUNT, payload, None)
        result = response.result
        if result is None:
            raise SignerLinkError("ACCOUNT_READ_UNAVAILABLE")
        return self._account_reader(result.model_dump(mode="json"))

    def positions(self) -> Mapping[str, Decimal]:
        """One authoritative conditional-asset read per tracked token; nothing is remembered."""
        if self._position_reader is None:
            return {}
        positions: dict[str, Decimal] = {}
        for token_id in self._tracked_tokens:
            payload = ReadAccountPayload(
                operation=ExecutionOperation.READ_ACCOUNT,
                signature_type=0,
                asset_type="CONDITIONAL",
                token_id=token_id,
            )
            response = self._exchange(None, ExecutionOperation.READ_ACCOUNT, payload, None)
            if response.result is None:
                raise SignerLinkError("ACCOUNT_READ_UNAVAILABLE")
            positions[token_id] = self._position_reader(response.result.model_dump(mode="json"))
        return positions

    def orders(self) -> SanitizedOperationResult:
        """Read the complete open-order collection through the one fixed signer operation."""
        response = self._exchange(
            None,
            ExecutionOperation.READ_ORDERS,
            ReadOrdersPayload(operation=ExecutionOperation.READ_ORDERS),
            None,
        )
        return _sanitized_read_result(response, ExecutionOperation.READ_ORDERS)

    def trades(self) -> SanitizedOperationResult:
        """Read the complete trade collection through the one fixed signer operation."""
        response = self._exchange(
            None,
            ExecutionOperation.READ_TRADES,
            ReadTradesPayload(operation=ExecutionOperation.READ_TRADES),
            None,
        )
        return _sanitized_read_result(response, ExecutionOperation.READ_TRADES)

    # -- internals ----------------------------------------------------------------------

    def _exchange(
        self,
        intent: ExecutionIntent | None,
        operation: ExecutionOperation,
        payload: object,
        capability_id: UUID | None,
    ) -> SignerResponse:
        now = self._clock()
        authority_proof = self._mutation_proof(operation, capability_id)
        request = SignerRequest(
            schema_version=1,
            request_id=uuid4(),
            intent_id=intent.intent_id if intent is not None else uuid4(),
            intent_fingerprint=intent.intent_fingerprint if intent is not None else "0" * 64,
            capability_digest=(intent.capability_fingerprint if intent is not None else "0" * 64),
            authority_digest=(
                authority_proof.grant.digest if authority_proof is not None else "0" * 64
            ),
            authority_proof=authority_proof,
            manifest_digest=self._manifest_digest,
            account_fingerprint=self._account_fingerprint,
            protocol_version=POLYMARKET_PILOT_PROTOCOL_VERSION,
            operation=operation,
            deadline=now + REQUEST_DEADLINE,
            payload=payload,  # type: ignore[arg-type]
        )
        try:
            write_frame(self._request_stream, canonical_request_bytes(request))
            frame = read_frame(self._response_stream)
        except SignerProtocolError as error:
            raise SignerLinkError("IPC_EXCHANGE_FAILED") from error
        response = SignerResponse.model_validate_json(frame)
        return _verified_response(request.request_id, response)

    def _mutation_proof(
        self,
        operation: ExecutionOperation,
        capability_id: UUID | None,
    ) -> SignerCapabilityProof | None:
        if operation not in {
            ExecutionOperation.SUBMIT_ORDER,
            ExecutionOperation.CANCEL_ORDER,
        }:
            return None
        if capability_id is None:
            raise SignerLinkError("CAPABILITY_PROOF_UNAVAILABLE")
        try:
            proof = self._proof_for(capability_id)
        except LookupError as error:
            raise SignerLinkError("CAPABILITY_PROOF_UNAVAILABLE") from error
        if proof.grant.capability_id != capability_id:
            raise SignerLinkError("CAPABILITY_PROOF_MISMATCH")
        return proof

    def _outcome(self, intent: ExecutionIntent, response: SignerResponse) -> LegOutcome:
        result = response.result
        code = getattr(result, "result_code", None)
        state = (
            "FILLED"
            if code in _FILLED_CODES or code in _CANCELLED_CODES
            else "REJECTED"
            if code in _REJECTED_CODES
            else "PARTIALLY_FILLED"
            if code == "ORDER_ACK_DELAYED"
            # Anything the signer did not classify is UNKNOWN, never assumed complete.
            else "UNKNOWN"
        )
        filled = intent.base_size or Decimal("0")
        return LegOutcome(
            leg_index=intent.leg_sequence,
            state=state,  # type: ignore[arg-type]
            filled_size=filled if state == "FILLED" else Decimal("0"),
            notional_usd=(intent.maximum_spend or Decimal("0"))
            if state == "FILLED"
            else Decimal("0"),
            venue_order_id=getattr(result, "venue_order_id", None),
            observed_at=getattr(result, "observed_at", None) or self._clock(),
        )


def describe_identity(
    request_stream: BinaryIO,
    response_stream: BinaryIO,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> tuple[str, str]:
    """Return the signer-derived public fingerprints before composing pilot services."""
    now = clock()
    request = SignerRequest(
        schema_version=1,
        request_id=uuid4(),
        intent_id=uuid4(),
        intent_fingerprint="0" * 64,
        capability_digest="0" * 64,
        authority_digest="0" * 64,
        manifest_digest="0" * 64,
        account_fingerprint="0" * 64,
        protocol_version=POLYMARKET_PILOT_PROTOCOL_VERSION,
        operation=ExecutionOperation.DESCRIBE_IDENTITY,
        deadline=now + IDENTITY_DEADLINE,
        payload=DescribeIdentityPayload(operation=ExecutionOperation.DESCRIBE_IDENTITY),
    )
    try:
        write_frame(request_stream, canonical_request_bytes(request))
        response = SignerResponse.model_validate_json(read_frame(response_stream))
    except SignerProtocolError as error:
        raise SignerLinkError("IPC_EXCHANGE_FAILED") from error
    _verified_response(request.request_id, response)
    if not isinstance(response.result, IdentityResult):
        raise SignerLinkError("IPC_REQUEST_INVALID")
    return response.result.account_fingerprint, response.result.wallet_fingerprint


def _verified_response(request_id: UUID, response: SignerResponse) -> SignerResponse:
    """Accept a reply bound to this request, preserving unbound sidecar failures."""
    if response.request_id is None and not response.ok:
        raise SignerLinkError(str(response.error_code))
    if response.request_id != request_id:
        raise SignerLinkError("IPC_REQUEST_COLLISION")
    if not response.ok:
        raise SignerLinkError(str(response.error_code))
    return response


def _sanitized_read_result(
    response: SignerResponse,
    operation: ExecutionOperation,
) -> SanitizedOperationResult:
    result = response.result
    if type(result) is not SanitizedOperationResult or result.operation is not operation:
        raise SignerLinkError("IPC_REQUEST_INVALID")
    return result


__all__ = ["REQUEST_DEADLINE", "SignerLinkError", "SignerLinkVenuePort", "describe_identity"]
