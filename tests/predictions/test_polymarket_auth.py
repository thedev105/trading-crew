from __future__ import annotations

import pickle
from dataclasses import asdict

import pytest
from eth_account import Account
from eth_account.messages import encode_typed_data

from polytrading.predictions.execution.models import PredictionRecord
from polytrading.predictions.polymarket_execution import load_protocol_snapshot
from polytrading.predictions.polymarket_execution.auth import (
    ClobAuthError,
    ClobCredentials,
    L2AuthHeaders,
    clob_auth_typed_data,
    l2_preimage,
    sign_clob_auth,
    sign_l2_request,
)

# Local deterministic Task 6 conformance vector. It is derived from the frozen Task 4
# schema/rules and is deliberately not represented as an official Polymarket fixture.
PRIVATE_KEY = bytes.fromhex("00" * 31 + "01")
ADDRESS = "0x7E5F4552091A69125d5DfCb7b8C2659029395Bdf"
TIMESTAMP = "1787673600"
API_KEY = b"task-6-api-key"
SECRET = b"cG9seW1hcmtldC10YXNrLTYtaG1hYy1rZXk="
PASSPHRASE = b"task-6-passphrase"
L1_SIGNATURE = (
    "0xb3b4e0ffe7b91188c491f6e4d1b83b41012ee676c57c8c95df10f911facb487d"
    "79b370710ca2d7b6259f0098c5b7dd94b654c3822f98c0f9b7eef6b20777dadf1c"
)
L2_SIGNATURE = "bOdkWlKitDLY4SC1s2liOHjReuOPlQPoQK9UtyIggOo="


def fixture_clob_credentials(**overrides: object) -> ClobCredentials:
    fields: dict[str, object] = {
        "address": ADDRESS,
        "api_key": API_KEY,
        "secret": SECRET,
        "passphrase": PASSPHRASE,
    }
    fields.update(overrides)
    return ClobCredentials(**fields)  # type: ignore[arg-type]


def test_clob_auth_typed_data_matches_frozen_schema_and_local_vector() -> None:
    snapshot = load_protocol_snapshot()

    typed_data = clob_auth_typed_data(ADDRESS, TIMESTAMP, snapshot)

    assert typed_data == {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
            ],
            "ClobAuth": [
                {"name": "address", "type": "address"},
                {"name": "timestamp", "type": "string"},
                {"name": "nonce", "type": "uint256"},
                {"name": "message", "type": "string"},
            ],
        },
        "primaryType": "ClobAuth",
        "domain": {"name": "ClobAuthDomain", "version": "1", "chainId": 137},
        "message": {
            "address": ADDRESS,
            "timestamp": TIMESTAMP,
            "nonce": 0,
            "message": "This message attests that I control the given wallet",
        },
    }


def test_sign_clob_auth_matches_local_vector_and_recovers_wallet() -> None:
    snapshot = load_protocol_snapshot()

    signature = sign_clob_auth(PRIVATE_KEY, TIMESTAMP, snapshot)

    assert signature == L1_SIGNATURE
    recovered = Account.recover_message(
        encode_typed_data(full_message=clob_auth_typed_data(ADDRESS, TIMESTAMP, snapshot)),
        signature=signature,
    )
    assert recovered == ADDRESS


def test_l2_preimage_matches_frozen_heartbeat_exact_bytes() -> None:
    assert (
        l2_preimage(
            TIMESTAMP,
            "post",
            "/v1/heartbeats",
            b'{"heartbeat_id":""}',
        )
        == b'1787673600POST/v1/heartbeats{"heartbeat_id":""}'
    )


def test_l2_signature_and_all_five_headers_match_local_vector() -> None:
    headers = sign_l2_request(
        fixture_clob_credentials(),
        timestamp=TIMESTAMP,
        method="post",
        route="/v1/heartbeats",
        body=b'{"heartbeat_id":""}',
    )

    assert isinstance(headers, L2AuthHeaders)
    assert headers.signature == L2_SIGNATURE
    assert tuple(headers) == (
        "POLY_ADDRESS",
        "POLY_SIGNATURE",
        "POLY_TIMESTAMP",
        "POLY_API_KEY",
        "POLY_PASSPHRASE",
    )
    assert dict(headers) == {
        "POLY_ADDRESS": ADDRESS,
        "POLY_SIGNATURE": L2_SIGNATURE,
        "POLY_TIMESTAMP": TIMESTAMP,
        "POLY_API_KEY": API_KEY.decode("ascii"),
        "POLY_PASSPHRASE": PASSPHRASE.decode("ascii"),
    }


def test_l2_signature_uses_exact_serialized_body_bytes() -> None:
    credentials = fixture_clob_credentials()
    first = sign_l2_request(
        credentials,
        timestamp="1787688000",
        method="POST",
        route="/order",
        body=b'{"a":1,"b":2}',
    )
    second = sign_l2_request(
        credentials,
        timestamp="1787688000",
        method="POST",
        route="/order",
        body=b'{ "a": 1, "b": 2 }',
    )

    assert first.signature != second.signature


def test_method_case_is_canonicalized_before_l2_signing() -> None:
    credentials = fixture_clob_credentials()
    signatures = {
        sign_l2_request(
            credentials,
            timestamp=TIMESTAMP,
            method=method,
            route="/order",
            body=b"{}",
        ).signature
        for method in ("POST", "post", "PoSt")
    }

    assert len(signatures) == 1


