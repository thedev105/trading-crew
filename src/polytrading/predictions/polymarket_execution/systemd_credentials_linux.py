"""Narrow systemd credential store for the Ubuntu headless pilot."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from threading import RLock
from typing import Final, Protocol

from polytrading.predictions.polymarket_execution.secret_labels import (
    ALLOWED_ACCOUNTS,
    CLOB_SERVICE,
    WALLET_PRIVATE_KEY_ACCOUNT,
)
from polytrading.predictions.polymarket_execution.secrets import (
    SecretBuffer,
    SecretCreation,
    SecretStoreError,
    _zeroize,
)

MAXIMUM_ITEM_BYTES: Final = 4096


class SystemdCredsRunner(Protocol):
    """The fixed systemd-creds encryption operation used by credential ceremonies."""

    def encrypt(self, *, account: str, value: SecretBuffer) -> bytearray: ...


class SystemdCredentialSecretStore:
    """Read systemd runtime credentials through a private directory descriptor."""

    __slots__ = (
        "_effective_uid",
        "_encrypted_directory",
        "_lock",
        "_runner",
        "_runtime_fd",
    )

    def __init__(
        self,
        runtime_directory: Path,
        encrypted_directory: Path,
        *,
        runner: SystemdCredsRunner | None = None,
        effective_uid: int | None = None,
    ) -> None:
        if (
            not isinstance(runtime_directory, Path)
            or not isinstance(encrypted_directory, Path)
            or not runtime_directory.is_absolute()
            or not encrypted_directory.is_absolute()
        ):
            raise SecretStoreError("SECRET_STORE_UNAVAILABLE") from None
        self._effective_uid = os.geteuid() if effective_uid is None else effective_uid
        self._encrypted_directory = encrypted_directory
        self._runner = runner
        self._lock = RLock()
        self._runtime_fd = -1
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            descriptor = os.open(runtime_directory, flags)
            status = os.fstat(descriptor)
            if (
                not stat.S_ISDIR(status.st_mode)
                or status.st_uid != self._effective_uid
                or status.st_mode & 0o077
            ):
                raise SecretStoreError("SECRET_STORE_UNAVAILABLE") from None
        except SecretStoreError:
            if "descriptor" in locals():
                os.close(descriptor)
            raise
        except (OSError, TypeError, ValueError):
            raise SecretStoreError("SECRET_STORE_UNAVAILABLE") from None
        self._runtime_fd = descriptor

    def __repr__(self) -> str:
        return "SystemdCredentialSecretStore(<redacted>)"

    def close(self) -> None:
        """Close the retained runtime directory descriptor."""
        lock = getattr(self, "_lock", None)
        if lock is None:
            return
        with lock:
            descriptor = getattr(self, "_runtime_fd", -1)
            self._runtime_fd = -1
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                return

    def __del__(self) -> None:
        self.close()

    @staticmethod
    def _require_label(service: str, account: str) -> None:
        if (
            type(service) is not str
            or type(account) is not str
            or service != CLOB_SERVICE
            or account not in ALLOWED_ACCOUNTS
        ):
            raise SecretStoreError("SECRET_LABEL_INVALID") from None

    def read_required(self, service: str, account: str, prompt: str) -> SecretBuffer:
        """Read one private, fixed-label credential without following links."""
        self._require_label(service, account)
        if type(prompt) is not str or not prompt:
            raise SecretStoreError("SECRET_LABEL_INVALID") from None
        with self._lock:
            if self._runtime_fd < 0:
                raise SecretStoreError("SECRET_STORE_UNAVAILABLE") from None
            try:
                descriptor = os.open(
                    account,
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=self._runtime_fd,
                )
            except FileNotFoundError:
                raise SecretStoreError("SECRET_ITEM_MISSING") from None
            except (OSError, TypeError, ValueError):
                raise SecretStoreError("SECRET_STORE_UNAVAILABLE") from None
        try:
            status = os.fstat(descriptor)
            if (
                not stat.S_ISREG(status.st_mode)
                or status.st_nlink != 1
                or status.st_uid != self._effective_uid
                or status.st_mode & 0o077
            ):
                raise SecretStoreError("SECRET_STORE_UNAVAILABLE") from None
            value = _read_at_most(descriptor)
            if not value:
                raise SecretStoreError("SECRET_VALUE_INVALID") from None
            if account == WALLET_PRIVATE_KEY_ACCOUNT:
                value = _normalize_wallet_private_key(value)
            result = SecretBuffer(value)
            value = bytearray()
            return result
        except SecretStoreError:
            if "value" in locals():
                _zeroize(value)
            raise
        except (OSError, TypeError, ValueError):
            if "value" in locals():
                _zeroize(value)
            raise SecretStoreError("SECRET_STORE_UNAVAILABLE") from None
        finally:
            os.close(descriptor)

    def write_protected(self, service: str, account: str, value: SecretBuffer) -> None:
        del value
        self._require_label(service, account)
        raise SecretStoreError("SECRET_WRITE_FAILED") from None

    def create_protected(
        self, service: str, account: str, value: SecretBuffer
    ) -> SecretCreation:
        del value
        self._require_label(service, account)
        raise SecretStoreError("SECRET_WRITE_FAILED") from None

    def delete_created(self, creation: SecretCreation) -> None:
        del creation
        raise SecretStoreError("SECRET_OWNERSHIP_LOST") from None

    def delete(self, service: str, account: str) -> None:
        self._require_label(service, account)
        raise SecretStoreError("SECRET_WRITE_FAILED") from None


def _read_at_most(descriptor: int) -> bytearray:
    storage = bytearray(MAXIMUM_ITEM_BYTES + 1)
    view = memoryview(storage)
    offset = 0
    try:
        while offset < len(storage):
            count = os.readv(descriptor, [view[offset:]])
            if count == 0:
                break
            offset += count
        if offset > MAXIMUM_ITEM_BYTES:
            raise SecretStoreError("SECRET_VALUE_INVALID") from None
        return bytearray(view[:offset])
    finally:
        view.release()
        _zeroize(storage)


def _normalize_wallet_private_key(value: bytearray) -> bytearray:
    offset = 2 if len(value) == 66 and value[0] == ord("0") and value[1] in b"xX" else 0
    if len(value) - offset != 64:
        return value
    decoded = bytearray(32)
    for index in range(32):
        high = _hex_nibble(value[offset + index * 2])
        low = _hex_nibble(value[offset + index * 2 + 1])
        if high < 0 or low < 0:
            _zeroize(decoded)
            return value
        decoded[index] = (high << 4) | low
    _zeroize(value)
    return decoded


def _hex_nibble(value: int) -> int:
    if ord("0") <= value <= ord("9"):
        return value - ord("0")
    if ord("a") <= value <= ord("f"):
        return value - ord("a") + 10
    if ord("A") <= value <= ord("F"):
        return value - ord("A") + 10
    return -1


__all__ = ["MAXIMUM_ITEM_BYTES", "SystemdCredentialSecretStore", "SystemdCredsRunner"]
