from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime
from hashlib import sha256
from multiprocessing import get_context
from typing import Any

import pytest

import polytrading.predictions.pilot.signer_bootstrap as signer_bootstrap
from polytrading.predictions.pilot.signer_bootstrap import (
    SECRET_ACCOUNTS,
    UNLOCK_PROMPT,
    ChildLaunch,
    CredentialCeremonyResult,
    SignerBootstrapError,
    _launch_credential_ceremony,
    launch_signer_sidecar,
)
from polytrading.predictions.polymarket_execution.credential_client import CredentialTransportError
from polytrading.predictions.polymarket_execution.keychain_macos import (
    CLOB_API_KEY_ACCOUNT,
    CLOB_API_SECRET_ACCOUNT,
    CLOB_PASSPHRASE_ACCOUNT,
    CLOB_SERVICE,
    WALLET_PRIVATE_KEY_ACCOUNT,
)
from polytrading.predictions.polymarket_execution.protocol import AccountSignatureBinding
from polytrading.predictions.polymarket_execution.secrets import (
    InMemorySecretStore,
    SecretBuffer,
    SecretStoreError,
    read_secret_descriptors,
)

PRIVATE_KEY = (7).to_bytes(32, "big")
API_KEY = b"api-key-canary"
API_SECRET = b"api-secret-canary"
PASSPHRASE = b"passphrase-canary"
VALUES = {
    "wallet-private-key": PRIVATE_KEY,
    "clob-api-key": API_KEY,
    "clob-api-secret": API_SECRET,
    "clob-passphrase": PASSPHRASE,
}

NOW = datetime(2026, 8, 29, 12, tzinfo=UTC)


class CreateOnlyCredentialClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, AccountSignatureBinding]] = []
        self.response_buffers: list[SecretBuffer] = []

    def create_or_derive(
        self, *, operation: str, binding: AccountSignatureBinding
    ) -> dict[str, SecretBuffer]:
        self.calls.append((operation, binding))
        returned = {
            account: SecretBuffer.from_bytes(VALUES[account])
            for account in (CLOB_API_KEY_ACCOUNT, CLOB_API_SECRET_ACCOUNT, CLOB_PASSPHRASE_ACCOUNT)
        }
        self.response_buffers.extend(returned.values())
        return returned


class SecondWriteRefusingStore(InMemorySecretStore):
    def create_protected(self, service: str, account: str, value: SecretBuffer) -> object:
        if account == CLOB_API_SECRET_ACCOUNT:
            raise SecretStoreError("SECRET_WRITE_FAILED")
        return super().create_protected(service, account, value)


class RollbackRefusingStore(SecondWriteRefusingStore):
    def delete_created(self, creation: object) -> None:
        del creation
        raise SecretStoreError("SECRET_WRITE_FAILED")


class ExternalUpdateDuringRollbackStore(InMemorySecretStore):
    """A non-cooperating writer replaces the first slot before our failed rollback."""

    def create_protected(self, service: str, account: str, value: SecretBuffer) -> object:
        if account == CLOB_API_SECRET_ACCOUNT:
            replacement = SecretBuffer.from_bytes(b"external-replacement")
            try:
                self.write_protected(service, CLOB_API_KEY_ACCOUNT, replacement)
            finally:
                replacement.close()
            raise SecretStoreError("SECRET_WRITE_FAILED")
        return super().create_protected(service, account, value)


def wallet_only_memory(store_type: type[InMemorySecretStore] = InMemorySecretStore) -> Any:
    memory = store_type()
    wallet = SecretBuffer.from_bytes(VALUES[WALLET_PRIVATE_KEY_ACCOUNT])
    memory.write_protected(CLOB_SERVICE, WALLET_PRIVATE_KEY_ACCOUNT, wallet)
    wallet.close()
    return memory


def clob_slots_are_absent(memory: InMemorySecretStore) -> bool:
    for account in (CLOB_API_KEY_ACCOUNT, CLOB_API_SECRET_ACCOUNT, CLOB_PASSPHRASE_ACCOUNT):
        try:
            buffer = memory.read_required(CLOB_SERVICE, account, "verify absence")
        except SecretStoreError as error:
            if error.code == "SECRET_ITEM_MISSING":
                continue
            raise
        else:
            buffer.close()
            return False
    return True


