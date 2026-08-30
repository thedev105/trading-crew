from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

from polytrading.predictions.domain import PredictionVenue
from polytrading.predictions.economics_models import EconomicsResult, ScanReport
from polytrading.predictions.experiments import ShadowExperiment
from polytrading.predictions.pilot.models import PilotProofFamily
from polytrading.predictions.pilot.qualification import (
    APPROVED_CASH_BENCHMARK_RATE,
    evaluate_pilot_qualification,
)
from polytrading.predictions.shadow_ledger import LedgerPosting, ShadowReconciliation
from polytrading.predictions.shadow_models import ShadowLegPlan, ShadowPlan, ShadowState
from polytrading.predictions.storage.store import PredictionMarketStore
from tests.predictions.proof_helpers import proof_artifact
from tests.predictions.scan_helpers import scan_report

AS_OF = datetime(2026, 8, 27, 12, tzinfo=UTC)
FAMILY = PilotProofFamily.BINARY_COMPLEMENT
PROOF_ID = UUID("00000000-0000-0000-0000-000000006001")
CANDIDATE_ID = UUID("00000000-0000-0000-0000-000000003001")
PLAN_HASH = "d" * 64
SHADOW_DAYS = 30
_SCENARIO_SLOTS = {"baseline": 1, "latency_1s": 2, "latency_5s": 3}
EVIDENCE_DAYS = 45


def _uuid(prefix: int, index: int) -> UUID:
    return UUID(f"{prefix:08x}-0000-0000-0000-{index:012d}")


def economics(**overrides: object) -> EconomicsResult:
    values: dict[str, object] = {
        "status": "evaluated",
        "insufficiency_reason": None,
        "quantity": Decimal("10"),
        "leg_plans": (),
        "proven_floor_usd": Decimal("101"),
        "all_in_cost_usd": Decimal("100"),
        "failure_reserve_usd": Decimal("1"),
        "conservative_surplus_usd": Decimal("1.00"),
        "return_on_assigned_capital": Decimal("0.01"),
        "capacity_usd_at_current_depth": Decimal("150"),
        "stranded_collateral_by_venue": {},
        "max_capital_lock_days": Decimal("3"),
        "doubled_cost_surplus_usd": Decimal("0.50"),
    }
    values.update(overrides)
    return EconomicsResult(**values)


def daily_scan_report(day: int, **economics_overrides: object) -> ScanReport:
    as_of = AS_OF - timedelta(days=day)
    return scan_report(
        candidate_id=CANDIDATE_ID,
        proof_id=PROOF_ID,
        decision="SHADOW_CANDIDATE",
        reason="qualified basket",
        economics=economics(**economics_overrides),
        as_of=as_of,
        observed_at=as_of,
    )


def shadow_plan(index: int, **overrides: object) -> ShadowPlan:
    values: dict[str, object] = {
        "schema_version": 1,
        "proposal_id": _uuid(0x70000000, index),
        "candidate_id": CANDIDATE_ID,
        "proof_id": PROOF_ID,
        "scan_report_id": _uuid(0x71000000, index),
        "legs": (
            ShadowLegPlan(
                leg_index=0,
                venue=PredictionVenue.POLYMARKET,
                market_id="market-yes",
                outcome_token_id="token-yes",
                sequence_position=0,
                limit_price_levels=((Decimal("0.40"), Decimal("2")),),
                max_quantity=Decimal("2"),
            ),
            ShadowLegPlan(
                leg_index=1,
                venue=PredictionVenue.POLYMARKET,
                market_id="market-no",
                outcome_token_id="token-no",
                sequence_position=1,
                limit_price_levels=((Decimal("0.55"), Decimal("2")),),
                max_quantity=Decimal("2"),
            ),
        ),
        "bottleneck_leg_index": 0,
        "max_quantity": Decimal("2"),
        "order_policy": "taker_cross_only",
        "expires_at": AS_OF + timedelta(minutes=5),
        "completion_path": "buy every remaining leg",
        "cancellation_path": "cancel unfilled orders",
        "unwind_path": "sell confirmed inventory",
        "max_incomplete_exposure_usd": Decimal("1"),
        "max_incomplete_loss_usd": Decimal("0.50"),
        "frozen_hashes": (PLAN_HASH,),
        "policy_id": "research-v1",
        "policy_version": "1",
        "risk_policy_version": "1",
        "minimum_basket_payout": Decimal("1"),
        "kill_conditions": ("book unavailable",),
        "information_cutoff": AS_OF - timedelta(days=index),
        "observed_at": AS_OF - timedelta(days=index),
    }
    values.update(overrides)
    return ShadowPlan(**values)


