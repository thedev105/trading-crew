from __future__ import annotations

from decimal import Decimal
from typing import Annotated, ClassVar, Literal

from pydantic import Field, StringConstraints, model_validator

from polytrading.predictions.domain import PredictionRecord

NonEmptyString = Annotated[str, StringConstraints(min_length=1)]
PositiveFiniteDecimal = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
NonNegativeFiniteDecimal = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
PositiveFraction = Annotated[Decimal, Field(gt=0, le=1, allow_inf_nan=False)]
UnitIntervalDecimal = Annotated[Decimal, Field(ge=0, le=1, allow_inf_nan=False)]

RiskRefusalReason = Literal[
    "BASKET_TOO_LARGE",
    "CLUSTER_CONCENTRATION",
    "INCOMPLETE_LOSS_TOO_LARGE",
    "DRAWDOWN_HALT",
    "DRAWDOWN_STOP_ALL",
    "CAPITAL_PRESERVATION_MODE",
]


class PredictionRiskPolicy(PredictionRecord):
    """Immutable, conservative limits for independent shadow proposals."""

    policy_version: NonEmptyString
    max_basket_fraction_of_equity: PositiveFraction = Decimal("0.05")
    max_event_cluster_fraction: PositiveFraction = Decimal("0.10")
    max_incomplete_loss_fraction: PositiveFraction = Decimal("0.0025")
    drawdown_halt_new_entries: PositiveFraction = Decimal("0.02")
    drawdown_halve_size: PositiveFraction = Decimal("0.05")
    drawdown_stop_all: PositiveFraction = Decimal("0.08")
    drawdown_close_nonguaranteed: PositiveFraction = Decimal("0.12")
    drawdown_capital_preservation: PositiveFraction = Decimal("0.15")
    max_live_venues: Annotated[int, Field(ge=1)] = 2
    pilot_cap_usd: PositiveFiniteDecimal = Decimal("250")
    starting_equity_usd: PositiveFiniteDecimal = Decimal("10000")

    _MAXIMUM_BINDING_LIMITS: ClassVar[dict[str, Decimal | int]] = {
        "max_basket_fraction_of_equity": Decimal("0.05"),
        "max_event_cluster_fraction": Decimal("0.10"),
        "max_incomplete_loss_fraction": Decimal("0.0025"),
        "drawdown_halt_new_entries": Decimal("0.02"),
        "drawdown_halve_size": Decimal("0.05"),
        "drawdown_stop_all": Decimal("0.08"),
        "drawdown_close_nonguaranteed": Decimal("0.12"),
        "drawdown_capital_preservation": Decimal("0.15"),
        "max_live_venues": 2,
        "pilot_cap_usd": Decimal("250"),
    }

    @model_validator(mode="after")
    def _require_ordered_drawdown_limits(self) -> PredictionRiskPolicy:
        for field_name, maximum in self._MAXIMUM_BINDING_LIMITS.items():
            if getattr(self, field_name) > maximum:
                raise ValueError(f"{field_name} cannot be less conservative than the default limit")
        if not (
            self.drawdown_halt_new_entries
            < self.drawdown_halve_size
            < self.drawdown_stop_all
            < self.drawdown_close_nonguaranteed
            < self.drawdown_capital_preservation
        ):
            raise ValueError("drawdown limits must be strictly ordered from halt to preservation")
        return self


DEFAULT_RISK_POLICY = PredictionRiskPolicy(policy_version="shadow-risk-v1")


