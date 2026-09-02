"""Secret-safe local status for the fixed Polymarket credential Keychain items."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Literal

from polytrading.predictions.polymarket_execution.credentials import CredentialFingerprint
from polytrading.predictions.polymarket_execution.keychain_macos import (
    CLOB_API_KEY_ACCOUNT,
    CLOB_API_SECRET_ACCOUNT,
    CLOB_PASSPHRASE_ACCOUNT,
    CLOB_SERVICE,
    WALLET_PRIVATE_KEY_ACCOUNT,
)
from polytrading.predictions.polymarket_execution.secrets import (
    SecretBuffer,
    SecretStore,
    SecretStoreError,
)

_CHECK_PROMPT: Final = "Unlock the Polymarket pilot wallet and credentials for this check"
_CREDENTIAL_ACCOUNTS: Final = (
    CLOB_API_KEY_ACCOUNT,
    CLOB_API_SECRET_ACCOUNT,
    CLOB_PASSPHRASE_ACCOUNT,
)


class CredentialCommandError(ValueError):
    """A stable public command failure with no exception text."""

    def __init__(self, code: str) -> None:
        super().__init__()
        self.code = code


@dataclass(frozen=True, slots=True)
class CredentialReadiness:
    wallet_ready: bool
    credentials_state: Literal["PRESENT", "ABSENT", "PARTIAL"]


def check_credential_readiness(store: SecretStore) -> CredentialReadiness:
    """Inspect only the reviewed local Keychain entries and return public status."""
    wallet_ready = _item_is_valid_wallet(store)
    present = tuple(_item_is_present(store, account) for account in _CREDENTIAL_ACCOUNTS)
    state: Literal["PRESENT", "ABSENT", "PARTIAL"]
    if all(present):
        state = "PRESENT"
    elif any(present):
        state = "PARTIAL"
    else:
        state = "ABSENT"
    return CredentialReadiness(wallet_ready=wallet_ready, credentials_state=state)


def render_credential_readiness(result: CredentialReadiness) -> str:
    """Render the fixed, secret-free operator-facing readiness status."""
    wallet_ready = "true" if result.wallet_ready else "false"
    return f"wallet_ready={wallet_ready}\ncredentials={result.credentials_state}"


def create_credentials(
    *,
    store: SecretStore,
    confirmed: bool,
    now: Callable[[], datetime] | None = None,
) -> CredentialFingerprint:
    """Run the explicitly confirmed absent-only create ceremony."""
    if confirmed is not True:
        raise CredentialCommandError("CONFIRMATION_REQUIRED")
    state = _credential_state(store)
    if state == "PRESENT":
        raise CredentialCommandError("CREDENTIALS_ALREADY_PRESENT")
    if state == "PARTIAL":
        raise CredentialCommandError("CREDENTIALS_PARTIAL")
    try:
        return _create_credentials_in_sidecar(store=store, now=now or _utc_now)
    except Exception as error:
        from polytrading.predictions.pilot.signer_bootstrap import SignerBootstrapError

        if not isinstance(error, SignerBootstrapError):
            raise
        code = (
            error.code
            if error.code
            in {
                "CREDENTIALS_ALREADY_PRESENT",
                "CREDENTIAL_CREATE_FAILED",
                "CREDENTIAL_STORE_FAILED",
            }
            else "SIGNER_BOOTSTRAP_FAILED"
        )
        raise CredentialCommandError(code) from None


def _credential_state(store: SecretStore) -> Literal["PRESENT", "ABSENT", "PARTIAL"]:
    present = tuple(_item_is_present(store, account) for account in _CREDENTIAL_ACCOUNTS)
    if all(present):
        return "PRESENT"
    if any(present):
        return "PARTIAL"
    return "ABSENT"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _create_credentials_in_sidecar(
    *, store: SecretStore, now: Callable[[], datetime]
) -> CredentialFingerprint:
    from polytrading.predictions.pilot.signer_bootstrap import create_credentials_in_sidecar

    return create_credentials_in_sidecar(store=store, now=now)


def _item_is_valid_wallet(store: SecretStore) -> bool:
    buffer = _read_required(store, WALLET_PRIVATE_KEY_ACCOUNT, wallet=True)
    try:
        return len(buffer) == 32
    finally:
        buffer.close()


def _item_is_present(store: SecretStore, account: str) -> bool:
    try:
        buffer = store.read_required(CLOB_SERVICE, account, _CHECK_PROMPT)
    except SecretStoreError as error:
        if error.code == "SECRET_ITEM_MISSING":
            return False
        _raise_public_error(error, wallet=False)
    try:
        return True
    finally:
        buffer.close()


def _read_required(store: SecretStore, account: str, *, wallet: bool) -> SecretBuffer:
    try:
        return store.read_required(CLOB_SERVICE, account, _CHECK_PROMPT)
    except SecretStoreError as error:
        _raise_public_error(error, wallet=wallet)


def _raise_public_error(error: SecretStoreError, *, wallet: bool) -> None:
    if error.code == "SECRET_ACCESS_DENIED":
        raise CredentialCommandError("KEYCHAIN_ACCESS_DENIED") from None
    if error.code == "SECRET_STORE_UNAVAILABLE":
        raise CredentialCommandError("KEYCHAIN_UNAVAILABLE") from None
    if wallet and error.code == "SECRET_ITEM_MISSING":
        raise CredentialCommandError("WALLET_MISSING") from None
    if wallet and error.code == "SECRET_VALUE_INVALID":
        raise CredentialCommandError("WALLET_INVALID") from None
    raise CredentialCommandError("KEYCHAIN_UNAVAILABLE") from None


__all__ = [
    "CredentialCommandError",
    "CredentialReadiness",
    "check_credential_readiness",
    "create_credentials",
    "render_credential_readiness",
]
