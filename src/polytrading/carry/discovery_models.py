from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import field_validator, model_validator

from polytrading.carry.dossier_models import (
    ContractDossierReport,
    DossierStatus,
    NonnegativeInt,
)
from polytrading.domain.models import StrictRecord, normalize_utc_timestamp

DISCOVERY_STATUS_RANK = {
    DossierStatus.COMPATIBLE: 0,
    DossierStatus.MODEL_REQUIRED: 1,
    DossierStatus.EVIDENCE_INCOMPLETE: 2,
    DossierStatus.INELIGIBLE: 3,
}
_ADVANCEABLE_STATUSES = frozenset({DossierStatus.COMPATIBLE, DossierStatus.MODEL_REQUIRED})


class DiscoveryStatusCounts(StrictRecord):
    compatible: NonnegativeInt
    model_required: NonnegativeInt
    evidence_incomplete: NonnegativeInt
    ineligible: NonnegativeInt


class VenueDiscoveryReport(StrictRecord):
    schema_version: Literal[1]
    observed_at: datetime
    warning: Literal["Research only — no trading authority."]
    candidates: tuple[ContractDossierReport, ...]
    counts: DiscoveryStatusCounts
    selected_dossier_id: str | None
    selection_reason_code: Literal[
        "best_nonblocking_complete_evidence",
        "no_advanceable_candidate",
    ]
    activation_status: Literal["not_authorized"]

    @field_validator("observed_at")
    @classmethod
    def normalize_observed_at(cls, value: datetime) -> datetime:
        return normalize_utc_timestamp(value)

    @model_validator(mode="after")
    def require_coherent_discovery(self) -> VenueDiscoveryReport:
        if not self.candidates:
            raise ValueError("discovery requires at least one candidate")

        dossier_ids = tuple(candidate.dossier_id for candidate in self.candidates)
        if len(set(dossier_ids)) != len(dossier_ids):
            raise ValueError("candidate IDs must be unique")
        pairs = tuple(
            (candidate.left_venue, candidate.right_venue) for candidate in self.candidates
        )
        if len(set(pairs)) != len(pairs):
            raise ValueError("candidate venue pairs must be unique")

        if self.observed_at != max(candidate.observed_at for candidate in self.candidates):
            raise ValueError("discovery observation must equal newest candidate observation")
        expected_counts = DiscoveryStatusCounts(
            compatible=sum(
                candidate.status is DossierStatus.COMPATIBLE for candidate in self.candidates
            ),
            model_required=sum(
                candidate.status is DossierStatus.MODEL_REQUIRED for candidate in self.candidates
            ),
            evidence_incomplete=sum(
                candidate.status is DossierStatus.EVIDENCE_INCOMPLETE
                for candidate in self.candidates
            ),
            ineligible=sum(
                candidate.status is DossierStatus.INELIGIBLE for candidate in self.candidates
            ),
        )
        if self.counts != expected_counts:
            raise ValueError("discovery counts must match candidate statuses")

        expected_order = tuple(
            sorted(
                self.candidates,
                key=lambda candidate: (
                    DISCOVERY_STATUS_RANK[candidate.status],
                    candidate.dossier_id,
                ),
            )
        )
        if self.candidates != expected_order:
            raise ValueError("candidates must use canonical discovery rank order")

        advanceable = tuple(
            candidate for candidate in self.candidates if candidate.status in _ADVANCEABLE_STATUSES
        )
        if self.selected_dossier_id is None:
            if advanceable:
                raise ValueError("advanceable catalog must select its best candidate")
            if self.selection_reason_code != "no_advanceable_candidate":
                raise ValueError("empty selection must use no-advanceable reason")
            return self

        if not advanceable or self.selected_dossier_id != advanceable[0].dossier_id:
            raise ValueError("selection must name the best advanceable candidate")
        selected = advanceable[0]
        if selected.counts.blocking or selected.counts.missing_evidence:
            raise ValueError("selected candidate must have no blocking or missing checks")
        if self.selection_reason_code != "best_nonblocking_complete_evidence":
            raise ValueError("selected candidate must use complete-evidence reason")
        return self
