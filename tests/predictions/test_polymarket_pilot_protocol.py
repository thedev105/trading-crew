from __future__ import annotations

import json
import shutil
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from polytrading.predictions.polymarket_execution.auth import sign_clob_auth
from polytrading.predictions.polymarket_execution.conformance import run_conformance
from polytrading.predictions.polymarket_execution.protocol import (
    POLYMARKET_PILOT_PROTOCOL_VERSION,
    POLYMARKET_PROTOCOL_VERSION,
    AccountSignatureModel,
    PolymarketProtocolSnapshot,
    ProtocolSnapshotError,
    bind_account_signature,
    bundled_fixture_path,
    load_protocol_snapshot,
    require_account_signature_model,
    verify_protocol_sources,
)
from polytrading.predictions.polymarket_execution.routes import (
    CREDENTIAL_ROUTE_SET_HASH,
    CREDENTIAL_ROUTE_SPECS,
    ROUTE_SPECS,
    CredentialProvisioningRequest,
    RouteKey,
    execution_route_keys,
)

VECTOR_ADDRESS = "0x7E5F4552091A69125d5DfCb7b8C2659029395Bdf"
FUNDER_ADDRESS = "0x" + "11" * 20
GRANT_DIGEST = "c" * 64


def pilot_snapshot() -> PolymarketProtocolSnapshot:
    return load_protocol_snapshot(version=POLYMARKET_PILOT_PROTOCOL_VERSION)


def snapshot_fields(**overrides: Any) -> dict[str, Any]:
    fields = json.loads((bundled_fixture_path() / "protocol_v2.json").read_text())
    fields.update(overrides)
    return fields


def credential_request(**overrides: Any) -> CredentialProvisioningRequest:
    fields: dict[str, Any] = {
        "route": RouteKey.CREATE_OR_DERIVE_CREDENTIALS,
        "operation": "CREATE",
        "signer_address": VECTOR_ADDRESS,
        "funder_address": FUNDER_ADDRESS,
        "signature_type": 0,
        "timestamp": "1787673600",
        "nonce": 0,
        "grant_digest": GRANT_DIGEST,
    }
    fields.update(overrides)
    return CredentialProvisioningRequest.model_validate(fields, strict=True)


def copied_fixtures(tmp_path: Path) -> Path:
    target = tmp_path / "fixtures"
    shutil.copytree(bundled_fixture_path(), target)
    return target


def test_both_checkpoints_load_and_the_v1_snapshot_is_unchanged() -> None:
    v1 = load_protocol_snapshot()
    v2 = pilot_snapshot()

    assert v1.version == POLYMARKET_PROTOCOL_VERSION
    assert v2.version == POLYMARKET_PILOT_PROTOCOL_VERSION
    assert v1.source_manifest_path == "sources_v1.json"
    assert v2.source_manifest_path == "sources_v2.json"
    assert verify_protocol_sources(v1).state == "CURRENT"
    assert (
        verify_protocol_sources(root=bundled_fixture_path(), version=v2.version).state == "CURRENT"
    )


def test_an_unknown_checkpoint_is_refused() -> None:
    with pytest.raises(ProtocolSnapshotError, match="PROTOCOL_VERSION_UNKNOWN"):
        load_protocol_snapshot(version="polymarket-clob-2027-01-01-v3")


def test_v2_sources_were_refreshed_for_this_checkpoint() -> None:
    sources = pilot_snapshot().sources

    assert sources
    assert all(
        source.protocol_fixture_version == POLYMARKET_PILOT_PROTOCOL_VERSION for source in sources
    )
    assert all(source.retrieved_at == "2026-08-29T00:00:00Z" for source in sources)
    assert {source.source_id for source in sources} >= {
        "polymarket_post_order_reference",
        "polymarket_trading_quickstart",
        "polymarket_geographic_restrictions",
    }


def test_new_wallet_requires_explicit_account_signature_model() -> None:
    snapshot = PolymarketProtocolSnapshot.model_validate_json(
        json.dumps(snapshot_fields(account_signature_model=None)), strict=True
    )
    with pytest.raises(ProtocolSnapshotError, match="ACCOUNT_SIGNATURE_MODEL_REQUIRED"):
        require_account_signature_model(snapshot)


