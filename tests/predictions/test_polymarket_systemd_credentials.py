from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from polytrading.predictions.polymarket_execution.secret_labels import (
    CLOB_API_KEY_ACCOUNT,
    CLOB_API_SECRET_ACCOUNT,
    CLOB_PASSPHRASE_ACCOUNT,
    CLOB_SERVICE,
    WALLET_PRIVATE_KEY_ACCOUNT,
)
from polytrading.predictions.polymarket_execution.secrets import SecretBuffer, SecretStoreError
from polytrading.predictions.polymarket_execution.systemd_credentials_linux import (
    MAXIMUM_ITEM_BYTES,
    SystemdCredentialSecretStore,
    SystemdCredsRunner,
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


class FakeSystemdCredsRunner:
    def __init__(self, encrypted_output: bytes = b"encrypted-systemd-credential") -> None:
        self.calls: list[tuple[str, bytes]] = []
        self.encrypted_output = encrypted_output

    def encrypt(self, *, account: str, value: SecretBuffer) -> bytearray:
        self.calls.append((account, value.use(bytes)))
        return bytearray(self.encrypted_output)


def writable_store(
    directories: tuple[Path, Path], runner: FakeSystemdCredsRunner
) -> SystemdCredentialSecretStore:
    runtime, encrypted = directories
    return SystemdCredentialSecretStore(runtime, encrypted, runner=runner)


def test_systemd_creds_runner_uses_only_fixed_process_channels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], bytes, object, object, dict[str, str], bool]] = []
    canary = b"runner-secret-canary"

    def fake_run(
        arguments: list[str],
        *,
        input: memoryview,
        stdout: object,
        stderr: object,
        env: dict[str, str],
        check: bool,
    ) -> subprocess.CompletedProcess[bytes]:
        calls.append((arguments, bytes(input), stdout, stderr, env, check))
        return subprocess.CompletedProcess(arguments, 0, b"encrypted-output", b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    value = SecretBuffer.from_bytes(canary)
    try:
        encrypted = SystemdCredsRunner().encrypt(account=CLOB_API_KEY_ACCOUNT, value=value)
    finally:
        value.close()

    assert encrypted == bytearray(b"encrypted-output")
    assert calls == [
        (
            [
                "/usr/bin/systemd-creds",
                "encrypt",
                "--with-key=host",
                f"--name={CLOB_API_KEY_ACCOUNT}",
                "-",
                "-",
            ],
            canary,
            subprocess.PIPE,
            subprocess.DEVNULL,
            {"PATH": "/usr/bin:/bin"},
            False,
        )
    ]
    assert canary.decode() not in repr(calls[0][0])
    assert canary.decode() not in repr(calls[0][4])


def test_create_protected_encrypts_a_new_fixed_clob_slot(
    credential_directories: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    runner = FakeSystemdCredsRunner()
    opened = writable_store(credential_directories, runner)
    canary = b"clob-secret-canary"
    value = SecretBuffer.from_bytes(canary)
    try:
        creation = opened.create_protected(CLOB_SERVICE, CLOB_API_SECRET_ACCOUNT, value)
    finally:
        value.close()
        opened.close()

    _, encrypted = credential_directories
    target = encrypted / f"{CLOB_API_SECRET_ACCOUNT}.cred"
    assert runner.calls == [(CLOB_API_SECRET_ACCOUNT, canary)]
    assert target.read_bytes() == runner.encrypted_output
    assert target.stat().st_mode & 0o777 == 0o600
    assert canary.decode() not in repr(creation)
    captured = capsys.readouterr()
    assert canary.decode() not in captured.out
    assert canary.decode() not in captured.err


@pytest.mark.parametrize("location", ["runtime", "encrypted"])
def test_create_protected_refuses_an_existing_slot(
    location: str, credential_directories: tuple[Path, Path]
) -> None:
    runtime, encrypted = credential_directories
    if location == "runtime":
        write_runtime_credential(runtime, CLOB_API_KEY_ACCOUNT, b"existing")
    else:
        (encrypted / f"{CLOB_API_KEY_ACCOUNT}.cred").write_bytes(b"existing-encrypted")
    runner = FakeSystemdCredsRunner()
    opened = writable_store(credential_directories, runner)
    value = SecretBuffer.from_bytes(b"new")
    try:
        with pytest.raises(SecretStoreError) as raised:
            opened.create_protected(CLOB_SERVICE, CLOB_API_KEY_ACCOUNT, value)
    finally:
        value.close()
        opened.close()

    assert raised.value.code == "SECRET_ITEM_EXISTS"
    assert runner.calls == []


def test_create_protected_refuses_the_wallet_slot(
    credential_directories: tuple[Path, Path],
) -> None:
    opened = writable_store(credential_directories, FakeSystemdCredsRunner())
    value = SecretBuffer.from_bytes(b"wallet")
    try:
        with pytest.raises(SecretStoreError) as raised:
            opened.create_protected(CLOB_SERVICE, WALLET_PRIVATE_KEY_ACCOUNT, value)
    finally:
        value.close()
        opened.close()
    assert raised.value.code == "SECRET_LABEL_INVALID"


def test_delete_created_removes_only_its_own_encrypted_blob(
    credential_directories: tuple[Path, Path],
) -> None:
    opened = writable_store(credential_directories, FakeSystemdCredsRunner())
    value = SecretBuffer.from_bytes(b"key")
    try:
        creation = opened.create_protected(CLOB_SERVICE, CLOB_API_KEY_ACCOUNT, value)
        _, encrypted = credential_directories
        target = encrypted / f"{CLOB_API_KEY_ACCOUNT}.cred"
        target.unlink()
        target.write_bytes(b"external-encrypted-replacement")
        target.chmod(0o600)

        with pytest.raises(SecretStoreError) as raised:
            opened.delete_created(creation)
    finally:
        value.close()
        opened.close()

    assert raised.value.code == "SECRET_OWNERSHIP_LOST"
    assert target.read_bytes() == b"external-encrypted-replacement"


def test_delete_created_removes_the_unchanged_owned_blob(
    credential_directories: tuple[Path, Path],
) -> None:
    opened = writable_store(credential_directories, FakeSystemdCredsRunner())
    value = SecretBuffer.from_bytes(b"key")
    try:
        creation = opened.create_protected(CLOB_SERVICE, CLOB_API_KEY_ACCOUNT, value)
        opened.delete_created(creation)
    finally:
        value.close()
        opened.close()

    _, encrypted = credential_directories
    assert not (encrypted / f"{CLOB_API_KEY_ACCOUNT}.cred").exists()


@pytest.mark.parametrize("account", [CLOB_API_KEY_ACCOUNT, CLOB_PASSPHRASE_ACCOUNT])
def test_linux_store_refuses_rotation_and_arbitrary_deletion(
    account: str, credential_directories: tuple[Path, Path]
) -> None:
    opened = writable_store(credential_directories, FakeSystemdCredsRunner())
    value = SecretBuffer.from_bytes(b"value")
    try:
        for operation in (
            lambda: opened.write_protected(CLOB_SERVICE, account, value),
            lambda: opened.delete(CLOB_SERVICE, account),
        ):
            with pytest.raises(SecretStoreError) as raised:
                operation()
            assert raised.value.code == "SECRET_WRITE_FAILED"
    finally:
        value.close()
        opened.close()
