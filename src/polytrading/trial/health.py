from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from uuid import UUID

from polytrading.carry.dossier import load_bundled_dossier
from polytrading.domain.models import Asset, Venue, normalize_utc_timestamp
from polytrading.storage.store import DuckDBStore
from polytrading.trial.book_evidence import (
    EligibleTrialBookPair,
    eligible_lighter_dydx_book_pair,
    select_hourly_trial_books,
)
from polytrading.trial.funding_lineage import select_prospective_funding
from polytrading.trial.funding_models import LighterDydxFundingCycle, TrialFundingCycleStatus
from polytrading.trial.health_models import (
    TRIAL_HEALTH_PROTOCOL_VERSION,
    TRIAL_HEALTH_WARNINGS,
    LighterDydxTrialHealthReport,
    ReviewedFeeEvidenceSummary,
    TrialAssetCoverage,
    TrialBoundaryAssetHealth,
    TrialBoundaryHealth,
    TrialCollectionStatus,
    TrialEvidenceStatus,
    TrialWindowBoundaries,
    resolve_latest_auditable_trial_boundary,
    trial_window_boundaries,
)

_ASSETS = (Asset.BTC, Asset.ETH, Asset.SOL)
_HOUR = timedelta(hours=1)
_TRAINING_HOURS = 720
_EVALUATION_HOURS = 1_440
_TOTAL_HOURS = 2_160
_CURRENT_HOURS = 168
_MINIMUM_TRAINING_COMPLETE = 713
_MINIMUM_EVALUATION_COMPLETE = 1_426
_MINIMUM_TOTAL_COMPLETE = 2_139
_MINIMUM_BOOK_COMPLETE = 1_426
_PROJECTION_SCAN_HOURS = 2_160
_MAXIMUM_HOURLY_BOOK_AGE_SECONDS = Decimal("300")
_MAXIMUM_BOOK_SKEW_MS = Decimal("1000")
_MAXIMUM_FRESH_BOOK_AGE_SECONDS = Decimal("30")
_SYMBOLS = {
    Asset.BTC: {Venue.DYDX: "BTC-USD", Venue.LIGHTER: "BTC"},
    Asset.ETH: {Venue.DYDX: "ETH-USD", Venue.LIGHTER: "ETH"},
    Asset.SOL: {Venue.DYDX: "SOL-USD", Venue.LIGHTER: "SOL"},
}
_ATTEMPT_RANK = {
    TrialFundingCycleStatus.COMPLETE: 0,
    TrialFundingCycleStatus.DEGRADED: 1,
    TrialFundingCycleStatus.LATE: 2,
}
_UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass(frozen=True)
class ProjectedAssetEvidence:
    asset: Asset
    funding_complete: frozenset[datetime]
    book_complete: frozenset[datetime]

    def __post_init__(self) -> None:
        for name, values in (
            ("funding", self.funding_complete),
            ("book", self.book_complete),
        ):
            if not isinstance(values, frozenset):
                raise TypeError(f"{name} evidence must be a frozenset")
            normalized = frozenset(normalize_utc_timestamp(value) for value in values)
            if any(any((value.minute, value.second, value.microsecond)) for value in normalized):
                raise ValueError(f"{name} evidence must align to whole UTC hours")
            object.__setattr__(self, f"{name}_complete", normalized)


@dataclass(frozen=True)
class _AssetAuditEvidence:
    asset: Asset
    funding_complete: frozenset[datetime]
    funding_conflicts: frozenset[datetime]
    funding_cycle_ids: dict[Venue, dict[datetime, UUID]]
    hourly_books: dict[datetime, EligibleTrialBookPair]
    dense_books: tuple[EligibleTrialBookPair, ...]
    latest_book: EligibleTrialBookPair | None


