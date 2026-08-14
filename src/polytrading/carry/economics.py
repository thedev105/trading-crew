from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID

from polytrading.carry.dossier_models import DossierStatus
from polytrading.carry.economics_execution import (
    ExecutableQuoteObservation,
    InsufficientDepthError,
    ShadowPosition,
    basis_reserve,
    entry_slippage_cost,
    forced_exit_cost,
    latency_reserve,
    margin_stress,
    quote_observation_counts,
    quoted_net_usd,
    size_shadow_position,
    walk_book,
)
from polytrading.carry.economics_funding import (
    FundingCashflowHorizonStatistics,
    FundingCashflowObservation,
    exact_median,
    funding_cashflow_horizon_statistics,
    orient_funding,
    select_direction,
)
from polytrading.carry.economics_models import (
    RESEARCH_WARNING,
    CandidateEconomicsReport,
    CompleteEconomics,
    EconomicsCostBreakdown,
    EconomicsDecision,
    FundingDirection,
    HorizonEconomics,
    policy_hash,
)
from polytrading.domain.models import (
    InstrumentSpec,
    Level2BookSnapshot,
    Venue,
    normalize_utc_timestamp,
)

if TYPE_CHECKING:
    from polytrading.carry.economics_assembler import (
        EconomicsAssemblyResult,
        EconomicsEvidenceBundle,
        PairedFundingObservation,
    )

_HORIZONS = (7, 14, 28)


class CandidateEconomicsEvaluator:
    """Evaluate an assembled, point-in-time evidence bundle deterministically."""

    def evaluate(
        self,
        result: EconomicsAssemblyResult,
        *,
        evaluated_at: datetime,
        evaluation_id: UUID,
    ) -> CandidateEconomicsReport:
        if not isinstance(evaluation_id, UUID):
            raise TypeError("evaluation ID must be a UUID")
        normalized_evaluated_at = normalize_utc_timestamp(evaluated_at)
        policy = result.policy
        training_start = policy.study_end - timedelta(
            days=policy.training_days + policy.evaluation_days
        )
        training_end = policy.study_end - timedelta(days=policy.evaluation_days)
        common = {
            "schema_version": 1,
            "protocol_version": policy.protocol_version,
            "evaluation_id": evaluation_id,
            "asset": policy.asset,
            "known_as_of": policy.known_as_of,
            "evaluated_at": normalized_evaluated_at,
            "training_start": training_start,
            "training_end": training_end,
            "evaluation_end": policy.study_end,
            "policy_hash": policy_hash(policy),
            "source_hashes": tuple(sorted(set(result.source_hashes))),
            "coverage": result.coverage,
            "warning": RESEARCH_WARNING,
        }
        if result.bundle is None:
            return CandidateEconomicsReport(
                **common,
                decision=EconomicsDecision.INSUFFICIENT_EVIDENCE,
                reason_codes=tuple(sorted(set(result.reason_codes))),
                direction=None,
                short_venue=None,
                long_venue=None,
                economics=None,
            )

        bundle = result.bundle
        training_differentials = tuple(
            pair.lighter.rate - pair.dydx.rate
            for pair in bundle.funding_pairs
            if pair.effective_at <= bundle.training_end
        )
        direction = select_direction(training_differentials)
        if direction is None:
            return CandidateEconomicsReport(
                **common,
                decision=EconomicsDecision.REJECTED,
                reason_codes=("TRAINING_FUNDING_MEDIAN_ZERO",),
                direction=None,
                short_venue=None,
                long_venue=None,
                economics=None,
            )
        short_venue, long_venue = _direction_venues(direction)
        position = size_shadow_position(
            policy=policy,
            direction=direction,
            lighter_book=bundle.latest_books.lighter,
            dydx_book=bundle.latest_books.dydx,
            lighter_instrument=_instrument(bundle, Venue.LIGHTER),
            dydx_instrument=_instrument(bundle, Venue.DYDX),
        )
        if position is None:
            return CandidateEconomicsReport(
                **common,
                decision=EconomicsDecision.REJECTED,
                reason_codes=("DEPTH_COMPATIBLE_SIZE_UNAVAILABLE",),
                direction=direction,
                short_venue=short_venue,
                long_venue=long_venue,
                economics=None,
            )

        try:
            economics, reasons = _complete_economics(bundle, direction, position)
        except _SizingRejection as error:
            return CandidateEconomicsReport(
                **common,
                decision=EconomicsDecision.REJECTED,
                reason_codes=(error.reason_code,),
                direction=direction,
                short_venue=short_venue,
                long_venue=long_venue,
                economics=None,
            )
        except _IncompleteCalculation as error:
            return CandidateEconomicsReport(
                **common,
                decision=EconomicsDecision.INSUFFICIENT_EVIDENCE,
                reason_codes=(error.reason_code,),
                direction=None,
                short_venue=None,
                long_venue=None,
                economics=None,
            )
        decision = EconomicsDecision.SHADOW_CANDIDATE if not reasons else EconomicsDecision.REJECTED
        return CandidateEconomicsReport(
            **common,
            decision=decision,
            reason_codes=tuple(sorted(reasons)),
            direction=direction,
            short_venue=short_venue,
            long_venue=long_venue,
            economics=economics,
        )