def experiment(index: int, scenario: str, day: int, **overrides: object) -> ShadowExperiment:
    observed_at = AS_OF - timedelta(days=day)
    values: dict[str, object] = {
        "experiment_id": _uuid(0x72000000 + _SCENARIO_SLOTS[scenario], index),
        "family_id": "pilot-qualification",
        "proposal_id": _uuid(0x70000000, index),
        "scenario_id": scenario,
        "terminal_state": ShadowState.RECONCILED,
        "paper_pnl_usd": Decimal("0.10"),
        "reconciled": True,
        "as_of": observed_at,
        "observed_at": observed_at,
    }
    values.update(overrides)
    return ShadowExperiment(**values)


def reconciliation(index: int, **overrides: object) -> ShadowReconciliation:
    values: dict[str, object] = {
        "reconciliation_id": _uuid(0x73000000, index),
        "proposal_id": _uuid(0x70000000, index),
        "terminal_event_id": _uuid(0x74000000, index),
        "terminal_state": ShadowState.COMPLETE,
        "venues_reconciled": (PredictionVenue.POLYMARKET,),
        "complete": True,
        "unexplained_difference_usd": Decimal("0"),
        "observed_at": AS_OF - timedelta(days=index),
    }
    values.update(overrides)
    return ShadowReconciliation(**values)


def qualified_store(store: PredictionMarketStore) -> None:
    """Persist the minimum evidence that satisfies every recomputed gate."""

    store.append_proof_artifact(
        proof_artifact(
            proof_id=PROOF_ID,
            candidate_id=CANDIDATE_ID,
            template=FAMILY.value,
            information_cutoff=AS_OF - timedelta(days=EVIDENCE_DAYS - 1),
            observed_at=AS_OF - timedelta(days=EVIDENCE_DAYS - 1),
        )
    )
    for day in range(EVIDENCE_DAYS):
        store.append_scan_report(daily_scan_report(day))
    for day in range(SHADOW_DAYS):
        store.append_shadow_plan(shadow_plan(day))
        store.append_shadow_experiment(experiment(day, "baseline", day))
        store.append_reconciliation(reconciliation(day))
        if day < 25:
            store.append_shadow_experiment(experiment(day, "latency_1s", day))
        if day < 10:
            store.append_shadow_experiment(experiment(day, "latency_5s", day))


@pytest.fixture
def store(tmp_path: Path) -> Iterator[PredictionMarketStore]:
    opened = PredictionMarketStore(tmp_path / "qualification.duckdb")
    try:
        qualified_store(opened)
        yield opened
    finally:
        opened.close()


def test_complete_evidence_qualifies_the_family(store: PredictionMarketStore) -> None:
    report = evaluate_pilot_qualification(store, FAMILY, AS_OF)
    assert report.failed_codes == ()
    assert report.qualified is True
    assert report.evidence_hashes
    assert report.policy_identities == ("research-v1@1",)


def test_report_binds_its_window_and_family(store: PredictionMarketStore) -> None:
    report = evaluate_pilot_qualification(store, FAMILY, AS_OF)
    assert report.proof_family is FAMILY
    assert report.evidence_window_start == AS_OF - timedelta(days=EVIDENCE_DAYS)
    assert report.shadow_window_start == AS_OF - timedelta(days=SHADOW_DAYS)


def test_evidence_of_one_family_never_qualifies_another(store: PredictionMarketStore) -> None:
    other = evaluate_pilot_qualification(store, PilotProofFamily.LOGICAL_IMPLICATION, AS_OF)
    assert other.qualified is False
    assert "EVIDENCE_DAYS_INSUFFICIENT" in other.failed_codes


