from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from polytrading.domain.models import Asset, StrictRecord, normalize_utc_timestamp

_TOKEN_PATTERN = re.compile(r"[a-z][a-z0-9_]*")
_DOSSIER_ID_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_ASSET_ORDER = (Asset.BTC, Asset.ETH, Asset.SOL)


class ResearchVenue(StrEnum):
    HYPERLIQUID = "hyperliquid"
    DYDX = "dydx"
    LIGHTER = "lighter"


_OFFICIAL_SOURCE_PREFIXES = {
    ResearchVenue.HYPERLIQUID: ("https://hyperliquid.gitbook.io/hyperliquid-docs/",),
    ResearchVenue.DYDX: (
        "https://help.dydx.trade/",
        "https://github.com/dydxprotocol/",
        "https://docs.dydx.community/",
        "https://docs.dydx.xyz/",
    ),
    ResearchVenue.LIGHTER: (
        "https://docs.lighter.xyz/",
        "https://apidocs.lighter.xyz/",
        "https://lighter.xyz/",
        "https://assets.lighter.xyz/",
    ),
}
RESEARCH_ONLY_WARNING = "Research only — no trading authority."


class DossierCheckKind(StrEnum):
    ASSET_AND_QUANTITY = "asset_and_quantity"
    PAYOFF_AND_QUOTE = "payoff_and_quote"
    COLLATERAL_AND_PNL = "collateral_and_pnl"
    ORACLE_CONSTRUCTION = "oracle_construction"
    MARK_AND_MARGIN = "mark_and_margin"
    LIQUIDATION = "liquidation"
    AUTO_DELEVERAGING = "auto_deleveraging"
    FUNDING_INTERVAL = "funding_interval"
    FUNDING_FORMULA = "funding_formula"
    FUNDING_CAP = "funding_cap"
    ORDER_CONSTRAINTS = "order_constraints"
    FEE_SCHEDULE = "fee_schedule"
    VENUE_FAILURE_DOMAIN = "venue_failure_domain"
    ACCESS_ELIGIBILITY = "access_eligibility"


CANONICAL_DOSSIER_CHECKS = tuple(DossierCheckKind)


class DossierJudgment(StrEnum):
    MATCHED = "matched"
    BLOCKING = "blocking"
    MODEL_REQUIRED = "model_required"
    MISSING_EVIDENCE = "missing_evidence"


class DossierStatus(StrEnum):
    INELIGIBLE = "ineligible"
    EVIDENCE_INCOMPLETE = "evidence_incomplete"
    MODEL_REQUIRED = "model_required"
    COMPATIBLE = "compatible"