class _IncompleteCalculation(Exception):
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


class _SizingRejection(_IncompleteCalculation):
    pass


def _direction_venues(direction: FundingDirection) -> tuple[Venue, Venue]:
    if direction is FundingDirection.SHORT_LIGHTER_LONG_DYDX:
        return Venue.LIGHTER, Venue.DYDX
    return Venue.DYDX, Venue.LIGHTER


def _funding_cashflows(
    pairs: tuple[PairedFundingObservation, ...],
    direction: FundingDirection,
    position: ShadowPosition,
) -> tuple[FundingCashflowObservation, ...]:
    lighter_sign = (
        Decimal(1) if direction is FundingDirection.SHORT_LIGHTER_LONG_DYDX else Decimal(-1)
    )
    dydx_sign = -lighter_sign
    return tuple(
        FundingCashflowObservation(
            effective_at=pair.effective_at,
            lighter_rate=pair.lighter.rate * lighter_sign,
            dydx_rate=pair.dydx.rate * dydx_sign,
            lighter_funding_usd=(
                position.lighter_entry_notional_usd * pair.lighter.rate * lighter_sign
            ),
            dydx_funding_usd=(position.dydx_entry_notional_usd * pair.dydx.rate * dydx_sign),
        )
        for pair in pairs
    )


def _instrument(bundle: EconomicsEvidenceBundle, venue: Venue) -> InstrumentSpec:
    return next(item for item in bundle.instruments if item.venue is venue)


def _walk_base_notional(
    levels: tuple,
    base_quantity: Decimal,
    instrument: InstrumentSpec,
) -> Decimal:
    return (
        walk_book(levels[:20], base_quantity / instrument.contract_multiplier).notional
        * instrument.contract_multiplier
    )


def _exit_notionals(
    bundle: EconomicsEvidenceBundle,
    position: ShadowPosition,
    lighter_book: Level2BookSnapshot,
    dydx_book: Level2BookSnapshot,
) -> tuple[Decimal, Decimal]:
    if position.direction is FundingDirection.SHORT_LIGHTER_LONG_DYDX:
        lighter_levels, dydx_levels = lighter_book.asks, dydx_book.bids
    else:
        lighter_levels, dydx_levels = lighter_book.bids, dydx_book.asks
    return (
        _walk_base_notional(
            lighter_levels,
            position.base_quantity,
            _instrument(bundle, Venue.LIGHTER),
        ),
        _walk_base_notional(
            dydx_levels,
            position.base_quantity,
            _instrument(bundle, Venue.DYDX),
        ),
    )


