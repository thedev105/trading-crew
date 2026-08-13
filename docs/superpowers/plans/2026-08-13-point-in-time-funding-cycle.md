# Point-in-Time Funding Cycle v1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an append-only one-shot collector that records an auditable BTC/ETH/SOL funding attempt at one exact UTC-hour boundary without backdating Bybit rules.

**Architecture:** Add strict cycle models, one forward-only DuckDB migration, an asynchronous public-data collector, stable renderers, and a scheduler-friendly CLI. The collector validates timing before network use, keeps current instrument snapshots invisible to the same boundary's Bybit normalization, persists every valid raw/normalized/cycle record atomically, and represents gaps rather than repairing them with later data.

**Tech Stack:** Python 3.12-3.14, Pydantic 2.13.4, DuckDB 1.5.4, httpx 0.28.1, pytest 9.1.1, Hypothesis 6.160.0, Ruff 0.15.22

## Global Constraints

- Support exactly BTC, ETH, and SOL on exactly Bybit and Hyperliquid.
- Accept one mandatory `cycle_end` aligned to a whole UTC hour.
- Reject a before-boundary invocation without network or writes.
- Perform network collection only from `cycle_end` through `cycle_end + 5 minutes`, inclusive.
- After five minutes, make no venue requests and append an explicit late cycle.
- Query funding with `start == end == cycle_end`; never import an older settlement into the cycle.
- Use only instrument specifications already committed at or before `cycle_end` for Bybit normalization.
- Store valid raw responses, normalized records, and the cycle in one transaction.
- Use stable sanitized reason codes; never persist exception messages.
- Make retries append-only; never mutate or replace an earlier attempt.
- Do not add credentials, authentication, accounts, positions, orders, trading, scheduling, a daemon, or profit recommendations.
- Fixture and fake-transport tests are completion evidence; mainnet collection is not a test or completion gate.
- Keep total coverage at or above 90%.

---

## File map

- Create `src/polytrading/venues/funding_cycle_models.py`: strict enums, item records, cycle records, timing validation, and immutable warnings.
- Create `src/polytrading/storage/schema/003_forward_funding_cycles.sql`: append-only cycle table.
- Modify `src/polytrading/storage/store.py`: append and query funding cycle records.
- Create `src/polytrading/venues/funding_cycle.py`: exact-boundary asynchronous collector and late-cycle constructor.
- Create `src/polytrading/venues/funding_cycle_report.py`: stable JSON and text renderers.
- Modify `src/polytrading/venues/__init__.py`: expose the public cycle interfaces.
- Modify `src/polytrading/cli.py`: add `collect funding-cycle` parsing and dispatch.
- Create `tests/venues/funding_cycle_helpers.py`: deterministic raw, instrument, funding, and fake-adapter helpers.
- Create `tests/venues/test_funding_cycle_models.py`: model and timing invariants.
- Create `tests/venues/test_funding_cycle.py`: concurrency, bootstrap, failure, and atomicity behavior.
- Create `tests/venues/test_funding_cycle_report.py`: stable renderer tests.
- Modify `tests/storage/test_store.py`: migration, cycle retry, conflict, ordering, and read-only tests.
- Modify `tests/test_cli.py`: parser, timing, session, output, and exit-code tests.
- Modify `README.md`: scheduler-ready usage and interpretation.

---

### Task 1: Strict funding-cycle evidence models

**Files:**

- Create: `src/polytrading/venues/funding_cycle_models.py`
- Create: `tests/venues/test_funding_cycle_models.py`

**Interfaces:**

