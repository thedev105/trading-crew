from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from polytrading.predictions.pilot.models import PILOT_CEILINGS
from polytrading.predictions.pilot.sessions import AutomationSessionRunner, SessionDecision

NOW = datetime(2026, 8, 29, 12, tzinfo=UTC)


@dataclass(frozen=True)
class FakeSessionState:
    kill_engaged: bool = False
    presence_present: bool = True
    loss_unknown: bool = False
    session_loss_usd: Decimal = Decimal("0")
    utc_day_loss_usd: Decimal = Decimal("0")
    active_strategies: int = 0
    strategies_started: int = 0
    deployed_capital_usd: Decimal = Decimal("0")


def runner(**overrides: Any) -> AutomationSessionRunner:
    state = FakeSessionState(**overrides)
    return AutomationSessionRunner(
        started_at=NOW, limits=PILOT_CEILINGS, state_reader=lambda _now: state
    )


def test_a_healthy_session_starts_one_strategy_without_prompting_again() -> None:
    decision = runner().tick(NOW + timedelta(seconds=1))

    assert isinstance(decision, SessionDecision)
    assert decision.action == "START_STRATEGY"
    assert decision.reason is None


def test_only_one_strategy_runs_at_a_time() -> None:
    decision = runner(active_strategies=1).tick(NOW + timedelta(seconds=1))

    assert decision.action == "WAIT"
    assert decision.reason == "STRATEGY_ACTIVE"


def test_a_session_expires_after_fifteen_minutes() -> None:
    decision = runner().tick(NOW + timedelta(minutes=15))

    assert decision.action == "STOP"
    assert decision.reason == "SESSION_EXPIRED"


def test_no_new_strategy_starts_in_the_final_minute() -> None:
    decision = runner().tick(NOW + timedelta(minutes=14, seconds=30))

    assert decision.action == "WAIT"
    assert decision.reason == "FINAL_MINUTE"


def test_the_deployment_ceiling_pauses_new_strategies() -> None:
    decision = runner(deployed_capital_usd=Decimal("50")).tick(NOW + timedelta(seconds=1))

    assert decision.action == "WAIT"
    assert decision.reason == "LIMIT_BREACH"


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"kill_engaged": True}, "PRESENCE_LOST"),
        ({"presence_present": False}, "PRESENCE_LOST"),
        ({"loss_unknown": True}, "UNKNOWN_OUTCOME"),
        ({"session_loss_usd": Decimal("5.01")}, "LIMIT_BREACH"),
        ({"utc_day_loss_usd": Decimal("10.01")}, "LIMIT_BREACH"),
    ],
)
def test_every_stop_condition_ends_the_session(overrides: dict[str, Any], reason: str) -> None:
    decision = runner(**overrides).tick(NOW + timedelta(seconds=1))

    assert decision.action == "STOP"
    assert decision.reason == reason


def test_a_session_at_its_loss_ceiling_still_runs() -> None:
    decision = runner(session_loss_usd=Decimal("5")).tick(NOW + timedelta(seconds=1))

    assert decision.action == "START_STRATEGY"


def test_an_operator_stop_is_final() -> None:
    session = runner()
    stopped = session.stop("OPERATOR_STOP", NOW + timedelta(seconds=5))

    assert stopped.action == "STOP"
    assert stopped.reason == "OPERATOR_STOP"
    assert session.tick(NOW + timedelta(seconds=6)).action == "STOP"


def test_the_session_reports_its_own_expiry() -> None:
    assert runner().expires_at == NOW + timedelta(minutes=15)


def test_a_decision_reports_persisted_counters_not_browser_ones() -> None:
    decision = runner(strategies_started=3, deployed_capital_usd=Decimal("12")).tick(
        NOW + timedelta(seconds=1)
    )

    assert decision.strategies_started == 3
    assert decision.deployed_capital_usd == Decimal("12")
