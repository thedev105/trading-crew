"""Inherited-descriptor secret loading with best-effort in-place zeroization."""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Final, Literal, Protocol

_DESCRIPTOR_HEADER_BYTES: Final = 4
_MAX_SECRET_BYTES: Final = 4096
_REDACTED: Final = "<redacted>"


class SecretBoundaryError(ValueError):
    """A context-free secret-boundary rejection identified by a stable code."""


def _zeroize(value: bytearray) -> None:
    try:
        for index in range(len(value)):
            value[index] = 0
    except (BufferError, TypeError, ValueError):
        return


class SecretMaterial:
    """Owned mutable secret buffers without generic copy or serialization surfaces."""

    __slots__ = ("_api_key", "_api_secret", "_passphrase", "_private_key", "_sealed")

    def __init__(
        self,
        private_key: bytearray,
        api_key: bytearray,
        api_secret: bytearray,
        passphrase: bytearray,
    ) -> None:
        values = (private_key, api_key, api_secret, passphrase)
        if (
            any(type(value) is not bytearray for value in values)
            or len(private_key) != 32
            or any(not 0 < len(value) <= _MAX_SECRET_BYTES for value in values[1:])
        ):
            raise SecretBoundaryError("SECRET_MATERIAL_INVALID") from None
        object.__setattr__(self, "_private_key", private_key)
        object.__setattr__(self, "_api_key", api_key)
        object.__setattr__(self, "_api_secret", api_secret)
        object.__setattr__(self, "_passphrase", passphrase)
        object.__setattr__(self, "_sealed", True)

    def __setattr__(self, name: str, value: object) -> None:
        if getattr(self, "_sealed", False):
            raise AttributeError("SECRET_MATERIAL_IMMUTABLE") from None
        object.__setattr__(self, name, value)

    @property
    def private_key(self) -> bytearray:
        return self._private_key

    @property
    def api_key(self) -> bytearray:
        return self._api_key

    @property
    def api_secret(self) -> bytearray:
        return self._api_secret

    @property
    def passphrase(self) -> bytearray:
        return self._passphrase

    def __repr__(self) -> str:
        return "SecretMaterial(<redacted>)"

    __str__ = __repr__

    def __copy__(self) -> object:
        raise SecretBoundaryError("SECRET_MATERIAL_NOT_COPYABLE") from None

    def __deepcopy__(self, memo: dict[int, object]) -> object:
        del memo
        raise SecretBoundaryError("SECRET_MATERIAL_NOT_COPYABLE") from None

    def __reduce__(self) -> object:
        raise SecretBoundaryError("SECRET_MATERIAL_NOT_SERIALIZABLE") from None

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise SecretBoundaryError("SECRET_MATERIAL_NOT_SERIALIZABLE") from None

    def __getstate__(self) -> object:
        raise SecretBoundaryError("SECRET_MATERIAL_NOT_SERIALIZABLE") from None

    def __eq__(self, other: object) -> bool:
        del other
        raise SecretBoundaryError("SECRET_MATERIAL_NOT_COMPARABLE") from None

    def __hash__(self) -> int:
        raise SecretBoundaryError("SECRET_MATERIAL_NOT_HASHABLE") from None

    def close(self) -> None:
        """Best-effort zeroize every original owned buffer; safe to call repeatedly."""
        for value in (self._private_key, self._api_key, self._api_secret, self._passphrase):
            _zeroize(value)


SecretStoreCode = Literal[
    "ABSTRACT_SECRET_STORE",
    "SECRET_LABEL_INVALID",
    "SECRET_ITEM_MISSING",
    "SECRET_ACCESS_DENIED",
    "SECRET_STORE_UNAVAILABLE",
    "SECRET_VALUE_INVALID",
    "SECRET_WRITE_FAILED",
    "SECRET_BUFFER_CLOSED",
]


class SecretStoreError(ValueError):
    """A secret-store rejection identified by a stable code and no secret content."""

    def __init__(self, code: SecretStoreCode) -> None:
        super().__init__(code)
        self.code = code


