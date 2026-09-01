"""Compose the deliberately transport-free pilot environment from verified evidence."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from polytrading.predictions.domain import PredictionVenue, Sha256
from polytrading.predictions.execution.models import canonical_execution_hash
from polytrading.predictions.pilot.capabilities import VenueBinding
from polytrading.predictions.pilot.execution_port import VenueSubmissionPort
from polytrading.predictions.pilot.models import PilotProofFamily
from polytrading.predictions.pilot.qualification import evaluate_pilot_qualification
from polytrading.predictions.pilot.reconciliation import reconcile_startup
from polytrading.predictions.pilot.services import PilotEnvironment
from polytrading.predictions.polymarket_execution.protocol import (
    POLYMARKET_PILOT_PROTOCOL_VERSION,
    load_protocol_snapshot,
)
from polytrading.predictions.polymarket_execution.routes import ROUTE_SET_HASH, ROUTE_SET_VERSION
from polytrading.predictions.storage.store import PredictionMarketStore


def compose_pilot_environment(
    store: PredictionMarketStore,
    *,
    account_fingerprint: Sha256,
    wallet_fingerprint: Sha256,
    credentials_present: bool,
    now: Callable[[], datetime],
    venue_port: VenueSubmissionPort,
) -> PilotEnvironment:
    """Load verified persisted evidence and the signer's authoritative account snapshot."""
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
    reconciliation = reconcile_startup(
        venue_port,
        account_fingerprint=account_fingerprint,
        now=lambda: observed_at,
    )

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
        account_state=venue_port.account_state,
    )


def _protocol_fixture_hash() -> Sha256:
    snapshot = load_protocol_snapshot(version=POLYMARKET_PILOT_PROTOCOL_VERSION)
    return canonical_execution_hash(
        {
            "version": snapshot.version,
            "fixtures": [item.model_dump(mode="json") for item in snapshot.fixture_hashes],
        }
    )


__all__ = ["compose_pilot_environment"]
