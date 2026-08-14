from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import ValidationError

from polytrading.carry.economics_models import (
    RESEARCH_WARNING,
    CandidateEconomicsReport,
    CompleteEconomics,
    EconomicsCostBreakdown,
    EconomicsDecision,
    EconomicsPolicy,
    EvidenceCoverage,
    FundingDirection,
    HorizonEconomics,
    VenueExecutionAssumption,
    VenueMarginAssumption,
    canonical_policy_json,
    policy_hash,
)
from polytrading.domain.models import Asset, FeeSchedule, Venue

STUDY_END = datetime(2026, 8, 13, 16, tzinfo=UTC)
KNOWN_AS_OF = datetime(2026, 8, 13, 17, tzinfo=UTC)


def execution_assumption(venue: Venue) -> VenueExecutionAssumption:
    if venue is Venue.DYDX:
        source_url = "https://help.dydx.trade/en/articles/166995-trading-fees-on-dydx"
    else:
        source_url = "https://docs.lighter.xyz/trading/trading-fees"
    return VenueExecutionAssumption(
        schema_version=1,
        venue=venue,
        fee_tier_name="reviewed-tier",
        account_type="standard",
        taker_latency_ms=Decimal("300"),
        observed_at=STUDY_END,
        source_url=source_url,
        source_hash=("a" if venue is Venue.DYDX else "b") * 64,
    )


def margin_assumption(venue: Venue, asset: Asset = Asset.BTC) -> VenueMarginAssumption:
    if venue is Venue.DYDX:
        source_url = "https://help.dydx.trade/en/articles/166991-liquidations-on-dydx-chain"
    else:
        source_url = "https://docs.lighter.xyz/trading/contract-specifications"
    return VenueMarginAssumption(
        schema_version=1,
        venue=venue,
        asset=asset,
        initial_margin_fraction=Decimal("1"),
        maintenance_margin_fraction=Decimal("0.05"),
        close_out_margin_fraction=Decimal("0.04"),
        liquidation_penalty_fraction=Decimal("0.01"),
        observed_at=STUDY_END,
        source_url=source_url,
        source_hash=("c" if venue is Venue.DYDX else "d") * 64,
    )


def policy(**overrides: object) -> EconomicsPolicy:
    values: dict[str, object] = {
        "schema_version": 1,
        "protocol_version": "lighter-dydx-shadow-economics-v1",
        "asset": Asset.BTC,
        "study_end": STUDY_END,
        "known_as_of": KNOWN_AS_OF,
        "account_equity_usd": Decimal("8000"),
        "cash_benchmark_annual_rate": Decimal("0.04"),
        "operational_cost_usd": Decimal("2"),
        "prefunded": False,
        "operational_source_url": "https://example.test/reviewed-operational-cost",
        "operational_source_hash": "e" * 64,
        "execution_assumptions": (
            execution_assumption(Venue.DYDX),
            execution_assumption(Venue.LIGHTER),
        ),
        "margin_assumptions": (
            margin_assumption(Venue.DYDX),
            margin_assumption(Venue.LIGHTER),
        ),
        "training_days": 30,
        "evaluation_days": 60,
        "minimum_coverage": Decimal("0.99"),
        "maximum_book_age_seconds": Decimal("30"),
        "maximum_cycle_skew_ms": Decimal("1000"),
        "maximum_hourly_book_age_seconds": Decimal("300"),
        "maximum_assigned_equity_fraction": Decimal("0.10"),
        "maximum_assigned_usd": Decimal("500"),
        "incomplete_leg_shock": Decimal("0.10"),
        "maximum_incomplete_loss_equity_fraction": Decimal("0.0025"),
        "minimum_hold_return": Decimal("0.003"),
        "minimum_profit_usd": Decimal("3"),
        "minimum_annualized_return": Decimal("0.12"),
        "cash_benchmark_spread": Decimal("0.05"),
        "maximum_stress_loss_equity_fraction": Decimal("0.0025"),
        "maximum_drawdown_fraction": Decimal("0.08"),
        "forced_exit_depth_multiplier": Decimal("2"),
        "doubled_cost_multiplier": Decimal("2"),
        "minimum_normal_quote_observations": 25,
        "minimum_stress_quote_observations": 10,
    }
    values.update(overrides)
    return EconomicsPolicy(**values)


