from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID

import pytest
from eth_account import Account

from polytrading.predictions.execution.models import ExecutionIntent, ExecutionOperation
from polytrading.predictions.pilot import signer_services
from polytrading.predictions.pilot.signer_services import offline_pilot_signer_service
from polytrading.predictions.polymarket_execution.ipc import (
    DescribeIdentityPayload,
    GeoblockEvidenceResult,
    ReadAccountPayload,
    ReadGeoblockPayload,
    SanitizedOperationResult,
    SignerCapabilityProof,
    SignerRequest,
    SignOrderPayload,
)
from polytrading.predictions.polymarket_execution.protocol import (
    POLYMARKET_PILOT_PROTOCOL_VERSION,
)
from polytrading.predictions.polymarket_execution.routes import (
    BalanceAllowancePayload,
    RestCode,
    RouteKey,
)
from polytrading.predictions.polymarket_execution.secrets import SecretMaterial
from tests.predictions.execution_helpers import execution_intent_fields
from tests.predictions.pilot_helpers import signer_capability_grant
from tests.predictions.test_polymarket_order_signing import PRIVATE_KEY

ACCOUNT_FINGERPRINT = sha256(bytes.fromhex(Account.from_key(PRIVATE_KEY).address[2:])).hexdigest()
NOW = datetime(2026, 8, 31, 16, tzinfo=UTC)
PUBLIC_KEY = b"p" * 32


