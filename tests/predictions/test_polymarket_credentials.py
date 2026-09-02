from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from polytrading.predictions.polymarket_execution.credentials import (
    CredentialProvisioner,
    CredentialProvisioningError,
    CredentialProvisioningGrant,
    provision_credentials,
)
from polytrading.predictions.polymarket_execution.keychain_macos import (
    CLOB_API_KEY_ACCOUNT,
    CLOB_API_SECRET_ACCOUNT,
    CLOB_PASSPHRASE_ACCOUNT,
    CLOB_SERVICE,
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
    SecretStoreError,
)
from polytrading.predictions.polymarket_execution.systemd_credentials_linux import (
    SystemdCredentialSecretStore,
)

NOW = datetime(2026, 8, 29, 12, tzinfo=UTC)
SIGNER_ADDRESS = "0x7E5F4552091A69125d5DfCb7b8C2659029395Bdf"
FUNDER_ADDRESS = "0x" + "11" * 20
TEST_API_KEY = b"clob-api-key-canary"
TEST_SECRET = b"clob-api-secret-canary"
TEST_PASSPHRASE = b"clob-passphrase-canary"


def fingerprint(address: str) -> str:
    return sha256(address.casefold().encode("ascii")).hexdigest()


def account_model(**overrides: Any):
    snapshot = load_protocol_snapshot(version=POLYMARKET_PILOT_PROTOCOL_VERSION)
    binding = bind_account_signature(
        snapshot,
        signer_address=SIGNER_ADDRESS,
        funder_address=FUNDER_ADDRESS,
        signature_type=0,
        negative_risk=False,
        credential_route_hash=CREDENTIAL_ROUTE_SET_HASH,
    )
    return replace(binding, **overrides) if overrides else binding


def valid_credential_grant(**overrides: Any) -> CredentialProvisioningGrant:
    fields: dict[str, Any] = {
        "grant_id": UUID("00000000-0000-0000-0000-0000000f0001"),
        "grant_kind": "CREDENTIAL_PROVISIONING",
        "operation": "CREATE",
        "wallet_fingerprint": fingerprint(FUNDER_ADDRESS),
        "account_fingerprint": fingerprint(SIGNER_ADDRESS),
        "protocol_version": POLYMARKET_PILOT_PROTOCOL_VERSION,
        "grant_digest": "a" * 64,
        "issued_at": NOW,
        "expires_at": NOW + timedelta(seconds=60),
    }
    fields.update(overrides)
    return CredentialProvisioningGrant(**fields)


class FakeCredentialClient:
    """Returns owned buffers for the one allowlisted L1 call and records how it was invoked."""

    def __init__(self, *, response: dict[str, bytes] | None = None) -> None:
        self.response = response
        self.calls: list[tuple[str, str]] = []

    def create_or_derive(self, *, operation: str, binding: Any) -> dict[str, SecretBuffer]:
        self.calls.append((operation, binding.signer_address))
        raw = self.response
        if raw is None:
            raw = {
                CLOB_API_KEY_ACCOUNT: TEST_API_KEY,
                CLOB_API_SECRET_ACCOUNT: TEST_SECRET,
                CLOB_PASSPHRASE_ACCOUNT: TEST_PASSPHRASE,
            }
        return {name: SecretBuffer.from_bytes(value) for name, value in raw.items()}


class RefusingStore(InMemorySecretStore):
    """Fails on one account so the partial-write rollback path is exercised."""

    def __init__(self, failing_account: str) -> None:
        super().__init__()
        self.failing_account = failing_account

    def create_protected(self, service: str, account: str, value: SecretBuffer) -> object:
        if account == self.failing_account:
            raise SecretStoreError("SECRET_WRITE_FAILED")
        return super().create_protected(service, account, value)


class RollbackRefusingStore(InMemorySecretStore):
    """Simulates a second create failure followed by a non-removable first slot."""

    def create_protected(self, service: str, account: str, value: SecretBuffer) -> object:
        if account == CLOB_API_SECRET_ACCOUNT:
            raise SecretStoreError("SECRET_WRITE_FAILED")
        return super().create_protected(service, account, value)

    def delete_created(self, creation: object) -> None:
        del creation
        raise SecretStoreError("SECRET_WRITE_FAILED")