def test_one_shot_credential_child_runs_only_create_and_returns_public_fingerprints() -> None:
    memory = store(missing=set(SECRET_ACCOUNTS[1:]))
    client = CreateOnlyCredentialClient()
    handed_off_private_keys: list[bytearray] = []

    def client_factory(*, private_key: bytearray, timestamp: Any) -> CreateOnlyCredentialClient:
        del timestamp
        handed_off_private_keys.append(private_key)
        return client

    result = _launch_credential_ceremony(
        store=memory,
        now=lambda: NOW,
        _spawn=lambda launch: launch.run(),
        _client_factory=client_factory,
    )

    assert result == CredentialCeremonyResult(
        ok=True,
        code="CREATED",
        account_fingerprint=result.account_fingerprint,
        credential_fingerprint=sha256(VALUES[CLOB_API_KEY_ACCOUNT]).hexdigest(),
    )
    assert result.account_fingerprint is not None
    assert client.calls[0][0] == "CREATE"
    assert client.calls[0][1].signer_address == client.calls[0][1].funder_address
    assert all(buffer.closed for buffer in client.response_buffers)
    assert handed_off_private_keys
    assert all(not any(private_key) for private_key in handed_off_private_keys)
    assert not hasattr(result, "api_key")
    assert not hasattr(result, "api_secret")
    assert not hasattr(result, "passphrase")


def test_linux_clean_credential_child_uses_the_platform_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory = wallet_only_memory()
    observed: list[tuple[object, str]] = []
    read_fd, write_fd = os.pipe()
    monkeypatch.setattr(signer_bootstrap, "open_pilot_secret_store", lambda: memory)

    def run_child(**arguments: object) -> None:
        observed.append((arguments["store"], str(arguments["operation"])))
        os.close(int(arguments["response_fd"]))

    monkeypatch.setattr(signer_bootstrap, "_run_credential_child", run_child)
    try:
        assert signer_bootstrap._run_clean_credential_child(write_fd, "DERIVE") == 0
    finally:
        os.close(read_fd)

    assert observed == [(memory, "DERIVE")]


def test_clean_credential_child_import_graph_excludes_the_macos_adapter() -> None:
    script = (
        "import sys; import polytrading.predictions.pilot.signer_bootstrap; "
        "raise SystemExit("
        "'polytrading.predictions.polymarket_execution.keychain_macos' in sys.modules)"
    )

    completed = subprocess.run(
        [sys.executable, "-I", "-c", script],
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0


def test_one_shot_credential_child_runs_only_derive_and_returns_public_fingerprints() -> None:
    memory = store(missing=set(SECRET_ACCOUNTS[1:]))
    client = CreateOnlyCredentialClient()

    result = _launch_credential_ceremony(
        store=memory,
        now=lambda: NOW,
        operation="DERIVE",
        _spawn=lambda launch: launch.run(),
        _client_factory=lambda **_kwargs: client,
    )

    assert result == CredentialCeremonyResult(
        ok=True,
        code="DERIVED",
        account_fingerprint=result.account_fingerprint,
        credential_fingerprint=sha256(VALUES[CLOB_API_KEY_ACCOUNT]).hexdigest(),
    )
    assert [operation for operation, _binding in client.calls] == ["DERIVE"]
    assert all(buffer.closed for buffer in client.response_buffers)


@pytest.mark.parametrize(
    ("missing", "expected"),
    [
        (set(), "CREDENTIALS_ALREADY_PRESENT"),
        ({CLOB_API_SECRET_ACCOUNT}, "CREDENTIALS_PARTIAL"),
    ],
)
def test_derive_refuses_nonempty_local_credential_state_before_remote_recovery(
    missing: set[str], expected: str
) -> None:
    client = CreateOnlyCredentialClient()

    result = _launch_credential_ceremony(
        store=store(missing=missing),
        now=lambda: NOW,
        operation="DERIVE",
        _spawn=lambda launch: launch.run(),
        _client_factory=lambda **_kwargs: client,
    )

    assert result == CredentialCeremonyResult(False, expected)
    assert client.calls == []


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        ("transport", "CREDENTIAL_TRANSPORT_UNAVAILABLE"),
        ("invalid_response", "CREDENTIAL_RESPONSE_INVALID"),
        ("second_write", "CREDENTIAL_STORE_FAILED"),
    ],
)
def test_create_failure_leaves_no_clob_trio(failure: str, expected: str) -> None:
    response_buffers: list[SecretBuffer] = []

    class FailingClient:
        def create_or_derive(
            self, *, operation: str, binding: AccountSignatureBinding
        ) -> dict[str, SecretBuffer]:
            assert operation == "CREATE"
            if failure == "transport":
                raise CredentialTransportError("CREDENTIAL_TRANSPORT_UNAVAILABLE")
            if failure == "invalid_response":
                buffer = SecretBuffer.from_bytes(VALUES[CLOB_API_KEY_ACCOUNT])
                response_buffers.append(buffer)
                return {CLOB_API_KEY_ACCOUNT: buffer}
            return CreateOnlyCredentialClient().create_or_derive(
                operation=operation, binding=binding
            )

    memory = (
        wallet_only_memory(SecondWriteRefusingStore)
        if failure == "second_write"
        else wallet_only_memory()
    )
    result = _launch_credential_ceremony(
        store=memory,
        now=lambda: NOW,
        _spawn=lambda launch: launch.run(),
        _client_factory=lambda **_kwargs: FailingClient(),
    )

    assert result == CredentialCeremonyResult(False, expected)
    assert clob_slots_are_absent(memory)
    assert all(buffer.closed for buffer in response_buffers)


