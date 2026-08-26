from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import ValidationError

from polytrading.predictions.domain import PredictionVenue
from polytrading.predictions.execution.authority import (
    AuthorityContext,
    AuthorityDecision,
    ExecutionCapability,
    UnavailableProductionCapabilityVerifier,
    VerifiedExecutionCapability,
    verify_mutation_authority,
)
from polytrading.predictions.execution.models import ExecutionOperation, canonical_execution_hash
from polytrading.predictions.manifest import AdapterImplementationState
from tests.predictions.manifest_helpers import venue_manifest

NOW = datetime(2026, 8, 25, 16, tzinfo=UTC)
HASHES = tuple(f"{digit}" * 64 for digit in "123456789abcdef")
ELIGIBLE_MANIFEST = venue_manifest(
    implementation_state=AdapterImplementationState.LIVE_ELIGIBLE,
    jurisdiction_review_status="ELIGIBILITY_REVIEWED",
    source_hashes=(HASHES[2],),
)
MANIFEST_HASH = canonical_execution_hash(ELIGIBLE_MANIFEST)


def execution_capability(**overrides: object) -> ExecutionCapability:
    fields: dict[str, object] = {
        "capability_version": 1,
        "capability_id": UUID("11111111-1111-4111-8111-111111111111"),
        "venue": PredictionVenue.POLYMARKET,
        "account_fingerprint": HASHES[0],
        "manifest_record_hash": MANIFEST_HASH,
        "manifest_source_hashes": (HASHES[2],),
        "eligibility_evidence_hashes": (HASHES[3],),
        "strategy_policy_hash": HASHES[4],
        "proof_policy_hash": HASHES[5],
        "economics_policy_hash": HASHES[6],
        "protocol_fixture_hash": HASHES[7],
        "allowed_operations": (
            ExecutionOperation.CANCEL_ORDER,
            ExecutionOperation.HEARTBEAT,
            ExecutionOperation.SIGN_ORDER,
            ExecutionOperation.SUBMIT_ORDER,
        ),
        "route_set_version": "polymarket-mutations-v1",
        "maximum_capital": Decimal("100"),
        "maximum_per_intent_notional": Decimal("10"),
        "maximum_position": Decimal("50"),
        "maximum_loss": Decimal("5"),
        "not_before": NOW - timedelta(minutes=1),
        "expires_at": NOW + timedelta(minutes=1),
        "activation_nonce": "activation-1",
        "issuer_key_id": "fixture-key-1",
        "detached_signature": b"fixture-signature-canary",
    }
    fields.update(overrides)
    return ExecutionCapability.model_validate(fields)


def verified_capability(**overrides: object) -> VerifiedExecutionCapability:
    capability = execution_capability()
    fields = capability.model_dump(exclude={"detached_signature"})
    fields.update(
        {
            "capability_digest": HASHES[8],
            "verified_at": NOW,
            "signature_valid": True,
            "canonical_bytes_valid": True,
        }
    )
    fields.update(overrides)
    return VerifiedExecutionCapability.model_validate(fields)


def authority_context(**overrides: object) -> AuthorityContext:
    manifest = ELIGIBLE_MANIFEST
    fields: dict[str, object] = {
        "manifest": manifest,
        "verified_capability": verified_capability(),
        "now": NOW,
        "venue": PredictionVenue.POLYMARKET,
        "account_fingerprint": HASHES[0],
        "manifest_record_hash": MANIFEST_HASH,
        "manifest_source_hashes": (HASHES[2],),
        "strategy_policy_hash": HASHES[4],
        "proof_policy_hash": HASHES[5],
        "economics_policy_hash": HASHES[6],
        "protocol_fixture_hash": HASHES[7],
        "route_set_version": "polymarket-mutations-v1",
        "requested_notional": Decimal("10"),
        "capital_after": Decimal("100"),
        "position_after": Decimal("50"),
        "loss_after": Decimal("5"),
        "activation_nonce": "activation-1",
        "used_activation_nonces": frozenset(),
        "revoked_capability_ids": frozenset(),
        "observed_clock_skew": timedelta(seconds=-2),
        "permitted_clock_skew": timedelta(seconds=2),
        "geoblock_allowed": True,
        "geoblock_evidence_hash": HASHES[9],
        "geoblock_expires_at": NOW + timedelta(seconds=1),
        "account_scope_account_fingerprint": HASHES[0],
        "account_scope_evidence_hash": HASHES[10],
        "account_scope_expires_at": NOW + timedelta(seconds=1),
        "kill_engaged": False,
        "evidence_hashes": tuple(sorted((HASHES[2], HASHES[8], HASHES[9], HASHES[10]))),
    }
    fields.update(overrides)
    return AuthorityContext.model_validate(fields)