class ConcurrentCredentialStore(InMemorySecretStore):
    """Claims the first slot after preflight but before this ceremony can create it."""

    def create_protected(self, service: str, account: str, value: SecretBuffer) -> object:
        if account == CLOB_API_KEY_ACCOUNT:
            competing = SecretBuffer.from_bytes(b"concurrent-credential")
            try:
                super().create_protected(service, account, competing)
            finally:
                competing.close()
        return super().create_protected(service, account, value)


def stored(store: InMemorySecretStore, account: str) -> bytes:
    return bytes(store.read_required(CLOB_SERVICE, account, "unlock").use(bytes))


def provisioner(store: InMemorySecretStore, client: FakeCredentialClient) -> CredentialProvisioner:
    return CredentialProvisioner(store, client)


def test_provisioner_writes_secrets_without_returning_them() -> None:
    store = InMemorySecretStore()
    client = FakeCredentialClient()

    result = provisioner(store, client).provision(
        valid_credential_grant(), account_model(), now=NOW
    )

    assert result.credential_fingerprint == sha256(TEST_API_KEY).hexdigest()
    assert result.result == "CREATED"
    assert stored(store, CLOB_API_SECRET_ACCOUNT) == TEST_SECRET
    assert stored(store, CLOB_PASSPHRASE_ACCOUNT) == TEST_PASSPHRASE
    for canary in (TEST_API_KEY, TEST_SECRET, TEST_PASSPHRASE):
        assert canary.decode() not in repr(result)
        assert canary.decode() not in str(result)


def test_a_derive_ceremony_reports_its_own_result_code() -> None:
    store = InMemorySecretStore()
    client = FakeCredentialClient()

    result = provision_credentials(
        valid_credential_grant(operation="DERIVE"),
        account_model(),
        store=store,
        client=client,
        now=NOW,
    )

    assert result.result == "DERIVED"
    assert client.calls == [("DERIVE", SIGNER_ADDRESS)]


def test_a_grant_is_single_use() -> None:
    store = InMemorySecretStore()
    client = FakeCredentialClient()
    ceremony = provisioner(store, client)
    grant = valid_credential_grant()
    ceremony.provision(grant, account_model(), now=NOW)

    with pytest.raises(CredentialProvisioningError) as raised:
        ceremony.provision(grant, account_model(), now=NOW)
    assert raised.value.code == "GRANT_ALREADY_USED"
    assert len(client.calls) == 1


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"expires_at": NOW + timedelta(seconds=61)}, "GRANT_INVALID"),
        ({"expires_at": NOW}, "GRANT_INVALID"),
        ({"grant_digest": "short"}, "GRANT_INVALID"),
        ({"grant_kind": "PRIMARY"}, "GRANT_IS_NOT_A_CREDENTIAL_GRANT"),
        ({"operation": "REVOKE"}, "GRANT_OPERATION_NOT_ALLOWED"),
        ({"protocol_version": "polymarket-clob-2026-08-25-v1"}, "GRANT_PROTOCOL_MISMATCH"),
        ({"wallet_fingerprint": "b" * 64}, "GRANT_WALLET_MISMATCH"),
        ({"account_fingerprint": "c" * 64}, "GRANT_ACCOUNT_MISMATCH"),
    ],
)
def test_every_grant_binding_is_checked(overrides: dict[str, Any], code: str) -> None:
    store = InMemorySecretStore()
    client = FakeCredentialClient()

    with pytest.raises(CredentialProvisioningError) as raised:
        provisioner(store, client).provision(
            valid_credential_grant(**overrides), account_model(), now=NOW
        )
    assert raised.value.code == code
    assert client.calls == []


def test_an_expired_grant_never_reaches_the_venue() -> None:
    store = InMemorySecretStore()
    client = FakeCredentialClient()

    with pytest.raises(CredentialProvisioningError) as raised:
        provisioner(store, client).provision(
            valid_credential_grant(), account_model(), now=NOW + timedelta(seconds=60)
        )
    assert raised.value.code == "GRANT_EXPIRED"
    assert client.calls == []


