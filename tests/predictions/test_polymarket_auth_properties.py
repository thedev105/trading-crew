from __future__ import annotations

import pytest
from eth_account import Account
from eth_account.messages import encode_typed_data
from hypothesis import given
from hypothesis import strategies as st

from polytrading.predictions.polymarket_execution import load_protocol_snapshot
from polytrading.predictions.polymarket_execution.auth import (
    ClobAuthError,
    ClobCredentials,
    clob_auth_typed_data,
    l2_preimage,
    sign_clob_auth,
    sign_l2_request,
)
from tests.predictions.test_polymarket_auth import (
    _assert_canaries_absent,
    _assert_context_free,
    _assert_sensitive_distinct,
    _assert_sensitive_equal,
)

PRIVATE_KEY = bytes.fromhex("00" * 31 + "01")
OTHER_PRIVATE_KEY = bytes.fromhex("00" * 31 + "02")
ADDRESS = "0x7E5F4552091A69125d5DfCb7b8C2659029395Bdf"
TIMESTAMP = "1787673600"
API_KEY = b"task-6-api-key"
SECRET = b"cG9seW1hcmtldC10YXNrLTYtaG1hYy1rZXk="
OTHER_SECRET = b"YW5vdGhlci10YXNrLTYtaG1hYy1rZXk="
PASSPHRASE = b"task-6-passphrase"


def _credentials(**overrides: object) -> ClobCredentials:
    fields: dict[str, object] = {
        "address": ADDRESS,
        "api_key": API_KEY,
        "secret": SECRET,
        "passphrase": PASSPHRASE,
    }
    fields.update(overrides)
    return ClobCredentials(**fields)  # type: ignore[arg-type]


def _l2_signature(credentials: ClobCredentials, **overrides: object) -> str:
    fields: dict[str, object] = {
        "timestamp": TIMESTAMP,
        "method": "POST",
        "route": "/order",
        "body": b'{"a":1,"b":2}',
    }
    fields.update(overrides)
    return sign_l2_request(credentials, **fields).signature  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field", "changed"),
    (
        ("timestamp", "1787673601"),
        ("nonce", 1),
    ),
)
def test_each_clob_auth_message_scalar_changes_the_l1_signature(
    field: str,
    changed: object,
) -> None:
    snapshot = load_protocol_snapshot()
    arguments: dict[str, object] = {
        "private_key": PRIVATE_KEY,
        "timestamp": TIMESTAMP,
        "snapshot": snapshot,
        "nonce": 0,
    }
    arguments[field] = changed

    changed_signature = sign_clob_auth(**arguments)  # type: ignore[arg-type]
    _assert_sensitive_distinct(
        changed_signature,
        sign_clob_auth(PRIVATE_KEY, TIMESTAMP, snapshot),
    )


def test_wallet_address_is_signed_by_clob_auth() -> None:
    snapshot = load_protocol_snapshot()

    first = sign_clob_auth(PRIVATE_KEY, TIMESTAMP, snapshot)
    second = sign_clob_auth(OTHER_PRIVATE_KEY, TIMESTAMP, snapshot)

    _assert_sensitive_distinct(first, second)
    typed_data = clob_auth_typed_data(
        Account.from_key(OTHER_PRIVATE_KEY).address,
        TIMESTAMP,
        snapshot,
    )
    assert (
        Account.recover_message(encode_typed_data(full_message=typed_data), signature=second)
        == Account.from_key(OTHER_PRIVATE_KEY).address
    )


@pytest.mark.parametrize(
    ("field", "changed"),
    (
        ("name", "ChangedClobAuthDomain"),
        ("version", "2"),
        ("chain_id", 1),
    ),
)
def test_each_clob_auth_domain_field_changes_the_l1_signature(
    field: str,
    changed: object,
) -> None:
    snapshot = load_protocol_snapshot()
    changed_domain = snapshot.authentication.clob_auth.domain.model_copy(update={field: changed})
    changed_clob = snapshot.authentication.clob_auth.model_copy(update={"domain": changed_domain})
    changed_authentication = snapshot.authentication.model_copy(update={"clob_auth": changed_clob})
    changed_snapshot = snapshot.model_copy(update={"authentication": changed_authentication})

    _assert_sensitive_distinct(
        sign_clob_auth(PRIVATE_KEY, TIMESTAMP, changed_snapshot),
        sign_clob_auth(PRIVATE_KEY, TIMESTAMP, snapshot),
    )


