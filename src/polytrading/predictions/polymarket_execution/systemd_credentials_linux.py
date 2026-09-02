"""Narrow systemd credential store for the Ubuntu headless pilot."""

from __future__ import annotations

import os
import stat
import subprocess
from contextlib import suppress
from pathlib import Path
from threading import RLock
from typing import Final
from uuid import uuid4

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
    _zeroize,
)

MAXIMUM_ITEM_BYTES: Final = 4096
MAXIMUM_ENCRYPTED_ITEM_BYTES: Final = 65_536
_CREDENTIAL_ACCOUNTS: Final = frozenset(
    {CLOB_API_KEY_ACCOUNT, CLOB_API_SECRET_ACCOUNT, CLOB_PASSPHRASE_ACCOUNT}
)


class SystemdCredsRunner:
    """The fixed systemd-creds encryption operation used by credential ceremonies."""

    __slots__ = ()

    def encrypt(self, *, account: str, value: SecretBuffer) -> bytearray:
        if (
            account not in _CREDENTIAL_ACCOUNTS
            or type(value) is not SecretBuffer
            or value.closed
            or not 0 < len(value) <= MAXIMUM_ITEM_BYTES
        ):
            raise SecretStoreError("SECRET_VALUE_INVALID") from None

        def _run(secret: memoryview) -> subprocess.CompletedProcess[bytes]:
            return subprocess.run(
                [
                    "/usr/bin/systemd-creds",
                    "encrypt",
                    "--with-key=host",
                    f"--name={account}",
                    "-",
                    "-",
                ],
                input=secret,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env={"PATH": "/usr/bin:/bin"},
                check=False,
            )

        try:
            completed = value.use(_run)
        except (OSError, subprocess.SubprocessError, TypeError, ValueError):
            raise SecretStoreError("SECRET_WRITE_FAILED") from None
        if (
            type(completed) is not subprocess.CompletedProcess
            or completed.returncode != 0
            or type(completed.stdout) is not bytes
            or not 0 < len(completed.stdout) <= MAXIMUM_ENCRYPTED_ITEM_BYTES
        ):
            raise SecretStoreError("SECRET_WRITE_FAILED") from None
        return bytearray(completed.stdout)


