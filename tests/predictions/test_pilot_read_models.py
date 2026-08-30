from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest

from polytrading.predictions.execution.models import ImmediateOrderType
from polytrading.predictions.pilot.models import (
    PILOT_CEILING_HASH,
    PILOT_CEILINGS,
    LossStatus,
    PilotLossState,
    PilotProofFamily,
    PresenceState,
)
from polytrading.predictions.pilot.qualification import QualificationGate, QualificationReport
from polytrading.predictions.pilot.read_models import (
    PilotActivationView,
    PilotAuditEntry,
    PilotSnapshot,
    build_limits_view,
    build_opportunity_views,
    build_readiness_view,
    build_session_view,
)
from polytrading.predictions.pilot.selector import PilotLeg, PilotOpportunity
from tests.predictions.pilot_helpers import EVIDENCE_HASH, PROTOCOL_FIXTURE_HASH

NOW = datetime(2026, 8, 29, 12, tzinfo=UTC)
CANARIES = (
    "0xdeadbeefwalletprivatekey",
    "clob-api-secret-canary",
    "passkey-assertion-bytes",
    "capability-bundle-bytes",
)


def qualification(*, qualified: bool = True) -> QualificationReport:
    return QualificationReport(
        schema_version=1,
        proof_family=PilotProofFamily.BINARY_COMPLEMENT,
        as_of=NOW,
        evidence_window_start=NOW - timedelta(days=45),
        shadow_window_start=NOW - timedelta(days=30),
        qualified=qualified,
        gates=(
            QualificationGate(
                code="EVIDENCE_DAYS_INSUFFICIENT",
                satisfied=qualified,
                observed=Decimal(45),
                threshold=Decimal(45),
            ),
        ),
        failed_codes=() if qualified else ("EVIDENCE_DAYS_INSUFFICIENT",),
        evidence_hashes=(EVIDENCE_HASH,),
        policy_identities=("research-v1@1",),
        protocol_fixture_hashes=(PROTOCOL_FIXTURE_HASH,),
    )


def readiness(**overrides: Any):
    fields: dict[str, Any] = {
        "kill_engaged": False,
        "presence_state": PresenceState.PRESENT,
        "manifest_state": "LIVE_ELIGIBLE",
        "protocol_state": "CURRENT",
        "protocol_version": "polymarket-clob-2026-08-29-v2",
        "qualifications": (qualification(),),
        "eligibility_expires_at": NOW + timedelta(days=30),
        "credentials_present": True,
        "reconciliation_complete": True,
        "information_cutoff": NOW - timedelta(seconds=3),
        "as_of": NOW,
        "evidence_hashes": (EVIDENCE_HASH,),
    }
    fields.update(overrides)
    return build_readiness_view(**fields)


def opportunity(**overrides: Any) -> PilotOpportunity:
    fields: dict[str, Any] = {
        "schema_version": 1,
        "proof_id": UUID("00000000-0000-0000-0000-000000006001"),
        "candidate_id": UUID("00000000-0000-0000-0000-000000003001"),
        "proposal_id": UUID("70000000-0000-0000-0000-000000000001"),
        "proof_family": PilotProofFamily.BINARY_COMPLEMENT,
        "legs": (
            PilotLeg(
                leg_index=0,
                outcome_token_id="token-0",
                side="buy",
                limit_price=Decimal("0.40"),
                size=Decimal("10"),
                order_type=ImmediateOrderType.FAK,
            ),
            PilotLeg(
                leg_index=1,
                outcome_token_id="token-1",
                side="buy",
                limit_price=Decimal("0.55"),
                size=Decimal("10"),
                order_type=ImmediateOrderType.FAK,
            ),
        ),
        "current_surplus_usd": Decimal("0.40"),
        "stressed_surplus_usd": Decimal("0.20"),
        "capacity_usd": Decimal("150"),
        "incomplete_loss_usd": Decimal("0.50"),
        "deployed_capital_usd": Decimal("9.50"),
        "evidence_hashes": (EVIDENCE_HASH,),
        "information_cutoff": NOW - timedelta(seconds=2),
    }
    fields.update(overrides)
    return PilotOpportunity.model_validate(fields, strict=True)


def loss_state(**overrides: Any) -> PilotLossState:
    fields: dict[str, Any] = {
        "status": LossStatus.KNOWN,
        "session_start_equity": Decimal("110"),
        "realized_loss": Decimal("1"),
        "unrealized_loss": Decimal("0.5"),
        "evidence_hashes": (EVIDENCE_HASH,),
        "evaluated_at": NOW,
    }
    fields.update(overrides)
    return PilotLossState.model_validate(fields, strict=True)


def session(**overrides: Any):
    fields: dict[str, Any] = {
        "active": True,
        "mode": "AUTOMATION_SESSION",
        "authority_expires_at": NOW + timedelta(minutes=10),
        "presence_state": PresenceState.PRESENT,
        "last_heartbeat_at": NOW,
        "strategies_started": 2,
        "deployed_capital_usd": Decimal("18"),
        "loss_state": loss_state(),
        "utc_day_loss_usd": Decimal("2"),
        "reconciliation_complete": True,
    }
    fields.update(overrides)
    return build_session_view(**fields)


