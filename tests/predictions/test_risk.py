from __future__ import annotations

import copy
import importlib
import json
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


def test_portfolio_exposure_mapping_is_immutable_and_copies_caller_data() -> None:
    """Mutable or copied cluster maps could bypass concentration checks after validation."""
    risk = _risk()
    caller_exposure = {"event-a": Decimal("100")}

    portfolio = _portfolio(risk, open_exposure_usd_by_cluster=caller_exposure)
    caller_exposure["event-a"] = Decimal("999")

    assert portfolio.open_exposure_usd_by_cluster == {"event-a": Decimal("100")}
    assert portfolio.model_dump()["open_exposure_usd_by_cluster"] == {"event-a": Decimal("100")}
    assert json.loads(portfolio.model_dump_json())["open_exposure_usd_by_cluster"] == {
        "event-a": "100"
    }
    with pytest.raises(TypeError):
        portfolio.open_exposure_usd_by_cluster["event-a"] = Decimal("999")
    with pytest.raises(TypeError):
        dict.__setitem__(portfolio.open_exposure_usd_by_cluster, "event-a", Decimal("999"))
    assert (
        copy.copy(portfolio.open_exposure_usd_by_cluster) is portfolio.open_exposure_usd_by_cluster
    )
    assert (
        copy.deepcopy(portfolio.open_exposure_usd_by_cluster)
        is portfolio.open_exposure_usd_by_cluster
    )
    assert (
        portfolio.model_copy().open_exposure_usd_by_cluster
        is portfolio.open_exposure_usd_by_cluster
    )
    assert (
        portfolio.model_copy(deep=True).open_exposure_usd_by_cluster
        is portfolio.open_exposure_usd_by_cluster
    )


def test_policy_is_frozen_so_strategy_cannot_raise_a_limit() -> None:
    """A mutable policy would let strategy code bypass a conservative risk gate."""
    risk = _risk()
    policy = _policy(risk)

    with pytest.raises(ValidationError):
        policy.max_basket_fraction_of_equity = Decimal("1")


@pytest.mark.parametrize(
    ("field", "more_permissive", "stricter"),
    [
        (
            "max_basket_fraction_of_equity",
            Decimal("0.0501"),
            Decimal("0.0499"),
        ),
        ("max_live_venues", 3, 1),
        ("pilot_cap_usd", Decimal("250.01"), Decimal("249.99")),
    ],
)
def test_policy_rejects_relaxed_limits_but_accepts_stricter_ones(
    field: str, more_permissive: object, stricter: object
) -> None:
    """Constructing a relaxed policy would let strategy code raise a binding limit."""
    risk = _risk()

    with pytest.raises(ValidationError):
        _policy(risk, **{field: more_permissive})

    policy = _policy(risk, **{field: stricter})

    assert getattr(policy, field) == stricter


@pytest.mark.parametrize("copy_method", ["model_copy", "model_construct"])
def test_gate_rejects_relaxed_unchecked_policy(copy_method: str) -> None:
    """Using unchecked Pydantic construction must not admit a 6% basket under a 5% policy."""
    risk = _risk()
    safe_policy = _policy(risk)
    values = safe_policy.model_dump() | {"max_basket_fraction_of_equity": Decimal("0.50")}
    unchecked_policy = (
        safe_policy.model_copy(update=values)
        if copy_method == "model_copy"
        else risk.PredictionRiskPolicy.model_construct(**values)
    )

    with pytest.raises(ValidationError):
        _evaluate(risk, basket_cost_usd=Decimal("600"), policy=unchecked_policy)


def test_gate_rejects_invalid_unchecked_portfolio_mapping() -> None:
    """Unchecked negative cluster exposure must be rejected before it affects the gate."""
    risk = _risk()
    portfolio = _portfolio(risk).model_copy(
        update={"open_exposure_usd_by_cluster": {"event-a": Decimal("-1")}}
    )

    with pytest.raises(ValidationError):
        _evaluate(risk, portfolio=portfolio)


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


@pytest.mark.parametrize("invalid", [Decimal("NaN"), Decimal("-0.01"), Decimal("1.01")])
def test_risk_decision_rejects_invalid_size_multiplier(invalid: Decimal) -> None:
    """An invalid multiplier could create a negative or enlarged shadow order."""
    risk = _risk()

    with pytest.raises(ValidationError):
        risk.RiskGateDecision(
            allowed=True,
            reason=None,
            size_multiplier=invalid,
            policy_version="risk-v1",
        )
