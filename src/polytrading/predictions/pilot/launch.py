"""Compose the deliberately transport-free pilot environment from verified evidence."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from decimal import Decimal
from http import HTTPStatus

from polytrading.predictions.domain import PredictionVenue, Sha256
from polytrading.predictions.execution.models import ExecutionOperation, canonical_execution_hash
from polytrading.predictions.manifest import VenueManifest
from polytrading.predictions.pilot.activation import PilotReconciliationState
from polytrading.predictions.pilot.capabilities import VenueBinding
from polytrading.predictions.pilot.execution_port import VenueSubmissionPort
from polytrading.predictions.pilot.models import PilotProofFamily
from polytrading.predictions.pilot.qualification import evaluate_pilot_qualification
from polytrading.predictions.pilot.reconciliation import reconcile_startup
from polytrading.predictions.pilot.selector import PilotAccountState
from polytrading.predictions.pilot.server import PilotRequestError
from polytrading.predictions.pilot.services import ExecutorFactory, PilotEnvironment
from polytrading.predictions.polymarket_execution.ipc import SanitizedOperationResult
from polytrading.predictions.polymarket_execution.protocol import (
    POLYMARKET_PILOT_PROTOCOL_VERSION,
    load_protocol_snapshot,
)
from polytrading.predictions.polymarket_execution.routes import (
    ROUTE_SET_HASH,
    ROUTE_SET_VERSION,
    BalanceAllowancePayload,
    RestCode,
    RouteKey,
)
from polytrading.predictions.storage.store import PredictionMarketStore


def compose_pilot_environment(
    store: PredictionMarketStore,
    *,
    account_fingerprint: Sha256,
    wallet_fingerprint: Sha256,
    credentials_present: bool,
    now: Callable[[], datetime],
    venue_port: VenueSubmissionPort | None = None,
    executor_factory: ExecutorFactory | None = None,
    manifest_provider: Callable[[], VenueManifest | None] | None = None,
    reconciliation_provider: Callable[[], PilotReconciliationState] | None = None,
) -> PilotEnvironment:
    """Load evidence, using authoritative signer reads when a venue port is available."""
    observed_at = now()
    manifest = store.verified_latest_venue_manifest_as_of(PredictionVenue.POLYMARKET, observed_at)
    attestations = store.verified_pilot_eligibility_attestations(account_fingerprint)
    fixture_hash = _protocol_fixture_hash()
    venue_binding = (
        None
        if manifest is None
        else VenueBinding(
            venue=PredictionVenue.POLYMARKET,
            manifest_record_hash=canonical_execution_hash(manifest),
            manifest_source_hashes=manifest.source_hashes,
            eligibility_evidence_hashes=tuple(
                sorted({fixture_hash, *(item.document_hash for item in attestations)})
            ),
            strategy_policy_hash=canonical_execution_hash({"strategy": "pilot-frozen-v1"}),
            proof_policy_hash=canonical_execution_hash({"proof": "persisted-qualification-v1"}),
            economics_policy_hash=canonical_execution_hash({"economics": "persisted-shadow-v1"}),
            protocol_fixture_hash=fixture_hash,
            route_set_version=ROUTE_SET_VERSION,
            route_set_hash=ROUTE_SET_HASH,
        )
    )
    reports = (
        tuple(
            evaluate_pilot_qualification(store, family, observed_at) for family in PilotProofFamily
        )
        if store.verified_scan_reports_as_of(observed_at)
        else ()
    )
    latest_attestation = max(attestations, key=lambda item: item.reviewed_at, default=None)
    if venue_port is None:
        reconciliation = PilotReconciliationState(
            account_fingerprint=account_fingerprint,
            active_submissions=0,
            unknown_outcomes=0,
            reconciliation_complete=False,
            unexplained_difference_usd=Decimal("0"),
            reconciliation_hash=canonical_execution_hash(
                {"account": account_fingerprint, "reconciliation": "transport-unavailable"}
            ),
            observed_at=observed_at,
        )
        account_state = _unavailable_account_state
    else:
        reconciliation = reconcile_startup(
            venue_port,
            account_fingerprint=account_fingerprint,
            now=lambda: observed_at,
        )
        account_state = venue_port.account_state

    return PilotEnvironment(
        account_fingerprint=account_fingerprint,
        wallet_fingerprint=wallet_fingerprint,
        venue_binding=venue_binding,
        manifest=manifest,
        manifest_state="MISSING" if manifest is None else manifest.implementation_state.value,
        protocol_state="CURRENT",
        qualifications=reports,
        eligibility_expires_at=(
            None if latest_attestation is None else latest_attestation.expires_at
        ),
        credentials_present=credentials_present,
        reconciliation=reconciliation,
        account_state=account_state,
        executor_factory=executor_factory,
        manifest_provider=manifest_provider,
        reconciliation_provider=reconciliation_provider,
    )


def signer_account_reader(
    *,
    account_fingerprint: Sha256,
    wallet_fingerprint: Sha256,
) -> Callable[[Mapping[str, object]], PilotAccountState]:
    """Decode only the fixed public collateral read into the pilot account projection."""

    def read(payload: Mapping[str, object]) -> PilotAccountState:
        result = SanitizedOperationResult.model_validate(payload, strict=False)
        public = result.public_payload
        if (
            result.operation is not ExecutionOperation.READ_ACCOUNT
            or result.result_code is not RestCode.READ_OK
            or result.route is not RouteKey.READ_BALANCE_ALLOWANCE
            or result.observed_at is None
            or type(public) is not BalanceAllowancePayload
        ):
            raise ValueError("ACCOUNT_READ_INVALID")
        assert isinstance(public, BalanceAllowancePayload)
        allowances = tuple(Decimal(item.amount) for item in public.allowances)
        return PilotAccountState(
            account_fingerprint=account_fingerprint,
            wallet_fingerprint=wallet_fingerprint,
            collateral_usd=Decimal(public.balance),
            allowance_usd=min(allowances, default=Decimal("0")),
            kill_engaged=False,
            observed_at=result.observed_at,
        )

    return read


def signer_position_reader(payload: Mapping[str, object]) -> Decimal:
    """Decode one fixed public conditional-token balance without retaining the response."""
    result = SanitizedOperationResult.model_validate(payload, strict=False)
    public = result.public_payload
    if (
        result.operation is not ExecutionOperation.READ_ACCOUNT
        or result.result_code is not RestCode.READ_OK
        or result.route is not RouteKey.READ_BALANCE_ALLOWANCE
        or type(public) is not BalanceAllowancePayload
    ):
        raise ValueError("POSITION_READ_INVALID")
    assert isinstance(public, BalanceAllowancePayload)
    return Decimal(public.balance)


def _unavailable_account_state() -> PilotAccountState:
    raise PilotRequestError(HTTPStatus.CONFLICT, "EXECUTION_UNAVAILABLE")


def _protocol_fixture_hash() -> Sha256:
    snapshot = load_protocol_snapshot(version=POLYMARKET_PILOT_PROTOCOL_VERSION)
    return canonical_execution_hash(
        {
            "version": snapshot.version,
            "fixtures": [item.model_dump(mode="json") for item in snapshot.fixture_hashes],
        }
    )


__all__ = [
    "compose_pilot_environment",
    "signer_account_reader",
    "signer_position_reader",
]
