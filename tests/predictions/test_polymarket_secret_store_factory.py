from __future__ import annotations

from pathlib import Path

import pytest

from polytrading.predictions.polymarket_execution import (
    keychain_macos,
    secret_labels,
    secret_store_factory,
)
from polytrading.predictions.polymarket_execution.secrets import SecretStoreError


def test_factory_selects_linux_store_with_only_fixed_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = object()
    observed: list[tuple[Path, Path]] = []
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", "/run/credentials/test")
    monkeypatch.setattr(
        secret_store_factory,
        "_open_systemd_store",
        lambda runtime, encrypted: observed.append((runtime, encrypted)) or expected,
    )

    assert secret_store_factory.open_pilot_secret_store(platform="linux") is expected
    assert observed == [(Path("/run/credentials/test"), Path("/var/lib/polytrading/credentials"))]


@pytest.mark.parametrize("value", [None, "", "relative/credentials"])
def test_linux_factory_requires_an_absolute_systemd_runtime_directory(
    monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    if value is None:
        monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
    else:
        monkeypatch.setenv("CREDENTIALS_DIRECTORY", value)

    with pytest.raises(SecretStoreError) as raised:
        secret_store_factory.open_pilot_secret_store(platform="linux")

    assert raised.value.code == "SECRET_STORE_UNAVAILABLE"
    assert not value or value not in str(raised.value)


def test_factory_refuses_unknown_platforms() -> None:
    with pytest.raises(SecretStoreError) as raised:
        secret_store_factory.open_pilot_secret_store(platform="freebsd")
    assert raised.value.code == "SECRET_STORE_UNAVAILABLE"


def test_keychain_reexports_the_canonical_fixed_labels() -> None:
    assert keychain_macos.CLOB_SERVICE == secret_labels.CLOB_SERVICE
    assert keychain_macos.WALLET_PRIVATE_KEY_ACCOUNT == secret_labels.WALLET_PRIVATE_KEY_ACCOUNT
    assert keychain_macos.CLOB_API_KEY_ACCOUNT == secret_labels.CLOB_API_KEY_ACCOUNT
    assert keychain_macos.CLOB_API_SECRET_ACCOUNT == secret_labels.CLOB_API_SECRET_ACCOUNT
    assert keychain_macos.CLOB_PASSPHRASE_ACCOUNT == secret_labels.CLOB_PASSPHRASE_ACCOUNT
    assert keychain_macos.ALLOWED_ACCOUNTS is secret_labels.ALLOWED_ACCOUNTS


def test_linux_clean_child_environment_carries_only_path_and_credential_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", "/run/credentials/test")
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "must-not-propagate")

    assert secret_store_factory._credential_child_environment(platform="linux") == {
        "PATH": "/usr/bin:/bin",
        "CREDENTIALS_DIRECTORY": "/run/credentials/test",
    }