def test_rollback_integrity_failure_refuses_to_report_an_ordinary_create_failure() -> None:
    result = _launch_credential_ceremony(
        store=wallet_only_memory(RollbackRefusingStore),
        now=lambda: NOW,
        _spawn=lambda launch: launch.run(),
        _client_factory=lambda **_kwargs: CreateOnlyCredentialClient(),
    )

    assert result == CredentialCeremonyResult(False, "CREDENTIAL_ROLLBACK_FAILED")


def test_rollback_never_deletes_an_external_update_and_fails_closed() -> None:
    memory = wallet_only_memory(ExternalUpdateDuringRollbackStore)

    result = _launch_credential_ceremony(
        store=memory,
        now=lambda: NOW,
        _spawn=lambda launch: launch.run(),
        _client_factory=lambda **_kwargs: CreateOnlyCredentialClient(),
    )

    retained = memory.read_required(CLOB_SERVICE, CLOB_API_KEY_ACCOUNT, "verify")
    try:
        assert retained.use(bytes) == b"external-replacement"
    finally:
        retained.close()
    assert result == CredentialCeremonyResult(False, "CREDENTIAL_ROLLBACK_FAILED")


def test_create_parent_forks_before_any_secret_store_read() -> None:
    memory = wallet_only_memory()
    handed_out: list[SecretBuffer] = []

    class TrackingStore:
        def read_required(self, service: str, account: str, prompt: str) -> SecretBuffer:
            buffer = memory.read_required(service, account, prompt)
            handed_out.append(buffer)
            return buffer

        def write_protected(self, service: str, account: str, value: SecretBuffer) -> None:
            memory.write_protected(service, account, value)

        def delete(self, service: str, account: str) -> None:
            memory.delete(service, account)

    client = CreateOnlyCredentialClient()

    def spawn(launch: Any) -> None:
        assert handed_out == []
        launch.run()

    _launch_credential_ceremony(
        store=TrackingStore(),
        now=lambda: NOW,
        _spawn=spawn,
        _client_factory=lambda **_kwargs: client,
    )


def test_post_lock_partial_state_refuses_before_remote_create() -> None:
    memory = wallet_only_memory()
    child_started = False
    client = CreateOnlyCredentialClient()

    class PostLockPartialStore:
        def read_required(self, service: str, account: str, prompt: str) -> SecretBuffer:
            nonlocal child_started
            if child_started and account == CLOB_API_KEY_ACCOUNT:
                inserted = SecretBuffer.from_bytes(b"post-lock-external-update")
                try:
                    memory.write_protected(CLOB_SERVICE, CLOB_API_KEY_ACCOUNT, inserted)
                finally:
                    inserted.close()
            return memory.read_required(service, account, prompt)

        def create_protected(self, service: str, account: str, value: SecretBuffer) -> object:
            return memory.create_protected(service, account, value)

        def delete_created(self, creation: object) -> None:
            memory.delete_created(creation)  # type: ignore[arg-type]

    def spawn(launch: Any) -> None:
        nonlocal child_started
        child_started = True
        launch.run()

    result = _launch_credential_ceremony(
        store=PostLockPartialStore(),
        now=lambda: NOW,
        _spawn=spawn,
        _client_factory=lambda **_kwargs: client,
    )

    assert result == CredentialCeremonyResult(False, "CREDENTIALS_PARTIAL")
    assert client.calls == []


