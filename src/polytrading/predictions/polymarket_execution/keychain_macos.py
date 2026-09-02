"""A narrow macOS Keychain adapter for the signer process.

Only four legacy Security-framework entry points are bound, through ``ctypes``, inside the signer.
Nothing here shells out to ``security``, reads an environment variable, or lets a secret reach a
string: values move as :class:`SecretBuffer` and every OS status becomes a stable code.
"""

from __future__ import annotations

import binascii
import sys
from threading import RLock
from typing import Final, Protocol

from polytrading.predictions.polymarket_execution.secret_labels import (
    ALLOWED_ACCOUNTS,
    CLOB_API_KEY_ACCOUNT,
    CLOB_API_SECRET_ACCOUNT,
    CLOB_PASSPHRASE_ACCOUNT,
    CLOB_SERVICE,
    WALLET_PRIVATE_KEY_ACCOUNT,
)
from polytrading.predictions.polymarket_execution.secrets import (
    SecretBuffer,
    SecretCreation,
    SecretStoreError,
)

# The exact labels this pilot may address. An unknown label is refused before any OS call.
MAXIMUM_ITEM_BYTES: Final = 4096
_CREDENTIAL_ACCOUNTS: Final = frozenset(
    {CLOB_API_KEY_ACCOUNT, CLOB_API_SECRET_ACCOUNT, CLOB_PASSPHRASE_ACCOUNT}
)

# Security framework OSStatus values this adapter translates.
_ERR_SEC_SUCCESS: Final = 0
_ERR_SEC_ITEM_NOT_FOUND: Final = -25300
_ERR_SEC_DUPLICATE_ITEM: Final = -25299
_ERR_SEC_AUTH_FAILED: Final = -25293
_ERR_SEC_USER_CANCELED: Final = -128
_ERR_SEC_INTERACTION_NOT_ALLOWED: Final = -25308
_DENIED_STATUSES: Final = frozenset(
    {_ERR_SEC_AUTH_FAILED, _ERR_SEC_USER_CANCELED, _ERR_SEC_INTERACTION_NOT_ALLOWED}
)


class KeychainLibrary(Protocol):
    """The four Security-framework calls this adapter is allowed to make."""

    def find_generic_password(self, service: bytes, account: bytes) -> tuple[int, bytes]: ...

    def add_generic_password(self, service: bytes, account: bytes, value: bytes) -> int: ...

    def add_generic_password_item(
        self, service: bytes, account: bytes, value: bytes
    ) -> tuple[int, object | None]: ...

    def update_generic_password(self, service: bytes, account: bytes, value: bytes) -> int: ...

    def delete_generic_password(self, service: bytes, account: bytes) -> int: ...

    def delete_created_generic_password(self, item: object) -> int: ...


