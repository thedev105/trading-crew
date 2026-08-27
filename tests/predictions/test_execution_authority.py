from __future__ import annotations

import json
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


def execution_capability_fields(**overrides: object) -> dict[str, object]:
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
        "route_set_hash": HASHES[12],
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
    return fields


def execution_capability(**overrides: object) -> ExecutionCapability:
    return ExecutionCapability.model_validate(execution_capability_fields(**overrides))


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
        "route_set_hash": HASHES[12],
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


def test_same_route_version_with_a_different_hash_is_rejected() -> None:
    capability = verified_capability(route_set_hash=HASHES[11])
    context = authority_context(verified_capability=capability)
    assert (
        verify_mutation_authority(context, ExecutionOperation.SUBMIT_ORDER).reason
        == "CAPABILITY_ROUTE_SET_MISMATCH"
    )


@pytest.mark.parametrize(
    "malformed_signature",
    ["private-signature-canary", bytearray(b"private-signature-canary")],
)
def test_malformed_signature_validation_never_echoes_raw_input(
    malformed_signature: object,
) -> None:
    fields = execution_capability_fields(detached_signature=malformed_signature)
    constructors = (
        lambda: ExecutionCapability.model_validate(fields),
        lambda: ExecutionCapability(**fields),
    )
    for construct in constructors:
        with pytest.raises(ValidationError) as raised:
            construct()
        assert "private-signature-canary" not in str(raised.value)
        assert "private-signature-canary" not in repr(raised.value)
        assert "private-signature-canary" not in str(raised.value.errors())
        assert "private-signature-canary" not in raised.value.json()


def test_malformed_signature_in_json_validation_never_echoes_raw_input() -> None:
    fields = execution_capability().model_dump(mode="json")
    fields["detached_signature"] = "private-signature-canary"
    with pytest.raises(ValidationError) as raised:
        ExecutionCapability.model_validate_json(json.dumps(fields))
    assert "private-signature-canary" not in str(raised.value)
    assert "private-signature-canary" not in repr(raised.value)
    assert "private-signature-canary" not in str(raised.value.errors())
    assert "private-signature-canary" not in raised.value.json()


def test_signature_canary_never_reaches_dumps_decisions_or_gate_rejections() -> None:
    canary = "private-signature-canary"
    capability = execution_capability(detached_signature=canary.encode())
    unavailable = UnavailableProductionCapabilityVerifier().verify(
        capability_bundle=canary.encode(), now=NOW
    )
    rejected = verify_mutation_authority(
        authority_context(verified_capability=verified_capability(signature_valid=False)),
        ExecutionOperation.SUBMIT_ORDER,
    )
    surfaces = (
        repr(capability),
        str(capability.model_dump()),
        capability.model_dump_json(),
        repr(unavailable),
        str(unavailable),
        repr(rejected),
        str(rejected.model_dump()),
        rejected.model_dump_json(),
    )
    assert all(canary not in surface for surface in surfaces)


def test_capability_exposes_deterministic_unsigned_bytes_without_signature_material() -> None:
    first = execution_capability(detached_signature=b"first-signature-canary")
    second = execution_capability(detached_signature=b"second-signature-canary")
    expected = (
        b'{"account_fingerprint":"1111111111111111111111111111111111111111111111111111111111111111",'
        b'"activation_nonce":"activation-1","allowed_operations":["CANCEL_ORDER","HEARTBEAT",'
        b'"SIGN_ORDER","SUBMIT_ORDER"],"capability_id":"11111111-1111-4111-8111-111111111111",'
        b'"capability_version":1,"economics_policy_hash":"77777777777777777777777777777777777'
        b'77777777777777777777777777777","eligibility_evidence_hashes":["4444444444444444444'
        b'444444444444444444444444444444444444444444444"],"expires_at":"2026-08-25T16:01:00Z",'
        b'"issuer_key_id":"fixture-key-1","manifest_record_hash":"363581cc4bba87b6df4473faa3bb95'
        b'f7a262de4cf34089a6f71f52a9a91de391","manifest_source_hashes":["3333333333333333333'
        b'333333333333333333333333333333333333333333333"],"maximum_capital":"100","maximum_loss":'
        b'"5","maximum_per_intent_notional":"10","maximum_position":"50","not_before":"2026-08-'
        b'25T15:59:00Z","proof_policy_hash":"66666666666666666666666666666666666666666666666666'
        b'66666666666666","protocol_fixture_hash":"8888888888888888888888888888888888888888888888'
        b'888888888888888888","route_set_hash":"dddddddddddddddddddddddddddddddddddddddddddddddd'
        b'dddddddddddddddd","route_set_version":"polymarket-mutations-v1","strategy_policy_hash":"5'
        b'555555555555555555555555555555555555555555555555555555555555555","venue":"polymarket"}'
    )
    assert first.canonical_unsigned_bundle == expected
    assert second.canonical_unsigned_bundle == expected
    assert b"first-signature-canary" not in first.canonical_unsigned_bundle
    assert "first-signature-canary" not in repr(first)
    assert "second-signature-canary" not in repr(second)
    assert "detached_signature" not in first.model_dump()
    assert "detached_signature" not in second.model_dump(mode="json")


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
            {"verified_capability": verified_capability(route_set_hash=HASHES[11])},
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
            {"revoked_capability_ids": frozenset({UUID("11111111-1111-4111-8111-111111111111")})},
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
        (
            {"implementation_state": AdapterImplementationState.WATCHLIST},
            "COLLECTION_NOT_PERMITTED",
        ),
        ({"automated_use_status": "restricted"}, "AUTOMATED_USE_RESTRICTED"),
        ({"jurisdiction_review_status": "BLOCKED"}, "JURISDICTION_BLOCKED"),
        ({"jurisdiction_review_status": "UNREVIEWED"}, "JURISDICTION_UNREVIEWED"),
        (
            {"implementation_state": AdapterImplementationState.LIVE_DISABLED},
            "LIVE_NOT_ELIGIBLE",
        ),
    ],
)
def test_manifest_rejections_precede_capability_rejections(
    manifest_override: dict[str, object] | None, reason: str
) -> None:
    if manifest_override is None:
        manifest = None
    else:
        fields: dict[str, object] = {
            "implementation_state": AdapterImplementationState.LIVE_ELIGIBLE,
            "jurisdiction_review_status": "ELIGIBILITY_REVIEWED",
            "source_hashes": (HASHES[2],),
        }
        fields.update(manifest_override)
        manifest = venue_manifest(**fields)
    context = authority_context(manifest=manifest, verified_capability=None)
    assert verify_mutation_authority(context, ExecutionOperation.SUBMIT_ORDER).reason == reason