def _concurrent_ceremony_worker(started: Any, release: Any, count: Any, results: Any) -> None:
    memory = wallet_only_memory()

    class BlockingClient(CreateOnlyCredentialClient):
        def create_or_derive(
            self, *, operation: str, binding: AccountSignatureBinding
        ) -> dict[str, SecretBuffer]:
            with count.get_lock():
                count.value += 1
            started.set()
            assert release.wait(5)
            return super().create_or_derive(operation=operation, binding=binding)

    result = _launch_credential_ceremony(
        store=memory,
        now=lambda: NOW,
        _spawn=lambda launch: launch.run(),
        _client_factory=lambda **_kwargs: BlockingClient(),
    )
    results.put(result.code)


def test_concurrent_create_ceremonies_issue_one_remote_create() -> None:
    context = get_context("fork")
    started = context.Event()
    release = context.Event()
    count = context.Value("i", 0)
    results = context.Queue()
    worker_args = (started, release, count, results)
    first = context.Process(target=_concurrent_ceremony_worker, args=worker_args)
    second = context.Process(target=_concurrent_ceremony_worker, args=worker_args)
    first.start()
    assert started.wait(5)
    second.start()
    second.join(5)
    release.set()
    first.join(5)

    assert first.exitcode == 0
    assert second.exitcode == 0
    assert count.value == 1
    assert {results.get(timeout=1), results.get(timeout=1)} == {
        "CREATED",
        "CREDENTIALS_CREATE_IN_PROGRESS",
    }


def store(*, missing: str | set[str] | None = None, denied: str | None = None) -> Any:
    memory = InMemorySecretStore()
    missing_accounts = {missing} if type(missing) is str else missing or set()
    for account, value in VALUES.items():
        if account in missing_accounts:
            continue
        memory.write_protected(CLOB_SERVICE, account, SecretBuffer.from_bytes(value))
    if denied is None:
        return memory

    class Denying:
        """Reads succeed until the operator declines one item's unlock."""

        def read_required(self, service: str, account: str, prompt: str) -> SecretBuffer:
            if account == denied:
                raise SecretStoreError("SECRET_ACCESS_DENIED")
            return memory.read_required(service, account, prompt)

        def write_protected(self, service: str, account: str, value: SecretBuffer) -> None:
            memory.write_protected(service, account, value)

        def delete(self, service: str, account: str) -> None:
            memory.delete(service, account)

    return Denying()


def prompting_store(recorded: list[tuple[str, str, str]]) -> Any:
    memory = store()

    class Recording:
        def read_required(self, service: str, account: str, prompt: str) -> SecretBuffer:
            recorded.append((service, account, prompt))
            return memory.read_required(service, account, prompt)

        def write_protected(self, service: str, account: str, value: SecretBuffer) -> None:
            memory.write_protected(service, account, value)

        def delete(self, service: str, account: str) -> None:
            memory.delete(service, account)

    return Recording()


def test_every_launch_secret_is_read_once_behind_one_prompt() -> None:
    recorded: list[tuple[str, str, str]] = []
    captured: dict[str, Any] = {}

    def spawn(launch: ChildLaunch) -> int | None:
        captured["launch"] = launch
        return None

    channel = launch_signer_sidecar(
        store=prompting_store(recorded),
        service_factory=lambda secrets: secrets,
        spawn=spawn,
    )
    try:
        assert [account for _service, account, _prompt in recorded] == list(SECRET_ACCOUNTS)
        assert {prompt for _service, _account, prompt in recorded} == {UNLOCK_PROMPT}
        assert channel.child_pid is None
    finally:
        channel.close()


def test_the_child_reads_exactly_the_four_inherited_descriptors() -> None:
    loaded: dict[str, Any] = {}

    def spawn(launch: ChildLaunch) -> int | None:
        # Stand in for the sidecar: consume the descriptor contract the parent just wrote.
        loaded["material"] = read_secret_descriptors(*launch.secret_descriptors)
        return None

    channel = launch_signer_sidecar(
        store=store(), service_factory=lambda secrets: secrets, spawn=spawn
    )
    try:
        material = loaded["material"]
        assert bytes(material.private_key) == PRIVATE_KEY
        assert bytes(material.api_key) == API_KEY
        assert bytes(material.api_secret) == API_SECRET
        assert bytes(material.passphrase) == PASSPHRASE
        material.close()
        assert bytes(material.api_secret) == b"\x00" * len(API_SECRET)
    finally:
        channel.close()