def test_production_verifier_always_rejects_without_a_configured_key() -> None:
    decision = UnavailableProductionCapabilityVerifier().verify(
        capability_bundle=b"fixture", now=NOW
    )
    assert decision.allowed is False
    assert decision.reason == "CAPABILITY_VERIFIER_NOT_CONFIGURED"
    assert b"fixture" not in repr(decision).encode()


def test_capability_exposes_deterministic_unsigned_bytes_without_signature_material() -> None:
    first = execution_capability()
    second = execution_capability()
    assert first.canonical_unsigned_bundle == second.canonical_unsigned_bundle
    assert b"fixture-signature-canary" not in first.canonical_unsigned_bundle
    assert "fixture-signature-canary" not in repr(first)
    assert "detached_signature" not in first.model_dump()


def test_capability_requires_signature_and_a_nonempty_time_window() -> None:
    with pytest.raises(ValidationError):
        execution_capability(detached_signature=b"")
    with pytest.raises(ValidationError):
        execution_capability(expires_at=NOW - timedelta(minutes=1))


@pytest.mark.parametrize("field", ["manifest_source_hashes", "eligibility_evidence_hashes"])
def test_capability_requires_nonempty_evidence_hashes(field: str) -> None:
    with pytest.raises(ValidationError):
        execution_capability(**{field: ()})


def test_live_disabled_manifest_cannot_be_overridden_by_valid_fixture_capability() -> None:
    manifest = venue_manifest(
        implementation_state=AdapterImplementationState.LIVE_DISABLED,
        jurisdiction_review_status="ELIGIBILITY_REVIEWED",
        source_hashes=(HASHES[2],),
    )
    context = authority_context(manifest=manifest)
    decision = verify_mutation_authority(context, ExecutionOperation.SUBMIT_ORDER)
    assert decision == AuthorityDecision(
        allowed=False,
        reason="LIVE_NOT_ELIGIBLE",
        evidence_hashes=context.evidence_hashes,
    )


def test_non_polymarket_manifest_cannot_cross_the_authority_boundary() -> None:
    manifest = venue_manifest(
        venue=PredictionVenue.KALSHI,
        implementation_state=AdapterImplementationState.LIVE_ELIGIBLE,
        jurisdiction_review_status="ELIGIBILITY_REVIEWED",
        source_hashes=(HASHES[2],),
    )
    context = authority_context(manifest=manifest)
    assert (
        verify_mutation_authority(context, ExecutionOperation.SUBMIT_ORDER).reason
        == "CAPABILITY_VENUE_MISMATCH"
    )


