"""Platform-passkey ceremonies bound to one exact local action.

Every assertion is verified against one fixed loopback origin, RP ID ``localhost``, one browser
session, and one unused action challenge derived from the approved action itself. Only public
credential data and digests survive verification -- raw assertion bytes stay ephemeral.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from hashlib import sha256
from typing import Annotated, Any, Literal, Protocol
from uuid import UUID

import webauthn
from pydantic import Field, StringConstraints
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from polytrading.predictions.domain import Sha256
from polytrading.predictions.execution.models import canonical_execution_hash
from polytrading.predictions.pilot.models import (
    AuthorizationChallenge,
    PilotRecord,
    UtcTimestamp,
)

RP_ID = "localhost"
RP_NAME = "Polymarket Local Pilot"
MAXIMUM_CHALLENGE_AGE = timedelta(minutes=5)

NonEmptyString = Annotated[str, StringConstraints(min_length=1)]
Base64Url = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]+$", max_length=4096)]

PasskeyCode = Literal[
    "ORIGIN_MISMATCH",
    "RP_ID_MISMATCH",
    "BROWSER_SESSION_MISMATCH",
    "CHALLENGE_UNKNOWN",
    "CHALLENGE_REPLAYED",
    "CHALLENGE_EXPIRED",
    "ACTION_DIGEST_MISMATCH",
    "USER_VERIFICATION_REQUIRED",
    "CREDENTIAL_UNKNOWN",
    "CREDENTIAL_REGISTRY_NOT_EMPTY",
    "WALLET_NOT_UNLOCKED",
    "EXISTING_CREDENTIAL_ASSERTION_REQUIRED",
    "SIGN_COUNT_REGRESSED",
    "ASSERTION_INVALID",
]


class PasskeyError(ValueError):
    """A passkey ceremony the pilot refuses, named by a stable code."""

    def __init__(self, code: PasskeyCode, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code


def pilot_origin(port: int) -> str:
    """The one origin this pilot accepts; 127.0.0.1 is a different origin and is refused."""
    if type(port) is not int or not 1 <= port <= 65535:
        raise PasskeyError("ORIGIN_MISMATCH", f"port {port!r} is not a loopback service port")
    return f"http://localhost:{port}"


def action_challenge_digest(challenge: AuthorizationChallenge) -> Sha256:
    """Derive the exact action digest a passkey assertion must sign over."""
    return canonical_execution_hash(
        {
            "account_fingerprint": challenge.account_fingerprint,
            "allowed_operations": [item.value for item in challenge.allowed_operations],
            "browser_session_hash": challenge.browser_session_hash,
            "ceiling_hash": challenge.ceiling_hash,
            "challenge_id": str(challenge.challenge_id),
            "confirmation_text_hash": challenge.confirmation_text_hash,
            "credential_id_hash": challenge.credential_id_hash,
            "evidence_hashes": list(challenge.evidence_hashes),
            "expires_at": challenge.expires_at.isoformat(),
            "grant_kind": challenge.grant_kind.value,
            "mode": challenge.mode.value,
            "nonce": challenge.nonce,
            "not_before": challenge.not_before.isoformat(),
            "policy_id": str(challenge.policy_id),
            "recovery_operations": [item.value for item in challenge.recovery_operations],
            "requested_limits_hash": challenge.requested_limits_hash,
            "target_id": str(challenge.target_id),
            "wallet_fingerprint": challenge.wallet_fingerprint,
        }
    )


class RegisteredCredential(PilotRecord):
    """Public data for one registered platform credential."""

    schema_version: Literal[1]
    credential_id: Base64Url
    credential_id_hash: Sha256
    public_credential_key: Base64Url
    sign_count: Annotated[int, Field(ge=0)]
    account_fingerprint: Sha256
    wallet_fingerprint: Sha256
    registered_at: UtcTimestamp


class VerifiedOperatorAssertion(PilotRecord):
    """What survives a successful assertion: digests, never assertion bytes."""

    schema_version: Literal[1]
    challenge_id: UUID
    credential_id_hash: Sha256
    browser_session_hash: Sha256
    account_fingerprint: Sha256
    action_digest: Sha256
    assertion_digest: Sha256
    sign_count: Annotated[int, Field(ge=0)]
    user_verified: Literal[True]
    verified_at: UtcTimestamp


class PasskeyService(Protocol):
    """The passkey port the control server depends on."""

    def registration_options(
        self, *, account_fingerprint: str, wallet_unlocked: bool
    ) -> dict[str, Any]: ...

    def complete_registration(
        self,
        *,
        credential: dict[str, Any],
        account_fingerprint: str,
        wallet_fingerprint: str,
        registered_at: datetime,
    ) -> RegisteredCredential: ...

    def authentication_options(self, challenge: AuthorizationChallenge) -> dict[str, Any]: ...

    def verify(
        self,
        *,
        credential: dict[str, Any],
        challenge: AuthorizationChallenge,
        browser_session_hash: str,
        origin: str,
        rp_id: str,
        verified_at: datetime,
    ) -> VerifiedOperatorAssertion: ...


class _CeremonyState:
    """One launch's credential registry and single-use challenge ledger."""

    def __init__(self) -> None:
        self.credentials: dict[str, RegisteredCredential] = {}
        self.open_challenges: dict[UUID, tuple[bytes, datetime]] = {}
        self.spent_challenges: set[UUID] = set()

    def open(self, challenge: AuthorizationChallenge, digest: Sha256) -> bytes:
        if challenge.challenge_id in self.spent_challenges:
            raise PasskeyError("CHALLENGE_REPLAYED", "this challenge was already used")
        material = bytes.fromhex(digest)
        self.open_challenges[challenge.challenge_id] = (material, challenge.expires_at)
        return material

    def spend(self, challenge: AuthorizationChallenge, verified_at: datetime) -> bytes:
        if challenge.challenge_id in self.spent_challenges:
            raise PasskeyError("CHALLENGE_REPLAYED", "this challenge was already used")
        entry = self.open_challenges.pop(challenge.challenge_id, None)
        if entry is None:
            raise PasskeyError("CHALLENGE_UNKNOWN", "no open challenge for this action")
        material, expires_at = entry
        if verified_at > expires_at or verified_at + MAXIMUM_CHALLENGE_AGE < expires_at:
            raise PasskeyError("CHALLENGE_EXPIRED", "the action challenge is no longer valid")
        self.spent_challenges.add(challenge.challenge_id)
        return material


