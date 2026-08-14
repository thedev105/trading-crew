import ast
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

from polytrading.carry.dossier import evaluate_dossier, load_bundled_dossier
from polytrading.carry.dossier_models import DossierStatus
from polytrading.carry.economics import CandidateEconomicsEvaluator
from polytrading.carry.economics_assembler import (
    EconomicsAssemblyResult,
    EconomicsEvidenceBundle,
    PairedFundingObservation,
)
from polytrading.carry.economics_execution import PairedBookObservation
from polytrading.carry.economics_models import EconomicsDecision, FundingDirection
from polytrading.domain.models import Asset, FundingObservation, Venue
from tests.carry.test_economics_models import (
    KNOWN_AS_OF,
    STUDY_END,
    coverage,
    fee_schedule,
    policy,
)
from tests.domain.factories import book_snapshot, instrument_spec

EVALUATION_ID = UUID("00000000-0000-0000-0000-000000000801")


def test_evaluator_import_boundary_has_no_nondeterministic_dependencies() -> None:
    source = Path("src/polytrading/carry/economics.py").read_text()
    tree = ast.parse(source)
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module != "polytrading.carry.economics_assembler"
    }
    prohibited = (
        "polytrading.storage",
        "polytrading.venues",
        "polytrading.ai",
        "httpx",
        "requests",
        "os",
        "random",
        "time",
    )

    assert not any(
        name == prefix or name.startswith(f"{prefix}.")
        for name in imported
        for prefix in prohibited
    )


def funding(venue: Venue, rate: Decimal, effective_at) -> FundingObservation:
    return FundingObservation(
        schema_version=1,
        venue=venue,
        symbol="BTC-USD" if venue is Venue.DYDX else "BTC",
        asset=Asset.BTC,
        rate=rate,
        interval_hours=Decimal("1"),
        effective_at=effective_at,
        observed_at=effective_at + timedelta(minutes=1),
        source_hash=("1" if venue is Venue.DYDX else "2") * 64,
    )


def complete_bundle(training_rate: Decimal) -> EconomicsEvidenceBundle:
    item = policy()
    training_start = STUDY_END - timedelta(days=90)
    training_end = STUDY_END - timedelta(days=60)
    rows = tuple(
        PairedFundingObservation(
            effective_at=effective_at,
            dydx=funding(Venue.DYDX, Decimal("0"), effective_at),
            lighter=funding(Venue.LIGHTER, training_rate, effective_at),
        )
        for effective_at in (
            training_start + timedelta(hours=1),
            training_end + timedelta(hours=1),
        )
    )
    return EconomicsEvidenceBundle(
        policy=item,
        training_start=training_start,
        training_end=training_end,
        evaluation_end=STUDY_END,
        dossier=evaluate_dossier(load_bundled_dossier("lighter-dydx-core-v1")),
        instruments=(
            instrument_spec(venue=Venue.DYDX, symbol="BTC-USD", asset=Asset.BTC),
            instrument_spec(venue=Venue.LIGHTER, symbol="BTC", asset=Asset.BTC),
        ),
        fees=(),  # Not consulted before a zero training median is rejected.
        funding_pairs=rows,
        hourly_books=(),
        dense_books=(),
        latest_books=None,  # type: ignore[arg-type]
        coverage=coverage(),
        source_hashes=("1" * 64, "2" * 64),
    )


def paired_book(effective_at) -> PairedBookObservation:
    return PairedBookObservation(
        effective_at=effective_at,
        lighter=book_snapshot(
            venue=Venue.LIGHTER,
            symbol="BTC",
            asset=Asset.BTC,
            effective_at=effective_at,
            observed_at=effective_at,
        ),
        dydx=book_snapshot(
            venue=Venue.DYDX,
            symbol="BTC-USD",
            asset=Asset.BTC,
            effective_at=effective_at,
            observed_at=effective_at,
        ),
    )