def test_the_account_model_never_defaults_to_signature_type_zero() -> None:
    model = require_account_signature_model(pilot_snapshot())

    assert model.default_signature_type is None
    assert model.requires_explicit_funder is True
    assert model.allowed_signature_types == (0, 1, 2)
    with pytest.raises(ValidationError):
        AccountSignatureModel.model_validate(
            {**model.model_dump(mode="python"), "default_signature_type": 0}, strict=True
        )


def test_the_deposit_wallet_path_stays_unsupported_until_review() -> None:
    snapshot = pilot_snapshot()
    model = require_account_signature_model(snapshot)

    assert [wallet.wallet for wallet in model.unsupported_wallets] == ["DEPOSIT"]
    assert 3 not in {wallet.signature_type for wallet in snapshot.eip712.wallets}
    with pytest.raises(ProtocolSnapshotError, match="ACCOUNT_SIGNATURE_MODEL_UNSUPPORTED"):
        bind_account_signature(
            snapshot,
            signer_address=VECTOR_ADDRESS,
            funder_address=FUNDER_ADDRESS,
            signature_type=3,
            negative_risk=False,
            credential_route_hash=CREDENTIAL_ROUTE_SET_HASH,
        )


def test_a_documented_wallet_binds_signer_funder_exchange_and_credential_route() -> None:
    snapshot = pilot_snapshot()
    binding = bind_account_signature(
        snapshot,
        signer_address=VECTOR_ADDRESS,
        funder_address=FUNDER_ADDRESS,
        signature_type=1,
        negative_risk=True,
        credential_route_hash=CREDENTIAL_ROUTE_SET_HASH,
    )

    assert binding.signer_address == VECTOR_ADDRESS
    assert binding.funder_address == FUNDER_ADDRESS
    assert binding.signature_type == 1
    assert binding.chain_id == 137
    assert binding.exchange_address == snapshot.eip712.exchange_addresses.negative_risk
    assert binding.credential_route_hash == CREDENTIAL_ROUTE_SET_HASH
    assert binding.protocol_version == POLYMARKET_PILOT_PROTOCOL_VERSION


def test_credential_route_is_not_an_execution_route() -> None:
    assert RouteKey.CREATE_OR_DERIVE_CREDENTIALS not in execution_route_keys()
    assert RouteKey.CREATE_OR_DERIVE_CREDENTIALS not in ROUTE_SPECS
    assert set(CREDENTIAL_ROUTE_SPECS) == {"CREATE", "DERIVE"}
    assert all(spec.auth_level == "L1" for spec in CREDENTIAL_ROUTE_SPECS.values())
    assert not any(spec.mutation for spec in CREDENTIAL_ROUTE_SPECS.values())


def test_credential_request_carries_no_order_or_secret_surface() -> None:
    request = credential_request()
    dumped = request.model_dump(mode="json")

    assert set(dumped) == {
        "route",
        "operation",
        "signer_address",
        "funder_address",
        "signature_type",
        "timestamp",
        "nonce",
        "grant_digest",
    }
    assert request.spec.path == "/auth/api-key"
    assert credential_request(operation="DERIVE").spec.path == "/auth/derive-api-key"
    for forbidden in ("order", "tokenId", "apiKey", "secret", "passphrase", "path", "host"):
        with pytest.raises(ValidationError):
            credential_request(**{forbidden: "x"})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("signer_address", "0xnot-an-address"),
        ("funder_address", ""),
        ("timestamp", "-1"),
        ("nonce", -1),
        ("signature_type", 3),
        ("operation", "REVOKE"),
        ("grant_digest", "short"),
    ],
)
def test_credential_request_rejects_each_mutated_binding_field(field: str, value: Any) -> None:
    with pytest.raises(ValidationError):
        credential_request(**{field: value})