class _BasePasskeyService:
    """Origin, session, and challenge rules shared by the real and fake verifiers."""

    def __init__(self, *, port: int) -> None:
        self._origin = pilot_origin(port)
        self._state = _CeremonyState()

    @property
    def origin(self) -> str:
        return self._origin

    @property
    def credentials(self) -> tuple[RegisteredCredential, ...]:
        return tuple(self._state.credentials.values())

    def _require_exact_binding(
        self,
        *,
        challenge: AuthorizationChallenge,
        browser_session_hash: str,
        origin: str,
        rp_id: str,
    ) -> RegisteredCredential:
        if origin != self._origin:
            raise PasskeyError("ORIGIN_MISMATCH", f"{origin} is not {self._origin}")
        if rp_id != RP_ID:
            raise PasskeyError("RP_ID_MISMATCH", f"{rp_id} is not {RP_ID}")
        if browser_session_hash != challenge.browser_session_hash:
            raise PasskeyError(
                "BROWSER_SESSION_MISMATCH", "the assertion belongs to another browser session"
            )
        for credential in self._state.credentials.values():
            if credential.credential_id_hash == challenge.credential_id_hash:
                return credential
        raise PasskeyError("CREDENTIAL_UNKNOWN", "no registered credential for this challenge")

    def _register(
        self,
        *,
        credential_id: str,
        public_credential_key: str,
        sign_count: int,
        account_fingerprint: str,
        wallet_fingerprint: str,
        wallet_unlocked: bool,
        registered_at: datetime,
    ) -> RegisteredCredential:
        if not wallet_unlocked:
            raise PasskeyError(
                "WALLET_NOT_UNLOCKED", "registration requires the unlocked dedicated wallet"
            )
        record = RegisteredCredential(
            schema_version=1,
            credential_id=credential_id,
            credential_id_hash=sha256(credential_id.encode("ascii")).hexdigest(),
            public_credential_key=public_credential_key,
            sign_count=sign_count,
            account_fingerprint=account_fingerprint,
            wallet_fingerprint=wallet_fingerprint,
            registered_at=registered_at,
        )
        self._state.credentials[record.credential_id] = record
        return record

    def require_empty_registry(self) -> None:
        if self._state.credentials:
            raise PasskeyError(
                "CREDENTIAL_REGISTRY_NOT_EMPTY",
                "replacing a credential requires an assertion from the existing credential",
            )

    def _verified_assertion(
        self,
        *,
        challenge: AuthorizationChallenge,
        credential: RegisteredCredential,
        assertion_bytes: bytes,
        sign_count: int,
        user_verified: bool,
        verified_at: datetime,
    ) -> VerifiedOperatorAssertion:
        if not user_verified:
            raise PasskeyError(
                "USER_VERIFICATION_REQUIRED", "the authenticator did not verify the operator"
            )
        if sign_count < credential.sign_count:
            raise PasskeyError("SIGN_COUNT_REGRESSED", "authenticator sign count moved backwards")
        self._state.credentials[credential.credential_id] = credential.model_copy(
            update={"sign_count": sign_count}
        )
        return VerifiedOperatorAssertion(
            schema_version=1,
            challenge_id=challenge.challenge_id,
            credential_id_hash=credential.credential_id_hash,
            browser_session_hash=challenge.browser_session_hash,
            account_fingerprint=challenge.account_fingerprint,
            action_digest=action_challenge_digest(challenge),
            assertion_digest=sha256(assertion_bytes).hexdigest(),
            sign_count=sign_count,
            user_verified=True,
            verified_at=verified_at,
        )


