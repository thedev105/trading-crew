# Carry Persistence Study v1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a deterministic, read-only study that tests the fixed long-Bybit/short-Hyperliquid funding hypothesis without presenting gross funding as executable profit.

**Architecture:** Add one provenance-preserving DuckDB query, a pure carry-study domain service, strict versioned report models, stable renderers, and a `polytrading carry study` CLI. The service aggregates native funding settlements into aligned eight-hour blocks, computes decimal-only distributions and concentration checks, and can emit only research gate states.

**Tech Stack:** Python 3.12-3.14, Pydantic 2.13.4, DuckDB 1.5.4, pytest 9.1.1, Hypothesis 6.160.0, Ruff 0.15.22

## Global Constraints

- The implementation is read-only: no network requests, authentication, database writes, credentials, orders, or trade recommendations.
- Use only BTC, ETH, and SOL; Bybit symbol names are `BTCUSDT`, `ETHUSDT`, and `SOLUSDT`; Hyperliquid symbol names are `BTC`, `ETH`, and `SOL`.
- Use fixed eight-hour blocks aligned to UTC epoch boundaries and half-open settlement windows `(start, end]`.
- Use fixed holding windows of 7, 14, and 28 days and trial family `hl-bybit-funding-persistence-v1`.
- Use a fixed long-Bybit/short-Hyperliquid direction; never choose the historically profitable sign.
- Use `Decimal` for every binding statistic and nearest-rank percentiles without interpolation.
- A historical reconstruction needs at least 365 requested days; a point-in-time forward study needs at least 90 requested days; either needs at least 99% paired block coverage.
- Gross reports must list omitted costs and can never emit `TRADE`, `APPROVED`, or `LIVE_ELIGIBLE`.
- Do not store or redistribute raw venue data in reports; retain only aggregates and sorted unique source hashes.
- Follow test-driven development: observe each focused test fail before implementing its behavior.

---

## File map

- Create `src/polytrading/carry/study_models.py`: strict enums and versioned report records.
- Create `src/polytrading/carry/study.py`: validation, settlement deduplication, block assembly, statistics, decisions, and store-backed orchestration.
- Create `src/polytrading/carry/study_report.py`: stable JSON and concise text rendering only.
- Create `tests/carry/study_helpers.py`: deterministic synthetic native funding histories shared by study tests.
- Create `tests/carry/test_study_blocks.py`: boundaries, revisions, availability, and block completeness.
- Create `tests/carry/test_study_statistics.py`: decimal statistics, holding windows, concentration, and decisions.
- Create `tests/carry/test_study_report.py`: stable serialized output and disclosure labels.
- Modify `src/polytrading/storage/store.py`: expose every funding revision known by an explicit cutoff.
- Modify `src/polytrading/cli.py`: add and dispatch the read-only `carry study` command.
- Modify `tests/storage/test_store.py`: verify cutoff, boundary, order, and revision behavior.
- Modify `tests/test_cli.py`: verify arguments, output, insufficient data, and absence of writes/network.
- Modify `README.md`: document the research-only command and interpretation.

---

### Task 1: Funding revisions known by an explicit cutoff

**Files:**

- Modify: `src/polytrading/storage/store.py`
- Modify: `tests/storage/test_store.py`

**Interfaces:**

- Consumes: existing `FundingObservation`, `Venue`, `normalize_utc_timestamp`, and the `funding_observations` table.
- Produces: `DuckDBStore.funding_revisions_between(venue: Venue, symbol: str, start: datetime, end: datetime, known_as_of: datetime) -> tuple[FundingObservation, ...]`.

- [ ] **Step 1: Write boundary and knowledge-cutoff tests**

Add tests that insert four observations: one exactly at `start`, two revisions at one settlement
inside the window, and one exactly at `end`. Assert that the method excludes `start`, includes
`end`, excludes a revision observed after `known_as_of`, and returns deterministic order by
`effective_at`, `observed_at`, and `source_hash`.

```python
rows = store.funding_revisions_between(
    Venue.BYBIT,
    "BTCUSDT",
    NOW - timedelta(hours=16),
    NOW,
    NOW + timedelta(minutes=1),
)
assert [(row.effective_at, row.observed_at) for row in rows] == [
    (NOW - timedelta(hours=8), NOW - timedelta(hours=8) + timedelta(minutes=1)),
    (NOW, NOW + timedelta(minutes=1)),
]
```

Also assert `start > end` and `known_as_of < end` raise exact `ValueError` messages.

- [ ] **Step 2: Run the focused tests and observe failure**

Run: `pytest tests/storage/test_store.py -q`