def test_credential_vector_signature_changes_with_every_bound_field() -> None:
    snapshot = pilot_snapshot()
    vectors = json.loads((bundled_fixture_path() / "credential_vectors_v2.json").read_text())
    vector = vectors["l1_signature_vector"]
    private_material = (1).to_bytes(32, "big")

    frozen = sign_clob_auth(private_material, vector["timestamp"], snapshot)
    assert sha256(frozen.encode("ascii")).hexdigest() == vector["signature_sha256"]

    shifted = sign_clob_auth(private_material, str(int(vector["timestamp"]) + 1), snapshot)
    assert shifted != frozen

    other_key = sign_clob_auth((2).to_bytes(32, "big"), vector["timestamp"], snapshot)
    assert other_key != frozen

    v1_domain = sign_clob_auth(private_material, vector["timestamp"], load_protocol_snapshot())
    assert v1_domain == frozen  # the ClobAuth domain is unchanged between both checkpoints


def test_credential_response_secrets_never_reach_the_caller() -> None:
    vectors = json.loads((bundled_fixture_path() / "credential_vectors_v2.json").read_text())
    handling = vectors["response_handling"]

    assert handling["secret_destination"] == "operating_system_keychain"
    assert not set(handling["returned_to_caller"]) & set(handling["secret_fields"])
    assert {"browser", "database", "logs"} <= set(handling["forbidden_destinations"])


def test_pilot_conformance_passes_offline_for_both_checkpoints() -> None:
    v1 = run_conformance(bundled_fixture_path(), "pilot-task-3")
    v2 = run_conformance(
        bundled_fixture_path(), "pilot-task-3", version=POLYMARKET_PILOT_PROTOCOL_VERSION
    )

    assert v1.result == "CONFORMANT"
    assert v2.result == "CONFORMANT"
    assert {"account_signature_model", "credential_vectors"} <= set(v2.executed_checks)
    assert {"account_signature_model", "credential_vectors"}.isdisjoint(v1.executed_checks)
    assert set(v1.fixture_hashes).isdisjoint(v2.fixture_hashes)


@pytest.mark.parametrize(
    "fixture_name",
    (
        "protocol_v2.json",
        "sources_v2.json",
        "order_vectors_v2.json",
        "event_vectors_v2.json",
        "credential_vectors_v2.json",
    ),
)
def test_one_changed_v2_byte_requires_review(tmp_path: Path, fixture_name: str) -> None:
    root = copied_fixtures(tmp_path)
    path = root / fixture_name
    path.write_bytes(path.read_bytes() + b" ")

    readiness = verify_protocol_sources(root=root, version=POLYMARKET_PILOT_PROTOCOL_VERSION)
    result = run_conformance(root, "mutated", version=POLYMARKET_PILOT_PROTOCOL_VERSION)

    assert readiness.state == "PROTOCOL_REVIEW_REQUIRED"
    assert result.result == "PROTOCOL_REVIEW_REQUIRED"


def test_a_drifting_account_model_fails_conformance_before_behavior_changes(
    tmp_path: Path,
) -> None:
    root = copied_fixtures(tmp_path)
    path = root / "protocol_v2.json"
    document = json.loads(path.read_text())
    document["account_signature_model"]["allowed_signature_types"] = [0, 1, 2, 3]
    path.write_bytes((json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))

    result = run_conformance(root, "drifted", version=POLYMARKET_PILOT_PROTOCOL_VERSION)

    assert result.result == "PROTOCOL_REVIEW_REQUIRED"
    with pytest.raises(ProtocolSnapshotError, match="ACCOUNT_SIGNATURE_MODEL_UNSUPPORTED"):
        require_account_signature_model(
            PolymarketProtocolSnapshot.model_validate_json(path.read_bytes(), strict=True)
        )


def test_v1_conformance_evidence_is_not_rewritten_by_the_new_checkpoint() -> None:
    v1 = run_conformance(bundled_fixture_path(), "pilot-task-3")
    fixture_digests = {
        sha256((bundled_fixture_path() / name).read_bytes()).hexdigest()
        for name in (
            "protocol_v1.json",
            "sources_v1.json",
            "order_vectors_v1.json",
            "event_vectors_v1.json",
        )
    }

    assert set(v1.fixture_hashes) == fixture_digests
    assert all(
        source.protocol_fixture_version == POLYMARKET_PROTOCOL_VERSION
        for source in load_protocol_snapshot().sources
    )