def test_bootstrap_launches_wallet_only_and_reports_credentials_absent() -> None:
    loaded: dict[str, Any] = {}

    def spawn(launch: ChildLaunch) -> int | None:
        loaded["material"] = read_secret_descriptors(*launch.secret_descriptors)
        return None

    channel = launch_signer_sidecar(
        store=store(missing={"clob-api-key", "clob-api-secret", "clob-passphrase"}),
        service_factory=lambda secrets: secrets,
        spawn=spawn,
    )
    try:
        material = loaded["material"]
        assert channel.credentials_present is False
        assert bytes(material.private_key) == PRIVATE_KEY
        assert bytes(material.api_key) == b""
        material.close()
    finally:
        channel.close()


def test_bootstrap_reports_credentials_present_when_all_four_exist() -> None:
    channel = launch_signer_sidecar(
        store=store(), service_factory=lambda secrets: secrets, spawn=lambda launch: None
    )
    try:
        assert channel.credentials_present is True
    finally:
        channel.close()


def test_the_parent_keeps_no_secret_after_launching() -> None:
    memory = store()
    channel = launch_signer_sidecar(
        store=memory, service_factory=lambda secrets: secrets, spawn=lambda launch: None
    )
    try:
        # The parent's own buffers were closed; only the framed stream pair survives.
        assert channel.request_stream.writable()
        assert channel.response_stream.readable()
        assert not hasattr(channel, "secrets")
        assert "canary" not in repr(channel)
    finally:
        channel.close()


@pytest.mark.parametrize("account", sorted(VALUES))
def test_a_missing_launch_secret_refuses_to_start_a_sidecar(account: str) -> None:
    started: list[bool] = []

    with pytest.raises(SignerBootstrapError) as raised:
        launch_signer_sidecar(
            store=store(missing=account),
            service_factory=lambda secrets: secrets,
            spawn=lambda launch: started.append(True),  # type: ignore[return-value]
        )

    assert raised.value.code == "SECRET_ITEM_MISSING"
    assert started == []


def test_a_denied_unlock_refuses_to_start_a_sidecar() -> None:
    started: list[bool] = []

    with pytest.raises(SignerBootstrapError) as raised:
        launch_signer_sidecar(
            store=store(denied="clob-api-secret"),
            service_factory=lambda secrets: secrets,
            spawn=lambda launch: started.append(True),  # type: ignore[return-value]
        )

    assert raised.value.code == "SECRET_ACCESS_DENIED"
    assert started == []


def test_closing_the_channel_releases_both_streams() -> None:
    channel = launch_signer_sidecar(
        store=store(), service_factory=lambda secrets: secrets, spawn=lambda launch: None
    )

    channel.close()

    assert channel.request_stream.closed
    assert channel.response_stream.closed


def test_no_secret_travels_through_an_argument_or_the_environment() -> None:
    import inspect

    from polytrading.predictions.pilot import signer_bootstrap

    source = inspect.getsource(signer_bootstrap)

    assert "os.environ" not in source
    assert "subprocess" not in source
    assert "shell" not in source
    assert "security " not in source
    signature = inspect.signature(signer_bootstrap.launch_signer_sidecar)
    assert "private_key" not in signature.parameters
    assert "api_secret" not in signature.parameters


def test_the_bootstrap_never_writes_a_secret_to_a_file() -> None:
    import inspect

    from polytrading.predictions.pilot import signer_bootstrap

    source = inspect.getsource(signer_bootstrap)

    # The only file descriptor created here is the public, empty advisory lock used by the
    # credential child.  It has no secret-bearing path or content.
    assert "open(" not in source.replace("os.fdopen(", "").replace("os.open(", "")
    assert "Path(" not in source


def test_a_short_lived_store_failure_leaves_no_descriptor_behind(tmp_path: Any) -> None:
    before = _open_descriptor_count()

    with pytest.raises(SignerBootstrapError):
        launch_signer_sidecar(
            store=store(missing="clob-api-key"),
            service_factory=lambda secrets: secrets,
            spawn=lambda launch: None,
        )

    assert _open_descriptor_count() <= before + 1


def _open_descriptor_count() -> int:
    count = 0
    for candidate in range(3, 512):
        try:
            os.fstat(candidate)
        except OSError:
            continue
        count += 1
    return count
