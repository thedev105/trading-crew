from __future__ import annotations

import base64
import copy
import io
import pickle
from collections.abc import Callable
from dataclasses import asdict

import httpx
import pytest
from eth_account import Account
from eth_account.messages import encode_typed_data

from polytrading.predictions.execution.models import PredictionRecord
from polytrading.predictions.polymarket_execution import auth as auth_module
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
_SECRET_ASSERTION_FAILED = "SECRET_CANARY_DETECTED"


class _TextSubclass(str):
    pass


def _assert_canaries_absent(
    observed: str | bytes,
    *canaries: str | bytes,
) -> None:
    observed_bytes = observed if type(observed) is bytes else observed.encode("utf-8")
    for canary in canaries:
        canary_bytes = canary if type(canary) is bytes else canary.encode("utf-8")
        if canary_bytes in observed_bytes:
            raise AssertionError(_SECRET_ASSERTION_FAILED) from None


def _assert_sensitive_equal(actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError("SENSITIVE_COMPARISON_FAILED") from None


def _assert_sensitive_distinct(first: object, second: object) -> None:
    if first == second:
        raise AssertionError("SENSITIVE_VALUES_NOT_DISTINCT") from None


def _assert_context_free(error: BaseException) -> None:
    if error.__cause__ is not None or error.__context__ is not None:
        raise AssertionError("AUTH_ERROR_CHAIN_NOT_EMPTY") from None


def _captured_auth_error(operation: Callable[[], object]) -> ClobAuthError:
    captured: ClobAuthError | None = None
    unexpected = False
    try:
        operation()
    except ClobAuthError as error:
        captured = error
    except Exception:
        unexpected = True
    if captured is not None:
        return captured
    if unexpected:
        raise AssertionError("AUTH_ERROR_TYPE_INVALID") from None
    raise AssertionError("AUTH_ERROR_NOT_RAISED") from None


def _error_observable(error: BaseException) -> str:
    chain = (error, error.__cause__, error.__context__)
    return "".join(str(item) + repr(item) for item in chain if item is not None)


def fixture_clob_credentials(**overrides: object) -> ClobCredentials:
    fields: dict[str, object] = {
        "address": ADDRESS,
        "api_key": API_KEY,
        "secret": SECRET,
        "passphrase": PASSPHRASE,
    }
    fields.update(overrides)
    return ClobCredentials(**fields)  # type: ignore[arg-type]


def fixture_l2_headers(**overrides: object) -> L2AuthHeaders:
    fields: dict[str, object] = {
        "address": ADDRESS,
        "signature": L2_SIGNATURE,
        "timestamp": TIMESTAMP,
        "api_key": API_KEY.decode("ascii"),
        "passphrase": PASSPHRASE.decode("ascii"),
    }
    fields.update(overrides)
    return L2AuthHeaders(**fields)  # type: ignore[arg-type]


def test_secret_absence_helper_fails_without_rendering_its_canary() -> None:
    canary = "negative-helper-secret-canary"

    with pytest.raises(AssertionError) as rejected:
        _assert_canaries_absent(canary, canary)

    _assert_canaries_absent(_error_observable(rejected.value), canary)
    _assert_context_free(rejected.value)


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

    _assert_sensitive_equal(signature, L1_SIGNATURE)
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
    _assert_sensitive_equal(headers.signature, L2_SIGNATURE)
    assert tuple(headers) == (
        "POLY_ADDRESS",
        "POLY_SIGNATURE",
        "POLY_TIMESTAMP",
        "POLY_API_KEY",
        "POLY_PASSPHRASE",
    )
    _assert_sensitive_equal(
        dict(headers),
        {
            "POLY_ADDRESS": ADDRESS,
            "POLY_SIGNATURE": L2_SIGNATURE,
            "POLY_TIMESTAMP": TIMESTAMP,
            "POLY_API_KEY": API_KEY.decode("ascii"),
            "POLY_PASSPHRASE": PASSPHRASE.decode("ascii"),
        },
    )


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

    _assert_sensitive_distinct(first.signature, second.signature)


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
    _assert_sensitive_equal(credentials.api_key, API_KEY)
    _assert_sensitive_equal(credentials.secret, SECRET)
    _assert_sensitive_equal(credentials.passphrase, PASSPHRASE)
    assert not hasattr(credentials, "model_dump")
    assert not hasattr(credentials, "model_dump_json")
    assert not hasattr(credentials, "__dict__")
    with pytest.raises(TypeError):
        vars(credentials)
    with pytest.raises(TypeError):
        asdict(credentials)  # type: ignore[arg-type]
    with pytest.raises(AttributeError):
        credentials.secret = b"changed"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("operation_name", "operation"),
    (
        ("pickle", pickle.dumps),
        ("copy", copy.copy),
        ("deepcopy", copy.deepcopy),
        ("reduce", lambda value: value.__reduce__()),
        ("reduce-ex", lambda value: value.__reduce_ex__(pickle.HIGHEST_PROTOCOL)),
        ("getstate", lambda value: value.__getstate__()),
    ),
)
@pytest.mark.parametrize(
    "auth_object_kind",
    ("credentials", "headers"),
)
def test_secret_bearing_auth_objects_refuse_every_serialization_and_copy_path(
    operation_name: str,
    operation: Callable[[object], object],
    auth_object_kind: str,
) -> None:
    del operation_name
    if auth_object_kind == "credentials":
        value = fixture_clob_credentials()
        error_code = "CLOB_CREDENTIALS_NOT_SERIALIZABLE"
        canaries: tuple[str | bytes, ...] = (API_KEY, SECRET, PASSPHRASE)
    else:
        value = fixture_l2_headers()
        error_code = "L2_AUTH_HEADERS_NOT_SERIALIZABLE"
        canaries = (API_KEY, PASSPHRASE, L2_SIGNATURE)
    error = _captured_auth_error(lambda: operation(value))

    _assert_sensitive_equal(str(error), error_code)
    _assert_canaries_absent(_error_observable(error), *canaries)
    _assert_context_free(error)


