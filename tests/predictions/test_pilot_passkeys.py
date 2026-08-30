from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from polytrading.predictions.pilot.models import AuthorizationChallenge
from polytrading.predictions.pilot.passkeys import (
    RP_ID,
    FakePasskeyService,
    PasskeyError,
    RegisteredCredential,
    action_challenge_digest,
    pilot_origin,
)
from tests.predictions.pilot_helpers import (
    ACCOUNT_FINGERPRINT,
    BROWSER_SESSION_HASH,
    CREDENTIAL_ID_HASH,
    NOW,
    WALLET_FINGERPRINT,
    challenge_fields,
)

PORT = 8788
ORIGIN = f"http://localhost:{PORT}"
CREDENTIAL_ID = "pilot-credential"


def challenge(**overrides: Any) -> AuthorizationChallenge:
    return AuthorizationChallenge.model_validate(challenge_fields(**overrides), strict=True)


def registered_service(**registration: Any) -> FakePasskeyService:
    from hashlib import sha256

    service = FakePasskeyService(port=PORT)
    service.registration_options(account_fingerprint=ACCOUNT_FINGERPRINT, wallet_unlocked=True)
    service.complete_registration(
        credential={"id": CREDENTIAL_ID, "public_key": "cHVibGlj", "sign_count": 0, **registration},
        account_fingerprint=ACCOUNT_FINGERPRINT,
        wallet_fingerprint=WALLET_FINGERPRINT,
        registered_at=NOW,
    )
    assert sha256(CREDENTIAL_ID.encode()).hexdigest() == CREDENTIAL_ID_HASH_FOR_TESTS
    return service


CREDENTIAL_ID_HASH_FOR_TESTS = __import__("hashlib").sha256(CREDENTIAL_ID.encode()).hexdigest()


def bound_challenge(**overrides: Any) -> AuthorizationChallenge:
    return challenge(credential_id_hash=CREDENTIAL_ID_HASH_FOR_TESTS, **overrides)


def assertion_payload(target: AuthorizationChallenge, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "action_digest": action_challenge_digest(target),
        "sign_count": 1,
        "user_verified": True,
    }
    payload.update(overrides)
    return payload


def verify(service: FakePasskeyService, target: AuthorizationChallenge, **overrides: Any):
    arguments: dict[str, Any] = {
        "credential": assertion_payload(target),
        "challenge": target,
        "browser_session_hash": BROWSER_SESSION_HASH,
        "origin": ORIGIN,
        "rp_id": RP_ID,
        "verified_at": target.not_before,
    }
    arguments.update(overrides)
    return service.verify(**arguments)


def test_pilot_origin_is_one_exact_localhost_origin() -> None:
    assert pilot_origin(PORT) == ORIGIN
    with pytest.raises(PasskeyError, match="ORIGIN_MISMATCH"):
        pilot_origin(0)


def test_assertion_rejects_127_origin_for_localhost_rp() -> None:
    service = registered_service()
    target = bound_challenge()
    service.authentication_options(target)

    with pytest.raises(PasskeyError, match="ORIGIN_MISMATCH"):
        verify(service, target, origin="http://127.0.0.1:8788")


def test_assertion_rejects_another_rp_id() -> None:
    service = registered_service()
    target = bound_challenge()
    service.authentication_options(target)

    with pytest.raises(PasskeyError, match="RP_ID_MISMATCH"):
        verify(service, target, rp_id="polymarket.com")


def test_assertion_rejects_another_browser_session() -> None:
    service = registered_service()
    target = bound_challenge()
    service.authentication_options(target)

    with pytest.raises(PasskeyError, match="BROWSER_SESSION_MISMATCH"):
        verify(service, target, browser_session_hash="9" * 64)


def test_an_unopened_challenge_cannot_be_asserted() -> None:
    service = registered_service()

    with pytest.raises(PasskeyError, match="CHALLENGE_UNKNOWN"):
        verify(service, bound_challenge())


