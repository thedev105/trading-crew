from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from polytrading.predictions.domain import PredictionVenue
from polytrading.predictions.execution.models import ImmediateOrderType
from polytrading.predictions.pilot.models import PILOT_CEILINGS, PilotProofFamily
from polytrading.predictions.pilot.selector import (
    FrozenRecoveryBranch,
    PilotAccountState,
    PilotLeg,
    PilotOpportunity,
    PilotSelectionError,
    compile_frozen_pilot_plan,
    eligible_opportunities,
    first_tie_break_field,
    rank_pilot_opportunities,
    require_eligible,
)
from polytrading.predictions.shadow_models import ShadowLegPlan, ShadowPlan, ShadowState
from polytrading.predictions.storage.store import PredictionMarketStore
from tests.predictions.pilot_helpers import ACCOUNT_FINGERPRINT, WALLET_FINGERPRINT
from tests.predictions.proof_helpers import equivalence_matrix, proof_artifact
from tests.predictions.scan_helpers import scan_report
from tests.predictions.test_pilot_qualification import economics, experiment

NOW = datetime(2026, 8, 29, 12, tzinfo=UTC)
PROOF_ID = UUID("00000000-0000-0000-0000-000000006001")
CANDIDATE_ID = UUID("00000000-0000-0000-0000-000000003001")
ALL_FAMILIES = frozenset(PilotProofFamily)


def account(**overrides: Any) -> PilotAccountState:
    fields: dict[str, Any] = {
        "account_fingerprint": ACCOUNT_FINGERPRINT,
        "wallet_fingerprint": WALLET_FINGERPRINT,
        "collateral_usd": Decimal("200"),
        "allowance_usd": Decimal("200"),
        "kill_engaged": False,
        "observed_at": NOW,
    }
    fields.update(overrides)
    return PilotAccountState.model_validate(fields, strict=True)


def leg(index: int, **overrides: Any) -> PilotLeg:
    fields: dict[str, Any] = {
        "leg_index": index,
        "outcome_token_id": f"token-{index}",
        "side": "buy",
        "limit_price": Decimal("0.40"),
        "size": Decimal("10"),
        "order_type": ImmediateOrderType.FAK,
    }
    fields.update(overrides)
    return PilotLeg.model_validate(fields, strict=True)


def opportunity(**overrides: Any) -> PilotOpportunity:
    fields: dict[str, Any] = {
        "schema_version": 1,
        "proof_id": PROOF_ID,
        "candidate_id": CANDIDATE_ID,
        "proposal_id": UUID("70000000-0000-0000-0000-000000000001"),
        "proof_family": PilotProofFamily.BINARY_COMPLEMENT,
        "legs": (leg(0), leg(1, outcome_token_id="token-1", limit_price=Decimal("0.55"))),
        "current_surplus_usd": Decimal("0.40"),
        "stressed_surplus_usd": Decimal("0.20"),
        "capacity_usd": Decimal("150"),
        "incomplete_loss_usd": Decimal("0.50"),
        "deployed_capital_usd": Decimal("9.50"),
        "evidence_hashes": ("a" * 64,),
        "information_cutoff": NOW,
    }
    fields.update(overrides)
    return PilotOpportunity.model_validate(fields, strict=True)


def eligible(**overrides: Any) -> None:
    require_eligible(
        opportunity(**overrides.pop("opportunity", {})),
        account=overrides.pop("account", account()),
        limits=overrides.pop("limits", PILOT_CEILINGS),
        enabled_families=overrides.pop("enabled_families", ALL_FAMILIES),
        as_of=overrides.pop("as_of", NOW),
    )


def test_a_complete_opportunity_is_eligible() -> None:
    eligible()


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"account": {"kill_engaged": True}}, "KILL_ENGAGED"),
        (
            {"enabled_families": frozenset({PilotProofFamily.LOGICAL_IMPLICATION})},
            "FAMILY_NOT_ENABLED",
        ),
        ({"as_of": NOW + timedelta(minutes=6)}, "EVIDENCE_STALE"),
        ({"opportunity": {"current_surplus_usd": Decimal("0")}}, "SURPLUS_NOT_POSITIVE"),
        ({"opportunity": {"stressed_surplus_usd": Decimal("0")}}, "STRESSED_SURPLUS_NOT_POSITIVE"),
        ({"opportunity": {"incomplete_loss_usd": Decimal("0.625")}}, "INCOMPLETE_LOSS_EXCEEDED"),
        ({"account": {"collateral_usd": Decimal("1")}}, "INSUFFICIENT_BALANCE"),
        ({"account": {"allowance_usd": Decimal("1")}}, "INSUFFICIENT_ALLOWANCE"),
    ],
)
def test_every_eligibility_gate_names_itself(kwargs: dict[str, Any], code: str) -> None:
    prepared: dict[str, Any] = {}
    if "account" in kwargs:
        prepared["account"] = account(**kwargs.pop("account"))
    prepared.update(kwargs)

    with pytest.raises(PilotSelectionError) as raised:
        eligible(**prepared)
    assert raised.value.code == code