class DossierSource(StrictRecord):
    schema_version: Literal[1]
    source_id: str
    venue: ResearchVenue
    url: str
    title: str
    observed_at: datetime
    evidence_excerpt: str
    excerpt_sha256: str

    @field_validator("observed_at")
    @classmethod
    def normalize_observed_at(cls, value: datetime) -> datetime:
        return normalize_utc_timestamp(value)

    @field_validator("source_id")
    @classmethod
    def require_source_id(cls, value: str) -> str:
        if _TOKEN_PATTERN.fullmatch(value) is None:
            raise ValueError("source ID must be a lowercase token")
        return value

    @field_validator("title")
    @classmethod
    def require_title(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("source title must not be blank")
        return value

    @field_validator("evidence_excerpt")
    @classmethod
    def require_excerpt(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("evidence excerpt must not be blank")
        return value

    @model_validator(mode="after")
    def require_verifiable_official_excerpt(self) -> DossierSource:
        if not self.url.startswith("https://"):
            raise ValueError("source URL must use HTTPS")
        prefixes = _OFFICIAL_SOURCE_PREFIXES.get(self.venue, ())
        if not any(self.url.startswith(prefix) for prefix in prefixes):
            raise ValueError("source URL must be an official source for venue")
        expected_hash = sha256(self.evidence_excerpt.encode("utf-8")).hexdigest()
        if self.excerpt_sha256 != expected_hash:
            raise ValueError("excerpt hash must match the exact stored UTF-8 excerpt")
        return self


class DossierCheck(StrictRecord):
    schema_version: Literal[1]
    kind: DossierCheckKind
    judgment: DossierJudgment
    reason_code: str
    left_summary: str
    right_summary: str
    source_ids: tuple[str, ...]

    @field_validator("reason_code")
    @classmethod
    def require_reason_code(cls, value: str) -> str:
        if _TOKEN_PATTERN.fullmatch(value) is None:
            raise ValueError("reason code must be a lowercase token")
        return value

    @field_validator("left_summary", "right_summary")
    @classmethod
    def require_summary(cls, value: str, info: object) -> str:
        if not value.strip():
            field_name = getattr(info, "field_name", "summary").replace("_", " ")
            raise ValueError(f"{field_name} must not be blank")
        return value

    @field_validator("source_ids")
    @classmethod
    def require_source_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or len(set(value)) != len(value):
            raise ValueError("check source IDs must be nonempty and unique")
        if any(_TOKEN_PATTERN.fullmatch(source_id) is None for source_id in value):
            raise ValueError("check source IDs must be lowercase tokens")
        return value


class ContractCompatibilityDossier(StrictRecord):
    schema_version: Literal[1]
    dossier_id: str
    left_venue: ResearchVenue
    right_venue: ResearchVenue
    assets: tuple[Asset, ...]
    observed_at: datetime
    decision_scope: Literal["research_only"]
    warning: Literal["Research only — no trading authority."]
    sources: tuple[DossierSource, ...]
    checks: tuple[DossierCheck, ...]

    @field_validator("dossier_id")
    @classmethod
    def require_dossier_id(cls, value: str) -> str:
        if _DOSSIER_ID_PATTERN.fullmatch(value) is None:
            raise ValueError("dossier ID must be lowercase hyphenated tokens")
        return value

    @field_validator("observed_at")
    @classmethod
    def normalize_observed_at(cls, value: datetime) -> datetime:
        return normalize_utc_timestamp(value)

    @field_validator("assets")
    @classmethod
    def require_canonical_assets(cls, value: tuple[Asset, ...]) -> tuple[Asset, ...]:
        canonical = tuple(asset for asset in _ASSET_ORDER if asset in value)
        if not value or len(set(value)) != len(value) or value != canonical:
            raise ValueError("assets must be unique canonical order")
        return value

    @field_validator("checks")
    @classmethod
    def require_canonical_checks(cls, value: tuple[DossierCheck, ...]) -> tuple[DossierCheck, ...]:
        if tuple(check.kind for check in value) != CANONICAL_DOSSIER_CHECKS:
            raise ValueError("checks must cover every kind in canonical order")
        return value

    @model_validator(mode="after")
    def require_coherent_dossier(self) -> ContractCompatibilityDossier:
        if self.left_venue is self.right_venue:
            raise ValueError("dossier venues must be distinct")
        if {source.venue for source in self.sources} != {self.left_venue, self.right_venue}:
            raise ValueError("source venues must exactly cover the compared venues")
        source_ids = tuple(source.source_id for source in self.sources)
        if not source_ids or len(set(source_ids)) != len(source_ids):
            raise ValueError("dossier source IDs must be nonempty and unique")
        if any(source.observed_at > self.observed_at for source in self.sources):
            raise ValueError("source observation must not follow dossier observation")
        known_sources = set(source_ids)
        cited_sources = {source_id for check in self.checks for source_id in check.source_ids}
        unknown_sources = cited_sources - known_sources
        if unknown_sources:
            raise ValueError(f"check references unknown source: {min(unknown_sources)}")
        uncited_sources = known_sources - cited_sources
        if uncited_sources:
            raise ValueError(f"dossier contains uncited source: {min(uncited_sources)}")
        return self


NonnegativeInt = Annotated[int, Field(ge=0)]


class DossierJudgmentCounts(StrictRecord):
    matched: NonnegativeInt
    blocking: NonnegativeInt
    model_required: NonnegativeInt
    missing_evidence: NonnegativeInt


class ContractDossierReport(StrictRecord):
    schema_version: Literal[1]
    dossier_id: str
    left_venue: ResearchVenue
    right_venue: ResearchVenue
    assets: tuple[Asset, ...]
    observed_at: datetime
    warning: Literal["Research only — no trading authority."]
    status: DossierStatus
    primary_reason_code: str | None
    counts: DossierJudgmentCounts
    sources: tuple[DossierSource, ...]
    checks: tuple[DossierCheck, ...]
    activation_status: Literal["not_authorized"]

    @field_validator("observed_at")
    @classmethod
    def normalize_observed_at(cls, value: datetime) -> datetime:
        return normalize_utc_timestamp(value)

    @model_validator(mode="after")
    def require_count_total(self) -> ContractDossierReport:
        if sum(self.counts.model_dump().values()) != len(self.checks):
            raise ValueError("judgment counts must cover every dossier check")
        if self.status is DossierStatus.COMPATIBLE and self.primary_reason_code is not None:
            raise ValueError("compatible report must not have a primary reason")
        if self.status is not DossierStatus.COMPATIBLE and self.primary_reason_code is None:
            raise ValueError("non-compatible report must have a primary reason")
        return self
