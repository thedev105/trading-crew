"""One-time CLOB credential provisioning, entirely off the execution surface.

A credential ceremony runs under its own short-lived grant, calls exactly one allowlisted L1
operation, writes every returned secret straight into the operating-system secret store, and
returns fingerprints. The browser, coordinator, database, and logs never see a credential value.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from typing import Literal, Protocol
from uuid import UUID

from polytrading.predictions.polymarket_execution.keychain_macos import (
    CLOB_API_KEY_ACCOUNT,
    CLOB_API_SECRET_ACCOUNT,
    CLOB_PASSPHRASE_ACCOUNT,
    CLOB_SERVICE,
)
from polytrading.predictions.polymarket_execution.protocol import (
    POLYMARKET_PILOT_PROTOCOL_VERSION,
    AccountSignatureBinding,
)
from polytrading.predictions.polymarket_execution.secrets import (
    SecretBuffer,
    SecretCreation,
    SecretStore,
    SecretStoreError,
)

# Spec section 4.4: the provisioning grant lives for at most 60 seconds and is single use.
MAXIMUM_GRANT_LIFETIME = timedelta(seconds=60)
CredentialOperation = Literal["CREATE", "DERIVE"]
_CREDENTIAL_ACCOUNTS = (
    CLOB_API_KEY_ACCOUNT,
    CLOB_API_SECRET_ACCOUNT,
    CLOB_PASSPHRASE_ACCOUNT,
)
_STORE_PREFLIGHT_PROMPT = "Confirm the CLOB credential slots are empty for this create"

CredentialProvisioningCode = Literal[
    "GRANT_INVALID",
    "GRANT_EXPIRED",
    "GRANT_ALREADY_USED",
    "GRANT_WALLET_MISMATCH",
    "GRANT_ACCOUNT_MISMATCH",
    "GRANT_PROTOCOL_MISMATCH",
    "GRANT_OPERATION_NOT_ALLOWED",
    "GRANT_IS_NOT_A_CREDENTIAL_GRANT",
    "CREDENTIAL_RESPONSE_INVALID",
    "CREDENTIAL_STORE_FAILED",
    "CREDENTIAL_ROLLBACK_FAILED",
]


class CredentialProvisioningError(ValueError):
    """A refused credential ceremony, named by a stable code and carrying no secret."""

    def __init__(self, code: CredentialProvisioningCode) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class CredentialProvisioningGrant:
    """The one-time authority for a single create-or-derive call.

    It is deliberately not an execution capability: it cannot submit, cancel, heartbeat, or reach
    any other route, and it cannot be converted into trading authority.
    """

    grant_id: UUID
    grant_kind: Literal["CREDENTIAL_PROVISIONING"]
    operation: CredentialOperation
    wallet_fingerprint: str
    account_fingerprint: str
    protocol_version: str
    grant_digest: str
    issued_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class CredentialFingerprint:
    """Everything a caller may learn about a provisioned credential."""

    account_fingerprint: str
    credential_fingerprint: str
    operation: CredentialOperation
    result: Literal["CREATED", "DERIVED"]

    def __repr__(self) -> str:
        return (
            "CredentialFingerprint("
            f"account={self.account_fingerprint[:8]}…, "
            f"credential={self.credential_fingerprint[:8]}…, "
            f"result={self.result})"
        )


class CredentialClient(Protocol):
    """The single allowlisted L1 call. Implementations return owned buffers, never strings."""

    def create_or_derive(
        self, *, operation: CredentialOperation, binding: AccountSignatureBinding
    ) -> dict[str, SecretBuffer]: ...


def credential_fingerprint(api_key: SecretBuffer) -> str:
    """Fingerprint a credential without ever copying it into a string."""
    return str(api_key.use(lambda view: sha256(bytes(view)).hexdigest()))


class CredentialProvisioner:
    """Runs at most one credential ceremony per grant, inside the signer process."""

    __slots__ = ("_client", "_service", "_store", "_used_grants")

    def __init__(
        self,
        store: SecretStore,
        client: CredentialClient,
        *,
        service: str = CLOB_SERVICE,
    ) -> None:
        self._store = store
        self._client = client
        self._service = service
        self._used_grants: set[UUID] = set()

    def provision(
        self,
        grant: CredentialProvisioningGrant,
        binding: AccountSignatureBinding,
        *,
        now: datetime,
    ) -> CredentialFingerprint:
        self._require_valid_grant(grant, binding, now=now)
        self._used_grants.add(grant.grant_id)
        returned = self._client.create_or_derive(operation=grant.operation, binding=binding)
        try:
            self._require_complete_response(returned)
            fingerprint = credential_fingerprint(returned[CLOB_API_KEY_ACCOUNT])
            self._store_atomically(returned)
        finally:
            for buffer in returned.values():
                buffer.close()
        return CredentialFingerprint(
            account_fingerprint=grant.account_fingerprint,
            credential_fingerprint=fingerprint,
            operation=grant.operation,
            result="CREATED" if grant.operation == "CREATE" else "DERIVED",
        )

    def _require_valid_grant(
        self,
        grant: CredentialProvisioningGrant,
        binding: AccountSignatureBinding,
        *,
        now: datetime,
    ) -> None:
        if type(grant) is not CredentialProvisioningGrant:
            raise CredentialProvisioningError("GRANT_IS_NOT_A_CREDENTIAL_GRANT")
        if grant.grant_kind != "CREDENTIAL_PROVISIONING":
            raise CredentialProvisioningError("GRANT_IS_NOT_A_CREDENTIAL_GRANT")
        if grant.operation not in ("CREATE", "DERIVE"):
            raise CredentialProvisioningError("GRANT_OPERATION_NOT_ALLOWED")
        if len(grant.grant_digest) != 64 or grant.expires_at <= grant.issued_at:
            raise CredentialProvisioningError("GRANT_INVALID")
        if grant.expires_at - grant.issued_at > MAXIMUM_GRANT_LIFETIME:
            raise CredentialProvisioningError("GRANT_INVALID")
        if grant.grant_id in self._used_grants:
            raise CredentialProvisioningError("GRANT_ALREADY_USED")
        if now < grant.issued_at or now >= grant.expires_at:
            raise CredentialProvisioningError("GRANT_EXPIRED")
        if grant.protocol_version != POLYMARKET_PILOT_PROTOCOL_VERSION:
            raise CredentialProvisioningError("GRANT_PROTOCOL_MISMATCH")
        if binding.protocol_version != grant.protocol_version:
            raise CredentialProvisioningError("GRANT_PROTOCOL_MISMATCH")
        if _address_fingerprint(binding.funder_address) != grant.wallet_fingerprint:
            raise CredentialProvisioningError("GRANT_WALLET_MISMATCH")
        if _address_fingerprint(binding.signer_address) != grant.account_fingerprint:
            raise CredentialProvisioningError("GRANT_ACCOUNT_MISMATCH")

    @staticmethod
    def _require_complete_response(returned: dict[str, SecretBuffer]) -> None:
        if set(returned) != set(_CREDENTIAL_ACCOUNTS) or any(
            type(buffer) is not SecretBuffer or buffer.closed or len(buffer) == 0
            for buffer in returned.values()
        ):
            raise CredentialProvisioningError("CREDENTIAL_RESPONSE_INVALID")

    def _store_atomically(self, returned: dict[str, SecretBuffer]) -> None:
        """Write all three fields or none: a partial credential is worse than no credential."""
        self._require_empty_store()
        created: list[SecretCreation] = []
        try:
            for account in _CREDENTIAL_ACCOUNTS:
                created.append(
                    self._store.create_protected(self._service, account, returned[account])
                )
        except (SecretStoreError, OSError) as error:
            rollback_failed = False
            for creation in created:
                try:
                    self._store.delete_created(creation)
                except SecretStoreError:
                    # A non-cooperating Keychain writer can replace a ceremony-created item
                    # between add and rollback.  Its opaque creation ticket then refuses to
                    # delete the replacement; preserve it and fail closed rather than claim
                    # all-or-none success that the local store cannot guarantee.
                    rollback_failed = True
            if rollback_failed:
                raise CredentialProvisioningError("CREDENTIAL_ROLLBACK_FAILED") from error
            raise CredentialProvisioningError("CREDENTIAL_STORE_FAILED") from error

    def _require_empty_store(self) -> None:
        """Establish rollback ownership before any ceremony-owned write occurs."""
        for account in _CREDENTIAL_ACCOUNTS:
            try:
                existing = self._store.read_required(
                    self._service, account, _STORE_PREFLIGHT_PROMPT
                )
            except SecretStoreError as error:
                if error.code == "SECRET_ITEM_MISSING":
                    continue
                raise CredentialProvisioningError("CREDENTIAL_STORE_FAILED") from error
            else:
                existing.close()
                raise CredentialProvisioningError("CREDENTIAL_STORE_FAILED")


def _address_fingerprint(address: str) -> str:
    return sha256(address.casefold().encode("ascii")).hexdigest()


def provision_credentials(
    grant: CredentialProvisioningGrant,
    account_model: AccountSignatureBinding,
    *,
    store: SecretStore,
    client: CredentialClient,
    now: datetime,
) -> CredentialFingerprint:
    """Run one credential ceremony and return only fingerprints."""
    return CredentialProvisioner(store, client).provision(grant, account_model, now=now)


__all__ = [
    "CredentialClient",
    "CredentialFingerprint",
    "CredentialProvisioner",
    "CredentialProvisioningError",
    "CredentialProvisioningGrant",
    "credential_fingerprint",
    "provision_credentials",
]
