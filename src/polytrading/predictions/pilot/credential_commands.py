"""Secret-safe local status for the fixed Polymarket credential Keychain items."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Literal

from polytrading.predictions.polymarket_execution.credentials import CredentialFingerprint
from polytrading.predictions.polymarket_execution.secret_labels import (
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
_SECP256K1_ORDER: Final = int(
    "FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141", 16
)
_PUBLIC_CREDENTIAL_CEREMONY_CODES: Final = frozenset(
    {
        "CREDENTIALS_ALREADY_PRESENT",
        "CREDENTIALS_PARTIAL",
        "CREDENTIALS_CREATE_IN_PROGRESS",
        "CREDENTIAL_CREATE_FAILED",
        "CREDENTIAL_TRANSPORT_UNAVAILABLE",
        "CREDENTIAL_REQUEST_REJECTED",
        "CREDENTIAL_RESPONSE_INVALID",
        "CREDENTIAL_SIGNING_FAILED",
        "CREDENTIAL_STORE_FAILED",
        "CREDENTIAL_ROLLBACK_FAILED",
    }
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
    confirmed: bool,
    store: SecretStore | None = None,
    now: Callable[[], datetime] | None = None,
) -> CredentialFingerprint:
    """Run the explicitly confirmed absent-only create ceremony."""
    if confirmed is not True:
        raise CredentialCommandError("CONFIRMATION_REQUIRED")
    try:
        if store is None:
            return _create_credentials_in_clean_sidecar()
        return _create_credentials_in_sidecar(store=store, now=now or _utc_now)
    except Exception as error:
        _raise_credential_ceremony_error(error)


def derive_credentials(
    *,
    confirmed: bool,
    store: SecretStore | None = None,
    now: Callable[[], datetime] | None = None,
) -> CredentialFingerprint:
    """Explicitly recover remote credentials only when all local slots are absent."""
    if confirmed is not True:
        raise CredentialCommandError("CONFIRMATION_REQUIRED")
    try:
        if store is None:
            return _derive_credentials_in_clean_sidecar()
        return _derive_credentials_in_sidecar(store=store, now=now or _utc_now)
    except Exception as error:
        _raise_credential_ceremony_error(error)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _create_credentials_in_sidecar(
    *, store: SecretStore, now: Callable[[], datetime]
) -> CredentialFingerprint:
    from polytrading.predictions.pilot.signer_bootstrap import create_credentials_in_sidecar

    return create_credentials_in_sidecar(store=store, now=now)


def _create_credentials_in_clean_sidecar() -> CredentialFingerprint:
    from polytrading.predictions.pilot.signer_bootstrap import (
        create_credentials_in_clean_sidecar,
    )

    return create_credentials_in_clean_sidecar()


def _derive_credentials_in_sidecar(
    *, store: SecretStore, now: Callable[[], datetime]
) -> CredentialFingerprint:
    from polytrading.predictions.pilot.signer_bootstrap import derive_credentials_in_sidecar

    return derive_credentials_in_sidecar(store=store, now=now)


def _derive_credentials_in_clean_sidecar() -> CredentialFingerprint:
    from polytrading.predictions.pilot.signer_bootstrap import (
        derive_credentials_in_clean_sidecar,
    )

    return derive_credentials_in_clean_sidecar()


def _raise_credential_ceremony_error(error: Exception) -> None:
    from polytrading.predictions.pilot.signer_bootstrap import SignerBootstrapError

    if not isinstance(error, SignerBootstrapError):
        raise error
    code = (
        error.code if error.code in _PUBLIC_CREDENTIAL_CEREMONY_CODES else "SIGNER_BOOTSTRAP_FAILED"
    )
    raise CredentialCommandError(code) from None


def _item_is_valid_wallet(store: SecretStore) -> bool:
    buffer = _read_required(store, WALLET_PRIVATE_KEY_ACCOUNT, wallet=True)
    try:
        return bool(
            buffer.use(
                lambda value: (
                    len(value) == 32 and 0 < int.from_bytes(value, "big") < _SECP256K1_ORDER
                )
            )
        )
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
    "derive_credentials",
    "render_credential_readiness",
]
