"""Launch the signer sidecar with inherited descriptors, once per operator launch.

The parent reads the four secrets from the operating-system store, writes each into its own pipe,
closes its copies, and hands the read ends to the child. After this function returns, the parent
holds no secret bytes: only the two framed IPC streams the pilot talks to.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import BinaryIO, Final, Protocol
from uuid import uuid4

from eth_account import Account

from polytrading.predictions.polymarket_execution.credential_client import (
    CredentialTransportError,
    HttpxCredentialClient,
)
from polytrading.predictions.polymarket_execution.credentials import (
    MAXIMUM_GRANT_LIFETIME,
    CredentialClient,
    CredentialFingerprint,
    CredentialProvisioner,
    CredentialProvisioningError,
    CredentialProvisioningGrant,
)
from polytrading.predictions.polymarket_execution.keychain_macos import (
    CLOB_API_KEY_ACCOUNT,
    CLOB_API_SECRET_ACCOUNT,
    CLOB_PASSPHRASE_ACCOUNT,
    CLOB_SERVICE,
    WALLET_PRIVATE_KEY_ACCOUNT,
)
from polytrading.predictions.polymarket_execution.protocol import (
    POLYMARKET_PILOT_PROTOCOL_VERSION,
    AccountSignatureBinding,
    bind_account_signature,
    load_protocol_snapshot,
)
from polytrading.predictions.polymarket_execution.routes import CREDENTIAL_ROUTE_SET_HASH
from polytrading.predictions.polymarket_execution.secrets import (
    SecretBoundaryError,
    SecretBuffer,
    SecretMaterial,
    SecretStore,
    SecretStoreError,
    read_secret_descriptors,
)
from polytrading.predictions.polymarket_execution.signer import (
    SignerService,
    run_signer_sidecar,
)

# The exact order the sidecar's descriptor contract expects.
SECRET_ACCOUNTS: Final = (
    WALLET_PRIVATE_KEY_ACCOUNT,
    CLOB_API_KEY_ACCOUNT,
    CLOB_API_SECRET_ACCOUNT,
    CLOB_PASSPHRASE_ACCOUNT,
)
UNLOCK_PROMPT: Final = "Unlock the Polymarket pilot wallet and credentials for this launch"
_LENGTH_HEADER_BYTES: Final = 4
MAXIMUM_SECRET_BYTES: Final = 4096
_MAXIMUM_CEREMONY_RESULT_BYTES: Final = 512
_CEREMONY_RESULT_CODES: Final = frozenset(
    {"CREATED", "SIGNER_BOOTSTRAP_FAILED", "CREDENTIAL_CREATE_FAILED", "CREDENTIAL_STORE_FAILED"}
)


class SignerBootstrapError(RuntimeError):
    """A refused signer launch, named by a stable code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class SignerServiceFactory(Protocol):
    def __call__(self, secrets: SecretMaterial) -> SignerService: ...


class CredentialClientFactory(Protocol):
    def __call__(
        self, *, private_key: bytearray, timestamp: Callable[[], str]
    ) -> CredentialClient: ...


@dataclass(frozen=True, slots=True)
class ChildLaunch:
    """Exactly what a child needs, so a spawner never has to introspect a closure."""

    request_fd: int
    response_fd: int
    secret_descriptors: tuple[int, int, int, int]
    run: Callable[[], None]


@dataclass(frozen=True, slots=True)
class CredentialCeremonyResult:
    """The complete public-only response the one-shot child may return."""

    ok: bool
    code: str
    account_fingerprint: str | None = None
    credential_fingerprint: str | None = None


@dataclass(frozen=True, slots=True)
class _CredentialChildLaunch:
    response_fd: int
    secret_descriptors: tuple[int, int, int, int]
    run: Callable[[], None]


@dataclass(frozen=True, slots=True)
class SignerChannel:
    """The parent's end of one launched sidecar. It carries streams, never secrets."""

    request_stream: BinaryIO
    response_stream: BinaryIO
    child_pid: int | None
    credentials_present: bool

    def close(self) -> None:
        for stream in (self.request_stream, self.response_stream):
            try:
                stream.close()
            except OSError:
                continue