@pytest.mark.parametrize(
    "auth_object_kind",
    ("credentials", "headers"),
)
def test_failed_pickle_writes_no_secret_canary_bytes(
    auth_object_kind: str,
) -> None:
    if auth_object_kind == "credentials":
        value = fixture_clob_credentials()
        error_code = "CLOB_CREDENTIALS_NOT_SERIALIZABLE"
        canaries: tuple[str | bytes, ...] = (API_KEY, SECRET, PASSPHRASE)
    else:
        value = fixture_l2_headers()
        error_code = "L2_AUTH_HEADERS_NOT_SERIALIZABLE"
        canaries = (API_KEY, PASSPHRASE, L2_SIGNATURE)
    buffer = io.BytesIO()
    error = _captured_auth_error(lambda: pickle.Pickler(buffer).dump(value))

    _assert_sensitive_equal(str(error), error_code)
    _assert_canaries_absent(buffer.getvalue(), *canaries)
    _assert_canaries_absent(pickle.dumps(error), *canaries)
    _assert_context_free(error)


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
    _assert_canaries_absent(rendered, API_KEY, SECRET, PASSPHRASE, L2_SIGNATURE)


def test_l2_headers_remain_an_exact_httpx_compatible_mapping() -> None:
    headers = fixture_l2_headers()

    request = httpx.Request("GET", "https://example.invalid/", headers=headers)

    assert tuple(headers) == (
        "POLY_ADDRESS",
        "POLY_SIGNATURE",
        "POLY_TIMESTAMP",
        "POLY_API_KEY",
        "POLY_PASSPHRASE",
    )
    assert len(headers) == 5
    for name, value in headers.items():
        _assert_sensitive_equal(request.headers[name], value)


