"""The last hop: one framed request to the signer sidecar, and its sanitized answer back.

This module speaks the existing signer IPC and nothing else. It holds no secret, opens no socket,
and constructs no transport: the descriptors it writes to were inherited at launch, and the signer
on the other side re-verifies the capability, protocol, route set, and account for itself before
it touches the venue.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from decimal import Decimal
from typing import BinaryIO
from uuid import UUID, uuid4

from polytrading.predictions.execution.models import ExecutionIntent, ExecutionOperation
from polytrading.predictions.pilot.selector import PilotAccountState
from polytrading.predictions.pilot.sessions import LegOutcome
from polytrading.predictions.polymarket_execution.ipc import (
    CancelOrderPayload,
    ReadAccountPayload,
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

    # -- internals ----------------------------------------------------------------------

    def _exchange(
        self,
        intent: ExecutionIntent | None,
        operation: ExecutionOperation,
        payload: object,
        capability_id: UUID | None,
    ) -> SignerResponse:
        now = self._clock()
        request = SignerRequest(
            schema_version=1,
            request_id=uuid4(),
            intent_id=intent.intent_id if intent is not None else uuid4(),
            intent_fingerprint=intent.intent_fingerprint if intent is not None else "0" * 64,
            capability_digest=(intent.capability_fingerprint if intent is not None else "0" * 64),
            manifest_digest=self._manifest_digest,
            account_fingerprint=self._account_fingerprint,
            protocol_version=POLYMARKET_PILOT_PROTOCOL_VERSION,
            operation=operation,
            deadline=now + REQUEST_DEADLINE,
            payload=payload,  # type: ignore[arg-type]
        )
        del capability_id  # the signer resolves authority from the request it verifies itself
        try:
            write_frame(self._request_stream, canonical_request_bytes(request))
            frame = read_frame(self._response_stream)
        except SignerProtocolError as error:
            raise SignerLinkError("IPC_EXCHANGE_FAILED") from error
        response = SignerResponse.model_validate_json(frame)
        if response.request_id != request.request_id:
            raise SignerLinkError("IPC_REQUEST_COLLISION")
        if not response.ok:
            raise SignerLinkError(str(response.error_code))
        return response

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


__all__ = ["REQUEST_DEADLINE", "SignerLinkError", "SignerLinkVenuePort"]