def test_a_challenge_is_single_use() -> None:
    service = registered_service()
    target = bound_challenge()
    service.authentication_options(target)
    assert verify(service, target).user_verified is True

    with pytest.raises(PasskeyError, match="CHALLENGE_REPLAYED"):
        verify(service, target)
    with pytest.raises(PasskeyError, match="CHALLENGE_REPLAYED"):
        service.authentication_options(target)


def test_an_expired_challenge_is_refused() -> None:
    service = registered_service()
    target = bound_challenge()
    service.authentication_options(target)

    with pytest.raises(PasskeyError, match="CHALLENGE_EXPIRED"):
        verify(service, target, verified_at=target.expires_at + timedelta(seconds=1))


def test_an_assertion_over_another_action_is_refused() -> None:
    service = registered_service()
    target = bound_challenge()
    other = bound_challenge(nonce="another-action")
    service.authentication_options(target)

    with pytest.raises(PasskeyError, match="ACTION_DIGEST_MISMATCH"):
        verify(
            service,
            target,
            credential=assertion_payload(other),
        )


def test_user_verification_is_required() -> None:
    service = registered_service()
    target = bound_challenge()
    service.authentication_options(target)

    with pytest.raises(PasskeyError, match="USER_VERIFICATION_REQUIRED"):
        verify(service, target, credential=assertion_payload(target, user_verified=False))


def test_a_regressing_sign_count_is_refused() -> None:
    service = registered_service()
    first = bound_challenge()
    service.authentication_options(first)
    verify(service, first, credential=assertion_payload(first, sign_count=5))

    second = bound_challenge(
        challenge_id=__import__("uuid").UUID("00000000-0000-0000-0000-00000000d001")
    )
    service.authentication_options(second)
    with pytest.raises(PasskeyError, match="SIGN_COUNT_REGRESSED"):
        verify(service, second, credential=assertion_payload(second, sign_count=4))


def test_registration_requires_an_unlocked_wallet_and_an_empty_registry() -> None:
    service = FakePasskeyService(port=PORT)
    with pytest.raises(PasskeyError, match="WALLET_NOT_UNLOCKED"):
        service.registration_options(account_fingerprint=ACCOUNT_FINGERPRINT, wallet_unlocked=False)

    populated = registered_service()
    with pytest.raises(PasskeyError, match="CREDENTIAL_REGISTRY_NOT_EMPTY"):
        populated.registration_options(
            account_fingerprint=ACCOUNT_FINGERPRINT, wallet_unlocked=True
        )


def test_an_unknown_credential_cannot_assert() -> None:
    service = registered_service()
    target = challenge(credential_id_hash=CREDENTIAL_ID_HASH)
    service.authentication_options(target)

    with pytest.raises(PasskeyError, match="CREDENTIAL_UNKNOWN"):
        verify(service, target)


def test_only_public_credential_data_and_digests_are_kept() -> None:
    service = registered_service()
    target = bound_challenge()
    service.authentication_options(target)
    verified = verify(service, target)

    stored = service.credentials[0]
    assert isinstance(stored, RegisteredCredential)
    assert set(stored.model_dump()) == {
        "schema_version",
        "credential_id",
        "credential_id_hash",
        "public_credential_key",
        "sign_count",
        "account_fingerprint",
        "wallet_fingerprint",
        "registered_at",
    }
    assert verified.action_digest == action_challenge_digest(target)
    assert set(verified.model_dump()) == {
        "schema_version",
        "challenge_id",
        "credential_id_hash",
        "browser_session_hash",
        "account_fingerprint",
        "action_digest",
        "assertion_digest",
        "sign_count",
        "user_verified",
        "verified_at",
    }


def test_the_action_digest_covers_every_bound_field() -> None:
    base = bound_challenge()
    baseline = action_challenge_digest(base)

    assert action_challenge_digest(bound_challenge()) == baseline
    for override in (
        {"nonce": "other-nonce"},
        {"confirmation_text_hash": "1" * 64},
        {"requested_limits_hash": "2" * 64},
        {"browser_session_hash": "0f" * 32},
        {"target_id": __import__("uuid").UUID("00000000-0000-0000-0000-00000000d002")},
    ):
        assert action_challenge_digest(bound_challenge(**override)) != baseline
