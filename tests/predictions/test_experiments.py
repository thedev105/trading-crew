from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import ValidationError

from polytrading.predictions.domain import PredictionVenue
from polytrading.predictions.experiments import ShadowExperiment, TrialFamily
from polytrading.predictions.shadow_models import ShadowState

NOW = datetime(2026, 8, 24, 16, 0, tzinfo=UTC)


def trial_family(**overrides: object) -> TrialFamily:
    values: dict[str, object] = {
        "family_id": "cross-venue-equivalence-v1",
        "hypothesis": "Equivalent contracts retain positive surplus after doubled costs.",
        "preregistered_at": NOW,
        "thresholds_json": '{"minimum_surplus_usd":"5.00","version":1}',
        "venues": (PredictionVenue.KALSHI, PredictionVenue.POLYMARKET),
        "registered_by": "research-operator@example.com",
    }
    values.update(overrides)
    return TrialFamily(**values)


def shadow_experiment(**overrides: object) -> ShadowExperiment:
    values: dict[str, object] = {
        "experiment_id": UUID("00000000-0000-0000-0000-00000000e001"),
        "family_id": "cross-venue-equivalence-v1",
        "proposal_id": UUID("00000000-0000-0000-0000-00000000e002"),
        "scenario_id": "baseline",
        "terminal_state": ShadowState.RECONCILED,
        "paper_pnl_usd": Decimal("-2.50"),
        "reconciled": True,
        "as_of": NOW,
        "observed_at": NOW,
    }
    values.update(overrides)
    return ShadowExperiment(**values)


def test_trial_family_preserves_exact_frozen_threshold_text_and_is_frozen() -> None:
    thresholds = '{ "version": 1, "minimum_surplus_usd": "5.00" }'
    family = trial_family(thresholds_json=thresholds)

    assert family.thresholds_json == thresholds
    with pytest.raises(ValidationError):
        family.hypothesis = "changed after seeing results"


@pytest.mark.parametrize("field", ["family_id", "hypothesis", "thresholds_json", "registered_by"])
def test_trial_family_rejects_empty_or_whitespace_text(field: str) -> None:
    with pytest.raises(ValidationError):
        trial_family(**{field: "   "})


@pytest.mark.parametrize("thresholds", ["[]", "null", '"threshold"', "not-json"])
def test_trial_family_requires_thresholds_json_to_be_a_json_object(thresholds: str) -> None:
    with pytest.raises(ValidationError):
        trial_family(thresholds_json=thresholds)


@pytest.mark.parametrize(
    "venues",
    [
        (),
        (PredictionVenue.POLYMARKET, PredictionVenue.KALSHI),
        (PredictionVenue.KALSHI, PredictionVenue.KALSHI),
    ],
)
def test_trial_family_requires_nonempty_sorted_unique_venues(
    venues: tuple[PredictionVenue, ...],
) -> None:
    with pytest.raises(ValidationError):
        trial_family(venues=venues)


def test_trial_family_normalizes_preregistered_at_to_utc_and_rejects_naive_time() -> None:
    eastern = timezone(timedelta(hours=-4))
    assert trial_family(preregistered_at=NOW.astimezone(eastern)).preregistered_at == NOW

    with pytest.raises(ValidationError):
        trial_family(preregistered_at=NOW.replace(tzinfo=None))


@pytest.mark.parametrize("field", ["family_id", "scenario_id"])
def test_shadow_experiment_rejects_empty_or_whitespace_text(field: str) -> None:
    with pytest.raises(ValidationError):
        shadow_experiment(**{field: "   "})


def test_shadow_experiment_requires_reconciliation_before_paper_pnl() -> None:
    with pytest.raises(ValidationError):
        shadow_experiment(paper_pnl_usd=Decimal("1.00"), reconciled=False)


@pytest.mark.parametrize(
    "state",
    [
        ShadowState.DISCOVERED,
        ShadowState.PROOF_VALIDATED,
        ShadowState.ECONOMICS_VALIDATED,
        ShadowState.SHADOW_PLANNED,
        ShadowState.FIRST_LEG_SIMULATED,
    ],
)
def test_shadow_experiment_rejects_intermediate_states(state: ShadowState) -> None:
    with pytest.raises(ValidationError):
        shadow_experiment(terminal_state=state)


@pytest.mark.parametrize(
    "state",
    [
        ShadowState.COMPLETE,
        ShadowState.UNWOUND,
        ShadowState.EXPIRED,
        ShadowState.UNKNOWN,
        ShadowState.RECONCILED,
    ],
)
def test_shadow_experiment_accepts_terminal_and_reconciled_states(state: ShadowState) -> None:
    assert shadow_experiment(terminal_state=state).terminal_state is state


def test_shadow_experiment_keeps_unknown_unreconciled_and_losing_rows() -> None:
    unknown = shadow_experiment(
        experiment_id=UUID("00000000-0000-0000-0000-00000000e003"),
        terminal_state=ShadowState.UNKNOWN,
        paper_pnl_usd=None,
        reconciled=False,
    )
    losing = shadow_experiment(paper_pnl_usd=Decimal("-2.50"), reconciled=True)

    assert unknown.paper_pnl_usd is None
    assert unknown.reconciled is False
    assert losing.paper_pnl_usd == Decimal("-2.50")


@pytest.mark.parametrize("field", ["as_of", "observed_at"])
def test_shadow_experiment_revalidates_utc_timestamps(field: str) -> None:
    eastern = timezone(timedelta(hours=-4))
    assert getattr(shadow_experiment(**{field: NOW.astimezone(eastern)}), field) == NOW

    with pytest.raises(ValidationError):
        shadow_experiment(**{field: NOW.replace(tzinfo=None)})
