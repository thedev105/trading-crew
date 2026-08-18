from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from polytrading.carry.economics_execution import PairedBookObservation
from polytrading.carry.economics_models import EconomicsDecision, FundingDirection
from polytrading.domain.models import Asset, BookLevel, InstrumentKind, Level2BookSnapshot, Venue
from polytrading.trial.paper_execution import (
    PaperOpenRejected,
    close_paper_position,
    current_regime_reversed,
    funding_accrual_transaction,
    open_paper_position,
)
from polytrading.trial.paper_models import PaperCloseReason
from tests.carry.test_economics_models import _report  # reuse the shared report factory


def _book(venue: Venue, bid: str, ask: str, cycle_id) -> Level2BookSnapshot:
    return Level2BookSnapshot(
        schema_version=1,
        cycle_id=cycle_id,
        venue=venue,
        symbol="BTC-USD" if venue is Venue.DYDX else "BTC",
        asset=Asset.BTC,
        bids=(BookLevel(price=Decimal(bid), quantity=Decimal("10"), order_count=None),),
        asks=(BookLevel(price=Decimal(ask), quantity=Decimal("10"), order_count=None),),
        depth_limit=20,
        sequence=None,
        effective_at=datetime(2026, 8, 17, 12, 0, tzinfo=UTC),
        observed_at=datetime(2026, 8, 17, 12, 0, tzinfo=UTC),
        source_hash="a" * 64,
    )


def _instrument(venue: Venue):
    from polytrading.domain.models import InstrumentSpec

    return InstrumentSpec(
        schema_version=1,
        instrument_id=f"{venue.value}-btc",
        venue=venue,
        symbol="BTC-USD" if venue is Venue.DYDX else "BTC",
        asset=Asset.BTC,
        kind=InstrumentKind.LINEAR_PERPETUAL,
        contract_multiplier=Decimal("1"),
        index_family=None,
        oracle_family=None,
        mark_method=None,
        liquidation_method=None,
        collateral_asset=None,
        pnl_asset=None,
        funding_formula_id=None,
        funding_cap=None,
        funding_interval_hours=Decimal("1"),
        funding_payment_offset_minutes=None,
        min_notional=Decimal("10"),
        quantity_step=Decimal("0.001"),
        price_tick=Decimal("0.1"),
        is_inverse=False,
        is_prelaunch=False,
        observed_at=datetime(2026, 8, 17, 12, 0, tzinfo=UTC),
        source_hash="b" * 64,
    )


def test_current_regime_reversed_true_when_median_nonpositive() -> None:
    assert current_regime_reversed((Decimal("0.0001"), Decimal("-0.0002"))) is True


def test_current_regime_reversed_false_when_median_positive() -> None:
    assert current_regime_reversed((Decimal("0.0001"), Decimal("0.0002"))) is False


def test_open_paper_position_walks_current_book_at_frozen_quantity() -> None:
    report = _report(
        decision=EconomicsDecision.SHADOW_CANDIDATE,
        direction=FundingDirection.SHORT_LIGHTER_LONG_DYDX,
        base_quantity=Decimal("1"),
    )
    cycle_id = uuid4()
    books = PairedBookObservation(
        effective_at=datetime(2026, 8, 17, 12, 0, tzinfo=UTC),
        lighter=_book(Venue.LIGHTER, "60000", "60010", cycle_id),
        dydx=_book(Venue.DYDX, "60005", "60015", cycle_id),
    )
    position, transaction = open_paper_position(
        report=report,
        current_books=books,
        lighter_instrument=_instrument(Venue.LIGHTER),
        dydx_instrument=_instrument(Venue.DYDX),
        position_id=uuid4(),
        opening_book_cycle_id=cycle_id,
        opened_at=datetime(2026, 8, 17, 12, 0, tzinfo=UTC),
    )
    # SHORT_LIGHTER_LONG_DYDX: short Lighter sells into Lighter bids (60000),
    # long dYdX buys from dYdX asks (60015).
    assert position.lighter_entry_price == Decimal("60000")
    assert position.dydx_entry_price == Decimal("60015")
    assert len(transaction.postings) == 4  # debit+credit pair for each of the two legs
    assert sum(p.debit for p in transaction.postings) == sum(p.credit for p in transaction.postings)


def test_open_paper_position_rejects_insufficient_depth() -> None:
    report = _report(
        decision=EconomicsDecision.SHADOW_CANDIDATE,
        direction=FundingDirection.SHORT_LIGHTER_LONG_DYDX,
        base_quantity=Decimal("1000"),  # exceeds the depth in `_book`
    )
    cycle_id = uuid4()
    books = PairedBookObservation(
        effective_at=datetime(2026, 8, 17, 12, 0, tzinfo=UTC),
        lighter=_book(Venue.LIGHTER, "60000", "60010", cycle_id),
        dydx=_book(Venue.DYDX, "60005", "60015", cycle_id),
    )
    with pytest.raises(PaperOpenRejected):
        open_paper_position(
            report=report,
            current_books=books,
            lighter_instrument=_instrument(Venue.LIGHTER),
            dydx_instrument=_instrument(Venue.DYDX),
            position_id=uuid4(),
            opening_book_cycle_id=cycle_id,
            opened_at=datetime(2026, 8, 17, 12, 0, tzinfo=UTC),
        )