def test_frozen_clob_auth_attestation_message_is_signed() -> None:
    snapshot = load_protocol_snapshot()
    changed_clob = snapshot.authentication.clob_auth.model_copy(
        update={"message": "Changed local attestation"}
    )
    changed_snapshot = snapshot.model_copy(
        update={
            "authentication": snapshot.authentication.model_copy(update={"clob_auth": changed_clob})
        }
    )

    _assert_sensitive_distinct(
        sign_clob_auth(PRIVATE_KEY, TIMESTAMP, changed_snapshot),
        sign_clob_auth(PRIVATE_KEY, TIMESTAMP, snapshot),
    )


@pytest.mark.parametrize(
    ("field", "changed", "error_code"),
    (
        ("address", "0x1234", "CLOB_AUTH_ADDRESS_INVALID"),
        ("address", b"not-text", "CLOB_AUTH_ADDRESS_INVALID"),
        ("timestamp", "not-seconds", "CLOB_AUTH_TIMESTAMP_INVALID"),
        ("timestamp", "1787673600\N{SNOWMAN}", "CLOB_AUTH_TIMESTAMP_INVALID"),
        ("timestamp", 1787673600, "CLOB_AUTH_TIMESTAMP_INVALID"),
        ("nonce", -1, "CLOB_AUTH_NONCE_INVALID"),
        ("nonce", 2**256, "CLOB_AUTH_NONCE_INVALID"),
        ("nonce", True, "CLOB_AUTH_NONCE_INVALID"),
    ),
    ids=(
        "address-shape",
        "address-type",
        "timestamp-shape",
        "timestamp-nonascii",
        "timestamp-type",
        "nonce-negative",
        "nonce-overflow",
        "nonce-bool",
    ),
)
def test_clob_auth_fields_fail_closed_before_eth_account(
    field: str,
    changed: object,
    error_code: str,
) -> None:
    snapshot = load_protocol_snapshot()
    arguments: dict[str, object] = {
        "address": ADDRESS,
        "timestamp": TIMESTAMP,
        "snapshot": snapshot,
        "nonce": 0,
    }
    arguments[field] = changed

    with pytest.raises(ClobAuthError, match=error_code) as rejected:
        clob_auth_typed_data(**arguments)  # type: ignore[arg-type]
    assert str(rejected.value) == error_code
    _assert_context_free(rejected.value)


@pytest.mark.parametrize(
    ("field", "changed", "error_code"),
    (
        ("timestamp", "not-seconds", "L2_TIMESTAMP_INVALID"),
        ("timestamp", "1787673600\N{SNOWMAN}", "L2_TIMESTAMP_INVALID"),
        ("timestamp", 1787673600, "L2_TIMESTAMP_INVALID"),
        ("method", "", "L2_METHOD_INVALID"),
        ("method", "PO ST", "L2_METHOD_INVALID"),
        ("method", "P\N{SNOWMAN}ST", "L2_METHOD_INVALID"),
        ("method", b"POST", "L2_METHOD_INVALID"),
        ("route", "order", "L2_ROUTE_INVALID"),
        ("route", "/order?query=1", "L2_ROUTE_INVALID"),
        ("route", "/order#fragment", "L2_ROUTE_INVALID"),
        ("route", "/ord\N{SNOWMAN}er", "L2_ROUTE_INVALID"),
        ("route", b"/order", "L2_ROUTE_INVALID"),
    ),
    ids=(
        "timestamp-shape",
        "timestamp-nonascii",
        "timestamp-type",
        "method-empty",
        "method-space",
        "method-nonascii",
        "method-type",
        "route-leading-slash",
        "route-query",
        "route-fragment",
        "route-nonascii",
        "route-type",
    ),
)
def test_l2_preimage_fields_fail_closed_with_stable_sanitized_errors(
    field: str,
    changed: object,
    error_code: str,
) -> None:
    arguments: dict[str, object] = {
        "timestamp": TIMESTAMP,
        "method": "POST",
        "route": "/order",
        "body": b"{}",
    }
    arguments[field] = changed

    with pytest.raises(ClobAuthError, match=error_code) as rejected:
        l2_preimage(**arguments)  # type: ignore[arg-type]
    assert str(rejected.value) == error_code
    _assert_context_free(rejected.value)