class PyWebAuthnPasskeyService(_BasePasskeyService):
    """The production verifier: webauthn 3.0.0 with user verification always required."""

    def registration_options(
        self, *, account_fingerprint: str, wallet_unlocked: bool
    ) -> dict[str, Any]:
        if not wallet_unlocked:
            raise PasskeyError(
                "WALLET_NOT_UNLOCKED", "registration requires the unlocked dedicated wallet"
            )
        self.require_empty_registry()
        options = webauthn.generate_registration_options(
            rp_id=RP_ID,
            rp_name=RP_NAME,
            user_name=account_fingerprint,
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.REQUIRED,
                user_verification=UserVerificationRequirement.REQUIRED,
            ),
        )
        self._registration_challenge = options.challenge
        return {"options": webauthn.options_to_json(options)}

    def complete_registration(
        self,
        *,
        credential: dict[str, Any],
        account_fingerprint: str,
        wallet_fingerprint: str,
        registered_at: datetime,
    ) -> RegisteredCredential:
        try:
            verified = webauthn.verify_registration_response(
                credential=credential,
                expected_challenge=self._registration_challenge,
                expected_rp_id=RP_ID,
                expected_origin=self._origin,
                require_user_verification=True,
            )
        except Exception as error:  # webauthn raises its own exception hierarchy
            raise PasskeyError("ASSERTION_INVALID", "registration response failed") from error
        return self._register(
            credential_id=webauthn.helpers.bytes_to_base64url(verified.credential_id),
            public_credential_key=webauthn.helpers.bytes_to_base64url(
                verified.credential_public_key
            ),
            sign_count=verified.sign_count,
            account_fingerprint=account_fingerprint,
            wallet_fingerprint=wallet_fingerprint,
            wallet_unlocked=True,
            registered_at=registered_at,
        )

    def authentication_options(self, challenge: AuthorizationChallenge) -> dict[str, Any]:
        material = self._state.open(challenge, action_challenge_digest(challenge))
        options = webauthn.generate_authentication_options(
            rp_id=RP_ID,
            challenge=material,
            allow_credentials=[
                PublicKeyCredentialDescriptor(
                    id=webauthn.base64url_to_bytes(credential.credential_id)
                )
                for credential in self._state.credentials.values()
                if credential.credential_id_hash == challenge.credential_id_hash
            ],
            user_verification=UserVerificationRequirement.REQUIRED,
        )
        return {"options": webauthn.options_to_json(options)}

    def verify(
        self,
        *,
        credential: dict[str, Any],
        challenge: AuthorizationChallenge,
        browser_session_hash: str,
        origin: str,
        rp_id: str,
        verified_at: datetime,
    ) -> VerifiedOperatorAssertion:
        registered = self._require_exact_binding(
            challenge=challenge,
            browser_session_hash=browser_session_hash,
            origin=origin,
            rp_id=rp_id,
        )
        material = self._state.spend(challenge, verified_at)
        try:
            verified = webauthn.verify_authentication_response(
                credential=credential,
                expected_challenge=material,
                expected_rp_id=RP_ID,
                expected_origin=self._origin,
                credential_public_key=webauthn.base64url_to_bytes(registered.public_credential_key),
                credential_current_sign_count=registered.sign_count,
                require_user_verification=True,
            )
        except Exception as error:  # webauthn raises its own exception hierarchy
            raise PasskeyError("ASSERTION_INVALID", "assertion verification failed") from error
        return self._verified_assertion(
            challenge=challenge,
            credential=registered,
            assertion_bytes=material,
            sign_count=verified.new_sign_count,
            user_verified=True,
            verified_at=verified_at,
        )


