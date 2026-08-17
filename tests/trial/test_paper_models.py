from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from polytrading.carry.economics_models import FundingDirection
from polytrading.domain.models import Asset
from polytrading.trial.paper_models import (
    PAPER_RESEARCH_WARNING,
    PaperCloseReason,
    PaperPosition,
    PaperPositionClosure,
)


def _position(**overrides: object) -> PaperPosition:
    fields = {
        "schema_version": 1,
        "position_id": uuid4(),
        "source_evaluation_id": uuid4(),
        "asset": Asset.BTC,
        "direction": FundingDirection.SHORT_LIGHTER_LONG_DYDX,
        "opened_at": datetime(2026, 8, 17, 12, 0, tzinfo=UTC),
        "base_quantity": Decimal("0.5"),
        "lighter_entry_notional_usd": Decimal("30000"),
        "dydx_entry_notional_usd": Decimal("30010"),
        "lighter_entry_price": Decimal("60000"),
        "dydx_entry_price": Decimal("60020"),
        "opening_book_cycle_id": uuid4(),
        "warning": PAPER_RESEARCH_WARNING,
    }
    fields.update(overrides)
    return PaperPosition(**fields)


def test_paper_position_accepts_valid_fields() -> None:
    position = _position()
    assert position.asset is Asset.BTC
    assert position.warning == PAPER_RESEARCH_WARNING


def test_paper_position_rejects_naive_opened_at() -> None:
    with pytest.raises(ValidationError):
        _position(opened_at=datetime(2026, 8, 17, 12, 0))


@pytest.mark.parametrize(
    "field",
    ["base_quantity", "lighter_entry_notional_usd", "dydx_entry_notional_usd"],
)
def test_paper_position_rejects_nonpositive_economics(field: str) -> None:
    with pytest.raises(ValidationError):
        _position(**{field: Decimal("0")})


def test_paper_position_rejects_wrong_warning_text() -> None:
    with pytest.raises(ValidationError):
        _position(warning="not the frozen warning")


def _closure(**overrides: object) -> PaperPositionClosure:
    fields = {
        "schema_version": 1,
        "position_id": uuid4(),
        "closed_at": datetime(2026, 8, 20, 12, 0, tzinfo=UTC),
        "close_reason": PaperCloseReason.MAX_HORIZON_REACHED,
        "lighter_exit_notional_usd": Decimal("29500"),
        "dydx_exit_notional_usd": Decimal("29600"),
        "lighter_exit_price": Decimal("59000"),
        "dydx_exit_price": Decimal("59200"),
        "closing_book_cycle_id": uuid4(),
        "realized_funding_usd": Decimal("120.50"),
        "realized_pnl_usd": Decimal("-45.25"),
    }
    fields.update(overrides)
    return PaperPositionClosure(**fields)


def test_paper_position_closure_accepts_negative_realized_pnl() -> None:
    closure = _closure()
    assert closure.realized_pnl_usd == Decimal("-45.25")


def test_paper_position_closure_rejects_nonpositive_exit_notional() -> None:
    with pytest.raises(ValidationError):
        _closure(lighter_exit_notional_usd=Decimal("0"))
