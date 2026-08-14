# Lighter-dYdX Prospective Evidence Operations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a restart-safe, scheduler-driven Lighter-dYdX public-evidence pipeline whose
funding, synchronized-book, readiness, and dashboard records can support the frozen shadow-
economics evaluator without introducing trading authority.

**Architecture:** Add a candidate-specific append-only funding protocol and bind economics funding
selection to its on-time cycle lineage. Reuse the generic synchronized-book schema through a
prepare-then-persist refactor, acquire short bounded database writer leases, compute point-in-time
readiness in pure/read-only layers, and extend the loopback dashboard without adding mutation
surfaces.

**Tech Stack:** Python 3.12–3.14, Pydantic 2.13.4, DuckDB 1.5.4, httpx 0.28.1, argparse, pytest
9.1.1, Hypothesis 6.160.0, Ruff 0.15.22, local HTML/CSS/JavaScript, and standard-library file
locking.

## Global Constraints

- Implement
  `docs/superpowers/specs/2026-08-14-lighter-dydx-evidence-operations-design.md` exactly.
- Keep `lighter-dydx-prospective-funding-v1` separate from the existing fixed
  `point-in-time-funding-cycle-v1` Bybit-Hyperliquid protocol.
- Support exactly dYdX and Lighter and exactly BTC, ETH, and SOL.
- Allow funding collection only from the exact UTC boundary through five minutes after it,
  inclusive; a later invocation makes no venue request.
- Never let generic historical, replayed, or post-window funding qualify for trial readiness or
  shadow economics.
- Require exact venue, asset, symbol, boundary, observed-at, and funding source-hash linkage from
  every economics funding row to an on-time captured candidate-cycle item.
- Fail a conflicting eligible funding revision closed with `FUNDING_REVISION_CONFLICT`.
- Use five-second samples in a scheduled 60-second pre-boundary book burst; do not implement a
  continuously running internal daemon.
- Select an hourly book only at or before its UTC boundary, no more than five minutes old, and with
  no more than one second pair skew.
- Keep the economics windows fixed at 720 training funding hours, 1,440 evaluation funding hours,
  2,160 total funding hours, 1,440 hourly book representatives, and the final 168 consecutive
  funding hours, with the existing 99% coverage threshold.
- Open DuckDB only for each trial book persistence transaction and release it before sleeping.
- Keep every funding attempt and book cycle append-only; never synthesize a missing rate, book,
  heartbeat, failure row, or historical repair.
- Keep health, dashboard, and historical-cutoff paths read-only and offline.
- Keep the dashboard GET-only, loopback-only, and explicit about stale/unavailable values.
- Add no credential, account, signer, wallet, balance, position, transfer, order, fill,
  cancellation, custody, paper-execution, or live-execution dependency.
- Keep AI outside collection, health, readiness, and economics decisions.
- Use exact `Decimal` values in models and canonical JSON; do not round coverage through float.
- Preserve at least 90% total repository coverage and keep the complete existing suite green.

## Scope decomposition

The specification crosses collection, storage, economics lineage, readiness, and presentation.
They remain in one sequential plan because the candidate-cycle identity is the shared contract and
each later subsystem consumes exact interfaces created earlier. Each task below still ends in a
reviewable, independently tested commit.

## File Structure

- Create `src/polytrading/trial/__init__.py`: public trial package boundary.
- Create `src/polytrading/trial/funding_models.py`: candidate funding timing, enums, items, and
  immutable cycle validators.
- Create `src/polytrading/trial/funding.py`: in-memory candidate funding collection and atomic
  persistence.
- Create `src/polytrading/trial/funding_report.py`: canonical candidate-cycle text and JSON.
- Create `src/polytrading/trial/funding_lineage.py`: binding on-time cycle-to-normalized-row
  selection and conflict detection.
- Create `src/polytrading/trial/writer_lease.py`: bounded cross-process local database writer lease.
- Create `src/polytrading/trial/books.py`: fixed Lighter-dYdX bounded/one-shot book runner.
- Create `src/polytrading/trial/health_models.py`: strict collection/readiness contracts and window
  arithmetic.
- Create `src/polytrading/trial/book_evidence.py`: shared no-look-ahead eligible book-pair selection.
- Create `src/polytrading/trial/health.py`: read-only boundary aggregation, readiness, and projection.
- Create `src/polytrading/trial/health_report.py`: canonical trial-health text and JSON.
- Create `src/polytrading/storage/schema/005_lighter_dydx_trial_operations.sql`: append-only
  candidate funding-cycle table.
- Modify `src/polytrading/storage/store.py`: candidate cycle, fee inventory, and evidence-count
  persistence/read APIs.
- Modify `src/polytrading/venues/synchronized.py`: split book preparation from persistence while
  preserving the existing one-shot API.
- Modify `src/polytrading/carry/economics_assembler.py`: require prospective trial lineage and use
  shared book eligibility.
- Modify `src/polytrading/cli.py`: register `trial funding`, `trial books`, and `trial health`.
- Modify `src/polytrading/web/models.py`, `dashboard.py`, and `server.py`: add the point-in-time trial
  snapshot, recipes, counts, and sanitized busy response.
- Modify `src/polytrading/web/assets/index.html`, `app.js`, and `app.css`: add the trial control room
  and bounded busy retries.
- Modify `README.md`: manual smoke test, scheduler, disk-volume, health, dashboard, and economics
  handoff guidance.
- Create `tests/trial/` focused helpers and tests; modify existing storage, economics, CLI, package,
  and web tests where their public contracts grow.

## Requirement-to-Task Map

- Strict funding timing, item/cycle invariants, and warnings: Task 1.
- Migration, append-only storage, cutoff-safe reads, fee inventory, and counts: Task 2.
- Bounded local writer ownership and exception-safe release: Task 3.
- Exact candidate funding collection, rendering, and CLI: Task 4.
- Prospective funding linkage, conflict failure, and economics restriction: Task 5.
- Prepare/persist book lifecycle, fixed venue runner, and one-minute burst CLI: Task 6.
- Strict readiness contracts, window identities, and status invariants: Task 7.
- Read-only health audit, no-look-ahead books, projections, renderers, and CLI: Task 8.
- Dashboard snapshot, recipes, inventory, and sanitized database-busy API: Task 9.
- Trial control-room markup, rendering, accessibility, and retry behavior: Task 10.
- Portable scheduling, disk guidance, package audit, full verification, and browser smoke: Task 11.

---

### Task 1: Strict candidate funding evidence contracts

**Files:**

- Create: `src/polytrading/trial/__init__.py`
- Create: `src/polytrading/trial/funding_models.py`
- Create: `tests/trial/__init__.py`
- Create: `tests/trial/funding_helpers.py`
- Create: `tests/trial/test_funding_models.py`

**Interfaces:**

- Consumes: `Asset`, `Venue`, `StrictRecord`, `normalize_utc_timestamp`, and SHA-256 conventions
  from `polytrading.domain.models`.
- Produces:
  - `TRIAL_FUNDING_PROTOCOL_VERSION = "lighter-dydx-prospective-funding-v1"`.
  - `TRIAL_FUNDING_POINT_IN_TIME_LAG = timedelta(minutes=5)`.
  - `TRIAL_FUNDING_WARNINGS: tuple[str, str, str]`.
  - `Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]`.
  - `TrialInstrumentOutcome`, `TrialFundingOutcome`, and `TrialFundingCycleStatus`.
  - `validate_trial_cycle_timing(cycle_end, now) -> tuple[datetime, datetime, bool]`.
  - `resolve_current_trial_cycle_end(now) -> datetime`.
  - `LighterDydxFundingItem` and `LighterDydxFundingCycle`.
- Consumers: Tasks 2, 4, 5, 7, 8, and 9 import these exact names.

- [ ] **Step 1: Write failing timing and enum tests**

Create tests with these exact boundary assertions:

```python
def test_current_trial_boundary_uses_one_aware_utc_floor() -> None:
    eastern = timezone(-timedelta(hours=4))
    now = datetime(2026, 8, 14, 3, 59, 59, 999999, tzinfo=eastern)

    assert resolve_current_trial_cycle_end(now) == datetime(2026, 8, 14, 7, tzinfo=UTC)


@pytest.mark.parametrize(
    ("offset", "late"),
    [
        (timedelta(0), False),
        (timedelta(minutes=5), False),
        (timedelta(minutes=5, microseconds=1), True),
    ],
)
def test_trial_cycle_timing_has_an_inclusive_five_minute_window(
    offset: timedelta, late: bool
) -> None:
    cycle_end = datetime(2026, 8, 14, 7, tzinfo=UTC)
    _, _, actual = validate_trial_cycle_timing(cycle_end, cycle_end + offset)
    assert actual is late
```

Also reject naive values, a non-hour-aligned boundary, and a clock before the boundary.

- [ ] **Step 2: Run the focused test and observe the missing module**

Run:

```bash
.venv/bin/python -m pytest tests/trial/test_funding_models.py -q
```

Expected: collection fails because `polytrading.trial.funding_models` does not exist.

- [ ] **Step 3: Implement constants, enums, symbol mapping, and timing helpers**

Use exact public values:

```python
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
TRIAL_FUNDING_PROTOCOL_VERSION = "lighter-dydx-prospective-funding-v1"
TRIAL_FUNDING_POINT_IN_TIME_LAG = timedelta(minutes=5)
TRIAL_FUNDING_WARNINGS = (
    "Research only: this cycle measures prospective public funding evidence, not returns.",
    "Readiness does not authorize paper or live trading.",
    "No credentials, accounts, balances, positions, orders, fills, or transfers were accessed.",
)

_EXPECTED_VENUES = (Venue.DYDX, Venue.LIGHTER)
_EXPECTED_SYMBOLS = {
    Venue.DYDX: {asset: f"{asset.value}-USD" for asset in Asset},
    Venue.LIGHTER: {asset: asset.value for asset in Asset},
}

class TrialInstrumentOutcome(StrEnum):
    CAPTURED = "captured"
    FAILED = "failed"
    LATE_NOT_COLLECTED = "late_not_collected"

class TrialFundingOutcome(StrEnum):
    CAPTURED = "captured"
    MISSING_EXPECTED = "missing_expected"
    FAILED = "failed"
    LATE_NOT_COLLECTED = "late_not_collected"

class TrialFundingCycleStatus(StrEnum):
    COMPLETE = "complete"
    DEGRADED = "degraded"
    LATE = "late"
```

`resolve_current_trial_cycle_end` must normalize first and then replace minute, second, and
microsecond with zero. `validate_trial_cycle_timing` must return normalized boundary, normalized
clock, and the late flag.

- [ ] **Step 4: Write failing item and cycle coherence tests**

In `tests/trial/funding_helpers.py`, expose exact deterministic factories:

```python
def trial_funding_item(
    *,
    venue: Venue = Venue.DYDX,
    asset: Asset = Asset.BTC,
    cycle_end: datetime = CYCLE_END,
) -> LighterDydxFundingItem:
    symbol = f"{asset.value}-USD" if venue is Venue.DYDX else asset.value
    return LighterDydxFundingItem(
        schema_version=1,
        venue=venue,
        asset=asset,
        symbol=symbol,
        instrument_outcome=TrialInstrumentOutcome.CAPTURED,
        funding_outcome=TrialFundingOutcome.CAPTURED,
        instrument_observed_at=cycle_end + timedelta(seconds=11),
        funding_effective_at=cycle_end,
        funding_observed_at=cycle_end + timedelta(seconds=12),
        instrument_source_hashes=("1" * 64,),
        funding_source_hashes=("2" * 64,),
        reason_codes=(),
    )

def trial_funding_cycle(
    *,
    cycle_id: UUID = CYCLE_ID,
    cycle_end: datetime = CYCLE_END,
    request_started_at: datetime | None = None,
    request_completed_at: datetime | None = None,
    items: tuple[LighterDydxFundingItem, ...] | None = None,
    status: TrialFundingCycleStatus = TrialFundingCycleStatus.COMPLETE,
) -> LighterDydxFundingCycle:
    selected = (
        tuple(
            trial_funding_item(venue=venue, asset=asset, cycle_end=cycle_end)
            for venue in (Venue.DYDX, Venue.LIGHTER)
            for asset in (Asset.BTC, Asset.ETH, Asset.SOL)
        )
        if items is None
        else items
    )
    return LighterDydxFundingCycle(
        schema_version=1,
        protocol_version=TRIAL_FUNDING_PROTOCOL_VERSION,
        cycle_id=cycle_id,
        cycle_end=cycle_end,
        assets=(Asset.BTC, Asset.ETH, Asset.SOL),
        venues=(Venue.DYDX, Venue.LIGHTER),
        request_started_at=request_started_at or cycle_end + timedelta(seconds=10),
        request_completed_at=request_completed_at or cycle_end + timedelta(seconds=20),
        items=selected,
        status=status,
        source_hashes=tuple(sorted({value for item in selected for value in (
            *item.instrument_source_hashes, *item.funding_source_hashes
        )})),
        warnings=TRIAL_FUNDING_WARNINGS,
    )
```

Write tests that reject wrong symbols, wrong venue order, noncanonical assets/items/hashes/reasons,
duplicate or missing venue/asset pairs, inconsistent optional timestamps, captured outcomes without
hashes, missing outcomes without `FUNDING_MISSING_EXPECTED`, unexpected `no_settlement`-style
semantics, item observations outside the request window, a funding effective time different from
the cycle boundary, altered warnings, and a status inconsistent with item outcomes/timing.

- [ ] **Step 5: Implement strict item and cycle models**

Define the exact field contracts:

```python
class LighterDydxFundingItem(StrictRecord):
    schema_version: Literal[1]
    venue: Venue
    asset: Asset
    symbol: str
    instrument_outcome: TrialInstrumentOutcome
    funding_outcome: TrialFundingOutcome
    instrument_observed_at: datetime | None
    funding_effective_at: datetime | None
    funding_observed_at: datetime | None
    instrument_source_hashes: tuple[Sha256, ...]
    funding_source_hashes: tuple[Sha256, ...]
    reason_codes: tuple[str, ...]

class LighterDydxFundingCycle(StrictRecord):
    schema_version: Literal[1]
    protocol_version: Literal["lighter-dydx-prospective-funding-v1"]
    cycle_id: UUID
    cycle_end: datetime
    assets: tuple[Asset, ...]
    venues: tuple[Venue, Venue]
    request_started_at: datetime
    request_completed_at: datetime
    items: tuple[LighterDydxFundingItem, ...]
    status: TrialFundingCycleStatus
    source_hashes: tuple[Sha256, ...]
    warnings: tuple[str, str, str]
```

Derive cycle status as `late` when invocation starts late or any successful component is observed
after the cutoff, otherwise `degraded` when a component is failed/missing, otherwise `complete`.
Require `venues == (Venue.DYDX, Venue.LIGHTER)` and exact canonical Cartesian item coverage.

Item reason validation accepts only `COLLECTION_WINDOW_MISSED` for each late-not-collected
component, `FUNDING_MISSING_EXPECTED` for a successful empty hourly response, one
instrument failure from `f"INSTRUMENT_FAILED:{venue.value}:{type(error).__name__}"`, and one
funding failure from
`f"FUNDING_FAILED:{venue.value}:{asset.value}:{type(error).__name__}"`. Construct these codes from
validated enum values and exception class names; persist no exception message.

- [ ] **Step 6: Add property coverage for canonicalization and time zones**

Use Hypothesis aware datetimes and permutations to prove that model construction either normalizes
to UTC/canonical order or rejects noncanonical serialized input. Assert the union of item source
hashes equals `cycle.source_hashes` for every accepted example.

- [ ] **Step 7: Verify and commit Task 1**

Run:

```bash
.venv/bin/python -m pytest tests/trial/test_funding_models.py -q
.venv/bin/ruff check src/polytrading/trial/funding_models.py tests/trial
.venv/bin/ruff format --check src/polytrading/trial/funding_models.py tests/trial
git add src/polytrading/trial tests/trial
git commit -m "feat(trial): define prospective funding contracts"
```

Expected: focused tests and static checks pass.

---

### Task 2: Candidate-cycle storage and cutoff-safe readers

**Files:**

- Create: `src/polytrading/storage/schema/005_lighter_dydx_trial_operations.sql`
- Modify: `src/polytrading/storage/store.py:1-35,250-300,590-850,925-975`
- Modify: `tests/storage/test_store.py`

**Interfaces:**

- Consumes: `LighterDydxFundingCycle` from Task 1.
- Produces:
  - `DuckDBStore.append_lighter_dydx_funding_cycle(record) -> bool`.
  - `DuckDBStore.lighter_dydx_funding_cycles_between(start, end, known_as_of)`.
  - `DuckDBStore.latest_lighter_dydx_funding_cycle_as_of(as_of)`.
  - `DuckDBStore.reviewed_fee_schedules_as_of(as_of) -> tuple[FeeSchedule, ...]`.
  - evidence count key `lighter_dydx_funding_cycles`.
- Consumers: Tasks 4, 5, 8, and 9.

- [ ] **Step 1: Write failing migration and round-trip tests**

Add this schema assertion and immutable round trip:

```python
def test_current_schema_contains_lighter_dydx_trial_cycles(tmp_path: Path) -> None:
    path = tmp_path / "trial.duckdb"
    store = DuckDBStore(path)
    tables = {row[0] for row in store._connection.execute("SHOW TABLES").fetchall()}
    store.close()

    assert "lighter_dydx_funding_cycles" in tables


def test_lighter_dydx_cycle_round_trip_is_idempotent_and_cutoff_safe(tmp_path: Path) -> None:
    store = DuckDBStore(tmp_path / "trial.duckdb")
    cycle = trial_funding_cycle()

    assert store.append_lighter_dydx_funding_cycle(cycle) is True
    assert store.append_lighter_dydx_funding_cycle(cycle) is False
    assert store.lighter_dydx_funding_cycles_between(
        cycle.cycle_end, cycle.cycle_end, cycle.request_completed_at
    ) == (cycle,)
    assert store.lighter_dydx_funding_cycles_between(
        cycle.cycle_end, cycle.cycle_end, cycle.request_completed_at - timedelta(microseconds=1)
    ) == ()
```

Also assert that changed content under the same UUID raises `ConflictingRecordError`, range and
cutoff ordering are validated, and read-only opening rejects schema version four after migration
five exists.

- [ ] **Step 2: Run storage tests and observe the missing table/API**

Run:

```bash
.venv/bin/python -m pytest tests/storage/test_store.py -q -k 'lighter_dydx or current_schema'
```

Expected: FAIL because migration 005 and the store methods do not exist.

- [ ] **Step 3: Add migration 005**

Create exactly:

```sql
CREATE TABLE lighter_dydx_funding_cycles (
    cycle_id UUID PRIMARY KEY,
    cycle_end TIMESTAMPTZ NOT NULL,
    request_completed_at TIMESTAMPTZ NOT NULL,
    status VARCHAR NOT NULL,
    record_json JSON NOT NULL,
    record_hash VARCHAR NOT NULL,
    CHECK (status IN ('complete', 'degraded', 'late'))
);
```

- [ ] **Step 4: Implement append and cutoff-safe cycle readers**

Follow existing `_normalized_retry` and canonical JSON conventions. The range reader must filter
both boundary and knowledge cutoff in SQL:

```sql
SELECT CAST(record_json AS VARCHAR)
FROM lighter_dydx_funding_cycles
WHERE cycle_end >= ? AND cycle_end <= ? AND request_completed_at <= ?
ORDER BY cycle_end, request_completed_at, cycle_id
```

The latest reader orders by `request_completed_at DESC, cycle_end DESC, cycle_id DESC` and filters
`request_completed_at <= as_of`.

- [ ] **Step 5: Write failing fee-inventory and evidence-count tests**

Insert two dYdX tiers, one Lighter tier, a future fee, and one candidate cycle. Assert:

```python
fees = store.reviewed_fee_schedules_as_of(as_of)
assert tuple((item.venue, item.tier_name) for item in fees) == (
    (Venue.DYDX, "retail"),
    (Venue.DYDX, "volume-1"),
    (Venue.LIGHTER, "standard"),
)
assert store.evidence_counts_as_of(as_of)["lighter_dydx_funding_cycles"] == 1
```

Require one latest applicable row per `(venue, tier_name)`, canonical venue/tier/effective/observed
ordering, and exclusion of future-effective or future-observed rows.

- [ ] **Step 6: Implement fee inventory and count query**

Use a window function partitioned by `(venue, tier_name)` and ordered by
`effective_from DESC, observed_at DESC, source_hash DESC`, filter
`effective_from <= as_of AND observed_at <= as_of`, retain rank one, and construct validated
`FeeSchedule` records. Add the new cycle count beside the existing funding-cycle count.

- [ ] **Step 7: Verify migrations, readers, and commit Task 2**

Run:

```bash
.venv/bin/python -m pytest tests/storage/test_store.py -q
.venv/bin/ruff check src/polytrading/storage/store.py tests/storage/test_store.py
.venv/bin/ruff format --check src/polytrading/storage/store.py tests/storage/test_store.py
git add src/polytrading/storage/schema/005_lighter_dydx_trial_operations.sql src/polytrading/storage/store.py tests/storage/test_store.py
git commit -m "feat(storage): persist Lighter-dYdX trial cycles"
```

Expected: all storage tests pass, including migration and legacy-schema rejection.

---

### Task 3: Bounded local database writer lease

**Files:**

- Create: `src/polytrading/trial/writer_lease.py`
- Create: `tests/trial/test_writer_lease.py`

**Interfaces:**

- Produces:
  - `WriterLeaseUnavailable(RuntimeError)`.
  - `writer_lease_path(database_path: Path) -> Path`.
  - `database_writer_lease(database_path, *, timeout_seconds, poll_seconds=0.05,
    monotonic=time.monotonic, sleep=time.sleep) -> ContextManager[None]`.