def passing_bundle() -> EconomicsEvidenceBundle:
    item = policy()
    training_start = STUDY_END - timedelta(days=90)
    training_end = STUDY_END - timedelta(days=60)
    funding_pairs = tuple(
        PairedFundingObservation(
            effective_at=effective_at,
            dydx=funding(Venue.DYDX, Decimal("0"), effective_at),
            lighter=funding(Venue.LIGHTER, Decimal("0.0004"), effective_at),
        )
        for hour in range(1, 90 * 24 + 1)
        if (effective_at := training_start + timedelta(hours=hour))
    )
    hourly_books = tuple(
        paired_book(training_end + timedelta(hours=hour)) for hour in range(1, 60 * 24 + 1)
    )
    dense_books = (
        paired_book(KNOWN_AS_OF - timedelta(seconds=6)),
        paired_book(KNOWN_AS_OF - timedelta(seconds=5)),
    )
    instruments = (
        instrument_spec(
            instrument_id="dydx:BTC-USD:linear_perpetual",
            venue=Venue.DYDX,
            symbol="BTC-USD",
            asset=Asset.BTC,
            funding_interval_hours=Decimal("1"),
        ),
        instrument_spec(
            instrument_id="lighter:BTC:linear_perpetual",
            venue=Venue.LIGHTER,
            symbol="BTC",
            asset=Asset.BTC,
            funding_interval_hours=Decimal("1"),
        ),
    )
    fees = (fee_schedule(Venue.DYDX), fee_schedule(Venue.LIGHTER))
    dossier = evaluate_dossier(load_bundled_dossier("lighter-dydx-core-v1"))
    source_hashes = tuple(
        sorted(
            {
                item.operational_source_hash,
                *(row.source_hash for row in item.execution_assumptions),
                *(row.source_hash for row in item.margin_assumptions),
                *(row.source_hash for row in fees),
                *(row.source_hash for row in instruments),
                *(row.excerpt_sha256 for row in dossier.sources),
                "1" * 64,
                "2" * 64,
            }
        )
    )
    return EconomicsEvidenceBundle(
        policy=item,
        training_start=training_start,
        training_end=training_end,
        evaluation_end=STUDY_END,
        dossier=dossier,
        instruments=instruments,
        fees=fees,
        funding_pairs=funding_pairs,
        hourly_books=hourly_books,
        dense_books=dense_books,
        latest_books=dense_books[-1],
        coverage=coverage(latency_sample_count=1),
        source_hashes=source_hashes,
    )


def evaluate_bundle(bundle: EconomicsEvidenceBundle):
    return CandidateEconomicsEvaluator().evaluate(
        EconomicsAssemblyResult(
            policy=bundle.policy,
            coverage=bundle.coverage,
            source_hashes=bundle.source_hashes,
            reason_codes=(),
            bundle=bundle,
        ),
        evaluated_at=KNOWN_AS_OF + timedelta(seconds=7),
        evaluation_id=EVALUATION_ID,
    )


def with_evaluation_rate(bundle: EconomicsEvidenceBundle, rate: Decimal) -> EconomicsEvidenceBundle:
    return replace(
        bundle,
        funding_pairs=tuple(
            pair
            if pair.effective_at <= bundle.training_end
            else replace(
                pair,
                lighter=funding(Venue.LIGHTER, rate, pair.effective_at),
            )
            for pair in bundle.funding_pairs
        ),
    )


def with_book_spread(row: PairedBookObservation, bid: Decimal, ask: Decimal):
    return replace(
        row,
        lighter=row.lighter.model_copy(
            update={
                "bids": tuple(
                    level.model_copy(update={"price": bid}) for level in row.lighter.bids
                ),
                "asks": tuple(
                    level.model_copy(update={"price": ask}) for level in row.lighter.asks
                ),
            }
        ),
        dydx=row.dydx.model_copy(
            update={
                "bids": tuple(level.model_copy(update={"price": bid}) for level in row.dydx.bids),
                "asks": tuple(level.model_copy(update={"price": ask}) for level in row.dydx.asks),
            }
        ),
    )


def test_incomplete_assembly_becomes_insufficient_without_fabricated_economics() -> None:
    item = policy()
    result = EconomicsAssemblyResult(
        policy=item,
        coverage=coverage(
            paired_book_hours=0,
            book_coverage=Decimal("0"),
            latest_book_age_seconds=None,
            latest_pair_skew_ms=None,
            latency_sample_count=0,
        ),
        source_hashes=("1" * 64,),
        reason_codes=("BOOK_COVERAGE_INSUFFICIENT",),
        bundle=None,
    )
    evaluated_at = KNOWN_AS_OF + timedelta(seconds=7)

    report = CandidateEconomicsEvaluator().evaluate(
        result,
        evaluated_at=evaluated_at,
        evaluation_id=EVALUATION_ID,
    )

    assert report.decision is EconomicsDecision.INSUFFICIENT_EVIDENCE
    assert report.reason_codes == ("BOOK_COVERAGE_INSUFFICIENT",)
    assert report.source_hashes == ("1" * 64,)
    assert report.evaluated_at == evaluated_at
    assert report.direction is None
    assert report.economics is None