@pytest.mark.parametrize(
    "response",
    [
        {CLOB_API_KEY_ACCOUNT: TEST_API_KEY},
        {
            CLOB_API_KEY_ACCOUNT: TEST_API_KEY,
            CLOB_API_SECRET_ACCOUNT: TEST_SECRET,
            CLOB_PASSPHRASE_ACCOUNT: TEST_PASSPHRASE,
            "refresh-token": b"unexpected",
        },
    ],
)
def test_an_incomplete_or_widened_response_stores_nothing(response: dict[str, bytes]) -> None:
    store = InMemorySecretStore()
    client = FakeCredentialClient(response=response)

    with pytest.raises(CredentialProvisioningError) as raised:
        provisioner(store, client).provision(valid_credential_grant(), account_model(), now=NOW)
    assert raised.value.code == "CREDENTIAL_RESPONSE_INVALID"
    with pytest.raises(SecretStoreError):
        store.read_required(CLOB_SERVICE, CLOB_API_KEY_ACCOUNT, "unlock")


def test_a_partial_write_is_rolled_back() -> None:
    store = RefusingStore(CLOB_PASSPHRASE_ACCOUNT)
    client = FakeCredentialClient()

    with pytest.raises(CredentialProvisioningError) as raised:
        provisioner(store, client).provision(valid_credential_grant(), account_model(), now=NOW)

    assert raised.value.code == "CREDENTIAL_STORE_FAILED"
    for account in (CLOB_API_KEY_ACCOUNT, CLOB_API_SECRET_ACCOUNT, CLOB_PASSPHRASE_ACCOUNT):
        with pytest.raises(SecretStoreError):
            store.read_required(CLOB_SERVICE, account, "unlock")


def test_rollback_failure_is_never_reported_as_an_ordinary_store_failure() -> None:
    store = RollbackRefusingStore()

    with pytest.raises(CredentialProvisioningError) as raised:
        provisioner(store, FakeCredentialClient()).provision(
            valid_credential_grant(), account_model(), now=NOW
        )

    assert raised.value.code == "CREDENTIAL_ROLLBACK_FAILED"
    retained = store.read_required(CLOB_SERVICE, CLOB_API_KEY_ACCOUNT, "unlock")
    retained.close()


def test_add_only_creation_preserves_a_concurrent_credential() -> None:
    store = ConcurrentCredentialStore()

    with pytest.raises(CredentialProvisioningError) as raised:
        provisioner(store, FakeCredentialClient()).provision(
            valid_credential_grant(), account_model(), now=NOW
        )

    assert raised.value.code == "CREDENTIAL_STORE_FAILED"
    retained = store.read_required(CLOB_SERVICE, CLOB_API_KEY_ACCOUNT, "unlock")
    assert len(retained) > 0
    retained.close()


def test_rollback_never_deletes_a_preexisting_credential() -> None:
    store = RefusingStore(CLOB_PASSPHRASE_ACCOUNT)
    existing = SecretBuffer.from_bytes(TEST_API_KEY)
    store.write_protected(CLOB_SERVICE, CLOB_API_KEY_ACCOUNT, existing)
    existing.close()

    with pytest.raises(CredentialProvisioningError) as raised:
        provisioner(store, FakeCredentialClient()).provision(
            valid_credential_grant(), account_model(), now=NOW
        )

    assert raised.value.code == "CREDENTIAL_STORE_FAILED"
    assert stored(store, CLOB_API_KEY_ACCOUNT) == TEST_API_KEY


def test_response_buffers_are_closed_even_when_storing_fails() -> None:
    store = RefusingStore(CLOB_API_KEY_ACCOUNT)
    handed_out: list[SecretBuffer] = []

    class TrackingClient(FakeCredentialClient):
        def create_or_derive(self, *, operation: str, binding: Any) -> dict[str, SecretBuffer]:
            buffers = super().create_or_derive(operation=operation, binding=binding)
            handed_out.extend(buffers.values())
            return buffers

    with pytest.raises(CredentialProvisioningError):
        provisioner(store, TrackingClient()).provision(
            valid_credential_grant(), account_model(), now=NOW
        )

    assert handed_out
    assert all(buffer.closed for buffer in handed_out)