class CtypesKeychainLibrary:
    """The real binding. Constructed only on Darwin, only inside the signer process."""

    __slots__ = ("_security",)

    def __init__(self) -> None:
        import ctypes
        import ctypes.util

        path = ctypes.util.find_library("Security")
        if path is None:
            raise SecretStoreError("SECRET_STORE_UNAVAILABLE") from None
        self._security = ctypes.cdll.LoadLibrary(path)

    def find_generic_password(self, service: bytes, account: bytes) -> tuple[int, bytes]:
        import ctypes

        length = ctypes.c_uint32(0)
        data = ctypes.c_void_p()
        item = ctypes.c_void_p()
        status = int(
            self._security.SecKeychainFindGenericPassword(
                None,
                ctypes.c_uint32(len(service)),
                service,
                ctypes.c_uint32(len(account)),
                account,
                ctypes.byref(length),
                ctypes.byref(data),
                ctypes.byref(item),
            )
        )
        if status != _ERR_SEC_SUCCESS:
            return status, b""
        try:
            if length.value > MAXIMUM_ITEM_BYTES:
                return _ERR_SEC_SUCCESS, b""
            value = ctypes.string_at(data, length.value)
        finally:
            self._security.SecKeychainItemFreeContent(None, data)
        return status, value

    def add_generic_password(self, service: bytes, account: bytes, value: bytes) -> int:
        status, item = self.add_generic_password_item(service, account, value)
        if item is not None:
            self._security.CFRelease(item)
        return status

    def add_generic_password_item(
        self, service: bytes, account: bytes, value: bytes
    ) -> tuple[int, object | None]:
        import ctypes

        item = ctypes.c_void_p()
        status = int(
            self._security.SecKeychainAddGenericPassword(
                None,
                ctypes.c_uint32(len(service)),
                service,
                ctypes.c_uint32(len(account)),
                account,
                ctypes.c_uint32(len(value)),
                value,
                ctypes.byref(item),
            )
        )
        return status, item if status == _ERR_SEC_SUCCESS else None

    def update_generic_password(self, service: bytes, account: bytes, value: bytes) -> int:
        import ctypes

        item = ctypes.c_void_p()
        status = int(
            self._security.SecKeychainFindGenericPassword(
                None,
                ctypes.c_uint32(len(service)),
                service,
                ctypes.c_uint32(len(account)),
                account,
                None,
                None,
                ctypes.byref(item),
            )
        )
        if status != _ERR_SEC_SUCCESS:
            return status
        try:
            return int(
                self._security.SecKeychainItemModifyAttributesAndData(
                    item, None, ctypes.c_uint32(len(value)), value
                )
            )
        finally:
            self._security.CFRelease(item)

    def delete_generic_password(self, service: bytes, account: bytes) -> int:
        import ctypes

        item = ctypes.c_void_p()
        status = int(
            self._security.SecKeychainFindGenericPassword(
                None,
                ctypes.c_uint32(len(service)),
                service,
                ctypes.c_uint32(len(account)),
                account,
                None,
                None,
                ctypes.byref(item),
            )
        )
        if status != _ERR_SEC_SUCCESS:
            return status
        try:
            return int(self._security.SecKeychainItemDelete(item))
        finally:
            self._security.CFRelease(item)

    def delete_created_generic_password(self, item: object) -> int:
        import ctypes

        if type(item) is not ctypes.c_void_p:
            return -50
        try:
            return int(self._security.SecKeychainItemDelete(item))
        finally:
            self._security.CFRelease(item)