- Consumes: `Asset`, `Venue`, `StrictRecord`, and `normalize_utc_timestamp` from `polytrading.domain.models`.
- Produces:
  - `FUNDING_CYCLE_PROTOCOL_VERSION = "point-in-time-funding-cycle-v1"`.
  - `FUNDING_POINT_IN_TIME_LAG = timedelta(minutes=5)`.
  - `FUNDING_CYCLE_WARNINGS: tuple[str, str]` with the exact research and no-account warnings.
  - `InstrumentCaptureOutcome(StrEnum)`: `CAPTURED`, `FAILED`, `LATE_NOT_COLLECTED`.
  - `FundingCaptureOutcome(StrEnum)`: `CAPTURED`, `NO_SETTLEMENT`, `MISSING_EXPECTED`, `BOOTSTRAP_REQUIRED`, `FAILED`, `LATE_NOT_COLLECTED`.
  - `FundingCycleStatus(StrEnum)`: `COMPLETE`, `DEGRADED`, `LATE`.
  - `FundingCycleItem(StrictRecord)`.
  - `FundingCollectionCycle(StrictRecord)`.
  - `validate_cycle_timing(cycle_end: datetime, now: datetime) -> tuple[datetime, datetime, bool]`, returning normalized values plus `is_late`.

- [ ] **Step 1: Write failing timing and item-model tests**

Test exact whole-hour alignment, a naive timestamp, before-boundary rejection, the inclusive
five-minute edge, and the first late microsecond. Define a valid captured item and prove that a
captured funding outcome requires both exact timestamps, while every other funding outcome
requires both timestamps to be absent.

```python
assert validate_cycle_timing(CYCLE_END, CYCLE_END + timedelta(minutes=5))[2] is False
assert validate_cycle_timing(
    CYCLE_END, CYCLE_END + timedelta(minutes=5, microseconds=1)
)[2] is True
with pytest.raises(ValueError, match="collection clock precedes cycle end"):
    validate_cycle_timing(CYCLE_END, CYCLE_END - timedelta(microseconds=1))
```

Require sorted unique source hashes and reason codes. Require no reason for fully captured items,
but require exact stable reasons for failed, missing, bootstrap, or late components.

- [ ] **Step 2: Run the focused tests and observe failure**

Run: `PYTHONPATH=. .venv/bin/pytest tests/venues/test_funding_cycle_models.py -q`

Expected: FAIL during collection because `funding_cycle_models.py` does not exist.

- [ ] **Step 3: Implement enums, constants, timing, and item validation**

Define the item fields exactly:

```python
class FundingCycleItem(StrictRecord):
    schema_version: Literal[1]
    venue: Venue
    asset: Asset
    symbol: str
    instrument_outcome: InstrumentCaptureOutcome
    funding_outcome: FundingCaptureOutcome
    instrument_observed_at: datetime | None
    funding_effective_at: datetime | None
    funding_observed_at: datetime | None
    instrument_source_hashes: tuple[Sha256, ...]
    funding_source_hashes: tuple[Sha256, ...]
    reason_codes: tuple[str, ...]
```

Canonicalize UTC optional timestamps and both sorted-unique hash tuples. Validate expected symbols using
`BTCUSDT`/`ETHUSDT`/`SOLUSDT` for Bybit and the asset name for Hyperliquid. Enforce:

```text
instrument_outcome=captured <=> instrument_observed_at is present
funding_outcome=captured <=> both funding timestamps are present
instrument_outcome=failed => an INSTRUMENT_FAILED:* reason exists
funding_outcome=failed => a FUNDING_FAILED:* reason exists
missing_expected => FUNDING_MISSING_EXPECTED
bootstrap_required => BYBIT_INSTRUMENT_BOOTSTRAP_REQUIRED
late_not_collected => COLLECTION_WINDOW_MISSED
```

- [ ] **Step 4: Write failing cycle-consistency tests**

Construct all six venue/asset pairs in deliberately shuffled order. Assert the model rejects that
order, missing pairs, duplicate pairs, request completion before request start, source hashes not
equal to the union of all item instrument/funding hashes, an item funding timestamp not equal to
`cycle_end`, and a status that does not match the items.

Prove the status derivation rules:

```text
all instruments captured on time + all HL funding captured on time + Bybit captured/no_settlement => complete
any bootstrap/failed/missing instrument or funding component => degraded
all items late_not_collected, or any captured instrument/funding observed after cutoff => late
```

- [ ] **Step 5: Implement `FundingCollectionCycle` validation**

Define the record fields exactly:

```python
class FundingCollectionCycle(StrictRecord):
    schema_version: Literal[1]
    protocol_version: Literal["point-in-time-funding-cycle-v1"]
    cycle_id: UUID
    cycle_end: datetime
    assets: tuple[Asset, ...]
    venues: tuple[Venue, ...]
    request_started_at: datetime
    request_completed_at: datetime
    items: tuple[FundingCycleItem, ...]
    status: FundingCycleStatus
    source_hashes: tuple[Sha256, ...]
    warnings: tuple[str, str]
```

Require assets ordered by `Asset.value`, venues ordered by `Venue.value`, and items ordered by
`(venue.value, asset.value)`. Require pair coverage to equal the Cartesian product of assets and
venues. Derive the required status in one private pure helper and compare it with `status`.

- [ ] **Step 6: Run model tests**

Run: `PYTHONPATH=. .venv/bin/pytest tests/venues/test_funding_cycle_models.py -q`

Expected: PASS.

- [ ] **Step 7: Commit the evidence models**

```bash
git add src/polytrading/venues/funding_cycle_models.py tests/venues/test_funding_cycle_models.py
git commit -m "feat(forward): define funding cycle evidence"
```

---

### Task 2: Append-only cycle persistence

**Files:**

- Create: `src/polytrading/storage/schema/003_forward_funding_cycles.sql`
- Modify: `src/polytrading/storage/store.py`
- Modify: `tests/storage/test_store.py`

**Interfaces:**

- Consumes: `FundingCollectionCycle` from Task 1, existing `_canonical_json`, `_record_hash`, `_normalized_retry`, and migration machinery.
- Produces:
  - `DuckDBStore.append_funding_collection_cycle(record: FundingCollectionCycle) -> bool`.
  - `DuckDBStore.funding_collection_cycles_between(start: datetime, end: datetime) -> tuple[FundingCollectionCycle, ...]` using a closed `[start, end]` cycle-end window ordered by `cycle_end`, `request_completed_at`, and `cycle_id`.

- [ ] **Step 1: Write failing migration and round-trip tests**

Update the migration-version assertion from `[(1, 1), (2, 1)]` to
`[(1, 1), (2, 1), (3, 1)]`. Assert the packaged `003_forward_funding_cycles.sql` exists and creates
the expected table and status check.

Append a deterministic cycle twice and assert `True` then `False`. Retry the same UUID with a
changed status-compatible item and assert `ConflictingRecordError("conflicting funding collection cycle for immutable identity")`.

- [ ] **Step 2: Run storage tests and observe failure**

Run: `PYTHONPATH=. .venv/bin/pytest tests/storage/test_store.py -q`

Expected: FAIL because migration 3 and store methods do not exist.

- [ ] **Step 3: Add migration 3**

Create exactly:

```sql
CREATE TABLE funding_collection_cycles (
    cycle_id UUID PRIMARY KEY,
    cycle_end TIMESTAMPTZ NOT NULL,
    request_completed_at TIMESTAMPTZ NOT NULL,
    status VARCHAR NOT NULL,
    record_json JSON NOT NULL,
    record_hash VARCHAR NOT NULL,
    CHECK (status IN ('complete', 'degraded', 'late'))
);
```

Do not modify migrations 1 or 2.

- [ ] **Step 4: Implement append and ordered query methods**

Import `FundingCollectionCycle`. Follow the existing book-cycle pattern:

```python
def append_funding_collection_cycle(self, record: FundingCollectionCycle) -> bool:
    if self._in_transaction:
        return self._append_funding_collection_cycle(record)
    with self.transaction():
        return self._append_funding_collection_cycle(record)
```

The private append uses `_normalized_retry` on `cycle_id` and stores canonical JSON and its hash.
The query validates UTC, requires `start <= end`, selects `record_json` inside the closed
cycle-end range, applies the stable ordering, and calls
`FundingCollectionCycle.model_validate_json` for each row.

- [ ] **Step 5: Add boundary, ordering, and read-only tests**

Append two attempts for the same boundary and one later cycle. Prove both boundary endpoints are
included and attempts are ordered by completion then UUID. Reopen read-only and query the same
records. Delete migration 3 from `schema_migrations` and assert read-only open fails without
writing it back.