def launch_signer_sidecar(
    *,
    store: SecretStore,
    service_factory: SignerServiceFactory,
    service: str = CLOB_SERVICE,
    prompt: str = UNLOCK_PROMPT,
    spawn: Callable[[ChildLaunch], int | None] | None = None,
    max_requests: int = 1024,
    max_lifetime_seconds: float = 300,
) -> SignerChannel:
    """Read the launch secrets once, hand them to a child, and keep only the IPC streams."""

    secrets = _read_secrets(store, service=service, prompt=prompt)
    credentials_present = len(secrets[1]) > 0
    secret_pipes = [os.pipe() for _ in SECRET_ACCOUNTS]
    request_pipe = os.pipe()
    response_pipe = os.pipe()
    try:
        for buffer, (_read_fd, write_fd) in zip(secrets, secret_pipes, strict=True):
            _write_framed_secret(write_fd, buffer)
    except BaseException:
        _close_all(secret_pipes, request_pipe, response_pipe)
        raise
    finally:
        # The parent's own copies die here, before any child is started.
        for buffer in secrets:
            buffer.close()
        for _read_fd, write_fd in secret_pipes:
            _close(write_fd)

    descriptors = tuple(read_fd for read_fd, _write_fd in secret_pipes)
    launch = ChildLaunch(
        request_fd=request_pipe[0],
        response_fd=response_pipe[1],
        secret_descriptors=descriptors,  # type: ignore[arg-type]
        run=lambda: _run_child(
            request_fd=request_pipe[0],
            response_fd=response_pipe[1],
            secret_descriptors=descriptors,
            service_factory=service_factory,
            max_requests=max_requests,
            max_lifetime_seconds=max_lifetime_seconds,
        ),
    )
    child = (spawn or _fork_child)(launch)
    # Whatever ran the child owns the read ends now; the parent keeps only its stream pair.
    for read_fd, _write_fd in secret_pipes:
        _close(read_fd)
    _close(request_pipe[0])
    _close(response_pipe[1])
    return SignerChannel(
        request_stream=os.fdopen(request_pipe[1], "wb", buffering=0),
        response_stream=os.fdopen(response_pipe[0], "rb", buffering=0),
        child_pid=child,
        credentials_present=credentials_present,
    )


def create_credentials_in_sidecar(
    *, store: SecretStore, now: Callable[[], datetime]
) -> CredentialFingerprint:
    """Run the fixed CREATE ceremony and return only its public fingerprint."""
    result = _launch_credential_ceremony(store=store, now=now)
    if not result.ok:
        raise SignerBootstrapError(result.code)
    if result.account_fingerprint is None or result.credential_fingerprint is None:
        raise SignerBootstrapError("SIGNER_BOOTSTRAP_FAILED")
    return CredentialFingerprint(
        account_fingerprint=result.account_fingerprint,
        credential_fingerprint=result.credential_fingerprint,
        operation="CREATE",
        result="CREATED",
    )


def _launch_credential_ceremony(
    *,
    store: SecretStore,
    now: Callable[[], datetime],
    _spawn: Callable[[_CredentialChildLaunch], int | None] | None = None,
    _client_factory: CredentialClientFactory | None = None,
) -> CredentialCeremonyResult:
    """Launch one fixed-operation child through the inherited four-descriptor contract."""
    secrets = _read_secrets(store, service=CLOB_SERVICE, prompt=UNLOCK_PROMPT)
    if any(len(buffer) > 0 for buffer in secrets[1:]):
        for buffer in secrets:
            buffer.close()
        raise SignerBootstrapError("CREDENTIALS_ALREADY_PRESENT")
    secret_pipes = [os.pipe() for _ in SECRET_ACCOUNTS]
    response_pipe = os.pipe()
    try:
        for buffer, (_read_fd, write_fd) in zip(secrets, secret_pipes, strict=True):
            _write_framed_secret(write_fd, buffer)
    except BaseException:
        _close_all(secret_pipes, response_pipe)
        raise
    finally:
        for buffer in secrets:
            buffer.close()
        for _read_fd, write_fd in secret_pipes:
            _close(write_fd)

    descriptors = tuple(read_fd for read_fd, _write_fd in secret_pipes)
    launch = _CredentialChildLaunch(
        response_fd=response_pipe[1],
        secret_descriptors=descriptors,  # type: ignore[arg-type]
        run=lambda: _run_credential_child(
            response_fd=response_pipe[1],
            secret_descriptors=descriptors,
            store=store,
            now=now,
            client_factory=_client_factory,
        ),
    )
    try:
        child_pid = (_spawn or _fork_credential_child)(launch)
    except BaseException:
        _close_all(secret_pipes, response_pipe)
        raise SignerBootstrapError("SIGNER_BOOTSTRAP_FAILED") from None
    for read_fd, _write_fd in secret_pipes:
        _close(read_fd)
    _close(response_pipe[1])
    try:
        return _read_credential_result(response_pipe[0])
    finally:
        _close(response_pipe[0])
        if child_pid is not None:
            with suppress(OSError):
                os.waitpid(child_pid, 0)