def test_forty_four_days_of_evidence_fail_the_evidence_clock(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "short-window.duckdb")
    try:
        qualified_store(store)
        store._connection.execute(
            "DELETE FROM scan_reports WHERE as_of = ?", [AS_OF - timedelta(days=44)]
        )
        report = evaluate_pilot_qualification(store, FAMILY, AS_OF)
        assert report.failed_codes == ("EVIDENCE_DAYS_INSUFFICIENT",)
    finally:
        store.close()


@pytest.mark.parametrize(
    ("scenario", "keep", "code"),
    [
        ("latency_1s", 24, "LATENCY_1S_OPPORTUNITIES_INSUFFICIENT"),
        ("latency_5s", 9, "LATENCY_5S_OPPORTUNITIES_INSUFFICIENT"),
    ],
)
def test_one_missing_latency_survivor_fails_that_gate(
    scenario: str, keep: int, code: str, tmp_path: Path
) -> None:
    store = PredictionMarketStore(tmp_path / f"latency-{scenario}.duckdb")
    try:
        qualified_store(store)
        store._connection.execute(
            "DELETE FROM shadow_experiments WHERE scenario_id = ? AND observed_at = ?",
            [scenario, AS_OF - timedelta(days=keep)],
        )
        report = evaluate_pilot_qualification(store, FAMILY, AS_OF)
        assert report.failed_codes == (code,)
    finally:
        store.close()


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"conservative_surplus_usd": Decimal("0.74")}, "MEDIAN_SURPLUS_INSUFFICIENT"),
        ({"capacity_usd_at_current_depth": Decimal("99")}, "MEDIAN_CAPACITY_INSUFFICIENT"),
        (
            {"return_on_assigned_capital": Decimal("0.001")},
            "RETURN_ON_CAPITAL_INSUFFICIENT",
        ),
    ],
)
def test_one_weakened_economics_threshold_fails_its_gate(
    overrides: dict[str, object], code: str, tmp_path: Path
) -> None:
    store = PredictionMarketStore(tmp_path / f"economics-{code}.duckdb")
    try:
        store.append_proof_artifact(
            proof_artifact(
                proof_id=PROOF_ID,
                candidate_id=CANDIDATE_ID,
                template=FAMILY.value,
                information_cutoff=AS_OF - timedelta(days=EVIDENCE_DAYS - 1),
                observed_at=AS_OF - timedelta(days=EVIDENCE_DAYS - 1),
            )
        )
        for day in range(EVIDENCE_DAYS):
            store.append_scan_report(daily_scan_report(day, **overrides))
        for day in range(SHADOW_DAYS):
            store.append_shadow_plan(shadow_plan(day))
            store.append_shadow_experiment(experiment(day, "baseline", day))
            store.append_reconciliation(reconciliation(day))
            if day < 25:
                store.append_shadow_experiment(experiment(day, "latency_1s", day))
            if day < 10:
                store.append_shadow_experiment(experiment(day, "latency_5s", day))
        report = evaluate_pilot_qualification(store, FAMILY, AS_OF)
        assert report.failed_codes == (code,)
    finally:
        store.close()


def test_a_review_overturned_guarantee_fails_the_false_payoff_gate(
    store: PredictionMarketStore,
) -> None:
    store.append_proof_artifact(
        proof_artifact(
            proof_id=_uuid(0x76000000, 1),
            candidate_id=CANDIDATE_ID,
            template=FAMILY.value,
            status="rejected",
            rejection_reason="OUTCOME_SET_NOT_EXHAUSTIVE",
            terminal_states=(),
            minimum_basket_payout=None,
            maximum_basket_payout=None,
            information_cutoff=AS_OF - timedelta(days=1),
            observed_at=AS_OF - timedelta(days=1),
        )
    )
    report = evaluate_pilot_qualification(store, FAMILY, AS_OF)
    assert report.failed_codes == ("FALSE_PAYOFF_CLAIM_PRESENT",)


def test_incomplete_leg_loss_at_the_threshold_fails(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "incomplete-loss.duckdb")
    try:
        qualified_store(store)
        store._connection.execute(
            "DELETE FROM shadow_plans WHERE proposal_id = ?", [_uuid(0x70000000, 0)]
        )
        store.append_shadow_plan(shadow_plan(0, max_incomplete_loss_usd=Decimal("0.625")))
        report = evaluate_pilot_qualification(store, FAMILY, AS_OF)
        assert report.failed_codes == ("INCOMPLETE_LEG_LOSS_EXCEEDED",)
    finally:
        store.close()