- [ ] **Step 6: Run storage tests**

Run: `PYTHONPATH=. .venv/bin/pytest tests/storage/test_store.py -q`

Expected: PASS.

- [ ] **Step 7: Commit persistence**

```bash
git add src/polytrading/storage/schema/003_forward_funding_cycles.sql src/polytrading/storage/store.py tests/storage/test_store.py
git commit -m "feat(forward): persist funding collection cycles"
```

---

### Task 3: Exact-boundary asynchronous collector

**Files:**

- Create: `src/polytrading/venues/funding_cycle.py`
- Create: `tests/venues/funding_cycle_helpers.py`
- Create: `tests/venues/test_funding_cycle.py`

**Interfaces:**

- Consumes: `PublicVenueAdapter`, `AdapterBatch`, `validate_adapter_batch`, `append_normalized`, Task 1 models, and Task 2 store methods.
- Produces:
  - `FundingCycleStore(PublicRecordStore, Protocol)`.
  - `PointInTimeFundingCollector(store: FundingCycleStore, *, clock: Callable[[], datetime], cycle_id_factory: Callable[[], UUID])`.
  - `PointInTimeFundingCollector.collect_once(adapters: Iterable[PublicVenueAdapter], assets: frozenset[Asset], cycle_end: datetime) -> FundingCollectionCycle`.
  - `record_late_funding_cycle(store: FundingCycleStore, assets: frozenset[Asset], cycle_end: datetime, now: datetime, *, cycle_id_factory: Callable[[], UUID] = uuid4) -> FundingCollectionCycle`.

- [ ] **Step 1: Build deterministic fake-adapter helpers**

Create raw helpers whose `source_hash` is the SHA-256 of exact payload JSON. Create exact
instrument and funding records for both venues. Implement one configurable fake adapter per venue
that records calls to:

```python
async def fetch_instruments(self, assets: frozenset[Asset], observed_at: datetime) -> AdapterBatch
async def fetch_funding_history(
    self, asset: Asset, start: datetime, end: datetime, observed_at: datetime
) -> AdapterBatch
```

All unrelated protocol methods raise `AssertionError("unexpected adapter method")`.

- [ ] **Step 2: Write failing validation and exact-request tests**

Assert the collector rejects empty assets, a missing venue, duplicate venue adapters, and a clock
outside the on-time window. With both fake adapters, assert every funding call receives
`start == end == cycle_end` and all six items are canonically ordered.

Preseed Bybit instrument specs observed before the boundary so this test exercises Bybit funding
instead of bootstrap.

- [ ] **Step 3: Run collector tests and observe failure**

Run: `PYTHONPATH=. .venv/bin/pytest tests/venues/test_funding_cycle.py -q`

Expected: FAIL because `PointInTimeFundingCollector` does not exist.

- [ ] **Step 4: Implement input validation and concurrent instrument capture**

Require both venue adapters exactly once and all requested assets nonempty. Capture
`request_started_at` from the injected clock and call `validate_cycle_timing`. Start both
`fetch_instruments` calls with `asyncio.gather(..., return_exceptions=True)`.

For each successful batch:

1. require only `InstrumentSpec` normalized records;
2. require the adapter venue;
3. require exactly the requested assets and expected symbols once each; and
4. call `validate_adapter_batch`.

Do not write the batch yet. Convert a failed or invalid batch to
`INSTRUMENT_FAILED:<venue>:<code>` for every venue item, where `code` is
`AdapterBatchIntegrityError.code` for lineage failures and `type(error).__name__` otherwise.
Re-raise `asyncio.CancelledError`.

- [ ] **Step 5: Implement preexisting Bybit basis checks and concurrent funding capture**

Before any write, use
`store.latest_instrument_as_of(Venue.BYBIT, symbol, cycle_end)` for each asset. Include that exact
method in `FundingCycleStore`. A missing value creates `bootstrap_required` and skips that exact
funding call even when the new instrument batch succeeded.

Run all eligible funding calls concurrently. Validate every successful batch:

```text
only FundingObservation records
zero or one normalized record
exact adapter venue, asset, expected symbol, and effective_at == cycle_end
valid raw lineage
```

An empty Hyperliquid batch becomes `missing_expected`; an empty Bybit batch becomes
`no_settlement`. A thrown or invalid batch becomes `failed`. A valid singleton becomes `captured`.

- [ ] **Step 6: Implement item assembly, status, and atomic persistence**

After requests finish, capture `request_completed_at`. Associate each valid instrument raw hash
with every matching venue/asset item's `instrument_source_hashes` and each funding batch hash with
that item's `funding_source_hashes`. Sort and deduplicate hashes and reasons. Construct
`FundingCollectionCycle`; let its validators enforce the derived status and hash conservation.

Persist in one transaction:

```python
with self._store.transaction() as transaction:
    for raw in ordered_valid_raw_records:
        transaction.append_raw(raw)
    for normalized in ordered_valid_normalized_records:
        append_normalized(transaction, normalized)
    transaction.append_funding_collection_cycle(cycle)
```

Order raw records by venue, endpoint, source hash, and event UUID. Order normalized instruments
before funding, then venue, asset, symbol, effective/observed time. Never persist a failed batch.

- [ ] **Step 7: Implement the pure late-cycle path**

`record_late_funding_cycle` validates that `now` is strictly later than the five-minute cutoff,
constructs all Cartesian item pairs with both outcomes `late_not_collected`, empty hashes, and
`COLLECTION_WINDOW_MISSED`, then appends just the cycle. It accepts no adapter and performs no
network work.

- [ ] **Step 8: Add bootstrap, empty-response, latency, and failure tests**

Cover:

- first cycle records new Bybit specs but marks Bybit funding `bootstrap_required`;
- a later boundary uses those committed specs and captures Bybit funding;
- empty Hyperliquid is `missing_expected`, empty Bybit is `no_settlement`;
- instrument failure and funding success remain two distinct outcomes;
- an instrument or funding observation after `cycle_end + 5 minutes` makes the cycle `late`;
- wrong venue, symbol, asset, timestamp, duplicate record, or raw lineage fails that item;
- one venue timeout retains other valid batches;
- cancellation escapes; and
- input adapter and asset ordering does not change items or hashes.

- [ ] **Step 9: Add atomic rollback tests**

Use a store wrapper whose `append_funding_collection_cycle` raises after raw and normalized append
calls. Assert every affected table remains empty. Then run a valid cycle and assert its cycle
source hashes equal the union of item hashes and each normalized row has same-venue raw lineage.

- [ ] **Step 10: Run collector and storage tests**

Run: `PYTHONPATH=. .venv/bin/pytest tests/venues/test_funding_cycle.py tests/storage/test_store.py -q`

Expected: PASS.

- [ ] **Step 11: Commit the collector**

```bash
git add src/polytrading/venues/funding_cycle.py tests/venues/funding_cycle_helpers.py tests/venues/test_funding_cycle.py
git commit -m "feat(forward): collect exact funding boundaries"
```

---

### Task 4: Stable research-only renderers

**Files:**

- Create: `src/polytrading/venues/funding_cycle_report.py`
- Create: `tests/venues/test_funding_cycle_report.py`

**Interfaces:**

- Consumes: `FundingCollectionCycle` from Task 1.
- Produces:
  - `render_funding_cycle_json(cycle: FundingCollectionCycle) -> str`.
  - `render_funding_cycle_text(cycle: FundingCollectionCycle) -> str`.

- [ ] **Step 1: Write failing JSON and text snapshot assertions**

Build one complete and one late cycle. Assert JSON parses, keys are stable and sorted, timestamps
use `Z`, enum values are lowercase, and item order is Bybit BTC/ETH/SOL followed by Hyperliquid
BTC/ETH/SOL.

Assert text starts exactly:

```text
Point-in-time funding cycle v1 | 2026-08-13T17:00:00Z | complete
Window cutoff: 2026-08-13T17:05:00Z
```

Each item line includes venue, asset, instrument outcome, funding outcome, and comma-separated
reasons or `none`. Both exact warnings must end the output.

