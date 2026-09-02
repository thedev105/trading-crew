from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from polytrading.predictions.polymarket_execution.secret_labels import (
    CLOB_API_KEY_ACCOUNT,
    CLOB_SERVICE,
    WALLET_PRIVATE_KEY_ACCOUNT,
)
from polytrading.predictions.polymarket_execution.secrets import SecretBuffer, SecretStoreError
from polytrading.predictions.polymarket_execution.systemd_credentials_linux import (
    MAXIMUM_ITEM_BYTES,
    SystemdCredentialSecretStore,
)


@pytest.fixture
def credential_directories(tmp_path: Path) -> tuple[Path, Path]:
    runtime = tmp_path / "runtime"
    encrypted = tmp_path / "encrypted"
    runtime.mkdir(mode=0o700)
    encrypted.mkdir(mode=0o700)
    return runtime, encrypted


@pytest.fixture
def store(
    credential_directories: tuple[Path, Path],
) -> Iterator[SystemdCredentialSecretStore]:
    runtime, encrypted = credential_directories
    opened = SystemdCredentialSecretStore(runtime, encrypted)
    try:
        yield opened
    finally:
        opened.close()


def write_runtime_credential(runtime: Path, account: str, value: bytes, mode: int = 0o400) -> Path:
    target = runtime / account
    target.write_bytes(value)
    target.chmod(mode)
    return target


@pytest.mark.parametrize("name", ["../wallet-private-key", "wallet-private-key/child"])
def test_runtime_store_refuses_path_traversal(
    name: str, store: SystemdCredentialSecretStore
) -> None:
    with pytest.raises(SecretStoreError) as raised:
        store.read_required(CLOB_SERVICE, name, "unlock")
    assert raised.value.code == "SECRET_LABEL_INVALID"


def test_runtime_store_reads_only_a_private_regular_file(
    credential_directories: tuple[Path, Path], store: SystemdCredentialSecretStore
) -> None:
    runtime, _ = credential_directories
    write_runtime_credential(runtime, WALLET_PRIVATE_KEY_ACCOUNT, b"00" * 32)

    value = store.read_required(CLOB_SERVICE, WALLET_PRIVATE_KEY_ACCOUNT, "unlock")
    try:
        assert isinstance(value, SecretBuffer)
        assert value.use(bytes) == bytes(32)
    finally:
        value.close()


@pytest.mark.parametrize("prefix", [b"", b"0x", b"0X"])
def test_runtime_store_normalizes_wallet_hex_without_changing_other_values(
    prefix: bytes,
    credential_directories: tuple[Path, Path],
    store: SystemdCredentialSecretStore,
) -> None:
    runtime, _ = credential_directories
    private_key = bytes(range(32))
    write_runtime_credential(
        runtime,
        WALLET_PRIVATE_KEY_ACCOUNT,
        prefix + private_key.hex().encode("ascii"),
    )

    value = store.read_required(CLOB_SERVICE, WALLET_PRIVATE_KEY_ACCOUNT, "unlock")
    try:
        assert value.use(bytes) == private_key
    finally:
        value.close()


def test_runtime_store_leaves_non_wallet_values_opaque(
    credential_directories: tuple[Path, Path], store: SystemdCredentialSecretStore
) -> None:
    runtime, _ = credential_directories
    write_runtime_credential(runtime, CLOB_API_KEY_ACCOUNT, b"00" * 32)

    value = store.read_required(CLOB_SERVICE, CLOB_API_KEY_ACCOUNT, "unlock")
    try:
        assert value.use(bytes) == b"00" * 32
    finally:
        value.close()


def test_runtime_store_refuses_a_symlinked_credential(
    credential_directories: tuple[Path, Path], store: SystemdCredentialSecretStore
) -> None:
    runtime, _ = credential_directories
    outside = runtime.parent / "outside"
    outside.write_bytes(b"secret-canary")
    (runtime / CLOB_API_KEY_ACCOUNT).symlink_to(outside)

    with pytest.raises(SecretStoreError) as raised:
        store.read_required(CLOB_SERVICE, CLOB_API_KEY_ACCOUNT, "unlock")
    assert raised.value.code == "SECRET_STORE_UNAVAILABLE"
    assert "secret-canary" not in str(raised.value)


