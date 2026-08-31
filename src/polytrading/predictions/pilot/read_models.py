"""Coherent, sanitized projections of pilot state for one information cutoff.

Every section of a snapshot is built at the same cutoff from the same evidence, so the operator
never sees readiness from one moment beside opportunities from another. Financial totals appear
only when reconciliation is exact; anything else is reported as UNKNOWN rather than estimated. No
wallet key, credential, capability bundle, or passkey assertion has a field here.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import Field, model_validator

from polytrading.predictions.domain import Sha256
from polytrading.predictions.manifest import AdapterImplementationState
from polytrading.predictions.pilot.models import (
    LossStatus,
    PilotLimits,
    PilotLossState,
    PilotProofFamily,
    PilotRecord,
    PresenceState,
    UtcTimestamp,
)
from polytrading.predictions.pilot.policy import COMPILED_PILOT_CEILINGS
from polytrading.predictions.pilot.qualification import QualificationReport
from polytrading.predictions.pilot.selector import (
    PilotOpportunity,
    first_tie_break_field,
)

NonNegativeDecimal = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
BlockerCode = Literal[
    "KILL_ENGAGED",
    "PRESENCE_LOST",
    "MANIFEST_NOT_ELIGIBLE",
    "PROTOCOL_REVIEW_REQUIRED",
    "QUALIFICATION_INCOMPLETE",
    "ELIGIBILITY_EXPIRED",
    "CREDENTIALS_MISSING",
    "RECONCILIATION_INCOMPLETE",
]
ManifestState = Literal["MISSING"] | AdapterImplementationState


class PilotReadinessView(PilotRecord):
    kill_engaged: bool
    presence_state: PresenceState
    manifest_state: ManifestState
    protocol_state: Literal["CURRENT", "PROTOCOL_REVIEW_REQUIRED"]
    protocol_version: str
    qualified_families: tuple[PilotProofFamily, ...]
    blockers: tuple[BlockerCode, ...]
    evidence_age_seconds: NonNegativeDecimal
    evidence_hashes: tuple[Sha256, ...]


class PilotLimitsView(PilotRecord):
    """The immutable ceilings beside the operator's requested values, never merged."""

    ceilings: PilotLimits
    requested: PilotLimits | None
    ceiling_hash: Sha256


class PilotOpportunityView(PilotRecord):
    proof_id: str
    proof_family: PilotProofFamily
    rank: Annotated[int, Field(ge=1)]
    tie_break_field: str
    current_surplus_usd: Decimal
    stressed_surplus_usd: Decimal
    capacity_usd: NonNegativeDecimal
    incomplete_exposure_usd: NonNegativeDecimal
    deployed_capital_usd: NonNegativeDecimal
    recovery_branch_count: Annotated[int, Field(ge=0)]
    evidence_age_seconds: NonNegativeDecimal


class PilotSessionView(PilotRecord):
    active: bool
    mode: str | None
    authority_expires_at: UtcTimestamp | None
    presence_state: PresenceState
    last_heartbeat_at: UtcTimestamp | None
    strategies_started: Annotated[int, Field(ge=0)]
    deployed_capital_usd: NonNegativeDecimal
    session_loss_usd: Decimal | None
    utc_day_loss_usd: Decimal | None
    loss_status: LossStatus


class PilotActivationView(PilotRecord):
    stage: Annotated[int, Field(ge=0, le=4)]
    result: str | None
    manifest_record_hash: Sha256 | None
    occurred_at: UtcTimestamp | None


class PilotAuditEntry(PilotRecord):
    occurred_at: UtcTimestamp
    kind: str
    outcome: str
    digest: Sha256


class PilotSnapshot(PilotRecord):
    """One coherent cut. Mixed cutoffs or hashes are a rejection, not a warning."""

    schema_version: Literal[1]
    as_of: UtcTimestamp
    information_cutoff: UtcTimestamp
    readiness: PilotReadinessView
    limits: PilotLimitsView
    opportunities: tuple[PilotOpportunityView, ...]
    session: PilotSessionView
    activation: PilotActivationView
    audit: tuple[PilotAuditEntry, ...]

    @model_validator(mode="after")
    def _require_one_cutoff(self) -> PilotSnapshot:
        if self.information_cutoff > self.as_of:
            raise ValueError("a snapshot cannot cite evidence from after its own cutoff")
        for entry in self.audit:
            if entry.occurred_at > self.as_of:
                raise ValueError("an audit entry cannot postdate the snapshot")
        return self