Expected: FAIL because `DuckDBStore` has no `funding_revisions_between` method.

- [ ] **Step 3: Implement the provenance-preserving query**

Normalize all timestamps, validate `start <= end` and `known_as_of >= end`, then query without a
window function so conflicting revisions remain visible:

```sql
SELECT venue, symbol, asset, rate, interval_hours, epoch_us(effective_at),
       epoch_us(observed_at), source_hash, schema_version
FROM funding_observations
WHERE venue = ? AND symbol = ?
  AND effective_at > ? AND effective_at <= ?
  AND observed_at <= ?
ORDER BY effective_at, observed_at, source_hash
```

Construct `FundingObservation` records using the same field mapping as `funding_between`.

- [ ] **Step 4: Run storage tests**

Run: `pytest tests/storage/test_store.py -q`

Expected: PASS.

- [ ] **Step 5: Commit the store query**

```bash
git add src/polytrading/storage/store.py tests/storage/test_store.py
git commit -m "feat(carry): query funding revisions by knowledge cutoff"
```

---

### Task 2: Strict study models and native settlement blocks

**Files:**

- Create: `src/polytrading/carry/study_models.py`
- Create: `src/polytrading/carry/study.py`
- Create: `tests/carry/study_helpers.py`
- Create: `tests/carry/test_study_blocks.py`

**Interfaces:**

- Consumes: `FundingObservation`, `Asset`, `Venue`, `StrictRecord`, and UTC normalization.
- Produces:
  - `AvailabilityClass(StrEnum)`: `POINT_IN_TIME`, `HISTORICAL_RECONSTRUCTION`, `INSUFFICIENT_DATA`.
  - `StudyDecision(StrEnum)`: `INSUFFICIENT_DATA`, `REPLICATION_FAILED`, `FORWARD_TEST_REQUIRED`, `NET_FORWARD_GATE_REQUIRED`.
  - `IncompleteBlock(schema_version=1, block_end: datetime, reason_codes: tuple[str, ...])`.
  - `PairedFundingBlock(schema_version=1, block_start: datetime, block_end: datetime, bybit_rate: Decimal, hyperliquid_rate: Decimal, spread: Decimal)`.
  - `CoverageSummary` and the report/statistic records consumed by later tasks.
  - `_prepare_blocks(asset, start, end, known_as_of, bybit_rows, hyperliquid_rows) -> _PreparedStudy` as a private testable function.

- [ ] **Step 1: Define failing model and alignment tests**

Create strict frozen models with UTC validators. Test that naive datetimes, non-increasing block
windows, negative counts, ratios outside `0..1`, unsorted source hashes, and an inconsistent
`spread != hyperliquid_rate - bybit_rate` are rejected.

Test `_prepare_blocks` with synthetic rows representing one Bybit eight-hour settlement and eight
Hyperliquid hourly settlements in `(00:00, 08:00]`:

```python
assert prepared.paired_blocks == (
    PairedFundingBlock(
        schema_version=1,
        block_start=at("2026-01-01T00:00:00Z"),
        block_end=at("2026-01-01T08:00:00Z"),
        bybit_rate=Decimal("0.00008"),
        hyperliquid_rate=Decimal("0.00016"),
        spread=Decimal("0.00008"),
    ),
)
```

Add boundary cases proving the `00:00` settlement is excluded and `08:00` is included.

- [ ] **Step 2: Run block tests and observe failure**

Run: `pytest tests/carry/test_study_blocks.py -q`

Expected: FAIL because the study modules do not exist.

- [ ] **Step 3: Implement models and validation constants**

Add:

```python
PROTOCOL_VERSION = "hl-bybit-funding-persistence-v1"
COMMON_BLOCK_HOURS = 8
POINT_IN_TIME_MAX_LAG = timedelta(minutes=5)
HOLDING_DAYS = (7, 14, 28)
MIN_PAIRED_COVERAGE = Decimal("0.99")
```

Use `Literal[1]` schema versions, `Decimal` ratio fields, sorted-unique tuple validators, and model
validators for arithmetic identities.

- [ ] **Step 4: Implement settlement deduplication and block assembly**

In `study.py`:

1. Require `start < end`, `known_as_of >= end`, exact eight-hour UTC epoch alignment, and a whole
   number of eight-hour blocks.
2. Validate every record's venue, asset, and exact expected symbol.
3. Group records by `(venue, symbol, effective_at)`. If all revisions have the same `rate` and
   `interval_hours`, retain one economic value, the earliest `observed_at`, and every source hash.
   Raise `ValueError("conflicting funding revisions for immutable settlement")` otherwise.