def fee_schedule(venue: Venue) -> FeeSchedule:
    return FeeSchedule(
        schema_version=1,
        venue=venue,
        tier_name="reviewed-tier",
        maker_rate=Decimal("0"),
        taker_rate=Decimal("0.0005"),
        effective_from=STUDY_END - timedelta(days=1),
        observed_at=STUDY_END,
        source_url=(
            "https://help.dydx.trade/en/articles/166995-trading-fees-on-dydx"
            if venue is Venue.DYDX
            else "https://docs.lighter.xyz/trading/trading-fees"
        ),
        source_hash=("f" if venue is Venue.DYDX else "1") * 64,
    )


def coverage(**overrides: object) -> EvidenceCoverage:
    values: dict[str, object] = {
        "schema_version": 1,
        "requested_training_hours": 720,
        "paired_training_hours": 720,
        "training_funding_coverage": Decimal("1"),
        "requested_evaluation_hours": 1440,
        "paired_evaluation_hours": 1440,
        "evaluation_funding_coverage": Decimal("1"),
        "requested_funding_hours": 2160,
        "paired_funding_hours": 2160,
        "funding_coverage": Decimal("1"),
        "requested_book_hours": 1440,
        "paired_book_hours": 1440,
        "book_coverage": Decimal("1"),
        "latest_book_age_seconds": Decimal("5"),
        "latest_pair_skew_ms": Decimal("100"),
        "latency_sample_count": 20,
    }
    values.update(overrides)
    return EvidenceCoverage(**values)


def costs(**overrides: object) -> EconomicsCostBreakdown:
    values: dict[str, object] = {
        "schema_version": 1,
        "entry_slippage_usd": Decimal("0.20"),
        "forced_exit_cost_usd": Decimal("0.30"),
        "taker_fee_cost_usd": Decimal("0.10"),
        "operational_cost_usd": Decimal("0.20"),
        "latency_reserve_usd": Decimal("0.20"),
        "normal_cost_usd": Decimal("1.00"),
        "doubled_transaction_cost_usd": Decimal("1.80"),
    }
    values.update(overrides)
    return EconomicsCostBreakdown(**values)


def horizon(days: int, **overrides: object) -> HorizonEconomics:
    funding_rate = {7: Decimal("0.02"), 14: Decimal("0.03"), 28: Decimal("0.04")}[days]
    gross = Decimal("500") * funding_rate
    reversal = Decimal("0.10")
    basis = Decimal("0.10")
    net = gross - Decimal("1.00") - reversal - basis
    assigned_return = net / Decimal("500")
    values: dict[str, object] = {
        "schema_version": 1,
        "holding_days": days,
        "conservative_funding_rate": funding_rate,
        "gross_funding_usd": gross,
        "funding_reversal_reserve_usd": reversal,
        "basis_divergence_reserve_usd": basis,
        "conservative_net_usd": net,
        "assigned_capital_return": assigned_return,
        "account_return": net / Decimal("8000"),
        "annualized_conservative_return": assigned_return * Decimal(365) / Decimal(days),
        "net_positive": True,
        "minimum_profit_pass": True,
        "annualized_return_pass": True,
    }
    values.update(overrides)
    return HorizonEconomics(**values)