def test_zero_training_median_is_directionless_rejection() -> None:
    bundle = complete_bundle(Decimal("0"))
    result = EconomicsAssemblyResult(
        policy=bundle.policy,
        coverage=bundle.coverage,
        source_hashes=bundle.source_hashes,
        reason_codes=(),
        bundle=bundle,
    )

    report = CandidateEconomicsEvaluator().evaluate(
        result,
        evaluated_at=KNOWN_AS_OF + timedelta(seconds=7),
        evaluation_id=EVALUATION_ID,
    )

    assert report.decision is EconomicsDecision.REJECTED
    assert report.reason_codes == ("TRAINING_FUNDING_MEDIAN_ZERO",)
    assert report.direction is None
    assert report.short_venue is None
    assert report.long_venue is None
    assert report.economics is None


def test_training_median_uses_only_the_training_window() -> None:
    bundle = complete_bundle(Decimal("0"))
    first, second = bundle.funding_pairs
    changed = replace(
        bundle,
        funding_pairs=(
            replace(first, lighter=funding(Venue.LIGHTER, Decimal("0"), first.effective_at)),
            replace(second, lighter=funding(Venue.LIGHTER, Decimal("1"), second.effective_at)),
        ),
    )
    result = EconomicsAssemblyResult(
        policy=changed.policy,
        coverage=changed.coverage,
        source_hashes=changed.source_hashes,
        reason_codes=(),
        bundle=changed,
    )

    report = CandidateEconomicsEvaluator().evaluate(
        result,
        evaluated_at=KNOWN_AS_OF + timedelta(seconds=7),
        evaluation_id=EVALUATION_ID,
    )

    assert report.reason_codes == ("TRAINING_FUNDING_MEDIAN_ZERO",)


def test_complete_economics_passes_only_when_every_gate_passes() -> None:
    report = evaluate_bundle(passing_bundle())

    assert report.decision is EconomicsDecision.SHADOW_CANDIDATE
    assert report.reason_codes == ()
    assert report.economics is not None
    assert tuple(item.holding_days for item in report.economics.horizons) == (7, 14, 28)
    assert report.economics.all_numeric_gates_pass
    assert report.economics.normal_quote_observations == 1440
    assert report.economics.stress_quote_observations == 1440
    seven_day = report.economics.horizons[0]
    assert seven_day.lighter_funding_rate_sum == Decimal("0.0672")
    assert seven_day.dydx_funding_rate_sum == 0
    assert seven_day.lighter_funding_usd == Decimal("13.3056000")
    assert seven_day.dydx_funding_usd == 0
    assert seven_day.gross_funding_usd == Decimal("13.3056000")
    assert seven_day.conservative_funding_rate == (
        seven_day.gross_funding_usd / report.economics.assigned_capital_usd
    )


def test_funding_cashflow_is_calculated_once_per_venue_leg() -> None:
    bundle = passing_bundle()
    changed = replace(
        bundle,
        funding_pairs=tuple(
            pair
            if pair.effective_at <= bundle.training_end
            else replace(
                pair,
                dydx=funding(Venue.DYDX, Decimal("0.0001"), pair.effective_at),
            )
            for pair in bundle.funding_pairs
        ),
    )

    report = evaluate_bundle(changed)

    assert report.economics is not None
    seven_day = report.economics.horizons[0]
    assert seven_day.lighter_funding_rate_sum == Decimal("0.0672")
    assert seven_day.dydx_funding_rate_sum == Decimal("-0.0168")
    assert seven_day.lighter_funding_usd == Decimal("13.3056000")
    assert seven_day.dydx_funding_usd == Decimal("-3.3596640")
    assert seven_day.gross_funding_usd == Decimal("9.9459360")


def test_reverse_direction_funding_components_keep_venue_signs() -> None:
    bundle = passing_bundle()
    changed = replace(
        bundle,
        funding_pairs=tuple(
            replace(
                pair,
                lighter=funding(Venue.LIGHTER, Decimal("-0.0004"), pair.effective_at),
            )
            for pair in bundle.funding_pairs
        ),
    )

    report = evaluate_bundle(changed)

    assert report.direction is FundingDirection.SHORT_DYDX_LONG_LIGHTER
    assert report.economics is not None
    seven_day = report.economics.horizons[0]
    assert seven_day.lighter_funding_rate_sum == Decimal("0.0672")
    assert seven_day.dydx_funding_rate_sum == 0
    assert seven_day.lighter_funding_usd == (
        report.economics.lighter_entry_notional_usd * Decimal("0.0672")
    )
    assert seven_day.gross_funding_usd == seven_day.lighter_funding_usd