def test_boundaries_are_half_open_for_time_and_inclusive_for_skew_and_limits() -> None:
    capability = verified_capability(not_before=NOW, expires_at=NOW + timedelta(microseconds=1))
    context = authority_context(verified_capability=capability)
    assert verify_mutation_authority(context, ExecutionOperation.SUBMIT_ORDER).allowed is True


def test_complete_capability_failure_order_is_stable() -> None:
    capability = verified_capability().model_copy(
        update={
            "signature_valid": False,
            "canonical_bytes_valid": False,
            "not_before": NOW + timedelta(seconds=1),
            "expires_at": NOW,
            "venue": PredictionVenue.KALSHI,
            "account_fingerprint": HASHES[11],
            "manifest_record_hash": HASHES[11],
            "manifest_source_hashes": (HASHES[11],),
            "strategy_policy_hash": HASHES[11],
            "proof_policy_hash": HASHES[11],
            "economics_policy_hash": HASHES[11],
            "protocol_fixture_hash": HASHES[11],
            "route_set_version": "wrong-routes",
            "route_set_hash": HASHES[11],
            "allowed_operations": (ExecutionOperation.CANCEL_ORDER,),
            "maximum_capital": Decimal("0"),
            "maximum_per_intent_notional": Decimal("0"),
            "maximum_position": Decimal("0"),
            "maximum_loss": Decimal("0"),
        }
    )
    context = authority_context(
        observed_clock_skew=timedelta(seconds=3),
        activation_nonce="different",
        used_activation_nonces=frozenset({"activation-1"}),
        revoked_capability_ids=frozenset({capability.capability_id}),
        geoblock_allowed=False,
        geoblock_evidence_hash=None,
        geoblock_expires_at=NOW,
        account_scope_account_fingerprint=HASHES[11],
        account_scope_evidence_hash=None,
        account_scope_expires_at=NOW,
        kill_engaged=True,
    ).model_copy(update={"verified_capability": capability})
    missing = context.model_copy(update={"verified_capability": None})
    assert verify_mutation_authority(missing, ExecutionOperation.SUBMIT_ORDER).reason == (
        "CAPABILITY_MISSING"
    )

    repairs: tuple[tuple[str, dict[str, object], dict[str, object]], ...] = (
        ("CAPABILITY_SIGNATURE_INVALID", {"signature_valid": True}, {}),
        ("CAPABILITY_CANONICAL_BYTES_INVALID", {"canonical_bytes_valid": True}, {}),
        ("CAPABILITY_NOT_YET_VALID", {"not_before": NOW}, {}),
        ("CAPABILITY_EXPIRED", {"expires_at": NOW + timedelta(seconds=1)}, {}),
        ("CAPABILITY_CLOCK_SKEW", {}, {"observed_clock_skew": timedelta(seconds=2)}),
        ("CAPABILITY_VENUE_MISMATCH", {"venue": PredictionVenue.POLYMARKET}, {}),
        ("CAPABILITY_ACCOUNT_MISMATCH", {"account_fingerprint": HASHES[0]}, {}),
        ("CAPABILITY_MANIFEST_MISMATCH", {"manifest_record_hash": MANIFEST_HASH}, {}),
        ("CAPABILITY_SOURCE_HASH_MISMATCH", {"manifest_source_hashes": (HASHES[2],)}, {}),
        ("CAPABILITY_STRATEGY_POLICY_MISMATCH", {"strategy_policy_hash": HASHES[4]}, {}),
        ("CAPABILITY_PROOF_POLICY_MISMATCH", {"proof_policy_hash": HASHES[5]}, {}),
        ("CAPABILITY_ECONOMICS_POLICY_MISMATCH", {"economics_policy_hash": HASHES[6]}, {}),
        ("CAPABILITY_PROTOCOL_MISMATCH", {"protocol_fixture_hash": HASHES[7]}, {}),
        ("CAPABILITY_ROUTE_SET_MISMATCH", {"route_set_version": "polymarket-mutations-v1"}, {}),
        ("CAPABILITY_ROUTE_SET_MISMATCH", {"route_set_hash": HASHES[12]}, {}),
        (
            "CAPABILITY_OPERATION_NOT_ALLOWED",
            {"allowed_operations": (ExecutionOperation.SUBMIT_ORDER,)},
            {},
        ),
        ("CAPABILITY_CAPITAL_LIMIT_EXCEEDED", {"maximum_capital": Decimal("100")}, {}),
        (
            "CAPABILITY_NOTIONAL_LIMIT_EXCEEDED",
            {"maximum_per_intent_notional": Decimal("10")},
            {},
        ),
        ("CAPABILITY_POSITION_LIMIT_EXCEEDED", {"maximum_position": Decimal("50")}, {}),
        ("CAPABILITY_LOSS_LIMIT_EXCEEDED", {"maximum_loss": Decimal("5")}, {}),
        ("CAPABILITY_NONCE_MISMATCH", {}, {"activation_nonce": "activation-1"}),
        ("CAPABILITY_NONCE_REPLAYED", {}, {"used_activation_nonces": frozenset()}),
        ("CAPABILITY_REVOKED", {}, {"revoked_capability_ids": frozenset()}),
        ("GEOBLOCK_EVIDENCE_MISSING", {}, {"geoblock_evidence_hash": HASHES[9]}),
        (
            "GEOBLOCK_EVIDENCE_STALE",
            {},
            {"geoblock_expires_at": NOW + timedelta(seconds=1)},
        ),
        ("GEOBLOCK_BLOCKED", {}, {"geoblock_allowed": True}),
        (
            "ACCOUNT_SCOPE_EVIDENCE_MISSING",
            {},
            {"account_scope_evidence_hash": HASHES[10]},
        ),
        (
            "ACCOUNT_SCOPE_EVIDENCE_STALE",
            {},
            {"account_scope_expires_at": NOW + timedelta(seconds=1)},
        ),
        (
            "ACCOUNT_SCOPE_MISMATCH",
            {},
            {"account_scope_account_fingerprint": HASHES[0]},
        ),
        ("EXECUTION_KILL_ENGAGED", {}, {"kill_engaged": False}),
    )
    for reason, capability_repair, context_repair in repairs:
        assert verify_mutation_authority(context, ExecutionOperation.SUBMIT_ORDER).reason == reason
        capability = capability.model_copy(update=capability_repair)
        context = context.model_copy(update={"verified_capability": capability, **context_repair})
    assert verify_mutation_authority(context, ExecutionOperation.SUBMIT_ORDER).allowed is True