def test_a_leg_above_the_order_ceiling_is_refused() -> None:
    with pytest.raises(PilotSelectionError) as raised:
        eligible(opportunity={"legs": (leg(0, size=Decimal("30")), leg(1))})
    assert raised.value.code == "ORDER_NOTIONAL_EXCEEDED"


def test_a_strategy_above_its_gross_ceiling_is_refused() -> None:
    legs = tuple(leg(index, size=Decimal("20"), limit_price=Decimal("0.45")) for index in range(3))
    with pytest.raises(PilotSelectionError) as raised:
        eligible(opportunity={"legs": legs})
    assert raised.value.code == "STRATEGY_NOTIONAL_EXCEEDED"


def test_a_single_leg_strategy_is_not_representable() -> None:
    with pytest.raises(ValueError, match="at least two legs"):
        opportunity(legs=(leg(0),))


def test_ranking_is_incomplete_loss_then_stressed_surplus_then_capacity_then_id() -> None:
    lowest_loss = opportunity(
        proof_id=UUID("00000000-0000-0000-0000-0000000000a1"),
        incomplete_loss_usd=Decimal("0.10"),
    )
    higher_loss_best_surplus = opportunity(
        proof_id=UUID("00000000-0000-0000-0000-0000000000a2"),
        incomplete_loss_usd=Decimal("0.50"),
        stressed_surplus_usd=Decimal("0.90"),
    )
    higher_loss_more_capacity = opportunity(
        proof_id=UUID("00000000-0000-0000-0000-0000000000a3"),
        incomplete_loss_usd=Decimal("0.50"),
        stressed_surplus_usd=Decimal("0.20"),
        capacity_usd=Decimal("400"),
    )
    identical_but_later_id = opportunity(
        proof_id=UUID("00000000-0000-0000-0000-0000000000a4"),
        incomplete_loss_usd=Decimal("0.50"),
        stressed_surplus_usd=Decimal("0.20"),
        capacity_usd=Decimal("150"),
    )
    tie_by_id = opportunity(
        proof_id=UUID("00000000-0000-0000-0000-0000000000a0"),
        incomplete_loss_usd=Decimal("0.50"),
        stressed_surplus_usd=Decimal("0.20"),
        capacity_usd=Decimal("150"),
    )
    permuted = [
        identical_but_later_id,
        higher_loss_more_capacity,
        tie_by_id,
        lowest_loss,
        higher_loss_best_surplus,
    ]

    ranked = rank_pilot_opportunities(permuted)

    assert [item.proof_id for item in ranked] == [
        lowest_loss.proof_id,
        higher_loss_best_surplus.proof_id,
        higher_loss_more_capacity.proof_id,
        tie_by_id.proof_id,
        identical_but_later_id.proof_id,
    ]
    assert rank_pilot_opportunities(list(reversed(permuted))) == ranked


def test_the_first_tie_break_field_is_reportable() -> None:
    base = opportunity()
    assert (
        first_tie_break_field(base, opportunity(incomplete_loss_usd=Decimal("0.10")))
        == "incomplete_loss_ratio"
    )
    assert (
        first_tie_break_field(base, opportunity(stressed_surplus_usd=Decimal("0.9")))
        == "stressed_surplus_usd"
    )
    assert first_tie_break_field(base, opportunity(capacity_usd=Decimal("400"))) == "capacity_usd"
    assert first_tie_break_field(base, opportunity()) == "proof_id"