def _run_credential_child(
    *,
    response_fd: int,
    secret_descriptors: tuple[int, ...],
    store: SecretStore,
    now: Callable[[], datetime],
    client_factory: CredentialClientFactory | None,
) -> None:
    material: SecretMaterial | None = None
    client: CredentialClient | None = None
    result = CredentialCeremonyResult(False, "SIGNER_BOOTSTRAP_FAILED")
    try:
        material = read_secret_descriptors(*secret_descriptors)  # type: ignore[arg-type]
        if material.credentials_present:
            raise SecretBoundaryError("SECRET_MATERIAL_INVALID")
        observed_at = _credential_time(now)
        private_key = material.private_key
        address = Account.from_key(private_key).address
        snapshot = load_protocol_snapshot(version=POLYMARKET_PILOT_PROTOCOL_VERSION)
        binding = bind_account_signature(
            snapshot,
            signer_address=address,
            funder_address=address,
            signature_type=0,
            negative_risk=False,
            credential_route_hash=CREDENTIAL_ROUTE_SET_HASH,
        )
        account_fingerprint = _credential_account_fingerprint(binding)
        grant = CredentialProvisioningGrant(
            grant_id=uuid4(),
            grant_kind="CREDENTIAL_PROVISIONING",
            operation="CREATE",
            wallet_fingerprint=account_fingerprint,
            account_fingerprint=account_fingerprint,
            protocol_version=POLYMARKET_PILOT_PROTOCOL_VERSION,
            grant_digest=CREDENTIAL_ROUTE_SET_HASH,
            issued_at=observed_at,
            expires_at=observed_at + MAXIMUM_GRANT_LIFETIME,
        )
        def timestamp() -> str:
            return str(int(observed_at.timestamp()))

        client = (
            client_factory(private_key=private_key, timestamp=timestamp)
            if client_factory is not None
            else HttpxCredentialClient(private_key=private_key, timestamp=timestamp)
        )
        fingerprint = CredentialProvisioner(store, client).provision(
            grant, binding, now=observed_at
        )
        result = CredentialCeremonyResult(
            True,
            "CREATED",
            account_fingerprint=fingerprint.account_fingerprint,
            credential_fingerprint=fingerprint.credential_fingerprint,
        )
    except CredentialProvisioningError as error:
        code = (
            "CREDENTIAL_STORE_FAILED"
            if error.code == "CREDENTIAL_STORE_FAILED"
            else "CREDENTIAL_CREATE_FAILED"
        )
        result = CredentialCeremonyResult(False, code)
    except CredentialTransportError:
        result = CredentialCeremonyResult(False, "CREDENTIAL_CREATE_FAILED")
    except SecretBoundaryError:
        result = CredentialCeremonyResult(False, "SIGNER_BOOTSTRAP_FAILED")
    except BaseException:
        result = CredentialCeremonyResult(False, "CREDENTIAL_CREATE_FAILED")
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()
        if material is not None:
            material.close()
    try:
        _write_credential_result(response_fd, result)
    finally:
        _close(response_fd)


def _credential_time(now: Callable[[], datetime]) -> datetime:
    observed = now()
    if (
        not isinstance(observed, datetime)
        or observed.tzinfo is None
        or observed.utcoffset() is None
    ):
        raise ValueError("CREDENTIAL_CLOCK_INVALID")
    return observed.astimezone(UTC)


def _credential_account_fingerprint(binding: AccountSignatureBinding) -> str:
    return sha256(binding.signer_address.casefold().encode("ascii")).hexdigest()


