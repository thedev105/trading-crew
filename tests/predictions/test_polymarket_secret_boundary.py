from __future__ import annotations

import copy
import json
import os
import pickle
from collections.abc import Callable
from dataclasses import asdict

import pytest

from polytrading.predictions.polymarket_execution.secrets import (
    SecretBoundaryError,
    SecretMaterial,
    read_secret_descriptors,
    redact_sensitive,
)

_SECRET_ASSERTION_FAILED = "SECRET_CANARY_DETECTED"


def _assert_canaries_absent(observed: str | bytes, *canaries: str | bytes) -> None:
    observed_bytes = observed if type(observed) is bytes else observed.encode("utf-8")
    for canary in canaries:
        canary_bytes = canary if type(canary) is bytes else canary.encode("utf-8")
        if canary_bytes in observed_bytes:
            raise AssertionError(_SECRET_ASSERTION_FAILED) from None


def _assert_context_free(error: BaseException) -> None:
    if error.__cause__ is not None or error.__context__ is not None:
        raise AssertionError("SECRET_ERROR_CHAIN_NOT_EMPTY") from None


def _secret_material() -> tuple[SecretMaterial, tuple[bytearray, ...]]:
    buffers = (
        bytearray(b"private-key-canary".ljust(32, b"!")),
        bytearray(b"api-key-canary"),
        bytearray(b"api-secret-canary"),
        bytearray(b"passphrase-canary"),
    )
    return SecretMaterial(*buffers), buffers


def _captured_boundary_error(operation: Callable[[], object]) -> SecretBoundaryError:
    captured: SecretBoundaryError | None = None
    try:
        operation()
    except SecretBoundaryError as error:
        captured = error
    if captured is None:
        raise AssertionError("SECRET_BOUNDARY_ERROR_NOT_RAISED") from None
    return captured


def _encoded_secret(value: bytes) -> bytes:
    return len(value).to_bytes(4, "big") + value


def _descriptor_for(value: bytes) -> int:
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, value)
    finally:
        os.close(write_fd)
    return read_fd


def test_secret_material_redacts_display_and_zeroizes_original_buffers_idempotently() -> None:
    material, buffers = _secret_material()
    canaries = tuple(bytes(value) for value in buffers)

    _assert_canaries_absent(repr(material) + str(material), *canaries)
    material.close()
    material.close()

    assert tuple(bytes(value) for value in buffers) == tuple(
        b"\x00" * len(value) for value in canaries
    )


@pytest.mark.parametrize(
    "operation",
    (
        pickle.dumps,
        copy.copy,
        copy.deepcopy,
        lambda value: value.__reduce__(),
        lambda value: value.__reduce_ex__(pickle.HIGHEST_PROTOCOL),
        lambda value: value.__getstate__(),
        lambda value: value == value,
        hash,
        asdict,
        json.dumps,
    ),
)
def test_secret_material_seals_generic_copy_state_equality_and_serialization(
    operation: Callable[[object], object],
) -> None:
    material, buffers = _secret_material()
    canaries = tuple(bytes(value) for value in buffers)

    with pytest.raises((SecretBoundaryError, TypeError)) as rejected:
        operation(material)

    _assert_canaries_absent(str(rejected.value) + repr(rejected.value), *canaries)
    _assert_context_free(rejected.value)
    material.close()


def test_descriptor_loader_reads_fixed_order_closes_fds_and_preserves_owned_buffers() -> None:
    values = (b"k" * 32, b"api-key", b"YXBpLXNlY3JldA==", b"passphrase")
    descriptors = tuple(_descriptor_for(_encoded_secret(value)) for value in values)

    material = read_secret_descriptors(*descriptors)

    assert (
        tuple(
            bytes(value)
            for value in (
                material.private_key,
                material.api_key,
                material.api_secret,
                material.passphrase,
            )
        )
        == values
    )
    for descriptor in descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)
    material.close()


def test_descriptor_loader_closes_every_fd_when_one_descriptor_is_truncated() -> None:
    descriptors = (
        _descriptor_for(_encoded_secret(b"k" * 32)),
        _descriptor_for(b"\x00\x00\x00\x08short"),
        _descriptor_for(_encoded_secret(b"YXBpLXNlY3JldA==")),
        _descriptor_for(_encoded_secret(b"passphrase")),
    )

    error = _captured_boundary_error(lambda: read_secret_descriptors(*descriptors))

    assert str(error) == "SECRET_DESCRIPTOR_TRUNCATED"
    _assert_context_free(error)
    for descriptor in descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)