class LighterDydxTrialHealthAuditor:
    def __init__(self, store: DuckDBStore) -> None:
        self._store = store

    def audit(self, as_of: datetime, recent_hours: int) -> LighterDydxTrialHealthReport:
        if isinstance(recent_hours, bool) or not isinstance(recent_hours, int):
            raise TypeError("recent hours must be an integer")
        if not 1 <= recent_hours <= _TOTAL_HOURS:
            raise ValueError("recent hours must be between 1 and 2160")
        normalized_as_of = normalize_utc_timestamp(as_of)
        latest_boundary = resolve_latest_auditable_trial_boundary(normalized_as_of)
        windows = trial_window_boundaries(latest_boundary)

        cycles = self._store.lighter_dydx_funding_cycles_between(
            _UNIX_EPOCH, latest_boundary, normalized_as_of
        )
        attempts_by_boundary: dict[datetime, list[LighterDydxFundingCycle]] = {}
        for cycle in cycles:
            attempts_by_boundary.setdefault(cycle.cycle_end, []).append(cycle)
        trial_started_at = min(
            (
                cycle.cycle_end
                for cycle in cycles
                if cycle.request_started_at <= cycle.cycle_end + timedelta(minutes=5)
            ),
            default=None,
        )

        source_hashes: set[str] = set()
        asset_evidence = tuple(
            self._asset_evidence(
                asset,
                windows,
                normalized_as_of,
                trial_started_at,
                source_hashes,
            )
            for asset in _ASSETS
        )
        projected = project_earliest_evaluation_end(
            trial_started_at,
            latest_boundary,
            tuple(
                ProjectedAssetEvidence(
                    asset=item.asset,
                    funding_complete=item.funding_complete,
                    book_complete=frozenset(item.hourly_books),
                )
                for item in asset_evidence
            ),
        )
        recent_boundaries = self._recent_boundaries(
            trial_started_at,
            latest_boundary,
            recent_hours,
            attempts_by_boundary,
            asset_evidence,
        )
        assets = tuple(
            self._coverage(item, windows, normalized_as_of, projected) for item in asset_evidence
        )

        dossier = load_bundled_dossier("lighter-dydx-core-v1")
        dossier_available = dossier.observed_at <= normalized_as_of
        if dossier_available:
            source_hashes.update(source.excerpt_sha256 for source in dossier.sources)

        reviewed_fees = tuple(
            sorted(
                (
                    ReviewedFeeEvidenceSummary(
                        schema_version=1,
                        venue=item.venue,
                        tier_name=item.tier_name,
                        effective_from=item.effective_from,
                        observed_at=item.observed_at,
                        source_hash=item.source_hash,
                    )
                    for item in self._store.reviewed_fee_schedules_as_of(normalized_as_of)
                    if item.venue in (Venue.DYDX, Venue.LIGHTER)
                ),
                key=lambda item: (
                    (Venue.DYDX, Venue.LIGHTER).index(item.venue),
                    item.tier_name,
                    item.effective_from,
                    item.observed_at,
                    item.source_hash,
                ),
            )
        )
        source_hashes.update(item.source_hash for item in reviewed_fees)

        if trial_started_at is None:
            status = TrialCollectionStatus.NOT_STARTED
            elapsed = 0
        else:
            elapsed = int((latest_boundary - trial_started_at) // _HOUR) + 1
            if any(item.status is not TrialEvidenceStatus.COMPLETE for item in recent_boundaries):
                status = TrialCollectionStatus.DEGRADED
            elif all(item.historical_windows_mature and item.fresh_book_ready for item in assets):
                status = TrialCollectionStatus.READY_FOR_ECONOMICS_EVALUATION
            else:
                status = TrialCollectionStatus.COLLECTING

        return LighterDydxTrialHealthReport(
            schema_version=1,
            protocol_version=TRIAL_HEALTH_PROTOCOL_VERSION,
            as_of=normalized_as_of,
            latest_auditable_boundary=latest_boundary,
            recent_hours=recent_hours,
            trial_started_at=trial_started_at,
            elapsed_auditable_hours=elapsed,
            status=status,
            recent_boundaries=recent_boundaries,
            assets=assets,
            dossier_available=dossier_available,
            reviewed_fees=reviewed_fees,
            source_hashes=tuple(sorted(source_hashes)),
            warnings=TRIAL_HEALTH_WARNINGS,
        )

    def _asset_evidence(
        self,
        asset: Asset,
        windows: TrialWindowBoundaries,
        as_of: datetime,
        trial_started_at: datetime | None,
        source_hashes: set[str],
    ) -> _AssetAuditEvidence:
        total_boundaries = windows.total_funding
        selection_start = total_boundaries[0] - _HOUR
        funding_cycle_ids: dict[Venue, dict[datetime, UUID]] = {}
        funding_hashes: dict[Venue, dict[datetime, str]] = {}
        complete_by_venue: dict[Venue, set[datetime]] = {}
        conflicts: set[datetime] = set()
        for venue in (Venue.DYDX, Venue.LIGHTER):
            selection = select_prospective_funding(
                self._store,
                venue,
                _SYMBOLS[asset][venue],
                asset,
                selection_start,
                total_boundaries[-1],
                as_of,
            )
            ids = {
                observation.effective_at: cycle_id
                for observation, cycle_id in zip(
                    selection.observations, selection.selected_cycle_ids, strict=True
                )
            }
            funding_cycle_ids[venue] = ids
            funding_hashes[venue] = {
                observation.effective_at: observation.source_hash
                for observation in selection.observations
            }
            complete_by_venue[venue] = set(ids)
            conflicts.update(selection.conflict_boundaries)
        funding_complete = complete_by_venue[Venue.DYDX] & complete_by_venue[Venue.LIGHTER]
        for boundary in funding_complete:
            for venue in (Venue.DYDX, Venue.LIGHTER):
                source_hashes.add(funding_hashes[venue][boundary])

        hourly = (
            ()
            if trial_started_at is None
            else tuple(
                item
                for item in select_hourly_trial_books(
                    self._store,
                    asset,
                    selection_start,
                    total_boundaries[-1],
                    as_of,
                    _MAXIMUM_HOURLY_BOOK_AGE_SECONDS,
                    _MAXIMUM_BOOK_SKEW_MS,
                )
                if item.pair.effective_at >= trial_started_at
            )
        )
        hourly_by_boundary = {item.pair.effective_at: item for item in hourly}

        all_book_cycles = (
            ()
            if trial_started_at is None
            else self._store.book_collection_cycles_between(_UNIX_EPOCH, as_of, as_of)
        )
        all_eligible = tuple(
            item
            for cycle in all_book_cycles
            if (
                item := eligible_lighter_dydx_book_pair(
                    self._store, cycle, asset, as_of, _MAXIMUM_BOOK_SKEW_MS
                )
            )
            is not None
        )
        prospective_cutoff = (
            None
            if trial_started_at is None
            else trial_started_at - timedelta(seconds=int(_MAXIMUM_HOURLY_BOOK_AGE_SECONDS))
        )
        prospective_eligible = tuple(
            item
            for item in all_eligible
            if prospective_cutoff is not None
            and item.cycle.request_completed_at >= prospective_cutoff
        )
        evaluation_start = windows.evaluation_books[0] - _HOUR
        dense = tuple(
            sorted(
                (
                    item
                    for item in prospective_eligible
                    if evaluation_start < item.cycle.request_completed_at <= as_of
                ),
                key=lambda item: (item.pair.effective_at, item.cycle.cycle_id),
            )
        )
        latest = max(
            prospective_eligible,
            key=lambda item: (item.cycle.request_completed_at, item.cycle.cycle_id),
            default=None,
        )
        used_books = {item.cycle.cycle_id: item for item in (*hourly, *dense)}
        if latest is not None:
            used_books[latest.cycle.cycle_id] = latest
        for item in used_books.values():
            source_hashes.update(item.cycle.source_hashes)
            source_hashes.update((item.pair.dydx.source_hash, item.pair.lighter.source_hash))

        return _AssetAuditEvidence(
            asset=asset,
            funding_complete=frozenset(funding_complete),
            funding_conflicts=frozenset(conflicts),
            funding_cycle_ids=funding_cycle_ids,
            hourly_books=hourly_by_boundary,
            dense_books=dense,
            latest_book=latest,
        )

    def _recent_boundaries(
        self,
        trial_started_at: datetime | None,
        latest_boundary: datetime,
        recent_hours: int,
        attempts_by_boundary: dict[datetime, list[LighterDydxFundingCycle]],
        evidence: tuple[_AssetAuditEvidence, ...],
    ) -> tuple[TrialBoundaryHealth, ...]:
        if trial_started_at is None:
            return ()
        first = max(trial_started_at, latest_boundary - timedelta(hours=recent_hours - 1))
        evidence_by_asset = {item.asset: item for item in evidence}
        rows: list[TrialBoundaryHealth] = []
        boundary = first
        while boundary <= latest_boundary:
            attempts = attempts_by_boundary.get(boundary, [])
            best = min(
                attempts,
                key=lambda item: (
                    _ATTEMPT_RANK[item.status],
                    item.request_completed_at,
                    item.cycle_id,
                ),
                default=None,
            )
            late_only = best is not None and best.status is TrialFundingCycleStatus.LATE
            assets = tuple(
                self._boundary_asset(evidence_by_asset[asset], boundary, late_only)
                for asset in _ASSETS
            )
            status = min(
                (
                    *(item.funding_status for item in assets),
                    *(item.book_status for item in assets),
                ),
                key=_evidence_rank,
            )
            complete_attempts = sum(
                item.status is TrialFundingCycleStatus.COMPLETE for item in attempts
            )
            degraded_attempts = sum(
                item.status is TrialFundingCycleStatus.DEGRADED for item in attempts
            )
            late_attempts = sum(item.status is TrialFundingCycleStatus.LATE for item in attempts)
            reasons = {reason for item in assets for reason in item.reason_codes}
            if status is TrialEvidenceStatus.MISSING:
                reasons.add("BOUNDARY_MISSING")
            elif status is TrialEvidenceStatus.LATE:
                reasons.add("BOUNDARY_LATE_ONLY")
            elif status is TrialEvidenceStatus.DEGRADED:
                reasons.add("BOUNDARY_DEGRADED_ONLY")
            if len(attempts) > 1:
                reasons.add("MULTIPLE_ATTEMPTS")
            if complete_attempts > 1:
                reasons.add("MULTIPLE_COMPLETE_ATTEMPTS")
            rows.append(
                TrialBoundaryHealth(
                    schema_version=1,
                    cycle_end=boundary,
                    status=status,
                    attempt_count=len(attempts),
                    complete_attempt_count=complete_attempts,
                    degraded_attempt_count=degraded_attempts,
                    late_attempt_count=late_attempts,
                    assets=assets,
                    reason_codes=tuple(sorted(reasons)),
                )
            )
            boundary += _HOUR
        return tuple(rows)

    @staticmethod
    def _boundary_asset(
        evidence: _AssetAuditEvidence, boundary: datetime, late_only: bool
    ) -> TrialBoundaryAssetHealth:
        if boundary in evidence.funding_complete:
            funding_status = TrialEvidenceStatus.COMPLETE
            funding_ids = tuple(
                sorted(
                    {
                        evidence.funding_cycle_ids[venue][boundary]
                        for venue in (Venue.DYDX, Venue.LIGHTER)
                    }
                )
            )
        elif boundary in evidence.funding_conflicts:
            funding_status = TrialEvidenceStatus.DEGRADED
            funding_ids = ()
        else:
            funding_status = TrialEvidenceStatus.LATE if late_only else TrialEvidenceStatus.MISSING
            funding_ids = ()
        selected_book = evidence.hourly_books.get(boundary)
        book_status = (
            TrialEvidenceStatus.COMPLETE
            if selected_book is not None
            else TrialEvidenceStatus.LATE
            if late_only
            else TrialEvidenceStatus.MISSING
        )
        reasons: set[str] = set()
        if funding_status is TrialEvidenceStatus.DEGRADED:
            reasons.add(funding_conflict_reason(evidence.asset))
        elif funding_status is not TrialEvidenceStatus.COMPLETE:
            reasons.add(funding_missing_reason(evidence.asset))
        if book_status is not TrialEvidenceStatus.COMPLETE:
            reasons.add(book_missing_reason(evidence.asset))
        return TrialBoundaryAssetHealth(
            schema_version=1,
            asset=evidence.asset,
            funding_status=funding_status,
            book_status=book_status,
            selected_funding_cycle_ids=funding_ids,
            selected_book_cycle_id=None if selected_book is None else selected_book.cycle.cycle_id,
            reason_codes=tuple(sorted(reasons)),
        )

    @staticmethod
    def _coverage(
        evidence: _AssetAuditEvidence,
        windows: TrialWindowBoundaries,
        as_of: datetime,
        projected: datetime | None,
    ) -> TrialAssetCoverage:
        funding = evidence.funding_complete
        books = frozenset(evidence.hourly_books)
        missing_training = tuple(
            boundary for boundary in windows.training_funding if boundary not in funding
        )
        missing_evaluation = tuple(
            boundary for boundary in windows.evaluation_funding if boundary not in funding
        )
        missing_books = tuple(
            boundary for boundary in windows.evaluation_books if boundary not in books
        )
        missing_current = tuple(
            boundary for boundary in windows.current_funding if boundary not in funding
        )
        paired_training = _TRAINING_HOURS - len(missing_training)
        paired_evaluation = _EVALUATION_HOURS - len(missing_evaluation)
        paired_books = _EVALUATION_HOURS - len(missing_books)
        paired_current = _CURRENT_HOURS - len(missing_current)

        consecutive_dense = sum(
            timedelta(0) < right.pair.effective_at - left.pair.effective_at <= timedelta(seconds=5)
            for left, right in pairwise(evidence.dense_books)
        )
        latest_completed: datetime | None = None
        latest_age: Decimal | None = None
        latest_skew: Decimal | None = None
        if evidence.latest_book is not None:
            latest_completed = evidence.latest_book.cycle.request_completed_at
            latest_age = _duration_seconds(as_of - latest_completed)
            latest_skew = _duration_milliseconds(
                abs(
                    evidence.latest_book.pair.dydx.effective_at
                    - evidence.latest_book.pair.lighter.effective_at
                )
            )
        fresh = (
            latest_age is not None
            and latest_skew is not None
            and latest_age <= _MAXIMUM_FRESH_BOOK_AGE_SECONDS
            and latest_skew <= _MAXIMUM_BOOK_SKEW_MS
        )
        training_coverage = Decimal(paired_training) / Decimal(_TRAINING_HOURS)
        evaluation_coverage = Decimal(paired_evaluation) / Decimal(_EVALUATION_HOURS)
        total_paired = paired_training + paired_evaluation
        total_coverage = Decimal(total_paired) / Decimal(_TOTAL_HOURS)
        book_coverage = Decimal(paired_books) / Decimal(_EVALUATION_HOURS)
        mature = (
            paired_training >= _MINIMUM_TRAINING_COMPLETE
            and paired_evaluation >= _MINIMUM_EVALUATION_COMPLETE
            and total_paired >= _MINIMUM_TOTAL_COMPLETE
            and paired_books >= _MINIMUM_BOOK_COMPLETE
            and paired_current == _CURRENT_HOURS
            and consecutive_dense >= 1
        )
        reasons: set[str] = set()
        if paired_training < _MINIMUM_TRAINING_COMPLETE:
            reasons.add("FUNDING_TRAINING_COVERAGE_INSUFFICIENT")
        if paired_evaluation < _MINIMUM_EVALUATION_COMPLETE:
            reasons.add("FUNDING_EVALUATION_COVERAGE_INSUFFICIENT")
        if total_paired < _MINIMUM_TOTAL_COMPLETE:
            reasons.add("FUNDING_COVERAGE_INSUFFICIENT")
        if paired_books < _MINIMUM_BOOK_COMPLETE:
            reasons.add("BOOK_COVERAGE_INSUFFICIENT")
        if paired_current != _CURRENT_HOURS:
            reasons.add("CURRENT_FUNDING_WINDOW_INSUFFICIENT")
        if consecutive_dense < 1:
            reasons.add("LATENCY_SAMPLES_MISSING")
        if latest_completed is None:
            reasons.add("BOOK_LATEST_MISSING")
        else:
            assert latest_age is not None and latest_skew is not None
            if latest_age > _MAXIMUM_FRESH_BOOK_AGE_SECONDS:
                reasons.add("BOOK_LATEST_STALE")
            if latest_skew > _MAXIMUM_BOOK_SKEW_MS:
                reasons.add("BOOK_LATEST_SKEW_EXCEEDED")
        return TrialAssetCoverage(
            schema_version=1,
            asset=evidence.asset,
            requested_training_funding_hours=_TRAINING_HOURS,
            paired_training_funding_hours=paired_training,
            training_funding_coverage=training_coverage,
            missing_training_funding_boundaries=missing_training,
            requested_evaluation_funding_hours=_EVALUATION_HOURS,
            paired_evaluation_funding_hours=paired_evaluation,
            evaluation_funding_coverage=evaluation_coverage,
            missing_evaluation_funding_boundaries=missing_evaluation,
            requested_total_funding_hours=_TOTAL_HOURS,
            paired_total_funding_hours=total_paired,
            total_funding_coverage=total_coverage,
            requested_book_hours=_EVALUATION_HOURS,
            paired_book_hours=paired_books,
            book_coverage=book_coverage,
            missing_book_boundaries=missing_books,
            current_funding_paired_hours=paired_current,
            current_funding_consecutive=paired_current == _CURRENT_HOURS,
            missing_current_funding_boundaries=missing_current,
            dense_book_pair_count=len(evidence.dense_books),
            consecutive_dense_sample_count=consecutive_dense,
            latest_funding_boundary=max(funding, default=None),
            latest_book_completed_at=latest_completed,
            latest_book_age_seconds=latest_age,
            latest_book_skew_ms=latest_skew,
            fresh_book_ready=fresh,
            historical_windows_mature=mature,
            projected_earliest_evaluation_end=projected,
            reason_codes=tuple(sorted(reasons)),
        )


def funding_missing_reason(asset: Asset) -> str:
    return f"FUNDING_{asset.value}_MISSING"


def funding_conflict_reason(asset: Asset) -> str:
    return f"FUNDING_{asset.value}_REVISION_CONFLICT"


def book_missing_reason(asset: Asset) -> str:
    return f"BOOK_{asset.value}_MISSING"


def _duration_microseconds(value: timedelta) -> int:
    return value.days * 86_400_000_000 + value.seconds * 1_000_000 + value.microseconds


def _duration_seconds(value: timedelta) -> Decimal:
    return Decimal(_duration_microseconds(value)) / Decimal(1_000_000)


def _duration_milliseconds(value: timedelta) -> Decimal:
    return Decimal(_duration_microseconds(value)) / Decimal(1_000)


def _evidence_rank(status: TrialEvidenceStatus) -> int:
    return {
        TrialEvidenceStatus.MISSING: 0,
        TrialEvidenceStatus.LATE: 1,
        TrialEvidenceStatus.DEGRADED: 2,
        TrialEvidenceStatus.COMPLETE: 3,
    }[status]


def project_earliest_evaluation_end(
    trial_started_at: datetime | None,
    latest_auditable_boundary: datetime,
    evidence: tuple[ProjectedAssetEvidence, ...],
) -> datetime | None:
    """Project collection maturity with known history and complete future boundaries."""
    latest = normalize_utc_timestamp(latest_auditable_boundary)
    if any((latest.minute, latest.second, latest.microsecond)):
        raise ValueError("latest auditable boundary must align to a whole UTC hour")
    if trial_started_at is None:
        return None
    start = normalize_utc_timestamp(trial_started_at)
    if any((start.minute, start.second, start.microsecond)):
        raise ValueError("trial start must align to a whole UTC hour")
    if start > latest:
        raise ValueError("trial start must not follow latest auditable boundary")
    if tuple(item.asset for item in evidence) != _ASSETS:
        raise ValueError("projection evidence must use canonical BTC/ETH/SOL order")

    try:
        candidate = start + timedelta(hours=_TOTAL_HOURS - 1)
        maximum_candidate = latest + timedelta(hours=_PROJECTION_SCAN_HOURS)
    except OverflowError:
        return None
    while candidate <= maximum_candidate:
        if all(_asset_projection_complete(candidate, latest, item) for item in evidence):
            return candidate
        try:
            candidate += _HOUR
        except OverflowError:
            return None
    return None


def _asset_projection_complete(
    candidate: datetime, latest: datetime, evidence: ProjectedAssetEvidence
) -> bool:
    try:
        total = tuple(
            candidate - timedelta(hours=_TOTAL_HOURS - index - 1) for index in range(_TOTAL_HOURS)
        )
    except OverflowError:
        return False
    training = total[:_TRAINING_HOURS]
    evaluation = total[_TRAINING_HOURS:]
    current = total[-_CURRENT_HOURS:]

    def funding_complete(boundary: datetime) -> bool:
        return boundary > latest or boundary in evidence.funding_complete

    def book_complete(boundary: datetime) -> bool:
        return boundary > latest or boundary in evidence.book_complete

    paired_training = sum(funding_complete(boundary) for boundary in training)
    paired_evaluation = sum(funding_complete(boundary) for boundary in evaluation)
    return (
        paired_training >= _MINIMUM_TRAINING_COMPLETE
        and paired_evaluation >= _MINIMUM_EVALUATION_COMPLETE
        and paired_training + paired_evaluation >= _MINIMUM_TOTAL_COMPLETE
        and sum(book_complete(boundary) for boundary in evaluation) >= _MINIMUM_BOOK_COMPLETE
        and all(funding_complete(boundary) for boundary in current)
    )