- Consumers: Tasks 4 and 6.

- [ ] **Step 1: Write failing validation, contention, and release tests**

Cover exact lock placement and a nested conflict:

```python
def test_writer_lease_lives_beside_the_database(tmp_path: Path) -> None:
    database = tmp_path / "trial.duckdb"
    assert writer_lease_path(database) == tmp_path / "trial.duckdb.writer.lock"


def test_second_writer_times_out_without_entering(tmp_path: Path) -> None:
    database = tmp_path / "trial.duckdb"
    with database_writer_lease(database, timeout_seconds=0):
        with pytest.raises(WriterLeaseUnavailable, match="database writer lease is busy"):
            with database_writer_lease(database, timeout_seconds=0):
                raise AssertionError("contended lease entered")


def test_writer_lease_releases_after_body_failure(tmp_path: Path) -> None:
    database = tmp_path / "trial.duckdb"
    with pytest.raises(RuntimeError, match="boom"):
        with database_writer_lease(database, timeout_seconds=0):
            raise RuntimeError("boom")
    with database_writer_lease(database, timeout_seconds=0):
        pass
```

Reject bool, negative, NaN, and infinite timeouts/poll intervals. Use injected monotonic/sleep to
assert the exact bounded retry sequence without real waiting.

- [ ] **Step 2: Run the focused test and observe the missing module**

Run:

```bash
.venv/bin/python -m pytest tests/trial/test_writer_lease.py -q
```

Expected: collection fails because `polytrading.trial.writer_lease` does not exist.

- [ ] **Step 3: Implement a cross-platform advisory lock adapter**

Keep the public loop independent of OS details:

```python
@contextmanager
def database_writer_lease(
    database_path: Path,
    *,
    timeout_seconds: float,
    poll_seconds: float = 0.05,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> Iterator[None]:
    timeout = _finite_nonnegative(timeout_seconds, "writer lease timeout")
    poll = _finite_positive(poll_seconds, "writer lease poll interval")
    deadline = monotonic() + timeout
    handle = writer_lease_path(database_path).open("a+b")
    try:
        while not _try_lock(handle):
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise WriterLeaseUnavailable("database writer lease is busy")
            sleep(min(poll, remaining))
        yield
    finally:
        _unlock_if_held(handle)
        handle.close()
```

Implement `_try_lock` with
`fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)` on POSIX and a one-byte
`msvcrt.locking` region on Windows; `_unlock_if_held` uses the matching unlock operation. Do not
delete the lock file; OS lock release, not file absence, is ownership truth. Do not create the
parent directory.

- [ ] **Step 4: Add two-process contention coverage**

Use `multiprocessing.get_context("spawn")` and a queue/event handshake so a child holds the lease
while the parent attempts a zero-timeout acquisition. Assert the parent fails, then joins the child
and acquires successfully. Mark no test as timing-dependent beyond a five-second process join.

- [ ] **Step 5: Verify and commit Task 3**

Run:

```bash
.venv/bin/python -m pytest tests/trial/test_writer_lease.py -q
.venv/bin/ruff check src/polytrading/trial/writer_lease.py tests/trial/test_writer_lease.py
.venv/bin/ruff format --check src/polytrading/trial/writer_lease.py tests/trial/test_writer_lease.py
git add src/polytrading/trial/writer_lease.py tests/trial/test_writer_lease.py
git commit -m "feat(trial): coordinate bounded database writers"
```

Expected: validation, same-process, cross-process, and exception-release tests pass.

---

### Task 4: Candidate funding collector, renderer, and CLI

**Files:**

- Create: `src/polytrading/trial/funding.py`
- Create: `src/polytrading/trial/funding_report.py`
- Create: `tests/trial/test_funding.py`
- Create: `tests/trial/test_funding_report.py`
- Modify: `src/polytrading/cli.py:125-235,270-305,540-720`
- Modify: `tests/test_cli.py`

**Interfaces:**

- Consumes: Task 1 models, Task 2 store append API, Task 3 writer lease, existing
  `PublicVenueAdapter`, `AdapterBatch`, validation, raw/normalized recorder conventions, dYdX and
  Lighter public adapters, and bounded HTTP client configuration.
- Produces:
  - `PreparedLighterDydxFundingCycle(raw, instruments, funding, cycle)`.
  - `LighterDydxFundingCollector.prepare_once(adapters: Iterable[PublicVenueAdapter],
    assets: frozenset[Asset], cycle_end: datetime) -> PreparedLighterDydxFundingCycle`.
  - `record_late_lighter_dydx_cycle(assets: frozenset[Asset], cycle_end: datetime, now: datetime,
    cycle_id_factory: Callable[[], UUID] = uuid4) -> LighterDydxFundingCycle`.
  - `persist_lighter_dydx_funding_cycle(store: DuckDBStore,
    prepared: PreparedLighterDydxFundingCycle) -> bool`.
  - `render_trial_funding_text(cycle: LighterDydxFundingCycle) -> str` and
    `render_trial_funding_json(cycle: LighterDydxFundingCycle) -> str`.
  - CLI example `trial funding --db var/lighter-dydx-trial.duckdb --cycle-end
    2026-08-14T07:00:00Z --format json`; `--current` is its mutually exclusive alternative.
- Consumers: Tasks 5, 8, 9, and 11.

- [ ] **Step 1: Write failing late/no-network and exact-request tests**

Use fake adapters that record calls:

```python
def test_late_trial_cycle_uses_no_adapter_and_has_all_missed_items() -> None:
    cycle = record_late_lighter_dydx_cycle(
        frozenset(Asset),
        CYCLE_END,
        CYCLE_END + timedelta(minutes=5, microseconds=1),
        cycle_id_factory=lambda: CYCLE_ID,
    )
    assert cycle.status is TrialFundingCycleStatus.LATE
    assert all(
        item.funding_outcome is TrialFundingOutcome.LATE_NOT_COLLECTED
        for item in cycle.items
    )


def test_collector_requests_every_exact_lighter_dydx_boundary() -> None:
    adapters = (FakeDydxAdapter(), FakeLighterAdapter())
    prepared = asyncio.run(
        LighterDydxFundingCollector(
            clock=SequenceClock(
                CYCLE_END + timedelta(seconds=10), CYCLE_END + timedelta(seconds=20)
            ),
            cycle_id_factory=lambda: CYCLE_ID,
        ).prepare_once(adapters, frozenset(Asset), CYCLE_END)
    )

    assert [call[1:3] for adapter in adapters for call in adapter.funding_calls] == [
        (CYCLE_END, CYCLE_END),
    ] * 6
    assert prepared.cycle.status is TrialFundingCycleStatus.COMPLETE
```

Test dYdX/Lighter empty responses as `MISSING_EXPECTED`, one asset/venue exception without erasing
other valid evidence, cancellation propagation, wrong interval/symbol/asset/boundary/cardinality,
missing raw lineage, duplicate identities, response timestamps after the cutoff, and canonical
input-order invariance.

- [ ] **Step 2: Run collector tests and observe the missing implementation**

Run:

```bash
.venv/bin/python -m pytest tests/trial/test_funding.py -q
```

Expected: collection fails because `polytrading.trial.funding` does not exist.

- [ ] **Step 3: Implement prepared collection without opening DuckDB**

Use a frozen prepared value:

```python
@dataclass(frozen=True)
class PreparedLighterDydxFundingCycle:
    raw: tuple[RawEnvelope, ...]
    instruments: tuple[InstrumentSpec, ...]
    funding: tuple[FundingObservation, ...]
    cycle: LighterDydxFundingCycle
```

`prepare_once` must validate its on-time start, sort unique adapters as dYdX/Lighter, fetch
instrument batches concurrently, then issue every exact-boundary funding request concurrently.
Re-raise `CancelledError`; translate other exceptions to
`f"INSTRUMENT_FAILED:{venue.value}:{type(error).__name__}"` or
`f"FUNDING_FAILED:{venue.value}:{asset.value}:{type(error).__name__}"`. Validate successful
batches before including their raw or normalized records. Build all six items even under partial
failure.

- [ ] **Step 4: Write failing atomic-persistence tests**

Construct one complete prepared cycle and assert raw-first atomic storage:

```python
def test_prepared_cycle_persists_raw_normalized_and_cycle_atomically(tmp_path: Path) -> None:
    store = DuckDBStore(tmp_path / "trial.duckdb")
    prepared = complete_prepared_cycle()

    assert persist_lighter_dydx_funding_cycle(store, prepared) is True
    assert store.lighter_dydx_funding_cycles_between(
        CYCLE_END, CYCLE_END, prepared.cycle.request_completed_at
    ) == (prepared.cycle,)
    assert len(store.funding_revisions_between(
        Venue.DYDX, "BTC-USD", CYCLE_END - timedelta(hours=1), CYCLE_END,
        prepared.cycle.request_completed_at,
    )) == 1
```

Monkeypatch the final cycle append to raise and assert raw, instrument, funding, and cycle table
counts all remain unchanged. Test exact identical persistence retry and conflict rollback.

- [ ] **Step 5: Implement one-transaction persistence**

Use existing normalized routing:

```python
def persist_lighter_dydx_funding_cycle(
    store: DuckDBStore, prepared: PreparedLighterDydxFundingCycle
) -> bool:
    with store.transaction() as transaction:
        for raw in prepared.raw:
            transaction.append_raw(raw)
        for instrument in prepared.instruments:
            transaction.append_instrument(instrument)
        for observation in prepared.funding:
            transaction.append_funding(observation)
        return transaction.append_lighter_dydx_funding_cycle(prepared.cycle)
```

Before the transaction, validate that every prepared normalized record and raw hash is conserved by
the typed cycle. The persisted cycle is the prepared cycle; do not reconstruct timestamps after
database acquisition.

- [ ] **Step 6: Write failing canonical renderer tests**

Assert sorted JSON keys, RFC 3339 `Z`, UUID strings, canonical item order, all three warnings, and
text lines for every venue/asset outcome. Assert the combined renderers do not contain uppercase
`TRADE`, `APPROVED`, `LIVE_ELIGIBLE`, or a return/profit claim.

- [ ] **Step 7: Implement text and JSON renderers**

Follow the existing funding-cycle renderer shape. JSON must use two-space indentation and sorted
keys. Text begins with boundary/status/attempt times, emits one stable item line ordered by venue
then asset, and ends with the exact three warnings.

- [ ] **Step 8: Write failing CLI parser, dispatch, and exit tests**

Test these exact contracts:

```python
parsed = cli.build_parser().parse_args([
    "trial", "funding", "--current", "--db", "var/trial.duckdb", "--format", "json",
])
assert parsed.command == "trial"
assert parsed.trial_command == "funding"
assert parsed.current is True
assert not hasattr(parsed, "venue")
assert not hasattr(parsed, "assets")
```

Reject neither/both current and explicit modes. Assert `--current` reads `_utc_now` once for
boundary/timing routing. Assert early input returns two before database/client creation; late mode
opens no client and persists a late cycle; complete/degraded/late persisted cycles return zero;
lease/store/network failure preventing persistence returns one; malformed timestamp returns two.