class FakePasskeyService(_BasePasskeyService):
    """A test verifier that enforces the same origin, session, and replay rules.

    It exists so automated tests can exercise every ceremony boundary without an authenticator;
    it never signs or verifies real assertion bytes.
    """

    def registration_options(
        self, *, account_fingerprint: str, wallet_unlocked: bool
    ) -> dict[str, Any]:
        if not wallet_unlocked:
            raise PasskeyError(
                "WALLET_NOT_UNLOCKED", "registration requires the unlocked dedicated wallet"
            )
        self.require_empty_registry()
        return {"rp_id": RP_ID, "origin": self._origin, "user_verification": "required"}

    def complete_registration(
        self,
        *,
        credential: dict[str, Any],
        account_fingerprint: str,
        wallet_fingerprint: str,
        registered_at: datetime,
    ) -> RegisteredCredential:
        return self._register(
            credential_id=str(credential["id"]),
            public_credential_key=str(credential["public_key"]),
            sign_count=int(credential.get("sign_count", 0)),
            account_fingerprint=account_fingerprint,
            wallet_fingerprint=wallet_fingerprint,
            wallet_unlocked=bool(credential.get("wallet_unlocked", True)),
            registered_at=registered_at,
        )

    def authentication_options(self, challenge: AuthorizationChallenge) -> dict[str, Any]:
        material = self._state.open(challenge, action_challenge_digest(challenge))
        return {"rp_id": RP_ID, "origin": self._origin, "challenge": material.hex()}

    def verify(
        self,
        *,
        credential: dict[str, Any],
        challenge: AuthorizationChallenge,
        browser_session_hash: str,
        origin: str,
        rp_id: str,
        verified_at: datetime,
    ) -> VerifiedOperatorAssertion:
        registered = self._require_exact_binding(
            challenge=challenge,
            browser_session_hash=browser_session_hash,
            origin=origin,
            rp_id=rp_id,
        )
        material = self._state.spend(challenge, verified_at)
        signed_digest = str(credential.get("action_digest", ""))
        if signed_digest != action_challenge_digest(challenge):
            raise PasskeyError("ACTION_DIGEST_MISMATCH", "the assertion signed a different action")
        return self._verified_assertion(
            challenge=challenge,
            credential=registered,
            assertion_bytes=material,
            sign_count=int(credential.get("sign_count", registered.sign_count + 1)),
            user_verified=bool(credential.get("user_verified", True)),
            verified_at=verified_at,
        )