def test_complete_depth_failure_is_direction_bearing_rejection_without_results() -> None:
    bundle = passing_bundle()
    shallow = paired_book(KNOWN_AS_OF - timedelta(seconds=5))
    shallow = replace(
        shallow,
        lighter=shallow.lighter.model_copy(
            update={
                "bids": tuple(
                    level.model_copy(update={"quantity": Decimal("0.001")})
                    for level in shallow.lighter.bids
                ),
                "asks": tuple(
                    level.model_copy(update={"quantity": Decimal("0.001")})
                    for level in shallow.lighter.asks
                ),
            }
        ),
        dydx=shallow.dydx.model_copy(
            update={
                "bids": tuple(
                    level.model_copy(update={"quantity": Decimal("0.001")})
                    for level in shallow.dydx.bids
                ),
                "asks": tuple(
                    level.model_copy(update={"quantity": Decimal("0.001")})
                    for level in shallow.dydx.asks
                ),
            }
        ),
    )

    report = evaluate_bundle(replace(bundle, latest_books=shallow))

    assert report.decision is EconomicsDecision.REJECTED
    assert report.reason_codes == ("DEPTH_COMPATIBLE_SIZE_UNAVAILABLE",)
    assert report.direction is not None
    assert report.economics is None


def test_forced_exit_depth_sizes_down_before_economics() -> None:
    bundle = passing_bundle()
    latest = replace(
        bundle.latest_books,
        lighter=bundle.latest_books.lighter.model_copy(
            update={
                "asks": tuple(
                    level.model_copy(update={"quantity": Decimal("1")})
                    for level in bundle.latest_books.lighter.asks
                )
            }
        ),
        dydx=bundle.latest_books.dydx.model_copy(
            update={
                "bids": tuple(
                    level.model_copy(update={"quantity": Decimal("1")})
                    for level in bundle.latest_books.dydx.bids
                )
            }
        ),
    )

    report = evaluate_bundle(replace(bundle, latest_books=latest))

    assert report.economics is not None
    assert report.economics.base_quantity == Decimal("1.000")
    assert "DEPTH_FORCED_EXIT_UNAVAILABLE" not in report.reason_codes
    assert report.direction is not None


def test_current_regime_reversal_is_rejected_without_changing_training_direction() -> None:
    bundle = passing_bundle()
    cutoff = bundle.evaluation_end - timedelta(days=7)
    changed = replace(
        bundle,
        funding_pairs=tuple(
            pair
            if pair.effective_at <= cutoff
            else replace(
                pair,
                lighter=funding(Venue.LIGHTER, Decimal("-0.0002"), pair.effective_at),
            )
            for pair in bundle.funding_pairs
        ),
    )

    report = evaluate_bundle(changed)

    assert report.decision is EconomicsDecision.REJECTED
    assert "CURRENT_FUNDING_REGIME_REVERSED" in report.reason_codes
    assert report.direction is not None
    assert report.economics is not None


def test_current_regime_requires_the_final_168_consecutive_hours() -> None:
    bundle = passing_bundle()
    missing_at = bundle.evaluation_end - timedelta(hours=3)
    changed = replace(
        bundle,
        funding_pairs=tuple(
            pair for pair in bundle.funding_pairs if pair.effective_at != missing_at
        ),
    )

    report = evaluate_bundle(changed)

    assert report.decision is EconomicsDecision.INSUFFICIENT_EVIDENCE
    assert report.reason_codes == ("CURRENT_FUNDING_WINDOW_INSUFFICIENT",)
    assert report.direction is None
    assert report.economics is None


@pytest.mark.parametrize(
    ("evaluation_rate", "reason_code"),
    [
        (Decimal("0.000130"), "HORIZON_7D_NONPOSITIVE"),
        (Decimal("0.000157"), "HORIZON_7D_MINIMUM_PROFIT"),
        (Decimal("0.000145"), "HORIZON_7D_ANNUALIZED_RETURN"),
    ],
)
def test_horizon_gates_reject_exact_threshold_failures(
    evaluation_rate: Decimal, reason_code: str
) -> None:
    report = evaluate_bundle(with_evaluation_rate(passing_bundle(), evaluation_rate))

    assert report.decision is EconomicsDecision.REJECTED
    assert reason_code in report.reason_codes
    assert report.economics is not None


def test_doubled_transaction_cost_gate_is_reported() -> None:
    bundle = passing_bundle()
    changed_policy = bundle.policy.model_copy(update={"operational_cost_usd": Decimal("30")})

    report = evaluate_bundle(replace(bundle, policy=changed_policy))

    assert "DOUBLED_COST_28D_NONPOSITIVE" in report.reason_codes


