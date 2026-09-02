"""Strict offline ClobAuth and exact-byte L2 request authentication."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
from collections.abc import Iterator, Mapping
from contextlib import suppress
from typing import Final

from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils.exceptions import ValidationError as EthValidationError

from polytrading.predictions.polymarket_execution.protocol import PolymarketProtocolSnapshot

_EIP712_DOMAIN_FIELDS: Final = (
    {"name": "name", "type": "string"},
    {"name": "version", "type": "string"},
    {"name": "chainId", "type": "uint256"},
)
_L2_HEADER_NAMES: Final = (
    "POLY_ADDRESS",
    "POLY_SIGNATURE",
    "POLY_TIMESTAMP",
    "POLY_API_KEY",
    "POLY_PASSPHRASE",
)
_ASCII_UNSIGNED_INTEGER: Final = re.compile(r"[0-9]+")
_ASCII_METHOD: Final = re.compile(r"[A-Za-z]+")
_EVM_ADDRESS: Final = re.compile(r"0x[0-9a-fA-F]{40}")
_CANONICAL_URLSAFE_BASE64: Final = re.compile(
    rb"(?:[A-Za-z0-9_-]{4})*(?:[A-Za-z0-9_-]{2}==|[A-Za-z0-9_-]{3}=)?"
)
_MAX_HEADER_VALUE_BYTES: Final = 4096


class ClobAuthError(ValueError):
    """A stable authentication rejection that never reflects supplied material."""


class ClobCredentials:
    """Immutable secret-bearing CLOB credentials with no generic serialization surface."""

    __slots__ = ("_address", "_api_key", "_passphrase", "_sealed", "_secret")

    def __init__(
        self,
        *,
        address: str,
        api_key: bytes,
        secret: bytes,
        passphrase: bytes,
    ) -> None:
        validated_address = _validated_evm_address(address, "CREDENTIAL_ADDRESS_INVALID")
        validated_api_key = _validated_header_bytes(api_key, "CREDENTIAL_API_KEY_INVALID")
        validated_secret = _validated_secret(secret)
        validated_passphrase = _validated_header_bytes(
            passphrase,
            "CREDENTIAL_PASSPHRASE_INVALID",
        )
        object.__setattr__(self, "_address", validated_address)
        object.__setattr__(self, "_api_key", validated_api_key)
        object.__setattr__(self, "_secret", validated_secret)
        object.__setattr__(self, "_passphrase", validated_passphrase)
        object.__setattr__(self, "_sealed", True)

    def __setattr__(self, name: str, value: object) -> None:
        if getattr(self, "_sealed", False):
            raise AttributeError("CLOB_CREDENTIALS_IMMUTABLE")
        object.__setattr__(self, name, value)

    @property
    def address(self) -> str:
        return self._address

    @property
    def api_key(self) -> bytes:
        return self._api_key

    @property
    def secret(self) -> bytes:
        return self._secret

    @property
    def passphrase(self) -> bytes:
        return self._passphrase

    def __repr__(self) -> str:
        return "ClobCredentials(<redacted>)"

    __str__ = __repr__

    def __reduce__(self) -> object:
        raise ClobAuthError("CLOB_CREDENTIALS_NOT_SERIALIZABLE") from None

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise ClobAuthError("CLOB_CREDENTIALS_NOT_SERIALIZABLE") from None

    def __getstate__(self) -> object:
        raise ClobAuthError("CLOB_CREDENTIALS_NOT_SERIALIZABLE") from None


class L2AuthHeaders(Mapping[str, str]):
    """The exact five L2 headers with sanitized display behavior."""

    __slots__ = ("_headers", "_sealed")

    def __init__(
        self,
        *,
        address: str,
        signature: str,
        timestamp: str,
        api_key: str,
        passphrase: str,
    ) -> None:
        values = (
            _validated_evm_address(address, "L2_HEADER_ADDRESS_INVALID"),
            _validated_l2_signature(signature),
            _validated_ascii_integer(timestamp, "L2_HEADER_TIMESTAMP_INVALID"),
            _validated_header_text(api_key, "L2_HEADER_API_KEY_INVALID"),
            _validated_header_text(passphrase, "L2_HEADER_PASSPHRASE_INVALID"),
        )
        object.__setattr__(
            self,
            "_headers",
            tuple(zip(_L2_HEADER_NAMES, values, strict=True)),
        )
        object.__setattr__(self, "_sealed", True)

    def __setattr__(self, name: str, value: object) -> None:
        if getattr(self, "_sealed", False):
            raise AttributeError("L2_AUTH_HEADERS_IMMUTABLE")
        object.__setattr__(self, name, value)

    @property
    def signature(self) -> str:
        return self["POLY_SIGNATURE"]

    def __getitem__(self, key: str) -> str:
        for name, value in self._headers:
            if name == key:
                return value
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        return (name for name, _ in self._headers)

    def __len__(self) -> int:
        return len(self._headers)

    def __repr__(self) -> str:
        return "L2AuthHeaders(<redacted>)"

    __str__ = __repr__

    def __reduce__(self) -> object:
        raise ClobAuthError("L2_AUTH_HEADERS_NOT_SERIALIZABLE") from None

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise ClobAuthError("L2_AUTH_HEADERS_NOT_SERIALIZABLE") from None

    def __getstate__(self) -> object:
        raise ClobAuthError("L2_AUTH_HEADERS_NOT_SERIALIZABLE") from None


def _validated_evm_address(value: object, error_code: str) -> str:
    if type(value) is not str or _EVM_ADDRESS.fullmatch(value) is None:
        raise ClobAuthError(error_code) from None
    return value


def _validated_ascii_integer(value: object, error_code: str) -> str:
    if (
        type(value) is not str
        or not 0 < len(value) <= _MAX_HEADER_VALUE_BYTES
        or not value.isascii()
        or _ASCII_UNSIGNED_INTEGER.fullmatch(value) is None
    ):
        raise ClobAuthError(error_code) from None
    return value


def _validated_nonce(value: object) -> int:
    if type(value) is not int or not 0 <= value <= 2**256 - 1:
        raise ClobAuthError("CLOB_AUTH_NONCE_INVALID") from None
    return value


def _validated_header_bytes(value: object, error_code: str) -> bytes:
    if (
        type(value) is not bytes
        or not 0 < len(value) <= _MAX_HEADER_VALUE_BYTES
        or any(byte < 0x20 or byte > 0x7E for byte in value)
    ):
        raise ClobAuthError(error_code) from None
    return value


def _validated_header_text(value: object, error_code: str) -> str:
    if (
        type(value) is not str
        or not 0 < len(value) <= _MAX_HEADER_VALUE_BYTES
        or not value.isascii()
        or any(ord(character) < 0x20 or ord(character) > 0x7E for character in value)
    ):
        raise ClobAuthError(error_code) from None
    return value


def _validated_l2_signature(value: object) -> str:
    if type(value) is not str or len(value) != 44 or not value.isascii():
        raise ClobAuthError("L2_HEADER_SIGNATURE_INVALID") from None
    encoded = value.encode("ascii")
    if _CANONICAL_URLSAFE_BASE64.fullmatch(encoded) is None:
        raise ClobAuthError("L2_HEADER_SIGNATURE_INVALID") from None
    decoded: bytes | None = None
    with suppress(binascii.Error):
        decoded = base64.urlsafe_b64decode(encoded)
    if (
        decoded is None
        or len(decoded) != hashlib.sha256().digest_size
        or not hmac.compare_digest(base64.urlsafe_b64encode(decoded), encoded)
    ):
        raise ClobAuthError("L2_HEADER_SIGNATURE_INVALID") from None
    return value


def _decode_secret(value: object) -> bytes:
    if (
        type(value) is not bytes
        or not 0 < len(value) <= _MAX_HEADER_VALUE_BYTES
        or _CANONICAL_URLSAFE_BASE64.fullmatch(value) is None
    ):
        raise ClobAuthError("CREDENTIAL_SECRET_INVALID") from None
    decoded: bytes | None = None
    with suppress(binascii.Error):
        decoded = base64.urlsafe_b64decode(value)
    if (
        decoded is None
        or not decoded
        or not hmac.compare_digest(base64.urlsafe_b64encode(decoded), value)
    ):
        raise ClobAuthError("CREDENTIAL_SECRET_INVALID") from None
    return decoded


def _validated_secret(value: object) -> bytes:
    _decode_secret(value)
    return value  # type: ignore[return-value]


def _validated_snapshot(snapshot: object) -> PolymarketProtocolSnapshot:
    if not isinstance(snapshot, PolymarketProtocolSnapshot):
        raise ClobAuthError("PROTOCOL_SNAPSHOT_INVALID") from None
    return snapshot


def clob_auth_typed_data(
    address: str,
    timestamp: str,
    snapshot: PolymarketProtocolSnapshot,
    *,
    nonce: int = 0,
) -> dict[str, object]:
    """Build the exact frozen ClobAuth EIP-712 message without loading credentials."""
    checked_snapshot = _validated_snapshot(snapshot)
    checked_address = _validated_evm_address(address, "CLOB_AUTH_ADDRESS_INVALID")
    checked_timestamp = _validated_ascii_integer(timestamp, "CLOB_AUTH_TIMESTAMP_INVALID")
    checked_nonce = _validated_nonce(nonce)
    contract = checked_snapshot.authentication.clob_auth
    return {
        "types": {
            "EIP712Domain": [dict(field) for field in _EIP712_DOMAIN_FIELDS],
            contract.primary_type: [field.model_dump() for field in contract.fields],
        },
        "primaryType": contract.primary_type,
        "domain": contract.domain.model_dump(mode="json", by_alias=True),
        "message": {
            "address": checked_address,
            "timestamp": checked_timestamp,
            "nonce": checked_nonce,
            "message": contract.message,
        },
    }


def sign_clob_auth(
    private_key: bytes | bytearray,
    timestamp: str,
    snapshot: PolymarketProtocolSnapshot,
    *,
    nonce: int = 0,
) -> str:
    """Sign and self-recover one frozen ClobAuth message with eth-account."""
    if type(private_key) not in (bytes, bytearray) or len(private_key) != 32:
        raise ClobAuthError("PRIVATE_KEY_INVALID") from None
    account = None
    with suppress(ValueError):
        account = Account.from_key(private_key)
    if account is None:
        raise ClobAuthError("PRIVATE_KEY_INVALID") from None
    typed_data = clob_auth_typed_data(account.address, timestamp, snapshot, nonce=nonce)
    signed = None
    with suppress(ValueError, EthValidationError):
        signed = Account.sign_typed_data(private_key, full_message=typed_data)
    if signed is None:
        raise ClobAuthError("CLOB_AUTH_SIGNING_FAILED") from None
    signature = "0x" + bytes(signed.signature).hex()
    signable_message = None
    with suppress(ValueError, EthValidationError):
        signable_message = encode_typed_data(full_message=typed_data)
    if signable_message is None:
        raise ClobAuthError("CLOB_AUTH_SIGNING_FAILED") from None
    recovered: str | None = None
    with suppress(ValueError, EthValidationError):
        recovered = Account.recover_message(
            signable_message,
            signature=signature,
        )
    if recovered is None:
        raise ClobAuthError("CLOB_AUTH_SIGNING_FAILED") from None
    if type(recovered) is not str or recovered.casefold() != account.address.casefold():
        raise ClobAuthError("CLOB_AUTH_RECOVERY_FAILED") from None
    return signature


def l2_preimage(timestamp: str, method: str, route: str, body: bytes) -> bytes:
    """Return timestamp + normalized method + route + exact body bytes."""
    checked_timestamp = _validated_ascii_integer(timestamp, "L2_TIMESTAMP_INVALID")
    if type(method) is not str or not method.isascii() or _ASCII_METHOD.fullmatch(method) is None:
        raise ClobAuthError("L2_METHOD_INVALID") from None
    if (
        type(route) is not str
        or not route.isascii()
        or not route.startswith("/")
        or "?" in route
        or "#" in route
        or any(ord(character) < 0x21 or ord(character) > 0x7E for character in route)
    ):
        raise ClobAuthError("L2_ROUTE_INVALID") from None
    if type(body) is not bytes:
        raise ClobAuthError("L2_BODY_BYTES_REQUIRED") from None
    return (
        checked_timestamp.encode("ascii")
        + method.upper().encode("ascii")
        + route.encode("ascii")
        + body
    )


def sign_l2_request(
    credentials: ClobCredentials,
    *,
    timestamp: str,
    method: str,
    route: str,
    body: bytes,
) -> L2AuthHeaders:
    """Sign exact already-serialized bytes and emit only the five frozen headers."""
    if type(credentials) is not ClobCredentials:
        raise ClobAuthError("CLOB_CREDENTIALS_REQUIRED") from None
    preimage = l2_preimage(timestamp, method, route, body)
    digest = hmac.new(
        _decode_secret(credentials.secret),
        preimage,
        hashlib.sha256,
    ).digest()
    signature = base64.urlsafe_b64encode(digest).decode("ascii")
    return L2AuthHeaders(
        address=credentials.address,
        signature=signature,
        timestamp=timestamp,
        api_key=credentials.api_key.decode("ascii"),
        passphrase=credentials.passphrase.decode("ascii"),
    )


__all__ = [
    "ClobAuthError",
    "L2AuthHeaders",
    "clob_auth_typed_data",
    "l2_preimage",
]
