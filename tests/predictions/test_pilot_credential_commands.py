from __future__ import annotations

import pytest

from polytrading.predictions.pilot.credential_commands import (
    CredentialCommandError,
    CredentialReadiness,
    check_credential_readiness,
    render_credential_readiness,
)
from polytrading.predictions.polymarket_execution.keychain_macos import (
    CLOB_API_KEY_ACCOUNT,
    CLOB_API_SECRET_ACCOUNT,
    CLOB_PASSPHRASE_ACCOUNT,
    CLOB_SERVICE,
    WALLET_PRIVATE_KEY_ACCOUNT,
)
from polytrading.predictions.polymarket_execution.secrets import SecretBuffer, SecretStoreError

WALLET_CANARY = b"wallet-canary" + b"-" * 19
CLOB_CANARY = b"clob-canary"
class FakeSecretStore:
    """A local fake with a network tripwire and observable returned-buffer ownership."""

    def __init__(
        self,
        items: dict[str, bytes],
        errors: dict[str, str] | None = None,
    ) -> None:
        self._items = items
        self._errors = errors or {}
        self.handed_out: list[SecretBuffer] = []
        self.network_opened = False

    def read_required(self, service: str, account: str, prompt: str) -> SecretBuffer:
        assert service == CLOB_SERVICE
        assert prompt
        if account in self._errors:
            raise SecretStoreError(self._errors[account])  # type: ignore[arg-type]
        if account not in self._items:
            raise SecretStoreError("SECRET_ITEM_MISSING")
        buffer = SecretBuffer.from_bytes(self._items[account])
        self.handed_out.append(buffer)
        return buffer


def wallet_only_store() -> FakeSecretStore:
    return FakeSecretStore({WALLET_PRIVATE_KEY_ACCOUNT: WALLET_CANARY})


def wallet_and_one_clob_item_store() -> FakeSecretStore:
    return FakeSecretStore(
        {
            WALLET_PRIVATE_KEY_ACCOUNT: WALLET_CANARY,
            CLOB_API_KEY_ACCOUNT: CLOB_CANARY,
        }
    )


def test_readiness_is_local_only_and_reports_a_valid_wallet_with_no_clob_credentials() -> None:
    store = wallet_only_store()

    result = check_credential_readiness(store)

    assert result == CredentialReadiness(wallet_ready=True, credentials_state="ABSENT")
    assert render_credential_readiness(result) == "wallet_ready=true\ncredentials=ABSENT"
    assert store.network_opened is False
    assert WALLET_CANARY.decode() not in render_credential_readiness(result)
    assert all(buffer.closed for buffer in store.handed_out)


def test_readiness_reports_present_clob_state_without_returning_a_value() -> None:
    store = FakeSecretStore(
        {
            WALLET_PRIVATE_KEY_ACCOUNT: WALLET_CANARY,
            CLOB_API_KEY_ACCOUNT: CLOB_CANARY,
            CLOB_API_SECRET_ACCOUNT: CLOB_CANARY + b"-secret",
            CLOB_PASSPHRASE_ACCOUNT: CLOB_CANARY + b"-passphrase",
        }
    )

    result = check_credential_readiness(store)

    assert result == CredentialReadiness(wallet_ready=True, credentials_state="PRESENT")
    assert CLOB_CANARY.decode() not in render_credential_readiness(result)
    assert all(buffer.closed for buffer in store.handed_out)


def test_readiness_reports_partial_clob_state_without_returning_a_value() -> None:
    store = wallet_and_one_clob_item_store()

    result = check_credential_readiness(store)

    assert result == CredentialReadiness(wallet_ready=True, credentials_state="PARTIAL")
    assert CLOB_CANARY.decode() not in render_credential_readiness(result)
    assert all(buffer.closed for buffer in store.handed_out)


@pytest.mark.parametrize(
    ("account", "error", "code"),
    [
        (WALLET_PRIVATE_KEY_ACCOUNT, "SECRET_ITEM_MISSING", "WALLET_MISSING"),
        (WALLET_PRIVATE_KEY_ACCOUNT, "SECRET_VALUE_INVALID", "WALLET_INVALID"),
        (WALLET_PRIVATE_KEY_ACCOUNT, "SECRET_ACCESS_DENIED", "KEYCHAIN_ACCESS_DENIED"),
        (WALLET_PRIVATE_KEY_ACCOUNT, "SECRET_STORE_UNAVAILABLE", "KEYCHAIN_UNAVAILABLE"),
        (CLOB_API_KEY_ACCOUNT, "SECRET_ACCESS_DENIED", "KEYCHAIN_ACCESS_DENIED"),
        (CLOB_API_KEY_ACCOUNT, "SECRET_STORE_UNAVAILABLE", "KEYCHAIN_UNAVAILABLE"),
    ],
)
def test_readiness_maps_keychain_failures_to_sanitized_public_codes(
    account: str, error: str, code: str
) -> None:
    store = FakeSecretStore({WALLET_PRIVATE_KEY_ACCOUNT: WALLET_CANARY}, {account: error})

    with pytest.raises(CredentialCommandError) as raised:
        check_credential_readiness(store)

    assert raised.value.code == code
    assert str(raised.value) == ""
    assert error not in repr(raised.value)


def test_readiness_rejects_a_wallet_buffer_that_is_not_32_bytes() -> None:
    store = FakeSecretStore({WALLET_PRIVATE_KEY_ACCOUNT: b"not-a-valid-wallet"})

    result = check_credential_readiness(store)

    assert result == CredentialReadiness(wallet_ready=False, credentials_state="ABSENT")
    assert all(buffer.closed for buffer in store.handed_out)
