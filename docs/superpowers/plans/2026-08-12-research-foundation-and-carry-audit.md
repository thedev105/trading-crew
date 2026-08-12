# Research Foundation and Read-Only Carry Audit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a reproducible, point-in-time research foundation and a read-only BTC/ETH/SOL perpetual-funding audit over Hyperliquid and Bybit public APIs, without credentials, balances, deposits, orders, or claims of profitability.

**Architecture:** A Python package separates immutable domain records, append-only DuckDB persistence, versioned registries, public venue adapters, deterministic compatibility checks, and a diagnostic carry report. Venue payloads are stored before normalization. The audit may rank raw funding spreads, but it must mark a pair ineligible whenever required compatibility or cost evidence is missing and must never create a trade proposal.

**Tech Stack:** Python 3.12–3.14, Pydantic 2.13.4, DuckDB 1.5.4, HTTPX 0.28.1, pytest 9.1.1, pytest-cov 7.1.0, Hypothesis 6.160.0, Ruff 0.15.22, setuptools 83.0.0.

## Global Constraints

- Approved specification: [`docs/superpowers/specs/2026-08-12-market-neutral-opportunity-router-design.md`](../specs/2026-08-12-market-neutral-opportunity-router-design.md), especially Sections 6, 9, 10, 14, 15.2, and 16.
- Scope ends at public-data collection, deterministic normalization, compatibility auditing, and reports. Do not add authentication, account endpoints, secrets, wallet code, deposits, withdrawals, balances, positions, orders, paper fills, or live/paper allocation.
- Use only BTC, ETH, and SOL linear perpetuals. Inverse, quanto, pre-launch, hyperp, options, dated futures, and borrowed-token structures fail closed.
- Store every source payload append-only before derived records. Retain the exchange timestamp, timezone-aware UTC receipt timestamp, `time.monotonic_ns()` receipt marker, latency, endpoint, source version, and SHA-256 content hash.
- Use `Decimal` for prices, rates, quantities, multipliers, fees, and P&L. JSON represents decimals as strings and timestamps as RFC 3339 UTC strings.
- A positive funding rate means longs pay shorts. Normalize to an hourly decimal rate before comparisons; annualized diagnostic values use `hourly_rate * Decimal("8760")` and are never presented as forecasts.
- Metadata needed for compatibility is required evidence. Unknown index, oracle, mark, liquidation, collateral, P&L currency, funding formula, cap, interval, or payment timing makes a pair ineligible.
- Hyperliquid USDC collateral and Bybit USDT settlement are intentionally expected to demonstrate `COLLATERAL_MISMATCH` or another failed compatibility reason in this phase. Never waive the master specification's unmatched-collateral exclusion to manufacture an eligible pair.
- Automated tests use checked-in fixtures and `httpx.MockTransport`; they must not depend on network availability. A manual public-network smoke command is permitted and must not be part of CI.
- Every task follows red-green-refactor: write the named failing test, run it and observe the stated failure, implement the smallest behavior, rerun the focused test, then run the task's regression command.
- Each persisted or reported record carries a schema version. Database schema changes are forward-only migrations; never overwrite point-in-time facts.
- Official API contracts used by the adapters:
  - [Hyperliquid perpetual info endpoint](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals)
  - [Hyperliquid L2 book snapshot](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint#l2-book-snapshot)
  - [Hyperliquid rate limits](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits)
  - [Bybit instruments info](https://bybit-exchange.github.io/docs/v5/market/instrument)
  - [Bybit tickers](https://bybit-exchange.github.io/docs/v5/market/tickers)
  - [Bybit funding history](https://bybit-exchange.github.io/docs/v5/market/history-fund-rate)
  - [Bybit orderbook snapshots](https://bybit-exchange.github.io/docs/v5/market/orderbook)

---

## Task 1: Bootstrap the Reproducible Python Package

**Files:**

- Create: `pyproject.toml`
- Create: `.gitignore`
- Create: `README.md`
- Create: `src/polytrading/__init__.py`
- Create: `src/polytrading/cli.py`
- Create: `tests/test_package.py`

- [ ] **Step 1: Write the failing package smoke test**

```python
# tests/test_package.py
from importlib.metadata import version

import polytrading


def test_package_exposes_installed_version() -> None:
    assert polytrading.__version__ == version("polytrading")
```

- [ ] **Step 2: Run the test and verify the expected failure**

Run:

```bash
python3 -m pytest tests/test_package.py -q
```

Expected: FAIL during collection with `ModuleNotFoundError: No module named 'polytrading'`.

- [ ] **Step 3: Add pinned package and tool configuration**

```toml
# pyproject.toml
[build-system]
requires = ["setuptools==83.0.0"]
build-backend = "setuptools.build_meta"

[project]
name = "polytrading"
version = "0.1.0"
description = "Point-in-time market-neutral trading research"
readme = "README.md"
requires-python = ">=3.12,<3.15"
dependencies = [
  "duckdb==1.5.4",
  "httpx==0.28.1",
  "pydantic==2.13.4",
]

[project.optional-dependencies]
dev = [
  "hypothesis==6.160.0",
  "pytest==9.1.1",
  "pytest-cov==7.1.0",
  "ruff==0.15.22",
]

[project.scripts]
polytrading = "polytrading.cli:main"

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "--strict-markers --strict-config"

[tool.ruff]
target-version = "py312"
line-length = 100

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "SIM", "RUF"]
```

Create `.gitignore` with `.venv/`, `*.duckdb`, `.pytest_cache/`, `.ruff_cache/`, `__pycache__/`, `dist/`, and `*.egg-info/`. Create a README that says this is research software, documents the public-data-only boundary, and contains the setup commands below.

- [ ] **Step 4: Add the package version and a non-operational CLI**

```python
# src/polytrading/__init__.py
from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("polytrading")
except PackageNotFoundError:
    __version__ = "0.1.0"
```

```python
# src/polytrading/cli.py
import argparse


def build_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(prog="polytrading", description="Read-only research tools")


def main() -> int:
    build_parser().parse_args()
    return 0
```

- [ ] **Step 5: Create the environment and run the test suite**

Run:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest tests/test_package.py -q
.venv/bin/ruff check .
```

Expected: one passing test and Ruff exits 0.

- [ ] **Step 6: Commit the bootstrap**

```bash
git add pyproject.toml .gitignore README.md src/polytrading tests/test_package.py
git commit -m "build: bootstrap research package"
```

---

## Task 2: Define Immutable Point-in-Time Domain Records

**Files:**

- Create: `src/polytrading/domain/__init__.py`
- Create: `src/polytrading/domain/models.py`
- Create: `tests/domain/test_models.py`

- [ ] **Step 1: Write failing validation and serialization tests**

Test these exact behaviors in `tests/domain/test_models.py`:

```python
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from polytrading.domain.models import Asset, FundingObservation, InstrumentSpec, Venue


NOW = datetime(2026, 8, 12, 12, tzinfo=UTC)


def test_funding_normalizes_to_hourly_rate() -> None:
    item = FundingObservation(
        schema_version=1,
        venue=Venue.BYBIT,
        symbol="BTCUSDT",
        asset=Asset.BTC,
        rate=Decimal("0.0001"),
        interval_hours=Decimal("8"),
        effective_at=NOW,
        observed_at=NOW,
        source_hash="a" * 64,
    )
    assert item.hourly_rate == Decimal("0.0000125")


def test_naive_timestamp_fails_closed() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        InstrumentSpec.example(observed_at=datetime(2026, 8, 12, 12))


def test_unknown_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        FundingObservation.model_validate({"unexpected": "field"})
```

`InstrumentSpec.example` is a test-only classmethod that returns a valid BTC linear-perpetual record while accepting field overrides. Keep it on the production model so fixtures across later tasks use one canonical valid record.

- [ ] **Step 2: Run the tests and verify the expected failure**

Run:

```bash
.venv/bin/python -m pytest tests/domain/test_models.py -q
```

Expected: FAIL during collection because `polytrading.domain.models` does not exist.

- [ ] **Step 3: Implement the strict shared model base and enumerations**

```python
class StrictRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    @field_validator("observed_at", check_fields=False)
    @classmethod
    def require_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        return value.astimezone(UTC)


class Venue(StrEnum):
    HYPERLIQUID = "hyperliquid"
    BYBIT = "bybit"


class Asset(StrEnum):
    BTC = "BTC"
    ETH = "ETH"
    SOL = "SOL"


class InstrumentKind(StrEnum):
    LINEAR_PERPETUAL = "linear_perpetual"
```

- [ ] **Step 4: Implement exact records and invariants**

Create these immutable records:

```python
class RawEnvelope(StrictRecord):
    schema_version: Literal[1]
    event_id: UUID
    venue: Venue
    endpoint: str
    venue_timestamp: datetime | None
    observed_at: datetime
    received_monotonic_ns: int
    request_latency_ms: Decimal
    source_version: str
    payload_json: str
    source_hash: str


class InstrumentSpec(StrictRecord):
    schema_version: Literal[1]
    instrument_id: str
    venue: Venue
    symbol: str
    asset: Asset
    kind: InstrumentKind
    contract_multiplier: Decimal
    index_family: str | None
    oracle_family: str | None
    mark_method: str | None
    liquidation_method: str | None
    collateral_asset: str | None
    pnl_asset: str | None
    funding_formula_id: str | None
    funding_cap: Decimal | None
    funding_interval_hours: Decimal
    funding_payment_offset_minutes: int | None
    min_notional: Decimal | None
    quantity_step: Decimal | None
    price_tick: Decimal | None
    is_inverse: bool
    is_prelaunch: bool
    observed_at: datetime
    source_hash: str


class FundingObservation(StrictRecord):
    schema_version: Literal[1]
    venue: Venue
    symbol: str
    asset: Asset
    rate: Decimal
    interval_hours: Decimal
    effective_at: datetime
    observed_at: datetime
    source_hash: str

    @computed_field
    @property
    def hourly_rate(self) -> Decimal:
        return self.rate / self.interval_hours


class MarketSnapshot(StrictRecord):
    schema_version: Literal[1]
    venue: Venue
    symbol: str
    asset: Asset
    bid: Decimal
    ask: Decimal
    mark: Decimal
    index: Decimal
    open_interest: Decimal | None
    effective_at: datetime
    observed_at: datetime
    source_hash: str


class BookLevel(StrictRecord):
    price: Decimal
    quantity: Decimal
    order_count: int | None


class Level2BookSnapshot(StrictRecord):
    schema_version: Literal[1]
    cycle_id: UUID
    venue: Venue
    symbol: str
    asset: Asset
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]
    depth_limit: Literal[20]
    sequence: str | None
    effective_at: datetime
    observed_at: datetime
    source_hash: str


class FeeSchedule(StrictRecord):
    schema_version: Literal[1]
    venue: Venue
    tier_name: str
    maker_rate: Decimal
    taker_rate: Decimal
    effective_from: datetime
    observed_at: datetime
    source_url: str
    source_hash: str
```

Validators must require positive intervals, multipliers, bids, asks, marks, indices, book prices,
and book quantities; require `ask >= bid`; require book bids in strictly descending price order and
asks in strictly ascending order with no crossed top of book; require 64 lowercase hexadecimal
characters for every source hash; and serialize all `Decimal` fields as strings in JSON mode.
`FundingObservation`, `MarketSnapshot`, and `Level2BookSnapshot` validate `effective_at` with the same
UTC rule as `observed_at`. Empty or one-sided L2 books fail closed.

- [ ] **Step 5: Run focused and property tests**

Add Hypothesis coverage proving that non-zero positive interval values preserve `hourly_rate * interval_hours == rate`, and that naive datetimes always fail.

Run:

```bash
.venv/bin/python -m pytest tests/domain/test_models.py -q
.venv/bin/ruff check src/polytrading/domain tests/domain
```

Expected: all domain tests pass and Ruff exits 0.

- [ ] **Step 6: Commit the domain layer**

```bash
git add src/polytrading/domain tests/domain
git commit -m "feat: add immutable market research records"
```

---

## Task 3: Add Append-Only DuckDB Storage and Forward-Only Migrations

**Files:**

- Create: `src/polytrading/storage/__init__.py`
- Create: `src/polytrading/storage/schema/001_initial.sql`
- Create: `src/polytrading/storage/store.py`
- Create: `tests/storage/test_store.py`

- [ ] **Step 1: Write failing append, idempotency, conflict, and as-of tests**

Create tests that use `tmp_path / "research.duckdb"` and prove:

1. migrations record version `1` exactly once;
2. raw envelopes, instruments, funding observations, market snapshots, L2 books, and fee schedules round-trip without float conversion;
3. inserting the same primary key and identical content is an idempotent no-op;
4. inserting the same primary key with different content raises `ConflictingRecordError`;
5. `latest_instrument_as_of` never returns a version observed after the requested timestamp;
6. an exception inside `store.transaction()` rolls back every row in that unit of work.

- [ ] **Step 2: Run the tests and verify the expected failure**

Run:

```bash
.venv/bin/python -m pytest tests/storage/test_store.py -q
```

Expected: FAIL during collection because `polytrading.storage.store` does not exist.

- [ ] **Step 3: Create the append-only schema**

`001_initial.sql` creates:

- `schema_migrations(version INTEGER PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL)`;
- `raw_envelopes(event_id UUID PRIMARY KEY, venue VARCHAR, endpoint VARCHAR, venue_timestamp TIMESTAMPTZ, observed_at TIMESTAMPTZ, received_monotonic_ns UBIGINT, request_latency_ms DECIMAL(18,6), source_version VARCHAR, payload_json JSON, source_hash VARCHAR, schema_version INTEGER)`;
- append-only `instrument_specs`, `funding_observations`, `market_snapshots`, `book_snapshots`,
  `book_levels`, and `fee_schedules` tables containing every field from Task 2;
- compound primary keys that include the immutable identity and `observed_at` or `effective_at` timestamp;
- a `record_hash VARCHAR NOT NULL` column on every normalized table for retry conflict detection.

Do not create update or delete methods. Migration application runs in one transaction and rejects an unknown gap such as version 3 when version 2 is absent.

- [ ] **Step 4: Implement the store public interface**

The public class is `DuckDBStore`. Its constructor is `__init__(path: Path)`. It exposes
`close()`, the `transaction()` context manager, `append_raw(record)`,
`append_instrument(record)`, `append_funding(record)`, `append_market_snapshot(record)`,
`append_book_snapshot(record)`, `append_fee_schedule(record)`,
`latest_instrument_as_of(venue, symbol, as_of)`, `latest_book_as_of(venue, symbol, as_of)`, and
`latest_fee_as_of(venue, tier_name, as_of)`, and `funding_between(venue, symbol, start, end)`.
Append methods return `True` for a new row and `False` only for an exact retry. The instrument
as-of method returns `InstrumentSpec | None`; the book as-of method returns
`Level2BookSnapshot | None`; the fee as-of method returns `FeeSchedule | None`; the range method
returns `tuple[FundingObservation, ...]`.

Canonicalize model JSON with sorted keys and compact separators, hash it, query the existing
`record_hash`, and raise `ConflictingRecordError` when an existing identity has different
content. Convert DuckDB decimal values back through Pydantic without casting them to `float`.

- [ ] **Step 5: Run focused tests and the regression suite**

Run:

```bash
.venv/bin/python -m pytest tests/storage/test_store.py -q
.venv/bin/python -m pytest -q
.venv/bin/ruff check src/polytrading/storage tests/storage
```

Expected: storage tests and all earlier tests pass; Ruff exits 0.

- [ ] **Step 6: Commit storage**

```bash
git add src/polytrading/storage tests/storage
git commit -m "feat: add append-only research store"
```

---

## Task 4: Add Versioned Registries, Experiment Records, and a Balanced Research Ledger

**Files:**

- Modify: `src/polytrading/storage/schema/001_initial.sql`
- Modify: `src/polytrading/storage/store.py`
- Create: `src/polytrading/registry/__init__.py`
- Create: `src/polytrading/registry/instruments.py`
- Create: `src/polytrading/registry/fees.py`
- Create: `src/polytrading/research/__init__.py`
- Create: `src/polytrading/research/models.py`
- Create: `src/polytrading/ledger/__init__.py`
- Create: `src/polytrading/ledger/models.py`
- Create: `tests/registry/test_instruments.py`
- Create: `tests/registry/test_fees.py`
- Create: `tests/research/test_registry.py`
- Create: `tests/ledger/test_journal.py`

- [ ] **Step 1: Write failing registry and ledger tests**

Test that `InstrumentRegistry.as_of` and `FeeRegistry.as_of` return the latest record at or before
the supplied timestamp, never a future version, and return `None` when no eligible version exists.
Test that `require_as_of` raises `MissingPointInTimeRecordError` carrying only the immutable lookup
key and timestamp.

Test `FeeRegistry.calculate(venue, tier_name, liquidity, notional, as_of)` for maker and taker rates,
exact Decimal multiplication, negative/rebate rates, zero notional, and missing point-in-time fee
evidence. This function reports a fee cash flow only; it does not choose order type or trade size.

Test that an `ExperimentRecord` freezes the hypothesis, feature allowlist, parameters, evaluation window, benchmark, success criteria, code revision, data cutoff, fee version, and trial-family identifier. Test that the store returns the experiment unchanged and rejects a conflicting duplicate.

Test that `JournalTransaction` accepts postings only when, for each asset, total debits equal total credits exactly:

```python
def test_unbalanced_transaction_is_rejected() -> None:
    with pytest.raises(ValidationError, match="debits must equal credits"):
        JournalTransaction(
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
```

- [ ] **Step 2: Run the tests and verify the expected failure**

Run:

```bash
.venv/bin/python -m pytest tests/registry tests/research/test_registry.py tests/ledger/test_journal.py -q
```

Expected: FAIL during collection because the registry, research, and ledger modules are absent.

- [ ] **Step 3: Implement the instrument and fee registry services**

`InstrumentRegistry.record(spec)` and `FeeRegistry.record(schedule)` delegate to append-only store
methods. `as_of` always requires an explicit timezone-aware timestamp. `require_as_of` wraps `as_of`
and fails closed. Registry services expose immutable domain records and contain no strategy logic,
network client, current-time lookup, or mutation API. `FeeRegistry.calculate` accepts
`liquidity: Literal["maker", "taker"]`, selects the corresponding point-in-time rate, and returns
`notional * rate`; it rejects negative notional and never assumes an absent fee tier.

- [ ] **Step 4: Implement immutable research and ledger records**

`ExperimentRecord` uses a UUID primary key and strict/frozen Pydantic configuration. `parameters`, `success_criteria`, and feature names are sorted into canonical tuples before hashing.

`JournalPosting` has `account`, `asset`, `debit`, and `credit`; exactly one of debit or credit must be positive. `JournalTransaction` has `transaction_id`, `occurred_at`, `observed_at`, `description`, at least two postings, and at least one evidence ID. Reject negative values and reject any per-asset imbalance.

- [ ] **Step 5: Persist both record types append-only**

Add `experiments`, `journal_transactions`, and `journal_postings` tables to the initial migration
because no released database exists yet. Add `append_experiment(record) -> bool`,
`append_journal_transaction(record) -> bool`,
`get_experiment(experiment_id) -> ExperimentRecord | None`, and
`journal_trial_balance(as_of) -> tuple[TrialBalanceRow, ...]`. Use transactional inserts. The
journal header and all postings commit atomically. A trial balance reports debit, credit, and
zero difference per asset and account without converting decimals to floats.

- [ ] **Step 6: Run focused tests and all quality checks**

Run:

```bash
.venv/bin/python -m pytest tests/registry tests/research/test_registry.py tests/ledger/test_journal.py -q
.venv/bin/python -m pytest -q
.venv/bin/ruff check .
```

Expected: all tests pass and Ruff exits 0.

- [ ] **Step 7: Commit governance records**

```bash
git add src/polytrading/storage src/polytrading/registry src/polytrading/research src/polytrading/ledger tests/registry tests/research tests/ledger
git commit -m "feat: add experiment registry and research ledger"
```

---

## Task 5: Define the Public Venue Boundary and Raw-First Recorder

**Files:**

- Create: `src/polytrading/venues/__init__.py`
- Create: `src/polytrading/venues/public.py`
- Create: `src/polytrading/venues/recorder.py`
- Create: `src/polytrading/venues/synchronized.py`
- Modify: `src/polytrading/storage/schema/001_initial.sql`
- Modify: `src/polytrading/storage/store.py`
- Create: `tests/venues/test_recorder.py`
- Create: `tests/venues/test_synchronized.py`

- [ ] **Step 1: Write the failing raw-first recorder test**

Use a fake adapter and store spy. The adapter returns one raw response plus one normalized funding observation. Assert that the store call order is exactly `append_raw`, then `append_funding`. Make `append_raw` raise and assert no normalized append occurs.

Write a second failing test in `test_synchronized.py` using two delayed fake adapters. Assert both
book requests begin before either completes, all six BTC/ETH/SOL venue pairs share one `cycle_id`,
and `BookCollectionCycle.max_effective_skew_ms` is the maximum exchange-timestamp difference rather
than local task completion time. Assert a cycle over 1,000 ms is stored with status
`skew_exceeds_research_target`.

- [ ] **Step 2: Run the test and verify the expected failure**

Run:

```bash
.venv/bin/python -m pytest tests/venues/test_recorder.py tests/venues/test_synchronized.py -q
```

Expected: FAIL during collection because the venue boundary does not exist.

- [ ] **Step 3: Define the adapter response and protocol**

```python
NormalizedRecord = InstrumentSpec | FundingObservation | MarketSnapshot | Level2BookSnapshot


@dataclass(frozen=True)
class AdapterBatch:
    raw: tuple[RawEnvelope, ...]
    normalized: tuple[NormalizedRecord, ...]


class PublicVenueAdapter(Protocol):
    venue: Venue

    async def fetch_instruments(
        self, assets: frozenset[Asset], observed_at: datetime
    ) -> AdapterBatch:
        """Fetch public point-in-time instrument specifications."""

    async def fetch_funding_history(
        self, asset: Asset, start: datetime, end: datetime, observed_at: datetime
    ) -> AdapterBatch:
        """Fetch public realized funding observations in the closed interval."""

    async def fetch_market_snapshots(
        self, assets: frozenset[Asset], observed_at: datetime
    ) -> AdapterBatch:
        """Fetch public top-of-book, mark, index, and open-interest snapshots."""

    async def fetch_order_books(
        self, assets: frozenset[Asset], observed_at: datetime, cycle_id: UUID
    ) -> AdapterBatch:
        """Fetch public 20-level book snapshots for one collection cycle."""
```

Production implementations must contain concrete code. The boundary contains no authenticated client or order methods.

- [ ] **Step 4: Implement raw-envelope construction and recording**

`make_raw_envelope` accepts the exact response bytes, endpoint, source version, optional venue timestamp, measured monotonic start/end values, and injected wall-clock receipt time. It stores UTF-8 JSON without reformatting and hashes the exact bytes.

`PublicRecorder.record(batch)` opens one store transaction, appends every raw envelope first, then dispatches normalized records by exact type. Unknown normalized types raise before commit. Add a test proving a normalized-record failure rolls back the raw insert as a unit.

```python
class BookCollectionCycle(StrictRecord):
    schema_version: Literal[1]
    cycle_id: UUID
    assets: tuple[Asset, ...]
    venues: tuple[Venue, ...]
    request_started_at: datetime
    request_completed_at: datetime
    effective_timestamps: tuple[datetime, ...]
    max_effective_skew_ms: Decimal
    status: Literal["complete", "failed", "skew_exceeds_research_target"]
    failure_codes: tuple[str, ...]
    source_hashes: tuple[str, ...]
```

`SynchronizedBookCollector.collect_once(adapters, assets, observed_at)` creates one UUID, launches
all adapter book calls concurrently with `asyncio.gather`, records both batches raw-first in one
transaction, and emits an immutable `BookCollectionCycle` with request start/end, each source
effective timestamp, maximum effective skew, success/failure per venue, and evidence hashes. One
venue failure records the failed cycle and no partial normalized books. This collector is research
infrastructure, not an execution synchronizer and does not imply cross-venue atomicity.

Add `book_collection_cycles` to `001_initial.sql` and
`append_book_collection_cycle(record) -> bool` plus
`latest_book_cycle_as_of(as_of) -> BookCollectionCycle | None` to `DuckDBStore`. Persist the cycle
and all of its raw and normalized book records in one transaction. The as-of query may return a
failed or high-skew cycle so the audit can report it, but must never return a future cycle.

- [ ] **Step 5: Run tests and lint**

Run:

```bash
.venv/bin/python -m pytest tests/venues/test_recorder.py tests/venues/test_synchronized.py -q
.venv/bin/ruff check src/polytrading/venues tests/venues
```

Expected: all recorder tests pass and Ruff exits 0.

- [ ] **Step 6: Commit the public boundary**

```bash
git add src/polytrading/venues tests/venues
git commit -m "feat: add raw-first public venue boundary"
```

---

## Task 6: Implement the Hyperliquid Public Adapter Fixture-First

**Files:**

- Create: `src/polytrading/venues/hyperliquid.py`
- Create: `tests/fixtures/hyperliquid/meta_and_asset_ctxs.json`
- Create: `tests/fixtures/hyperliquid/funding_history_page_1.json`
- Create: `tests/fixtures/hyperliquid/funding_history_page_2.json`
- Create: `tests/fixtures/hyperliquid/l2_book.json`
- Create: `tests/venues/test_hyperliquid.py`

- [ ] **Step 1: Check in minimal representative official-response fixtures**

The metadata fixture contains BTC, ETH, SOL, one unsupported asset, universe `szDecimals`, and contexts with `markPx`, `oraclePx`, `funding`, and `openInterest`. The two funding fixtures overlap on one timestamp to test deduplication and end with an empty page. Keep numeric JSON values as strings whenever the API returns strings.

- [ ] **Step 2: Write failing request-shape and parser tests**

Using `httpx.MockTransport`, assert:

- the URL is `https://api.hyperliquid.xyz/info`;
- metadata uses `POST {"type": "metaAndAssetCtxs"}`;
- funding uses `POST {"type": "fundingHistory", "coin": "BTC", "startTime": 1786449600000, "endTime": 1786453200000}` for the fixed one-hour test range;
- only BTC, ETH, and SOL are returned;
- instrument kind is linear perpetual, collateral and P&L asset are `USDC`, interval is one hour, and unsupported or unknown metadata remains `None` rather than guessed;
- funding pages are deduplicated, UTC-normalized, range-bounded, and sorted ascending;
- each `POST {"type": "l2Book", "coin": "BTC", "nSigFigs": null}` response becomes one 20-level-or-less immutable book with Hyperliquid's response time, per-level order counts, and `sequence=None`;
- reversed sides, crossed books, empty sides, and more than 20 returned levels fail closed;
- a repeated page with no timestamp progress raises `PaginationStalledError`;
- every batch includes the exact raw response envelope before normalized records.

- [ ] **Step 3: Run tests and verify the expected failure**

Run:

```bash
.venv/bin/python -m pytest tests/venues/test_hyperliquid.py -q
```

Expected: FAIL during collection because `polytrading.venues.hyperliquid` does not exist.

- [ ] **Step 4: Implement the adapter with injected transport and clocks**

```python
class HyperliquidPublicAdapter:
    venue = Venue.HYPERLIQUID

    def __init__(
        self,
        client: httpx.AsyncClient,
        wall_clock: Callable[[], datetime],
        monotonic_ns: Callable[[], int],
    ) -> None:
        self._client = client
        self._wall_clock = wall_clock
        self._monotonic_ns = monotonic_ns
```

Implement the `PublicVenueAdapter` methods. Use a finite request budget derived from the requested range, stop on an empty response or when the maximum returned millisecond timestamp reaches `end`, and set the next `startTime` to `max_time + 1`. Reject non-list responses, missing required numeric fields, invalid decimal strings, and timestamps outside the requested range. Do not infer undocumented index, mark, liquidation, cap, or payment-offset identifiers; store them as unknown so compatibility fails closed.

- [ ] **Step 5: Run focused tests and the regression suite**

Run:

```bash
.venv/bin/python -m pytest tests/venues/test_hyperliquid.py -q
.venv/bin/python -m pytest -q
.venv/bin/ruff check src/polytrading/venues tests/venues
```

Expected: all tests pass and Ruff exits 0.

- [ ] **Step 6: Commit the Hyperliquid adapter**

```bash
git add src/polytrading/venues/hyperliquid.py tests/fixtures/hyperliquid tests/venues/test_hyperliquid.py
git commit -m "feat: ingest Hyperliquid public funding data"
```

---

## Task 7: Implement the Bybit Public Adapter Fixture-First

**Files:**

- Create: `src/polytrading/venues/bybit.py`
- Create: `tests/fixtures/bybit/instruments_page_1.json`
- Create: `tests/fixtures/bybit/instruments_page_2.json`
- Create: `tests/fixtures/bybit/tickers.json`
- Create: `tests/fixtures/bybit/funding_history_page_1.json`
- Create: `tests/fixtures/bybit/funding_history_page_2.json`
- Create: `tests/fixtures/bybit/orderbook.json`
- Create: `tests/venues/test_bybit.py`

- [ ] **Step 1: Check in representative official-response fixtures**

Include cursor pagination, BTCUSDT/ETHUSDT/SOLUSDT, an inverse contract, a pre-listing contract, and an unsupported asset. Include a funding-page overlap. Preserve Bybit's string numbers and millisecond timestamps.

- [ ] **Step 2: Write failing request-shape and parser tests**

Using `httpx.MockTransport`, assert:

- instruments call `GET /v5/market/instruments-info` with `category=linear`, `limit=1000`, and returned `nextPageCursor` values;
- tickers call `GET /v5/market/tickers` with `category=linear`;
- funding history calls `GET /v5/market/funding/history` with `category=linear`, a symbol, `startTime`, `endTime`, and `limit=200`;
- non-zero `retCode` raises `VenueResponseError` with the endpoint and code but not the whole payload;
- only active BTC/ETH/SOL linear, non-prelaunch perpetuals survive;
- `fundingInterval` minutes becomes exact decimal hours;
- Bybit USDT collateral/P&L and every documented specification field are normalized without guesses;
- backward pagination uses `earliest_time - 1`, deduplicates the overlap, and sorts output ascending;
- order books call `GET /v5/market/orderbook` with `category=linear`, exact symbol, and `limit=20`, preserving `u` and `seq` in the sequence string and `cts` as the matching-engine effective time;
- reversed sides, crossed books, missing `cts`, and repeated sequence IDs within one cycle fail closed;
- an unchanged earliest timestamp raises `PaginationStalledError`.

- [ ] **Step 3: Run tests and verify the expected failure**

Run:

```bash
.venv/bin/python -m pytest tests/venues/test_bybit.py -q
```

Expected: FAIL during collection because `polytrading.venues.bybit` does not exist.

- [ ] **Step 4: Implement the Bybit adapter**

```python
class BybitPublicAdapter:
    venue = Venue.BYBIT

    def __init__(
        self,
        client: httpx.AsyncClient,
        wall_clock: Callable[[], datetime],
        monotonic_ns: Callable[[], int],
    ) -> None:
        self._client = client
        self._wall_clock = wall_clock
        self._monotonic_ns = monotonic_ns
```

Implement finite cursor and history loops with duplicate detection. Join ticker values to instrument records by exact symbol; absence of a ticker produces no `MarketSnapshot` and records a structured warning. Parse values through `Decimal` directly from strings. Reject results missing `result.list`, timestamps outside the requested range, and funding whose interval cannot be resolved from the point-in-time instrument registry.

- [ ] **Step 5: Run focused tests and all adapters**

Run:

```bash
.venv/bin/python -m pytest tests/venues/test_bybit.py -q
.venv/bin/python -m pytest tests/venues -q
.venv/bin/ruff check src/polytrading/venues tests/venues
```

Expected: all venue tests pass and Ruff exits 0.

- [ ] **Step 6: Commit the Bybit adapter**

```bash
git add src/polytrading/venues/bybit.py tests/fixtures/bybit tests/venues/test_bybit.py
git commit -m "feat: ingest Bybit public funding data"
```

---

## Task 8: Normalize Funding and Fail Closed on Contract Compatibility

**Files:**

- Create: `src/polytrading/carry/__init__.py`
- Create: `src/polytrading/carry/models.py`
- Create: `src/polytrading/carry/compatibility.py`
- Create: `src/polytrading/carry/normalize.py`
- Create: `tests/carry/test_compatibility.py`
- Create: `tests/carry/test_normalize.py`

- [ ] **Step 1: Write failing compatibility tests for every required field**

Start with two identical `InstrumentSpec.example()` values and assert compatibility. Parametrize mutations for underlying, kind, multiplier, inverse flag, index family, oracle family, mark method, liquidation method, collateral, P&L asset, funding formula, funding cap, interval, and payment offset. Assert one stable reason code per mutation. Parametrize every required field as `None`; for example, a missing collateral field must emit `MISSING_METADATA:collateral_asset`.

Include this required phase-one case:

```python
def test_hyperliquid_usdc_and_bybit_usdt_are_ineligible() -> None:
    result = compare_contracts(hyperliquid_btc(), bybit_btc())
    assert result.compatible is False
    assert CompatibilityReason.COLLATERAL_MISMATCH in result.reasons
    assert CompatibilityReason.PNL_ASSET_MISMATCH in result.reasons
```

- [ ] **Step 2: Write failing funding-sign and annualization tests**

Prove that positive funding means the high-rate venue is the short leg, negative rates preserve signs, 1-hour and 8-hour observations compare by hourly rate, and `hourly_spread * 8760` returns the diagnostic annualized decimal. Add a property test proving that swapping the legs negates the spread.

- [ ] **Step 3: Run tests and verify the expected failure**

Run:

```bash
.venv/bin/python -m pytest tests/carry/test_compatibility.py tests/carry/test_normalize.py -q
```

Expected: FAIL during collection because the carry modules do not exist.

- [ ] **Step 4: Implement stable compatibility and diagnostic models**

```python
class CompatibilityReason(StrEnum):
    ASSET_MISMATCH = "asset_mismatch"
    KIND_MISMATCH = "kind_mismatch"
    MULTIPLIER_MISMATCH = "multiplier_mismatch"
    INVERSE_UNSUPPORTED = "inverse_unsupported"
    INDEX_MISMATCH = "index_mismatch"
    ORACLE_MISMATCH = "oracle_mismatch"
    MARK_METHOD_MISMATCH = "mark_method_mismatch"
    LIQUIDATION_METHOD_MISMATCH = "liquidation_method_mismatch"
    COLLATERAL_MISMATCH = "collateral_mismatch"
    PNL_ASSET_MISMATCH = "pnl_asset_mismatch"
    FUNDING_FORMULA_MISMATCH = "funding_formula_mismatch"
    FUNDING_CAP_MISMATCH = "funding_cap_mismatch"
    FUNDING_INTERVAL_MISMATCH = "funding_interval_mismatch"
    FUNDING_PAYMENT_TIME_MISMATCH = "funding_payment_time_mismatch"
    PRELAUNCH_UNSUPPORTED = "prelaunch_unsupported"


class CompatibilityResult(StrictRecord):
    compatible: bool
    reasons: tuple[str, ...]
```

`compare_contracts` emits all applicable reasons in enumeration order. It returns `compatible=True` only when the reason tuple is empty.

- [ ] **Step 5: Implement funding comparison without forecasting**

```python
class FundingSpreadDiagnostic(StrictRecord):
    schema_version: Literal[1]
    asset: Asset
    long_venue: Venue
    long_symbol: str
    short_venue: Venue
    short_symbol: str
    long_hourly_rate: Decimal
    short_hourly_rate: Decimal
    hourly_spread: Decimal
    diagnostic_annualized_spread: Decimal
    as_of: datetime
    compatibility: CompatibilityResult
    forecast_status: Literal["not_evaluated"] = "not_evaluated"
```

`compare_latest_funding` selects the lower hourly rate as long and higher hourly rate as short, computes the two diagnostic fields exactly, attaches compatibility, and never emits expected return, probability, size, or a trade instruction.

- [ ] **Step 6: Run focused tests and regression checks**

Run:

```bash
.venv/bin/python -m pytest tests/carry -q
.venv/bin/python -m pytest -q
.venv/bin/ruff check src/polytrading/carry tests/carry
```

Expected: all tests pass and Ruff exits 0.

- [ ] **Step 7: Commit normalization and compatibility**

```bash
git add src/polytrading/carry tests/carry
git commit -m "feat: audit perpetual funding compatibility"
```

---

## Task 9: Build a Read-Only Carry Audit with Explicit Ineligibility

**Files:**

- Create: `src/polytrading/carry/audit.py`
- Create: `src/polytrading/carry/report.py`
- Create: `tests/carry/test_audit.py`
- Create: `tests/carry/test_report.py`
- Create: `tests/fixtures/replay/public_snapshot.jsonl`

- [ ] **Step 1: Write failing audit tests**

Construct an as-of store with two versions of each instrument and funding record. Assert that `CarryAuditor.audit(as_of)` reads only records available at `as_of`, creates exactly one cross-venue diagnostic per asset, and does not expose proposal, order, quantity, leverage, allocation, or expected-profit fields.

Add two L2 cycles around the cutoff. Assert the report selects only the latest complete cycle at or
before `as_of`, reports effective-time skew, per-venue book age, top-level spread, and cumulative
notional at the common first 20 levels, and never turns depth into a proposed quantity. A future
cycle, partial cycle, crossed book, or cycle above the configured skew target cannot count as
executable-book evidence.

Assert these status rules:

- `INELIGIBLE` when compatibility reasons exist;
- `INSUFFICIENT_DATA` when either venue lacks a current instrument or funding observation;
- `STALE` when any selected record is older than its configured maximum age;
- `DIAGNOSTIC_ONLY` only when compatibility is complete and current, because 12-month history, 90-day forward evidence, fees, execution depth, reversal risk, and stress tests are not implemented in this phase.

Funding and book readiness are separate fields. Missing or stale books do not hide funding
observations; they add `BOOK_EVIDENCE_MISSING`, `BOOK_EVIDENCE_STALE`, or
`BOOK_CYCLE_SKEW_EXCEEDED` and prevent `DIAGNOSTIC_ONLY` status.

- [ ] **Step 2: Write failing deterministic report tests**

For the fixed replay fixture, compare the emitted JSON byte-for-byte to a checked-in expected structure and assert the text report contains:

```text
RESEARCH ONLY — NOT A TRADE RECOMMENDATION
No credentials, balances, positions, or orders were accessed.
Instantaneous annualization is diagnostic, not a funding forecast.
```

Also assert every asset row has `status`, `reason_codes`, source hashes, selected record timestamps,
hourly rates, `forecast_status=not_evaluated`, book cycle ID, book ages, cycle skew, and depth-summary
evidence or an explicit missing-book reason.

- [ ] **Step 3: Run tests and verify the expected failure**

Run:

```bash
.venv/bin/python -m pytest tests/carry/test_audit.py tests/carry/test_report.py -q
```

Expected: FAIL during collection because the audit and report modules do not exist.

- [ ] **Step 4: Implement the audit service**

```python
class CarryAuditor:
    def __init__(
        self,
        store: DuckDBStore,
        max_instrument_age: timedelta,
        max_funding_age: timedelta,
        max_book_age: timedelta,
        max_book_cycle_skew: timedelta,
    ) -> None:
        self._store = store
        self._max_instrument_age = max_instrument_age
        self._max_funding_age = max_funding_age
        self._max_book_age = max_book_age
        self._max_book_cycle_skew = max_book_cycle_skew

    def audit(self, as_of: datetime) -> CarryAuditReport:
        """Return BTC, ETH, and SOL diagnostics in that stable order."""
```

The service uses exact as-of queries, never the computer's current time internally. It attaches every rejection reason without suppressing the raw spread. It must not estimate the master design's 7-, 14-, or 30-day conservative carry until a separately registered historical model and fee/depth data exist.

- [ ] **Step 5: Implement canonical JSON and human-readable reports**

`render_json(report)` uses sorted keys, two-space indentation, RFC 3339 `Z`, and decimal strings. `render_text(report)` prints the three warnings first, then one stable row per asset. The report footer lists missing activation evidence: 12 months point-in-time history, 45 continuous days of synchronized books, fee and slippage models, reversal/forced-exit reserve, complete stress suite, 90 forward days, ledger reconciliation, and eligibility review.

- [ ] **Step 6: Run focused and full tests**

Run:

```bash
.venv/bin/python -m pytest tests/carry/test_audit.py tests/carry/test_report.py -q
.venv/bin/python -m pytest -q
.venv/bin/ruff check .
```

Expected: all tests pass and report snapshots are deterministic.

- [ ] **Step 7: Commit the audit**

```bash
git add src/polytrading/carry tests/carry tests/fixtures/replay
git commit -m "feat: report read-only carry diagnostics"
```

---

## Task 10: Wire Replay, Public Collection, and End-to-End Verification

**Files:**

- Modify: `src/polytrading/cli.py`
- Create: `src/polytrading/replay.py`
- Create: `tests/test_cli.py`
- Modify: `README.md`

- [ ] **Step 1: Write failing CLI tests**

Call `main(argv)` directly and use a temporary database. Prove these commands and exit codes:

```text
polytrading replay --input tests/fixtures/replay/public_snapshot.jsonl --db /tmp/cli-test.duckdb   => 0
polytrading carry audit --db /tmp/cli-test.duckdb --as-of 2026-08-12T12:00:00Z --format json      => 0
polytrading carry audit --db /tmp/cli-test.duckdb --as-of invalid                                 => 2
polytrading collect public --venue unknown --db /tmp/cli-test.duckdb                              => 2
polytrading collect books --venue all --assets BTC,ETH,SOL --once --db /tmp/cli-test.duckdb         => 0
```

Assert the audit output is deterministic and every fixture row is represented in raw storage before
its normalized record. Assert the one-shot book command launches both venue calls in one cycle. Add
an AST-based boundary test that fails if files under `src/polytrading/venues` define method names
matching `place_order`, `cancel_order`, `withdraw`, `transfer`, `authenticate`, or `sign`.

- [ ] **Step 2: Run tests and verify the expected failure**

Run:

```bash
.venv/bin/python -m pytest tests/test_cli.py -q
```

Expected: FAIL because `main` does not accept `argv` and has no subcommands.

- [ ] **Step 3: Implement replay and CLI composition**

Change the CLI signature to `main(argv: Sequence[str] | None = None) -> int`. Implement:

- `replay`: validates each JSONL row as an `AdapterBatch`, records it raw-first, and aborts the transaction on any malformed row;
- `collect public`: accepts `hyperliquid`, `bybit`, or `all`; accepts `--assets BTC,ETH,SOL`, `--start`, `--end`, and `--db`; uses only public clients; records instruments, current snapshots, and funding history;
- `collect books`: accepts the same venue/assets/database arguments plus mutually exclusive `--once`
  or `--duration-seconds`; concurrently captures 20 levels from both venues every
  `--interval-seconds` (default 5), records cycle evidence, and continues after a failed cycle with a
  bounded backoff while never filling a missing cycle with old data;
- `carry audit`: requires explicit `--as-of`; prints text or canonical JSON; returns 0 even when every row is ineligible because ineligibility is a valid research result.

HTTP clients use a 10-second connect timeout, 30-second read timeout, a descriptive user agent, bounded retry for `429` and transient `5xx`, jitter-free deterministic backoff in tests, and no retry for parse or schema errors. Limit the manual collection default to a seven-day history window so a typo cannot trigger a large scrape.

- [ ] **Step 4: Document exact workflows and boundaries**

README sections:

1. research purpose and no-profit disclaimer;
2. setup and pinned environment;
3. deterministic fixture replay;
4. public-network smoke collection;
5. synchronized 20-level book collection, effective-time skew, gap reporting, Hyperliquid's absent
   REST sequence number, and why REST snapshots do not prove continuous sequence integrity;
6. carry audit interpretation, including why a large raw annualized spread can still be `INELIGIBLE`;
7. database backup/replay and schema-version behavior;
8. explicit absence of credentials and trading methods;
9. evidence still required by the Class C activation gate.

Document these commands:

```bash
.venv/bin/polytrading replay \
  --input tests/fixtures/replay/public_snapshot.jsonl \
  --db var/replay.duckdb
.venv/bin/polytrading carry audit \
  --db var/replay.duckdb \
  --as-of 2026-08-12T12:00:00Z \
  --format text
.venv/bin/polytrading collect public \
  --venue all \
  --assets BTC,ETH,SOL \
  --start 2026-08-05T12:00:00Z \
  --end 2026-08-12T12:00:00Z \
  --db var/public.duckdb
.venv/bin/polytrading collect books \
  --venue all \
  --assets BTC,ETH,SOL \
  --duration-seconds 3600 \
  --interval-seconds 5 \
  --db var/public.duckdb
```

- [ ] **Step 5: Run final verification**

Run:

```bash
.venv/bin/python -m pytest --cov=polytrading --cov-report=term-missing --cov-fail-under=90
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/polytrading replay --input tests/fixtures/replay/public_snapshot.jsonl --db /tmp/polytrading-replay.duckdb
.venv/bin/polytrading carry audit --db /tmp/polytrading-replay.duckdb --as-of 2026-08-12T12:00:00Z --format text
git diff --check
```

Expected: tests pass with at least 90% coverage, Ruff and `git diff --check` exit 0, replay succeeds,
and the replay report shows all three assets with research-only warnings and explicit status/reasons.
Run the documented public-network book smoke command separately; it is not a completion gate and
does not make CI depend on external availability.

- [ ] **Step 6: Perform the scope audit**

Run:

```bash
rg -n -i 'api[_ -]?key|private[_ -]?key|place[_ -]?order|cancel[_ -]?order|withdraw|deposit|transfer|wallet|signing' src tests README.md
```

Expected: only documentation, the AST boundary test's prohibited-name list, and assertions describing absent capabilities match. Investigate every other match before committing.

- [ ] **Step 7: Commit the completed first increment**

```bash
git add src/polytrading/cli.py src/polytrading/replay.py tests/test_cli.py README.md
git commit -m "feat: complete read-only carry research increment"
```

---

## Plan Completion Checks

- [ ] Every requirement in master-design rollout Step 1 and the read-only portion of Step 2 maps to at least one task above.
- [ ] Hyperliquid and Bybit raw payloads, normalized records, registries, experiments, and journal entries are append-only and queryable as-of a supplied timestamp.
- [ ] Concurrent 20-level book cycles preserve raw responses, effective timestamps, sequence evidence
  where exposed, cycle skew, failures, and gaps without claiming REST snapshot continuity or atomicity.
- [ ] Compatibility has no permissive unknown state; missing evidence is an explicit rejection reason.
- [ ] Reports distinguish instantaneous diagnostics from forecasts and never present expected profit or a trade recommendation.
- [ ] The package contains no credential, balance, position, signing, deposit, withdrawal, transfer, or order capability.
- [ ] Test fixtures cover pagination overlap, stalled pagination, malformed responses, future-data exclusion, duplicate conflicts, UTC failures, decimal precision, and collateral mismatch.
- [ ] All tests, coverage, lint, format, replay, scope scan, and `git diff --check` commands pass before the plan is declared implemented.