def test_a_successful_ceremony_also_closes_its_response_buffers() -> None:
    handed_out: list[SecretBuffer] = []

    class TrackingClient(FakeCredentialClient):
        def create_or_derive(self, *, operation: str, binding: Any) -> dict[str, SecretBuffer]:
            buffers = super().create_or_derive(operation=operation, binding=binding)
            handed_out.extend(buffers.values())
            return buffers

    store = InMemorySecretStore()
    provisioner(store, TrackingClient()).provision(
        valid_credential_grant(), account_model(), now=NOW
    )

    assert all(buffer.closed for buffer in handed_out)
    assert stored(store, CLOB_API_KEY_ACCOUNT) == TEST_API_KEY


def test_a_credential_grant_is_not_an_execution_capability() -> None:
    from polytrading.predictions.execution.authority import ExecutionCapability

    grant = valid_credential_grant()

    assert not isinstance(grant, ExecutionCapability)
    assert not hasattr(grant, "allowed_operations")
    assert not hasattr(grant, "maximum_capital")


class FailingLinuxEncryptionRunner:
    def __init__(self, encrypted_directory: Path, *, replace_first: bool = False) -> None:
        self.encrypted_directory = encrypted_directory
        self.replace_first = replace_first
        self.calls: list[str] = []

    def encrypt(self, *, account: str, value: SecretBuffer) -> bytearray:
        assert len(value) > 0
        self.calls.append(account)
        if account == CLOB_API_SECRET_ACCOUNT:
            if self.replace_first:
                target = self.encrypted_directory / f"{CLOB_API_KEY_ACCOUNT}.cred"
                target.unlink()
                target.write_bytes(b"external-encrypted-replacement")
                target.chmod(0o600)
            raise SecretStoreError("SECRET_WRITE_FAILED")
        return bytearray(f"encrypted-{account}".encode("ascii"))


def linux_store(
    tmp_path: Path, runner: FailingLinuxEncryptionRunner
) -> SystemdCredentialSecretStore:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    return SystemdCredentialSecretStore(runtime, runner.encrypted_directory, runner=runner)


def test_linux_encrypted_store_rolls_back_a_partial_credential_set(tmp_path: Path) -> None:
    encrypted = tmp_path / "encrypted"
    encrypted.mkdir(mode=0o700)
    runner = FailingLinuxEncryptionRunner(encrypted)
    store = linux_store(tmp_path, runner)
    try:
        with pytest.raises(CredentialProvisioningError) as raised:
            provisioner(store, FakeCredentialClient()).provision(
                valid_credential_grant(), account_model(), now=NOW
            )
    finally:
        store.close()

    assert raised.value.code == "CREDENTIAL_STORE_FAILED"
    assert runner.calls == [CLOB_API_KEY_ACCOUNT, CLOB_API_SECRET_ACCOUNT]
    assert list(encrypted.iterdir()) == []


def test_linux_rollback_preserves_a_replacement_and_reports_ownership_loss(
    tmp_path: Path,
) -> None:
    encrypted = tmp_path / "encrypted"
    encrypted.mkdir(mode=0o700)
    runner = FailingLinuxEncryptionRunner(encrypted, replace_first=True)
    store = linux_store(tmp_path, runner)
    try:
        with pytest.raises(CredentialProvisioningError) as raised:
            provisioner(store, FakeCredentialClient()).provision(
                valid_credential_grant(), account_model(), now=NOW
            )
    finally:
        store.close()

    replacement = encrypted / f"{CLOB_API_KEY_ACCOUNT}.cred"
    assert raised.value.code == "CREDENTIAL_ROLLBACK_FAILED"
    assert replacement.read_bytes() == b"external-encrypted-replacement"