def _taker_fee_cost(
    bundle: EconomicsEvidenceBundle,
    position: ShadowPosition,
    lighter_exit_notional: Decimal,
    dydx_exit_notional: Decimal,
) -> Decimal:
    fee_by_venue = {item.venue: item.taker_rate for item in bundle.fees}
    return (position.lighter_entry_notional_usd + lighter_exit_notional) * fee_by_venue[
        Venue.LIGHTER
    ] + (position.dydx_entry_notional_usd + dydx_exit_notional) * fee_by_venue[Venue.DYDX]


def _entry_quote_cost(
    bundle: EconomicsEvidenceBundle,
    direction: FundingDirection,
    base_quantity: Decimal,
    lighter_book: Level2BookSnapshot,
    dydx_book: Level2BookSnapshot,
) -> Decimal:
    lighter_instrument = _instrument(bundle, Venue.LIGHTER)
    dydx_instrument = _instrument(bundle, Venue.DYDX)
    if direction is FundingDirection.SHORT_LIGHTER_LONG_DYDX:
        short_proceeds = _walk_base_notional(lighter_book.bids, base_quantity, lighter_instrument)
        long_payment = _walk_base_notional(dydx_book.asks, base_quantity, dydx_instrument)
    else:
        short_proceeds = _walk_base_notional(dydx_book.bids, base_quantity, dydx_instrument)
        long_payment = _walk_base_notional(lighter_book.asks, base_quantity, lighter_instrument)
    return long_payment - short_proceeds


def _latency_cost(
    bundle: EconomicsEvidenceBundle,
    direction: FundingDirection,
    position: ShadowPosition,
) -> Decimal:
    observations: list[ExecutableQuoteObservation] = []
    for row in bundle.dense_books:
        try:
            cost = _entry_quote_cost(
                bundle,
                direction,
                position.base_quantity,
                row.lighter,
                row.dydx,
            )
        except InsufficientDepthError:
            continue
        observations.append(
            ExecutableQuoteObservation(observed_at=row.effective_at, quote_cost_usd=cost)
        )
    documented_floor = max(item.taker_latency_ms for item in bundle.policy.execution_assumptions)
    reserve = latency_reserve(tuple(observations), documented_floor)
    if reserve is None:
        raise _IncompleteCalculation("LATENCY_EXECUTABLE_SAMPLE_INSUFFICIENT")
    return reserve


def _quote_counts(
    bundle: EconomicsEvidenceBundle,
    direction: FundingDirection,
    seven_day: FundingCashflowHorizonStatistics,
    seven_day_funding_reversal_rate: Decimal,
    seven_day_basis_rate: Decimal,
    latency_cost_usd: Decimal,
) -> tuple[int, int]:
    normal: list[Decimal] = []
    stress: list[Decimal] = []
    lighter_instrument = _instrument(bundle, Venue.LIGHTER)
    dydx_instrument = _instrument(bundle, Venue.DYDX)
    for row in bundle.hourly_books:
        position = size_shadow_position(
            policy=bundle.policy,
            direction=direction,
            lighter_book=row.lighter,
            dydx_book=row.dydx,
            lighter_instrument=lighter_instrument,
            dydx_instrument=dydx_instrument,
        )
        if position is None:
            continue
        try:
            entry = entry_slippage_cost(position, row.lighter, row.dydx)
            forced = forced_exit_cost(
                position,
                row.lighter,
                row.dydx,
                bundle.policy.forced_exit_depth_multiplier,
            )
            lighter_exit, dydx_exit = _exit_notionals(bundle, position, row.lighter, row.dydx)
        except InsufficientDepthError:
            continue
        fee = _taker_fee_cost(bundle, position, lighter_exit, dydx_exit)
        gross = (
            position.lighter_entry_notional_usd * seven_day.lighter_rate_sum
            + position.dydx_entry_notional_usd * seven_day.dydx_rate_sum
        )
        reference_notional = position.assigned_capital_usd / Decimal(2)
        reversal = reference_notional * seven_day_funding_reversal_rate
        basis = reference_notional * seven_day_basis_rate
        shared = {
            "gross_funding_usd": gross,
            "entry_cost_usd": entry,
            "exit_cost_usd": forced,
            "fee_cost_usd": fee,
            "operational_cost_usd": bundle.policy.operational_cost_usd,
            "funding_reversal_reserve_usd": reversal,
            "basis_reserve_usd": basis,
        }
        normal.append(quoted_net_usd(**shared, latency_reserve_usd=Decimal(0)))
        stress.append(quoted_net_usd(**shared, latency_reserve_usd=latency_cost_usd))
    counts = quote_observation_counts(
        normal_net_values=tuple(normal), stress_net_values=tuple(stress)
    )
    return counts.normal_positive, counts.stress_positive


