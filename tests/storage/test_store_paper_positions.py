from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from polytrading.carry.economics_models import FundingDirection
from polytrading.domain.models import Asset
from polytrading.ledger.models import JournalPosting, JournalTransaction
from polytrading.storage.store import ConflictingRecordError, DuckDBStore
from polytrading.trial.paper_execution import funding_accrual_transaction
from polytrading.trial.paper_models import (
    PAPER_RESEARCH_WARNING,
    PaperCloseReason,
    PaperPosition,
    PaperPositionClosure,
)
from tests.carry.test_economics_models import legacy_report_json, report


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


def _closure(position_id, **overrides: object) -> PaperPositionClosure:
    fields = {
        "schema_version": 1,
        "position_id": position_id,
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


def test_append_and_read_open_paper_position(tmp_path: Path) -> None:
    store = DuckDBStore(tmp_path / "test.duckdb")
    try:
        position = _position()
        assert store.append_paper_position(position) is True
        assert store.append_paper_position(position) is False  # idempotent retry
        assert store.open_paper_position_for_asset(Asset.BTC) == position
        assert store.open_paper_position_for_asset(Asset.ETH) is None
        assert store.paper_position(position.position_id) == position
    finally:
        store.close()


def test_append_paper_position_conflict_raises(tmp_path: Path) -> None:
    store = DuckDBStore(tmp_path / "test.duckdb")
    try:
        position = _position()
        store.append_paper_position(position)
        conflicting = _position(position_id=position.position_id, base_quantity=Decimal("0.6"))
        with pytest.raises(ConflictingRecordError):
            store.append_paper_position(conflicting)
    finally:
        store.close()


def test_closing_a_position_removes_it_from_open_lookup(tmp_path: Path) -> None:
    store = DuckDBStore(tmp_path / "test.duckdb")
    try:
        position = _position()
        store.append_paper_position(position)
        closure = _closure(position.position_id)
        assert store.append_paper_position_closure(closure) is True
        assert store.open_paper_position_for_asset(Asset.BTC) is None
        assert store.paper_position_closure(position.position_id) == closure
    finally:
        store.close()


def test_get_economic_evaluation_returns_stored_shadow_candidate(tmp_path: Path) -> None:
    store = DuckDBStore(tmp_path / "test.duckdb")
    try:
        item = report()
        store.append_economic_evaluation(item)
        assert store.get_economic_evaluation(item.evaluation_id) == item
        assert store.get_economic_evaluation(uuid4()) is None
    finally:
        store.close()


def test_get_economic_evaluation_treats_legacy_schema_as_not_found(tmp_path: Path) -> None:
    store = DuckDBStore(tmp_path / "test.duckdb")
    try:
        current_shape = report()
        payload = legacy_report_json(current_shape)
        store._connection.execute(
            """
            INSERT INTO economic_evaluations VALUES (?, ?, ?, ?, ?, ?, ?, ?::JSON, ?, ?)
            """,
            [
                current_shape.evaluation_id,
                current_shape.asset.value,
                current_shape.known_as_of,
                current_shape.evaluated_at,
                current_shape.decision.value,
                current_shape.direction.value,
                current_shape.policy_hash,
                payload,
                1,
                "9" * 64,
            ],
        )

        assert store.get_economic_evaluation(current_shape.evaluation_id) is None
    finally:
        store.close()


def test_paper_position_realized_funding_returns_zero_with_no_accruals(tmp_path: Path) -> None:
    store = DuckDBStore(tmp_path / "test.duckdb")
    try:
        position = _position()
        store.append_paper_position(position)
        assert store.paper_position_realized_funding(position.position_id) == Decimal(0)
        assert store.paper_position_realized_funding(uuid4()) == Decimal(0)
    finally:
        store.close()


def test_paper_position_realized_funding_sums_only_matching_accrual_postings(
    tmp_path: Path,
) -> None:
    store = DuckDBStore(tmp_path / "test.duckdb")
    try:
        position = _position()
        store.append_paper_position(position)

        # An unrelated transaction that also posts to paper:cash (e.g. the
        # position's own open transaction) must not be counted — only
        # transactions whose description marks them as funding accruals for
        # this position's asset should contribute.
        unrelated = JournalTransaction(
            schema_version=1,
            transaction_id=uuid4(),
            occurred_at=position.opened_at,
            observed_at=position.opened_at,
            description=f"paper open {position.asset.value} {position.direction.value}",
            postings=(
                JournalPosting(
                    account="paper:cash", asset="USD", debit=Decimal(0), credit=Decimal("100")
                ),
                JournalPosting(
                    account="paper:position:lighter",
                    asset="USD",
                    debit=Decimal("100"),
                    credit=Decimal(0),
                ),
            ),
            evidence_ids=("unrelated",),
        )
        store.append_journal_transaction(unrelated)

        first_accrual = funding_accrual_transaction(
            position=position,
            effective_at=position.opened_at + timedelta(hours=1),
            lighter_rate=Decimal("0.0001"),
            dydx_rate=Decimal("0.00005"),
        )
        second_accrual = funding_accrual_transaction(
            position=position,
            effective_at=position.opened_at + timedelta(hours=2),
            lighter_rate=Decimal("-0.0002"),
            dydx_rate=Decimal("0.0001"),
        )
        assert first_accrual is not None
        assert second_accrual is not None
        store.append_journal_transaction(first_accrual)
        store.append_journal_transaction(second_accrual)

        expected = Decimal(0)
        for transaction in (first_accrual, second_accrual):
            for posting in transaction.postings:
                if posting.account == "paper:cash":
                    expected += posting.debit - posting.credit

        assert expected != Decimal(0)
        assert store.paper_position_realized_funding(position.position_id) == expected
    finally:
        store.close()


def test_paper_position_realized_funding_does_not_leak_across_sequential_positions(
    tmp_path: Path,
) -> None:
    """A later position on the same asset must not inherit an earlier,
    already-closed position's funding accruals.

    Concurrently open positions on the same asset are forbidden, but
    sequential open/close cycles on the same asset are expected normal
    usage (see `open_paper_position_for_asset`). The realized-funding query
    previously scoped only by `asset`, so a fresh position with zero
    accruals of its own would incorrectly inherit an unrelated, earlier
    position's leftover funding.
    """
    store = DuckDBStore(tmp_path / "test.duckdb")
    try:
        position_a = _position()
        store.append_paper_position(position_a)

        accrual_a = funding_accrual_transaction(
            position=position_a,
            effective_at=position_a.opened_at + timedelta(hours=1),
            lighter_rate=Decimal("0.0001"),
            dydx_rate=Decimal("0.00005"),
        )
        assert accrual_a is not None
        store.append_journal_transaction(accrual_a)

        accrued_a = Decimal(0)
        for posting in accrual_a.postings:
            if posting.account == "paper:cash":
                accrued_a += posting.debit - posting.credit
        assert accrued_a != Decimal(0)
        assert store.paper_position_realized_funding(position_a.position_id) == accrued_a

        store.append_paper_position_closure(_closure(position_a.position_id))

        # Position B opens later on the *same* asset, with zero accruals of
        # its own.
        position_b = _position(
            position_id=uuid4(),
            opened_at=position_a.opened_at + timedelta(days=1),
        )
        store.append_paper_position(position_b)

        assert store.paper_position_realized_funding(position_b.position_id) == Decimal(0)
        # And position A's own total must be unaffected.
        assert store.paper_position_realized_funding(position_a.position_id) == accrued_a
    finally:
        store.close()


def test_paper_position_funding_accrued_reflects_posted_transactions(tmp_path: Path) -> None:
    store = DuckDBStore(tmp_path / "test.duckdb")
    try:
        position = _position()
        store.append_paper_position(position)
        effective_at = position.opened_at + timedelta(hours=1)

        assert store.paper_position_funding_accrued(position.position_id, effective_at) is False

        accrual = funding_accrual_transaction(
            position=position,
            effective_at=effective_at,
            lighter_rate=Decimal("0.0001"),
            dydx_rate=Decimal("0.00005"),
        )
        assert accrual is not None
        store.append_journal_transaction(accrual)

        assert store.paper_position_funding_accrued(position.position_id, effective_at) is True
        # A different hour for the same position is a distinct, unposted accrual.
        assert (
            store.paper_position_funding_accrued(
                position.position_id, effective_at + timedelta(hours=1)
            )
            is False
        )
    finally:
        store.close()


def test_paper_positions_as_of_returns_positions_opened_on_or_before_as_of(
    tmp_path: Path,
) -> None:
    store = DuckDBStore(tmp_path / "test.duckdb")
    try:
        early = _position(opened_at=datetime(2026, 8, 1, tzinfo=UTC))
        late = _position(
            position_id=uuid4(),
            asset=Asset.ETH,
            opened_at=datetime(2026, 8, 10, tzinfo=UTC),
        )
        store.append_paper_position(early)
        store.append_paper_position(late)

        assert store.paper_positions_as_of(datetime(2026, 7, 31, tzinfo=UTC)) == ()
        assert store.paper_positions_as_of(datetime(2026, 8, 1, tzinfo=UTC)) == (early,)
        assert store.paper_positions_as_of(datetime(2026, 8, 31, tzinfo=UTC)) == (
            early,
            late,
        )
    finally:
        store.close()


def test_paper_position_hourly_funding_returns_empty_with_no_accruals(tmp_path: Path) -> None:
    store = DuckDBStore(tmp_path / "test.duckdb")
    try:
        position = _position()
        store.append_paper_position(position)
        assert store.paper_position_hourly_funding(position.position_id) == ()
        assert store.paper_position_hourly_funding(uuid4()) == ()
    finally:
        store.close()


def test_paper_position_hourly_funding_returns_signed_deltas_in_order(tmp_path: Path) -> None:
    store = DuckDBStore(tmp_path / "test.duckdb")
    try:
        position = _position()
        store.append_paper_position(position)

        first_accrual = funding_accrual_transaction(
            position=position,
            effective_at=position.opened_at + timedelta(hours=1),
            lighter_rate=Decimal("0.0001"),
            dydx_rate=Decimal("0.00005"),
        )
        second_accrual = funding_accrual_transaction(
            position=position,
            effective_at=position.opened_at + timedelta(hours=2),
            lighter_rate=Decimal("-0.0002"),
            dydx_rate=Decimal("0.0001"),
        )
        assert first_accrual is not None
        assert second_accrual is not None
        store.append_journal_transaction(first_accrual)
        store.append_journal_transaction(second_accrual)

        expected = []
        for transaction in (first_accrual, second_accrual):
            for posting in transaction.postings:
                if posting.account == "paper:cash":
                    expected.append((transaction.occurred_at, posting.debit - posting.credit))

        assert store.paper_position_hourly_funding(position.position_id) == tuple(expected)
    finally:
        store.close()


def test_paper_position_hourly_funding_does_not_leak_across_sequential_positions(
    tmp_path: Path,
) -> None:
    """Mirrors the leak scenario covered for `paper_position_realized_funding`:
    a later position on the same asset must not inherit an earlier,
    already-closed position's hourly funding accruals.
    """
    store = DuckDBStore(tmp_path / "test.duckdb")
    try:
        position_a = _position()
        store.append_paper_position(position_a)

        accrual_a = funding_accrual_transaction(
            position=position_a,
            effective_at=position_a.opened_at + timedelta(hours=1),
            lighter_rate=Decimal("0.0001"),
            dydx_rate=Decimal("0.00005"),
        )
        assert accrual_a is not None
        store.append_journal_transaction(accrual_a)
        assert store.paper_position_hourly_funding(position_a.position_id) != ()

        store.append_paper_position_closure(_closure(position_a.position_id))

        position_b = _position(
            position_id=uuid4(),
            opened_at=position_a.opened_at + timedelta(days=1),
        )
        store.append_paper_position(position_b)

        assert store.paper_position_hourly_funding(position_b.position_id) == ()
        assert store.paper_position_hourly_funding(position_a.position_id) != ()
    finally:
        store.close()


def test_paper_position_funding_accrued_false_when_net_funding_is_zero(tmp_path: Path) -> None:
    """When funding nets to exactly zero, `funding_accrual_transaction` returns
    `None` and nothing is ever posted for that hour — the existence check must
    reflect that rather than assuming a transaction always follows.
    """
    store = DuckDBStore(tmp_path / "test.duckdb")
    try:
        position = _position()
        store.append_paper_position(position)
        effective_at = position.opened_at + timedelta(hours=1)

        accrual = funding_accrual_transaction(
            position=position,
            effective_at=effective_at,
            lighter_rate=Decimal("0"),
            dydx_rate=Decimal("0"),
        )
        assert accrual is None
        assert store.paper_position_funding_accrued(position.position_id, effective_at) is False
    finally:
        store.close()