class SecretBuffer:
    """One owned, bounded, mutable secret value with no string or serialization surface."""

    __slots__ = ("_closed", "_value")

    def __init__(self, value: bytearray) -> None:
        if type(value) is not bytearray or not 0 < len(value) <= _MAX_SECRET_BYTES:
            raise SecretStoreError("SECRET_VALUE_INVALID") from None
        self._value = value
        self._closed = False

    @classmethod
    def from_bytes(cls, value: bytes | bytearray) -> SecretBuffer:
        if type(value) not in (bytes, bytearray):
            raise SecretStoreError("SECRET_VALUE_INVALID") from None
        return cls(bytearray(value))

    def __len__(self) -> int:
        return 0 if self._closed else len(self._value)

    @property
    def closed(self) -> bool:
        return self._closed

    def use(self, consumer: Callable[[memoryview], object]) -> object:
        """Expose the bytes to one consumer without ever materializing a copy or a string."""
        if self._closed:
            raise SecretStoreError("SECRET_BUFFER_CLOSED") from None
        view = memoryview(self._value)
        try:
            return consumer(view)
        finally:
            view.release()

    def copy(self) -> SecretBuffer:
        """Deliberate, explicit duplication for a second owner; ``__copy__`` stays forbidden."""
        if self._closed:
            raise SecretStoreError("SECRET_BUFFER_CLOSED") from None
        return SecretBuffer(bytearray(self._value))

    def close(self) -> None:
        _zeroize(self._value)
        self._closed = True

    def __repr__(self) -> str:
        return "SecretBuffer(<redacted>)"

    __str__ = __repr__

    def __copy__(self) -> object:
        raise SecretBoundaryError("SECRET_MATERIAL_NOT_COPYABLE") from None

    def __deepcopy__(self, memo: dict[int, object]) -> object:
        del memo
        raise SecretBoundaryError("SECRET_MATERIAL_NOT_COPYABLE") from None

    def __reduce__(self) -> object:
        raise SecretBoundaryError("SECRET_MATERIAL_NOT_SERIALIZABLE") from None

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise SecretBoundaryError("SECRET_MATERIAL_NOT_SERIALIZABLE") from None

    def __getstate__(self) -> object:
        raise SecretBoundaryError("SECRET_MATERIAL_NOT_SERIALIZABLE") from None

    def __eq__(self, other: object) -> bool:
        del other
        raise SecretBoundaryError("SECRET_MATERIAL_NOT_COMPARABLE") from None

    def __hash__(self) -> int:
        raise SecretBoundaryError("SECRET_MATERIAL_NOT_HASHABLE") from None


class SecretStore(Protocol):
    """The operating-system secret boundary. No method returns ``str``."""

    def read_required(self, service: str, account: str, prompt: str) -> SecretBuffer:
        raise SecretStoreError("ABSTRACT_SECRET_STORE")

    def write_protected(self, service: str, account: str, value: SecretBuffer) -> None:
        raise SecretStoreError("ABSTRACT_SECRET_STORE")

    def delete(self, service: str, account: str) -> None:
        raise SecretStoreError("ABSTRACT_SECRET_STORE")


class InMemorySecretStore:
    """A process-local store with the same label rules, for environments without a keychain.

    It never persists anything: closing it zeroizes every buffer it holds. Automated tests use it
    so no test can reach a real operating-system keychain.
    """

    __slots__ = ("_denied", "_items")

    def __init__(self, *, denied: frozenset[tuple[str, str]] = frozenset()) -> None:
        self._items: dict[tuple[str, str], SecretBuffer] = {}
        self._denied = denied

    def _key(self, service: str, account: str) -> tuple[str, str]:
        if (
            type(service) is not str
            or type(account) is not str
            or not 0 < len(service) <= 128
            or not 0 < len(account) <= 128
        ):
            raise SecretStoreError("SECRET_LABEL_INVALID") from None
        if (service, account) in self._denied:
            raise SecretStoreError("SECRET_ACCESS_DENIED") from None
        return (service, account)

    def read_required(self, service: str, account: str, prompt: str) -> SecretBuffer:
        del prompt
        stored = self._items.get(self._key(service, account))
        if stored is None:
            raise SecretStoreError("SECRET_ITEM_MISSING") from None
        return stored.copy()

    def write_protected(self, service: str, account: str, value: SecretBuffer) -> None:
        key = self._key(service, account)
        if type(value) is not SecretBuffer or value.closed:
            raise SecretStoreError("SECRET_VALUE_INVALID") from None
        previous = self._items.get(key)
        self._items[key] = value.copy()
        if previous is not None:
            previous.close()

    def delete(self, service: str, account: str) -> None:
        stored = self._items.pop(self._key(service, account), None)
        if stored is not None:
            stored.close()

    def close(self) -> None:
        for stored in self._items.values():
            stored.close()
        self._items.clear()