def test_credentials_are_strict_immutable_nonrecord_secret_objects() -> None:
    credentials = fixture_clob_credentials()

    assert not isinstance(credentials, PredictionRecord)
    assert credentials.address == ADDRESS
    assert credentials.api_key == API_KEY
    assert credentials.secret == SECRET
    assert credentials.passphrase == PASSPHRASE
    assert not hasattr(credentials, "model_dump")
    assert not hasattr(credentials, "model_dump_json")
    assert not hasattr(credentials, "__dict__")
    with pytest.raises(TypeError):
        vars(credentials)
    with pytest.raises(TypeError):
        asdict(credentials)  # type: ignore[arg-type]
    with pytest.raises(AttributeError):
        credentials.secret = b"changed"  # type: ignore[misc]


def test_credentials_cannot_escape_through_generic_pickle_serialization() -> None:
    credentials = fixture_clob_credentials()

    with pytest.raises(TypeError, match="CLOB_CREDENTIALS_NOT_SERIALIZABLE") as rejected:
        pickle.dumps(credentials)
    rendered = str(rejected.value) + repr(rejected.value)
    for value in (API_KEY, SECRET, PASSPHRASE):
        assert value.decode("ascii") not in rendered


def test_credentials_and_headers_never_render_secret_bearing_values() -> None:
    credentials = fixture_clob_credentials()
    headers = sign_l2_request(
        credentials,
        timestamp=TIMESTAMP,
        method="GET",
        route="/data/orders",
        body=b"",
    )

    rendered = repr(credentials) + str(credentials) + repr(headers) + str(headers)
    for value in (API_KEY, SECRET, PASSPHRASE):
        assert value.decode("ascii") not in rendered
    assert L2_SIGNATURE not in rendered


@pytest.mark.parametrize(
    ("body", "error_code"),
    (
        ({}, "L2_BODY_BYTES_REQUIRED"),
        ("{}", "L2_BODY_BYTES_REQUIRED"),
        (bytearray(b"{}"), "L2_BODY_BYTES_REQUIRED"),
        (memoryview(b"{}"), "L2_BODY_BYTES_REQUIRED"),
    ),
    ids=("dict", "string", "bytearray", "memoryview"),
)
def test_l2_signing_rejects_unserialized_or_body_like_values(
    body: object,
    error_code: str,
) -> None:
    with pytest.raises(ClobAuthError, match=error_code) as rejected:
        sign_l2_request(
            fixture_clob_credentials(),
            timestamp=TIMESTAMP,
            method="POST",
            route="/order",
            body=body,  # type: ignore[arg-type]
        )
    assert rejected.value.__cause__ is None


@pytest.mark.parametrize(
    ("field", "value", "error_code"),
    (
        ("address", b"not-text", "CREDENTIAL_ADDRESS_INVALID"),
        ("address", "0x1234", "CREDENTIAL_ADDRESS_INVALID"),
        ("api_key", "not-bytes", "CREDENTIAL_API_KEY_INVALID"),
        ("api_key", b"", "CREDENTIAL_API_KEY_INVALID"),
        ("api_key", b"line\nbreak", "CREDENTIAL_API_KEY_INVALID"),
        ("secret", "not-bytes", "CREDENTIAL_SECRET_INVALID"),
        ("secret", b"not+url/safe", "CREDENTIAL_SECRET_INVALID"),
        ("secret", b"a===", "CREDENTIAL_SECRET_INVALID"),
        ("secret", b"", "CREDENTIAL_SECRET_INVALID"),
        ("passphrase", "not-bytes", "CREDENTIAL_PASSPHRASE_INVALID"),
        ("passphrase", b"", "CREDENTIAL_PASSPHRASE_INVALID"),
        ("passphrase", b"control\x00byte", "CREDENTIAL_PASSPHRASE_INVALID"),
    ),
    ids=(
        "address-type",
        "address-shape",
        "api-key-type",
        "api-key-empty",
        "api-key-control",
        "secret-type",
        "secret-standard-base64",
        "secret-padding",
        "secret-empty",
        "passphrase-type",
        "passphrase-empty",
        "passphrase-control",
    ),
)
def test_credentials_fail_closed_with_stable_sanitized_errors(
    field: str,
    value: object,
    error_code: str,
) -> None:
    fields: dict[str, object] = {
        "address": ADDRESS,
        "api_key": API_KEY,
        "secret": SECRET,
        "passphrase": PASSPHRASE,
    }
    fields[field] = value

    with pytest.raises(ClobAuthError, match=error_code) as rejected:
        ClobCredentials(**fields)  # type: ignore[arg-type]
    assert str(rejected.value) == error_code
    assert rejected.value.__cause__ is None


def test_private_key_and_credential_canaries_never_escape_errors_or_chains() -> None:
    private_canary = b"private-key-canary-never-render"
    secret_canary = b"secret-canary!not-base64"
    passphrase_canary = b"passphrase-canary\nnot-header-safe"

    with pytest.raises(ClobAuthError, match="PRIVATE_KEY_INVALID") as private_error:
        sign_clob_auth(private_canary, TIMESTAMP, load_protocol_snapshot())
    with pytest.raises(ClobAuthError, match="CREDENTIAL_SECRET_INVALID") as secret_error:
        fixture_clob_credentials(secret=secret_canary)
    with pytest.raises(ClobAuthError, match="CREDENTIAL_PASSPHRASE_INVALID") as passphrase_error:
        fixture_clob_credentials(passphrase=passphrase_canary)

    rendered = "".join(
        str(error.value) + repr(error.value)
        for error in (private_error, secret_error, passphrase_error)
    )
    for canary in (private_canary, secret_canary, passphrase_canary):
        assert canary.decode("ascii") not in rendered
    assert private_error.value.__cause__ is None
    assert secret_error.value.__cause__ is None
    assert passphrase_error.value.__cause__ is None
