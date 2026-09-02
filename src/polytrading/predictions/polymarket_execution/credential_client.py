"""The one authenticated call the pilot may make outside order execution.

This client runs inside the signer process, signs a single ClobAuth message with the wallet key,
performs exactly one create-or-derive request against the frozen credential route, and hands the
returned fields back as owned buffers. It cannot reach any other route, and it never returns,
logs, or formats a credential value.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Final, Literal

import httpx

from polytrading.predictions.polymarket_execution.auth import ClobAuthError, sign_clob_auth
from polytrading.predictions.polymarket_execution.keychain_macos import (
    CLOB_API_KEY_ACCOUNT,
    CLOB_API_SECRET_ACCOUNT,
    CLOB_PASSPHRASE_ACCOUNT,
)
from polytrading.predictions.polymarket_execution.protocol import (
    POLYMARKET_PILOT_PROTOCOL_VERSION,
    AccountSignatureBinding,
    PolymarketProtocolSnapshot,
    load_protocol_snapshot,
)
from polytrading.predictions.polymarket_execution.secrets import SecretBuffer

MAXIMUM_RESPONSE_BYTES: Final = 8192
REQUEST_TIMEOUT_SECONDS: Final = 10.0
_RESPONSE_FIELDS: Final[Mapping[str, str]] = {
    "apiKey": CLOB_API_KEY_ACCOUNT,
    "secret": CLOB_API_SECRET_ACCOUNT,
    "passphrase": CLOB_PASSPHRASE_ACCOUNT,
}


class CredentialTransportError(ValueError):
    """A refused credential call, named by a stable code and carrying no response body."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _production_client() -> httpx.Client:
    """Construct the only production transport with its non-configurable timeout."""
    return httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS)


class HttpxCredentialClient:
    """One create-or-derive call, over the frozen route, with no other reachable surface."""

    __slots__ = ("_client_factory", "_closed", "_private_key", "_snapshot", "_timestamp")

    def __init__(
        self,
        *,
        private_key: bytes | bytearray,
        timestamp: Callable[[], str],
        _client_factory: Callable[[], httpx.Client] | None = None,
        snapshot: PolymarketProtocolSnapshot | None = None,
    ) -> None:
        self._private_key = (
            private_key if type(private_key) is bytearray else bytearray(private_key)
        )
        self._closed = False
        self._timestamp = timestamp
        self._snapshot = snapshot or load_protocol_snapshot(
            version=POLYMARKET_PILOT_PROTOCOL_VERSION
        )
        self._client_factory = _client_factory or _production_client

    def create_or_derive(
        self, *, operation: Literal["CREATE", "DERIVE"], binding: AccountSignatureBinding
    ) -> dict[str, SecretBuffer]:
        if self._closed:
            raise CredentialTransportError("CREDENTIAL_SIGNING_FAILED")
        route = (
            self._snapshot.routes.create_api_key
            if operation == "CREATE"
            else self._snapshot.routes.derive_api_key
        )
        if binding.protocol_version != self._snapshot.version:
            raise CredentialTransportError("CREDENTIAL_PROTOCOL_MISMATCH")
        timestamp = self._timestamp()
        try:
            signature = sign_clob_auth(bytes(self._private_key), timestamp, self._snapshot)
        except ClobAuthError as error:
            raise CredentialTransportError("CREDENTIAL_SIGNING_FAILED") from error
        headers = {
            "POLY_ADDRESS": binding.signer_address,
            "POLY_SIGNATURE": signature,
            "POLY_TIMESTAMP": timestamp,
            "POLY_NONCE": str(self._snapshot.authentication.clob_auth.default_nonce),
        }
        if tuple(headers) != self._snapshot.authentication.clob_auth.l1_headers:
            raise CredentialTransportError("CREDENTIAL_HEADERS_INVALID")

        payload = self._request(route.host, route.method, route.path, headers)
        return _buffers_from(payload)

    def close(self) -> None:
        """Best-effort zeroize the child-owned signing-key copy."""
        for index in range(len(self._private_key)):
            self._private_key[index] = 0
        self._closed = True

    def _request(
        self, host: str, method: str, path: str, headers: Mapping[str, str]
    ) -> Mapping[str, Any]:
        try:
            with self._client_factory() as client:
                response = client.request(method, f"{host}{path}", headers=dict(headers))
        except Exception as error:  # any transport failure, reduced to one stable code
            # The exception never carries the response body: only a stable code escapes.
            raise CredentialTransportError("CREDENTIAL_TRANSPORT_UNAVAILABLE") from error
        if response.status_code != 200:
            raise CredentialTransportError("CREDENTIAL_REQUEST_REJECTED")
        if len(response.content) > MAXIMUM_RESPONSE_BYTES:
            raise CredentialTransportError("CREDENTIAL_RESPONSE_INVALID")
        try:
            payload = response.json()
        except ValueError as error:
            raise CredentialTransportError("CREDENTIAL_RESPONSE_INVALID") from error
        if not isinstance(payload, dict):
            raise CredentialTransportError("CREDENTIAL_RESPONSE_INVALID")
        return payload


def _buffers_from(payload: Mapping[str, Any]) -> dict[str, SecretBuffer]:
    """Move every returned field straight into an owned buffer, or keep nothing at all."""

    buffers: dict[str, SecretBuffer] = {}
    try:
        for field, account in _RESPONSE_FIELDS.items():
            value = payload.get(field)
            if type(value) is not str or not value:
                raise CredentialTransportError("CREDENTIAL_RESPONSE_INVALID")
            buffers[account] = SecretBuffer.from_bytes(value.encode("utf-8"))
    except BaseException:
        for buffer in buffers.values():
            buffer.close()
        raise
    return buffers


__all__ = [
    "MAXIMUM_RESPONSE_BYTES",
    "CredentialTransportError",
    "HttpxCredentialClient",
]
