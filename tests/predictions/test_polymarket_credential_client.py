from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from polytrading.predictions.polymarket_execution import credential_client as client_module
from polytrading.predictions.polymarket_execution.credential_client import (
    MAXIMUM_RESPONSE_BYTES,
    REQUEST_TIMEOUT_SECONDS,
    CredentialTransportError,
    HttpxCredentialClient,
)
from polytrading.predictions.polymarket_execution.keychain_macos import (
    CLOB_API_KEY_ACCOUNT,
    CLOB_API_SECRET_ACCOUNT,
    CLOB_PASSPHRASE_ACCOUNT,
)
from polytrading.predictions.polymarket_execution.protocol import (
    POLYMARKET_PILOT_PROTOCOL_VERSION,
    bind_account_signature,
    load_protocol_snapshot,
)
from polytrading.predictions.polymarket_execution.routes import CREDENTIAL_ROUTE_SET_HASH
from polytrading.predictions.polymarket_execution.secrets import SecretBuffer

SIGNER_ADDRESS = "0x7E5F4552091A69125d5DfCb7b8C2659029395Bdf"
FUNDER_ADDRESS = "0x" + "11" * 20
PRIVATE_KEY = (1).to_bytes(32, "big")
TIMESTAMP = "1787673600"
CREDENTIALS = {"apiKey": "key-canary", "secret": "secret-canary", "passphrase": "pass-canary"}


def binding(**overrides: Any):
    snapshot = load_protocol_snapshot(version=POLYMARKET_PILOT_PROTOCOL_VERSION)
    bound = bind_account_signature(
        snapshot,
        signer_address=SIGNER_ADDRESS,
        funder_address=FUNDER_ADDRESS,
        signature_type=0,
        negative_risk=False,
        credential_route_hash=CREDENTIAL_ROUTE_SET_HASH,
    )
    if overrides:
        return bound.model_copy(update=overrides)
    return bound


def client(handler: httpx.MockTransport) -> HttpxCredentialClient:
    return HttpxCredentialClient._for_test(
        private_key=PRIVATE_KEY,
        timestamp=lambda: TIMESTAMP,
        transport=handler,
    )


def test_production_httpx_client_uses_the_fixed_timeout() -> None:
    with HttpxCredentialClient._new_client() as production:
        assert production.timeout.connect == REQUEST_TIMEOUT_SECONDS
        assert production.timeout.read == REQUEST_TIMEOUT_SECONDS
        assert production.timeout.write == REQUEST_TIMEOUT_SECONDS
        assert production.timeout.pool == REQUEST_TIMEOUT_SECONDS


def test_closing_the_client_destroys_its_signing_key_before_any_request() -> None:
    captured: list[httpx.Request] = []
    credential_client = client(responder(capture=captured))

    credential_client.close()

    with pytest.raises(CredentialTransportError) as raised:
        credential_client.create_or_derive(operation="CREATE", binding=binding())
    assert raised.value.code == "CREDENTIAL_SIGNING_FAILED"
    assert captured == []


def test_client_signs_with_owned_mutable_key_then_zeroizes_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handed_to_signer: list[bytearray] = []
    private_key = bytearray(PRIVATE_KEY)

    def sign_mutable_key(key: bytearray, timestamp: str, snapshot: object) -> str:
        del timestamp, snapshot
        handed_to_signer.append(key)
        return "0x" + "0" * 130

    monkeypatch.setattr(client_module, "sign_clob_auth", sign_mutable_key)
    credential_client = HttpxCredentialClient._for_test(
        private_key=private_key,
        timestamp=lambda: TIMESTAMP,
        transport=responder(),
    )

    buffers = credential_client.create_or_derive(operation="CREATE", binding=binding())
    for buffer in buffers.values():
        buffer.close()
    credential_client.close()

    assert handed_to_signer == [private_key]
    assert not any(private_key)


def responder(
    *, status: int = 200, payload: Any = None, capture: list[httpx.Request] | None = None
) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        if capture is not None:
            capture.append(request)
        body = CREDENTIALS if payload is None else payload
        return httpx.Response(status, json=body)

    return httpx.MockTransport(handle)