_INVALID_L2_HEADER_CASES: dict[str, tuple[str, object, str]] = {
    "address-type": ("address", b"not-text", "L2_HEADER_ADDRESS_INVALID"),
    "address-subclass": ("address", _TextSubclass(ADDRESS), "L2_HEADER_ADDRESS_INVALID"),
    "address-shape": ("address", "0x1234", "L2_HEADER_ADDRESS_INVALID"),
    "signature-type": ("signature", b"not-text", "L2_HEADER_SIGNATURE_INVALID"),
    "signature-subclass": (
        "signature",
        _TextSubclass(L2_SIGNATURE),
        "L2_HEADER_SIGNATURE_INVALID",
    ),
    "signature-padding": (
        "signature",
        L2_SIGNATURE.rstrip("="),
        "L2_HEADER_SIGNATURE_INVALID",
    ),
    "signature-decoded-size": (
        "signature",
        base64.urlsafe_b64encode(b"x" * 33).decode("ascii"),
        "L2_HEADER_SIGNATURE_INVALID",
    ),
    "signature-alphabet": ("signature", "!" * 44, "L2_HEADER_SIGNATURE_INVALID"),
    "signature-oversized": ("signature", "A" * 4097, "L2_HEADER_SIGNATURE_INVALID"),
    "timestamp-type": ("timestamp", 1787673600, "L2_HEADER_TIMESTAMP_INVALID"),
    "timestamp-subclass": (
        "timestamp",
        _TextSubclass(TIMESTAMP),
        "L2_HEADER_TIMESTAMP_INVALID",
    ),
    "timestamp-empty": ("timestamp", "", "L2_HEADER_TIMESTAMP_INVALID"),
    "timestamp-control": ("timestamp", "12\n34", "L2_HEADER_TIMESTAMP_INVALID"),
    "timestamp-nonascii-digit": (
        "timestamp",
        "\N{ARABIC-INDIC DIGIT ONE}",
        "L2_HEADER_TIMESTAMP_INVALID",
    ),
    "timestamp-oversized": ("timestamp", "1" * 4097, "L2_HEADER_TIMESTAMP_INVALID"),
    "api-key-type": ("api_key", b"not-text", "L2_HEADER_API_KEY_INVALID"),
    "api-key-subclass": (
        "api_key",
        _TextSubclass(API_KEY.decode("ascii")),
        "L2_HEADER_API_KEY_INVALID",
    ),
    "api-key-empty": ("api_key", "", "L2_HEADER_API_KEY_INVALID"),
    "api-key-nonascii": ("api_key", "not-ascii-☃", "L2_HEADER_API_KEY_INVALID"),
    "api-key-newline": ("api_key", "line\nbreak", "L2_HEADER_API_KEY_INVALID"),
    "api-key-nul": ("api_key", "nul\x00byte", "L2_HEADER_API_KEY_INVALID"),
    "api-key-oversized": ("api_key", "a" * 4097, "L2_HEADER_API_KEY_INVALID"),
    "passphrase-type": ("passphrase", b"not-text", "L2_HEADER_PASSPHRASE_INVALID"),
    "passphrase-subclass": (
        "passphrase",
        _TextSubclass(PASSPHRASE.decode("ascii")),
        "L2_HEADER_PASSPHRASE_INVALID",
    ),
    "passphrase-empty": ("passphrase", "", "L2_HEADER_PASSPHRASE_INVALID"),
    "passphrase-nonascii": (
        "passphrase",
        "not-ascii-☃",
        "L2_HEADER_PASSPHRASE_INVALID",
    ),
    "passphrase-newline": (
        "passphrase",
        "line\nbreak",
        "L2_HEADER_PASSPHRASE_INVALID",
    ),
    "passphrase-nul": ("passphrase", "nul\x00byte", "L2_HEADER_PASSPHRASE_INVALID"),
    "passphrase-oversized": (
        "passphrase",
        "p" * 4097,
        "L2_HEADER_PASSPHRASE_INVALID",
    ),
}


@pytest.mark.parametrize("case_id", tuple(_INVALID_L2_HEADER_CASES))
def test_direct_l2_header_construction_rejects_invalid_values(
    case_id: str,
) -> None:
    field, value, error_code = _INVALID_L2_HEADER_CASES[case_id]
    fields: dict[str, object] = {
        "address": ADDRESS,
        "signature": L2_SIGNATURE,
        "timestamp": TIMESTAMP,
        "api_key": API_KEY.decode("ascii"),
        "passphrase": PASSPHRASE.decode("ascii"),
    }
    fields[field] = value

    error = _captured_auth_error(lambda: L2AuthHeaders(**fields))  # type: ignore[arg-type]

    _assert_sensitive_equal(str(error), error_code)
    canaries = (value,) if type(value) in (str, bytes) and value else ()
    _assert_canaries_absent(_error_observable(error), *canaries)
    _assert_context_free(error)


def test_direct_l2_header_construction_accepts_the_resource_safety_boundary() -> None:
    headers = L2AuthHeaders(
        address=ADDRESS,
        signature=L2_SIGNATURE,
        timestamp="1" * 4096,
        api_key="a" * 4096,
        passphrase="p" * 4096,
    )

    assert len(headers["POLY_TIMESTAMP"]) == 4096
    assert len(headers["POLY_API_KEY"]) == 4096
    assert len(headers["POLY_PASSPHRASE"]) == 4096


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
    error = _captured_auth_error(
        lambda: sign_l2_request(
            fixture_clob_credentials(),
            timestamp=TIMESTAMP,
            method="POST",
            route="/order",
            body=body,  # type: ignore[arg-type]
        )
    )

    _assert_sensitive_equal(str(error), error_code)
    _assert_context_free(error)