def snapshot(**overrides: Any) -> PilotSnapshot:
    fields: dict[str, Any] = {
        "schema_version": 1,
        "as_of": NOW,
        "information_cutoff": NOW - timedelta(seconds=3),
        "readiness": readiness(),
        "limits": build_limits_view(PILOT_CEILINGS),
        "opportunities": build_opportunity_views((opportunity(),), as_of=NOW),
        "session": session(),
        "activation": PilotActivationView(
            stage=3, result="APPROVED", manifest_record_hash=EVIDENCE_HASH, occurred_at=NOW
        ),
        "audit": (
            PilotAuditEntry(
                occurred_at=NOW - timedelta(seconds=1),
                kind="CAPABILITY_ISSUED",
                outcome="ISSUED",
                digest=EVIDENCE_HASH,
            ),
        ),
    }
    fields.update(overrides)
    return PilotSnapshot.model_validate(fields, strict=True)


def test_a_ready_pilot_reports_no_blockers() -> None:
    view = readiness()

    assert view.blockers == ()
    assert view.qualified_families == (PilotProofFamily.BINARY_COMPLEMENT,)
    assert view.evidence_age_seconds == Decimal("3")


@pytest.mark.parametrize(
    ("overrides", "blocker"),
    [
        ({"kill_engaged": True}, "KILL_ENGAGED"),
        ({"presence_state": PresenceState.TERMINAL}, "PRESENCE_LOST"),
        ({"manifest_state": "LIVE_DISABLED"}, "MANIFEST_NOT_ELIGIBLE"),
        ({"protocol_state": "PROTOCOL_REVIEW_REQUIRED"}, "PROTOCOL_REVIEW_REQUIRED"),
        ({"qualifications": (qualification(qualified=False),)}, "QUALIFICATION_INCOMPLETE"),
        ({"eligibility_expires_at": NOW}, "ELIGIBILITY_EXPIRED"),
        ({"credentials_present": False}, "CREDENTIALS_MISSING"),
        ({"reconciliation_complete": False}, "RECONCILIATION_INCOMPLETE"),
    ],
)
def test_every_blocker_is_named(overrides: dict[str, Any], blocker: str) -> None:
    assert blocker in readiness(**overrides).blockers


def test_the_limits_view_keeps_ceilings_and_requests_separate() -> None:
    lowered = PILOT_CEILINGS.model_copy(update={"order_notional": Decimal("4")})
    view = build_limits_view(lowered)

    assert view.ceilings == PILOT_CEILINGS
    assert view.requested is not None
    assert view.requested.order_notional == Decimal("4")
    assert view.ceiling_hash == PILOT_CEILING_HASH


def test_opportunity_views_carry_rank_and_the_first_tie_break_field() -> None:
    ranked = (
        opportunity(incomplete_loss_usd=Decimal("0.10")),
        opportunity(
            proof_id=UUID("00000000-0000-0000-0000-000000006002"),
            incomplete_loss_usd=Decimal("0.50"),
        ),
    )
    views = build_opportunity_views(ranked, as_of=NOW)

    assert [view.rank for view in views] == [1, 2]
    assert views[0].tie_break_field == "rank_1"
    assert views[1].tie_break_field == "incomplete_loss_ratio"
    assert views[0].evidence_age_seconds == Decimal("2")


def test_totals_are_hidden_until_reconciliation_is_exact() -> None:
    unreconciled = session(reconciliation_complete=False)
    unknown = session(
        loss_state=loss_state(
            status=LossStatus.UNKNOWN,
            session_start_equity=None,
            realized_loss=None,
            unrealized_loss=None,
        )
    )

    for view in (unreconciled, unknown):
        assert view.session_loss_usd is None
        assert view.utc_day_loss_usd is None
        assert view.loss_status is LossStatus.UNKNOWN
    assert session().session_loss_usd == Decimal("1.5")


def test_a_snapshot_rejects_evidence_from_after_its_own_cutoff() -> None:
    with pytest.raises(ValueError, match="after its own cutoff"):
        snapshot(information_cutoff=NOW + timedelta(seconds=1))
    with pytest.raises(ValueError, match="postdate"):
        snapshot(
            audit=(
                PilotAuditEntry(
                    occurred_at=NOW + timedelta(seconds=1),
                    kind="CAPABILITY_ISSUED",
                    outcome="ISSUED",
                    digest=EVIDENCE_HASH,
                ),
            )
        )


def test_a_serialized_snapshot_contains_no_secret_canary() -> None:
    payload = json.dumps(snapshot().model_dump(mode="json"))

    for canary in CANARIES:
        assert canary not in payload
    for forbidden in ("private_key", "api_secret", "passphrase", "detached_signature", "cookie"):
        assert forbidden not in payload


def test_a_snapshot_is_one_coherent_cut() -> None:
    view = snapshot()

    assert view.as_of == NOW
    assert view.information_cutoff <= view.as_of
    assert view.readiness.evidence_age_seconds >= 0
    assert all(item.evidence_age_seconds >= 0 for item in view.opportunities)