@pytest.mark.parametrize(
    ("updates", "reason"),
    [
        ({"verified_capability": None}, "CAPABILITY_MISSING"),
        (
            {"verified_capability": verified_capability(signature_valid=False)},
            "CAPABILITY_SIGNATURE_INVALID",
        ),
        (
            {"verified_capability": verified_capability(canonical_bytes_valid=False)},
            "CAPABILITY_CANONICAL_BYTES_INVALID",
        ),
        (
            {
                "verified_capability": verified_capability(
                    not_before=NOW + timedelta(microseconds=1)
                )
            },
            "CAPABILITY_NOT_YET_VALID",
        ),
        ({"verified_capability": verified_capability(expires_at=NOW)}, "CAPABILITY_EXPIRED"),
        ({"observed_clock_skew": timedelta(seconds=3)}, "CAPABILITY_CLOCK_SKEW"),
        (
            {"verified_capability": verified_capability(venue=PredictionVenue.KALSHI)},
            "CAPABILITY_VENUE_MISMATCH",
        ),
        (
            {"verified_capability": verified_capability(account_fingerprint=HASHES[11])},
            "CAPABILITY_ACCOUNT_MISMATCH",
        ),
        (
            {"verified_capability": verified_capability(manifest_record_hash=HASHES[11])},
            "CAPABILITY_MANIFEST_MISMATCH",
        ),
        (
            {"verified_capability": verified_capability(manifest_source_hashes=(HASHES[11],))},
            "CAPABILITY_SOURCE_HASH_MISMATCH",
        ),
        (
            {"verified_capability": verified_capability(strategy_policy_hash=HASHES[11])},
            "CAPABILITY_STRATEGY_POLICY_MISMATCH",
        ),
        (
            {"verified_capability": verified_capability(proof_policy_hash=HASHES[11])},
            "CAPABILITY_PROOF_POLICY_MISMATCH",
        ),
        (
            {"verified_capability": verified_capability(economics_policy_hash=HASHES[11])},
            "CAPABILITY_ECONOMICS_POLICY_MISMATCH",
        ),
        (
            {"verified_capability": verified_capability(protocol_fixture_hash=HASHES[11])},
            "CAPABILITY_PROTOCOL_MISMATCH",
        ),
        (
            {"verified_capability": verified_capability(route_set_version="wrong-routes")},
            "CAPABILITY_ROUTE_SET_MISMATCH",
        ),
        (
            {
                "verified_capability": verified_capability(
                    allowed_operations=(ExecutionOperation.CANCEL_ORDER,)
                )
            },
            "CAPABILITY_OPERATION_NOT_ALLOWED",
        ),
        ({"capital_after": Decimal("100.01")}, "CAPABILITY_CAPITAL_LIMIT_EXCEEDED"),
        ({"requested_notional": Decimal("10.01")}, "CAPABILITY_NOTIONAL_LIMIT_EXCEEDED"),
        ({"position_after": Decimal("50.01")}, "CAPABILITY_POSITION_LIMIT_EXCEEDED"),
        ({"loss_after": Decimal("5.01")}, "CAPABILITY_LOSS_LIMIT_EXCEEDED"),
        ({"activation_nonce": "different"}, "CAPABILITY_NONCE_MISMATCH"),
        (
            {"used_activation_nonces": frozenset({"activation-1"})},
            "CAPABILITY_NONCE_REPLAYED",
        ),
        (
            {
                "revoked_capability_ids": frozenset(
                    {UUID("11111111-1111-4111-8111-111111111111")}
                )
            },
            "CAPABILITY_REVOKED",
        ),
        ({"geoblock_evidence_hash": None}, "GEOBLOCK_EVIDENCE_MISSING"),
        ({"geoblock_expires_at": NOW}, "GEOBLOCK_EVIDENCE_STALE"),
        ({"geoblock_allowed": False}, "GEOBLOCK_BLOCKED"),
        ({"account_scope_evidence_hash": None}, "ACCOUNT_SCOPE_EVIDENCE_MISSING"),
        ({"account_scope_expires_at": NOW}, "ACCOUNT_SCOPE_EVIDENCE_STALE"),
        (
            {"account_scope_account_fingerprint": HASHES[11]},
            "ACCOUNT_SCOPE_MISMATCH",
        ),
        ({"kill_engaged": True}, "EXECUTION_KILL_ENGAGED"),
    ],
)
def test_mutation_authority_rejects_each_fail_closed_condition(
    updates: dict[str, object], reason: str
) -> None:
    context = authority_context(**updates)
    decision = verify_mutation_authority(context, ExecutionOperation.SUBMIT_ORDER)
    assert decision == AuthorityDecision(False, reason, context.evidence_hashes)