_INVALID_CREDENTIAL_CASES: dict[str, tuple[str, object, str]] = {
    "address-type": ("address", b"not-text", "CREDENTIAL_ADDRESS_INVALID"),
    "address-shape": ("address", "0x1234", "CREDENTIAL_ADDRESS_INVALID"),
    "api-key-type": ("api_key", "not-bytes", "CREDENTIAL_API_KEY_INVALID"),
    "api-key-empty": ("api_key", b"", "CREDENTIAL_API_KEY_INVALID"),
    "api-key-control": ("api_key", b"line\nbreak", "CREDENTIAL_API_KEY_INVALID"),
    "secret-type": ("secret", "not-bytes", "CREDENTIAL_SECRET_INVALID"),
    "secret-standard-base64": ("secret", b"not+url/safe", "CREDENTIAL_SECRET_INVALID"),
    "secret-padding": ("secret", b"a===", "CREDENTIAL_SECRET_INVALID"),
    "secret-empty": ("secret", b"", "CREDENTIAL_SECRET_INVALID"),
    "passphrase-type": ("passphrase", "not-bytes", "CREDENTIAL_PASSPHRASE_INVALID"),
    "passphrase-empty": ("passphrase", b"", "CREDENTIAL_PASSPHRASE_INVALID"),
    "passphrase-control": (
        "passphrase",
        b"control\x00byte",
        "CREDENTIAL_PASSPHRASE_INVALID",
    ),
}


@pytest.mark.parametrize("case_id", tuple(_INVALID_CREDENTIAL_CASES))
def test_credentials_fail_closed_with_stable_sanitized_errors(
    case_id: str,
) -> None:
    field, value, error_code = _INVALID_CREDENTIAL_CASES[case_id]
    fields: dict[str, object] = {
        "address": ADDRESS,
        "api_key": API_KEY,
        "secret": SECRET,
        "passphrase": PASSPHRASE,
    }
    fields[field] = value

    error = _captured_auth_error(lambda: ClobCredentials(**fields))  # type: ignore[arg-type]

    _assert_sensitive_equal(str(error), error_code)
    canaries = (value,) if type(value) in (str, bytes) and value else ()
    _assert_canaries_absent(_error_observable(error), *canaries)
    _assert_context_free(error)


def test_private_key_and_credential_canaries_never_escape_errors_or_chains() -> None:
    private_canary = b"private-key-canary-never-render"
    secret_canary = b"secret-canary!not-base64"
    passphrase_canary = b"passphrase-canary\nnot-header-safe"

    private_error = _captured_auth_error(
        lambda: sign_clob_auth(private_canary, TIMESTAMP, load_protocol_snapshot())
    )
    secret_error = _captured_auth_error(lambda: fixture_clob_credentials(secret=secret_canary))
    passphrase_error = _captured_auth_error(
        lambda: fixture_clob_credentials(passphrase=passphrase_canary)
    )

    _assert_sensitive_equal(str(private_error), "PRIVATE_KEY_INVALID")
    _assert_sensitive_equal(str(secret_error), "CREDENTIAL_SECRET_INVALID")
    _assert_sensitive_equal(str(passphrase_error), "CREDENTIAL_PASSPHRASE_INVALID")
    rendered = "".join(
        _error_observable(error) for error in (private_error, secret_error, passphrase_error)
    )
    _assert_canaries_absent(rendered, private_canary, secret_canary, passphrase_canary)
    for error in (private_error, secret_error, passphrase_error):
        _assert_context_free(error)


def test_library_failures_are_translated_without_secret_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_key = bytes(32)
    signature_canary = "signature-library-canary"
    base64_canary = "base64-library-canary"

    private_error = _captured_auth_error(
        lambda: sign_clob_auth(private_key, TIMESTAMP, load_protocol_snapshot())
    )

    def raise_signature_error(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise ValueError(signature_canary)

    with monkeypatch.context() as patch:
        patch.setattr(auth_module.Account, "sign_typed_data", raise_signature_error)
        signature_error = _captured_auth_error(
            lambda: sign_clob_auth(PRIVATE_KEY, TIMESTAMP, load_protocol_snapshot())
        )

    def raise_base64_error(value: object) -> bytes:
        del value
        raise ValueError(base64_canary)

    with monkeypatch.context() as patch:
        patch.setattr(auth_module.base64, "urlsafe_b64decode", raise_base64_error)
        base64_error = _captured_auth_error(lambda: fixture_clob_credentials(secret=SECRET))
        header_base64_error = _captured_auth_error(fixture_l2_headers)

    for error, error_code, canary in (
        (private_error, "PRIVATE_KEY_INVALID", private_key),
        (signature_error, "CLOB_AUTH_SIGNING_FAILED", signature_canary),
        (base64_error, "CREDENTIAL_SECRET_INVALID", base64_canary),
        (header_base64_error, "L2_HEADER_SIGNATURE_INVALID", base64_canary),
    ):
        _assert_sensitive_equal(str(error), error_code)
        _assert_canaries_absent(_error_observable(error), canary)
        _assert_context_free(error)
