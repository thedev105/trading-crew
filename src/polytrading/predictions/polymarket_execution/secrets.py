"""Inherited-descriptor secret loading with best-effort in-place zeroization."""

from __future__ import annotations

import os
from typing import Final

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
    "SecretBoundaryError",
    "SecretMaterial",
    "read_secret_descriptors",
    "redact_sensitive",
]