@pytest.mark.parametrize(
    ("manifest_override", "reason"),
    [
        (None, "MANIFEST_NOT_FOUND"),
        ({"jurisdiction_review_status": "BLOCKED"}, "JURISDICTION_BLOCKED"),
        ({"jurisdiction_review_status": "UNREVIEWED"}, "JURISDICTION_UNREVIEWED"),
    ],
)
def test_manifest_rejections_precede_capability_rejections(
    manifest_override: dict[str, object] | None, reason: str
) -> None:
    if manifest_override is None:
        manifest = None
    else:
        manifest = venue_manifest(
            implementation_state=AdapterImplementationState.LIVE_ELIGIBLE,
            source_hashes=(HASHES[2],),
            **manifest_override,
        )
    context = authority_context(manifest=manifest, verified_capability=None)
    assert verify_mutation_authority(context, ExecutionOperation.SUBMIT_ORDER).reason == reason


def test_boundaries_are_half_open_for_time_and_inclusive_for_skew_and_limits() -> None:
    capability = verified_capability(not_before=NOW, expires_at=NOW + timedelta(microseconds=1))
    context = authority_context(verified_capability=capability)
    assert verify_mutation_authority(context, ExecutionOperation.SUBMIT_ORDER).allowed is True


@pytest.mark.parametrize(
    ("updates", "reason"),
    [
        (
            {
                "verified_capability": verified_capability(
                    signature_valid=False, canonical_bytes_valid=False
                ),
                "kill_engaged": True,
            },
            "CAPABILITY_SIGNATURE_INVALID",
        ),
        (
            {
                "verified_capability": verified_capability(
                    manifest_source_hashes=(HASHES[11],), strategy_policy_hash=HASHES[11]
                )
            },
            "CAPABILITY_SOURCE_HASH_MISMATCH",
        ),
        (
            {
                "used_activation_nonces": frozenset({"activation-1"}),
                "revoked_capability_ids": frozenset(
                    {UUID("11111111-1111-4111-8111-111111111111")}
                ),
            },
            "CAPABILITY_NONCE_REPLAYED",
        ),
        (
            {
                "geoblock_evidence_hash": None,
                "geoblock_expires_at": NOW,
                "geoblock_allowed": False,
                "kill_engaged": True,
            },
            "GEOBLOCK_EVIDENCE_MISSING",
        ),
        (
            {"account_scope_evidence_hash": None, "kill_engaged": True},
            "ACCOUNT_SCOPE_EVIDENCE_MISSING",
        ),
    ],
)
def test_first_failure_order_is_stable(updates: dict[str, object], reason: str) -> None:
    context = authority_context(**updates)
    assert verify_mutation_authority(context, ExecutionOperation.SUBMIT_ORDER).reason == reason


def test_malformed_evidence_is_sanitized_to_the_stable_missing_code() -> None:
    context = authority_context().model_copy(update={"geoblock_evidence_hash": "raw-secret"})
    decision = verify_mutation_authority(context, ExecutionOperation.SUBMIT_ORDER)
    assert decision.reason == "GEOBLOCK_EVIDENCE_MISSING"
    assert "raw-secret" not in repr(decision)


@pytest.mark.parametrize(
    "operation", [ExecutionOperation.CANCEL_ORDER, ExecutionOperation.HEARTBEAT]
)
def test_cancel_and_heartbeat_require_the_full_gate_with_zero_notional(
    operation: ExecutionOperation,
) -> None:
    context = authority_context(requested_notional=Decimal("0"))
    assert verify_mutation_authority(context, operation).allowed is True
    assert verify_mutation_authority(
        context.model_copy(update={"kill_engaged": True}), operation
    ).reason == "EXECUTION_KILL_ENGAGED"


def test_each_boundary_evaluates_the_snapshot_without_a_cached_pass() -> None:
    context = authority_context()
    coordinator = verify_mutation_authority(context, ExecutionOperation.SUBMIT_ORDER)
    signer = verify_mutation_authority(
        context.model_copy(update={"kill_engaged": True}), ExecutionOperation.SUBMIT_ORDER
    )
    assert coordinator.allowed is True
    assert signer.reason == "EXECUTION_KILL_ENGAGED"


def test_manifest_hash_is_bound_to_the_current_manifest() -> None:
    context = authority_context()
    assert canonical_execution_hash(context.manifest) == context.manifest_record_hash