class MacOSKeychainSecretStore:
    """The pilot's operating-system secret boundary on macOS."""

    __slots__ = ("_creations", "_library", "_lock", "_service")

    def __init__(
        self,
        *,
        library: KeychainLibrary | None = None,
        platform: str = sys.platform,
        service: str = CLOB_SERVICE,
    ) -> None:
        if platform != "darwin":
            # Every other operating system fails closed until it has its own reviewed adapter.
            raise SecretStoreError("SECRET_STORE_UNAVAILABLE") from None
        if type(service) is not str or not 0 < len(service) <= 128:
            raise SecretStoreError("SECRET_LABEL_INVALID") from None
        self._service = service
        self._library = library if library is not None else CtypesKeychainLibrary()
        self._creations: dict[SecretCreation, object] = {}
        self._lock = RLock()

    def _labels(self, service: str, account: str) -> tuple[bytes, bytes]:
        if service != self._service or account not in ALLOWED_ACCOUNTS:
            raise SecretStoreError("SECRET_LABEL_INVALID") from None
        return service.encode("utf-8"), account.encode("utf-8")

    @staticmethod
    def _raise_for_status(status: int) -> None:
        if status == _ERR_SEC_SUCCESS:
            return
        if status == _ERR_SEC_ITEM_NOT_FOUND:
            raise SecretStoreError("SECRET_ITEM_MISSING") from None
        if status in _DENIED_STATUSES:
            raise SecretStoreError("SECRET_ACCESS_DENIED") from None
        raise SecretStoreError("SECRET_STORE_UNAVAILABLE") from None

    def read_required(self, service: str, account: str, prompt: str) -> SecretBuffer:
        """Read one item, prompting the operator; a missing or denied item is fatal."""
        if type(prompt) is not str or not prompt:
            raise SecretStoreError("SECRET_LABEL_INVALID") from None
        service_bytes, account_bytes = self._labels(service, account)
        status, value = self._library.find_generic_password(service_bytes, account_bytes)
        self._raise_for_status(status)
        if not value or len(value) > MAXIMUM_ITEM_BYTES:
            raise SecretStoreError("SECRET_VALUE_INVALID") from None
        if account == WALLET_PRIVATE_KEY_ACCOUNT:
            value = _normalize_wallet_private_key(value)
        buffer = SecretBuffer.from_bytes(value)
        value = b""
        return buffer

    def write_protected(self, service: str, account: str, value: SecretBuffer) -> None:
        """Create or replace one item, never widening the label set."""
        service_bytes, account_bytes = self._labels(service, account)
        if type(value) is not SecretBuffer or value.closed or len(value) > MAXIMUM_ITEM_BYTES:
            raise SecretStoreError("SECRET_VALUE_INVALID") from None

        def _store(view: memoryview) -> int:
            raw = bytes(view)
            status = self._library.add_generic_password(service_bytes, account_bytes, raw)
            if status == _ERR_SEC_DUPLICATE_ITEM:
                status = self._library.update_generic_password(service_bytes, account_bytes, raw)
            return status

        with self._lock:
            status = int(value.use(_store))  # type: ignore[arg-type]
            if status == _ERR_SEC_ITEM_NOT_FOUND:
                raise SecretStoreError("SECRET_WRITE_FAILED") from None
            self._raise_for_status(status)
            self._discard_creations(service, account)

    def create_protected(
        self, service: str, account: str, value: SecretBuffer
    ) -> SecretCreation:
        """Add one absent reviewed slot without updating an existing credential."""
        if service != CLOB_SERVICE or account not in _CREDENTIAL_ACCOUNTS:
            raise SecretStoreError("SECRET_LABEL_INVALID") from None
        service_bytes, account_bytes = self._labels(service, account)
        if type(value) is not SecretBuffer or value.closed or len(value) > MAXIMUM_ITEM_BYTES:
            raise SecretStoreError("SECRET_VALUE_INVALID") from None

        def _create(view: memoryview) -> tuple[int, object | None]:
            return self._library.add_generic_password_item(
                service_bytes, account_bytes, bytes(view)
            )

        with self._lock:
            status, item = value.use(_create)  # type: ignore[misc]
            if status == _ERR_SEC_DUPLICATE_ITEM:
                raise SecretStoreError("SECRET_ITEM_EXISTS") from None
            self._raise_for_status(status)
            if item is None:
                raise SecretStoreError("SECRET_STORE_UNAVAILABLE") from None
            creation = SecretCreation(service=service, account=account, token=object())
            self._creations[creation] = item
            return creation

    def delete_created(self, creation: SecretCreation) -> None:
        """Rollback only a credential slot created by this store instance."""
        if type(creation) is not SecretCreation:
            raise SecretStoreError("SECRET_OWNERSHIP_LOST") from None
        with self._lock:
            item = self._creations.get(creation)
            if item is None:
                raise SecretStoreError("SECRET_OWNERSHIP_LOST") from None
            status = self._library.delete_created_generic_password(item)
            if status == _ERR_SEC_ITEM_NOT_FOUND:
                self._creations.pop(creation, None)
                return
            self._raise_for_status(status)
            self._creations.pop(creation, None)

    def delete(self, service: str, account: str) -> None:
        service_bytes, account_bytes = self._labels(service, account)
        with self._lock:
            status = self._library.delete_generic_password(service_bytes, account_bytes)
            if status == _ERR_SEC_ITEM_NOT_FOUND:
                self._discard_creations(service, account)
                return
            self._raise_for_status(status)
            self._discard_creations(service, account)

    def _discard_creations(self, service: str, account: str) -> None:
        for creation in tuple(self._creations):
            if creation.service == service and creation.account == account:
                self._creations.pop(creation, None)


def _normalize_wallet_private_key(value: bytes) -> bytes:
    """Decode the exact hexadecimal form used by the native Keychain UI."""
    encoded = value[2:] if value[:2] in (b"0x", b"0X") else value
    if len(encoded) != 64:
        return value
    try:
        decoded = binascii.unhexlify(encoded)
    except binascii.Error:
        return value
    return decoded


__all__ = [
    "ALLOWED_ACCOUNTS",
    "CLOB_API_KEY_ACCOUNT",
    "CLOB_API_SECRET_ACCOUNT",
    "CLOB_PASSPHRASE_ACCOUNT",
    "CLOB_SERVICE",
    "WALLET_PRIVATE_KEY_ACCOUNT",
    "CtypesKeychainLibrary",
    "KeychainLibrary",
    "MacOSKeychainSecretStore",
]
