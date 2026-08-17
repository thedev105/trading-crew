# Lighter–dYdX Forward Paper-Execution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the forward paper-execution gate — simulated taker-only positions opened from a persisted `SHADOW_CANDIDATE` report, monitored hourly for automatic close, fully ledger-reconciled, and shown on a new read-only dashboard section.

**Architecture:** New `trial/paper_models.py` (typed records) → `trial/paper_execution.py` (pure open/close/accrual/regime functions, reusing `carry/economics_execution.py`'s book-walk primitives and `carry/economics_funding.py`'s direction/median math) → `storage/store.py` additions (append-only `paper_positions`/`paper_position_closures` tables plus the existing `journal_transactions` tables) → `cli.py` (`trial paper open|close|monitor`) → dashboard (`web/models.py`, `web/dashboard.py`, `web/assets/*`).

**Tech Stack:** Python 3.12+, Pydantic v2 (`StrictRecord`), DuckDB, pytest/hypothesis, no new dependencies.

**Spec:** `docs/superpowers/specs/2026-08-17-lighter-dydx-forward-paper-execution-design.md`

## Global Constraints

- No venue credential, wallet, custody, or live-order dependency may enter the dependency graph — every simulated fill walks an already-stored `Level2BookSnapshot`.
- Every mutating CLI command requires `--confirm`; omitting it must print the would-be effect and write nothing.
- Every DB write is one DuckDB transaction (position + closure + ledger transaction succeed or fail together), following `DuckDBStore.transaction()` / `_normalized_retry` conventions already used by `_append_economic_evaluation`.
- Reuse, never reimplement: `walk_book`, `InsufficientDepthError`, `PairedBookObservation`, `entry_slippage_cost`, `forced_exit_cost` from `carry/economics_execution.py`; `select_direction`, `orient_funding`, `exact_median` from `carry/economics_funding.py`; `select_prospective_funding` from `trial/funding_lineage.py`; `eligible_lighter_dydx_book_pair`/`EligibleTrialBookPair` from `trial/book_evidence.py`; `JournalTransaction`/`JournalPosting` from `ledger/models.py` unchanged.
- One open position per asset at a time; a position is open iff it has a `paper_positions` row with no matching `paper_position_closures` row.
- All money math is `Decimal`, never float. All timestamps pass through `normalize_utc_timestamp`.

---

### Task 1: Paper position domain models

**Files:**
- Create: `src/polytrading/trial/paper_models.py`
- Test: `tests/trial/test_paper_models.py`

**Interfaces:**
- Produces: `PaperCloseReason` (StrEnum: `REGIME_REVERSED`, `MAX_HORIZON_REACHED`, `OPERATOR_CLOSED`), `PaperPosition` (StrictRecord), `PaperPositionClosure` (StrictRecord), `PAPER_RESEARCH_WARNING: str`.

- [ ] **Step 1: Write the failing test**

```python
# tests/trial/test_paper_models.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/trial/test_paper_models.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'polytrading.trial.paper_models'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/polytrading/trial/paper_models.py
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import field_validator, model_validator

from polytrading.carry.economics_models import FundingDirection
from polytrading.domain.models import Asset, Decimal38x18, StrictRecord, normalize_utc_timestamp

PAPER_RESEARCH_WARNING = (
    "Research only — simulated paper position, not a live fill or trading authorization."
)


class PaperCloseReason(StrEnum):
    REGIME_REVERSED = "REGIME_REVERSED"
    MAX_HORIZON_REACHED = "MAX_HORIZON_REACHED"
    OPERATOR_CLOSED = "OPERATOR_CLOSED"


class PaperPosition(StrictRecord):
    schema_version: Literal[1]
    position_id: UUID
    source_evaluation_id: UUID
    asset: Asset
    direction: FundingDirection
    opened_at: datetime
    base_quantity: Decimal38x18
    lighter_entry_notional_usd: Decimal38x18
    dydx_entry_notional_usd: Decimal38x18
    lighter_entry_price: Decimal38x18
    dydx_entry_price: Decimal38x18
    opening_book_cycle_id: UUID
    warning: Literal[
        "Research only — simulated paper position, not a live fill or trading authorization."
    ]

    @field_validator("opened_at")
    @classmethod
    def require_utc_opened_at(cls, value: datetime) -> datetime:
        return normalize_utc_timestamp(value)

    @model_validator(mode="after")
    def require_positive_economics(self) -> PaperPosition:
        for value, label in (
            (self.base_quantity, "base quantity"),
            (self.lighter_entry_notional_usd, "Lighter entry notional"),
            (self.dydx_entry_notional_usd, "dYdX entry notional"),
            (self.lighter_entry_price, "Lighter entry price"),
            (self.dydx_entry_price, "dYdX entry price"),
        ):
            if value <= 0:
                raise ValueError(f"{label} must be positive")
        return self


class PaperPositionClosure(StrictRecord):
    schema_version: Literal[1]
    position_id: UUID
    closed_at: datetime
    close_reason: PaperCloseReason
    lighter_exit_notional_usd: Decimal38x18
    dydx_exit_notional_usd: Decimal38x18
    lighter_exit_price: Decimal38x18
    dydx_exit_price: Decimal38x18
    closing_book_cycle_id: UUID
    realized_funding_usd: Decimal38x18
    realized_pnl_usd: Decimal38x18

    @field_validator("closed_at")
    @classmethod
    def require_utc_closed_at(cls, value: datetime) -> datetime:
        return normalize_utc_timestamp(value)

    @model_validator(mode="after")
    def require_positive_exit_economics(self) -> PaperPositionClosure:
        for value, label in (
            (self.lighter_exit_notional_usd, "Lighter exit notional"),
            (self.dydx_exit_notional_usd, "dYdX exit notional"),
            (self.lighter_exit_price, "Lighter exit price"),
            (self.dydx_exit_price, "dYdX exit price"),
        ):
            if value <= 0:
                raise ValueError(f"{label} must be positive")
        return self
```

