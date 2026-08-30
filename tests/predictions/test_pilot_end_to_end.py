"""One operator's whole path through the control plane, with a fake venue and no real secret."""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from http import HTTPStatus
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from polytrading.predictions.domain import PredictionVenue
from polytrading.predictions.execution.models import (
    canonical_execution_hash,
)
from polytrading.predictions.manifest import AdapterImplementationState
from polytrading.predictions.pilot.activation import PilotReconciliationState
from polytrading.predictions.pilot.capabilities import VenueBinding
from polytrading.predictions.pilot.execution_port import (
    CoordinatorExecutionPort,
    ExecutionEvidence,
)
from polytrading.predictions.pilot.passkeys import FakePasskeyService, action_challenge_digest
from polytrading.predictions.pilot.runtime import build_pilot_runtime
from polytrading.predictions.pilot.selector import PilotAccountState
from polytrading.predictions.pilot.server import (
    CSRF_HEADER,
    SESSION_COOKIE,
    PilotRequest,
    PilotResponse,
)
from polytrading.predictions.pilot.services import PilotEnvironment
from polytrading.predictions.pilot.sessions import PilotExecutor
from polytrading.predictions.storage.store import PredictionMarketStore
from tests.predictions.manifest_helpers import venue_manifest
from tests.predictions.pilot_helpers import (
    ACCOUNT_FINGERPRINT,
    EVIDENCE_HASH,
    WALLET_FINGERPRINT,
    venue_binding_fields,
)
from tests.predictions.test_pilot_execution_port import FakeSigner
from tests.predictions.test_pilot_read_models import qualification
from tests.predictions.test_pilot_selector import populated_store

# The same cutoff the seeded evidence was collected at: a live decision needs current evidence.
NOW = datetime(2026, 8, 29, 12, tzinfo=UTC)
PORT = 8788
ORIGIN = f"http://localhost:{PORT}"
HOST = f"localhost:{PORT}"
CREDENTIAL_ID = "operator-passkey"
PROOF_ID = "00000000-0000-0000-0000-000000006001"
ELIGIBLE_MANIFEST = venue_manifest(
    venue=PredictionVenue.POLYMARKET,
    implementation_state=AdapterImplementationState.LIVE_ELIGIBLE,
    jurisdiction_review_status="ELIGIBILITY_REVIEWED",
    source_hashes=(EVIDENCE_HASH,),
    reviewed_at=NOW - timedelta(days=1),
)


def account_state() -> PilotAccountState:
    return PilotAccountState.model_validate(
        {
            "account_fingerprint": ACCOUNT_FINGERPRINT,
            "wallet_fingerprint": WALLET_FINGERPRINT,
            "collateral_usd": Decimal("200"),
            "allowance_usd": Decimal("200"),
            "kill_engaged": False,
            "observed_at": NOW,
        },
        strict=True,
    )


def reconciliation() -> PilotReconciliationState:
    return PilotReconciliationState.model_validate(
        {
            "account_fingerprint": ACCOUNT_FINGERPRINT,
            "active_submissions": 0,
            "unknown_outcomes": 0,
            "reconciliation_complete": True,
            "unexplained_difference_usd": Decimal("0"),
            "reconciliation_hash": "8" * 64,
            "observed_at": NOW,
        },
        strict=True,
    )


def evidence() -> ExecutionEvidence:
    return ExecutionEvidence(
        manifest=ELIGIBLE_MANIFEST,
        geoblock_allowed=True,
        geoblock_evidence_hash="a" * 64,
        geoblock_expires_at=NOW + timedelta(minutes=1),
        account_scope_evidence_hash="b" * 64,
        account_scope_expires_at=NOW + timedelta(minutes=1),
        kill_engaged=False,
        operator_present=True,
    )