- [ ] **Step 2: Run renderer tests and observe failure**

Run: `PYTHONPATH=. .venv/bin/pytest tests/venues/test_funding_cycle_report.py -q`

Expected: FAIL because the renderer module does not exist.

- [ ] **Step 3: Implement renderers without float conversion**

Reuse the explicit recursive JSON conversion pattern from `carry/study_report.py`: UTC datetimes
become RFC 3339 strings, enums become values, tuples become arrays, and dictionaries preserve
stable serialized key sorting. Build text lines explicitly; do not iterate unordered mappings.

- [ ] **Step 4: Add forbidden-authority assertions**

Case-fold both outputs and assert they contain none of `trade`, `approved`, `live_eligible`,
`expected profit`, `recommended`, `api key`, or `private key`. The warning phrase "orders were
accessed" is permitted only as part of the exact negative warning.

- [ ] **Step 5: Run renderer tests**

Run: `PYTHONPATH=. .venv/bin/pytest tests/venues/test_funding_cycle_report.py -q`

Expected: PASS.

- [ ] **Step 6: Commit renderers**

```bash
git add src/polytrading/venues/funding_cycle_report.py tests/venues/test_funding_cycle_report.py
git commit -m "feat(forward): render funding cycle evidence"
```

---

### Task 5: Scheduler-friendly CLI integration

**Files:**

- Modify: `src/polytrading/venues/__init__.py`
- Modify: `src/polytrading/cli.py`
- Modify: `tests/test_cli.py`

**Interfaces:**

- Consumes: `_parse_assets`, `_parse_timestamp`, `_utc_now`, `public_adapter_session`, Task 3 collector/late constructor, and Task 4 renderers.
- Produces: `polytrading collect funding-cycle --db PATH --assets BTC,ETH,SOL --cycle-end ISO --format {text,json}`.

- [ ] **Step 1: Write failing parser and usage tests**

Assert `--db` and `--cycle-end` are required, assets default to `BTC,ETH,SOL`, format defaults to
text, a venue option is not accepted, and an invalid/non-hour timestamp returns exit 2 through the
existing sanitized `polytrading: error:` path.

- [ ] **Step 2: Write failing early and late dispatch tests**

Patch `_utc_now` and `public_adapter_session`. For a before-boundary call, assert exit 2, no
database file, and no session. For a late call, assert exit 0, the session was never entered, and
one late cycle with six items was stored.

- [ ] **Step 3: Write failing on-time integration test**

Patch `public_adapter_session` to yield the fake adapters and `_utc_now` to return deterministic
on-time values. Preseed Bybit specs before `cycle_end`. Invoke `main([... "--format", "json"])`,
parse stdout, and assert a complete cycle, six items, exact-boundary calls, raw/normalized rows,
and one cycle row. Patch account/order-like methods to raise if touched.

- [ ] **Step 4: Run focused CLI tests and observe failure**

Run: `PYTHONPATH=. .venv/bin/pytest tests/test_cli.py -q`

Expected: FAIL because `collect funding-cycle` is not registered.

- [ ] **Step 5: Register parser and dispatch**

Add beside `public` and `books`:

```python
funding_cycle = collect_commands.add_parser(
    "funding-cycle", help="collect one point-in-time funding boundary"
)
funding_cycle.add_argument("--db", required=True, type=Path)
funding_cycle.add_argument("--assets", default="BTC,ETH,SOL")
funding_cycle.add_argument("--cycle-end", required=True)
funding_cycle.add_argument("--format", choices=("text", "json"), default="text")
```

Dispatch it explicitly before the final books fallback.

- [ ] **Step 6: Implement `_collect_funding_cycle`**

Parse and validate the boundary and assets, call `now = _utc_now()` exactly once for the routing
decision, and call `validate_cycle_timing` before creating a database or public session when the
clock precedes the boundary.

For late timing, open the store, call `record_late_funding_cycle`, and close it in `finally`.
For on-time timing, open the store, enter `public_adapter_session` with
`(Venue.BYBIT, Venue.HYPERLIQUID)`, construct `PointInTimeFundingCollector(store, clock=_utc_now)`,
and await `collect_once`. Render exactly once and return zero for every persisted research status.