Note: `Decimal38x18` (from `domain/models.py`) forbids non-finite values but allows negative ones, so `realized_funding_usd`/`realized_pnl_usd` can be negative while `base_quantity`/notionals/prices are separately constrained positive above.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/trial/test_paper_models.py -v`
Expected: PASS (8 tests)

- [ ] **Step 5: Commit**

```bash
git add src/polytrading/trial/paper_models.py tests/trial/test_paper_models.py
git commit -m "feat(trial): add paper position and closure domain models"
```

---

### Task 2: Storage — paper position tables and DuckDBStore methods

**Files:**
- Create: `src/polytrading/storage/schema/006_paper_positions.sql`
- Modify: `src/polytrading/storage/store.py`
- Test: `tests/storage/test_store_paper_positions.py`

**Interfaces:**
- Consumes: `PaperPosition`, `PaperPositionClosure` from Task 1.
- Produces on `DuckDBStore`: `append_paper_position(record: PaperPosition) -> bool`, `append_paper_position_closure(record: PaperPositionClosure) -> bool`, `open_paper_position_for_asset(asset: Asset) -> PaperPosition | None`, `paper_position_closure(position_id: UUID) -> PaperPositionClosure | None`, `paper_position(position_id: UUID) -> PaperPosition | None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/storage/test_store_paper_positions.py
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from polytrading.carry.economics_models import FundingDirection
from polytrading.domain.models import Asset
from polytrading.storage.store import ConflictingRecordError, DuckDBStore
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
        conflicting = _position(
            position_id=position.position_id, base_quantity=Decimal("0.6")
        )
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/storage/test_store_paper_positions.py -v`
Expected: FAIL — `duckdb.CatalogException: Table with name paper_positions does not exist`

- [ ] **Step 3: Write minimal implementation**

Create `src/polytrading/storage/schema/006_paper_positions.sql`:

```sql
CREATE TABLE paper_positions (
    position_id UUID PRIMARY KEY,
    source_evaluation_id UUID NOT NULL,
    asset VARCHAR NOT NULL,
    opened_at TIMESTAMPTZ NOT NULL,
    record_json JSON NOT NULL,
    schema_version INTEGER NOT NULL,
    record_hash VARCHAR NOT NULL
);

CREATE TABLE paper_position_closures (
    position_id UUID PRIMARY KEY,
    closed_at TIMESTAMPTZ NOT NULL,
    close_reason VARCHAR NOT NULL,
    record_json JSON NOT NULL,
    schema_version INTEGER NOT NULL,
    record_hash VARCHAR NOT NULL,
    CHECK (close_reason IN ('REGIME_REVERSED', 'MAX_HORIZON_REACHED', 'OPERATOR_CLOSED'))
);
```

Add to `src/polytrading/storage/store.py` (import `PaperPosition`, `PaperPositionClosure` under the existing `TYPE_CHECKING` block next to `CandidateEconomicsReport`, alongside the other `append_*` methods near `append_economic_evaluation`):

```python
    def append_paper_position(self, record: PaperPosition) -> bool:
        if self._in_transaction:
            return self._append_paper_position(record)
        with self.transaction():
            return self._append_paper_position(record)

    def append_paper_position_closure(self, record: PaperPositionClosure) -> bool:
        if self._in_transaction:
            return self._append_paper_position_closure(record)
        with self.transaction():
            return self._append_paper_position_closure(record)

    def open_paper_position_for_asset(self, asset: Asset) -> PaperPosition | None:
        row = self._connection.execute(
            """
            SELECT CAST(position.record_json AS VARCHAR)
            FROM paper_positions AS position
            LEFT JOIN paper_position_closures AS closure
              ON closure.position_id = position.position_id
            WHERE position.asset = ? AND closure.position_id IS NULL
            """,
            [asset.value],
        ).fetchone()
        if row is None:
            return None
        from polytrading.trial.paper_models import PaperPosition

        return PaperPosition.model_validate_json(row[0])

    def paper_position(self, position_id: UUID) -> PaperPosition | None:
        row = self._connection.execute(
            "SELECT CAST(record_json AS VARCHAR) FROM paper_positions WHERE position_id = ?",
            [position_id],
        ).fetchone()
        if row is None:
            return None
        from polytrading.trial.paper_models import PaperPosition

        return PaperPosition.model_validate_json(row[0])

    def paper_position_closure(self, position_id: UUID) -> PaperPositionClosure | None:
        row = self._connection.execute(
            "SELECT CAST(record_json AS VARCHAR) FROM paper_position_closures WHERE position_id = ?",
            [position_id],
        ).fetchone()
        if row is None:
            return None
        from polytrading.trial.paper_models import PaperPositionClosure

        return PaperPositionClosure.model_validate_json(row[0])
```

And the private helpers, near `_append_economic_evaluation`:

```python
    def _append_paper_position(self, record: PaperPosition) -> bool:
        if self._normalized_retry(
            "paper position",
            record,
            "paper_positions",
            "position_id = ?",
            [record.position_id],
        ):
            return False
        self._connection.execute(
            "INSERT INTO paper_positions VALUES (?, ?, ?, ?, ?::JSON, ?, ?)",
            [
                record.position_id,
                record.source_evaluation_id,
                record.asset.value,
                record.opened_at,
                _canonical_json(record),
                record.schema_version,
                _record_hash(record),
            ],
        )
        return True

    def _append_paper_position_closure(self, record: PaperPositionClosure) -> bool:
        if self._normalized_retry(
            "paper position closure",
            record,
            "paper_position_closures",
            "position_id = ?",
            [record.position_id],
        ):
            return False
        self._connection.execute(
            "INSERT INTO paper_position_closures VALUES (?, ?, ?, ?::JSON, ?, ?)",
            [
                record.position_id,
                record.closed_at,
                record.close_reason.value,
                _canonical_json(record),
                record.schema_version,
                _record_hash(record),
            ],
        )
        return True
```

Add `PaperPosition, PaperPositionClosure` to the existing `if TYPE_CHECKING:` import block that already imports `CandidateEconomicsReport` from `polytrading.carry.economics_models` — add a second import line for `from polytrading.trial.paper_models import PaperPosition, PaperPositionClosure`.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/storage/test_store_paper_positions.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add src/polytrading/storage/schema/006_paper_positions.sql src/polytrading/storage/store.py tests/storage/test_store_paper_positions.py
git commit -m "feat(storage): add append-only paper position and closure tables"
```

---

### Task 3: Paper execution — open/close/accrual functions and ledger reconciliation

**Files:**
- Create: `src/polytrading/trial/paper_execution.py`
- Test: `tests/trial/test_paper_execution.py`

**Interfaces:**
- Consumes: `walk_book`, `InsufficientDepthError`, `PairedBookObservation`, `entry_slippage_cost`, `forced_exit_cost` (`carry/economics_execution.py`); `select_direction`, `orient_funding`, `exact_median` (`carry/economics_funding.py`); `CandidateEconomicsReport`, `EconomicsDecision`, `FundingDirection` (`carry/economics_models.py`); `JournalTransaction`, `JournalPosting` (`ledger/models.py`); `PaperPosition`, `PaperPositionClosure`, `PaperCloseReason` (Task 1).
- Produces: `PaperOpenRejected(Exception)`, `open_paper_position(report, current_books, lighter_instrument, dydx_instrument, position_id, opening_book_cycle_id, opened_at) -> tuple[PaperPosition, JournalTransaction]`, `close_paper_position(position, current_books, lighter_instrument, dydx_instrument, closing_book_cycle_id, closed_at, close_reason, realized_funding_usd) -> tuple[PaperPositionClosure, JournalTransaction]`, `funding_accrual_transaction(position, effective_at, lighter_rate, dydx_rate) -> JournalTransaction | None` (returns `None` when net funding is exactly zero, since a zero-value posting is not a valid `JournalPosting`), `current_regime_reversed(oriented_hourly_rates: tuple[Decimal, ...]) -> bool`.

