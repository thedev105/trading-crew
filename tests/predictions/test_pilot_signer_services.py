from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID

from eth_account import Account

from polytrading.predictions.execution.models import ExecutionIntent, ExecutionOperation
from polytrading.predictions.pilot.signer_services import offline_pilot_signer_service
from polytrading.predictions.polymarket_execution.ipc import (
    DescribeIdentityPayload,
    ReadAccountPayload,
    SignerCapabilityProof,
    SignerRequest,
    SignOrderPayload,
)
from polytrading.predictions.polymarket_execution.protocol import (
    POLYMARKET_PILOT_PROTOCOL_VERSION,
)
from polytrading.predictions.polymarket_execution.secrets import SecretMaterial
from tests.predictions.execution_helpers import execution_intent_fields
from tests.predictions.pilot_helpers import signer_capability_grant
from tests.predictions.test_polymarket_order_signing import PRIVATE_KEY

ACCOUNT_FINGERPRINT = sha256(bytes.fromhex(Account.from_key(PRIVATE_KEY).address[2:])).hexdigest()


def _request(operation: ExecutionOperation) -> SignerRequest:
    now = datetime.now(UTC)
    grant = signer_capability_grant(account_fingerprint=ACCOUNT_FINGERPRINT, now=now)
    intent = ExecutionIntent(
        **execution_intent_fields(
            account_fingerprint=ACCOUNT_FINGERPRINT,
            capability_fingerprint=grant.digest,
            created_at=now,
            deadline=now + timedelta(seconds=10),
            protocol_version=POLYMARKET_PILOT_PROTOCOL_VERSION,
        )
    )
    payload: object
    if operation is ExecutionOperation.DESCRIBE_IDENTITY:
        payload = DescribeIdentityPayload(operation=operation)
    elif operation is ExecutionOperation.SIGN_ORDER:
        payload = SignOrderPayload(operation=operation, intent=intent)
    else:
        payload = ReadAccountPayload(
            operation=operation,
            signature_type=0,
            asset_type="COLLATERAL",
            token_id=None,
        )
    return SignerRequest(
        schema_version=1,
        request_id=UUID("00000000-0000-4000-8000-000000000001"),
        intent_id=intent.intent_id,
        intent_fingerprint=intent.intent_fingerprint,
        capability_digest=grant.digest,
        authority_proof=(
            None
            if operation in {ExecutionOperation.DESCRIBE_IDENTITY, ExecutionOperation.READ_ACCOUNT}
            else SignerCapabilityProof(
                grant=grant,
                signature=b"cHVibGljLXNpZ25hdHVyZQ==",
            )
        ),
        manifest_digest="0" * 64,
        account_fingerprint="0" * 64
        if operation is ExecutionOperation.DESCRIBE_IDENTITY
        else ACCOUNT_FINGERPRINT,
        protocol_version=POLYMARKET_PILOT_PROTOCOL_VERSION,
        operation=operation,
        deadline=now + timedelta(seconds=5),
        payload=payload,
    )


def _secrets() -> SecretMaterial:
    return SecretMaterial(
        bytearray(PRIVATE_KEY), bytearray(), bytearray(), bytearray()
    )


def test_offline_service_answers_describe_identity() -> None:
    service = offline_pilot_signer_service(_secrets())
    try:
        response = service.handle(_request(ExecutionOperation.DESCRIBE_IDENTITY))
    finally:
        service.close()

    assert response.ok
    assert response.result is not None
    assert response.result.account_fingerprint == ACCOUNT_FINGERPRINT


def test_offline_service_refuses_reads_with_execution_unavailable() -> None:
    service = offline_pilot_signer_service(_secrets())
    try:
        response = service.handle(_request(ExecutionOperation.READ_ACCOUNT))
    finally:
        service.close()

    assert response.ok is False
    assert response.error_code == "EXECUTION_UNAVAILABLE"


def test_offline_service_refuses_mutations_with_execution_unavailable() -> None:
    service = offline_pilot_signer_service(_secrets())
    try:
        response = service.handle(_request(ExecutionOperation.SIGN_ORDER))
    finally:
        service.close()

    assert response.ok is False
    assert response.error_code == "EXECUTION_UNAVAILABLE"