- [ ] **Step 9: Implement exact-pair adapter session and CLI routing**

Add a private async context manager in `cli.py` that creates only `DydxPublicAdapter` and
`LighterPublicAdapter` with independent bounded public clients and always closes both clients.
For on-time mode, acquire the writer lease before opening clients, but do not open DuckDB. Bound
lease waiting by the remaining point-in-time window and revalidate timing after acquisition; if the
window closed while waiting, append a late diagnostic without opening clients. Otherwise collect
in memory while holding only the writer lease, then open `DuckDBStore`, persist even when a response
made the prepared cycle late, close the store, release, and render. For an invocation already late,
construct first and persist without opening clients. Do not create an assets or venue option.

- [ ] **Step 10: Verify and commit Task 4**

Run:

```bash
.venv/bin/python -m pytest tests/trial/test_funding.py tests/trial/test_funding_report.py tests/test_cli.py -q -k 'trial or lighter_dydx'
.venv/bin/ruff check src/polytrading/trial/funding.py src/polytrading/trial/funding_report.py src/polytrading/cli.py tests/trial tests/test_cli.py
.venv/bin/ruff format --check src/polytrading/trial/funding.py src/polytrading/trial/funding_report.py src/polytrading/cli.py tests/trial tests/test_cli.py
git add src/polytrading/trial/funding.py src/polytrading/trial/funding_report.py src/polytrading/cli.py tests/trial tests/test_cli.py
git commit -m "feat(trial): collect prospective funding cycles"
```

Expected: focused collector, report, and CLI tests pass.

---

### Task 5: Binding prospective funding lineage in economics

**Files:**

- Create: `src/polytrading/trial/funding_lineage.py`
- Create: `tests/trial/test_funding_lineage.py`
- Modify: `src/polytrading/carry/economics_assembler.py:258-310`
- Modify: `tests/carry/test_economics_assembler.py`

**Interfaces:**

- Consumes: Task 2 candidate-cycle range reader, existing normalized funding revisions, and the
  exact item identities from Task 1.
- Produces:
  - `SelectedProspectiveFunding(cycle_id, observation)`.
  - `ProspectiveFundingSelection(observations, selected_cycle_ids, conflict_boundaries,
    source_hashes)`.
  - `select_prospective_funding(store: DuckDBStore, venue: Venue, symbol: str, asset: Asset,
    start: datetime, end: datetime, known_as_of: datetime) -> ProspectiveFundingSelection`.
- The economics assembler must add `FUNDING_REVISION_CONFLICT` and withhold the bundle when any
  venue selection reports a conflict.
- Consumers: Task 8 uses the same selector for health.

- [ ] **Step 1: Write failing eligibility and exclusion tests**

Create one generic historical funding row, one post-window candidate item, and one exact on-time
linked row. Assert only the linked row qualifies:

```python
def test_only_on_time_candidate_linkage_selects_funding(tmp_path: Path) -> None:
    store = DuckDBStore(tmp_path / "trial.duckdb")
    generic, late, eligible = seed_mixed_funding_lineage(store)

    selected = select_prospective_funding(
        store, Venue.DYDX, "BTC-USD", Asset.BTC,
        CYCLE_END - timedelta(hours=1), CYCLE_END, CYCLE_END + timedelta(minutes=5),
    )

    assert selected.observations == (eligible,)
    assert generic.source_hash not in selected.source_hashes
    assert late.source_hash not in selected.source_hashes
```

Test exact mismatches in venue, asset, symbol, effective time, observed time, interval, and funding
source hash. A degraded cycle caused by another asset must not disqualify a captured on-time item.
A late cycle may contribute an item only when that exact item's response is within the cutoff and
the invocation itself began within the window.

- [ ] **Step 2: Write failing same-value retry and conflict tests**

For two linked attempts at one venue/asset/hour, assert identical normalized values select the
earliest observation then UUID. When rates differ, assert no row is selected for that boundary and
`conflict_boundaries == (CYCLE_END,)`; include both conflicting raw source hashes in lineage.

- [ ] **Step 3: Run lineage tests and observe the missing module**

Run:

```bash
.venv/bin/python -m pytest tests/trial/test_funding_lineage.py -q
```

Expected: collection fails because `polytrading.trial.funding_lineage` does not exist.

- [ ] **Step 4: Implement binding selection and conflict detection**

Use immutable dataclasses:

```python
@dataclass(frozen=True)
class SelectedProspectiveFunding:
    cycle_id: UUID
    observation: FundingObservation

@dataclass(frozen=True)
class ProspectiveFundingSelection:
    observations: tuple[FundingObservation, ...]
    selected_cycle_ids: tuple[UUID, ...]
    conflict_boundaries: tuple[datetime, ...]
    source_hashes: tuple[str, ...]
```

Load candidate cycles and normalized revisions by cutoff, construct exact item-to-row matches, and
group them by boundary in the start-exclusive, end-inclusive interval `(start, end]`, matching the
economics assembler's expected hourly windows. Define value equality as exact equality of venue,
symbol, asset, rate, interval, and effective timestamp; observation/source/cycle identities may
differ. Conflicts omit the boundary from `observations` and retain every conflicting external hash.

- [ ] **Step 5: Write failing economics-assembler regressions**

Update the complete assembler fixture to append candidate cycles linking all 2,160 funding hours.
Then add:

```python
def test_economics_ignores_complete_unlinked_historical_funding(tmp_path: Path) -> None:
    store, policy = seeded_economics_store(tmp_path, link_trial_funding=False)
    result = EconomicsEvidenceAssembler(store).assemble(policy)
    assert result.bundle is None
    assert "FUNDING_COVERAGE_INSUFFICIENT" in result.reason_codes


def test_economics_fails_closed_on_linked_revision_conflict(tmp_path: Path) -> None:
    store, policy = seeded_economics_store(tmp_path)
    append_conflicting_trial_revision(store, policy.asset, policy.study_end)
    result = EconomicsEvidenceAssembler(store).assemble(policy)
    assert result.bundle is None
    assert "FUNDING_REVISION_CONFLICT" in result.reason_codes
```

Retain all existing exact-168-hour, 99%-coverage, point-in-time, and source-hash tests.

- [ ] **Step 6: Replace unrestricted assembler funding reads**

In `_funding`, call `select_prospective_funding` separately for dYdX and Lighter instead of
`funding_revisions_between`. Add every selection source hash. Add `FUNDING_REVISION_CONFLICT` when
either conflict tuple is nonempty. Pair only the selected exact hourly observations; never fall
back to generic rows.

- [ ] **Step 7: Verify focused economics and commit Task 5**

Run:

```bash
.venv/bin/python -m pytest tests/trial/test_funding_lineage.py tests/carry/test_economics_assembler.py -q
.venv/bin/ruff check src/polytrading/trial/funding_lineage.py src/polytrading/carry/economics_assembler.py tests/trial/test_funding_lineage.py tests/carry/test_economics_assembler.py
.venv/bin/ruff format --check src/polytrading/trial/funding_lineage.py src/polytrading/carry/economics_assembler.py tests/trial/test_funding_lineage.py tests/carry/test_economics_assembler.py
git add src/polytrading/trial/funding_lineage.py src/polytrading/carry/economics_assembler.py tests/trial/test_funding_lineage.py tests/carry/test_economics_assembler.py
git commit -m "fix(carry): require prospective trial funding lineage"
```

Expected: linked complete fixtures pass; generic and conflicting funding fail closed.

---

### Task 6: Detached synchronized-book bursts

**Files:**

- Modify: `src/polytrading/venues/synchronized.py:65-210`
- Create: `src/polytrading/trial/books.py`
- Modify: `src/polytrading/cli.py:200-235,285-305,820-890`
- Modify: `tests/venues/test_synchronized.py`
- Create: `tests/trial/test_books.py`
- Modify: `tests/test_cli.py`

**Interfaces:**

- Produces from `venues.synchronized`:
  - `PreparedBookCollectionCycle(raw_records, books, cycle, warnings)`.
  - `SynchronizedBookCollector.prepare_once(adapters: Iterable[PublicVenueAdapter],
    assets: frozenset[Asset], observed_at: datetime) -> PreparedBookCollectionCycle`.
  - `persist_prepared_book_cycle(store: BookCollectionStore,
    prepared: PreparedBookCollectionCycle) -> bool`.
  - Existing `collect_once(adapters, assets, observed_at) -> BookCollectionCycle` remains backward
    compatible.
- Produces from `trial.books`:
  - `TrialBookRunSummary(attempted_cycles, persisted_cycles, failed_cycles,
    skewed_cycles, lease_skipped_cycles)`.
  - `run_trial_book_session(adapters: Iterable[PublicVenueAdapter], database_path: Path, *,
    duration_seconds: float | None, interval_seconds: float, monotonic: Callable[[], float],
    wall_clock: Callable[[], datetime], sleep: Callable[[float], Awaitable[None]],
    store_factory: Callable[[Path], DuckDBStore]) -> TrialBookRunSummary`.
  - CLI example `trial books --db var/lighter-dydx-trial.duckdb --duration-seconds 60
    --interval-seconds 5`; `--once` is its mutually exclusive alternative.
- Consumers: Tasks 8, 9, and 11.

- [ ] **Step 1: Write failing prepare/persist compatibility tests**

Extend synchronized collector tests:

```python
def test_prepare_once_has_no_store_side_effect() -> None:
    store = RecordingStore()
    collector = SynchronizedBookCollector(store=None, clock=SequenceClock(NOW, LATER))
    prepared = asyncio.run(collector.prepare_once(complete_adapters(), frozenset(Asset), NOW))

    assert prepared.cycle.status == "complete"
    assert store.events == []


def test_prepared_book_cycle_persists_in_one_transaction() -> None:
    store = RecordingStore()
    prepared = complete_prepared_book_cycle()
    assert persist_prepared_book_cycle(store, prepared) is True
    assert store.events[0] == "begin"
    assert store.events[-1] == "commit"
```

Keep the existing `collect_once` tests unchanged and assert it delegates to prepare then persist.
Test rollback when cycle append fails.

- [ ] **Step 2: Run synchronized tests and observe missing prepare API**

Run:

```bash
.venv/bin/python -m pytest tests/venues/test_synchronized.py -q
```

Expected: FAIL because `PreparedBookCollectionCycle` and `prepare_once` do not exist.

- [ ] **Step 3: Refactor synchronized collection into preparation and persistence**

Move all network, validation, sorting, warning, and cycle construction logic into `prepare_once`:

```python
@dataclass(frozen=True)
class PreparedBookCollectionCycle:
    raw_records: tuple[RawEnvelope, ...]
    books: tuple[Level2BookSnapshot, ...]
    cycle: BookCollectionCycle
    warnings: tuple[AdapterWarning, ...]
```

`persist_prepared_book_cycle` performs the existing raw/book/cycle transaction. The constructor
accepts `store: BookCollectionStore | None`; `collect_once` requires a configured store and keeps
its prior return/warning behavior. Preparation emits no warning until the caller chooses to persist
or report the prepared result.