def _request(
    operation: ExecutionOperation, *, now: datetime | None = None
) -> SignerRequest:
    now = now or datetime.now(UTC)
    grant = signer_capability_grant(account_fingerprint=ACCOUNT_FINGERPRINT, now=now)
    intent = ExecutionIntent(
        **execution_intent_fields(
            account_fingerprint=ACCOUNT_FINGERPRINT,
            capability_fingerprint=grant.plan_hash,
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
    elif operation is ExecutionOperation.READ_GEOBLOCK:
        payload = ReadGeoblockPayload(operation=operation)
    else:
        payload = ReadAccountPayload(
            operation=operation,
            signature_type=0,
            asset_type="COLLATERAL",
            token_id=None,
        )
    return SignerRequest(
        schema_version=1,
        request_id=UUID(
            {
                ExecutionOperation.DESCRIBE_IDENTITY: "00000000-0000-4000-8000-000000000001",
                ExecutionOperation.READ_ACCOUNT: "00000000-0000-4000-8000-000000000002",
                ExecutionOperation.SIGN_ORDER: "00000000-0000-4000-8000-000000000003",
                ExecutionOperation.READ_GEOBLOCK: "00000000-0000-4000-8000-000000000004",
            }[operation]
        ),
        intent_id=intent.intent_id,
        intent_fingerprint=intent.intent_fingerprint,
        capability_digest=grant.plan_hash,
        authority_digest=(
            "0" * 64
            if operation
            in {
                ExecutionOperation.DESCRIBE_IDENTITY,
                ExecutionOperation.READ_ACCOUNT,
                ExecutionOperation.READ_GEOBLOCK,
            }
            else grant.digest
        ),
        authority_proof=(
            None
            if operation
            in {
                ExecutionOperation.DESCRIBE_IDENTITY,
                ExecutionOperation.READ_ACCOUNT,
                ExecutionOperation.READ_GEOBLOCK,
            }
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


def _full_secrets() -> SecretMaterial:
    return SecretMaterial(
        bytearray(PRIVATE_KEY),
        bytearray(b"pilot-api-key"),
        bytearray(base64.urlsafe_b64encode(b"pilot-api-secret")),
        bytearray(b"pilot-passphrase"),
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


def test_offline_service_refuses_the_geoblock_read_with_execution_unavailable() -> None:
    service = offline_pilot_signer_service(_secrets())
    try:
        response = service.handle(_request(ExecutionOperation.READ_GEOBLOCK))
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


def test_live_factory_constructs_fixed_rest_handlers_only_when_credentials_exist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeTransport:
        def __init__(self, *, timestamp: object, clock: object) -> None:
            if not callable(timestamp) or not callable(clock):
                raise TypeError("CALLABLES_REQUIRED")

    class FakeRestHandlers:
        def __init__(self, *, credentials: object, transport: object) -> None:
            if type(credentials) is not signer_services.ClobCredentials:
                raise TypeError("CLOB_CREDENTIALS_REQUIRED")
            if type(transport) is not FakeTransport:
                raise TypeError("FAKE_TRANSPORT_REQUIRED")

        def as_operation_handlers(self) -> signer_services.SignerOperationHandlers:
            def unreachable(*_args: object) -> object:
                raise AssertionError("UNEXPECTED_OPERATION")

            return signer_services.SignerOperationHandlers(
                submit_order=unreachable,
                cancel_order=unreachable,
                heartbeat=unreachable,
                read_orders=unreachable,
                read_trades=unreachable,
                    read_account=lambda _payload: SanitizedOperationResult(
                    operation=ExecutionOperation.READ_ACCOUNT,
                    result_code=RestCode.READ_OK,
                    evidence_hashes=("1" * 64,),
                    route=RouteKey.READ_BALANCE_ALLOWANCE,
                    observed_at=NOW,
                    raw_body_hash="1" * 64,
                    attempts=1,
                    recovery_required=False,
                    kill_required=False,
                    public_payload=BalanceAllowancePayload(
                        kind="BALANCE_ALLOWANCE",
                        balance="10",
                        allowances=(),
                        ),
                    ),
                    read_geoblock=lambda _payload: GeoblockEvidenceResult(
                        operation=ExecutionOperation.READ_GEOBLOCK,
                        allowed=True,
                        evidence_hash="9" * 64,
                        observed_at=NOW,
                        expires_at=NOW + timedelta(minutes=5),
                    ),
                )

    monkeypatch.setattr(
        signer_services,
        "HttpxPolymarketRestTransport",
        FakeTransport,
    )
    monkeypatch.setattr(
        signer_services,
        "SignerRestHandlers",
        FakeRestHandlers,
    )
    service = signer_services.live_pilot_signer_service(
        capability_public_key=PUBLIC_KEY,
        clock=lambda: NOW,
    )(_full_secrets())
    try:
        response = service.handle(_request(ExecutionOperation.READ_ACCOUNT, now=NOW))
        geoblock = service.handle(_request(ExecutionOperation.READ_GEOBLOCK, now=NOW))
    finally:
        service.close()

    assert response.ok is True
    assert response.result is not None
    assert response.result.result_code == "READ_OK"
    assert geoblock.ok is True
    assert type(geoblock.result) is GeoblockEvidenceResult
    assert geoblock.result.evidence_hash == "9" * 64


def test_wallet_only_live_factory_refuses_authenticated_operations_without_creating_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def transport_must_not_be_created(**_kwargs: object) -> object:
        raise AssertionError("WALLET_ONLY_MUST_NOT_CREATE_TRANSPORT")

    monkeypatch.setattr(
        signer_services,
        "HttpxPolymarketRestTransport",
        transport_must_not_be_created,
        raising=False,
    )
    service = signer_services.live_pilot_signer_service(
        capability_public_key=PUBLIC_KEY,
        clock=lambda: NOW,
    )(_secrets())
    try:
        identity = service.handle(_request(ExecutionOperation.DESCRIBE_IDENTITY, now=NOW))
        response = service.handle(_request(ExecutionOperation.READ_ACCOUNT, now=NOW))
    finally:
        service.close()

    assert identity.ok is True
    assert response.ok is False
    assert response.error_code == "CREDENTIALS_UNAVAILABLE"


def test_live_read_guard_expires_at_the_five_minute_boundary() -> None:
    guard = signer_services._live_read_guard(
        account_fingerprint=ACCOUNT_FINGERPRINT,
        credentials_present=True,
        expires_at=NOW + timedelta(minutes=5),
    )

    decision = guard(
        _request(ExecutionOperation.READ_ACCOUNT, now=NOW + timedelta(minutes=5)),
        NOW + timedelta(minutes=5),
    )

    assert decision.allowed is False
    assert decision.reason == "CAPABILITY_EXPIRED"


def test_live_read_guard_accepts_only_the_fixed_geoblock_read() -> None:
    guard = signer_services._live_read_guard(
        account_fingerprint=ACCOUNT_FINGERPRINT,
        credentials_present=True,
        expires_at=NOW + timedelta(minutes=5),
    )

    decision = guard(_request(ExecutionOperation.READ_GEOBLOCK, now=NOW), NOW)

    assert decision.allowed is True
    assert decision.reason is None


def test_live_read_guard_never_allows_a_mutation() -> None:
    guard = signer_services._live_read_guard(
        account_fingerprint=ACCOUNT_FINGERPRINT,
        credentials_present=True,
        expires_at=NOW + timedelta(minutes=5),
    )

    decision = guard(_request(ExecutionOperation.SIGN_ORDER, now=NOW), NOW)

    assert decision.allowed is False
    assert decision.reason == "CAPABILITY_OPERATION_NOT_ALLOWED"


def test_live_read_guard_is_bound_to_the_signed_account() -> None:
    guard = signer_services._live_read_guard(
        account_fingerprint="f" * 64,
        credentials_present=True,
        expires_at=NOW + timedelta(minutes=5),
    )

    decision = guard(_request(ExecutionOperation.READ_ACCOUNT, now=NOW), NOW)

    assert decision.allowed is False
    assert decision.reason == "CAPABILITY_ACCOUNT_MISMATCH"