class Cockpit:
    """Drives the real HTTP application exactly as the browser would."""

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime
        self.application = runtime.application
        opened = self._respond("POST", "/api/v1/pilot/session")
        assert opened.status is HTTPStatus.CREATED
        self.token = opened.set_cookie.split(";", 1)[0].split("=", 1)[1]
        self.csrf = body(opened)["csrf_token"]

    def _respond(self, method: str, target: str, **overrides: Any) -> PilotResponse:
        return self.application.respond(
            PilotRequest(
                method=method,
                target=target,
                host=HOST,
                received_at=overrides.pop("received_at", NOW),
                origin=ORIGIN if method == "POST" else None,
                headers=overrides.pop("headers", {}),
                cookies=overrides.pop("cookies", {}),
                body=overrides.pop("body", b""),
            )
        )

    def get(self, path: str) -> dict[str, Any]:
        response = self._respond("GET", path, cookies={SESSION_COOKIE: (self.token,)})
        assert response.status is HTTPStatus.OK, body(response)
        return body(response)

    def post(self, path: str, payload: Mapping[str, Any] | None = None) -> PilotResponse:
        return self._respond(
            "POST",
            path,
            headers={CSRF_HEADER: (self.csrf,)},
            cookies={SESSION_COOKIE: (self.token,)},
            body=json.dumps(payload or {}).encode("utf-8"),
        )

    @property
    def session_hash(self) -> str:
        from hashlib import sha256

        return sha256(self.token.encode("ascii")).hexdigest()


def body(response: PilotResponse) -> dict[str, Any]:
    return json.loads(response.body.decode("utf-8"))


@pytest.fixture
def cockpit(tmp_path: Path) -> Iterator[Cockpit]:
    database = tmp_path / "pilot.duckdb"
    seed = PredictionMarketStore(database)
    populated_store(seed)
    seed.close()

    signer = FakeSigner(account=account_state())
    passkeys = FakePasskeyService(port=PORT)
    runtime_holder: dict[str, Any] = {}

    def executor_factory(grants: Mapping[UUID, Any]) -> PilotExecutor:
        runtime = runtime_holder["runtime"]
        port = CoordinatorExecutionPort(
            store=runtime.store,
            signer=signer,
            verifier=runtime.verifier,
            grants=grants,
            evidence=evidence,
            clock=lambda: NOW,
        )
        return PilotExecutor(port, clock=lambda: NOW)

    environment = PilotEnvironment(
        account_fingerprint=ACCOUNT_FINGERPRINT,
        wallet_fingerprint=WALLET_FINGERPRINT,
        venue_binding=VenueBinding.model_validate(
            venue_binding_fields(manifest_record_hash=canonical_execution_hash(ELIGIBLE_MANIFEST)),
            strict=True,
        ),
        manifest=ELIGIBLE_MANIFEST,
        manifest_state="LIVE_ELIGIBLE",
        protocol_state="CURRENT",
        qualifications=(qualification(),),
        eligibility_expires_at=NOW + timedelta(days=30),
        credentials_present=True,
        reconciliation=reconciliation(),
        account_state=account_state,
        executor_factory=executor_factory,
    )
    runtime = build_pilot_runtime(
        database,
        PORT,
        now=lambda: NOW,
        environment=environment,
        passkeys=passkeys,
        presence_source=_StillSource(),
    )
    runtime_holder["runtime"] = runtime
    passkeys.registration_options(account_fingerprint=ACCOUNT_FINGERPRINT, wallet_unlocked=True)
    passkeys.complete_registration(
        credential={"id": CREDENTIAL_ID, "public_key": "cHVibGlj", "sign_count": 0},
        account_fingerprint=ACCOUNT_FINGERPRINT,
        wallet_fingerprint=WALLET_FINGERPRINT,
        registered_at=NOW,
    )
    try:
        yield Cockpit(runtime)
    finally:
        runtime.close()


class _StillSource:
    def drain(self) -> tuple[str, ...]:
        return ()

    def monotonic_ns(self) -> int:
        return 0


def test_a_launched_cockpit_reports_a_killed_posture(cockpit: Cockpit) -> None:
    readiness = cockpit.get("/api/v1/pilot/readiness")

    assert readiness["kill_engaged"] is True
    assert readiness["live_authority"] is False
    assert "KILL_ENGAGED" in readiness["blockers"]
    assert readiness["protocol_version"] == "polymarket-clob-2026-08-29-v2"