Ledger design note (read before implementing Step 3): a spread position has one long leg and one short leg, and their P&L signs are opposite functions of price (long profits when price rises, short profits when price falls). A single combined `paper:position` account keyed only on aggregate entry/exit notional cannot represent this — the two legs must post to **separate per-venue position accounts** (`paper:position:lighter`, `paper:position:dydx`), with the long leg using the ordinary debit-position/credit-cash-at-open convention and the short leg using the mirrored credit-position/debit-cash-at-open convention. Step 3 below shows the exact postings; the reconciliation tests in Step 1 are what prove this is right, not hand-derivation, so if a test fails, trust the test over the prose here.

- [ ] **Step 1: Write the failing test**

```python
# tests/trial/test_paper_execution.py
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from polytrading.carry.economics_execution import InsufficientDepthError, PairedBookObservation
from polytrading.carry.economics_models import EconomicsDecision, FundingDirection
from polytrading.domain.models import Asset, BookLevel, InstrumentKind, Level2BookSnapshot, Venue
from polytrading.trial.paper_execution import (
    PaperOpenRejected,
    close_paper_position,
    current_regime_reversed,
    funding_accrual_transaction,
    open_paper_position,
)
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
    books = PairedBookObservation(
        effective_at=datetime(2026, 8, 17, 12, 0, tzinfo=UTC),
        lighter=_book(Venue.LIGHTER, "60000", "60000", cycle_id),
        dydx=_book(Venue.DYDX, "60000", "60000", cycle_id),
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
    expected_trading_pnl = Decimal("60000") - Decimal("59010") + (Decimal("59005") - Decimal("60015"))
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
        realized_funding = sum(
            (p.debit - p.credit) for p in accrual_txn.postings if p.account == "paper:pnl:funding"
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/trial/test_paper_execution.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'polytrading.trial.paper_execution'`. (If `tests/carry/test_economics_models.py` has no importable `_report` helper, first add one there — a small frozen-default factory returning a valid `CandidateEconomicsReport` with keyword overrides, matching the pattern already used by `tests/carry/test_economics.py`'s own report-building test helpers.)

- [ ] **Step 3: Write minimal implementation**

```python
# src/polytrading/trial/paper_execution.py
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from polytrading.carry.economics_execution import (
    InsufficientDepthError,
    PairedBookObservation,
    WalkedQuote,
    walk_book,
)
from polytrading.carry.economics_funding import exact_median
from polytrading.carry.economics_models import CandidateEconomicsReport, EconomicsDecision, FundingDirection
from polytrading.domain.models import InstrumentSpec, Level2BookSnapshot, normalize_utc_timestamp
from polytrading.ledger.models import JournalPosting, JournalTransaction
from polytrading.trial.paper_models import PaperCloseReason, PaperPosition, PaperPositionClosure

PAPER_RESEARCH_WARNING = (
    "Research only — simulated paper position, not a live fill or trading authorization."
)


class PaperOpenRejected(ValueError):
    """Raised when a paper position cannot be opened from the given report/book."""


def current_regime_reversed(oriented_hourly_rates: tuple[Decimal, ...]) -> bool:
    """True when the trailing frozen-direction funding median is not positive."""
    return exact_median(oriented_hourly_rates) <= 0


def _entry_levels(
    direction: FundingDirection, lighter_book: Level2BookSnapshot, dydx_book: Level2BookSnapshot
) -> tuple[tuple, tuple]:
    if direction is FundingDirection.SHORT_LIGHTER_LONG_DYDX:
        return lighter_book.bids[:20], dydx_book.asks[:20]
    return lighter_book.asks[:20], dydx_book.bids[:20]


def _exit_levels(
    direction: FundingDirection, lighter_book: Level2BookSnapshot, dydx_book: Level2BookSnapshot
) -> tuple[tuple, tuple]:
    if direction is FundingDirection.SHORT_LIGHTER_LONG_DYDX:
        return lighter_book.asks[:20], dydx_book.bids[:20]
    return lighter_book.bids[:20], dydx_book.asks[:20]


def _walk_base(
    levels: tuple, base_quantity: Decimal, instrument: InstrumentSpec
) -> WalkedQuote:
    walked = walk_book(levels, base_quantity / instrument.contract_multiplier)
    return WalkedQuote(
        quantity=base_quantity,
        notional=walked.notional * instrument.contract_multiplier,
        weighted_average_price=walked.weighted_average_price,
    )


def _leg_is_long(direction: FundingDirection, venue: Venue) -> bool:
    if direction is FundingDirection.SHORT_LIGHTER_LONG_DYDX:
        return venue is Venue.DYDX
    return venue is Venue.LIGHTER


def open_paper_position(
    *,
    report: CandidateEconomicsReport,
    current_books: PairedBookObservation,
    lighter_instrument: InstrumentSpec,
    dydx_instrument: InstrumentSpec,
    position_id: UUID,
    opening_book_cycle_id: UUID,
    opened_at: datetime,
) -> tuple[PaperPosition, JournalTransaction]:
    if report.decision is not EconomicsDecision.SHADOW_CANDIDATE or report.economics is None:
        raise PaperOpenRejected("source report is not a SHADOW_CANDIDATE")
    direction = report.direction
    if direction is None:
        raise PaperOpenRejected("source report has no frozen direction")
    base_quantity = report.economics.base_quantity
    lighter_levels, dydx_levels = _entry_levels(direction, current_books.lighter, current_books.dydx)
    try:
        lighter_walk = _walk_base(lighter_levels, base_quantity, lighter_instrument)
        dydx_walk = _walk_base(dydx_levels, base_quantity, dydx_instrument)
    except InsufficientDepthError as error:
        raise PaperOpenRejected("current book cannot fill the frozen base quantity") from error

    normalized_opened_at = normalize_utc_timestamp(opened_at)
    position = PaperPosition(
        schema_version=1,
        position_id=position_id,
        source_evaluation_id=report.evaluation_id,
        asset=report.asset,
        direction=direction,
        opened_at=normalized_opened_at,
        base_quantity=base_quantity,
        lighter_entry_notional_usd=lighter_walk.notional,
        dydx_entry_notional_usd=dydx_walk.notional,
        lighter_entry_price=lighter_walk.weighted_average_price,
        dydx_entry_price=dydx_walk.weighted_average_price,
        opening_book_cycle_id=opening_book_cycle_id,
        warning=PAPER_RESEARCH_WARNING,
    )
    postings: list[JournalPosting] = []
    for venue, notional in (
        (Venue.LIGHTER, lighter_walk.notional),
        (Venue.DYDX, dydx_walk.notional),
    ):
        account = f"paper:position:{venue.value}"
        if _leg_is_long(direction, venue):
            postings.append(
                JournalPosting(
                    account=account, asset=report.asset.value, debit=notional, credit=Decimal(0)
                )
            )
            postings.append(
                JournalPosting(
                    account="paper:cash", asset=report.asset.value, debit=Decimal(0), credit=notional
                )
            )
        else:
            postings.append(
                JournalPosting(
                    account="paper:cash", asset=report.asset.value, debit=notional, credit=Decimal(0)
                )
            )
            postings.append(
                JournalPosting(
                    account=account, asset=report.asset.value, debit=Decimal(0), credit=notional
                )
            )
    transaction = JournalTransaction(
        schema_version=1,
        transaction_id=position_id,
        occurred_at=normalized_opened_at,
        observed_at=normalized_opened_at,
        description=f"paper open {report.asset.value} {direction.value}",
        postings=tuple(postings),
        evidence_ids=(str(opening_book_cycle_id),),
    )
    return position, transaction


def close_paper_position(
    *,
    position: PaperPosition,
    current_books: PairedBookObservation,
    lighter_instrument: InstrumentSpec,
    dydx_instrument: InstrumentSpec,
    closing_book_cycle_id: UUID,
    closed_at: datetime,
    close_reason: PaperCloseReason,
    realized_funding_usd: Decimal,
) -> tuple[PaperPositionClosure, JournalTransaction]:
    lighter_levels, dydx_levels = _exit_levels(
        position.direction, current_books.lighter, current_books.dydx
    )
    try:
        lighter_walk = _walk_base(lighter_levels, position.base_quantity, lighter_instrument)
        dydx_walk = _walk_base(dydx_levels, position.base_quantity, dydx_instrument)
    except InsufficientDepthError as error:
        raise PaperOpenRejected("current book cannot fill the exit quantity") from error

    normalized_closed_at = normalize_utc_timestamp(closed_at)
    postings: list[JournalPosting] = []
    trading_pnl = Decimal(0)
    for venue, entry_notional, exit_notional in (
        (Venue.LIGHTER, position.lighter_entry_notional_usd, lighter_walk.notional),
        (Venue.DYDX, position.dydx_entry_notional_usd, dydx_walk.notional),
    ):
        account = f"paper:position:{venue.value}"
        is_long = _leg_is_long(position.direction, venue)
        # long leg profits when price rises (exit > entry); short leg profits when
        # price falls (entry > exit) — this is the one place direction-sensitive
        # sign enters the ledger; everything else below is direction-agnostic.
        signed_pnl = (exit_notional - entry_notional) if is_long else (entry_notional - exit_notional)
        trading_pnl += signed_pnl
        if is_long:
            postings.append(
                JournalPosting(
                    account="paper:cash",
                    asset=position.asset.value,
                    debit=exit_notional,
                    credit=Decimal(0),
                )
            )
            postings.append(
                JournalPosting(
                    account=account, asset=position.asset.value, debit=Decimal(0), credit=entry_notional
                )
            )
        else:
            postings.append(
                JournalPosting(
                    account=account, asset=position.asset.value, debit=entry_notional, credit=Decimal(0)
                )
            )
            postings.append(
                JournalPosting(
                    account="paper:cash",
                    asset=position.asset.value,
                    debit=Decimal(0),
                    credit=exit_notional,
                )
            )
        pnl_debit = max(Decimal(0), -signed_pnl)
        pnl_credit = max(Decimal(0), signed_pnl)
        if pnl_debit > 0 or pnl_credit > 0:
            postings.append(
                JournalPosting(
                    account="paper:pnl:trading",
                    asset=position.asset.value,
                    debit=pnl_debit,
                    credit=pnl_credit,
                )
            )
    realized_pnl = trading_pnl + realized_funding_usd

    closure = PaperPositionClosure(
        schema_version=1,
        position_id=position.position_id,
        closed_at=normalized_closed_at,
        close_reason=close_reason,
        lighter_exit_notional_usd=lighter_walk.notional,
        dydx_exit_notional_usd=dydx_walk.notional,
        lighter_exit_price=lighter_walk.weighted_average_price,
        dydx_exit_price=dydx_walk.weighted_average_price,
        closing_book_cycle_id=closing_book_cycle_id,
        realized_funding_usd=realized_funding_usd,
        realized_pnl_usd=realized_pnl,
    )
    transaction = JournalTransaction(
        schema_version=1,
        transaction_id=position.position_id,
        occurred_at=normalized_closed_at,
        observed_at=normalized_closed_at,
        description=f"paper close {position.asset.value} {close_reason.value}",
        postings=tuple(postings),
        evidence_ids=(str(closing_book_cycle_id),),
    )
    return closure, transaction


def funding_accrual_transaction(
    *,
    position: PaperPosition,
    effective_at: datetime,
    lighter_rate: Decimal,
    dydx_rate: Decimal,
) -> JournalTransaction | None:
    if position.direction is FundingDirection.SHORT_LIGHTER_LONG_DYDX:
        lighter_funding_usd = -position.lighter_entry_notional_usd * lighter_rate
        dydx_funding_usd = position.dydx_entry_notional_usd * dydx_rate
    else:
        lighter_funding_usd = position.lighter_entry_notional_usd * lighter_rate
        dydx_funding_usd = -position.dydx_entry_notional_usd * dydx_rate
    net = lighter_funding_usd + dydx_funding_usd
    if net == 0:
        # a zero-value posting is not a valid JournalPosting; there is simply no
        # funding event to record for this hour.
        return None
    normalized_effective_at = normalize_utc_timestamp(effective_at)
    cash_debit = net if net > 0 else Decimal(0)
    cash_credit = -net if net < 0 else Decimal(0)
    from uuid import uuid5

    transaction_id = uuid5(position.position_id, normalized_effective_at.isoformat())
    return JournalTransaction(
        schema_version=1,
        transaction_id=transaction_id,
        occurred_at=normalized_effective_at,
        observed_at=normalized_effective_at,
        description=f"paper funding accrual {position.asset.value} {normalized_effective_at.isoformat()}",
        postings=(
            JournalPosting(
                account="paper:cash",
                asset=position.asset.value,
                debit=cash_debit,
                credit=cash_credit,
            ),
            JournalPosting(
                account="paper:pnl:funding",
                asset=position.asset.value,
                debit=cash_credit,
                credit=cash_debit,
            ),
        ),
        evidence_ids=(f"{position.position_id}:{normalized_effective_at.isoformat()}",),
    )
```

Each leg's own postings balance independently (open: debit X / credit X; close: debit exit + pnl_debit = credit entry + pnl_credit, which the reconciliation tests below verify numerically), so the whole transaction balances regardless of direction. `transaction_id` for a funding accrual is derived from `(position_id, effective_at)` via `uuid5`, not reused verbatim from `position_id` — each hour's accrual is its own distinct, idempotent, immutable transaction row rather than colliding across hours.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/trial/test_paper_execution.py -v`
Expected: PASS (10 tests)

- [ ] **Step 5: Commit**

```bash
git add src/polytrading/trial/paper_execution.py tests/trial/test_paper_execution.py
git commit -m "feat(trial): add paper open/close/funding-accrual execution logic"
```

---

### Task 4: CLI — `trial paper open` and `trial paper close`

**Files:**
- Modify: `src/polytrading/cli.py`
- Test: `tests/test_cli_paper.py`

**Interfaces:**
- Consumes: everything from Tasks 1–3, plus existing `database_writer_lease`, `_WRITER_LEASE_TIMEOUT_SECONDS`, `CliUsageError`, `_parse_timestamp`, `_utc_now`, `DuckDBStore`, `owned_resource_cleanup`.
- Produces: `polytrading trial paper open --evaluation-id <uuid> --db <path> [--confirm]`, `polytrading trial paper close --position-id <uuid> --db <path> [--confirm]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli_paper.py
from pathlib import Path

from polytrading.cli import main


def test_paper_open_without_confirm_writes_nothing_and_prints_preview(
    tmp_path: Path, capsys
) -> None:
    db = tmp_path / "test.duckdb"
    exit_code = main(
        [
            "trial",
            "paper",
            "open",
            "--evaluation-id",
            "00000000-0000-0000-0000-000000000000",
            "--db",
            str(db),
        ]
    )
    assert exit_code != 0
    assert "confirm" in capsys.readouterr().err.lower()


def test_paper_open_rejects_missing_evaluation(tmp_path: Path, capsys) -> None:
    from polytrading.storage.store import DuckDBStore

    db = tmp_path / "test.duckdb"
    DuckDBStore(db).close()
    exit_code = main(
        [
            "trial",
            "paper",
            "open",
            "--evaluation-id",
            "00000000-0000-0000-0000-000000000000",
            "--db",
            str(db),
            "--confirm",
        ]
    )
    assert exit_code != 0
    assert "not found" in capsys.readouterr().err.lower() or "shadow" in capsys.readouterr().err.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_cli_paper.py -v`
Expected: FAIL — `argparse` error, unrecognized `trial paper` subcommand.

- [ ] **Step 3: Write minimal implementation**

In `src/polytrading/cli.py`, extend the existing `trial` subparser block (near `trial_health = trial_commands.add_parser(...)`):

```python
    trial_paper = trial_commands.add_parser("paper", help="simulated forward paper execution")
    trial_paper_commands = trial_paper.add_subparsers(dest="trial_paper_command", required=True)
    trial_paper_open = trial_paper_commands.add_parser("open", help="open a paper position")
    trial_paper_open.add_argument("--evaluation-id", required=True)
    trial_paper_open.add_argument("--db", required=True, type=Path)
    trial_paper_open.add_argument("--confirm", action="store_true")
    trial_paper_close = trial_paper_commands.add_parser("close", help="close a paper position")
    trial_paper_close.add_argument("--position-id", required=True)
    trial_paper_close.add_argument("--db", required=True, type=Path)
    trial_paper_close.add_argument("--confirm", action="store_true")
    trial_paper_monitor = trial_paper_commands.add_parser(
        "monitor", help="close-eligible positions and accrue hourly funding"
    )
    trial_paper_monitor.add_argument("--db", required=True, type=Path)
    trial_paper_monitor.add_argument("--as-of")
```

In the `main()` dispatch, replace `return _trial_health(arguments)` (the trailing branch of the `trial` block) with:

```python
        if arguments.trial_command == "health":
            return _trial_health(arguments)
        if arguments.trial_paper_command == "open":
            return _trial_paper_open(arguments)
        if arguments.trial_paper_command == "close":
            return _trial_paper_close(arguments)
        return _trial_paper_monitor(arguments)
```

Add the command functions near `_trial_health`:

```python
def _trial_paper_open(arguments: argparse.Namespace) -> int:
    if not arguments.confirm:
        print(
            "polytrading: dry run — pass --confirm to open a paper position",
            file=sys.stderr,
        )
        return 2
    try:
        evaluation_id = UUID(arguments.evaluation_id)
    except (TypeError, ValueError, AttributeError) as error:
        raise CliUsageError("invalid evaluation UUID") from error
    if not arguments.db.is_file():
        raise CliUsageError("paper execution database is unavailable or not current")

    from polytrading.carry.economics_models import EconomicsDecision
    from polytrading.trial.book_evidence import eligible_lighter_dydx_book_pair
    from polytrading.trial.paper_execution import PaperOpenRejected, open_paper_position

    store: DuckDBStore | None = None
    try:
        with database_writer_lease(arguments.db, timeout_seconds=_WRITER_LEASE_TIMEOUT_SECONDS):
            store = DuckDBStore(arguments.db)
            report = store.get_economic_evaluation(evaluation_id)
            if report is None or report.decision is not EconomicsDecision.SHADOW_CANDIDATE:
                raise CliUsageError("evaluation not found or not a SHADOW_CANDIDATE")
            if store.open_paper_position_for_asset(report.asset) is not None:
                raise CliUsageError(f"a paper position is already open for {report.asset.value}")
            now = _utc_now()
            if now - report.evaluated_at > timedelta(hours=24):
                raise CliUsageError("SHADOW_CANDIDATE report is stale; re-run carry economics")
            cycles = store.book_collection_cycles_between(now - timedelta(minutes=5), now, now)
            eligible = next(
                (
                    item
                    for cycle in sorted(cycles, key=lambda c: c.request_completed_at, reverse=True)
                    if (
                        item := eligible_lighter_dydx_book_pair(
                            store, cycle, report.asset, now, Decimal("1000")
                        )
                    )
                    is not None
                ),
                None,
            )
            if eligible is None:
                raise CliUsageError("no eligible current book cycle to open against")
            lighter_instrument = store.latest_instrument_as_of(
                Venue.LIGHTER, eligible.pair.lighter.symbol, now
            )
            dydx_instrument = store.latest_instrument_as_of(
                Venue.DYDX, eligible.pair.dydx.symbol, now
            )
            if lighter_instrument is None or dydx_instrument is None:
                raise CliUsageError("current instrument specification is unavailable")
            try:
                position, transaction = open_paper_position(
                    report=report,
                    current_books=eligible.pair,
                    lighter_instrument=lighter_instrument,
                    dydx_instrument=dydx_instrument,
                    position_id=uuid4(),
                    opening_book_cycle_id=eligible.cycle.cycle_id,
                    opened_at=now,
                )
            except PaperOpenRejected as error:
                raise CliUsageError(str(error)) from error
            with store.transaction():
                store.append_paper_position(position)
                store.append_journal_transaction(transaction)
    except ConflictingRecordError as error:
        raise CliUsageError("paper position persistence conflict") from error
    finally:
        if store is not None:
            store.close()
    print(f"opened paper position {position.position_id} for {position.asset.value}")
    return 0


def _trial_paper_close(arguments: argparse.Namespace) -> int:
    if not arguments.confirm:
        print(
            "polytrading: dry run — pass --confirm to close a paper position",
            file=sys.stderr,
        )
        return 2
    try:
        position_id = UUID(arguments.position_id)
    except (TypeError, ValueError, AttributeError) as error:
        raise CliUsageError("invalid position UUID") from error
    if not arguments.db.is_file():
        raise CliUsageError("paper execution database is unavailable or not current")

    from polytrading.trial.book_evidence import eligible_lighter_dydx_book_pair
    from polytrading.trial.paper_execution import PaperOpenRejected, close_paper_position
    from polytrading.trial.paper_models import PaperCloseReason

    store: DuckDBStore | None = None
    try:
        with database_writer_lease(arguments.db, timeout_seconds=_WRITER_LEASE_TIMEOUT_SECONDS):
            store = DuckDBStore(arguments.db)
            position = store.paper_position(position_id)
            if position is None or store.paper_position_closure(position_id) is not None:
                raise CliUsageError("position not found or already closed")
            now = _utc_now()
            cycles = store.book_collection_cycles_between(now - timedelta(minutes=5), now, now)
            eligible = next(
                (
                    item
                    for cycle in sorted(cycles, key=lambda c: c.request_completed_at, reverse=True)
                    if (
                        item := eligible_lighter_dydx_book_pair(
                            store, cycle, position.asset, now, Decimal("1000")
                        )
                    )
                    is not None
                ),
                None,
            )
            if eligible is None:
                raise CliUsageError("no eligible current book cycle to close against")
            lighter_instrument = store.latest_instrument_as_of(
                Venue.LIGHTER, eligible.pair.lighter.symbol, now
            )
            dydx_instrument = store.latest_instrument_as_of(
                Venue.DYDX, eligible.pair.dydx.symbol, now
            )
            if lighter_instrument is None or dydx_instrument is None:
                raise CliUsageError("current instrument specification is unavailable")
            realized_funding = store.paper_position_realized_funding(position_id)
            try:
                closure, transaction = close_paper_position(
                    position=position,
                    current_books=eligible.pair,
                    lighter_instrument=lighter_instrument,
                    dydx_instrument=dydx_instrument,
                    closing_book_cycle_id=eligible.cycle.cycle_id,
                    closed_at=now,
                    close_reason=PaperCloseReason.OPERATOR_CLOSED,
                    realized_funding_usd=realized_funding,
                )
            except PaperOpenRejected as error:
                raise CliUsageError(str(error)) from error
            with store.transaction():
                store.append_paper_position_closure(closure)
                store.append_journal_transaction(transaction)
    except ConflictingRecordError as error:
        raise CliUsageError("paper position closure persistence conflict") from error
    finally:
        if store is not None:
            store.close()
    print(f"closed paper position {position_id}: realized pnl {closure.realized_pnl_usd}")
    return 0
```

`store.get_economic_evaluation(evaluation_id)` and `store.paper_position_realized_funding(position_id)` are two small new `DuckDBStore` methods this task also adds: the first is a straight `SELECT ... WHERE evaluation_id = ?` against `economic_evaluations` (mirroring `paper_position`'s shape), the second sums `journal_postings.debit - journal_postings.credit` for `account = 'paper:cash'` joined to `journal_transactions` where `description` starts with `'paper funding accrual'` and the posting's transaction shares the position's asset — add both alongside the other query methods near `latest_economic_evaluation_as_of`, with a focused unit test for each in `tests/storage/test_store_paper_positions.py` before wiring them into the CLI.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_cli_paper.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add src/polytrading/cli.py tests/test_cli_paper.py
git commit -m "feat(cli): add trial paper open/close commands"
```

---

### Task 5: CLI — `trial paper monitor` (hourly close/accrual loop)

**Files:**
- Modify: `src/polytrading/cli.py`
- Test: `tests/test_cli_paper_monitor.py`

**Interfaces:**
- Consumes: `current_regime_reversed`, `funding_accrual_transaction`, `close_paper_position` (Task 3); `select_prospective_funding` (`trial/funding_lineage.py`); `orient_funding` (`carry/economics_funding.py`).
- Produces: `polytrading trial paper monitor --db <path> [--as-of <iso8601>]`, printing one line per open position describing what happened (`held`, `accrued`, `closed:<reason>`), exit `0` always (monitoring is never a hard failure unless the database itself is unavailable).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli_paper_monitor.py
from pathlib import Path

from polytrading.cli import main


def test_monitor_with_no_open_positions_exits_zero(tmp_path: Path, capsys) -> None:
    from polytrading.storage.store import DuckDBStore

    db = tmp_path / "test.duckdb"
    DuckDBStore(db).close()
    exit_code = main(["trial", "paper", "monitor", "--db", str(db)])
    assert exit_code == 0
    assert "no open paper positions" in capsys.readouterr().out.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_cli_paper_monitor.py -v`
Expected: FAIL — `NameError`/`AttributeError` from the not-yet-implemented `_trial_paper_monitor`.

- [ ] **Step 3: Write minimal implementation**

Add to `src/polytrading/cli.py`:

```python
def _trial_paper_monitor(arguments: argparse.Namespace) -> int:
    as_of = _parse_timestamp(arguments.as_of) if arguments.as_of else _utc_now()
    if not arguments.db.is_file():
        raise CliUsageError("paper execution database is unavailable or not current")

    from polytrading.carry.economics_funding import orient_funding
    from polytrading.trial.book_evidence import eligible_lighter_dydx_book_pair
    from polytrading.trial.funding_lineage import select_prospective_funding
    from polytrading.trial.paper_execution import (
        close_paper_position,
        current_regime_reversed,
        funding_accrual_transaction,
    )
    from polytrading.trial.paper_models import PaperCloseReason

    symbols = {
        Venue.DYDX: {Asset.BTC: "BTC-USD", Asset.ETH: "ETH-USD", Asset.SOL: "SOL-USD"},
        Venue.LIGHTER: {Asset.BTC: "BTC", Asset.ETH: "ETH", Asset.SOL: "SOL"},
    }
    store: DuckDBStore | None = None
    lines: list[str] = []
    try:
        with database_writer_lease(arguments.db, timeout_seconds=_WRITER_LEASE_TIMEOUT_SECONDS):
            store = DuckDBStore(arguments.db)
            for asset in (Asset.BTC, Asset.ETH, Asset.SOL):
                position = store.open_paper_position_for_asset(asset)
                if position is None:
                    continue
                window_start = as_of - timedelta(hours=168)
                lighter_selection = select_prospective_funding(
                    store, Venue.LIGHTER, symbols[Venue.LIGHTER][asset], asset, window_start, as_of, as_of
                )
                dydx_selection = select_prospective_funding(
                    store, Venue.DYDX, symbols[Venue.DYDX][asset], asset, window_start, as_of, as_of
                )
                lighter_by_hour = {o.effective_at: o for o in lighter_selection.observations}
                dydx_by_hour = {o.effective_at: o for o in dydx_selection.observations}
                paired_hours = sorted(set(lighter_by_hour) & set(dydx_by_hour))
                differentials = tuple(
                    lighter_by_hour[hour].rate - dydx_by_hour[hour].rate for hour in paired_hours
                )
                if len(differentials) < 168:
                    lines.append(f"{asset.value}: held (insufficient regime evidence this cycle)")
                    continue
                oriented = orient_funding(differentials[-168:], position.direction)
                reversed_regime = current_regime_reversed(oriented)
                age = as_of - position.opened_at
                should_close = reversed_regime or age >= timedelta(days=28)
                if should_close:
                    reason = (
                        PaperCloseReason.REGIME_REVERSED
                        if reversed_regime
                        else PaperCloseReason.MAX_HORIZON_REACHED
                    )
                    cycles = store.book_collection_cycles_between(
                        as_of - timedelta(minutes=5), as_of, as_of
                    )
                    eligible = next(
                        (
                            item
                            for cycle in sorted(
                                cycles, key=lambda c: c.request_completed_at, reverse=True
                            )
                            if (
                                item := eligible_lighter_dydx_book_pair(
                                    store, cycle, asset, as_of, Decimal("1000")
                                )
                            )
                            is not None
                        ),
                        None,
                    )
                    if eligible is None:
                        lines.append(f"{asset.value}: held (no eligible book to close against yet)")
                        continue
                    lighter_instrument = store.latest_instrument_as_of(
                        Venue.LIGHTER, eligible.pair.lighter.symbol, as_of
                    )
                    dydx_instrument = store.latest_instrument_as_of(
                        Venue.DYDX, eligible.pair.dydx.symbol, as_of
                    )
                    if lighter_instrument is None or dydx_instrument is None:
                        lines.append(f"{asset.value}: held (instrument specification unavailable)")
                        continue
                    realized_funding = store.paper_position_realized_funding(position.position_id)
                    closure, transaction = close_paper_position(
                        position=position,
                        current_books=eligible.pair,
                        lighter_instrument=lighter_instrument,
                        dydx_instrument=dydx_instrument,
                        closing_book_cycle_id=eligible.cycle.cycle_id,
                        closed_at=as_of,
                        close_reason=reason,
                        realized_funding_usd=realized_funding,
                    )
                    with store.transaction():
                        store.append_paper_position_closure(closure)
                        store.append_journal_transaction(transaction)
                    lines.append(f"{asset.value}: closed:{reason.value} pnl={closure.realized_pnl_usd}")
                    continue
                latest_hour = paired_hours[-1]
                accrual = None
                if latest_hour > position.opened_at and not store.paper_position_funding_accrued(
                    position.position_id, latest_hour
                ):
                    accrual = funding_accrual_transaction(
                        position=position,
                        effective_at=latest_hour,
                        lighter_rate=lighter_by_hour[latest_hour].rate,
                        dydx_rate=dydx_by_hour[latest_hour].rate,
                    )
                if accrual is not None:
                    store.append_journal_transaction(accrual)
                    lines.append(f"{asset.value}: accrued funding for {latest_hour.isoformat()}")
                else:
                    lines.append(f"{asset.value}: held")
    finally:
        if store is not None:
            store.close()
    if not lines:
        print("no open paper positions")
    else:
        for line in lines:
            print(line)
    return 0
```

`store.paper_position_funding_accrued(position_id, effective_at)` is one more small `DuckDBStore` addition: since Task 3's `funding_accrual_transaction` derives `transaction_id = uuid5(position_id, effective_at.isoformat())`, this check is a straight `SELECT 1 FROM journal_transactions WHERE transaction_id = ?` using that same derived UUID — recompute it with `uuid5(position_id, effective_at.isoformat())` inside this method rather than adding a new lookup column.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_cli_paper_monitor.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/polytrading/cli.py tests/test_cli_paper_monitor.py src/polytrading/trial/paper_execution.py
git commit -m "feat(cli): add trial paper monitor for hourly close/accrual"
```

---

### Task 6: Dashboard data model — Paper Positions section

**Files:**
- Modify: `src/polytrading/web/models.py`
- Modify: `src/polytrading/web/dashboard.py`
- Test: `tests/web/test_dashboard_paper_positions.py`

**Interfaces:**
- Produces: `PaperPositionRow` (StrictRecord: `position_id`, `asset`, `direction`, `status` [`OPEN`/`CLOSED_REGIME_REVERSED`/`CLOSED_MAX_HORIZON_REACHED`/`CLOSED_OPERATOR_CLOSED`], `opened_at`, `closed_at`, `current_pnl_usd`, `hourly_pnl_points: tuple[tuple[datetime, Decimal], ...]`), extends `DashboardSnapshot` with `paper_position_rows: tuple[PaperPositionRow, ...]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/web/test_dashboard_paper_positions.py
from pathlib import Path

from polytrading.storage.store import DuckDBStore
from polytrading.web.dashboard import DashboardBuilder


def test_dashboard_snapshot_includes_empty_paper_positions(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    store = DuckDBStore(tmp_path / "test.duckdb")
    try:
        snapshot = DashboardBuilder(store, tmp_path / "test.duckdb").build(
            datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
        )
        assert snapshot.paper_position_rows == ()
    finally:
        store.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/web/test_dashboard_paper_positions.py -v`
Expected: FAIL — `AttributeError: 'DashboardSnapshot' object has no attribute 'paper_position_rows'`

- [ ] **Step 3: Write minimal implementation**

In `src/polytrading/web/models.py`, add:

```python
class PaperPositionRow(StrictRecord):
    schema_version: Literal[1]
    position_id: UUID
    asset: Asset
    direction: FundingDirection
    status: Literal[
        "OPEN",
        "CLOSED_REGIME_REVERSED",
        "CLOSED_MAX_HORIZON_REACHED",
        "CLOSED_OPERATOR_CLOSED",
    ]
    opened_at: datetime
    closed_at: datetime | None
    current_pnl_usd: Decimal
    hourly_pnl_points: tuple[tuple[datetime, Decimal], ...]

    @field_validator("opened_at", "closed_at")
    @classmethod
    def normalize_optional_timestamp(cls, value: datetime | None) -> datetime | None:
        return None if value is None else normalize_utc_timestamp(value)

    @model_validator(mode="after")
    def require_coherent_status(self) -> PaperPositionRow:
        if self.status == "OPEN" and self.closed_at is not None:
            raise ValueError("an OPEN row must not carry a closed_at")
        if self.status != "OPEN" and self.closed_at is None:
            raise ValueError("a closed row must carry closed_at")
        return self
```

Add `paper_position_rows: tuple[PaperPositionRow, ...]` as a new field on `DashboardSnapshot`, and extend `require_one_point_in_time` with:

```python
        if any(
            (row.closed_at is not None and row.closed_at > self.as_of)
            or row.opened_at > self.as_of
            for row in self.paper_position_rows
        ):
            raise ValueError("paper position evidence must not follow dashboard as-of")
```

In `src/polytrading/web/dashboard.py`, add a `_paper_position_rows(self, as_of: datetime) -> tuple[PaperPositionRow, ...]` method that queries `self._store` for every `paper_positions` row with `opened_at <= as_of` (a new `DuckDBStore.paper_positions_as_of(as_of)` method returning `tuple[PaperPosition, ...]`, plus reusing `paper_position_closure` per position and the existing `journal_trial_balance`-style querying — sum `paper:pnl:funding` + `paper:pnl:trading` postings for that position's asset up to `as_of` for `current_pnl_usd`, and bucket the same postings by hour for `hourly_pnl_points`), and wire it into `build()`'s returned `DashboardSnapshot(..., paper_position_rows=self._paper_position_rows(normalized_as_of))`.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/web/test_dashboard_paper_positions.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/polytrading/web/models.py src/polytrading/web/dashboard.py tests/web/test_dashboard_paper_positions.py
git commit -m "feat(web): add paper positions to the dashboard snapshot"
```

---

### Task 7: Dashboard UI — stat tiles, status badges, P&L sparkline

**Files:**
- Modify: `src/polytrading/web/assets/index.html`
- Modify: `src/polytrading/web/assets/app.css`
- Modify: `src/polytrading/web/assets/app.js`
- Test: `tests/web/test_dashboard_render.py` (extend existing render test if present, else create)

**Interfaces:**
- Consumes: `paper_position_rows` from the JSON snapshot (Task 7).

- [ ] **Step 1: Write the failing test**

```python
# tests/web/test_dashboard_render.py (add this test; keep existing tests in the file if present)
def test_dashboard_html_declares_paper_positions_section() -> None:
    html = (Path(__file__).parents[2] / "src/polytrading/web/assets/index.html").read_text()
    assert 'id="paper-positions"' in html
```

(Add `from pathlib import Path` at the top if the file doesn't already import it.)

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/web/test_dashboard_render.py -v`
Expected: FAIL

- [ ] **Step 3: Write minimal implementation**

In `src/polytrading/web/assets/index.html`, add a new section after the existing `<section id="economics" ...>` block:

```html
    <section id="paper-positions" class="dashboard-section" aria-labelledby="paper-positions-title">
      <h2 id="paper-positions-title">Paper positions</h2>
      <div class="count-grid" id="paper-stat-tiles"></div>
      <div id="paper-position-cards"></div>
    </section>
```

In `src/polytrading/web/assets/app.css`, add (near the existing `.status-pill` rule):

```css
.paper-pill[data-status="OPEN"] { color: var(--cyan); }
.paper-pill[data-status="CLOSED_REGIME_REVERSED"] { color: var(--amber); }
.paper-pill[data-status="CLOSED_MAX_HORIZON_REACHED"] { color: var(--info); }
.paper-sparkline { display: block; width: 100%; height: 40px; margin-top: 10px; }
.paper-sparkline .baseline { stroke: var(--line); stroke-width: 1; }
.paper-sparkline .trend { stroke: var(--dim); stroke-width: 2; fill: none; }
.paper-sparkline .endpoint-good { fill: var(--cyan); }
.paper-sparkline .endpoint-bad { fill: var(--coral); }
.paper-sparkline text { font-family: var(--mono); font-size: 0.62rem; fill: var(--ink); }
```

In `src/polytrading/web/assets/app.js`, add a `renderPaperPositions(snapshot)` function following the existing `renderTrial`/`renderDiscovery` pattern (reusing the file's existing `element()` helper for DOM construction):

```javascript
function renderPaperPositions(snapshot) {
  const rows = snapshot.paper_position_rows;
  const tiles = document.getElementById("paper-stat-tiles");
  tiles.replaceChildren();
  const open = rows.filter((r) => r.status === "OPEN");
  const regimeClosed = rows.filter((r) => r.status === "CLOSED_REGIME_REVERSED");
  const horizonClosed = rows.filter((r) => r.status === "CLOSED_MAX_HORIZON_REACHED");
  const aggregatePnl = open.reduce((sum, r) => sum + Number(r.current_pnl_usd), 0);
  [
    ["Open positions", open.length],
    ["Closed — regime reversed", regimeClosed.length],
    ["Closed — max horizon", horizonClosed.length],
    ["Aggregate open P&L (USD)", aggregatePnl.toFixed(2)],
  ].forEach(([label, value]) => {
    tiles.appendChild(
      element("div", { class: "stat-tile" }, [
        element("span", { class: "label" }, [label]),
        element("strong", {}, [String(value)]),
      ])
    );
  });

  const cards = document.getElementById("paper-position-cards");
  cards.replaceChildren();
  rows.forEach((row) => {
    const card = element("div", { class: "trial-detail-card" }, [
      element("header", {}, [
        element("span", {}, [`${row.asset} · ${row.direction}`]),
        element("span", { class: "paper-pill status-pill", "data-status": row.status }, [row.status]),
      ]),
      renderSparkline(row.hourly_pnl_points, Number(row.current_pnl_usd)),
    ]);
    cards.appendChild(card);
  });
}

function renderSparkline(points, currentValue) {
  const width = 280;
  const height = 40;
  const values = points.map(([, v]) => Number(v));
  const max = Math.max(0, ...values, currentValue);
  const min = Math.min(0, ...values, currentValue);
  const span = max - min || 1;
  const zeroY = height - ((0 - min) / span) * height;
  const path = points
    .map(([, v], i) => {
      const x = (i / Math.max(1, points.length - 1)) * width;
      const y = height - ((Number(v) - min) / span) * height;
      return `${i === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
  const endpointX = width;
  const endpointY = height - ((currentValue - min) / span) * height;
  const endpointClass = currentValue >= 0 ? "endpoint-good" : "endpoint-bad";
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "paper-sparkline");
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.innerHTML = `
    <line class="baseline" x1="0" y1="${zeroY}" x2="${width}" y2="${zeroY}" />
    <path class="trend" d="${path}" />
    <circle class="${endpointClass}" cx="${endpointX}" cy="${endpointY}" r="4" />
    <text x="${endpointX - 6}" y="${endpointY - 8}" text-anchor="end">${currentValue.toFixed(2)}</text>
  `;
  return svg;
}
```

Call `renderPaperPositions(snapshot)` from wherever the existing `render*` functions are invoked together (the same place `renderTrial(snapshot)` / `renderDiscovery(snapshot)` are called after a fetch).

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/web/test_dashboard_render.py -v`
Expected: PASS

- [ ] **Step 5: Manually verify in a browser**

```bash
.venv/bin/polytrading dashboard --db var/forward.duckdb --port 8787
```

Open `http://127.0.0.1:8787`, confirm the Paper positions section renders (empty state is fine with no positions yet), and confirm no console errors.

- [ ] **Step 6: Commit**

```bash
git add src/polytrading/web/assets/index.html src/polytrading/web/assets/app.css src/polytrading/web/assets/app.js tests/web/test_dashboard_render.py
git commit -m "feat(web): render paper positions stat tiles, badges, and P&L sparklines"
```

---

### Task 8: Full-suite verification

- [ ] **Step 1: Run the complete test suite**

Run: `.venv/bin/python -m pytest`
Expected: all tests pass, including every test added in Tasks 1–8.

- [ ] **Step 2: Run lint**

Run: `.venv/bin/ruff check .`
Expected: no findings. Fix any and re-run.

- [ ] **Step 3: Update graphify's graph**

Run: `graphify update .`

- [ ] **Step 4: Final scope audit**

Grep the diff for any credential, key, wallet, or signer reference introduced by this feature:

```bash
git diff main --stat
git diff main -- src/polytrading/trial/paper_execution.py src/polytrading/cli.py | grep -iE "api_key|secret|private_key|signer|wallet" || echo "clean"
```

Expected: `clean` — confirming no execution/credential dependency was introduced, matching the spec's completion criteria.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "chore: refresh graphify graph after paper-execution feature"
```
