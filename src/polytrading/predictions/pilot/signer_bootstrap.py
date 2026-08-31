"""Launch the signer sidecar with inherited descriptors, once per operator launch.

The parent reads the four secrets from the operating-system store, writes each into its own pipe,
closes its copies, and hands the read ends to the child. After this function returns, the parent
holds no secret bytes: only the two framed IPC streams the pilot talks to.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import BinaryIO, Final, Protocol

from polytrading.predictions.polymarket_execution.keychain_macos import (
    CLOB_API_KEY_ACCOUNT,
    CLOB_API_SECRET_ACCOUNT,
    CLOB_PASSPHRASE_ACCOUNT,
    CLOB_SERVICE,
    WALLET_PRIVATE_KEY_ACCOUNT,
)
from polytrading.predictions.polymarket_execution.secrets import (
    SecretBuffer,
    SecretMaterial,
    SecretStore,
    SecretStoreError,
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


class SignerBootstrapError(RuntimeError):
    """A refused signer launch, named by a stable code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class SignerServiceFactory(Protocol):
    def __call__(self, secrets: SecretMaterial) -> SignerService: ...


@dataclass(frozen=True, slots=True)
class ChildLaunch:
    """Exactly what a child needs, so a spawner never has to introspect a closure."""

    request_fd: int
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