def redact_sensitive(value: object) -> str:
    """Return a constant marker without inspecting or formatting secret-bearing input."""
    del value
    return _REDACTED


def _disable_core_dumps() -> bool:
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except (AttributeError, ImportError, OSError, ValueError):
        return False
    return True


def _read_descriptor_exact(descriptor: int, size: int) -> bytearray:
    value = bytearray()
    while len(value) < size:
        read_failed = False
        try:
            chunk = os.read(descriptor, size - len(value))
        except OSError:
            read_failed = True
            chunk = b""
        if read_failed:
            _zeroize(value)
            raise SecretBoundaryError("SECRET_DESCRIPTOR_READ_FAILED") from None
        if not chunk:
            _zeroize(value)
            raise SecretBoundaryError("SECRET_DESCRIPTOR_TRUNCATED") from None
        value.extend(chunk)
        chunk = b""
    return value


def _read_secret_descriptor(descriptor: int) -> bytearray:
    length_raw = _read_descriptor_exact(descriptor, _DESCRIPTOR_HEADER_BYTES)
    length = int.from_bytes(length_raw, "big")
    _zeroize(length_raw)
    if length <= 0 or length > _MAX_SECRET_BYTES:
        raise SecretBoundaryError("SECRET_DESCRIPTOR_SIZE_INVALID") from None
    value = _read_descriptor_exact(descriptor, length)
    read_failed = False
    try:
        trailing = os.read(descriptor, 1)
    except OSError:
        read_failed = True
        trailing = b""
    if read_failed:
        _zeroize(value)
        raise SecretBoundaryError("SECRET_DESCRIPTOR_READ_FAILED") from None
    if trailing:
        _zeroize(value)
        raise SecretBoundaryError("SECRET_DESCRIPTOR_TRAILING_BYTES") from None
    return value


def read_secret_descriptors(
    private_key_fd: int,
    api_key_fd: int,
    api_secret_fd: int,
    passphrase_fd: int,
) -> SecretMaterial:
    """Read the fixed four-descriptor startup contract and close every descriptor."""
    descriptors = (private_key_fd, api_key_fd, api_secret_fd, passphrase_fd)
    closable_descriptors = tuple(
        dict.fromkeys(
            descriptor for descriptor in descriptors if type(descriptor) is int and descriptor >= 0
        )
    )
    loaded: list[bytearray] = []
    try:
        if len(closable_descriptors) != len(descriptors) or any(
            type(descriptor) is not int for descriptor in descriptors
        ):
            raise SecretBoundaryError("SECRET_DESCRIPTOR_INVALID") from None
        _disable_core_dumps()
        for descriptor in descriptors:
            loaded.append(_read_secret_descriptor(descriptor))
        if len(loaded[0]) != 32:
            raise SecretBoundaryError("SECRET_PRIVATE_KEY_SIZE_INVALID") from None
        return SecretMaterial(*loaded)
    except BaseException:
        for value in loaded:
            _zeroize(value)
        raise
    finally:
        for descriptor in closable_descriptors:
            try:
                os.close(descriptor)
            except OSError:
                continue


__all__ = [
    "InMemorySecretStore",
    "SecretBoundaryError",
    "SecretBuffer",
    "SecretMaterial",
    "SecretStore",
    "SecretStoreError",
    "read_secret_descriptors",
    "redact_sensitive",
]