- [ ] **Step 4: Write failing fixed-scope session tests**

Use fake adapter and store factories to prove:

```python
def test_trial_book_session_closes_store_between_five_second_samples(tmp_path: Path) -> None:
    stores = RecordingStoreFactory()
    clock = AdvancingClock()
    summary = asyncio.run(
        run_trial_book_session(
            complete_trial_adapters(), tmp_path / "trial.duckdb",
            duration_seconds=11, interval_seconds=5,
            monotonic=clock.monotonic, wall_clock=lambda: NOW,
            sleep=clock.sleep, store_factory=stores,
        )
    )
    assert summary.attempted_cycles == 3
    assert summary.persisted_cycles == 3
    assert stores.events == ["open", "close", "open", "close", "open", "close"]
```

Assert exact adapter venues `(DYDX, LIGHTER)`, exact assets BTC/ETH/SOL, at most 12 attempts in a
60-second zero-latency session, `--once` exactly one attempt, lease contention increments
`lease_skipped_cycles` without opening DuckDB, failed/skewed counts, bounded failure backoff,
deadline behavior, and cancellation cleanup.

- [ ] **Step 5: Implement the fixed trial runner**

Define:

```python
@dataclass(frozen=True)
class TrialBookRunSummary:
    attempted_cycles: int
    persisted_cycles: int
    failed_cycles: int
    skewed_cycles: int
    lease_skipped_cycles: int
```

For each sample, call `prepare_once`, acquire
`database_writer_lease(database_path, timeout_seconds=0)`, open `DuckDBStore`, persist once, and
close in `finally`. Use monotonic start/deadline and the existing exponential network-failure
backoff capped at 30 seconds. Do not run catch-up cycles after a slow request.

- [ ] **Step 6: Write failing parser and CLI routing tests**

Assert mutually exclusive `--once`/`--duration-seconds`, finite positive duration/interval,
absence of venue/assets options, exact-pair client construction, stable completion summary, and
exit one when no attempted cycle can persist because of database/lease failure. A session with at
least one persisted failed/skewed diagnostic still exits zero and prints counts.

- [ ] **Step 7: Implement `trial books` CLI**

Reuse the Task 4 exact-pair adapter context manager. Map `--once` to `duration_seconds=None`; use
default interval `5.0`. Print one stable line with all five summary counts and the exact no-
authority warning. Leave the existing `collect books` command behavior unchanged.

- [ ] **Step 8: Verify and commit Task 6**

Run:

```bash
.venv/bin/python -m pytest tests/venues/test_synchronized.py tests/trial/test_books.py tests/test_cli.py -q -k 'synchronized or trial_books'
.venv/bin/ruff check src/polytrading/venues/synchronized.py src/polytrading/trial/books.py src/polytrading/cli.py tests/venues/test_synchronized.py tests/trial/test_books.py tests/test_cli.py
.venv/bin/ruff format --check src/polytrading/venues/synchronized.py src/polytrading/trial/books.py src/polytrading/cli.py tests/venues/test_synchronized.py tests/trial/test_books.py tests/test_cli.py
git add src/polytrading/venues/synchronized.py src/polytrading/trial/books.py src/polytrading/cli.py tests/venues/test_synchronized.py tests/trial/test_books.py tests/test_cli.py
git commit -m "feat(trial): collect bounded synchronized book bursts"
```

Expected: old generic collector tests and new detached-session tests pass.

---

### Task 7: Strict trial-health and readiness contracts

**Files:**

- Create: `src/polytrading/trial/health_models.py`
- Create: `tests/trial/test_health_models.py`

**Interfaces:**

- Consumes: Task 1 funding-cycle types, existing `Asset`, `Venue`, `StrictRecord`,
  `FeeSchedule`, and economics decision summaries.
- Produces:
  - `TRIAL_HEALTH_PROTOCOL_VERSION = "lighter-dydx-trial-health-v1"`.
  - `TRIAL_HEALTH_WARNINGS: tuple[str, str, str]`.
  - `TrialCollectionStatus` and `TrialEvidenceStatus`.
  - `resolve_latest_auditable_trial_boundary(as_of) -> datetime`.
  - `trial_window_boundaries(study_end) -> TrialWindowBoundaries`.
  - `TrialBoundaryAssetHealth`, `TrialBoundaryHealth`, `TrialAssetCoverage`,
    `ReviewedFeeEvidenceSummary`, and `LighterDydxTrialHealthReport`.
- Consumers: Tasks 8–10.

- [ ] **Step 1: Write failing boundary and fixed-window tests**

Use exact cutoff/window identities:

```python
@pytest.mark.parametrize(
    ("as_of", "latest"),
    [
        (at(17, 4, 59, 999999), at(16)),
        (at(17, 5), at(17)),
    ],
)
def test_latest_auditable_trial_boundary_closes_at_five_minutes(
    as_of: datetime, latest: datetime
) -> None:
    assert resolve_latest_auditable_trial_boundary(as_of) == latest


def test_trial_windows_have_exact_frozen_lengths() -> None:
    windows = trial_window_boundaries(at(17))
    assert len(windows.training_funding) == 720
    assert len(windows.evaluation_funding) == 1_440
    assert len(windows.total_funding) == 2_160
    assert len(windows.evaluation_books) == 1_440
    assert len(windows.current_funding) == 168
    assert windows.total_funding[-1] == at(17)
```

Reject naive and pre-epoch cutoffs and non-hour study ends. Prove every tuple is strictly
consecutive and training/evaluation partition total funding without overlap.

- [ ] **Step 2: Run health-model tests and observe the missing module**

Run:

```bash
.venv/bin/python -m pytest tests/trial/test_health_models.py -q
```

Expected: collection fails because `polytrading.trial.health_models` does not exist.

- [ ] **Step 3: Implement enums, warnings, and window helpers**

Use exact status values:

```python
NonnegativeInt = Annotated[int, Field(ge=0)]
NonnegativeDecimal = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
Fraction = Annotated[Decimal, Field(ge=0, le=1, allow_inf_nan=False)]
TRIAL_HEALTH_PROTOCOL_VERSION = "lighter-dydx-trial-health-v1"
TRIAL_HEALTH_WARNINGS = (
    "Research only: this report measures public evidence collection, not expected returns.",
    "READY_FOR_ECONOMICS_EVALUATION does not authorize paper or live trading.",
    "No credentials, accounts, balances, positions, orders, fills, or transfers were accessed.",
)

class TrialCollectionStatus(StrEnum):
    NOT_STARTED = "NOT_STARTED"
    COLLECTING = "COLLECTING"
    DEGRADED = "DEGRADED"
    READY_FOR_ECONOMICS_EVALUATION = "READY_FOR_ECONOMICS_EVALUATION"

class TrialEvidenceStatus(StrEnum):
    MISSING = "missing"
    LATE = "late"
    DEGRADED = "degraded"
    COMPLETE = "complete"

@dataclass(frozen=True)
class TrialWindowBoundaries:
    training_funding: tuple[datetime, ...]
    evaluation_funding: tuple[datetime, ...]
    total_funding: tuple[datetime, ...]
    evaluation_books: tuple[datetime, ...]
    current_funding: tuple[datetime, ...]
```

Generate boundaries ending at `study_end` with the first item one hour after each window start.
Use the same inclusive five-minute close as the funding protocol.

- [ ] **Step 4: Write failing strict nested-model tests**

Define and test exact public fields:

```python
class TrialBoundaryAssetHealth(StrictRecord):
    schema_version: Literal[1]
    asset: Asset
    funding_status: TrialEvidenceStatus
    book_status: TrialEvidenceStatus
    selected_funding_cycle_ids: tuple[UUID, ...]
    selected_book_cycle_id: UUID | None
    reason_codes: tuple[str, ...]

class TrialBoundaryHealth(StrictRecord):
    schema_version: Literal[1]
    cycle_end: datetime
    status: TrialEvidenceStatus
    attempt_count: NonnegativeInt
    complete_attempt_count: NonnegativeInt
    degraded_attempt_count: NonnegativeInt
    late_attempt_count: NonnegativeInt
    assets: tuple[TrialBoundaryAssetHealth, ...]
    reason_codes: tuple[str, ...]

class ReviewedFeeEvidenceSummary(StrictRecord):
    schema_version: Literal[1]
    venue: Venue
    tier_name: str
    effective_from: datetime
    observed_at: datetime
    source_hash: Sha256
```

Require canonical BTC/ETH/SOL asset order, canonical unique cycle IDs/reasons, exact attempt-count
sum, status equal to the conservative minimum of asset funding/book statuses, exact duplicate/
missing/degraded/late reason codes, supported fee venues, nonblank tiers, and canonical fee order.

- [ ] **Step 5: Implement `TrialAssetCoverage` with recomputed identities**

Use exact fields:

```python
class TrialAssetCoverage(StrictRecord):
    schema_version: Literal[1]
    asset: Asset
    requested_training_funding_hours: Literal[720]
    paired_training_funding_hours: NonnegativeInt
    training_funding_coverage: Fraction
    missing_training_funding_boundaries: tuple[datetime, ...]
    requested_evaluation_funding_hours: Literal[1440]
    paired_evaluation_funding_hours: NonnegativeInt
    evaluation_funding_coverage: Fraction
    missing_evaluation_funding_boundaries: tuple[datetime, ...]
    requested_total_funding_hours: Literal[2160]
    paired_total_funding_hours: NonnegativeInt
    total_funding_coverage: Fraction
    requested_book_hours: Literal[1440]
    paired_book_hours: NonnegativeInt
    book_coverage: Fraction
    missing_book_boundaries: tuple[datetime, ...]
    current_funding_paired_hours: NonnegativeInt
    current_funding_consecutive: bool
    missing_current_funding_boundaries: tuple[datetime, ...]
    dense_book_pair_count: NonnegativeInt
    consecutive_dense_sample_count: NonnegativeInt
    latest_funding_boundary: datetime | None
    latest_book_completed_at: datetime | None
    latest_book_age_seconds: NonnegativeDecimal | None
    latest_book_skew_ms: NonnegativeDecimal | None
    fresh_book_ready: bool
    historical_windows_mature: bool
    projected_earliest_evaluation_end: datetime | None
    reason_codes: tuple[str, ...]
```

Validators require every missing-boundary tuple to be UTC, strictly ordered, unique, and inside its
named fixed window; recompute paired counts from requested minus missing, every coverage Decimal,
the total funding pair sum, exact 168-hour flag, `fresh_book_ready` from age at most 30 seconds and
skew at most 1,000 ms, and `historical_windows_mature` from all fixed 99% windows, the exact current
window, and at least one consecutive dense sample. Reasons are the exact applicable subset of:
`FUNDING_TRAINING_COVERAGE_INSUFFICIENT`, `FUNDING_EVALUATION_COVERAGE_INSUFFICIENT`,
`FUNDING_COVERAGE_INSUFFICIENT`, `BOOK_COVERAGE_INSUFFICIENT`,
`CURRENT_FUNDING_WINDOW_INSUFFICIENT`, `LATENCY_SAMPLES_MISSING`, `BOOK_LATEST_MISSING`,
`BOOK_LATEST_STALE`, and `BOOK_LATEST_SKEW_EXCEEDED`.