@pytest.mark.parametrize("mode", [0o440, 0o404])
def test_runtime_store_refuses_group_or_world_readable_credentials(
    mode: int,
    credential_directories: tuple[Path, Path],
    store: SystemdCredentialSecretStore,
) -> None:
    runtime, _ = credential_directories
    write_runtime_credential(runtime, CLOB_API_KEY_ACCOUNT, b"secret-canary", mode)

    with pytest.raises(SecretStoreError) as raised:
        store.read_required(CLOB_SERVICE, CLOB_API_KEY_ACCOUNT, "unlock")
    assert raised.value.code == "SECRET_STORE_UNAVAILABLE"


def test_runtime_store_refuses_a_multiply_linked_credential(
    credential_directories: tuple[Path, Path], store: SystemdCredentialSecretStore
) -> None:
    runtime, _ = credential_directories
    source = write_runtime_credential(runtime, CLOB_API_KEY_ACCOUNT, b"secret-canary")
    os.link(source, runtime / "second-link")

    with pytest.raises(SecretStoreError) as raised:
        store.read_required(CLOB_SERVICE, CLOB_API_KEY_ACCOUNT, "unlock")
    assert raised.value.code == "SECRET_STORE_UNAVAILABLE"


def test_runtime_store_refuses_a_credential_owned_by_another_uid(
    credential_directories: tuple[Path, Path], store: SystemdCredentialSecretStore
) -> None:
    runtime, _ = credential_directories
    write_runtime_credential(runtime, CLOB_API_KEY_ACCOUNT, b"secret-canary")
    store._effective_uid = os.geteuid() + 1

    with pytest.raises(SecretStoreError) as raised:
        store.read_required(CLOB_SERVICE, CLOB_API_KEY_ACCOUNT, "unlock")
    assert raised.value.code == "SECRET_STORE_UNAVAILABLE"


@pytest.mark.parametrize("value", [b"", b"x" * (MAXIMUM_ITEM_BYTES + 1)])
def test_runtime_store_refuses_empty_or_oversize_values(
    value: bytes,
    credential_directories: tuple[Path, Path],
    store: SystemdCredentialSecretStore,
) -> None:
    runtime, _ = credential_directories
    write_runtime_credential(runtime, CLOB_API_KEY_ACCOUNT, value)

    with pytest.raises(SecretStoreError) as raised:
        store.read_required(CLOB_SERVICE, CLOB_API_KEY_ACCOUNT, "unlock")
    assert raised.value.code == "SECRET_VALUE_INVALID"


def test_runtime_store_reports_a_missing_item_with_a_stable_code(
    store: SystemdCredentialSecretStore,
) -> None:
    with pytest.raises(SecretStoreError) as raised:
        store.read_required(CLOB_SERVICE, CLOB_API_KEY_ACCOUNT, "unlock")
    assert raised.value.code == "SECRET_ITEM_MISSING"


@pytest.mark.parametrize("mode", [0o750, 0o707])
def test_runtime_store_refuses_an_unsafe_runtime_directory(tmp_path: Path, mode: int) -> None:
    runtime = tmp_path / "runtime"
    encrypted = tmp_path / "encrypted"
    runtime.mkdir(mode=mode)
    encrypted.mkdir(mode=0o700)

    with pytest.raises(SecretStoreError) as raised:
        SystemdCredentialSecretStore(runtime, encrypted)
    assert raised.value.code == "SECRET_STORE_UNAVAILABLE"


def test_runtime_store_refuses_a_symlinked_runtime_directory(tmp_path: Path) -> None:
    actual = tmp_path / "actual"
    actual.mkdir(mode=0o700)
    runtime = tmp_path / "runtime"
    runtime.symlink_to(actual, target_is_directory=True)

    with pytest.raises(SecretStoreError) as raised:
        SystemdCredentialSecretStore(runtime, tmp_path / "encrypted")
    assert raised.value.code == "SECRET_STORE_UNAVAILABLE"


def test_runtime_store_never_reveals_a_secret_canary(
    credential_directories: tuple[Path, Path],
    store: SystemdCredentialSecretStore,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime, _ = credential_directories
    canary = b"runtime-secret-canary"
    write_runtime_credential(runtime, CLOB_API_KEY_ACCOUNT, canary, 0o440)

    with pytest.raises(SecretStoreError) as raised:
        store.read_required(CLOB_SERVICE, CLOB_API_KEY_ACCOUNT, "unlock")

    captured = capsys.readouterr()
    assert canary.decode() not in repr(store)
    assert canary.decode() not in repr(raised.value)
    assert canary.decode() not in captured.out
    assert canary.decode() not in captured.err