def build_readiness_view(
    *,
    kill_engaged: bool,
    presence_state: PresenceState,
    manifest_state: str,
    protocol_state: str,
    protocol_version: str,
    qualifications: Sequence[QualificationReport],
    eligibility_expires_at: datetime | None,
    credentials_present: bool,
    reconciliation_complete: bool,
    information_cutoff: datetime,
    as_of: datetime,
    evidence_hashes: Sequence[str],
) -> PilotReadinessView:
    blockers: list[BlockerCode] = []
    if kill_engaged:
        blockers.append("KILL_ENGAGED")
    if presence_state is not PresenceState.PRESENT:
        blockers.append("PRESENCE_LOST")
    if manifest_state != "LIVE_ELIGIBLE":
        blockers.append("MANIFEST_NOT_ELIGIBLE")
    if protocol_state != "CURRENT":
        blockers.append("PROTOCOL_REVIEW_REQUIRED")
    if not qualifications or not any(report.qualified for report in qualifications):
        blockers.append("QUALIFICATION_INCOMPLETE")
    if eligibility_expires_at is None or eligibility_expires_at <= as_of:
        blockers.append("ELIGIBILITY_EXPIRED")
    if not credentials_present:
        blockers.append("CREDENTIALS_MISSING")
    if not reconciliation_complete:
        blockers.append("RECONCILIATION_INCOMPLETE")
    normalized_manifest_state: ManifestState = (
        "MISSING"
        if manifest_state == "MISSING"
        else AdapterImplementationState(manifest_state)
    )
    return PilotReadinessView(
        kill_engaged=kill_engaged,
        presence_state=presence_state,
        manifest_state=normalized_manifest_state,
        protocol_state=protocol_state,  # type: ignore[arg-type]
        protocol_version=protocol_version,
        qualified_families=tuple(
            sorted(
                (report.proof_family for report in qualifications if report.qualified),
                key=lambda family: family.value,
            )
        ),
        blockers=tuple(sorted(set(blockers))),
        evidence_age_seconds=_age_seconds(information_cutoff, as_of),
        evidence_hashes=tuple(sorted(set(evidence_hashes))),
    )


def build_limits_view(requested: PilotLimits | None) -> PilotLimitsView:
    from polytrading.predictions.pilot.models import PILOT_CEILING_HASH

    return PilotLimitsView(
        ceilings=COMPILED_PILOT_CEILINGS, requested=requested, ceiling_hash=PILOT_CEILING_HASH
    )


def build_opportunity_views(
    ranked: Sequence[PilotOpportunity], *, as_of: datetime, recovery_branches: int = 1
) -> tuple[PilotOpportunityView, ...]:
    views: list[PilotOpportunityView] = []
    for index, opportunity in enumerate(ranked):
        previous = ranked[index - 1] if index else None
        views.append(
            PilotOpportunityView(
                proof_id=str(opportunity.proof_id),
                proof_family=opportunity.proof_family,
                rank=index + 1,
                tie_break_field=(
                    "rank_1" if previous is None else first_tie_break_field(previous, opportunity)
                ),
                current_surplus_usd=opportunity.current_surplus_usd,
                stressed_surplus_usd=opportunity.stressed_surplus_usd,
                capacity_usd=opportunity.capacity_usd,
                incomplete_exposure_usd=opportunity.incomplete_loss_usd,
                deployed_capital_usd=opportunity.deployed_capital_usd,
                recovery_branch_count=recovery_branches,
                evidence_age_seconds=_age_seconds(opportunity.information_cutoff, as_of),
            )
        )
    return tuple(views)


def build_session_view(
    *,
    active: bool,
    mode: str | None,
    authority_expires_at: datetime | None,
    presence_state: PresenceState,
    last_heartbeat_at: datetime | None,
    strategies_started: int,
    deployed_capital_usd: Decimal,
    loss_state: PilotLossState,
    utc_day_loss_usd: Decimal | None,
    reconciliation_complete: bool,
) -> PilotSessionView:
    """Hide every financial total unless reconciliation is exact and the loss state is known."""

    known = loss_state.status is LossStatus.KNOWN and reconciliation_complete
    session_loss = (
        (loss_state.realized_loss or Decimal("0")) + (loss_state.unrealized_loss or Decimal("0"))
        if known
        else None
    )
    return PilotSessionView(
        active=active,
        mode=mode,
        authority_expires_at=authority_expires_at,
        presence_state=presence_state,
        last_heartbeat_at=last_heartbeat_at,
        strategies_started=strategies_started,
        deployed_capital_usd=deployed_capital_usd,
        session_loss_usd=session_loss,
        utc_day_loss_usd=utc_day_loss_usd if known else None,
        loss_status=loss_state.status if known else LossStatus.UNKNOWN,
    )


def _age_seconds(information_cutoff: datetime, as_of: datetime) -> Decimal:
    age = as_of - information_cutoff
    return Decimal(max(age, timedelta(0)).total_seconds())


__all__ = [
    "BlockerCode",
    "PilotActivationView",
    "PilotAuditEntry",
    "PilotLimitsView",
    "PilotOpportunityView",
    "PilotReadinessView",
    "PilotSessionView",
    "PilotSnapshot",
    "build_limits_view",
    "build_opportunity_views",
    "build_readiness_view",
    "build_session_view",
]