def test_a_frozen_plan_binds_legs_recovery_and_budgets() -> None:
    plan = compile_frozen_pilot_plan(
        opportunity(), PILOT_CEILINGS, account(), deadline=NOW + timedelta(seconds=30)
    )

    assert plan.legs == opportunity().legs
    assert len(plan.recovery_branches) == 1
    assert plan.recovery_branches[0].leg.order_type is ImmediateOrderType.FOK
    assert plan.recovery_branches[0].leg.side == "sell"
    assert plan.recovery_branches[0].additional_deployed_capital_usd == Decimal("0")
    assert plan.gross_notional_usd > opportunity().gross_notional
    assert plan.plan_hash == plan.model_copy().plan_hash


def test_a_recovery_branch_must_reduce_worst_case_exposure() -> None:
    with pytest.raises(ValueError, match="reduce worst-case incomplete exposure"):
        FrozenRecoveryBranch.model_validate(
            {
                "trigger": "LEG_INCOMPLETE",
                "leg": leg(0),
                "worst_case_exposure_before_usd": Decimal("4"),
                "worst_case_exposure_after_usd": Decimal("4"),
            },
            strict=True,
        )


def test_recovery_consumes_the_shared_strategy_budget() -> None:
    # Two legs of USD 9 fit the USD 25 strategy ceiling on their own; adding the USD 9 recovery
    # unwind does not, so the plan is refused rather than quietly exceeding the budget.
    crowded = opportunity(
        legs=(
            leg(0, size=Decimal("20"), limit_price=Decimal("0.45")),
            leg(1, size=Decimal("20"), limit_price=Decimal("0.45")),
        )
    )
    with pytest.raises(PilotSelectionError) as raised:
        compile_frozen_pilot_plan(
            crowded, PILOT_CEILINGS, account(), deadline=NOW + timedelta(seconds=30)
        )
    assert raised.value.code == "STRATEGY_NOTIONAL_EXCEEDED"


def shadow_plan(**overrides: Any) -> ShadowPlan:
    venue = overrides.pop("venue", PredictionVenue.POLYMARKET)
    fields: dict[str, Any] = {
        "schema_version": 1,
        "proposal_id": UUID("70000000-0000-0000-0000-000000000001"),
        "candidate_id": CANDIDATE_ID,
        "proof_id": PROOF_ID,
        "scan_report_id": UUID("71000000-0000-0000-0000-000000000001"),
        "legs": (
            ShadowLegPlan(
                leg_index=0,
                venue=PredictionVenue.POLYMARKET,
                market_id="market-yes",
                outcome_token_id="token-yes",
                sequence_position=0,
                limit_price_levels=((Decimal("0.40"), Decimal("10")),),
                max_quantity=Decimal("10"),
            ),
            ShadowLegPlan(
                leg_index=1,
                venue=venue,
                market_id="market-no",
                outcome_token_id="token-no",
                sequence_position=1,
                limit_price_levels=((Decimal("0.55"), Decimal("10")),),
                max_quantity=Decimal("10"),
            ),
        ),
        "bottleneck_leg_index": 0,
        "max_quantity": Decimal("10"),
        "order_policy": "taker_cross_only",
        "expires_at": NOW + timedelta(minutes=5),
        "completion_path": "buy every remaining leg",
        "cancellation_path": "cancel unfilled orders",
        "unwind_path": "sell confirmed inventory",
        "max_incomplete_exposure_usd": Decimal("1"),
        "max_incomplete_loss_usd": Decimal("0.50"),
        "frozen_hashes": ("d" * 64,),
        "policy_id": "research-v1",
        "policy_version": "1",
        "risk_policy_version": "1",
        "minimum_basket_payout": Decimal("1"),
        "kill_conditions": ("book unavailable",),
        "information_cutoff": NOW,
        "observed_at": NOW,
    }
    fields.update(overrides)
    return ShadowPlan(**fields)


def populated_store(store: PredictionMarketStore, **overrides: Any) -> None:
    template = overrides.pop("template", PilotProofFamily.BINARY_COMPLEMENT.value)
    store.append_proof_artifact(
        proof_artifact(
            proof_id=PROOF_ID,
            candidate_id=CANDIDATE_ID,
            template=template,
            equivalence_matrix=(
                equivalence_matrix()
                if template == PilotProofFamily.WITHIN_VENUE_EQUIVALENCE.value
                else None
            ),
            information_cutoff=NOW,
            observed_at=NOW,
        )
    )
    store.append_scan_report(
        scan_report(
            candidate_id=CANDIDATE_ID,
            proof_id=PROOF_ID,
            decision="SHADOW_CANDIDATE",
            reason="eligible basket",
            economics=economics(
                all_in_cost_usd=Decimal("9.50"),
                conservative_surplus_usd=Decimal("0.40"),
                doubled_cost_surplus_usd=Decimal("0.20"),
            ),
            as_of=NOW,
            observed_at=NOW,
        )
    )
    store.append_shadow_plan(shadow_plan(**overrides.pop("plan", {})))
    for scenario in overrides.pop("scenarios", ("baseline", "latency_5s")):
        store.append_shadow_experiment(
            experiment(1, scenario, 0, observed_at=NOW, as_of=NOW).model_copy(
                update={"proposal_id": UUID("70000000-0000-0000-0000-000000000001")}
            )
        )