def test_open_paper_position_rejects_non_shadow_candidate() -> None:
    report = _report(decision=EconomicsDecision.REJECTED, direction=None)
    cycle_id = uuid4()
    books = PairedBookObservation(
        effective_at=datetime(2026, 8, 17, 12, 0, tzinfo=UTC),
        lighter=_book(Venue.LIGHTER, "60000", "60010", cycle_id),
        dydx=_book(Venue.DYDX, "60005", "60015", cycle_id),
    )
    with pytest.raises(PaperOpenRejected):
        open_paper_position(
            report=report,
            current_books=books,
            lighter_instrument=_instrument(Venue.LIGHTER),
            dydx_instrument=_instrument(Venue.DYDX),
            position_id=uuid4(),
            opening_book_cycle_id=cycle_id,
            opened_at=datetime(2026, 8, 17, 12, 0, tzinfo=UTC),
        )


def test_funding_accrual_transaction_is_balanced() -> None:
    report = _report(
        decision=EconomicsDecision.SHADOW_CANDIDATE,
        direction=FundingDirection.SHORT_LIGHTER_LONG_DYDX,
        base_quantity=Decimal("1"),
    )
    cycle_id = uuid4()
    books = PairedBookObservation(
        effective_at=datetime(2026, 8, 17, 12, 0, tzinfo=UTC),
        lighter=_book(Venue.LIGHTER, "60000", "60010", cycle_id),
        dydx=_book(Venue.DYDX, "60005", "60015", cycle_id),
    )
    position, _ = open_paper_position(
        report=report,
        current_books=books,
        lighter_instrument=_instrument(Venue.LIGHTER),
        dydx_instrument=_instrument(Venue.DYDX),
        position_id=uuid4(),
        opening_book_cycle_id=cycle_id,
        opened_at=datetime(2026, 8, 17, 12, 0, tzinfo=UTC),
    )
    transaction = funding_accrual_transaction(
        position=position,
        effective_at=datetime(2026, 8, 17, 13, 0, tzinfo=UTC),
        lighter_rate=Decimal("0.0001"),
        dydx_rate=Decimal("-0.00005"),
    )
    assert transaction is not None
    assert sum(p.debit for p in transaction.postings) == sum(p.credit for p in transaction.postings)


def test_funding_accrual_transaction_is_none_when_net_is_exactly_zero() -> None:
    report = _report(
        decision=EconomicsDecision.SHADOW_CANDIDATE,
        direction=FundingDirection.SHORT_LIGHTER_LONG_DYDX,
        base_quantity=Decimal("1"),
    )
    cycle_id = uuid4()
    # SHORT_LIGHTER_LONG_DYDX entry uses the Lighter bid and the dYdX ask; pick a
    # (non-crossed) book on each venue where those two prices are equal, so the
    # two legs' entry notionals are equal and equal-magnitude rates cancel exactly.
    books = PairedBookObservation(
        effective_at=datetime(2026, 8, 17, 12, 0, tzinfo=UTC),
        lighter=_book(Venue.LIGHTER, "60000", "60010", cycle_id),
        dydx=_book(Venue.DYDX, "59990", "60000", cycle_id),
    )
    position, _ = open_paper_position(
        report=report,
        current_books=books,
        lighter_instrument=_instrument(Venue.LIGHTER),
        dydx_instrument=_instrument(Venue.DYDX),
        position_id=uuid4(),
        opening_book_cycle_id=cycle_id,
        opened_at=datetime(2026, 8, 17, 12, 0, tzinfo=UTC),
    )
    # equal notionals and equal-magnitude opposite rates net to exactly zero
    transaction = funding_accrual_transaction(
        position=position,
        effective_at=datetime(2026, 8, 17, 13, 0, tzinfo=UTC),
        lighter_rate=Decimal("0.0001"),
        dydx_rate=Decimal("0.0001"),
    )
    assert transaction is None


