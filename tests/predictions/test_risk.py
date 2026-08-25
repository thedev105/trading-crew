from __future__ import annotations

import importlib
from decimal import Decimal

import pytest
from pydantic import ValidationError


def _risk() -> object:
    """Load the module lazily so RED is a test failure when it does not exist yet."""
    return importlib.import_module("polytrading.predictions.risk")


def _policy(risk: object, **overrides: object) -> object:
    values = {"policy_version": "risk-v1"}
    values.update(overrides)
    return risk.PredictionRiskPolicy(**values)


def _portfolio(risk: object, **overrides: object) -> object:
    values = {
        "total_equity_usd": Decimal("10000"),
        "open_exposure_usd_by_cluster": {},
        "peak_equity_usd": Decimal("10000"),
        "equity_24h_ago_usd": Decimal("10000"),
        "open_proposal_count": 0,
    }
    values.update(overrides)
    return risk.ShadowPortfolioState(**values)


def _evaluate(
    risk: object,
    *,
    basket_cost_usd: Decimal = Decimal("100"),
    max_incomplete_loss_usd: Decimal = Decimal("10"),
    event_cluster_id: str = "event-a",
    portfolio: object | None = None,
    policy: object | None = None,
) -> object:
    return risk.evaluate_risk_gate(
        basket_cost_usd=basket_cost_usd,
        max_incomplete_loss_usd=max_incomplete_loss_usd,
        event_cluster_id=event_cluster_id,
        portfolio=portfolio or _portfolio(risk),
        policy=policy or _policy(risk),
    )


def test_policy_defaults_are_the_conservative_shadow_limits() -> None:
    """A relaxed default could allow a larger shadow trade without review."""
    risk = _risk()

    policy = _policy(risk)

    assert policy.max_basket_fraction_of_equity == Decimal("0.05")
    assert policy.max_event_cluster_fraction == Decimal("0.10")
    assert policy.max_incomplete_loss_fraction == Decimal("0.0025")
    assert policy.drawdown_halt_new_entries == Decimal("0.02")
    assert policy.drawdown_halve_size == Decimal("0.05")
    assert policy.drawdown_stop_all == Decimal("0.08")
    assert policy.drawdown_close_nonguaranteed == Decimal("0.12")
    assert policy.drawdown_capital_preservation == Decimal("0.15")
    assert policy.max_live_venues == 2
    assert policy.pilot_cap_usd == Decimal("250")
    assert policy.starting_equity_usd == Decimal("10000")
    assert risk.DEFAULT_RISK_POLICY.policy_version


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("starting_equity_usd", Decimal("0")),
        ("max_basket_fraction_of_equity", Decimal("NaN")),
        ("max_event_cluster_fraction", Decimal("Infinity")),
        ("max_incomplete_loss_fraction", Decimal("-0.01")),
        ("drawdown_halt_new_entries", Decimal("0")),
        ("pilot_cap_usd", Decimal("-1")),
        ("max_live_venues", 0),
    ],
)
def test_policy_rejects_invalid_risk_numerics(field: str, invalid: object) -> None:
    """Non-finite or non-positive limits make a gate unsafe to evaluate."""
    risk = _risk()

    with pytest.raises(ValidationError):
        _policy(risk, **{field: invalid})


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("total_equity_usd", Decimal("0")),
        ("peak_equity_usd", Decimal("NaN")),
        ("equity_24h_ago_usd", Decimal("Infinity")),
        ("open_exposure_usd_by_cluster", {"event-a": Decimal("-1")}),
        ("open_proposal_count", -1),
    ],
)
def test_portfolio_rejects_invalid_risk_numerics(field: str, invalid: object) -> None:
    """Invalid equity or exposure values could bypass percentage risk limits."""
    risk = _risk()

    with pytest.raises(ValidationError):
        _portfolio(risk, **{field: invalid})


def test_policy_is_frozen_so_strategy_cannot_raise_a_limit() -> None:
    """A mutable policy would let strategy code bypass a conservative risk gate."""
    risk = _risk()
    policy = _policy(risk)

    with pytest.raises(ValidationError):
        policy.max_basket_fraction_of_equity = Decimal("1")


@pytest.mark.parametrize(
    ("portfolio_overrides", "expected_reason"),
    [
        (
            {"total_equity_usd": Decimal("8500"), "peak_equity_usd": Decimal("10000")},
            "CAPITAL_PRESERVATION_MODE",
        ),
        (
            {"total_equity_usd": Decimal("9200"), "peak_equity_usd": Decimal("10000")},
            "DRAWDOWN_STOP_ALL",
        ),
        (
            {
                "total_equity_usd": Decimal("9800"),
                "peak_equity_usd": Decimal("10000"),
                "equity_24h_ago_usd": Decimal("10000"),
            },
            "DRAWDOWN_HALT",
        ),
    ],
)
def test_drawdown_refuses_at_each_literal_threshold(
    portfolio_overrides: dict[str, object], expected_reason: str
) -> None:
    """Changing a >= drawdown check to > would admit its stated threshold."""
    risk = _risk()

    decision = _evaluate(risk, portfolio=_portfolio(risk, **portfolio_overrides))

    assert decision.allowed is False
    assert decision.reason == expected_reason


def test_capital_preservation_wins_when_every_drawdown_gate_trips() -> None:
    """Checking stop-all before preservation would return the less severe refusal."""
    risk = _risk()
    portfolio = _portfolio(
        risk,
        total_equity_usd=Decimal("8000"),
        peak_equity_usd=Decimal("10000"),
        equity_24h_ago_usd=Decimal("10000"),
    )

    decision = _evaluate(risk, portfolio=portfolio)

    assert decision.allowed is False
    assert decision.reason == "CAPITAL_PRESERVATION_MODE"


