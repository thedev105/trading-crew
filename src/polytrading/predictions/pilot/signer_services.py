"""Signer-service composition for a pilot build with no venue transport."""

from __future__ import annotations

from datetime import UTC, datetime

from polytrading.predictions.execution.authority import AuthorityDecision
from polytrading.predictions.polymarket_execution.secrets import SecretMaterial
from polytrading.predictions.polymarket_execution.signer import (
    SignerOperationHandlers,
    SignerService,
)
from polytrading.predictions.polymarket_execution.protocol import (
    POLYMARKET_PILOT_PROTOCOL_VERSION,
    load_protocol_snapshot,
)


def _unavailable(*_: object) -> AuthorityDecision:
    return AuthorityDecision(False, "EXECUTION_UNAVAILABLE", ())


def _unreachable(*_: object) -> object:
    raise RuntimeError("EXECUTION_UNAVAILABLE")


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


__all__ = ["offline_pilot_signer_service"]
