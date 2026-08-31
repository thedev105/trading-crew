"""Closed signer-service composition for posture-only and live pilot launches."""

from __future__ import annotations

import hmac
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from hashlib import sha256

from eth_account import Account

from polytrading.predictions.execution.authority import AuthorityDecision
from polytrading.predictions.execution.models import ExecutionOperation
from polytrading.predictions.pilot.signer_bootstrap import SignerServiceFactory
from polytrading.predictions.polymarket_execution.auth import ClobCredentials
from polytrading.predictions.polymarket_execution.ipc import SignerRequest
from polytrading.predictions.polymarket_execution.protocol import (
    POLYMARKET_PILOT_PROTOCOL_VERSION,
    load_protocol_snapshot,
)
from polytrading.predictions.polymarket_execution.rest import (
    HttpxPolymarketRestTransport,
    SignerRestHandlers,
)
from polytrading.predictions.polymarket_execution.secrets import SecretMaterial
from polytrading.predictions.polymarket_execution.signer import (
    SignerOperationHandlers,
    SignerService,
)

_LIVE_READ_OPERATIONS = frozenset(
    {
        ExecutionOperation.READ_ACCOUNT,
        ExecutionOperation.READ_ORDERS,
        ExecutionOperation.READ_TRADES,
    }
)
_LIVE_READ_LIFETIME = timedelta(minutes=5)


def _unavailable(*_: object) -> AuthorityDecision:
    return AuthorityDecision(False, "EXECUTION_UNAVAILABLE", ())


def _unreachable(*_: object) -> object:
    raise RuntimeError("EXECUTION_UNAVAILABLE")


def _credentials_unavailable(*_: object) -> AuthorityDecision:
    return AuthorityDecision(False, "CREDENTIALS_UNAVAILABLE", ())


def _credentials_unreachable(*_: object) -> object:
    raise RuntimeError("CREDENTIALS_UNAVAILABLE")


def _account_identity(secrets: SecretMaterial) -> tuple[str, str]:
    private_key = bytes(secrets.private_key)
    try:
        address = Account.from_key(private_key).address
        fingerprint = sha256(bytes.fromhex(address[2:])).hexdigest()
        return address, fingerprint
    finally:
        private_key = b""


def _live_read_guard(
    *,
    account_fingerprint: str,
    credentials_present: bool,
    expires_at: datetime,
) -> Callable[[SignerRequest, datetime], AuthorityDecision]:
    def guard(request: SignerRequest, observed_at: datetime) -> AuthorityDecision:
        if request.operation not in _LIVE_READ_OPERATIONS:
            return AuthorityDecision(False, "CAPABILITY_OPERATION_NOT_ALLOWED", ())
        if not hmac.compare_digest(request.account_fingerprint, account_fingerprint):
            return AuthorityDecision(False, "CAPABILITY_ACCOUNT_MISMATCH", ())
        if observed_at >= expires_at:
            return AuthorityDecision(False, "CAPABILITY_EXPIRED", ())
        if not credentials_present:
            return _credentials_unavailable()
        return AuthorityDecision(True, None, ())

    return guard


def live_pilot_signer_service(
    *,
    capability_public_key: bytes,
    clock: Callable[[], datetime],
) -> SignerServiceFactory:
    """Close over public launch state; create all secret-bearing objects in the child."""

    def factory(secrets: SecretMaterial) -> SignerService:
        launched_at = clock()
        if (
            not isinstance(launched_at, datetime)
            or launched_at.tzinfo is None
            or launched_at.utcoffset() is None
        ):
            raise ValueError("IPC_CLOCK_INVALID")
        launched_at = launched_at.astimezone(UTC)
        address, account_fingerprint = _account_identity(secrets)
        credentials_present = secrets.credentials_present
        if credentials_present:
            credentials = ClobCredentials(
                address=address,
                api_key=bytes(secrets.api_key),
                secret=bytes(secrets.api_secret),
                passphrase=bytes(secrets.passphrase),
            )
            transport = HttpxPolymarketRestTransport(
                timestamp=lambda: str(int(clock().timestamp())),
                clock=clock,
            )
            handlers = SignerRestHandlers(
                credentials=credentials,
                transport=transport,
            ).as_operation_handlers()
            authority_context_factory = _unavailable
        else:
            handlers = SignerOperationHandlers(
                submit_order=_credentials_unreachable,
                cancel_order=_credentials_unreachable,
                heartbeat=_credentials_unreachable,
                read_orders=_credentials_unreachable,
                read_trades=_credentials_unreachable,
                read_account=_credentials_unreachable,
            )
            authority_context_factory = _credentials_unavailable
        return SignerService(
            secrets=secrets,
            authority_context_factory=authority_context_factory,
            read_guard=_live_read_guard(
                account_fingerprint=account_fingerprint,
                credentials_present=credentials_present,
                expires_at=launched_at + _LIVE_READ_LIFETIME,
            ),
            handlers=handlers,
            clock=clock,
            capability_public_key=capability_public_key,
            snapshot=load_protocol_snapshot(version=POLYMARKET_PILOT_PROTOCOL_VERSION),
        )

    return factory


def offline_pilot_signer_service(secrets: SecretMaterial) -> SignerService:
    """Build the identity-capable signer used before a venue transport exists."""
    return SignerService(
        secrets=secrets,
        authority_context_factory=_unavailable,
        read_guard=_unavailable,
        handlers=SignerOperationHandlers(
            submit_order=_unreachable,
            cancel_order=_unreachable,
            heartbeat=_unreachable,
            read_orders=_unreachable,
            read_trades=_unreachable,
            read_account=_unreachable,
        ),
        clock=lambda: datetime.now(UTC),
        snapshot=load_protocol_snapshot(version=POLYMARKET_PILOT_PROTOCOL_VERSION),
    )


__all__ = ["live_pilot_signer_service", "offline_pilot_signer_service"]