def complete_economics(**overrides: object) -> CompleteEconomics:
    horizon_rows = (horizon(7), horizon(14), horizon(28))
    twenty_eight = horizon_rows[-1]
    values: dict[str, object] = {
        "schema_version": 1,
        "execution_assumptions": (
            execution_assumption(Venue.DYDX),
            execution_assumption(Venue.LIGHTER),
        ),
        "margin_assumptions": (
            margin_assumption(Venue.DYDX),
            margin_assumption(Venue.LIGHTER),
        ),
        "fee_schedules": (fee_schedule(Venue.DYDX), fee_schedule(Venue.LIGHTER)),
        "base_quantity": Decimal("0.005"),
        "lighter_entry_notional_usd": Decimal("250"),
        "dydx_entry_notional_usd": Decimal("250"),
        "assigned_capital_usd": Decimal("500"),
        "account_equity_usd": Decimal("8000"),
        "unused_cash_usd": Decimal("7500"),
        "cash_benchmark_annual_rate": Decimal("0.04"),
        "minimum_profit_required_usd": Decimal("3"),
        "required_annualized_return": Decimal("0.12"),
        "prefunded": False,
        "operational_source_url": "https://example.test/reviewed-operational-cost",
        "operational_source_hash": "e" * 64,
        "costs": costs(),
        "horizons": horizon_rows,
        "normal_quote_observations": 25,
        "stress_quote_observations": 10,
        "incomplete_leg_loss_usd": Decimal("20"),
        "funding_and_forced_exit_loss_rate": (
            twenty_eight.funding_reversal_reserve_usd + Decimal("0.30") + Decimal("0.20")
        )
        / Decimal("8000"),
        "modeled_drawdown_rate": (
            twenty_eight.funding_reversal_reserve_usd
            + twenty_eight.basis_divergence_reserve_usd
            + Decimal("0.30")
            + Decimal("0.20")
        )
        / Decimal("500"),
        "modeled_liquidation": False,
        "doubled_cost_28d_net_usd": (
            twenty_eight.gross_funding_usd
            - Decimal("1.80")
            - twenty_eight.funding_reversal_reserve_usd
            - twenty_eight.basis_divergence_reserve_usd
        ),
        "doubled_cost_28d_pass": True,
        "stress_loss_pass": True,
        "drawdown_pass": True,
        "liquidation_pass": True,
        "quote_observations_pass": True,
    }
    values.update(overrides)
    return CompleteEconomics(**values)


def report(**overrides: object) -> CandidateEconomicsReport:
    values: dict[str, object] = {
        "schema_version": 1,
        "protocol_version": "lighter-dydx-shadow-economics-v1",
        "evaluation_id": UUID("00000000-0000-0000-0000-000000000701"),
        "asset": Asset.BTC,
        "known_as_of": KNOWN_AS_OF,
        "evaluated_at": KNOWN_AS_OF + timedelta(seconds=1),
        "training_start": STUDY_END - timedelta(days=90),
        "training_end": STUDY_END - timedelta(days=60),
        "evaluation_end": STUDY_END,
        "policy_hash": "2" * 64,
        "source_hashes": tuple(value * 64 for value in sorted(("1", "a", "b", "c", "d", "e", "f"))),
        "decision": EconomicsDecision.SHADOW_CANDIDATE,
        "reason_codes": (),
        "direction": FundingDirection.SHORT_LIGHTER_LONG_DYDX,
        "short_venue": Venue.LIGHTER,
        "long_venue": Venue.DYDX,
        "coverage": coverage(),
        "economics": complete_economics(),
        "warning": RESEARCH_WARNING,
    }
    values.update(overrides)
    return CandidateEconomicsReport(**values)