class _FrozenExposureByCluster(dict[str, Decimal]):
    """A serializable mapping that cannot be mutated after portfolio validation."""

    def __init__(self, values: dict[str, Decimal]) -> None:
        dict.__init__(self, values)

    @staticmethod
    def _immutable(*args: object, **kwargs: object) -> None:
        raise TypeError("open_exposure_usd_by_cluster is immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    __ior__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable


class ShadowPortfolioState(PredictionRecord):
    """A validated snapshot used exclusively for pure risk-gate evaluation."""

    total_equity_usd: PositiveFiniteDecimal
    open_exposure_usd_by_cluster: dict[str, NonNegativeFiniteDecimal]
    peak_equity_usd: PositiveFiniteDecimal
    equity_24h_ago_usd: PositiveFiniteDecimal
    open_proposal_count: Annotated[int, Field(ge=0)]

    @model_validator(mode="after")
    def _require_peak_to_include_current_equity(self) -> ShadowPortfolioState:
        if self.peak_equity_usd < self.total_equity_usd:
            raise ValueError("peak_equity_usd must be at least total_equity_usd")
        object.__setattr__(
            self,
            "open_exposure_usd_by_cluster",
            _FrozenExposureByCluster(dict(self.open_exposure_usd_by_cluster)),
        )
        return self


class RiskGateDecision(PredictionRecord):
    allowed: bool
    reason: RiskRefusalReason | None
    size_multiplier: UnitIntervalDecimal
    policy_version: str


def evaluate_risk_gate(
    *,
    basket_cost_usd: NonNegativeFiniteDecimal,
    max_incomplete_loss_usd: NonNegativeFiniteDecimal,
    event_cluster_id: str,
    portfolio: ShadowPortfolioState,
    policy: PredictionRiskPolicy,
) -> RiskGateDecision:
    """Return a deterministic refusal or permitted size under the frozen policy.

    Check ordering is intentionally explicit: it makes the reported reason stable when a
    portfolio breaches more than one limit simultaneously.
    """
    _require_nonnegative_finite_decimal(basket_cost_usd, "basket_cost_usd")
    _require_nonnegative_finite_decimal(max_incomplete_loss_usd, "max_incomplete_loss_usd")

    peak_drawdown = (
        portfolio.peak_equity_usd - portfolio.total_equity_usd
    ) / portfolio.peak_equity_usd
    daily_loss = (
        portfolio.equity_24h_ago_usd - portfolio.total_equity_usd
    ) / portfolio.equity_24h_ago_usd
    size_multiplier = (
        Decimal("0.5") if peak_drawdown >= policy.drawdown_halve_size else Decimal("1")
    )

    if peak_drawdown >= policy.drawdown_capital_preservation:
        return _refused("CAPITAL_PRESERVATION_MODE", size_multiplier, policy)
    if peak_drawdown >= policy.drawdown_stop_all:
        return _refused("DRAWDOWN_STOP_ALL", size_multiplier, policy)
    if daily_loss >= policy.drawdown_halt_new_entries:
        return _refused("DRAWDOWN_HALT", size_multiplier, policy)
    if basket_cost_usd > portfolio.total_equity_usd * policy.max_basket_fraction_of_equity:
        return _refused("BASKET_TOO_LARGE", size_multiplier, policy)

    cluster_exposure = portfolio.open_exposure_usd_by_cluster.get(event_cluster_id, Decimal("0"))
    if (
        cluster_exposure + basket_cost_usd
        > portfolio.total_equity_usd * policy.max_event_cluster_fraction
    ):
        return _refused("CLUSTER_CONCENTRATION", size_multiplier, policy)
    if max_incomplete_loss_usd >= portfolio.total_equity_usd * policy.max_incomplete_loss_fraction:
        return _refused("INCOMPLETE_LOSS_TOO_LARGE", size_multiplier, policy)
    return RiskGateDecision(
        allowed=True,
        reason=None,
        size_multiplier=size_multiplier,
        policy_version=policy.policy_version,
    )


def _refused(
    reason: RiskRefusalReason,
    size_multiplier: Decimal,
    policy: PredictionRiskPolicy,
) -> RiskGateDecision:
    return RiskGateDecision(
        allowed=False,
        reason=reason,
        size_multiplier=size_multiplier,
        policy_version=policy.policy_version,
    )


def _require_nonnegative_finite_decimal(value: object, name: str) -> None:
    if not isinstance(value, Decimal):
        raise TypeError(f"{name} must be a Decimal")
    if not value.is_finite() or value < 0:
        raise ValueError(f"{name} must be a finite non-negative Decimal")