4. Reject any retained observation with `observed_at < effective_at`.
5. Assign each effective time to an aligned `(block_start, block_end]` block.
6. Sum native `rate` and native `interval_hours` by venue and block. Raise on interval totals above
   eight hours. Mark totals below eight hours incomplete without filling them.
7. Pair only blocks complete on both venues and produce stable incomplete reason codes such as
   `BYBIT_INTERVAL_UNDERFILLED` and `HYPERLIQUID_INTERVAL_UNDERFILLED`.
8. Classify complete evidence as point-in-time only when every earliest observation lag is within
   zero to five minutes; otherwise classify it as historical reconstruction.

- [ ] **Step 5: Add revision, completeness, and ordering tests**

Cover exact duplicate revisions, conflicting revisions, observation-before-settlement, missing
hours, overfilled intervals, mixed symbols/assets, input-order invariance, source-hash ordering,
and the availability threshold at exactly five minutes versus five minutes plus one microsecond.

- [ ] **Step 6: Run block tests**

Run: `pytest tests/carry/test_study_blocks.py -q`

Expected: PASS.

- [ ] **Step 7: Commit the block layer**

```bash
git add src/polytrading/carry/study_models.py src/polytrading/carry/study.py tests/carry/study_helpers.py tests/carry/test_study_blocks.py
git commit -m "feat(carry): assemble aligned funding blocks"
```

---

### Task 3: Decimal statistics, sufficiency, and research decisions

**Files:**

- Modify: `src/polytrading/carry/study_models.py`
- Modify: `src/polytrading/carry/study.py`
- Create: `tests/carry/test_study_statistics.py`

**Interfaces:**

- Consumes: `_PreparedStudy` and `PairedFundingBlock` from Task 2.
- Produces:
  - `DistributionSummary(count, mean, median, percentile_05, percentile_95, minimum, maximum, positive_fraction, zero_fraction, negative_fraction)`.
  - `HoldingWindowSummary(holding_days, block_count, distribution)`.
  - `MonthlyContribution(month: str, gross_funding: Decimal)`.
  - `StudyStatistics(block_distribution, sign_persistence, sign_reversals, longest_adverse_run, cumulative_gross_funding, maximum_drawdown, gross_annualized_mean, monthly_contributions, cumulative_without_best_month, holding_windows)`.
  - `CarryPersistenceReport` containing protocol, request, coverage, availability, statistics, decision, source hashes, `economic_basis="gross_funding_only"`, and the exact omitted-cost tuple.
  - `CarryPersistenceStudy(store: DuckDBStore).run(asset, start, end, known_as_of) -> CarryPersistenceReport`.

- [ ] **Step 1: Write failing nearest-rank and path-statistic tests**

Use small exact sequences to prove:

```python
assert _nearest_rank((Decimal("1"), Decimal("2"), Decimal("100")), Decimal("0.05")) == Decimal("1")
assert _nearest_rank((Decimal("1"), Decimal("2"), Decimal("100")), Decimal("0.50")) == Decimal("2")
assert _maximum_drawdown((Decimal("2"), Decimal("-1"), Decimal("-3"), Decimal("4"))) == Decimal("4")
assert _longest_adverse_run((Decimal("-1"), Decimal("-2"), Decimal("0"), Decimal("-3"))) == 2
```

Test that zeros break sign transitions, a direct positive-to-negative change increments the reversal
count, and persistence is `None` when no nonzero transition exists.

- [ ] **Step 2: Write failing holding-window and month-concentration tests**

Generate contiguous eight-hour blocks across month boundaries. Assert that a 7-day window contains
21 blocks, missing paired blocks prevent a window from bridging the gap, monthly totals use the
block-end calendar month, and `cumulative_without_best_month` subtracts exactly one maximum month.

- [ ] **Step 3: Write failing decision tests**

Generate deterministic 90-day point-in-time and 365-day reconstructed histories with positive and
adverse spreads. Assert:

- coverage below `0.99` -> `INSUFFICIENT_DATA` with `statistics is None`;
- 89 point-in-time days -> `INSUFFICIENT_DATA`;
- 364 reconstructed days -> `INSUFFICIENT_DATA`;
- non-positive median, any non-positive horizon fifth percentile, or non-positive result after
  removing the best month -> `REPLICATION_FAILED`;
- passing 365-day reconstruction -> `FORWARD_TEST_REQUIRED`;
- passing 90-day point-in-time history -> `NET_FORWARD_GATE_REQUIRED`.

- [ ] **Step 4: Run statistics tests and observe failure**

Run: `pytest tests/carry/test_study_statistics.py -q`

