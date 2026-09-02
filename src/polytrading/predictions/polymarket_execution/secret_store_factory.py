"""Select the reviewed operating-system secret store for the pilot."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from polytrading.predictions.polymarket_execution.secrets import SecretStore, SecretStoreError

_ENCRYPTED_CREDENTIAL_DIRECTORY = Path("/var/lib/polytrading/credentials")
_SYSTEMD_RUNTIME_DIRECTORY_VARIABLE = "CREDENTIALS_DIRECTORY"
_CLEAN_CHILD_PATH = "/usr/bin:/bin"


def _open_macos_store(platform: str) -> SecretStore:
    from polytrading.predictions.polymarket_execution.keychain_macos import (
        MacOSKeychainSecretStore,
    )

    return MacOSKeychainSecretStore(platform=platform)


def _open_systemd_store(runtime: Path, encrypted: Path) -> SecretStore:
    from polytrading.predictions.polymarket_execution.systemd_credentials_linux import (
        SystemdCredentialSecretStore,
    )

    return SystemdCredentialSecretStore(
        runtime_directory=runtime,
        encrypted_directory=encrypted,
    )


def _systemd_credentials_directory() -> Path:
    configured = os.environ.get(_SYSTEMD_RUNTIME_DIRECTORY_VARIABLE)
    if not configured:
        raise SecretStoreError("SECRET_STORE_UNAVAILABLE") from None
    directory = Path(configured)
    if not directory.is_absolute():
        raise SecretStoreError("SECRET_STORE_UNAVAILABLE") from None
    return directory


def _credential_child_environment(*, platform: str = sys.platform) -> dict[str, str]:
    environment = {"PATH": _CLEAN_CHILD_PATH}
    if platform == "darwin":
        return environment
    if platform == "linux":
        environment[_SYSTEMD_RUNTIME_DIRECTORY_VARIABLE] = str(_systemd_credentials_directory())
        return environment
    raise SecretStoreError("SECRET_STORE_UNAVAILABLE") from None


def open_pilot_secret_store(*, platform: str = sys.platform) -> SecretStore:
    """Open the fixed pilot store for macOS or a systemd-managed Linux service."""
    if platform == "darwin":
        return _open_macos_store(platform)
    if platform == "linux":
        return _open_systemd_store(
            _systemd_credentials_directory(),
            _ENCRYPTED_CREDENTIAL_DIRECTORY,
        )
    raise SecretStoreError("SECRET_STORE_UNAVAILABLE") from None


__all__ = ["open_pilot_secret_store"]
