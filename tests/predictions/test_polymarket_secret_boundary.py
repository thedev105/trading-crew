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
        (b"\x00\x00\x00\x00", "SECRET_MATERIAL_INVALID"),
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
    ids=("private-key-size", "partial-clob", "oversized"),
)
def test_secret_material_constructor_enforces_the_descriptor_size_contract(
    values: tuple[bytearray, ...],
) -> None:
    error = _captured_boundary_error(lambda: SecretMaterial(*values))

    assert str(error) == "SECRET_MATERIAL_INVALID"


def test_secret_material_accepts_an_all_empty_clob_trio() -> None:
    material = SecretMaterial(bytearray(32), bytearray(), bytearray(), bytearray())

    try:
        assert material.credentials_present is False
        assert len(material.api_key) == 0
    finally:
        material.close()


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


# -- credential ceremony canaries -----------------------------------------------------------

CREDENTIAL_CANARY = b"clob-credential-canary-do-not-leak"


def _credential_setup() -> tuple[object, object, object, object]:
    from datetime import UTC, datetime, timedelta
    from hashlib import sha256
    from uuid import UUID

    from polytrading.predictions.polymarket_execution.credentials import (
        CredentialProvisioningGrant,
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
    from polytrading.predictions.polymarket_execution.secrets import (
        InMemorySecretStore,
        SecretBuffer,
    )

    now = datetime(2026, 8, 29, 12, tzinfo=UTC)
    signer_address = "0x7E5F4552091A69125d5DfCb7b8C2659029395Bdf"
    funder_address = "0x" + "11" * 20
    snapshot = load_protocol_snapshot(version=POLYMARKET_PILOT_PROTOCOL_VERSION)
    binding = bind_account_signature(
        snapshot,
        signer_address=signer_address,
        funder_address=funder_address,
        signature_type=0,
        negative_risk=False,
        credential_route_hash=CREDENTIAL_ROUTE_SET_HASH,
    )
    grant = CredentialProvisioningGrant(
        grant_id=UUID("00000000-0000-0000-0000-0000000f0002"),
        grant_kind="CREDENTIAL_PROVISIONING",
        operation="CREATE",
        wallet_fingerprint=sha256(funder_address.casefold().encode("ascii")).hexdigest(),
        account_fingerprint=sha256(signer_address.casefold().encode("ascii")).hexdigest(),
        protocol_version=POLYMARKET_PILOT_PROTOCOL_VERSION,
        grant_digest="a" * 64,
        issued_at=now,
        expires_at=now + timedelta(seconds=60),
    )

    class CanaryClient:
        def create_or_derive(self, *, operation: str, binding: object) -> dict[str, SecretBuffer]:
            del operation, binding
            return {
                CLOB_API_KEY_ACCOUNT: SecretBuffer.from_bytes(CREDENTIAL_CANARY),
                CLOB_API_SECRET_ACCOUNT: SecretBuffer.from_bytes(CREDENTIAL_CANARY + b"-secret"),
                CLOB_PASSPHRASE_ACCOUNT: SecretBuffer.from_bytes(CREDENTIAL_CANARY + b"-pass"),
            }

    return grant, binding, InMemorySecretStore(), CanaryClient()


def test_a_credential_ceremony_never_emits_its_canary_to_any_observable_surface(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from polytrading.predictions.polymarket_execution.credentials import provision_credentials

    grant, binding, store, client = _credential_setup()
    result = provision_credentials(
        grant,
        binding,
        store=store,
        client=client,
        now=grant.issued_at,  # type: ignore[arg-type]
    )

    captured = capsys.readouterr()
    canary = CREDENTIAL_CANARY.decode()
    for surface in (
        repr(result),
        str(result),
        json.dumps(asdict(result)),
        repr(grant),
        json.dumps(asdict(grant), default=str),
        captured.out,
        captured.err,
    ):
        assert canary not in surface


def test_a_failed_credential_ceremony_raises_a_code_without_its_canary() -> None:
    from polytrading.predictions.polymarket_execution.credentials import (
        CredentialProvisioningError,
        provision_credentials,
    )
    from polytrading.predictions.polymarket_execution.keychain_macos import CLOB_API_KEY_ACCOUNT
    from polytrading.predictions.polymarket_execution.secrets import (
        SecretBuffer,
        SecretStoreError,
    )

    grant, binding, _unused_store, client = _credential_setup()

    class RefusingStore:
        def read_required(self, service: str, account: str, prompt: str) -> SecretBuffer:
            raise SecretStoreError("SECRET_ITEM_MISSING")

        def write_protected(self, service: str, account: str, value: SecretBuffer) -> None:
            del service, value
            if account == CLOB_API_KEY_ACCOUNT:
                raise SecretStoreError("SECRET_WRITE_FAILED")

        def delete(self, service: str, account: str) -> None:
            del service, account

    with pytest.raises(CredentialProvisioningError) as raised:
        provision_credentials(
            grant,
            binding,  # type: ignore[arg-type]
            store=RefusingStore(),
            client=client,
            now=grant.issued_at,
        )

    rendered = f"{raised.value!r}{raised.value!s}{raised.traceback!r}"
    assert raised.value.code == "CREDENTIAL_STORE_FAILED"
    assert CREDENTIAL_CANARY.decode() not in rendered


def test_secret_buffers_never_reach_pickle_copy_or_json() -> None:
    from polytrading.predictions.polymarket_execution.secrets import SecretBuffer

    buffer = SecretBuffer.from_bytes(CREDENTIAL_CANARY)
    for operation in (
        lambda: pickle.dumps(buffer),
        lambda: copy.copy(buffer),
        lambda: copy.deepcopy(buffer),
    ):
        with pytest.raises(ValueError):
            operation()
    with pytest.raises(TypeError):
        json.dumps(buffer)  # type: ignore[arg-type]
    assert CREDENTIAL_CANARY.decode() not in repr(buffer)