class SystemdCredentialSecretStore:
    """Read systemd runtime credentials through a private directory descriptor."""

    __slots__ = (
        "_creations",
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
        self._runner = runner if runner is not None else SystemdCredsRunner()
        self._creations: dict[SecretCreation, tuple[int, int, int, int]] = {}
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

    def create_protected(self, service: str, account: str, value: SecretBuffer) -> SecretCreation:
        self._require_clob_label(service, account)
        if (
            type(value) is not SecretBuffer
            or value.closed
            or not 0 < len(value) <= MAXIMUM_ITEM_BYTES
        ):
            raise SecretStoreError("SECRET_VALUE_INVALID") from None
        with self._lock:
            encrypted_fd = self._open_encrypted_directory()
            try:
                self._require_absent(account, encrypted_fd)
                encrypted = self._runner.encrypt(account=account, value=value)
                try:
                    identity = self._publish_encrypted_new(account, encrypted, encrypted_fd)
                finally:
                    _zeroize(encrypted)
            finally:
                os.close(encrypted_fd)
            creation = SecretCreation(service=service, account=account, token=object())
            self._creations[creation] = identity
            return creation

    def delete_created(self, creation: SecretCreation) -> None:
        if type(creation) is not SecretCreation:
            raise SecretStoreError("SECRET_OWNERSHIP_LOST") from None
        with self._lock:
            identity = self._creations.get(creation)
            if identity is None:
                raise SecretStoreError("SECRET_OWNERSHIP_LOST") from None
            encrypted_fd = self._open_encrypted_directory()
            target = _encrypted_name(creation.account)
            try:
                try:
                    status = os.stat(target, dir_fd=encrypted_fd, follow_symlinks=False)
                except FileNotFoundError:
                    self._creations.pop(creation, None)
                    return
                observed = (status.st_dev, status.st_ino, status.st_size, status.st_ctime_ns)
                if not stat.S_ISREG(status.st_mode) or observed != identity:
                    raise SecretStoreError("SECRET_OWNERSHIP_LOST") from None
                try:
                    os.unlink(target, dir_fd=encrypted_fd)
                    os.fsync(encrypted_fd)
                except OSError:
                    raise SecretStoreError("SECRET_OWNERSHIP_LOST") from None
                self._creations.pop(creation, None)
            finally:
                os.close(encrypted_fd)

    def delete(self, service: str, account: str) -> None:
        self._require_label(service, account)
        raise SecretStoreError("SECRET_WRITE_FAILED") from None

    @staticmethod
    def _require_clob_label(service: str, account: str) -> None:
        if (
            type(service) is not str
            or type(account) is not str
            or service != CLOB_SERVICE
            or account not in _CREDENTIAL_ACCOUNTS
        ):
            raise SecretStoreError("SECRET_LABEL_INVALID") from None

    def _open_encrypted_directory(self) -> int:
        try:
            descriptor = os.open(
                self._encrypted_directory,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            )
            status = os.fstat(descriptor)
            if (
                not stat.S_ISDIR(status.st_mode)
                or status.st_uid != self._effective_uid
                or status.st_mode & 0o077
            ):
                raise SecretStoreError("SECRET_WRITE_FAILED") from None
            return descriptor
        except SecretStoreError:
            if "descriptor" in locals():
                os.close(descriptor)
            raise
        except (OSError, TypeError, ValueError):
            raise SecretStoreError("SECRET_WRITE_FAILED") from None

    def _require_absent(self, account: str, encrypted_fd: int) -> None:
        if _entry_exists(account, self._runtime_fd) or _entry_exists(
            _encrypted_name(account), encrypted_fd
        ):
            raise SecretStoreError("SECRET_ITEM_EXISTS") from None

    def _publish_encrypted_new(
        self, account: str, encrypted: bytearray, encrypted_fd: int
    ) -> tuple[int, int, int, int]:
        if not 0 < len(encrypted) <= MAXIMUM_ENCRYPTED_ITEM_BYTES:
            raise SecretStoreError("SECRET_WRITE_FAILED") from None
        temporary = f".{account}.{uuid4().hex}.tmp"
        target = _encrypted_name(account)
        temporary_fd = -1
        published_identity: tuple[int, int] | None = None
        try:
            temporary_fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=encrypted_fd,
            )
            _write_all(temporary_fd, encrypted)
            os.fsync(temporary_fd)
            temporary_status = os.fstat(temporary_fd)
            os.close(temporary_fd)
            temporary_fd = -1
            try:
                os.link(
                    temporary,
                    target,
                    src_dir_fd=encrypted_fd,
                    dst_dir_fd=encrypted_fd,
                    follow_symlinks=False,
                )
            except FileExistsError:
                raise SecretStoreError("SECRET_ITEM_EXISTS") from None
            published_identity = (temporary_status.st_dev, temporary_status.st_ino)
            os.unlink(temporary, dir_fd=encrypted_fd)
            published = os.stat(target, dir_fd=encrypted_fd, follow_symlinks=False)
            identity = (
                published.st_dev,
                published.st_ino,
                published.st_size,
                published.st_ctime_ns,
            )
            os.fsync(encrypted_fd)
            return identity
        except SecretStoreError:
            if published_identity is not None:
                _unlink_if_identity(encrypted_fd, target, published_identity)
            raise
        except (OSError, TypeError, ValueError):
            if published_identity is not None:
                _unlink_if_identity(encrypted_fd, target, published_identity)
            raise SecretStoreError("SECRET_WRITE_FAILED") from None
        finally:
            if temporary_fd >= 0:
                with suppress(OSError):
                    os.close(temporary_fd)
            with suppress(OSError):
                os.unlink(temporary, dir_fd=encrypted_fd)


def _encrypted_name(account: str) -> str:
    return f"{account}.cred"


def _entry_exists(name: str, directory_fd: int) -> bool:
    try:
        os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    except (OSError, TypeError, ValueError):
        raise SecretStoreError("SECRET_WRITE_FAILED") from None
    return True


def _write_all(descriptor: int, value: bytearray) -> None:
    view = memoryview(value)
    offset = 0
    try:
        while offset < len(view):
            count = os.writev(descriptor, [view[offset:]])
            if count <= 0:
                raise SecretStoreError("SECRET_WRITE_FAILED") from None
            offset += count
    finally:
        view.release()


def _unlink_if_identity(directory_fd: int, name: str, identity: tuple[int, int]) -> None:
    try:
        status = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if (status.st_dev, status.st_ino) == identity:
            os.unlink(name, dir_fd=directory_fd)
            os.fsync(directory_fd)
    except OSError:
        return


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


__all__ = [
    "MAXIMUM_ENCRYPTED_ITEM_BYTES",
    "MAXIMUM_ITEM_BYTES",
    "SystemdCredentialSecretStore",
    "SystemdCredsRunner",
]