def test_stress_drawdown_and_liquidation_gates_use_complete_components() -> None:
    bundle = passing_bundle()
    latest = replace(
        bundle.latest_books,
        lighter=bundle.latest_books.lighter.model_copy(
            update={
                "asks": tuple(
                    level.model_copy(update={"price": Decimal("110")})
                    for level in bundle.latest_books.lighter.asks
                )
            }
        ),
        dydx=bundle.latest_books.dydx.model_copy(
            update={
                "bids": tuple(
                    level.model_copy(update={"price": Decimal("90")})
                    for level in bundle.latest_books.dydx.bids
                )
            }
        ),
    )
    stressed_margins = tuple(
        assumption.model_copy(
            update={
                "maintenance_margin_fraction": Decimal("0.95"),
                "close_out_margin_fraction": Decimal("0.94"),
            }
        )
        for assumption in bundle.policy.margin_assumptions
    )
    changed_policy = bundle.policy.model_copy(update={"margin_assumptions": stressed_margins})

    report = evaluate_bundle(replace(bundle, policy=changed_policy, latest_books=latest))

    assert report.economics is not None
    assert "STRESS_LOSS_LIMIT_EXCEEDED" in report.reason_codes
    assert "MODELED_DRAWDOWN_LIMIT_EXCEEDED" in report.reason_codes
    assert "MODELED_LIQUIDATION" in report.reason_codes
    twenty_eight = report.economics.horizons[-1]
    expected_stress = (
        twenty_eight.funding_reversal_reserve_usd
        + report.economics.costs.forced_exit_cost_usd
        + report.economics.costs.latency_reserve_usd
    ) / report.economics.account_equity_usd
    expected_drawdown = (
        twenty_eight.funding_reversal_reserve_usd
        + twenty_eight.basis_divergence_reserve_usd
        + report.economics.costs.forced_exit_cost_usd
        + report.economics.costs.latency_reserve_usd
    ) / report.economics.assigned_capital_usd
    assert report.economics.funding_and_forced_exit_loss_rate == expected_stress
    assert report.economics.modeled_drawdown_rate == expected_drawdown


def test_normal_and_five_second_quote_count_gates_are_distinct() -> None:
    bundle = passing_bundle()
    wide_hourly = tuple(
        with_book_spread(row, Decimal("90"), Decimal("110")) for row in bundle.hourly_books
    )
    normal_failed = evaluate_bundle(replace(bundle, hourly_books=wide_hourly))
    assert "NORMAL_QUOTE_OBSERVATIONS_INSUFFICIENT" in normal_failed.reason_codes

    first, second = bundle.dense_books
    adverse_second = replace(
        second,
        lighter=second.lighter.model_copy(
            update={
                "bids": tuple(
                    level.model_copy(update={"price": Decimal("80")})
                    for level in second.lighter.bids
                )
            }
        ),
        dydx=second.dydx.model_copy(
            update={
                "asks": tuple(
                    level.model_copy(update={"price": Decimal("120")}) for level in second.dydx.asks
                )
            }
        ),
    )
    stress_failed = evaluate_bundle(replace(bundle, dense_books=(first, adverse_second)))
    assert "STRESS_QUOTE_OBSERVATIONS_INSUFFICIENT" in stress_failed.reason_codes


def test_blocking_compatibility_check_is_never_promoted() -> None:
    bundle = passing_bundle()
    dossier = bundle.dossier.model_copy(
        update={
            "status": DossierStatus.INELIGIBLE,
            "counts": bundle.dossier.counts.model_copy(update={"blocking": 1, "model_required": 9}),
        }
    )

    report = evaluate_bundle(replace(bundle, dossier=dossier))

    assert report.decision is EconomicsDecision.REJECTED
    assert "COMPATIBILITY_BLOCKING" in report.reason_codes


def test_higher_operator_cost_never_improves_conservative_net_or_decision() -> None:
    bundle = passing_bundle()
    baseline = evaluate_bundle(bundle)
    costly_policy = bundle.policy.model_copy(update={"operational_cost_usd": Decimal("6")})
    costly = evaluate_bundle(replace(bundle, policy=costly_policy))

    assert baseline.economics is not None and costly.economics is not None
    assert tuple(item.conservative_net_usd for item in costly.economics.horizons) <= tuple(
        item.conservative_net_usd for item in baseline.economics.horizons
    )
    assert costly.decision is not EconomicsDecision.SHADOW_CANDIDATE