def _complete_economics(
    bundle: EconomicsEvidenceBundle,
    direction: FundingDirection,
    position: ShadowPosition,
) -> tuple[CompleteEconomics, set[str]]:
    policy = bundle.policy
    evaluation_pairs = tuple(
        pair
        for pair in bundle.funding_pairs
        if bundle.training_end < pair.effective_at <= bundle.evaluation_end
    )
    evaluation_rows = tuple(
        (pair.effective_at, pair.lighter.rate - pair.dydx.rate) for pair in evaluation_pairs
    )
    oriented_rows = tuple(
        zip(
            (timestamp for timestamp, _ in evaluation_rows),
            orient_funding(tuple(value for _, value in evaluation_rows), direction),
            strict=True,
        )
    )
    if len(oriented_rows) < 28 * 24:
        raise _IncompleteCalculation("FUNDING_CONTIGUOUS_WINDOW_INSUFFICIENT")
    current_start = bundle.evaluation_end - timedelta(hours=7 * 24 - 1)
    expected_current_timestamps = tuple(
        current_start + timedelta(hours=offset) for offset in range(7 * 24)
    )
    current_rows = tuple(
        row for row in oriented_rows if current_start <= row[0] <= bundle.evaluation_end
    )
    if tuple(timestamp for timestamp, _ in current_rows) != expected_current_timestamps:
        raise _IncompleteCalculation("CURRENT_FUNDING_WINDOW_INSUFFICIENT")
    cashflow_rows = _funding_cashflows(evaluation_pairs, direction, position)
    try:
        statistics = tuple(
            funding_cashflow_horizon_statistics(
                cashflow_rows,
                holding_days,  # type: ignore[arg-type]
            )
            for holding_days in _HORIZONS
        )
        basis_rates = tuple(
            basis_reserve(bundle.hourly_books, direction, holding_days)  # type: ignore[arg-type]
            for holding_days in _HORIZONS
        )
    except ValueError as error:
        raise _IncompleteCalculation("EVALUATION_WINDOW_INSUFFICIENT") from error

    current_values = tuple(value for _, value in current_rows)
    reasons: set[str] = set()
    if exact_median(current_values) <= 0:
        reasons.add("CURRENT_FUNDING_REGIME_REVERSED")
    if bundle.dossier.status is DossierStatus.INELIGIBLE or bundle.dossier.counts.blocking:
        reasons.add("COMPATIBILITY_BLOCKING")
    if (
        bundle.dossier.status is DossierStatus.EVIDENCE_INCOMPLETE
        or bundle.dossier.counts.missing_evidence
    ):
        reasons.add("COMPATIBILITY_EVIDENCE_INCOMPLETE")

    latest = bundle.latest_books
    try:
        entry = entry_slippage_cost(position, latest.lighter, latest.dydx)
        forced = forced_exit_cost(
            position,
            latest.lighter,
            latest.dydx,
            policy.forced_exit_depth_multiplier,
        )
        lighter_exit, dydx_exit = _exit_notionals(bundle, position, latest.lighter, latest.dydx)
    except InsufficientDepthError as error:
        raise _SizingRejection("DEPTH_FORCED_EXIT_UNAVAILABLE") from error
    fee = _taker_fee_cost(bundle, position, lighter_exit, dydx_exit)
    latency = _latency_cost(bundle, direction, position)
    transaction = entry + forced + fee + policy.operational_cost_usd
    costs = EconomicsCostBreakdown(
        schema_version=1,
        entry_slippage_usd=entry,
        forced_exit_cost_usd=forced,
        taker_fee_cost_usd=fee,
        operational_cost_usd=policy.operational_cost_usd,
        latency_reserve_usd=latency,
        normal_cost_usd=transaction + latency,
        doubled_transaction_cost_usd=transaction * policy.doubled_cost_multiplier + latency,
    )
    minimum_profit = max(
        policy.minimum_profit_usd,
        position.assigned_capital_usd * policy.minimum_hold_return,
    )
    required_annualized = max(
        policy.minimum_annualized_return,
        policy.cash_benchmark_annual_rate + policy.cash_benchmark_spread,
    )
    reference_notional = position.assigned_capital_usd / Decimal(2)
    horizons: list[HorizonEconomics] = []
    for stats, basis_rate in zip(statistics, basis_rates, strict=True):
        gross = stats.gross_funding_usd
        reversal = stats.maximum_drawdown_usd
        basis = reference_notional * basis_rate
        net = gross - costs.normal_cost_usd - reversal - basis
        assigned_return = net / position.assigned_capital_usd
        annualized = assigned_return * Decimal(365) / Decimal(stats.holding_days)
        horizon = HorizonEconomics(
            schema_version=1,
            holding_days=stats.holding_days,
            conservative_funding_rate=gross / position.assigned_capital_usd,
            lighter_funding_rate_sum=stats.lighter_rate_sum,
            dydx_funding_rate_sum=stats.dydx_rate_sum,
            lighter_funding_usd=stats.lighter_funding_usd,
            dydx_funding_usd=stats.dydx_funding_usd,
            gross_funding_usd=gross,
            funding_reversal_reserve_usd=reversal,
            basis_divergence_rate=basis_rate,
            basis_divergence_reserve_usd=basis,
            conservative_net_usd=net,
            assigned_capital_return=assigned_return,
            account_return=net / policy.account_equity_usd,
            annualized_conservative_return=annualized,
            net_positive=net > 0,
            minimum_profit_pass=net >= minimum_profit,
            annualized_return_pass=annualized >= required_annualized,
        )
        label = f"HORIZON_{stats.holding_days}D"
        if not horizon.net_positive:
            reasons.add(f"{label}_NONPOSITIVE")
        if not horizon.minimum_profit_pass:
            reasons.add(f"{label}_MINIMUM_PROFIT")
        if not horizon.annualized_return_pass:
            reasons.add(f"{label}_ANNUALIZED_RETURN")
        horizons.append(horizon)

    twenty_eight = horizons[-1]
    doubled_net = (
        twenty_eight.gross_funding_usd
        - costs.doubled_transaction_cost_usd
        - twenty_eight.funding_reversal_reserve_usd
        - twenty_eight.basis_divergence_reserve_usd
    )
    stress_rate = (
        twenty_eight.funding_reversal_reserve_usd
        + costs.forced_exit_cost_usd
        + costs.latency_reserve_usd
    ) / policy.account_equity_usd
    drawdown_rate = (
        twenty_eight.funding_reversal_reserve_usd
        + twenty_eight.basis_divergence_reserve_usd
        + costs.forced_exit_cost_usd
        + costs.latency_reserve_usd
    ) / position.assigned_capital_usd
    fee_by_venue = {item.venue: item.taker_rate for item in bundle.fees}
    margin_by_venue = {item.venue: item for item in policy.margin_assumptions}
    if direction is FundingDirection.SHORT_LIGHTER_LONG_DYDX:
        long_venue = Venue.DYDX
    else:
        long_venue = Venue.LIGHTER
    margin_results = tuple(
        margin_stress(
            is_long=venue is long_venue,
            base_quantity=position.base_quantity,
            entry_price=(
                position.lighter_entry.weighted_average_price
                if venue is Venue.LIGHTER
                else position.dydx_entry.weighted_average_price
            ),
            collateral_usd=(
                position.lighter_entry_notional_usd
                if venue is Venue.LIGHTER
                else position.dydx_entry_notional_usd
            ),
            taker_fee_rate=fee_by_venue[venue],
            shock=policy.incomplete_leg_shock,
            assumption=margin_by_venue[venue],
        )
        for venue in (Venue.DYDX, Venue.LIGHTER)
    )
    modeled_liquidation = any(item.modeled_liquidation for item in margin_results)
    normal_count, stress_count = _quote_counts(
        bundle,
        direction,
        statistics[0],
        statistics[0].maximum_drawdown_usd / reference_notional,
        basis_rates[0],
        latency,
    )
    doubled_pass = doubled_net > 0
    stress_pass = stress_rate <= policy.maximum_stress_loss_equity_fraction
    drawdown_pass = drawdown_rate < policy.maximum_drawdown_fraction
    liquidation_pass = not modeled_liquidation
    quote_pass = (
        normal_count >= policy.minimum_normal_quote_observations
        and stress_count >= policy.minimum_stress_quote_observations
    )
    if not doubled_pass:
        reasons.add("DOUBLED_COST_28D_NONPOSITIVE")
    if not stress_pass:
        reasons.add("STRESS_LOSS_LIMIT_EXCEEDED")
    if not drawdown_pass:
        reasons.add("MODELED_DRAWDOWN_LIMIT_EXCEEDED")
    if not liquidation_pass:
        reasons.add("MODELED_LIQUIDATION")
    if normal_count < policy.minimum_normal_quote_observations:
        reasons.add("NORMAL_QUOTE_OBSERVATIONS_INSUFFICIENT")
    if stress_count < policy.minimum_stress_quote_observations:
        reasons.add("STRESS_QUOTE_OBSERVATIONS_INSUFFICIENT")

    economics = CompleteEconomics(
        schema_version=1,
        execution_assumptions=policy.execution_assumptions,
        margin_assumptions=policy.margin_assumptions,
        fee_schedules=bundle.fees,
        base_quantity=position.base_quantity,
        lighter_entry_notional_usd=position.lighter_entry_notional_usd,
        dydx_entry_notional_usd=position.dydx_entry_notional_usd,
        assigned_capital_usd=position.assigned_capital_usd,
        account_equity_usd=policy.account_equity_usd,
        unused_cash_usd=position.unused_cash_usd,
        cash_benchmark_annual_rate=policy.cash_benchmark_annual_rate,
        minimum_profit_required_usd=minimum_profit,
        required_annualized_return=required_annualized,
        prefunded=policy.prefunded,
        operational_source_url=policy.operational_source_url,
        operational_source_hash=policy.operational_source_hash,
        costs=costs,
        horizons=tuple(horizons),  # type: ignore[arg-type]
        normal_quote_observations=normal_count,
        stress_quote_observations=stress_count,
        incomplete_leg_loss_usd=position.incomplete_leg_loss_usd,
        funding_and_forced_exit_loss_rate=stress_rate,
        modeled_drawdown_rate=drawdown_rate,
        modeled_liquidation=modeled_liquidation,
        doubled_cost_28d_net_usd=doubled_net,
        doubled_cost_28d_pass=doubled_pass,
        stress_loss_pass=stress_pass,
        drawdown_pass=drawdown_pass,
        liquidation_pass=liquidation_pass,
        quote_observations_pass=quote_pass,
    )
    return economics, reasons
