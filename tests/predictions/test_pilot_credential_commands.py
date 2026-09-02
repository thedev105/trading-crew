from __future__ import annotations

from collections.abc import Callable
from hashlib import sha256

import pytest

from polytrading.predictions.pilot import credential_commands
from polytrading.predictions.pilot.credential_commands import (
    CredentialCommandError,
    CredentialReadiness,
    check_credential_readiness,
    create_credentials,
    render_credential_readiness,
)
from polytrading.predictions.pilot.signer_bootstrap import SignerBootstrapError
from polytrading.predictions.polymarket_execution.credentials import CredentialFingerprint
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
        self.calls: list[tuple[str, str]] = []

    def read_required(self, service: str, account: str, prompt: str) -> SecretBuffer:
        assert service == CLOB_SERVICE
        assert prompt
        self.calls.append(("read", account))
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


def complete_store() -> FakeSecretStore:
    return FakeSecretStore(
        {
            WALLET_PRIVATE_KEY_ACCOUNT: WALLET_CANARY,
            CLOB_API_KEY_ACCOUNT: CLOB_CANARY,
            CLOB_API_SECRET_ACCOUNT: CLOB_CANARY + b"-secret",
            CLOB_PASSPHRASE_ACCOUNT: CLOB_CANARY + b"-passphrase",
        }
    )


def test_create_requires_confirmation_before_keychain_or_child_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = wallet_only_store()
    child_started: list[bool] = []
    monkeypatch.setattr(
        credential_commands,
        "_create_credentials_in_sidecar",
        lambda **_kwargs: child_started.append(True),
    )

    with pytest.raises(CredentialCommandError) as raised:
        create_credentials(store=store, confirmed=False)

    assert raised.value.code == "CONFIRMATION_REQUIRED"
    assert store.calls == []
    assert child_started == []


def test_create_preflights_only_clob_presence_and_returns_the_public_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = wallet_only_store()
    expected = CredentialFingerprint(
        account_fingerprint="a" * 64,
        credential_fingerprint=sha256(CLOB_CANARY).hexdigest(),
        operation="CREATE",
        result="CREATED",
    )
    monkeypatch.setattr(
        credential_commands,
        "_create_credentials_in_sidecar",
        lambda **_kwargs: expected,
    )

    result = create_credentials(store=store, confirmed=True)

    assert result == expected
    assert [account for _operation, account in store.calls] == [
        CLOB_API_KEY_ACCOUNT,
        CLOB_API_SECRET_ACCOUNT,
        CLOB_PASSPHRASE_ACCOUNT,
    ]
    assert WALLET_PRIVATE_KEY_ACCOUNT not in {account for _operation, account in store.calls}
    assert CLOB_CANARY.decode() not in repr(result)


@pytest.mark.parametrize(
    ("store_factory", "code"),
    [
        (complete_store, "CREDENTIALS_ALREADY_PRESENT"),
        (wallet_and_one_clob_item_store, "CREDENTIALS_PARTIAL"),
    ],
)
def test_existing_or_partial_credentials_fail_before_the_child(
    monkeypatch: pytest.MonkeyPatch,
    store_factory: Callable[[], FakeSecretStore],
    code: str,
) -> None:
    store = store_factory()
    child_started: list[bool] = []
    monkeypatch.setattr(
        credential_commands,
        "_create_credentials_in_sidecar",
        lambda **_kwargs: child_started.append(True),
    )

    with pytest.raises(CredentialCommandError) as raised:
        create_credentials(store=store, confirmed=True)

    assert raised.value.code == code
    assert child_started == []


@pytest.mark.parametrize(
    ("sidecar_code", "command_code"),
    [
        ("SECRET_DESCRIPTOR_READ_FAILED", "SIGNER_BOOTSTRAP_FAILED"),
        ("CREDENTIAL_CREATE_FAILED", "CREDENTIAL_CREATE_FAILED"),
        ("CREDENTIAL_STORE_FAILED", "CREDENTIAL_STORE_FAILED"),
    ],
)
def test_create_maps_sidecar_failures_to_the_fixed_public_codes(
    monkeypatch: pytest.MonkeyPatch,
    sidecar_code: str,
    command_code: str,
) -> None:
    def fail(**_kwargs: object) -> CredentialFingerprint:
        raise SignerBootstrapError(sidecar_code)

    monkeypatch.setattr(credential_commands, "_create_credentials_in_sidecar", fail)

    with pytest.raises(CredentialCommandError) as raised:
        create_credentials(store=wallet_only_store(), confirmed=True)

    assert raised.value.code == command_code
    assert str(raised.value) == ""


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