def _write_credential_result(descriptor: int, result: CredentialCeremonyResult) -> None:
    payload = json.dumps(
        {
            "account_fingerprint": result.account_fingerprint,
            "code": result.code,
            "credential_fingerprint": result.credential_fingerprint,
            "ok": result.ok,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    if len(payload) > _MAXIMUM_CEREMONY_RESULT_BYTES:
        raise SignerBootstrapError("SIGNER_BOOTSTRAP_FAILED")
    _write_all(descriptor, len(payload).to_bytes(_LENGTH_HEADER_BYTES, "big"))
    _write_all(descriptor, payload)


def _write_all(descriptor: int, payload: bytes) -> None:
    written = 0
    while written < len(payload):
        written += os.write(descriptor, payload[written:])


def _read_credential_result(descriptor: int) -> CredentialCeremonyResult:
    try:
        header = _read_exact(descriptor, _LENGTH_HEADER_BYTES)
        length = int.from_bytes(header, "big")
        if not 0 < length <= _MAXIMUM_CEREMONY_RESULT_BYTES:
            raise ValueError
        payload = json.loads(_read_exact(descriptor, length).decode("ascii"))
        if type(payload) is not dict or set(payload) != {
            "account_fingerprint",
            "code",
            "credential_fingerprint",
            "ok",
        }:
            raise ValueError
        result = CredentialCeremonyResult(**payload)
        if type(result.ok) is not bool or result.code not in _CEREMONY_RESULT_CODES:
            raise ValueError
        fingerprints = (result.account_fingerprint, result.credential_fingerprint)
        if result.ok != all(
            type(value) is str
            and len(value) == 64
            and all(character in "0123456789abcdef" for character in value)
            for value in fingerprints
        ):
            raise ValueError
        return result
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        raise SignerBootstrapError("SIGNER_BOOTSTRAP_FAILED") from None


def _read_exact(descriptor: int, size: int) -> bytes:
    value = bytearray()
    while len(value) < size:
        chunk = os.read(descriptor, size - len(value))
        if not chunk:
            raise ValueError
        value.extend(chunk)
    return bytes(value)


def _fork_credential_child(launch: _CredentialChildLaunch) -> int:
    pid = os.fork()
    if pid == 0:  # pragma: no cover - the child never returns to the test process
        code = 0
        try:
            launch.run()
        except BaseException:
            code = 1
        finally:
            os._exit(code)
    return pid


def _read_secrets(store: SecretStore, *, service: str, prompt: str) -> list[SecretBuffer]:
    buffers: list[SecretBuffer] = []
    try:
        for index, account in enumerate(SECRET_ACCOUNTS):
            try:
                buffers.append(store.read_required(service, account, prompt))
            except SecretStoreError as error:
                if index > 0 and error.code == "SECRET_ITEM_MISSING":
                    buffers.append(SecretBuffer.empty())
                    continue
                raise
        clob = buffers[1:]
        if any(len(buffer) == 0 for buffer in clob) and any(len(buffer) > 0 for buffer in clob):
            raise SignerBootstrapError("SECRET_ITEM_MISSING")
    except SecretStoreError as error:
        for buffer in buffers:
            buffer.close()
        raise SignerBootstrapError(error.code) from error
    except BaseException:
        for buffer in buffers:
            buffer.close()
        raise
    return buffers


def _write_framed_secret(descriptor: int, buffer: SecretBuffer) -> None:
    length = len(buffer)
    if not 0 <= length <= MAXIMUM_SECRET_BYTES:
        raise SignerBootstrapError("SECRET_VALUE_INVALID")

    def _write(view: memoryview) -> None:
        header = length.to_bytes(_LENGTH_HEADER_BYTES, "big")
        os.write(descriptor, header)
        if length == 0:
            return
        written = 0
        while written < length:
            written += os.write(descriptor, view[written:])

    try:
        buffer.use(_write)  # type: ignore[arg-type]
    except OSError as error:
        raise SignerBootstrapError("SECRET_DESCRIPTOR_WRITE_FAILED") from error


def _run_child(
    *,
    request_fd: int,
    response_fd: int,
    secret_descriptors: tuple[int, ...],
    service_factory: SignerServiceFactory,
    max_requests: int,
    max_lifetime_seconds: float,
) -> None:
    run_signer_sidecar(
        request_fd=request_fd,
        response_fd=response_fd,
        secret_descriptors=secret_descriptors,  # type: ignore[arg-type]
        service_factory=service_factory,  # type: ignore[arg-type]
        max_requests=max_requests,
        max_lifetime_seconds=max_lifetime_seconds,
    )


def _fork_child(launch: ChildLaunch) -> int:
    """Fork so the sidecar inherits the descriptors directly; it never re-executes a CLI."""
    pid = os.fork()
    if pid == 0:  # pragma: no cover - the child never returns to the test process
        code = 0
        try:
            launch.run()
        except BaseException:
            code = 1
        finally:
            os._exit(code)
    return pid


def _close(descriptor: int) -> None:
    try:
        os.close(descriptor)
    except OSError:
        return


def _close_all(secret_pipes: list[tuple[int, int]], *pipes: tuple[int, int]) -> None:
    for read_fd, write_fd in [*secret_pipes, *pipes]:
        _close(read_fd)
        _close(write_fd)


__all__ = [
    "MAXIMUM_SECRET_BYTES",
    "SECRET_ACCOUNTS",
    "UNLOCK_PROMPT",
    "ChildLaunch",
    "SignerBootstrapError",
    "SignerChannel",
    "SignerServiceFactory",
    "launch_signer_sidecar",
]
