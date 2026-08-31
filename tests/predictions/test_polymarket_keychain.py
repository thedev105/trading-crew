from __future__ import annotations

import pytest

from polytrading.predictions.polymarket_execution.keychain_macos import (
    ALLOWED_ACCOUNTS,
    CLOB_API_KEY_ACCOUNT,
    CLOB_SERVICE,
    MAXIMUM_ITEM_BYTES,
    WALLET_PRIVATE_KEY_ACCOUNT,
    MacOSKeychainSecretStore,
)
from polytrading.predictions.polymarket_execution.secrets import (
    InMemorySecretStore,
    SecretBuffer,
    SecretStoreError,
)

SECRET = b"clob-api-key-canary"
ITEM_NOT_FOUND = -25300
DUPLICATE_ITEM = -25299
AUTH_FAILED = -25293
USER_CANCELED = -128
INTERACTION_NOT_ALLOWED = -25308
UNEXPECTED_STATUS = -50


class FakeKeychainLibrary:
    """A stand-in for Security.framework so no test can reach the operator's real Keychain."""

    def __init__(self, *, status: int | None = None, oversize: bool = False) -> None:
        self.items: dict[tuple[bytes, bytes], bytes] = {}
        self.status = status
        self.oversize = oversize
        self.calls: list[str] = []

    def find_generic_password(self, service: bytes, account: bytes) -> tuple[int, bytes]:
        self.calls.append("find")
        if self.status is not None:
            return self.status, b""
        if self.oversize:
            return 0, b"x" * (MAXIMUM_ITEM_BYTES + 1)
        stored = self.items.get((service, account))
        return (0, stored) if stored is not None else (ITEM_NOT_FOUND, b"")

    def add_generic_password(self, service: bytes, account: bytes, value: bytes) -> int:
        self.calls.append("add")
        if self.status is not None:
            return self.status
        if (service, account) in self.items:
            return DUPLICATE_ITEM
        self.items[(service, account)] = value
        return 0

    def update_generic_password(self, service: bytes, account: bytes, value: bytes) -> int:
        self.calls.append("update")
        if self.status is not None:
            return self.status
        self.items[(service, account)] = value
        return 0

    def delete_generic_password(self, service: bytes, account: bytes) -> int:
        self.calls.append("delete")
        if self.status is not None:
            return self.status
        return 0 if self.items.pop((service, account), None) is not None else ITEM_NOT_FOUND


def store(**overrides: object) -> MacOSKeychainSecretStore:
    arguments: dict[str, object] = {"library": FakeKeychainLibrary(), "platform": "darwin"}
    arguments.update(overrides)
    return MacOSKeychainSecretStore(**arguments)  # type: ignore[arg-type]


def test_an_unsupported_operating_system_fails_closed() -> None:
    for platform in ("linux", "win32", "freebsd"):
        with pytest.raises(SecretStoreError) as raised:
            store(platform=platform)
        assert raised.value.code == "SECRET_STORE_UNAVAILABLE"


def test_a_written_item_reads_back_as_a_buffer_never_a_string() -> None:
    keychain = store()
    keychain.write_protected(CLOB_SERVICE, CLOB_API_KEY_ACCOUNT, SecretBuffer.from_bytes(SECRET))
    value = keychain.read_required(CLOB_SERVICE, CLOB_API_KEY_ACCOUNT, "unlock the pilot wallet")

    assert isinstance(value, SecretBuffer)
    assert value.use(lambda view: bytes(view)) == SECRET
    assert SECRET.decode() not in repr(value)


@pytest.mark.parametrize("prefix", (b"", b"0x"))
def test_a_wallet_hex_key_is_normalized_to_the_signer_key_bytes(prefix: bytes) -> None:
    keychain = store()
    private_key = bytes(range(32))
    keychain.write_protected(
        CLOB_SERVICE,
        WALLET_PRIVATE_KEY_ACCOUNT,
        SecretBuffer.from_bytes(prefix + private_key.hex().encode("ascii")),
    )

    value = keychain.read_required(CLOB_SERVICE, WALLET_PRIVATE_KEY_ACCOUNT, "unlock")

    assert value.use(lambda view: bytes(view)) == private_key


def test_writing_twice_updates_rather_than_duplicating() -> None:
    library = FakeKeychainLibrary()
    keychain = store(library=library)
    keychain.write_protected(CLOB_SERVICE, CLOB_API_KEY_ACCOUNT, SecretBuffer.from_bytes(b"one"))
    keychain.write_protected(CLOB_SERVICE, CLOB_API_KEY_ACCOUNT, SecretBuffer.from_bytes(b"two"))

    assert library.calls == ["add", "add", "update"]
    assert (
        keychain.read_required(CLOB_SERVICE, CLOB_API_KEY_ACCOUNT, "unlock").use(
            lambda view: bytes(view)
        )
        == b"two"
    )