- [ ] **Step 7: Export the public cycle interfaces**

Update `polytrading.venues.__all__` with `FundingCollectionCycle`, `FundingCycleItem`,
`PointInTimeFundingCollector`, and `record_late_funding_cycle`. Do not expose internal validators
or helper types unless another production module imports them.

- [ ] **Step 8: Run CLI, collector, and legacy collection tests**

Run: `PYTHONPATH=. .venv/bin/pytest tests/test_cli.py tests/venues/test_funding_cycle.py tests/venues/test_synchronized.py -q`

Expected: PASS.

- [ ] **Step 9: Commit CLI integration**

```bash
git add src/polytrading/venues/__init__.py src/polytrading/cli.py tests/test_cli.py
git commit -m "feat(forward): expose funding cycle command"
```

---

### Task 6: Documentation, properties, and complete verification

**Files:**

- Modify: `README.md`
- Modify: `tests/venues/test_funding_cycle_models.py`
- Modify: `tests/venues/test_funding_cycle.py`

**Interfaces:**

- Consumes: every interface from Tasks 1-5.
- Produces: operator guidance, property-level regression protection, and final reproducibility evidence.

- [ ] **Step 1: Add bounded property tests**

With at most 50 examples, generate nonempty subsets of BTC/ETH/SOL and shuffled adapter/item
orders. Prove:

- canonical items always equal the venue/asset Cartesian product;
- cycle source hashes equal the sorted union of every item instrument/funding hash;
- moving a captured instrument or funding observation from the cutoff to one microsecond after it
  changes required status from complete/degraded to late; and
- no permutation changes persisted normalized identities.

Use bounded lists and deterministic UUID/hash factories so the suite stays fast.

- [ ] **Step 2: Run property tests**

Run: `PYTHONPATH=. .venv/bin/pytest tests/venues/test_funding_cycle_models.py tests/venues/test_funding_cycle.py -q`

Expected: PASS.

- [ ] **Step 3: Document bootstrap, hourly scheduling, and interpretation**

Add a README section with the exact command:

```bash
.venv/bin/polytrading collect funding-cycle \
  --db var/forward.duckdb \
  --assets BTC,ETH,SOL \
  --cycle-end 2026-08-13T17:00:00Z \
  --format json
```

Explain that an external scheduler must supply the just-ended UTC hour and invoke within five
minutes; the first Bybit cycle may be bootstrap-only; degraded/late records are successful
diagnostics, not complete evidence; repeated attempts append rather than overwrite; no catch-up can
be relabeled point-in-time; raw data remains local; and the command does not install scheduling or
authorize live collection, trading, or redistribution.

- [ ] **Step 4: Run formatting and static checks**

Run: `PYTHONPATH=. .venv/bin/ruff format --check .`

Expected: exit 0.

Run: `PYTHONPATH=. .venv/bin/ruff check .`

Expected: exit 0.

- [ ] **Step 5: Run the complete suite with coverage**

Run: `PYTHONPATH=. .venv/bin/pytest --cov=polytrading --cov-report=term-missing -q`

Expected: all tests pass and total coverage is at least 90%.

- [ ] **Step 6: Audit the final diff and authority boundary**

Run:

```bash
git diff --check
rg -n "private.?key|api.?key|place.?order|submit.?order|cancel.?order|LIVE_ELIGIBLE|recommended trade" src/polytrading README.md
git status --short
```

Expected: no whitespace errors; any match is an explicit prohibition or negative warning; only
planned files changed.

- [ ] **Step 7: Commit documentation and property checks**

```bash
git add README.md tests/venues/test_funding_cycle_models.py tests/venues/test_funding_cycle.py
git commit -m "docs(forward): explain point-in-time collection"
```

- [ ] **Step 8: Record final reproducibility evidence**

Run:

```bash
git log -8 --oneline
git status --short --branch
git show --stat --oneline HEAD
```

Expected: a clean feature branch containing the six reviewed task deliverables.