def test_opportunities_stay_empty_while_the_kill_is_engaged(cockpit: Cockpit) -> None:
    assert cockpit.get("/api/v1/pilot/opportunities") == {
        "opportunities": [],
        "reason": "PILOT_KILL_ENGAGED",
    }


def test_the_limits_view_serves_the_compiled_ceilings(cockpit: Cockpit) -> None:
    policy = cockpit.get("/api/v1/pilot/policy")

    assert policy["ceilings"]["order_notional"] == "10"
    assert policy["requested"] is None
    assert policy["ceiling_hash"]


def test_a_lowered_policy_is_accepted_and_a_raised_one_is_refused(cockpit: Cockpit) -> None:
    lowered = cockpit.post("/api/v1/pilot/policy", {"requested_limits": {"order_notional": "4"}})
    raised = cockpit.post("/api/v1/pilot/policy", {"requested_limits": {"order_notional": "10.01"}})

    assert lowered.status is HTTPStatus.OK, body(lowered)
    assert body(lowered)["accepted"] is True
    assert raised.status is HTTPStatus.BAD_REQUEST
    assert body(raised)["error"] == "ORDER_NOTIONAL_CEILING"
    assert cockpit.get("/api/v1/pilot/policy")["requested"]["order_notional"] == "4"


def test_the_kill_must_be_cleared_before_anything_is_offered(cockpit: Cockpit) -> None:
    cleared = clear_kill(cockpit)

    assert cleared.status is HTTPStatus.OK, body(cleared)
    assert body(cleared)["cleared"] is True
    readiness = cockpit.get("/api/v1/pilot/readiness")
    assert readiness["kill_engaged"] is False
    assert "KILL_ENGAGED" not in readiness["blockers"]
    opportunities = cockpit.get("/api/v1/pilot/opportunities")["opportunities"]
    assert [item["proof_id"] for item in opportunities] == [PROOF_ID]
    assert opportunities[0]["rank"] == 1


def clear_kill(cockpit: Cockpit) -> PilotResponse:
    options = cockpit.post(
        "/api/v1/pilot/passkeys/authenticate/options",
        {"mode": "COMPLETE_STRATEGY", "browser_session_hash": cockpit.session_hash},
    )
    assert options.status is HTTPStatus.OK, body(options)
    challenge_id = body(options)["challenge_id"]
    return cockpit.post(
        "/api/v1/pilot/kill/clear",
        {
            "challenge_id": challenge_id,
            "kill_event_id": "00000000-0000-0000-0000-0000000000f1",
            "confirmation_phrase": "CLEAR POLYMARKET PILOT KILL",
            "browser_session_hash": cockpit.session_hash,
            "assertion": _assertion(cockpit, challenge_id),
        },
    )


def _assertion(cockpit: Cockpit, challenge_id: str) -> dict[str, Any]:
    challenge = cockpit.application.services.open_challenge(UUID(challenge_id))
    return {"action_digest": action_challenge_digest(challenge), "sign_count": 1}


def approve(cockpit: Cockpit, mode: str = "COMPLETE_STRATEGY") -> PilotResponse:
    options = cockpit.post(
        "/api/v1/pilot/passkeys/authenticate/options",
        {
            "mode": mode,
            "opportunity_id": PROOF_ID,
            "browser_session_hash": cockpit.session_hash,
        },
    )
    assert options.status is HTTPStatus.OK, body(options)
    payload = body(options)
    return cockpit.post(
        "/api/v1/pilot/authorizations",
        {
            "challenge_id": payload["challenge_id"],
            "confirmation_text": payload["confirmation_text"],
            "browser_session_hash": cockpit.session_hash,
            "assertion": _assertion(cockpit, payload["challenge_id"]),
        },
    )


def test_the_confirmation_text_is_exact_and_server_supplied(cockpit: Cockpit) -> None:
    clear_kill(cockpit)
    options = cockpit.post(
        "/api/v1/pilot/passkeys/authenticate/options",
        {
            "mode": "COMPLETE_STRATEGY",
            "opportunity_id": PROOF_ID,
            "browser_session_hash": cockpit.session_hash,
        },
    )

    text = body(options)["confirmation_text"]

    assert text.startswith("STRATEGY ")
    assert text.endswith(" USD")


