from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from itertools import pairwise

from polytrading.carry.dossier import evaluate_dossier, load_bundled_dossier
from polytrading.carry.dossier_models import ContractDossierReport, DossierStatus
from polytrading.carry.economics_execution import PairedBookObservation
from polytrading.carry.economics_models import EconomicsPolicy, EvidenceCoverage
from polytrading.domain.models import (
    Asset,
    FeeSchedule,
    FundingObservation,
    InstrumentKind,
    InstrumentSpec,
    Venue,
)
from polytrading.storage.store import DuckDBStore
from polytrading.trial.funding_lineage import select_prospective_funding
from polytrading.venues.synchronized import BookCollectionCycle

_VENUES = (Venue.DYDX, Venue.LIGHTER)
_SYMBOLS = {
    Asset.BTC: {Venue.DYDX: "BTC-USD", Venue.LIGHTER: "BTC"},
    Asset.ETH: {Venue.DYDX: "ETH-USD", Venue.LIGHTER: "ETH"},
    Asset.SOL: {Venue.DYDX: "SOL-USD", Venue.LIGHTER: "SOL"},
}


@dataclass(frozen=True)
class PairedFundingObservation:
    effective_at: datetime
    dydx: FundingObservation
    lighter: FundingObservation


@dataclass(frozen=True)
class EconomicsEvidenceBundle:
    policy: EconomicsPolicy
    training_start: datetime
    training_end: datetime
    evaluation_end: datetime
    dossier: ContractDossierReport
    instruments: tuple[InstrumentSpec, InstrumentSpec]
    fees: tuple[FeeSchedule, FeeSchedule]
    funding_pairs: tuple[PairedFundingObservation, ...]
    hourly_books: tuple[PairedBookObservation, ...]
    dense_books: tuple[PairedBookObservation, ...]
    latest_books: PairedBookObservation
    coverage: EvidenceCoverage
    source_hashes: tuple[str, ...]


@dataclass(frozen=True)
class EconomicsAssemblyResult:
    policy: EconomicsPolicy
    coverage: EvidenceCoverage
    source_hashes: tuple[str, ...]
    reason_codes: tuple[str, ...]
    bundle: EconomicsEvidenceBundle | None


@dataclass(frozen=True)
class _CyclePair:
    cycle: BookCollectionCycle
    pair: PairedBookObservation


def _duration_microseconds(value: timedelta) -> int:
    return value.days * 86_400_000_000 + value.seconds * 1_000_000 + value.microseconds


def _duration_seconds(value: timedelta) -> Decimal:
    return Decimal(_duration_microseconds(value)) / Decimal(1_000_000)


def _duration_milliseconds(value: timedelta) -> Decimal:
    return Decimal(_duration_microseconds(value)) / Decimal(1_000)