def test_the_selector_rebuilds_eligible_opportunities_from_the_store(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "selector.duckdb")
    try:
        populated_store(store)
        selected = eligible_opportunities(store, account(), NOW)

        assert len(selected) == 1
        assert selected[0].proof_family is PilotProofFamily.BINARY_COMPLEMENT
        assert selected[0].legs[0].order_type is ImmediateOrderType.FAK
        assert selected[0].stressed_surplus_usd == Decimal("0.20")
    finally:
        store.close()


def test_a_cross_venue_plan_is_never_selected(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "cross-venue.duckdb")
    try:
        populated_store(store, plan={"venue": PredictionVenue.KALSHI})
        assert eligible_opportunities(store, account(), NOW) == ()
    finally:
        store.close()


def test_a_plan_without_a_shadow_replay_is_never_selected(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "no-replay.duckdb")
    try:
        populated_store(store, scenarios=("latency_5s",))
        assert eligible_opportunities(store, account(), NOW) == ()
    finally:
        store.close()


def test_a_plan_without_a_latency_survivor_has_no_stressed_surplus(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "no-stress.duckdb")
    try:
        populated_store(store, scenarios=("baseline",))
        assert eligible_opportunities(store, account(), NOW) == ()
    finally:
        store.close()


def test_a_killed_account_selects_nothing(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "killed.duckdb")
    try:
        populated_store(store)
        assert eligible_opportunities(store, account(kill_engaged=True), NOW) == ()
    finally:
        store.close()


@pytest.mark.parametrize(
    "template",
    [
        PilotProofFamily.EXHAUSTIVE_OUTCOME_SET.value,
        PilotProofFamily.LOGICAL_IMPLICATION.value,
        PilotProofFamily.WITHIN_VENUE_EQUIVALENCE.value,
    ],
)
def test_every_enabled_proof_family_can_be_selected(template: str, tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / f"family-{template[:6]}.duckdb")
    try:
        populated_store(store, template=template)
        selected = eligible_opportunities(store, account(), NOW)
        assert [item.proof_family.value for item in selected] == [template]
    finally:
        store.close()


def test_a_family_that_is_not_qualified_is_filtered_out(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "unqualified.duckdb")
    try:
        populated_store(store)
        selected = eligible_opportunities(
            store,
            account(),
            NOW,
            enabled_families=frozenset({PilotProofFamily.LOGICAL_IMPLICATION}),
        )
        assert selected == ()
    finally:
        store.close()


def test_stale_evidence_selects_nothing(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "stale.duckdb")
    try:
        populated_store(store)
        assert eligible_opportunities(store, account(), NOW + timedelta(minutes=10)) == ()
    finally:
        store.close()


def test_an_unproven_relationship_is_never_selected(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "rejected-proof.duckdb")
    try:
        store.append_proof_artifact(
            proof_artifact(
                proof_id=PROOF_ID,
                candidate_id=CANDIDATE_ID,
                status="rejected",
                rejection_reason="OUTCOME_SET_NOT_EXHAUSTIVE",
                terminal_states=(),
                minimum_basket_payout=None,
                maximum_basket_payout=None,
                information_cutoff=NOW,
                observed_at=NOW,
            )
        )
        store.append_shadow_plan(shadow_plan())
        store.append_shadow_experiment(
            experiment(1, "baseline", 0, observed_at=NOW, as_of=NOW).model_copy(
                update={"proposal_id": UUID("70000000-0000-0000-0000-000000000001")}
            )
        )
        assert eligible_opportunities(store, account(), NOW) == ()
    finally:
        store.close()


def test_shadow_states_used_for_survival_are_the_terminal_successes() -> None:
    assert ShadowState.COMPLETE.value == "complete"
    assert ShadowState.RECONCILED.value == "reconciled"
