from __future__ import annotations

import os
from typing import Any

import pytest

from polytrading.predictions.pilot.signer_bootstrap import (
    SECRET_ACCOUNTS,
    UNLOCK_PROMPT,
    ChildLaunch,
    SignerBootstrapError,
    launch_signer_sidecar,
)
from polytrading.predictions.polymarket_execution.keychain_macos import CLOB_SERVICE
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


def store(*, missing: str | None = None, denied: str | None = None) -> Any:
    memory = InMemorySecretStore()
    for account, value in VALUES.items():
        if account == missing:
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

    assert "open(" not in source.replace("os.fdopen(", "")
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