- [ ] **Step 6: Implement and test the top-level report contract**

Define:

```python
class LighterDydxTrialHealthReport(StrictRecord):
    schema_version: Literal[1]
    protocol_version: Literal["lighter-dydx-trial-health-v1"]
    as_of: datetime
    latest_auditable_boundary: datetime
    recent_hours: Annotated[int, Field(ge=1, le=2160)]
    trial_started_at: datetime | None
    elapsed_auditable_hours: NonnegativeInt
    status: TrialCollectionStatus
    recent_boundaries: tuple[TrialBoundaryHealth, ...]
    assets: tuple[TrialAssetCoverage, ...]
    dossier_available: bool
    reviewed_fees: tuple[ReviewedFeeEvidenceSummary, ...]
    source_hashes: tuple[Sha256, ...]
    warnings: tuple[str, str, str]
```

Tests must reject future evidence, wrong recent boundary span/order, elapsed-count mismatch,
noncanonical assets/fees/hashes, altered warnings, `NOT_STARTED` with a start, collecting/ready
without a start, `READY` unless every asset has mature windows and fresh books, `COLLECTING` when a
recent boundary is degraded, and `DEGRADED` when all recent boundaries are complete. Calendar
immaturity with healthy recent boundaries must validate as `COLLECTING`.

For `NOT_STARTED`, `recent_boundaries` is empty. After start it must cover exactly the intersection
of the requested recent window and boundaries at or after `trial_started_at`; pre-start hours are
not materialized as missing failures. Recompute elapsed hours as
`((latest_auditable_boundary - trial_started_at) // timedelta(hours=1)) + 1`, so the first started
boundary counts as hour one.

- [ ] **Step 7: Add exact 99% and current-window boundary tests**

Assert 712/720 fails and 713/720 passes training, 1,425/1,440 fails and 1,426/1,440 passes
evaluation/books, 2,138/2,160 fails and 2,139/2,160 passes total, and 167/168 always fails current.
Use exact Decimal division in expected values.

- [ ] **Step 8: Verify and commit Task 7**

Run:

```bash
.venv/bin/python -m pytest tests/trial/test_health_models.py -q
.venv/bin/ruff check src/polytrading/trial/health_models.py tests/trial/test_health_models.py
.venv/bin/ruff format --check src/polytrading/trial/health_models.py tests/trial/test_health_models.py
git add src/polytrading/trial/health_models.py tests/trial/test_health_models.py
git commit -m "feat(trial): define evidence readiness contracts"
```

Expected: strict model, exact-window, and threshold-edge tests pass.

---

### Task 8: Read-only trial health auditor, projection, reports, and CLI

**Files:**

- Create: `src/polytrading/trial/book_evidence.py`
- Create: `src/polytrading/trial/health.py`
- Create: `src/polytrading/trial/health_report.py`
- Create: `tests/trial/test_book_evidence.py`
- Create: `tests/trial/test_health.py`
- Create: `tests/trial/test_health_report.py`
- Modify: `src/polytrading/carry/economics_assembler.py:317-415`
- Modify: `tests/carry/test_economics_assembler.py`
- Modify: `src/polytrading/cli.py:180-235,280-305,690-720`
- Modify: `tests/test_cli.py`

**Interfaces:**

- Consumes: Task 5 prospective funding selector, Task 7 health contracts, candidate cycles, generic
  book cycles/snapshots, fee inventory, bundled dossier, and latest economics store reader.
- Produces:
  - `EligibleTrialBookPair(cycle, pair)`.
  - `eligible_lighter_dydx_book_pair(store: DuckDBStore, cycle: BookCollectionCycle,
    asset: Asset, known_as_of: datetime, maximum_skew_ms: Decimal) ->
    EligibleTrialBookPair | None`.
  - `select_hourly_trial_books(store: DuckDBStore, asset: Asset, start: datetime, end: datetime,
    known_as_of: datetime, maximum_age_seconds: Decimal, maximum_skew_ms: Decimal) ->
    tuple[EligibleTrialBookPair, ...]`.
  - `ProjectedAssetEvidence(asset, funding_complete, book_complete)`.
  - `project_earliest_evaluation_end(trial_started_at: datetime | None,
    latest_auditable_boundary: datetime, evidence: tuple[ProjectedAssetEvidence, ...]) ->
    datetime | None`.
  - `LighterDydxTrialHealthAuditor(store: DuckDBStore).audit(as_of: datetime,
    recent_hours: int) -> LighterDydxTrialHealthReport`.
  - `render_trial_health_text(report: LighterDydxTrialHealthReport) -> str` and
    `render_trial_health_json(report: LighterDydxTrialHealthReport) -> str`.
  - CLI example `trial health --db var/lighter-dydx-trial.duckdb --recent-hours 24 --as-of
    2026-08-14T07:06:00Z --format json`; omitting `--as-of` selects one current UTC clock value.
- Consumers: Tasks 9–11.

- [ ] **Step 1: Write failing shared book-eligibility tests**

Seed paired dYdX/Lighter book cycles around one boundary and assert:

```python
def test_hourly_book_selection_never_looks_after_boundary(tmp_path: Path) -> None:
    store = seeded_book_store(tmp_path, completions=(BOUNDARY - timedelta(seconds=1),
                                                    BOUNDARY + timedelta(microseconds=1)))
    selected = select_hourly_trial_books(
        store, Asset.BTC, BOUNDARY - timedelta(hours=1), BOUNDARY, BOUNDARY + timedelta(minutes=5),
        maximum_age_seconds=Decimal("300"), maximum_skew_ms=Decimal("1000"),
    )
    assert len(selected) == 1
    assert selected[0].cycle.request_completed_at == BOUNDARY - timedelta(seconds=1)
```

Test exact five-minute age and 1,000-ms skew inclusion, one microsecond/millisecond over exclusion,
failed/skewed cycle exclusion, missing/duplicate venue-asset books, wrong symbols, missing hashes,
future observation/completion exclusion, and boundary relabeling while nested timestamps remain
unchanged.

- [ ] **Step 2: Implement shared eligible book selection and use it in economics**

Move the semantics of `EconomicsEvidenceAssembler._eligible_cycle_pair` into
`trial.book_evidence` without weakening them. `EligibleTrialBookPair` retains the selected cycle and
a `PairedBookObservation`; label that pair with the exact hour only from
`select_hourly_trial_books`, while preserving source books' own effective/observed times. Update the
assembler to consume each result's `.pair` and delete its private duplicate. Rerun every economics
assembler book-selection regression. Hourly selection treats `start` as exclusive and `end` as
inclusive and returns exactly one representative per eligible UTC hour in that interval.

- [ ] **Step 3: Write failing empty, collecting, degraded, and ready audit tests**

Use deterministic 2,160-hour fixture generators:

```python
def test_empty_trial_is_not_started(tmp_path: Path) -> None:
    store = DuckDBStore(tmp_path / "trial.duckdb")
    report = LighterDydxTrialHealthAuditor(store).audit(AS_OF, 24)
    assert report.status is TrialCollectionStatus.NOT_STARTED
    assert report.trial_started_at is None
    assert report.elapsed_auditable_hours == 0


def test_calendar_immaturity_is_collecting_not_degraded(tmp_path: Path) -> None:
    store = seed_complete_trial_hours(tmp_path, hours=24)
    report = LighterDydxTrialHealthAuditor(store).audit(AS_OF, 24)
    assert report.status is TrialCollectionStatus.COLLECTING
    assert all(item.status is TrialEvidenceStatus.COMPLETE for item in report.recent_boundaries)


def test_recent_missing_book_makes_health_degraded(tmp_path: Path) -> None:
    store = seed_complete_trial_hours(tmp_path, hours=24, missing_book_hours={AS_OF_HOUR})
    report = LighterDydxTrialHealthAuditor(store).audit(AS_OF, 24)
    assert report.status is TrialCollectionStatus.DEGRADED
    assert "BOOK_BOUNDARY_MISSING" in report.recent_boundaries[-1].reason_codes
```

For a full ready fixture, require all three assets, exact linked funding, 99% windows, final 168
complete, eligible hourly books, dense consecutive samples, and latest book age at most 30 seconds.
Assert one asset's failure prevents top-level readiness but leaves other per-asset rows complete.

- [ ] **Step 4: Implement boundary aggregation and trial-start semantics**

`audit` captures normalized `as_of`, validates `recent_hours` as a non-bool integer in 1..2,160,
and derives the latest auditable boundary. Query candidate cycles by cutoff. Trial start is the
earliest auditable boundary whose attempt began within its five-minute window; late-only invocations
do not start it. Do not move start forward because its book is missing.

For every recent boundary, count attempts and classify attempt status. For each asset, call
`select_prospective_funding` for both venues and require an exact pair; call shared hourly book
selection; derive stable reasons and conservative status. Duplicate attempts remain warnings/counts
even when the selected evidence is complete.

Rank attempts `complete`, then `degraded`, then `late`; within the best rank select earliest
`request_completed_at` and then UUID. Boundary reasons use the exact deterministic vocabulary
`BOUNDARY_MISSING`, `BOUNDARY_LATE_ONLY`, `BOUNDARY_DEGRADED_ONLY`, `MULTIPLE_ATTEMPTS`,
`MULTIPLE_COMPLETE_ATTEMPTS`, plus the outputs of these exact helpers:

```python
def funding_missing_reason(asset: Asset) -> str:
    return f"FUNDING_{asset.value}_MISSING"

def funding_conflict_reason(asset: Asset) -> str:
    return f"FUNDING_{asset.value}_REVISION_CONFLICT"

def book_missing_reason(asset: Asset) -> str:
    return f"BOOK_{asset.value}_MISSING"
```

Sort and deduplicate reasons; duplicate warnings alone do not degrade complete evidence.

- [ ] **Step 5: Implement fixed-window coverage and dense/latest book facts**

When elapsed history is shorter than 2,160 hours, compute counts over the fixed window ending at
latest auditable boundary with unavailable pre-start hours remaining missing. Preserve every exact
missing boundary in the corresponding asset tuple. Use the exact 720/1,440/2,160 and final-168
tuples from Task 7. Query eligible dense cycles during the evaluation window; count pairwise
samples whose effective timestamps differ by `(0, 5 seconds]`. Latest book age uses cycle
`request_completed_at`; current depth is fresh only at 30 seconds or less and one-second skew or
less.

Fee summaries come from `reviewed_fee_schedules_as_of`. Dossier availability requires the bundled
`lighter-dydx-core-v1` observation time at or before `as_of`; do not reinterpret its checks here.
The top-level source hashes are the sorted union of selected funding, eligible books, fee evidence,
and dossier excerpts actually known by the cutoff.

- [ ] **Step 6: Write failing projected-boundary tests**