@pytest.mark.parametrize(
    ("service", "account"),
    [
        ("another.service", CLOB_API_KEY_ACCOUNT),
        (CLOB_SERVICE, "wallet-recovery-phrase"),
        (CLOB_SERVICE, ""),
    ],
)
def test_only_the_exact_label_set_is_addressable(service: str, account: str) -> None:
    keychain = store()
    for operation in (
        lambda: keychain.read_required(service, account, "unlock"),
        lambda: keychain.write_protected(service, account, SecretBuffer.from_bytes(SECRET)),
        lambda: keychain.delete(service, account),
    ):
        with pytest.raises(SecretStoreError) as raised:
            operation()
        assert raised.value.code == "SECRET_LABEL_INVALID"


def test_the_allowed_account_set_is_exactly_the_pilot_items() -> None:
    assert {
        "wallet-private-key",
        "clob-api-key",
        "clob-api-secret",
        "clob-passphrase",
    } == ALLOWED_ACCOUNTS


def test_a_read_requires_an_operation_prompt() -> None:
    keychain = store()
    with pytest.raises(SecretStoreError) as raised:
        keychain.read_required(CLOB_SERVICE, CLOB_API_KEY_ACCOUNT, "")
    assert raised.value.code == "SECRET_LABEL_INVALID"


def test_a_missing_item_is_reported_without_inventing_a_value() -> None:
    with pytest.raises(SecretStoreError) as raised:
        store().read_required(CLOB_SERVICE, CLOB_API_KEY_ACCOUNT, "unlock")
    assert raised.value.code == "SECRET_ITEM_MISSING"


@pytest.mark.parametrize("status", [AUTH_FAILED, USER_CANCELED, INTERACTION_NOT_ALLOWED])
def test_a_denied_unlock_is_not_an_empty_secret(status: int) -> None:
    with pytest.raises(SecretStoreError) as raised:
        store(library=FakeKeychainLibrary(status=status)).read_required(
            CLOB_SERVICE, CLOB_API_KEY_ACCOUNT, "unlock"
        )
    assert raised.value.code == "SECRET_ACCESS_DENIED"


def test_an_unexpected_os_status_is_translated_not_leaked() -> None:
    with pytest.raises(SecretStoreError) as raised:
        store(library=FakeKeychainLibrary(status=UNEXPECTED_STATUS)).read_required(
            CLOB_SERVICE, CLOB_API_KEY_ACCOUNT, "unlock"
        )
    assert raised.value.code == "SECRET_STORE_UNAVAILABLE"
    assert str(UNEXPECTED_STATUS) not in str(raised.value)


def test_an_oversize_item_is_refused() -> None:
    with pytest.raises(SecretStoreError) as raised:
        store(library=FakeKeychainLibrary(oversize=True)).read_required(
            CLOB_SERVICE, CLOB_API_KEY_ACCOUNT, "unlock"
        )
    assert raised.value.code == "SECRET_VALUE_INVALID"


def test_deleting_a_missing_item_is_not_an_error() -> None:
    store().delete(CLOB_SERVICE, CLOB_API_KEY_ACCOUNT)


def test_a_closed_buffer_can_neither_be_written_nor_read() -> None:
    buffer = SecretBuffer.from_bytes(SECRET)
    buffer.close()

    assert len(buffer) == 0
    with pytest.raises(SecretStoreError) as raised:
        store().write_protected(CLOB_SERVICE, CLOB_API_KEY_ACCOUNT, buffer)
    assert raised.value.code == "SECRET_VALUE_INVALID"
    with pytest.raises(SecretStoreError):
        buffer.use(lambda view: bytes(view))


def test_secret_buffers_refuse_copy_comparison_and_serialization() -> None:
    import copy
    import pickle

    buffer = SecretBuffer.from_bytes(SECRET)
    for operation in (
        lambda: copy.copy(buffer),
        lambda: copy.deepcopy(buffer),
        lambda: pickle.dumps(buffer),
        lambda: buffer == SecretBuffer.from_bytes(SECRET),
        lambda: hash(buffer),
    ):
        with pytest.raises(ValueError):
            operation()


def test_the_in_memory_store_enforces_the_same_rules() -> None:
    memory = InMemorySecretStore(denied=frozenset({(CLOB_SERVICE, "clob-passphrase")}))
    memory.write_protected(CLOB_SERVICE, CLOB_API_KEY_ACCOUNT, SecretBuffer.from_bytes(SECRET))

    assert (
        memory.read_required(CLOB_SERVICE, CLOB_API_KEY_ACCOUNT, "unlock").use(
            lambda view: bytes(view)
        )
        == SECRET
    )
    with pytest.raises(SecretStoreError) as denied:
        memory.read_required(CLOB_SERVICE, "clob-passphrase", "unlock")
    assert denied.value.code == "SECRET_ACCESS_DENIED"
    with pytest.raises(SecretStoreError) as missing:
        memory.read_required(CLOB_SERVICE, "clob-api-secret", "unlock")
    assert missing.value.code == "SECRET_ITEM_MISSING"
    memory.close()
    with pytest.raises(SecretStoreError):
        memory.read_required(CLOB_SERVICE, CLOB_API_KEY_ACCOUNT, "unlock")