def test_a_create_call_signs_l1_headers_for_the_frozen_route() -> None:
    captured: list[httpx.Request] = []

    buffers = client(responder(capture=captured)).create_or_derive(
        operation="CREATE", binding=binding()
    )

    request = captured[0]
    assert str(request.url) == "https://clob.polymarket.com/auth/api-key"
    assert request.method == "POST"
    assert request.headers["POLY_ADDRESS"] == SIGNER_ADDRESS
    assert request.headers["POLY_TIMESTAMP"] == TIMESTAMP
    assert request.headers["POLY_NONCE"] == "0"
    assert request.headers["POLY_SIGNATURE"].startswith("0x")
    assert set(buffers) == {
        CLOB_API_KEY_ACCOUNT,
        CLOB_API_SECRET_ACCOUNT,
        CLOB_PASSPHRASE_ACCOUNT,
    }
    assert buffers[CLOB_API_KEY_ACCOUNT].use(lambda view: bytes(view)) == b"key-canary"
    for buffer in buffers.values():
        buffer.close()


def test_a_derive_call_uses_the_derive_route() -> None:
    captured: list[httpx.Request] = []

    buffers = client(responder(capture=captured)).create_or_derive(
        operation="DERIVE", binding=binding()
    )

    assert str(captured[0].url).endswith("/auth/derive-api-key")
    assert captured[0].method == "GET"
    for buffer in buffers.values():
        buffer.close()


def test_returned_values_never_become_strings_in_the_result() -> None:
    buffers = client(responder()).create_or_derive(operation="CREATE", binding=binding())

    for buffer in buffers.values():
        assert isinstance(buffer, SecretBuffer)
        assert "canary" not in repr(buffer)
        buffer.close()


@pytest.mark.parametrize(
    "payload",
    [
        {"apiKey": "key"},
        {"apiKey": "key", "secret": "s", "passphrase": ""},
        {"apiKey": 1, "secret": "s", "passphrase": "p"},
        ["not", "an", "object"],
    ],
)
def test_an_incomplete_response_yields_no_buffer(payload: Any) -> None:
    with pytest.raises(CredentialTransportError) as raised:
        client(responder(payload=payload)).create_or_derive(operation="CREATE", binding=binding())
    assert raised.value.code == "CREDENTIAL_RESPONSE_INVALID"


def test_a_rejected_request_never_leaks_the_response_body() -> None:
    with pytest.raises(CredentialTransportError) as raised:
        client(
            responder(status=403, payload={"error": "operator-identifying detail"})
        ).create_or_derive(operation="CREATE", binding=binding())

    assert raised.value.code == "CREDENTIAL_REQUEST_REJECTED"
    assert "operator-identifying" not in str(raised.value)


def test_a_transport_failure_is_a_stable_code() -> None:
    def explode(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused to 127.0.0.1:1", request=request)

    with pytest.raises(CredentialTransportError) as raised:
        client(httpx.MockTransport(explode)).create_or_derive(operation="CREATE", binding=binding())

    assert raised.value.code == "CREDENTIAL_TRANSPORT_UNAVAILABLE"
    assert "127.0.0.1" not in str(raised.value)


def test_an_oversize_response_is_refused() -> None:
    payload = {**CREDENTIALS, "padding": "x" * (MAXIMUM_RESPONSE_BYTES + 1)}

    with pytest.raises(CredentialTransportError) as raised:
        client(responder(payload=payload)).create_or_derive(operation="CREATE", binding=binding())

    assert raised.value.code == "CREDENTIAL_RESPONSE_INVALID"


def test_a_binding_from_another_checkpoint_is_refused() -> None:
    with pytest.raises(CredentialTransportError) as raised:
        client(responder()).create_or_derive(
            operation="CREATE",
            binding=binding(protocol_version="polymarket-clob-2026-08-25-v1"),
        )

    assert raised.value.code == "CREDENTIAL_PROTOCOL_MISMATCH"


def test_the_client_reaches_no_route_other_than_the_two_credential_routes() -> None:
    source = json.dumps(
        __import__("pathlib")
        .Path("src/polytrading/predictions/polymarket_execution/credential_client.py")
        .read_text(encoding="utf-8")
    )

    assert "/order" not in source
    assert "/cancel" not in source
    assert "heartbeat" not in source