def test_malformed_evidence_is_sanitized_to_the_stable_missing_code() -> None:
    context = authority_context().model_copy(update={"geoblock_evidence_hash": "raw-secret"})
    decision = verify_mutation_authority(context, ExecutionOperation.SUBMIT_ORDER)
    assert decision.reason == "GEOBLOCK_EVIDENCE_MISSING"
    assert "raw-secret" not in repr(decision)


@pytest.mark.parametrize(
    "operation",
    [
        ExecutionOperation.SIGN_ORDER,
        ExecutionOperation.SUBMIT_ORDER,
        ExecutionOperation.CANCEL_ORDER,
        ExecutionOperation.HEARTBEAT,
    ],
)
def test_every_mutation_operation_requires_the_full_gate_with_zero_notional(
    operation: ExecutionOperation,
) -> None:
    context = authority_context(requested_notional=Decimal("0"))
    assert verify_mutation_authority(context, operation).allowed is True
    assert (
        verify_mutation_authority(
            context.model_copy(update={"kill_engaged": True}), operation
        ).reason
        == "EXECUTION_KILL_ENGAGED"
    )


def test_each_boundary_evaluates_the_snapshot_without_a_cached_pass() -> None:
    coordinator_context = authority_context()
    signer_context = authority_context()
    assert coordinator_context == signer_context
    assert coordinator_context is not signer_context

    coordinator = verify_mutation_authority(coordinator_context, ExecutionOperation.SUBMIT_ORDER)
    signer = verify_mutation_authority(signer_context, ExecutionOperation.SUBMIT_ORDER)
    assert coordinator.allowed is True
    assert signer.allowed is True

    fresh_signer_context = authority_context(kill_engaged=True)
    fresh_signer = verify_mutation_authority(fresh_signer_context, ExecutionOperation.SUBMIT_ORDER)
    assert fresh_signer.reason == "EXECUTION_KILL_ENGAGED"


def test_manifest_hash_is_bound_to_the_current_manifest() -> None:
    context = authority_context()
    assert canonical_execution_hash(context.manifest) == context.manifest_record_hash