def test_policy_freezes_protocol_thresholds_and_assumption_order() -> None:
    item = policy()

    assert tuple(EconomicsDecision) == (
        EconomicsDecision.INSUFFICIENT_EVIDENCE,
        EconomicsDecision.REJECTED,
        EconomicsDecision.SHADOW_CANDIDATE,
    )
    assert tuple(FundingDirection) == (
        FundingDirection.SHORT_LIGHTER_LONG_DYDX,
        FundingDirection.SHORT_DYDX_LONG_LIGHTER,
    )
    assert item.protocol_version == "lighter-dydx-shadow-economics-v1"
    assert item.account_equity_usd == Decimal("8000")
    assert tuple(row.venue for row in item.execution_assumptions) == (
        Venue.DYDX,
        Venue.LIGHTER,
    )
    assert tuple(row.venue for row in item.margin_assumptions) == (
        Venue.DYDX,
        Venue.LIGHTER,
    )
    assert item.training_days == 30
    assert item.evaluation_days == 60
    assert item.minimum_coverage == Decimal("0.99")
    assert item.maximum_assigned_usd == Decimal("500")


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"account_equity_usd": Decimal("2999.99")}, "greater than or equal"),
        ({"account_equity_usd": Decimal("10000.01")}, "less than or equal"),
        ({"account_equity_usd": 8000.0}, "instance of Decimal"),
        ({"study_end": STUDY_END.replace(tzinfo=None)}, "timezone-aware"),
        ({"study_end": STUDY_END + timedelta(minutes=1)}, "whole UTC hour"),
        ({"study_end": STUDY_END + timedelta(hours=2)}, "knowledge cutoff"),
        ({"known_as_of": STUDY_END + timedelta(minutes=66)}, "65 minutes"),
        ({"operational_cost_usd": Decimal("-0.01")}, "greater than or equal"),
        ({"operational_cost_usd": Decimal("0")}, "prefunded"),
        ({"minimum_coverage": Decimal("0.98")}, "frozen by protocol"),
        ({"maximum_assigned_usd": Decimal("501")}, "frozen by protocol"),
        ({"minimum_normal_quote_observations": 24}, "frozen by protocol"),
    ],
)
def test_policy_rejects_weakened_or_ambiguous_inputs(
    overrides: dict[str, object], match: str
) -> None:
    with pytest.raises(ValidationError, match=match):
        policy(**overrides)


@pytest.mark.parametrize(
    ("field_name", "weakened_value"),
    [
        ("training_days", 29),
        ("evaluation_days", 59),
        ("minimum_coverage", Decimal("0.98")),
        ("maximum_book_age_seconds", Decimal("31")),
        ("maximum_cycle_skew_ms", Decimal("1001")),
        ("maximum_hourly_book_age_seconds", Decimal("301")),
        ("maximum_assigned_equity_fraction", Decimal("0.11")),
        ("maximum_assigned_usd", Decimal("501")),
        ("incomplete_leg_shock", Decimal("0.09")),
        ("maximum_incomplete_loss_equity_fraction", Decimal("0.003")),
        ("minimum_hold_return", Decimal("0.002")),
        ("minimum_profit_usd", Decimal("2")),
        ("minimum_annualized_return", Decimal("0.11")),
        ("cash_benchmark_spread", Decimal("0.04")),
        ("maximum_stress_loss_equity_fraction", Decimal("0.003")),
        ("maximum_drawdown_fraction", Decimal("0.09")),
        ("forced_exit_depth_multiplier", Decimal("1.5")),
        ("doubled_cost_multiplier", Decimal("1.5")),
        ("minimum_normal_quote_observations", 24),
        ("minimum_stress_quote_observations", 9),
    ],
)
def test_every_protocol_threshold_is_immutable(field_name: str, weakened_value: object) -> None:
    with pytest.raises(ValidationError, match="frozen by protocol"):
        policy(**{field_name: weakened_value})


