from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import duckdb
import pytest
from pydantic import ValidationError

from polytrading.ledger.models import JournalPosting, JournalTransaction, TrialBalanceRow
from polytrading.storage.store import ConflictingRecordError, DuckDBStore
from tests.domain.factories import NOW


def journal_transaction(**overrides: object) -> JournalTransaction:
    values: dict[str, object] = {
        "schema_version": 1,
        "transaction_id": UUID("00000000-0000-0000-0000-000000000001"),
        "occurred_at": NOW,
        "observed_at": NOW,
        "description": "simulated funding",
        "postings": (
            JournalPosting(account="research:funding", asset="USD", debit=Decimal("1")),
            JournalPosting(account="research:cash", asset="USD", credit=Decimal("1")),
        ),
        "evidence_ids": ("funding:bybit:BTCUSDT:2026-08-12T12:00:00Z",),
    }
    values.update(overrides)
    return JournalTransaction(**values)


def test_unbalanced_transaction_is_rejected() -> None:
    with pytest.raises(ValidationError, match="debits must equal credits"):
        JournalTransaction(
            schema_version=1,
            transaction_id=UUID("00000000-0000-0000-0000-000000000001"),
            occurred_at=NOW,
            observed_at=NOW,
            description="simulated funding",
            postings=(
                JournalPosting(account="research:funding", asset="USD", debit=Decimal("1")),
                JournalPosting(account="research:cash", asset="USD", credit=Decimal("0.99")),
            ),
            evidence_ids=("funding:bybit:BTCUSDT:2026-08-12T12:00:00Z",),
        )


@pytest.mark.parametrize("payload_change", ["absent", "wrong"])
def test_journal_requires_schema_version_one(payload_change: str) -> None:
    payload = journal_transaction().model_dump()
    if payload_change == "absent":
        payload.pop("schema_version")
    else:
        payload["schema_version"] = 2

    with pytest.raises(ValidationError):
        JournalTransaction.model_validate(payload)


@pytest.mark.parametrize("side", ["debit", "credit"])
def test_journal_posting_rejects_values_below_duckdb_decimal_scale(side: str) -> None:
    with pytest.raises(ValidationError) as exception:
        JournalPosting(
            account="research:cash",
            asset="USD",
            **{side: Decimal("6E-19")},
        )

    assert exception.value.errors()[0]["type"] == "decimal_max_places"


def test_each_asset_must_balance_independently() -> None:
    with pytest.raises(ValidationError, match="debits must equal credits"):
        journal_transaction(
            postings=(
                JournalPosting(account="research:funding", asset="USD", debit=Decimal("1")),
                JournalPosting(account="research:cash", asset="BTC", credit=Decimal("1")),
            )
        )


@pytest.mark.parametrize(
    "posting",
    [
        JournalPosting.model_construct(
            account="a", asset="USD", debit=Decimal("0"), credit=Decimal("0")
        ),
        JournalPosting.model_construct(
            account="a", asset="USD", debit=Decimal("1"), credit=Decimal("1")
        ),
        JournalPosting.model_construct(
            account="a", asset="USD", debit=Decimal("-1"), credit=Decimal("0")
        ),
    ],
)
def test_posting_requires_exactly_one_positive_side(posting: JournalPosting) -> None:
    with pytest.raises(ValidationError, match="exactly one of debit or credit must be positive"):
        JournalPosting.model_validate(posting.model_dump())


def test_journal_requires_evidence_and_timezone_aware_timestamps() -> None:
    with pytest.raises(ValidationError, match="at least 1 item"):
        journal_transaction(evidence_ids=())
    with pytest.raises(ValidationError, match="timezone-aware"):
        journal_transaction(observed_at=datetime(2026, 8, 12, 12))


def test_journal_round_trip_is_atomic_idempotent_and_conflict_safe(tmp_path: Path) -> None:
    path = tmp_path / "research.duckdb"
    store = DuckDBStore(path)
    record = journal_transaction()

    assert store.append_journal_transaction(record) is True
    assert store.append_journal_transaction(record) is False
    with pytest.raises(ConflictingRecordError, match="conflicting journal transaction"):
        store.append_journal_transaction(journal_transaction(description="changed"))

    store.close()

    with duckdb.connect(str(path), read_only=True) as connection:
        assert connection.execute(
            "SELECT schema_version FROM journal_transactions WHERE transaction_id = ?",
            [record.transaction_id],
        ).fetchone() == (1,)


def test_trial_balance_is_exact_deterministic_and_excludes_future_transactions(
    tmp_path: Path,
) -> None:
    store = DuckDBStore(tmp_path / "research.duckdb")
    current = journal_transaction()
    future = journal_transaction(
        transaction_id=UUID("00000000-0000-0000-0000-000000000002"),
        occurred_at=NOW + timedelta(hours=1),
        observed_at=NOW + timedelta(hours=2),
        postings=(
            JournalPosting(account="research:fees", asset="BTC", debit=Decimal("0.1")),
            JournalPosting(account="research:cash", asset="BTC", credit=Decimal("0.1")),
        ),
        evidence_ids=("future-evidence",),
    )
    store.append_journal_transaction(current)
    store.append_journal_transaction(future)

    assert store.journal_trial_balance(NOW) == (
        TrialBalanceRow(
            asset="USD",
            account="research:cash",
            debit=Decimal("0"),
            credit=Decimal("1"),
            difference=Decimal("-1"),
        ),
        TrialBalanceRow(
            asset="USD",
            account="research:funding",
            debit=Decimal("1"),
            credit=Decimal("0"),
            difference=Decimal("1"),
        ),
    )
    assert sum((row.difference for row in store.journal_trial_balance(NOW)), Decimal(0)) == 0
    store.close()


def test_trial_balance_requires_explicit_timezone_aware_cutoff(tmp_path: Path) -> None:
    store = DuckDBStore(tmp_path / "research.duckdb")

    with pytest.raises(ValueError, match="timezone-aware"):
        store.journal_trial_balance(datetime(2026, 8, 12, 12))

    store.close()