def test_close_paper_position_realized_pnl_matches_signed_leg_pnl() -> None:
    report = _report(
        decision=EconomicsDecision.SHADOW_CANDIDATE,
        direction=FundingDirection.SHORT_LIGHTER_LONG_DYDX,
        base_quantity=Decimal("1"),
    )
    open_cycle_id = uuid4()
    open_books = PairedBookObservation(
        effective_at=datetime(2026, 8, 17, 12, 0, tzinfo=UTC),
        lighter=_book(Venue.LIGHTER, "60000", "60010", open_cycle_id),
        dydx=_book(Venue.DYDX, "60005", "60015", open_cycle_id),
    )
    position, _ = open_paper_position(
        report=report,
        current_books=open_books,
        lighter_instrument=_instrument(Venue.LIGHTER),
        dydx_instrument=_instrument(Venue.DYDX),
        position_id=uuid4(),
        opening_book_cycle_id=open_cycle_id,
        opened_at=datetime(2026, 8, 17, 12, 0, tzinfo=UTC),
    )
    close_cycle_id = uuid4()
    # Lighter (short leg) price falls -> short profits. dYdX (long leg) price falls -> long loses.
    close_books = PairedBookObservation(
        effective_at=datetime(2026, 8, 18, 12, 0, tzinfo=UTC),
        lighter=_book(Venue.LIGHTER, "59000", "59010", close_cycle_id),
        dydx=_book(Venue.DYDX, "59005", "59015", close_cycle_id),
    )
    closure, transaction = close_paper_position(
        position=position,
        current_books=close_books,
        lighter_instrument=_instrument(Venue.LIGHTER),
        dydx_instrument=_instrument(Venue.DYDX),
        closing_book_cycle_id=close_cycle_id,
        closed_at=datetime(2026, 8, 18, 12, 0, tzinfo=UTC),
        close_reason=PaperCloseReason.MAX_HORIZON_REACHED,
        realized_funding_usd=Decimal("10"),
    )
    # short Lighter: entry(bid 60000) - exit(ask 59010) = 990 profit on the short leg
    # long dYdX: exit(bid 59005) - entry(ask 60015) = -1010 loss on the long leg
    expected_trading_pnl = Decimal("60000") - Decimal("59010") + (
        Decimal("59005") - Decimal("60015")
    )
    assert closure.realized_pnl_usd == expected_trading_pnl + Decimal("10")
    assert sum(p.debit for p in transaction.postings) == sum(p.credit for p in transaction.postings)


def test_open_accrue_close_cycle_reconciles_via_journal_trial_balance(tmp_path) -> None:
    from datetime import timedelta

    from polytrading.storage.store import DuckDBStore

    store = DuckDBStore(tmp_path / "test.duckdb")
    try:
        report = _report(
            decision=EconomicsDecision.SHADOW_CANDIDATE,
            direction=FundingDirection.SHORT_LIGHTER_LONG_DYDX,
            base_quantity=Decimal("1"),
        )
        open_cycle_id = uuid4()
        opened_at = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
        open_books = PairedBookObservation(
            effective_at=opened_at,
            lighter=_book(Venue.LIGHTER, "60000", "60010", open_cycle_id),
            dydx=_book(Venue.DYDX, "60005", "60015", open_cycle_id),
        )
        position, open_txn = open_paper_position(
            report=report,
            current_books=open_books,
            lighter_instrument=_instrument(Venue.LIGHTER),
            dydx_instrument=_instrument(Venue.DYDX),
            position_id=uuid4(),
            opening_book_cycle_id=open_cycle_id,
            opened_at=opened_at,
        )
        store.append_paper_position(position)
        store.append_journal_transaction(open_txn)

        accrual_txn = funding_accrual_transaction(
            position=position,
            effective_at=opened_at + timedelta(hours=1),
            lighter_rate=Decimal("0.0001"),
            dydx_rate=Decimal("-0.00005"),
        )
        assert accrual_txn is not None
        store.append_journal_transaction(accrual_txn)
        # `debit - credit` on a paper:pnl:* account is the mirror of its P&L
        # contribution (mirroring is what keeps the transaction balanced); flip
        # it back here to get the actual signed dollar funding P&L to feed into
        # close_paper_position's realized_funding_usd (positive == gain).
        realized_funding = sum(
            (p.credit - p.debit) for p in accrual_txn.postings if p.account == "paper:pnl:funding"
        )

        close_cycle_id = uuid4()
        closed_at = opened_at + timedelta(days=1)
        close_books = PairedBookObservation(
            effective_at=closed_at,
            lighter=_book(Venue.LIGHTER, "59000", "59010", close_cycle_id),
            dydx=_book(Venue.DYDX, "59005", "59015", close_cycle_id),
        )
        closure, close_txn = close_paper_position(
            position=position,
            current_books=close_books,
            lighter_instrument=_instrument(Venue.LIGHTER),
            dydx_instrument=_instrument(Venue.DYDX),
            closing_book_cycle_id=close_cycle_id,
            closed_at=closed_at,
            close_reason=PaperCloseReason.MAX_HORIZON_REACHED,
            realized_funding_usd=realized_funding,
        )
        store.append_paper_position_closure(closure)
        store.append_journal_transaction(close_txn)

        trial_balance = store.journal_trial_balance(closed_at)
        by_account = {row.account: row.debit - row.credit for row in trial_balance}
        # trial balance is a debit/credit ledger: total across all accounts always nets to zero
        assert sum(by_account.values()) == Decimal(0)
        # every account touched by this position's lifecycle must be fully wound down to
        # zero except the two P&L accounts, which together must equal realized P&L negated
        # (a *_row.debit - credit* convention on paper:pnl:* is the mirror of the P&L sign)
        assert by_account.get("paper:position:lighter", Decimal(0)) == Decimal(0)
        assert by_account.get("paper:position:dydx", Decimal(0)) == Decimal(0)
        pnl_accounts_total = by_account.get("paper:pnl:funding", Decimal(0)) + by_account.get(
            "paper:pnl:trading", Decimal(0)
        )
        assert -pnl_accounts_total == closure.realized_pnl_usd
    finally:
        store.close()