def test_policy_rejects_noncanonical_or_future_assumptions() -> None:
    reversed_execution = (
        execution_assumption(Venue.LIGHTER),
        execution_assumption(Venue.DYDX),
    )
    with pytest.raises(ValidationError, match="canonical dYdX/Lighter order"):
        policy(execution_assumptions=reversed_execution)

    wrong_asset = (
        margin_assumption(Venue.DYDX, Asset.ETH),
        margin_assumption(Venue.LIGHTER, Asset.ETH),
    )
    with pytest.raises(ValidationError, match="asset must match"):
        policy(margin_assumptions=wrong_asset)

    future = execution_assumption(Venue.LIGHTER).model_copy(
        update={"observed_at": KNOWN_AS_OF + timedelta(microseconds=1)}
    )
    with pytest.raises(ValidationError, match="observed by knowledge cutoff"):
        policy(
            execution_assumptions=(
                execution_assumption(Venue.DYDX),
                future,
            )
        )


def test_assumptions_require_ordered_margin_and_exact_official_hosts() -> None:
    invalid_margin = margin_assumption(Venue.LIGHTER).model_dump()
    invalid_margin["close_out_margin_fraction"] = Decimal("0.06")
    with pytest.raises(ValidationError, match="close-out <= maintenance <= initial"):
        VenueMarginAssumption(**invalid_margin)

    with pytest.raises(ValidationError, match="official source"):
        VenueExecutionAssumption(
            **{
                **execution_assumption(Venue.DYDX).model_dump(),
                "source_url": "https://help.dydx.trade.evil.test/fees",
            }
        )
    with pytest.raises(ValidationError, match="String should match pattern"):
        VenueExecutionAssumption(
            **{
                **execution_assumption(Venue.DYDX).model_dump(),
                "source_hash": "A" * 64,
            }
        )


def test_policy_hash_is_canonical_and_sensitive_to_operator_input() -> None:
    first = policy()
    second = policy(cash_benchmark_annual_rate=Decimal("0.041"))

    assert canonical_policy_json(first).startswith('{"account_equity_usd":"8000"')
    assert '"known_as_of":"2026-08-13T17:00:00Z"' in canonical_policy_json(first)
    assert policy_hash(first) == policy_hash(first)
    assert policy_hash(first) != policy_hash(second)
    assert len(policy_hash(first)) == 64


def test_coverage_requires_exact_count_ratios_and_component_totals() -> None:
    assert coverage().funding_coverage == Decimal("1")

    with pytest.raises(ValidationError, match="funding requested hours"):
        coverage(requested_funding_hours=2159, paired_funding_hours=2159)
    with pytest.raises(ValidationError, match="book coverage"):
        coverage(book_coverage=Decimal("0.99"))
    with pytest.raises(ValidationError, match="paired hours cannot exceed requested"):
        coverage(paired_training_hours=721)


def test_complete_economics_enforces_capital_cost_and_horizon_identities() -> None:
    item = complete_economics()

    assert item.assigned_capital_usd == (
        item.lighter_entry_notional_usd + item.dydx_entry_notional_usd
    )
    assert item.unused_cash_usd == item.account_equity_usd - item.assigned_capital_usd
    assert tuple(row.holding_days for row in item.horizons) == (7, 14, 28)

    with pytest.raises(ValidationError, match="assigned capital must equal"):
        complete_economics(assigned_capital_usd=Decimal("499"))
    with pytest.raises(ValidationError, match="normal cost identity"):
        complete_economics(costs=costs(normal_cost_usd=Decimal("1.01")))
    with pytest.raises(ValidationError, match="gross funding identity"):
        rows = list(complete_economics().horizons)
        rows[0] = horizon(7, gross_funding_usd=Decimal("99"))
        complete_economics(horizons=tuple(rows))
    with pytest.raises(ValidationError, match="holding horizons"):
        rows = complete_economics().horizons
        complete_economics(horizons=(rows[1], rows[0], rows[2]))


def test_complete_report_requires_every_nested_source_in_sorted_lineage() -> None:
    expected_hashes = tuple(f"{value}" * 64 for value in ("1", "a", "b", "c", "d", "e", "f"))
    item = report(source_hashes=expected_hashes)

    assert item.source_hashes == expected_hashes
    with pytest.raises(ValidationError, match="nested evidence lineage"):
        report(source_hashes=expected_hashes[:-1])