def test_a_mistyped_confirmation_never_issues_a_capability(cockpit: Cockpit) -> None:
    clear_kill(cockpit)
    options = cockpit.post(
        "/api/v1/pilot/passkeys/authenticate/options",
        {
            "mode": "COMPLETE_STRATEGY",
            "opportunity_id": PROOF_ID,
            "browser_session_hash": cockpit.session_hash,
        },
    )
    payload = body(options)

    refused = cockpit.post(
        "/api/v1/pilot/authorizations",
        {
            "challenge_id": payload["challenge_id"],
            "confirmation_text": "STRATEGY 1.00 USD",
            "browser_session_hash": cockpit.session_hash,
            "assertion": _assertion(cockpit, payload["challenge_id"]),
        },
    )

    assert refused.status is HTTPStatus.BAD_REQUEST
    assert body(refused)["error"] == "CONFIRMATION_TEXT_MISMATCH"
    assert cockpit.get("/api/v1/pilot/audit")["events"] == []


def test_an_approval_issues_a_capability_and_records_it(cockpit: Cockpit) -> None:
    clear_kill(cockpit)

    approved = approve(cockpit)

    assert approved.status is HTTPStatus.OK, body(approved)
    assert body(approved)["capability_id"]
    audit = cockpit.get("/api/v1/pilot/audit")["events"]
    assert [event["outcome"] for event in audit] == ["ISSUED"]


def test_a_replayed_challenge_is_refused(cockpit: Cockpit) -> None:
    clear_kill(cockpit)
    options = cockpit.post(
        "/api/v1/pilot/passkeys/authenticate/options",
        {
            "mode": "COMPLETE_STRATEGY",
            "opportunity_id": PROOF_ID,
            "browser_session_hash": cockpit.session_hash,
        },
    )
    payload = body(options)
    request = {
        "challenge_id": payload["challenge_id"],
        "confirmation_text": payload["confirmation_text"],
        "browser_session_hash": cockpit.session_hash,
        "assertion": _assertion(cockpit, payload["challenge_id"]),
    }
    first = cockpit.post("/api/v1/pilot/authorizations", request)
    second = cockpit.post("/api/v1/pilot/authorizations", request)

    assert first.status is HTTPStatus.OK, body(first)
    assert second.status is HTTPStatus.FORBIDDEN
    assert body(second)["error"] == "CHALLENGE_REPLAYED"


def test_stopping_re_engages_the_kill_and_ends_authority(cockpit: Cockpit) -> None:
    clear_kill(cockpit)
    approve(cockpit)

    stopped = cockpit.post("/api/v1/pilot/stop")

    assert stopped.status is HTTPStatus.OK
    assert body(stopped) == {"kill_engaged": True, "reason": "OPERATOR_STOP"}
    readiness = cockpit.get("/api/v1/pilot/readiness")
    assert readiness["kill_engaged"] is True
    assert readiness["live_authority"] is False


def test_the_credential_ceremony_refuses_without_a_configured_signer(cockpit: Cockpit) -> None:
    response = cockpit.post("/api/v1/pilot/credentials/provision")

    assert response.status is HTTPStatus.CONFLICT
    assert body(response)["error"] == "CREDENTIAL_CEREMONY_UNAVAILABLE"


def test_no_response_ever_carries_a_secret_or_a_session_token(cockpit: Cockpit) -> None:
    clear_kill(cockpit)
    approve(cockpit)
    payloads = [
        json.dumps(cockpit.get(path))
        for path in (
            "/api/v1/pilot/readiness",
            "/api/v1/pilot/policy",
            "/api/v1/pilot/opportunities",
            "/api/v1/pilot/live-session",
            "/api/v1/pilot/audit",
        )
    ]

    for payload in payloads:
        assert cockpit.token not in payload
        for forbidden in ("private_key", "api_secret", "passphrase", "detached_signature"):
            assert forbidden not in payload