class EconomicsEvidenceAssembler:
    def __init__(self, store: DuckDBStore) -> None:
        self._store = store

    def assemble(self, policy: EconomicsPolicy) -> EconomicsAssemblyResult:
        reasons: set[str] = set()
        source_hashes = {
            policy.operational_source_hash,
            *(item.source_hash for item in policy.execution_assumptions),
            *(item.source_hash for item in policy.margin_assumptions),
        }
        study_days = policy.training_days + policy.evaluation_days
        training_start = policy.study_end - timedelta(days=study_days)
        training_end = policy.study_end - timedelta(days=policy.evaluation_days)

        dossier = self._dossier(policy, reasons, source_hashes)
        instruments = self._instruments(policy, reasons, source_hashes)
        fees = self._fees(policy, reasons, source_hashes)
        funding_pairs, funding_counts = self._funding(
            policy, training_start, training_end, reasons, source_hashes
        )
        hourly_books, dense_books, latest_cycle_pair, book_count = self._books(
            policy, training_end, reasons, source_hashes
        )

        latest_age: Decimal | None = None
        latest_skew: Decimal | None = None
        if latest_cycle_pair is not None:
            latest_age = _duration_seconds(
                policy.known_as_of - latest_cycle_pair.cycle.request_completed_at
            )
            latest_skew = _duration_milliseconds(
                abs(
                    latest_cycle_pair.pair.lighter.effective_at
                    - latest_cycle_pair.pair.dydx.effective_at
                )
            )
        latency_sample_count = sum(
            timedelta(0) < right.effective_at - left.effective_at <= timedelta(seconds=5)
            for left, right in pairwise(dense_books)
        )
        requested_training = policy.training_days * 24
        requested_evaluation = policy.evaluation_days * 24
        paired_training, paired_evaluation = funding_counts
        coverage = EvidenceCoverage(
            schema_version=1,
            requested_training_hours=requested_training,
            paired_training_hours=paired_training,
            training_funding_coverage=Decimal(paired_training) / Decimal(requested_training),
            requested_evaluation_hours=requested_evaluation,
            paired_evaluation_hours=paired_evaluation,
            evaluation_funding_coverage=Decimal(paired_evaluation) / Decimal(requested_evaluation),
            requested_funding_hours=requested_training + requested_evaluation,
            paired_funding_hours=paired_training + paired_evaluation,
            funding_coverage=Decimal(paired_training + paired_evaluation)
            / Decimal(requested_training + requested_evaluation),
            requested_book_hours=requested_evaluation,
            paired_book_hours=book_count,
            book_coverage=Decimal(book_count) / Decimal(requested_evaluation),
            latest_book_age_seconds=latest_age,
            latest_pair_skew_ms=latest_skew,
            latency_sample_count=latency_sample_count,
        )
        self._coverage_reasons(policy, coverage, reasons)
        canonical_hashes = tuple(sorted(source_hashes))
        canonical_reasons = tuple(sorted(reasons))
        if canonical_reasons:
            return EconomicsAssemblyResult(
                policy=policy,
                coverage=coverage,
                source_hashes=canonical_hashes,
                reason_codes=canonical_reasons,
                bundle=None,
            )
        if dossier is None or instruments is None or fees is None or latest_cycle_pair is None:
            raise RuntimeError("complete assembly invariant is inconsistent")
        bundle = EconomicsEvidenceBundle(
            policy=policy,
            training_start=training_start,
            training_end=training_end,
            evaluation_end=policy.study_end,
            dossier=dossier,
            instruments=instruments,
            fees=fees,
            funding_pairs=funding_pairs,
            hourly_books=hourly_books,
            dense_books=dense_books,
            latest_books=latest_cycle_pair.pair,
            coverage=coverage,
            source_hashes=canonical_hashes,
        )
        return EconomicsAssemblyResult(
            policy=policy,
            coverage=coverage,
            source_hashes=canonical_hashes,
            reason_codes=(),
            bundle=bundle,
        )

    @staticmethod
    def _dossier(
        policy: EconomicsPolicy, reasons: set[str], source_hashes: set[str]
    ) -> ContractDossierReport | None:
        dossier = load_bundled_dossier("lighter-dydx-core-v1")
        if dossier.observed_at > policy.known_as_of:
            reasons.add("COMPATIBILITY_DOSSIER_MISSING")
            return None
        report = evaluate_dossier(dossier)
        source_hashes.update(source.excerpt_sha256 for source in report.sources)
        if report.status is DossierStatus.EVIDENCE_INCOMPLETE:
            reasons.add("COMPATIBILITY_EVIDENCE_INCOMPLETE")
            return None
        return report

    def _instruments(
        self, policy: EconomicsPolicy, reasons: set[str], source_hashes: set[str]
    ) -> tuple[InstrumentSpec, InstrumentSpec] | None:
        rows: list[InstrumentSpec] = []
        for venue in _VENUES:
            symbol = _SYMBOLS[policy.asset][venue]
            item = self._store.latest_instrument_as_of(venue, symbol, policy.known_as_of)
            if item is None:
                reasons.add(f"INSTRUMENT_{venue.value.upper()}_MISSING")
                continue
            rows.append(item)
            source_hashes.add(item.source_hash)
        if len(rows) != 2:
            return None
        dydx, lighter = rows
        valid = all(
            item.asset is policy.asset
            and item.kind is InstrumentKind.LINEAR_PERPETUAL
            and not item.is_inverse
            and not item.is_prelaunch
            and item.contract_multiplier > 0
            and item.quantity_step is not None
            and item.quantity_step > 0
            and item.min_notional is not None
            and item.min_notional > 0
            and item.funding_interval_hours == 1
            for item in rows
        )
        compatible = (
            dydx.collateral_asset is not None
            and dydx.collateral_asset == lighter.collateral_asset
            and dydx.pnl_asset is not None
            and dydx.pnl_asset == lighter.pnl_asset
            and dydx.index_family is not None
            and dydx.index_family == lighter.index_family
        )
        if not valid or not compatible:
            reasons.add("INSTRUMENT_COMPATIBILITY_INVALID")
            return None
        return dydx, lighter

    def _fees(
        self, policy: EconomicsPolicy, reasons: set[str], source_hashes: set[str]
    ) -> tuple[FeeSchedule, FeeSchedule] | None:
        rows: list[FeeSchedule] = []
        for assumption in policy.execution_assumptions:
            item = self._store.latest_fee_as_of(
                assumption.venue,
                assumption.fee_tier_name,
                policy.known_as_of,
            )
            if item is None:
                reasons.add(f"FEE_{assumption.venue.value.upper()}_MISSING")
                continue
            rows.append(item)
            source_hashes.add(item.source_hash)
        if len(rows) != 2:
            return None
        return rows[0], rows[1]

    def _funding(
        self,
        policy: EconomicsPolicy,
        training_start: datetime,
        training_end: datetime,
        reasons: set[str],
        source_hashes: set[str],
    ) -> tuple[tuple[PairedFundingObservation, ...], tuple[int, int]]:
        selected: dict[Venue, dict[datetime, FundingObservation]] = {}
        for venue in _VENUES:
            symbol = _SYMBOLS[policy.asset][venue]
            selection = select_prospective_funding(
                self._store,
                venue,
                symbol,
                policy.asset,
                training_start,
                policy.study_end,
                policy.known_as_of,
            )
            source_hashes.update(selection.source_hashes)
            if selection.conflict_boundaries:
                reasons.add("FUNDING_REVISION_CONFLICT")
            selected[venue] = {
                observation.effective_at: observation for observation in selection.observations
            }
        expected = tuple(
            training_start + timedelta(hours=hour)
            for hour in range(1, (policy.training_days + policy.evaluation_days) * 24 + 1)
        )
        pairs: list[PairedFundingObservation] = []
        paired_training = 0
        paired_evaluation = 0
        for effective_at in expected:
            dydx = selected[Venue.DYDX].get(effective_at)
            lighter = selected[Venue.LIGHTER].get(effective_at)
            if dydx is None or lighter is None:
                continue
            pair = PairedFundingObservation(
                effective_at=effective_at,
                dydx=dydx,
                lighter=lighter,
            )
            pairs.append(pair)
            if effective_at <= training_end:
                paired_training += 1
            else:
                paired_evaluation += 1
        return tuple(pairs), (paired_training, paired_evaluation)

    def _books(
        self,
        policy: EconomicsPolicy,
        evaluation_start: datetime,
        reasons: set[str],
        source_hashes: set[str],
    ) -> tuple[
        tuple[PairedBookObservation, ...],
        tuple[PairedBookObservation, ...],
        _CyclePair | None,
        int,
    ]:
        cycles = self._store.book_collection_cycles_between(
            evaluation_start - timedelta(minutes=5),
            policy.known_as_of,
            policy.known_as_of,
        )
        eligible: list[_CyclePair] = []
        for cycle in cycles:
            pair = self._eligible_cycle_pair(cycle, policy)
            if pair is None:
                continue
            eligible.append(_CyclePair(cycle=cycle, pair=pair))
            source_hashes.update(cycle.source_hashes)
            source_hashes.update((pair.dydx.source_hash, pair.lighter.source_hash))
        eligible.sort(key=lambda item: (item.cycle.request_completed_at, item.cycle.cycle_id))
        hourly: list[PairedBookObservation] = []
        for hour in range(1, policy.evaluation_days * 24 + 1):
            boundary = evaluation_start + timedelta(hours=hour)
            selected = next(
                (
                    item
                    for item in reversed(eligible)
                    if item.cycle.request_completed_at <= boundary
                    and boundary - item.cycle.request_completed_at
                    <= timedelta(seconds=int(policy.maximum_hourly_book_age_seconds))
                    and item.pair.dydx.observed_at <= boundary
                    and item.pair.lighter.observed_at <= boundary
                ),
                None,
            )
            if selected is not None:
                hourly.append(
                    PairedBookObservation(
                        effective_at=boundary,
                        lighter=selected.pair.lighter,
                        dydx=selected.pair.dydx,
                    )
                )
        dense = tuple(
            item.pair
            for item in eligible
            if evaluation_start < item.cycle.request_completed_at <= policy.known_as_of
        )
        latest = eligible[-1] if eligible else None
        if latest is None:
            reasons.add("BOOK_LATEST_MISSING")
        elif policy.known_as_of - latest.cycle.request_completed_at > timedelta(
            seconds=int(policy.maximum_book_age_seconds)
        ):
            reasons.add("BOOK_LATEST_STALE")
        return tuple(hourly), dense, latest, len(hourly)

    def _eligible_cycle_pair(
        self, cycle: BookCollectionCycle, policy: EconomicsPolicy
    ) -> PairedBookObservation | None:
        if (
            cycle.status != "complete"
            or policy.asset not in cycle.assets
            or not set(_VENUES).issubset(cycle.venues)
            or not cycle.source_hashes
            or cycle.max_effective_skew_ms > policy.maximum_cycle_skew_ms
            or cycle.request_completed_at > policy.known_as_of
        ):
            return None
        books = self._store.books_for_cycle(cycle.cycle_id)
        selected = {
            venue: tuple(
                item for item in books if item.venue is venue and item.asset is policy.asset
            )
            for venue in _VENUES
        }
        if any(len(rows) != 1 for rows in selected.values()):
            return None
        dydx = selected[Venue.DYDX][0]
        lighter = selected[Venue.LIGHTER][0]
        if (
            dydx.symbol != _SYMBOLS[policy.asset][Venue.DYDX]
            or lighter.symbol != _SYMBOLS[policy.asset][Venue.LIGHTER]
            or not dydx.source_hash
            or not lighter.source_hash
            or dydx.source_hash not in cycle.source_hashes
            or lighter.source_hash not in cycle.source_hashes
            or dydx.observed_at > cycle.request_completed_at
            or lighter.observed_at > cycle.request_completed_at
            or dydx.effective_at > cycle.request_completed_at
            or lighter.effective_at > cycle.request_completed_at
            or _duration_milliseconds(abs(dydx.effective_at - lighter.effective_at))
            > policy.maximum_cycle_skew_ms
        ):
            return None
        return PairedBookObservation(
            effective_at=max(dydx.effective_at, lighter.effective_at),
            lighter=lighter,
            dydx=dydx,
        )

    @staticmethod
    def _coverage_reasons(
        policy: EconomicsPolicy, coverage: EvidenceCoverage, reasons: set[str]
    ) -> None:
        if coverage.training_funding_coverage < policy.minimum_coverage:
            reasons.add("FUNDING_TRAINING_COVERAGE_INSUFFICIENT")
        if coverage.evaluation_funding_coverage < policy.minimum_coverage:
            reasons.add("FUNDING_EVALUATION_COVERAGE_INSUFFICIENT")
        if coverage.funding_coverage < policy.minimum_coverage:
            reasons.add("FUNDING_COVERAGE_INSUFFICIENT")
        if coverage.book_coverage < policy.minimum_coverage:
            reasons.add("BOOK_COVERAGE_INSUFFICIENT")
        if coverage.latency_sample_count == 0:
            reasons.add("LATENCY_SAMPLES_MISSING")