Expected: FAIL because statistic functions and the orchestrator are not implemented.

- [ ] **Step 5: Implement deterministic statistics**

Implement helpers using `Decimal` and stable timestamp order:

```python
def _nearest_rank(values: tuple[Decimal, ...], probability: Decimal) -> Decimal:
    ordered = tuple(sorted(values))
    rank = max(1, (len(ordered) * int(probability * 100) + 99) // 100)
    return ordered[rank - 1]
```

Use an exact general ceiling calculation based on `Decimal` rather than limiting the helper to
two decimal probability inputs. Include a zero starting point when calculating cumulative maximum
drawdown. Generate holding-window sums only across consecutive expected block ends.

- [ ] **Step 6: Implement sufficiency and decisions**

Use the prepared coverage plus requested duration. Withhold all economic statistics for an
insufficient study. For sufficient studies, apply the frozen pass conditions without rounding:

```text
block median > 0
every holding-window fifth percentile > 0
cumulative gross funding after removing best month > 0
```

Return `NET_FORWARD_GATE_REQUIRED` only for point-in-time evidence; return
`FORWARD_TEST_REQUIRED` only for historical reconstruction. Define the omitted costs in stable
order as `basis_pnl`, `collateral_effects`, `failure_reserve`, `fees`, `financing`, `slippage`, and
`taxes`.

- [ ] **Step 7: Implement the store-backed service**

Map the asset to the two fixed symbols, call `funding_revisions_between` for each venue with the
same exact request, and pass both immutable tuples into the pure report builder. Do not call any
append method and do not open a network client.

- [ ] **Step 8: Run carry study unit tests**

Run: `pytest tests/carry/test_study_blocks.py tests/carry/test_study_statistics.py -q`

Expected: PASS.

- [ ] **Step 9: Commit the complete study service**

```bash
git add src/polytrading/carry/study_models.py src/polytrading/carry/study.py tests/carry/test_study_statistics.py
git commit -m "feat(carry): compute persistence study decisions"
```

---

### Task 4: Stable research-only renderers

**Files:**

- Create: `src/polytrading/carry/study_report.py`
- Create: `tests/carry/test_study_report.py`

**Interfaces:**

- Consumes: `CarryPersistenceReport`.
- Produces: `render_study_json(report: CarryPersistenceReport) -> str` and `render_study_text(report: CarryPersistenceReport) -> str`.

- [ ] **Step 1: Write failing renderer snapshot assertions**

Build one insufficient and one sufficient report. Assert JSON is parseable, keys and arrays are
stable, decimals serialize as strings under Pydantic JSON mode, source hashes are sorted, omitted
costs are present, and no output contains `TRADE`, `APPROVED`, `LIVE_ELIGIBLE`, `expected profit`,
or `recommended`.

For text, assert the first lines are exactly:

```text
Carry persistence study v1 | BTC | FORWARD_TEST_REQUIRED
Evidence: historical_reconstruction | economics=gross_funding_only
Coverage: paired=1095/1095 (1.000000)
```

- [ ] **Step 2: Run renderer tests and observe failure**

Run: `pytest tests/carry/test_study_report.py -q`

Expected: FAIL because `study_report.py` does not exist.

- [ ] **Step 3: Implement renderers**

Use `report.model_dump_json(indent=2)` for JSON. Construct text fields explicitly in stable order;
do not derive text by iterating unordered mappings. Label annualization as `gross funding per matched
leg notional`, print `statistics=withheld` for insufficient data, and always end with:

```text
Research only: fees, slippage, basis P&L, collateral effects, financing, taxes, and failure reserves are omitted.
```

- [ ] **Step 4: Run renderer tests**

Run: `pytest tests/carry/test_study_report.py -q`

Expected: PASS.

- [ ] **Step 5: Commit renderers**

```bash
git add src/polytrading/carry/study_report.py tests/carry/test_study_report.py
git commit -m "feat(carry): render persistence study reports"
```

---

### Task 5: Read-only CLI integration

**Files:**

- Modify: `src/polytrading/cli.py`
- Modify: `tests/test_cli.py`

**Interfaces:**

- Consumes: `CarryPersistenceStudy`, `render_study_json`, `render_study_text`, existing `_parse_timestamp`, and `DuckDBStore`.
- Produces: `polytrading carry study --db PATH --asset {BTC,ETH,SOL} --start ISO --end ISO --known-as-of ISO --format {text,json}`.

- [ ] **Step 1: Write failing parser and dispatch tests**

Assert the parser requires every argument except `--format`, whose default is `text`. Patch the
study service in a dispatch test and assert parsed UTC datetimes and `Asset.BTC` are passed exactly.
Assert study validation errors return exit code `2` with the existing sanitized
`polytrading: error:` prefix.