def test_a_losing_shadow_month_fails_contribution_drawdown_and_profit(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "losing-shadow.duckdb")
    try:
        store.append_proof_artifact(
            proof_artifact(
                proof_id=PROOF_ID,
                candidate_id=CANDIDATE_ID,
                template=FAMILY.value,
                information_cutoff=AS_OF - timedelta(days=EVIDENCE_DAYS - 1),
                observed_at=AS_OF - timedelta(days=EVIDENCE_DAYS - 1),
            )
        )
        for day in range(EVIDENCE_DAYS):
            store.append_scan_report(daily_scan_report(day))
        for day in range(SHADOW_DAYS):
            store.append_shadow_plan(shadow_plan(day))
            store.append_shadow_experiment(
                experiment(day, "baseline", day, paper_pnl_usd=Decimal("-30"))
            )
            store.append_reconciliation(reconciliation(day))
            if day < 25:
                store.append_shadow_experiment(experiment(day, "latency_1s", day))
            if day < 10:
                store.append_shadow_experiment(experiment(day, "latency_5s", day))
        report = evaluate_pilot_qualification(store, FAMILY, AS_OF)
        assert report.failed_codes == (
            "ANNUAL_CONTRIBUTION_INSUFFICIENT",
            "DRAWDOWN_EXCEEDED",
            "SHADOW_PROFIT_NOT_POSITIVE",
        )
    finally:
        store.close()


def test_twenty_nine_shadow_days_fail_the_shadow_clock(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "short-shadow.duckdb")
    try:
        qualified_store(store)
        store._connection.execute(
            "DELETE FROM shadow_experiments WHERE scenario_id = 'baseline' AND observed_at = ?",
            [AS_OF - timedelta(days=29)],
        )
        report = evaluate_pilot_qualification(store, FAMILY, AS_OF)
        assert report.failed_codes == ("SHADOW_DAYS_INSUFFICIENT",)
    finally:
        store.close()


def test_reward_credited_profit_fails_the_reward_gate(store: PredictionMarketStore) -> None:
    store.append_ledger_posting(
        LedgerPosting(
            posting_id=_uuid(0x77000000, 1),
            proposal_id=_uuid(0x70000000, 0),
            event_id=_uuid(0x78000000, 1),
            venue=PredictionVenue.POLYMARKET,
            account="venue_cash",
            debit_usd=Decimal("1"),
            credit_usd=Decimal("0"),
            occurred_at=AS_OF - timedelta(days=1),
            detail="liquidity reward credited to the basket",
        )
    )
    report = evaluate_pilot_qualification(store, FAMILY, AS_OF)
    assert report.failed_codes == ("SHADOW_PROFIT_REWARD_DEPENDENT",)


def test_an_unknown_shadow_outcome_is_a_risk_breach(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "risk-breach.duckdb")
    try:
        qualified_store(store)
        store.append_shadow_experiment(
            experiment(
                0,
                "baseline",
                1,
                experiment_id=_uuid(0x79000000, 1),
                terminal_state=ShadowState.UNKNOWN,
                paper_pnl_usd=None,
                reconciled=False,
            )
        )
        report = evaluate_pilot_qualification(store, FAMILY, AS_OF)
        assert report.failed_codes == ("SHADOW_RISK_BREACH",)
    finally:
        store.close()


def test_unreconciled_shadow_state_fails_reconciliation(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "unreconciled.duckdb")
    try:
        qualified_store(store)
        store._connection.execute(
            "DELETE FROM shadow_reconciliations WHERE proposal_id = ?", [_uuid(0x70000000, 3)]
        )
        report = evaluate_pilot_qualification(store, FAMILY, AS_OF)
        assert report.failed_codes == ("SHADOW_RECONCILIATION_INCOMPLETE",)
    finally:
        store.close()


def test_the_cash_benchmark_comes_from_the_frozen_research_policy() -> None:
    assert Decimal("0.0002") * 365 == APPROVED_CASH_BENCHMARK_RATE