@pytest.mark.parametrize(
    ("invalid_wire", "error_code"),
    (
        (b"\x00\x00\x00\x00", "SECRET_DESCRIPTOR_SIZE_INVALID"),
        ((4097).to_bytes(4, "big"), "SECRET_DESCRIPTOR_SIZE_INVALID"),
        (_encoded_secret(b"api-key") + b"x", "SECRET_DESCRIPTOR_TRAILING_BYTES"),
    ),
    ids=("empty", "oversized", "trailing"),
)
def test_descriptor_loader_rejects_invalid_secret_frames_and_closes_every_fd(
    invalid_wire: bytes,
    error_code: str,
) -> None:
    descriptors = (
        _descriptor_for(_encoded_secret(b"k" * 32)),
        _descriptor_for(invalid_wire),
        _descriptor_for(_encoded_secret(b"YXBpLXNlY3JldA==")),
        _descriptor_for(_encoded_secret(b"passphrase")),
    )

    error = _captured_boundary_error(lambda: read_secret_descriptors(*descriptors))

    assert str(error) == error_code
    _assert_context_free(error)
    for descriptor in descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)


@pytest.mark.parametrize("private_key_size", (31, 33))
def test_descriptor_loader_requires_an_exact_32_byte_private_key(
    private_key_size: int,
) -> None:
    descriptors = (
        _descriptor_for(_encoded_secret(b"k" * private_key_size)),
        _descriptor_for(_encoded_secret(b"api-key")),
        _descriptor_for(_encoded_secret(b"YXBpLXNlY3JldA==")),
        _descriptor_for(_encoded_secret(b"passphrase")),
    )

    error = _captured_boundary_error(lambda: read_secret_descriptors(*descriptors))

    assert str(error) == "SECRET_PRIVATE_KEY_SIZE_INVALID"
    for descriptor in descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)


@pytest.mark.parametrize(
    "values",
    (
        (bytearray(b"k" * 31), bytearray(b"a"), bytearray(b"s"), bytearray(b"p")),
        (bytearray(b"k" * 32), bytearray(), bytearray(b"s"), bytearray(b"p")),
        (
            bytearray(b"k" * 32),
            bytearray(b"a" * 4097),
            bytearray(b"s"),
            bytearray(b"p"),
        ),
    ),
    ids=("private-key-size", "empty", "oversized"),
)
def test_secret_material_constructor_enforces_the_descriptor_size_contract(
    values: tuple[bytearray, ...],
) -> None:
    error = _captured_boundary_error(lambda: SecretMaterial(*values))

    assert str(error) == "SECRET_MATERIAL_INVALID"


@pytest.mark.parametrize("invalid_kind", ("negative", "duplicate"))
def test_invalid_descriptor_arguments_still_close_every_valid_descriptor(
    invalid_kind: str,
) -> None:
    valid = tuple(_descriptor_for(_encoded_secret(b"x")) for _ in range(3))
    descriptors = (
        (valid[0], valid[1], valid[2], -1)
        if invalid_kind == "negative"
        else (valid[0], valid[1], valid[2], valid[2])
    )

    error = _captured_boundary_error(lambda: read_secret_descriptors(*descriptors))

    assert str(error) == "SECRET_DESCRIPTOR_INVALID"
    for descriptor in valid:
        with pytest.raises(OSError):
            os.fstat(descriptor)


def test_descriptor_os_error_is_sanitized_and_context_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    descriptors = tuple(_descriptor_for(_encoded_secret(b"x")) for _ in range(4))

    def fail_read(descriptor: int, size: int) -> bytes:
        del descriptor, size
        raise OSError("descriptor-io-canary")

    monkeypatch.setattr(os, "read", fail_read)
    error = _captured_boundary_error(lambda: read_secret_descriptors(*descriptors))

    assert str(error) == "SECRET_DESCRIPTOR_READ_FAILED"
    _assert_canaries_absent(str(error) + repr(error), "descriptor-io-canary")
    _assert_context_free(error)


def test_redaction_is_constant_and_never_reflects_supplied_material() -> None:
    canary = "redaction-input-canary"

    rendered = redact_sensitive(canary)

    assert rendered == "<redacted>"
    _assert_canaries_absent(rendered, canary)