- [ ] **Step 2: Write failing read-only integration test**

Seed a temporary DuckDB with synthetic funding rows, capture the database file hash before and
after the CLI invocation, and patch `make_public_http_client` to raise if called. Assert:

```python
assert main([
    "carry", "study", "--db", str(db), "--asset", "BTC",
    "--start", "2026-01-01T00:00:00Z",
    "--end", "2026-04-01T00:00:00Z",
    "--known-as-of", "2026-04-01T00:05:00Z",
    "--format", "json",
]) == 0
assert before_hash == after_hash
```

Because DuckDB can update file metadata when opened, compare table contents and row counts in
addition to the file hash; if the engine changes non-semantic bytes on read-only open, require
semantic equality rather than weakening the no-write assertion.

- [ ] **Step 3: Run focused CLI tests and observe failure**

Run: `pytest tests/test_cli.py -q`

Expected: FAIL because `carry study` is not registered.

- [ ] **Step 4: Add parser and dispatch**

Add a `study` parser beside `audit`. Change carry dispatch to branch on
`arguments.carry_command`, preserving the existing audit command. Implement `_carry_study` to
parse timestamps, open and always close the store, run the service, select the renderer, print one
report, and return zero for every valid research decision including failure/insufficiency.

- [ ] **Step 5: Run CLI and legacy carry tests**

Run: `pytest tests/test_cli.py tests/carry/test_audit.py tests/carry/test_report.py -q`

Expected: PASS.

- [ ] **Step 6: Commit CLI integration**

```bash
git add src/polytrading/cli.py tests/test_cli.py
git commit -m "feat(carry): expose read-only persistence study"
```

---

### Task 6: Property checks, documentation, and complete verification

**Files:**

- Modify: `tests/carry/test_study_blocks.py`
- Modify: `tests/carry/test_study_statistics.py`
- Modify: `README.md`

**Interfaces:**

- Consumes: all study interfaces from Tasks 1-5.
- Produces: regression protection, user-facing interpretation, and verification evidence.

- [ ] **Step 1: Add property tests**

With Hypothesis, generate bounded lists of hourly rates and verify:

- shuffling input observations leaves paired blocks and source-hash order unchanged;
- for complete blocks, `sum(block.spread) == sum(hyperliquid rates) - sum(bybit rates)`;
- maximum drawdown is nonnegative and no greater than peak-to-trough range; and
- adding a constant positive spread to every block cannot reduce cumulative gross funding.

Use at most 50 examples and bounded decimal places so the suite remains fast and deterministic.

- [ ] **Step 2: Run property tests**

Run: `pytest tests/carry/test_study_blocks.py tests/carry/test_study_statistics.py -q`

Expected: PASS.

- [ ] **Step 3: Document the command and safety semantics**

Add a README section containing the exact CLI example from the design. Explain that:

- `FORWARD_TEST_REQUIRED` means only that a gross historical replication passed;
- `NET_FORWARD_GATE_REQUIRED` still does not recommend or approve a trade;
- a 10% annualized funding spread on leg notional is not a 10% account return;
- real costs and failure reserves are omitted; and
- live use remains disabled and requires separate data-use, eligibility, risk, execution, and
  reconciliation approval.

- [ ] **Step 4: Run formatting and static checks**

Run: `ruff format --check .`

Expected: exit code 0.

Run: `ruff check .`

Expected: exit code 0.

- [ ] **Step 5: Run the complete suite with coverage**

Run: `pytest --cov=polytrading --cov-report=term-missing -q`

Expected: all tests pass and total coverage remains at least 90%.

- [ ] **Step 6: Inspect the final diff and forbidden authority expansion**

Run:

```bash
git diff --check
rg -n "private.?key|api.?key|place.?order|submit.?order|LIVE_ELIGIBLE|recommended trade" src/polytrading/carry src/polytrading/cli.py README.md
git status --short
```

Expected: no whitespace errors; no credential or order authority was added; any status-label match
is only an explicit prohibition or research decision disclosure; only intended files are changed.

- [ ] **Step 7: Commit final checks and documentation**

```bash
git add README.md tests/carry/test_study_blocks.py tests/carry/test_study_statistics.py
git commit -m "docs(carry): document persistence research gate"
```

- [ ] **Step 8: Record final reproducibility evidence**

Run:

```bash
git log -6 --oneline
git status --short --branch
git show --stat --oneline HEAD
```

Expected: the branch is clean and the latest commits correspond to the six reviewed task
deliverables above.