def test_stop_all_wins_before_daily_halt_and_position_limits() -> None:
    """Moving the 8% check later would report a less severe risk failure."""
    risk = _risk()
    portfolio = _portfolio(
        risk,
        total_equity_usd=Decimal("9200"),
        peak_equity_usd=Decimal("10000"),
        equity_24h_ago_usd=Decimal("10000"),
    )

    decision = _evaluate(
        risk,
        basket_cost_usd=Decimal("501"),
        max_incomplete_loss_usd=Decimal("26"),
        portfolio=portfolio,
    )

    assert decision.allowed is False
    assert decision.reason == "DRAWDOWN_STOP_ALL"


def test_daily_halt_wins_before_position_limits() -> None:
    """Moving the daily loss check later would report a basket-size refusal instead."""
    risk = _risk()
    portfolio = _portfolio(
        risk, total_equity_usd=Decimal("9800"), equity_24h_ago_usd=Decimal("10000")
    )

    decision = _evaluate(risk, basket_cost_usd=Decimal("501"), portfolio=portfolio)

    assert decision.allowed is False
    assert decision.reason == "DRAWDOWN_HALT"


@pytest.mark.parametrize(
    ("basket_cost_usd", "allowed", "reason"),
    [
        (Decimal("500"), True, None),
        (Decimal("500.01"), False, "BASKET_TOO_LARGE"),
    ],
)
def test_basket_fraction_allows_its_cap_and_refuses_above_it(
    basket_cost_usd: Decimal, allowed: bool, reason: str | None
) -> None:
    """A wrong basket comparison would mishandle the 5% equity boundary."""
    risk = _risk()

    decision = _evaluate(risk, basket_cost_usd=basket_cost_usd)

    assert decision.allowed is allowed
    assert decision.reason == reason


@pytest.mark.parametrize(
    ("existing_exposure", "basket_cost_usd", "allowed", "reason"),
    [
        (Decimal("500"), Decimal("500"), True, None),
        (Decimal("600"), Decimal("400.01"), False, "CLUSTER_CONCENTRATION"),
    ],
)
def test_cluster_fraction_includes_this_basket_at_the_cap(
    existing_exposure: Decimal, basket_cost_usd: Decimal, allowed: bool, reason: str | None
) -> None:
    """Omitting the proposed basket would let cluster exposure exceed its 10% cap."""
    risk = _risk()
    portfolio = _portfolio(risk, open_exposure_usd_by_cluster={"event-a": existing_exposure})

    decision = _evaluate(risk, basket_cost_usd=basket_cost_usd, portfolio=portfolio)

    assert decision.allowed is allowed
    assert decision.reason == reason


def test_basket_limit_wins_before_cluster_concentration() -> None:
    """Checking cluster concentration first would hide the earlier basket refusal."""
    risk = _risk()
    portfolio = _portfolio(risk, open_exposure_usd_by_cluster={"event-a": Decimal("1000")})

    decision = _evaluate(risk, basket_cost_usd=Decimal("501"), portfolio=portfolio)

    assert decision.allowed is False
    assert decision.reason == "BASKET_TOO_LARGE"


@pytest.mark.parametrize(
    ("max_incomplete_loss_usd", "allowed", "reason"),
    [
        (Decimal("24.99"), True, None),
        (Decimal("25"), False, "INCOMPLETE_LOSS_TOO_LARGE"),
    ],
)
def test_incomplete_loss_is_strictly_below_the_quarter_percent_cap(
    max_incomplete_loss_usd: Decimal, allowed: bool, reason: str | None
) -> None:
    """Using <= would allow the prohibited 0.25% incomplete-loss boundary."""
    risk = _risk()

    decision = _evaluate(risk, max_incomplete_loss_usd=max_incomplete_loss_usd)

    assert decision.allowed is allowed
    assert decision.reason == reason


def test_cluster_limit_wins_before_incomplete_loss_limit() -> None:
    """Checking incomplete loss first would hide the earlier concentration refusal."""
    risk = _risk()
    portfolio = _portfolio(risk, open_exposure_usd_by_cluster={"event-a": Decimal("1000")})

    decision = _evaluate(
        risk,
        basket_cost_usd=Decimal("100"),
        max_incomplete_loss_usd=Decimal("25"),
        portfolio=portfolio,
    )

    assert decision.allowed is False
    assert decision.reason == "CLUSTER_CONCENTRATION"


def test_five_percent_peak_drawdown_halves_a_permitted_new_basket() -> None:
    """A missed 5% rule would retain full size after the review threshold."""
    risk = _risk()
    portfolio = _portfolio(
        risk,
        total_equity_usd=Decimal("9500"),
        peak_equity_usd=Decimal("10000"),
        equity_24h_ago_usd=Decimal("9500"),
    )

    decision = _evaluate(risk, portfolio=portfolio)

    assert decision.allowed is True
    assert decision.reason is None
    assert decision.size_multiplier == Decimal("0.5")
    assert decision.policy_version == "risk-v1"


def test_normal_risk_state_allows_full_size() -> None:
    """A gate that always halves or refuses cannot admit a normal basket."""
    risk = _risk()

    decision = _evaluate(risk)

    assert decision.allowed is True
    assert decision.reason is None
    assert decision.size_multiplier == Decimal("1")
    assert decision.policy_version == "risk-v1"