def test_direction_bearing_report_enforces_frozen_freshness_limits() -> None:
    with pytest.raises(ValidationError, match="latest book age"):
        report(coverage=coverage(latest_book_age_seconds=Decimal("30.000001")))
    with pytest.raises(ValidationError, match="pair skew"):
        report(coverage=coverage(latest_pair_skew_ms=Decimal("1000.000001")))
    with pytest.raises(ValidationError, match="whole UTC hour"):
        report(evaluation_end=STUDY_END + timedelta(microseconds=1))


def test_candidate_report_supports_shadow_rejected_and_insufficient_contracts() -> None:
    assert report().decision is EconomicsDecision.SHADOW_CANDIDATE

    insufficient = report(
        decision=EconomicsDecision.INSUFFICIENT_EVIDENCE,
        reason_codes=("FUNDING_COVERAGE_INSUFFICIENT",),
        direction=None,
        short_venue=None,
        long_venue=None,
        economics=None,
    )
    assert insufficient.economics is None

    zero_median = report(
        decision=EconomicsDecision.REJECTED,
        reason_codes=("TRAINING_FUNDING_MEDIAN_ZERO",),
        direction=None,
        short_venue=None,
        long_venue=None,
        economics=None,
    )
    assert zero_median.direction is None

    economic_rejection = report(
        decision=EconomicsDecision.REJECTED,
        reason_codes=("COMPATIBILITY_MARGIN_MODEL_BLOCKING",),
    )
    assert economic_rejection.economics is not None

    sizing_rejection = report(
        decision=EconomicsDecision.REJECTED,
        reason_codes=("DEPTH_COMPATIBLE_SIZE_UNAVAILABLE",),
        economics=None,
    )
    assert sizing_rejection.direction is not None
    assert sizing_rejection.economics is None

    with pytest.raises(ValidationError, match="complete evidence coverage"):
        report(
            decision=EconomicsDecision.REJECTED,
            reason_codes=("DEPTH_COMPATIBLE_SIZE_UNAVAILABLE",),
            economics=None,
            coverage=coverage(
                paired_book_hours=1400,
                book_coverage=Decimal(1400) / Decimal(1440),
            ),
        )

    with pytest.raises(ValidationError, match="sizing reasons"):
        report(
            decision=EconomicsDecision.REJECTED,
            reason_codes=("CURRENT_FUNDING_REGIME_REVERSED",),
            economics=None,
        )


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"source_hashes": ("3" * 64, "3" * 64)}, "source hashes"),
        ({"reason_codes": ("lowercase",)}, "reason code"),
        ({"evaluated_at": KNOWN_AS_OF - timedelta(seconds=1)}, "evaluation time"),
        ({"training_end": STUDY_END - timedelta(days=59)}, "30 days"),
        ({"evaluation_end": STUDY_END - timedelta(hours=1)}, "60 days"),
        (
            {
                "decision": EconomicsDecision.SHADOW_CANDIDATE,
                "reason_codes": ("NET_NONPOSITIVE_7D",),
            },
            "shadow candidate",
        ),
        (
            {
                "decision": EconomicsDecision.REJECTED,
                "reason_codes": ("TRAINING_FUNDING_MEDIAN_ZERO",),
                "direction": None,
                "short_venue": None,
                "long_venue": None,
            },
            "directionless rejection",
        ),
    ],
)
def test_candidate_report_rejects_incoherent_identity_or_decision(
    overrides: dict[str, object], match: str
) -> None:
    with pytest.raises(ValidationError, match=match):
        report(**overrides)


def test_candidate_report_requires_exact_direction_venue_mapping_and_warning() -> None:
    with pytest.raises(ValidationError, match="direction venue mapping"):
        report(short_venue=Venue.DYDX, long_venue=Venue.LIGHTER)
    with pytest.raises(ValidationError, match="literal_error"):
        report(warning="Trade now")