def test_every_exact_body_byte_participates_in_l2_signature() -> None:
    credentials = _credentials()
    body = bytes(range(256))
    baseline = _l2_signature(credentials, body=body)

    for index in range(len(body)):
        changed = body[:index] + bytes((body[index] ^ 1,)) + body[index + 1 :]
        _assert_sensitive_distinct(_l2_signature(credentials, body=changed), baseline)


@given(
    timestamp=st.integers(min_value=0, max_value=2**63 - 1).map(str),
    method=st.sampled_from(("GET", "POST", "DELETE")),
    route=st.sampled_from(("/order", "/orders", "/data/orders", "/v1/heartbeats")),
    body=st.binary(max_size=128),
)
def test_l2_preimage_is_literal_ascii_components_plus_exact_body(
    timestamp: str,
    method: str,
    route: str,
    body: bytes,
) -> None:
    assert l2_preimage(timestamp, method.lower(), route, body) == (
        timestamp.encode("ascii") + method.encode("ascii") + route.encode("ascii") + body
    )


def test_timestamp_route_method_and_secret_participate_in_l2_signature() -> None:
    credentials = _credentials()
    baseline = _l2_signature(credentials)

    _assert_sensitive_distinct(_l2_signature(credentials, timestamp="1787673601"), baseline)
    _assert_sensitive_distinct(_l2_signature(credentials, method="DELETE"), baseline)
    _assert_sensitive_distinct(_l2_signature(credentials, route="/orders"), baseline)
    _assert_sensitive_distinct(_l2_signature(_credentials(secret=OTHER_SECRET)), baseline)


def test_address_api_key_and_passphrase_are_emitted_but_not_signed() -> None:
    credentials = _credentials()
    baseline = sign_l2_request(
        credentials,
        timestamp=TIMESTAMP,
        method="POST",
        route="/order",
        body=b"{}",
    )
    changes = (
        ("address", "0x" + "22" * 20, "POLY_ADDRESS", "0x" + "22" * 20),
        ("api_key", b"changed-api-key", "POLY_API_KEY", "changed-api-key"),
        ("passphrase", b"changed-passphrase", "POLY_PASSPHRASE", "changed-passphrase"),
    )

    for field, changed, header, expected in changes:
        result = sign_l2_request(
            _credentials(**{field: changed}),
            timestamp=TIMESTAMP,
            method="POST",
            route="/order",
            body=b"{}",
        )
        _assert_sensitive_equal(result.signature, baseline.signature)
        _assert_sensitive_equal(result[header], expected)
        _assert_sensitive_distinct(dict(result), dict(baseline))


def test_secret_is_signed_but_never_emitted_as_a_header() -> None:
    baseline = sign_l2_request(
        _credentials(),
        timestamp=TIMESTAMP,
        method="POST",
        route="/order",
        body=b"{}",
    )
    changed = sign_l2_request(
        _credentials(secret=OTHER_SECRET),
        timestamp=TIMESTAMP,
        method="POST",
        route="/order",
        body=b"{}",
    )

    _assert_sensitive_distinct(changed.signature, baseline.signature)
    assert set(changed) == {
        "POLY_ADDRESS",
        "POLY_SIGNATURE",
        "POLY_TIMESTAMP",
        "POLY_API_KEY",
        "POLY_PASSPHRASE",
    }
    emitted_values = "\x00".join(dict(changed).values())
    _assert_canaries_absent(emitted_values, SECRET, OTHER_SECRET)