Test four exact cases: no trial start returns `None`; a clean new trial projects start + 2,159
hours; known misses within the 99% allowances preserve that projection; misses beyond an allowance
shift the projection until enough bad boundaries roll out. Treat every future boundary as complete,
scan at most 2,160 hours beyond `as_of`, and require the final 168 future/current hours consecutive.

- [ ] **Step 7: Implement deterministic projection**

Define the exact projection input:

```python
@dataclass(frozen=True)
class ProjectedAssetEvidence:
    asset: Asset
    funding_complete: frozenset[datetime]
    book_complete: frozenset[datetime]
```

The pure function receives trial start, latest auditable boundary, and the canonical BTC/ETH/SOL
tuple of these records. For candidate ends in increasing hourly order, overlay known truth through
the latest auditable boundary and treat only future boundaries as complete. Return the first end
where all three assets meet exact training/evaluation/total/book/current gates; return `None` only
before trial start or when validated datetime arithmetic would leave the supported range.

- [ ] **Step 8: Write failing text/JSON report tests**

Assert canonical JSON scalar formatting and stable recent boundary/asset/fee order. Text must show
status, cutoff, trial start, elapsed/target hours, projection, per-asset four coverage ratios,
current 168, dense/consecutive counts, fresh depth, recent gap reasons, and separate dossier/fee/
policy statements. Assert exact three warnings and forbidden authority strings.

- [ ] **Step 9: Implement health renderers**

Use sorted two-space JSON and RFC 3339 `Z`. Text must call the projected date a
`collection-only projection assuming complete future boundaries` and must state `operator policy:
not assessed`. Never select or recommend a fee tier.

- [ ] **Step 10: Write failing read-only CLI tests**

Assert an existing current-schema database is required and opened with `read_only=True`; no HTTP
client, writer lease, migration, or append method is called. Verify `--recent-hours` 1/2,160 edges,
bool-like/zero/2,161 rejection, explicit historical `--as-of`, text/JSON routing, exit zero for
healthy `COLLECTING`/`READY`, exit one for `NOT_STARTED`/`DEGRADED`, and exit two for invalid or
unavailable storage.

- [ ] **Step 11: Implement `trial health` CLI and exports**

Register the parser under the existing `trial` group and route without `asyncio.run`. Capture
`_utc_now` once only when `--as-of` is absent. Close read-only store in `finally`; sanitize DuckDB
and model reconstruction failures to `trial health database is unavailable or not current`.

- [ ] **Step 12: Verify and commit Task 8**

Run:

```bash
.venv/bin/python -m pytest tests/trial/test_book_evidence.py tests/trial/test_health.py tests/trial/test_health_report.py tests/carry/test_economics_assembler.py tests/test_cli.py -q -k 'trial or economics_assembler'
.venv/bin/ruff check src/polytrading/trial src/polytrading/carry/economics_assembler.py src/polytrading/cli.py tests/trial tests/carry/test_economics_assembler.py tests/test_cli.py
.venv/bin/ruff format --check src/polytrading/trial src/polytrading/carry/economics_assembler.py src/polytrading/cli.py tests/trial tests/carry/test_economics_assembler.py tests/test_cli.py
git add src/polytrading/trial src/polytrading/carry/economics_assembler.py src/polytrading/cli.py tests/trial tests/carry/test_economics_assembler.py tests/test_cli.py
git commit -m "feat(trial): audit prospective evidence readiness"
```

Expected: health, shared book selection, economics, report, and CLI tests pass.

---

### Task 9: Point-in-time dashboard snapshot and database-busy API

**Files:**

- Modify: `src/polytrading/web/models.py:200-285`
- Modify: `src/polytrading/web/dashboard.py:45-185,245-265`
- Modify: `src/polytrading/web/server.py:70-100`
- Modify: `tests/web/test_models.py`
- Modify: `tests/web/test_dashboard.py`
- Modify: `tests/web/test_server.py`

**Interfaces:**

- Consumes: `LighterDydxTrialHealthAuditor`, health report models/render-safe Pydantic values,
  candidate-cycle evidence count, existing economics summaries, and one dashboard `as_of`.
- Produces:
  - `DashboardSnapshot.trial_health: LighterDydxTrialHealthReport`.
  - `EvidenceCounts.lighter_dydx_funding_cycles`.
  - `OperationRecipes.collect_trial_funding`, `collect_trial_books_burst`,
    `collect_trial_books_once`, `inspect_trial_health`, `import_trial_fees`,
    `evaluate_trial_btc`, and `trial_scheduler_example`.
  - API error `503 {"error":{"code":"DATABASE_BUSY"}}` for DuckDB file-lock contention.
- Consumers: Task 10 assets and Task 11 operator documentation.

- [ ] **Step 1: Write failing web-model consistency tests**

Extend the snapshot constructor and assert:

```python
assert snapshot.trial_health.as_of == snapshot.as_of
assert snapshot.evidence_counts.lighter_dydx_funding_cycles >= 0
assert "trial funding --current" in snapshot.operation_recipes.collect_trial_funding
assert "trial books --duration-seconds 60 --interval-seconds 5" in (
    snapshot.operation_recipes.collect_trial_books_burst
)
assert "trial books --once" in snapshot.operation_recipes.collect_trial_books_once
assert "trial health --recent-hours 24" in snapshot.operation_recipes.inspect_trial_health
assert "fees import --input reviewed-fees.json" in snapshot.operation_recipes.import_trial_fees
assert "carry economics --policy policy/BTC.json" in snapshot.operation_recipes.evaluate_trial_btc
assert "58 * * * *" in snapshot.operation_recipes.trial_scheduler_example
```

Reject a trial report with a different cutoff, future trial timestamps, negative new count, blank
recipes, and extra/omitted recipe fields. Preserve shell quoting for spaces and apostrophes in the
database path. Evaluation templates use shell-safe literal tokens `REPLACE_WITH_EVALUATED_AT` and
`REPLACE_WITH_EVALUATION_UUID`, never angle brackets or executable command substitution.

- [ ] **Step 2: Implement strict model additions**

Add `trial_health` after legacy `funding_health`, add the count, and extend `OperationRecipes` with
the seven exact string fields. In `DashboardSnapshot.require_one_point_in_time`, require
`trial_health.as_of == as_of` and every selected trial/evidence timestamp no later than `as_of`.

- [ ] **Step 3: Write failing builder cutoff tests**

For an empty database, assert trial status `NOT_STARTED`, all per-asset values unavailable/zero as
defined by the health model, no invented projection, and new evidence count zero. Seed one on-time
cycle and books plus future cycles; assert only pre-cutoff attempts and snapshots affect health,
counts, and rendered JSON. Assert latest economics remains selected by its existing cutoff and is
not promoted by collection readiness.

- [ ] **Step 4: Build trial health under the dashboard's single cutoff**

In `DashboardBuilder.build`, call:

```python
trial_health = LighterDydxTrialHealthAuditor(self._store).audit(normalized_as_of, 24)
```

Pass it directly into `DashboardSnapshot`, include the new evidence count, and generate shell-
quoted recipes against the same database path. Keep the dashboard read-only; do not catch health
validation and replace it with zeros.

- [ ] **Step 5: Write failing sanitized busy-response tests**

Monkeypatch `DuckDBStore` to raise `duckdb.IOException` with and without the lock phrase:

```python
def test_dashboard_distinguishes_database_lock_without_leaking_path(
    database_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def busy(*_args: object, **_kwargs: object) -> NoReturn:
        raise duckdb.IOException(f"Could not set lock on file {database_path}")
    monkeypatch.setattr(web_server, "DuckDBStore", busy)

    response = DashboardApplication(database_path, clock=lambda: AS_OF).respond(
        "GET", "/api/v1/dashboard", "127.0.0.1:8787"
    )
    assert response.status is HTTPStatus.SERVICE_UNAVAILABLE
    assert json.loads(response.body) == {"error": {"code": "DATABASE_BUSY"}}
    assert str(database_path).encode() not in response.body
```

Non-lock DuckDB/OSError/schema failures remain `DATABASE_UNAVAILABLE`; unexpected errors remain
`INTERNAL_ERROR`. Security/cache/connection headers are unchanged.

- [ ] **Step 6: Implement narrow busy classification**

Add a private predicate that returns true only for a `duckdb.IOException` whose string contains
`Could not set lock on file`, matching DuckDB 1.5.4 lock failures. The response body contains only
the stable code. Do not return exception text.

- [ ] **Step 7: Verify and commit Task 9**

Run:

```bash
.venv/bin/python -m pytest tests/web/test_models.py tests/web/test_dashboard.py tests/web/test_server.py -q
.venv/bin/ruff check src/polytrading/web tests/web
.venv/bin/ruff format --check src/polytrading/web tests/web
git add src/polytrading/web/models.py src/polytrading/web/dashboard.py src/polytrading/web/server.py tests/web/test_models.py tests/web/test_dashboard.py tests/web/test_server.py
git commit -m "feat(web): expose prospective trial health"
```

Expected: model, point-in-time builder, serialization, and sanitized error tests pass.

---

### Task 10: Read-only trial control-room UI and bounded retries

**Files:**

- Modify: `src/polytrading/web/assets/index.html`
- Modify: `src/polytrading/web/assets/app.js`
- Modify: `src/polytrading/web/assets/app.css`
- Modify: `tests/web/test_assets.py`

**Interfaces:**

- Consumes: `snapshot.trial_health`, new evidence count, and seven trial operation recipes from
  Task 9.
- Produces: semantic `#trial` section, trial progress/status rendering, 24-hour funding/book matrix,
  stale snapshot badge, and exact retry delays 250/500/1,000 ms for `DATABASE_BUSY` only.

- [ ] **Step 1: Write failing semantic markup and safety tests**

Require these IDs and navigation target:

```python
assert {
    "trial", "trial-summary", "trial-asset-rows", "trial-boundary-rows",
    "trial-gap-reasons", "trial-fees",
}.issubset(ids)
assert any(tag == "a" and attrs.get("href") == "#trial" for tag, attrs in tags)
```

Require headings/copy `Lighter-dYdX prospective trial`, `collection-only projection`, `Operator
policy not assessed`, `Research only`, and `No trading authority`. Continue to reject forms,
mutation methods, remote URLs, websocket/event-source clients, passwords, API keys, and unsafe DOM
insertion APIs.

- [ ] **Step 2: Add trial section markup**

Insert the trial section after the system overview and renumber later visible section indices. Use
a summary card grid, a three-row asset coverage table, a recent-boundary table/strip, reason list,
and reviewed-fee inventory. Every empty container has `aria-live="polite"` only where updates need
announcement; table headings use `scope="col"`.

- [ ] **Step 3: Write failing snapshot validation and render-token tests**

Assert JavaScript requires `snapshot.trial_health`, exactly three canonical asset rows, and an
array of recent boundaries. Require functions named `renderTrial`, `renderTrialAssets`, and
`renderTrialBoundaries`; require visible `Unavailable` for null projection/timestamps and exact
status tones for all four top-level states.

- [ ] **Step 4: Implement trial rendering with safe DOM construction**

Extend `nodes`, then render:

```javascript
const trialTones = {
  NOT_STARTED: "missing",
  COLLECTING: "collecting",
  DEGRADED: "degraded",
  READY_FOR_ECONOMICS_EVALUATION: "ready",
};

function renderTrial(snapshot) {
  const trial = snapshot.trial_health;
  const projections = trial.assets.map((item) => item.projected_earliest_evaluation_end);
  const projected = projections.every((value) => value)
    ? [...projections].sort().at(-1)
    : null;
  nodes.trialSummary.replaceChildren(
    statusCard("Trial status", trial.status, `As of ${compactTime(trial.as_of)}`, trialTones[trial.status]),
    statusCard("Elapsed evidence", `${trial.elapsed_auditable_hours}/2160 hours`,
      trial.trial_started_at ? `Started ${compactTime(trial.trial_started_at)}` : "Not started",
      trialTones[trial.status]),
    statusCard("Projected evaluation", compactTime(projected),
      "Collection-only projection assuming complete future boundaries", trialTones[trial.status]),
  );
  renderTrialAssets(trial);
  renderTrialBoundaries(trial);
}
```

Render all asset-specific projections rather than assuming they are equal in the final table. Show
funding/book status per boundary, exact reasons, dense/consecutive counts, fresh-depth state,
dossier availability, fee venue/tier/effective/observed values, and `Operator policy not assessed`.
Extend `recipeLabels` with all seven Task 9 recipe keys so scheduler, fee-import, and economics
templates are rendered as copy-only cards. Use only `textContent`, `createElement`, and
`replaceChildren`.

- [ ] **Step 5: Write failing bounded database-busy retry tests as source invariants**

Assert the asset source contains an exact delay tuple and retries only the busy code:

```python
assert "const databaseBusyRetryMs = [250, 500, 1000];" in javascript
assert 'code !== "DATABASE_BUSY"' in javascript
assert "state.lastSnapshot" in javascript
assert "Stale" in javascript
```

Also require one abort controller for the whole bounded attempt sequence and preservation of the
last rendered snapshot after exhausted retries.

- [ ] **Step 6: Implement retry-aware fetch without recursive refresh**

Extract:

```javascript
const databaseBusyRetryMs = [250, 500, 1000];
const wait = (milliseconds) => new Promise((resolve) => window.setTimeout(resolve, milliseconds));

async function fetchSnapshot(signal) {
  for (let attempt = 0; ; attempt += 1) {
    const response = await fetch("/api/v1/dashboard", {
      headers: { Accept: "application/json" }, signal, cache: "no-store",
    });
    const document = await response.json();
    const code = document?.error?.code;
    if (response.ok) return validateSnapshot(document);
    if (code !== "DATABASE_BUSY" || attempt === databaseBusyRetryMs.length) {
      throw new Error(code || "REFRESH_FAILED");
    }
    setStatus(state.lastSnapshot ? "Stale · database busy; retrying…" : "Database busy; retrying…", "stale");
    await wait(databaseBusyRetryMs[attempt]);
  }
}
```

`refreshSnapshot` keeps its 10-second abort budget, calls `fetchSnapshot`, replaces
`state.lastSnapshot` only after validation, and schedules the ordinary 15-second refresh exactly
once in `finally`.

- [ ] **Step 7: Add responsive and status styling**

Add trial summary/table/matrix styles using existing variables. Include tones for `[data-tone="collecting"]`
and `[data-tone="ready"]`, a clearly visible stale badge, horizontal table overflow, one-column
mobile summary below 720px, focus-visible behavior, and reduced-motion preservation. Do not add
animation that conveys hidden progress.

- [ ] **Step 8: Verify and commit Task 10**

Run:

```bash
.venv/bin/python -m pytest tests/web/test_assets.py -q
.venv/bin/ruff check tests/web/test_assets.py
.venv/bin/ruff format --check tests/web/test_assets.py
git add src/polytrading/web/assets/index.html src/polytrading/web/assets/app.js src/polytrading/web/assets/app.css tests/web/test_assets.py
git commit -m "feat(web): add prospective trial control room"
```

Expected: semantic, safe-DOM, retry, responsive, and no-mutation asset tests pass.

---

### Task 11: Operator workflow, package audit, and full verification

**Files:**

- Modify: `README.md:100-210,268-580`
- Modify: `tests/test_package.py`
- Modify: `tests/test_cli.py`
- Modify: `tests/web/test_assets.py`
- Verify only: every source and test file changed in Tasks 1–10.

**Interfaces:**

- Consumes: all completed commands and reports.
- Produces: one documented manual smoke sequence, four portable scheduler entries, explicit disk-
  growth and gap semantics, fresh-book economics handoff, and final verification evidence.

- [ ] **Step 1: Write failing README/package contract tests**

Add assertions that README contains the exact commands and safety language:

```python
readme = Path("README.md").read_text(encoding="utf-8")
assert "polytrading trial funding --current" in readme
assert "polytrading trial books --duration-seconds 60 --interval-seconds 5" in readme
assert "polytrading trial health --recent-hours 24" in readme
assert "4.15 million normalized book levels" in readme
assert "historical collection cannot repair prospective trial lineage" in readme.lower()
assert "READY_FOR_ECONOMICS_EVALUATION is not trading authorization" in readme
```

In `test_package.py`, import every public `polytrading.trial` module, assert migration 005 is
packaged, and scan trial imports/source for private venue clients, credential loaders, wallet,
signer, order, balance, position, fill, transfer, and execution modules.

- [ ] **Step 2: Run documentation/package tests and observe missing guidance**

Run:

```bash
.venv/bin/python -m pytest tests/test_package.py tests/test_cli.py tests/web/test_assets.py -q -k 'trial or README or package'
```

Expected: new README assertions fail until operator guidance is added.

- [ ] **Step 3: Document manual smoke and scheduler sequence**

Add a dedicated README section using one database and these portable examples:

```cron
1 * * * * cd /absolute/path/poly-trading && .venv/bin/polytrading trial funding --current --db var/lighter-dydx-trial.duckdb --format json >> var/trial-funding.log 2>&1
4 * * * * cd /absolute/path/poly-trading && .venv/bin/polytrading trial funding --current --db var/lighter-dydx-trial.duckdb --format json >> var/trial-funding.log 2>&1
6 * * * * cd /absolute/path/poly-trading && .venv/bin/polytrading trial health --recent-hours 24 --db var/lighter-dydx-trial.duckdb --format json >> var/trial-health.log 2>&1
58 * * * * cd /absolute/path/poly-trading && .venv/bin/polytrading trial books --duration-seconds 60 --interval-seconds 5 --db var/lighter-dydx-trial.duckdb >> var/trial-books.log 2>&1
```

State that these are documentation only, the path must be replaced, UTC boundary math is internal,
and the project does not install or modify a scheduler. Document exit 0/1/2 meanings and duplicate
funding attempt semantics.

- [ ] **Step 4: Document volume, gaps, dashboard, and evaluation handoff**

Explain the maximum 12 book cycles/hour and approximate 4.15 million normalized 20-level rows
over 60 days, before evaluator eligibility exclusions; raw payload size is venue-dependent and must
be monitored. State that the system performs no automatic retention/deletion and that backups and
disk capacity remain operator responsibilities.

Include the exact sentence `Historical collection cannot repair prospective trial lineage.` and
explain that missing, late, and generic historical rows remain visible but ineligible. Show:

```bash
.venv/bin/polytrading trial books --once --db var/lighter-dydx-trial.duckdb
.venv/bin/polytrading trial health --recent-hours 24 --db var/lighter-dydx-trial.duckdb
.venv/bin/polytrading carry economics --policy policy/BTC.json \
  --db var/lighter-dydx-trial.duckdb --evaluated-at 2026-11-12T17:59:10Z \
  --evaluation-id 00000000-0000-0000-0000-000000000001 --format json
```

Require the operator to update the explicit policy `known_as_of` after the fresh immutable book
cycle. State exactly that `READY_FOR_ECONOMICS_EVALUATION` is not trading authorization and the
economics result still cannot authorize orders.

- [ ] **Step 5: Run focused integration suites**

Run:

```bash
.venv/bin/python -m pytest tests/trial tests/venues/test_synchronized.py tests/carry/test_economics_assembler.py tests/storage/test_store.py tests/test_cli.py tests/web -q
```

Expected: all new cross-subsystem behavior and existing affected suites pass.

- [ ] **Step 6: Run static and formatting gates**

Run:

```bash
.venv/bin/ruff check .
.venv/bin/ruff format --check .
```

Expected: both commands exit zero with no diagnostics.

- [ ] **Step 7: Run full coverage verification**

Run:

```bash
.venv/bin/python -m pytest --cov=polytrading --cov-report=term-missing --cov-fail-under=90 -q
```

Expected: the complete suite passes and total coverage is at least 90%.

- [ ] **Step 8: Build and smoke-test a fresh wheel install**

Run:

```bash
verification_root="$(mktemp -d)"
.venv/bin/python -m pip wheel . --no-deps --no-build-isolation --wheel-dir "$verification_root/wheels"
.venv/bin/python -m venv --system-site-packages "$verification_root/venv"
"$verification_root/venv/bin/python" -m pip install --no-deps "$verification_root"/wheels/polytrading-*.whl
"$verification_root/venv/bin/polytrading" --help
"$verification_root/venv/bin/polytrading" trial funding --help
"$verification_root/venv/bin/polytrading" trial books --help
"$verification_root/venv/bin/polytrading" trial health --help
```

Expected: wheel build/install succeeds and all help commands exit zero without importing an
unpackaged module or migration.

- [ ] **Step 9: Run a local browser smoke check**

Create a fresh migrated database, start the loopback dashboard on an unused port, and inspect one
desktop and one narrow viewport. Verify the trial section shows `NOT_STARTED`, no console errors,
no horizontal page overflow outside table shells, copy buttons only, and GET-only network traffic.
Then hold the writer lease during refresh and verify `DATABASE_BUSY` retries preserve the prior
snapshot with a visible stale label.

- [ ] **Step 10: Run authority and prospective-lineage audits**

Run:

```bash
rg -n "api[_-]?key|private[_-]?key|seed phrase|wallet|signer|place_order|create_order|cancel_order|withdraw|transfer|paper execution|live execution" src/polytrading/trial
rg -n "funding_revisions_between" src/polytrading/carry/economics_assembler.py
```

Expected: the first command finds only explicit warning/test-safe prose or no matches; the second
finds no unrestricted economics funding query. Inspect every match before continuing.

- [ ] **Step 11: Commit documentation and final audit adjustments**

```bash
git add README.md tests/test_package.py tests/test_cli.py tests/web/test_assets.py
git commit -m "docs: add prospective trial operations workflow"
git status --short --branch
```

Expected: the documentation commit succeeds and no task-owned path remains uncommitted; preserve
and report any unrelated pre-existing user changes rather than altering them.

---

## Final review checkpoint

Before integration, compare every design section to the requirement-to-task map and record the
final command outputs in the execution handoff. Do not claim the milestone complete when a focused
test passes but a full gate, package smoke, browser smoke, or authority audit is outstanding.
