# Lighter–dYdX Shadow Economics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a deterministic point-in-time Lighter–dYdX shadow-economics gate that persists and
displays `INSUFFICIENT_EVIDENCE`, `REJECTED`, or `SHADOW_CANDIDATE` reports without adding paper or
live trading authority.

**Architecture:** Add strict policy/report contracts, focused pure funding and execution math, a
point-in-time DuckDB assembler, and a deterministic evaluator. Persist immutable reports, expose
reviewed fee import and economics commands, and render latest-known results in the existing
read-only dashboard while leaving the legacy Bybit–Hyperliquid protocols unchanged.

**Tech Stack:** Python 3.12–3.14, Pydantic 2.13.4, DuckDB 1.5.4, argparse, pytest 9.1.1,
Hypothesis 6.160.0, Ruff 0.15.22, local HTML/CSS/JavaScript.

## Global Constraints

- Implement `lighter-dydx-shadow-economics-v1` exactly as specified in
  `docs/superpowers/specs/2026-08-13-lighter-dydx-shadow-economics-design.md`.
- Use exact `Decimal` math for every financial value; do not convert through binary float.
- Support only Lighter and dYdX BTC, ETH, and SOL active linear perpetuals.
- Accept account equity only from USD 3,000 through USD 10,000 inclusive.
- Use a fixed 30-day training window followed by a fixed 60-day evaluation window.
- Require at least 99% paired funding and hourly representative-book coverage.
- Use taker entry and exit only; exclude maker completion, rebates, rewards, and basis-convergence
  credit.
- Cap assigned capital by 10% of equity, USD 500, available paired depth, and the incomplete-leg
  stress gate.
- Preserve every input's point-in-time and SHA-256 lineage and add `evaluated_at` separately from
  `known_as_of`.
- Never replace missing evidence with zero or a current web value.
- Add no credential, account, signer, wallet, balance, position, transfer, order, fill, cancellation,
  paper-execution, or live-execution code.
- Keep Bybit–Hyperliquid carry study, carry audit, funding cycle, and health semantics unchanged.
- Keep repository coverage at or above 90%.

## File Structure

- Create `src/polytrading/carry/economics_models.py`: public enums, policy, assumptions, coverage,
  costs, metrics, horizon results, and report validators.
- Create `src/polytrading/carry/economics_funding.py`: direction selection, exact distributions,
  contiguous rolling funding windows, and funding-reversal reserve.
- Create `src/polytrading/carry/economics_execution.py`: compatible quantity, level walking, basis,
  latency, capital, margin, and quote-observation calculations.
- Create `src/polytrading/carry/economics_assembler.py`: point-in-time evidence selection and
  complete-or-missing assembly result.
- Create `src/polytrading/carry/economics.py`: deterministic orchestration and decision gates.
- Create `src/polytrading/carry/economics_report.py`: stable text and canonical JSON renderers.
- Create `src/polytrading/carry/fee_import.py`: strict local reviewed-fee document parsing.
- Create `src/polytrading/storage/schema/004_economic_evaluations.sql`: append-only report storage.
- Modify `src/polytrading/storage/store.py`: cycle/book readers and economic-report persistence.
- Modify `src/polytrading/cli.py`: `fees import` and `carry economics` commands.
- Modify `src/polytrading/web/models.py`, `dashboard.py`, and local assets: three-row economics view.
- Modify `README.md`: operator workflow, interpretation, and no-authority boundary.

## Requirement-to-Task Map

- Scope, frozen thresholds, point-in-time identities, report coherence: Task 1.
- Fixed 30/60 direction and conservative funding distributions: Task 2.
- Equal-base depth, capital, basis, latency, margin, and quote economics: Task 3.
- Immutable reports and cutoff-safe book access: Task 4.
- Human-reviewed account fee evidence: Task 5.
- Complete-or-missing 90-day evidence construction and lineage: Task 6.
- Every decision gate, canonical rendering, persistence, and CLI: Task 7.
- Latest-known BTC/ETH/SOL read-only presentation: Task 8.
- Operator boundary, packaging, browser review, no-AI/no-execution audits, and full gates: Task 9.

---

### Task 1: Strict policy and report contracts

**Files:**

- Create: `src/polytrading/carry/economics_models.py`
- Create: `tests/carry/test_economics_models.py`

**Interfaces:**

- Produces: `EconomicsDecision`, `FundingDirection`, `VenueExecutionAssumption`,
  `VenueMarginAssumption`, `EconomicsPolicy`, `EvidenceCoverage`, `EconomicsCostBreakdown`,
  `HorizonEconomics`, `CompleteEconomics`, `CandidateEconomicsReport`, `canonical_policy_json`, and
  `policy_hash`.
- Consumers: Tasks 4–8 import these exact public names.

- [ ] **Step 1: Write failing enum and policy happy-path tests**

Create a deterministic `policy()` factory and assert the frozen protocol and canonical venue order:

```python
def test_policy_freezes_protocol_thresholds_and_assumption_order() -> None:
    item = policy()

    assert item.protocol_version == "lighter-dydx-shadow-economics-v1"
    assert item.account_equity_usd == Decimal("8000")
    assert tuple(row.venue for row in item.execution_assumptions) == (
        Venue.DYDX,
        Venue.LIGHTER,
    )
    assert tuple(row.venue for row in item.margin_assumptions) == (
        Venue.DYDX,
        Venue.LIGHTER,
    )
    assert item.training_days == 30
    assert item.evaluation_days == 60
    assert item.minimum_coverage == Decimal("0.99")
    assert item.maximum_assigned_usd == Decimal("500")
```

- [ ] **Step 2: Run the model test and observe the missing module**

Run:

```bash
.venv/bin/python -m pytest tests/carry/test_economics_models.py -q
```

Expected: collection fails because `polytrading.carry.economics_models` does not exist.

- [ ] **Step 3: Implement the enums and assumption models**

Define exact public identities:

```python
PROTOCOL_VERSION = "lighter-dydx-shadow-economics-v1"
RESEARCH_WARNING = (
    "Research only — shadow candidate, not a fill, recommendation, or trading authorization."
)

class EconomicsDecision(StrEnum):
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    REJECTED = "REJECTED"
    SHADOW_CANDIDATE = "SHADOW_CANDIDATE"

class FundingDirection(StrEnum):
    SHORT_LIGHTER_LONG_DYDX = "short_lighter_long_dydx"
    SHORT_DYDX_LONG_LIGHTER = "short_dydx_long_lighter"
```

`VenueExecutionAssumption` must contain venue, exact fee-tier name, account type, nonnegative taker
latency in milliseconds, `observed_at`, official source URL, and SHA-256 hash.
`VenueMarginAssumption` must
contain venue, asset, positive initial margin, maintenance margin, close-out margin, nonnegative
liquidation penalty, observation time, official source URL, and source hash. Require
`0 < close_out <= maintenance <= initial <= 1` and only official `docs.lighter.xyz` or
`help.dydx.trade` URLs matching the record venue.

- [ ] **Step 4: Implement `EconomicsPolicy` with frozen threshold validation**

Use strict fields for the flexible inputs and include the protocol thresholds as serialized values:

```python
class EconomicsPolicy(StrictRecord):
    schema_version: Literal[1]
    protocol_version: Literal["lighter-dydx-shadow-economics-v1"]
    asset: Asset
    study_end: datetime
    known_as_of: datetime
    account_equity_usd: Decimal
    cash_benchmark_annual_rate: Decimal
    operational_cost_usd: Decimal
    prefunded: bool
    operational_source_url: str
    operational_source_hash: Sha256
    execution_assumptions: tuple[VenueExecutionAssumption, VenueExecutionAssumption]
    margin_assumptions: tuple[VenueMarginAssumption, VenueMarginAssumption]
    training_days: int
    evaluation_days: int
    minimum_coverage: Decimal
    maximum_book_age_seconds: Decimal
    maximum_cycle_skew_ms: Decimal
    maximum_hourly_book_age_seconds: Decimal
    maximum_assigned_equity_fraction: Decimal
    maximum_assigned_usd: Decimal
    incomplete_leg_shock: Decimal
    maximum_incomplete_loss_equity_fraction: Decimal
    minimum_hold_return: Decimal
    minimum_profit_usd: Decimal
    minimum_annualized_return: Decimal
    cash_benchmark_spread: Decimal
    maximum_stress_loss_equity_fraction: Decimal
    maximum_drawdown_fraction: Decimal
    forced_exit_depth_multiplier: Decimal
    doubled_cost_multiplier: Decimal
    minimum_normal_quote_observations: int
    minimum_stress_quote_observations: int
```

The after-validator must require the exact tuple of frozen values from the design, require equity in
`[3000, 10000]`, require the most recent whole-hour `study_end`, allow at most 65 minutes from study
end to cutoff, require canonical dYdX/Lighter assumption order, require both margin rows to match the
policy asset, and require nonnegative operational cost. Zero operational cost is valid only when
`prefunded` is true.

- [ ] **Step 5: Add policy failure-boundary tests**

Parameterize account equity below/above bounds, naive or misaligned timestamps, stale study end,
reversed cutoff, float inputs, wrong venue order, duplicated venue, mismatched asset, unofficial
source domains, bad source hashes, negative operational cost, zero cost without prefunding, invalid
margin ordering, and every weakened frozen threshold. Assert stable validation fragments rather
than full Pydantic prose.

- [ ] **Step 6: Implement coverage, costs, horizon, complete metrics, and report contracts**

Use nested models so missing evidence can withhold dependent values:

```python
class CandidateEconomicsReport(StrictRecord):
    schema_version: Literal[1]
    protocol_version: Literal["lighter-dydx-shadow-economics-v1"]
    evaluation_id: UUID
    asset: Asset
    known_as_of: datetime
    evaluated_at: datetime
    training_start: datetime
    training_end: datetime
    evaluation_end: datetime
    policy_hash: Sha256
    source_hashes: tuple[Sha256, ...]
    decision: EconomicsDecision
    reason_codes: tuple[str, ...]
    direction: FundingDirection | None
    short_venue: Venue | None
    long_venue: Venue | None
    coverage: EvidenceCoverage
    economics: CompleteEconomics | None
    warning: Literal[
        "Research only — shadow candidate, not a fill, recommendation, or trading authorization."
    ]
```

Require UTC chronological windows, `evaluated_at >= known_as_of`, canonical hashes/reasons, exactly
7/14/28-day horizon order for complete economics, `economics is None` for insufficient reports and
for the directionless `TRAINING_FUNDING_MEDIAN_ZERO` rejection, no direction or long/short venues
for those reports, exact long/short venues and complete economics for every direction-bearing
decision, and a shadow decision only when all horizon and stress pass flags are true and reason
codes are empty. Complete economics must retain the exact execution, margin, and fee assumptions
used rather than only their calculated totals.

- [ ] **Step 7: Add exact identity and decision-coherence tests**

Assert capital conservation, unused cash, cost sum, per-horizon net identity, assigned/account
returns, simple annualization, doubled-cost identity, incompatible decision/economics combinations,
wrong horizon order, duplicate hashes/reasons, and the exact research warning. Assert canonical
policy JSON uses sorted compact keys, Decimal strings, and UTC `Z`; its SHA-256 must change when any
operator-selectable input changes and remain stable across repeated serialization.

- [ ] **Step 8: Verify and commit Task 1**

Run:

```bash
.venv/bin/python -m pytest tests/carry/test_economics_models.py -q
.venv/bin/ruff check src/polytrading/carry/economics_models.py tests/carry/test_economics_models.py
.venv/bin/ruff format --check src/polytrading/carry/economics_models.py tests/carry/test_economics_models.py
git add src/polytrading/carry/economics_models.py tests/carry/test_economics_models.py
git commit -m "feat(carry): define shadow economics contracts"
```

Expected: all focused tests and static checks pass.

---

### Task 2: Funding direction and conservative horizon statistics

**Files:**

- Create: `src/polytrading/carry/economics_funding.py`
- Create: `tests/carry/test_economics_funding.py`

**Interfaces:**

- Consumes: `FundingDirection` from Task 1 and paired `(datetime, Decimal, Decimal)` hourly rates.
- Produces: `select_direction`, `orient_funding`, `rolling_funding_sums`, `nearest_rank`,
  `maximum_funding_drawdown`, and `funding_horizon_statistics`.

- [ ] **Step 1: Write failing direction tests**

```python
def test_training_median_selects_one_direction_and_evaluation_does_not_change_it() -> None:
    training = (Decimal("0.0002"), Decimal("0.0001"), Decimal("-0.0001"))
    evaluation = (Decimal("-1"),) * 10

    direction = select_direction(training)

    assert direction is FundingDirection.SHORT_LIGHTER_LONG_DYDX
    assert orient_funding(evaluation, direction) == (Decimal("-1"),) * 10
```

Also require a negative median to select the inverse and a zero median to return `None`.

- [ ] **Step 2: Run focused tests and observe failure**

Run:

```bash
.venv/bin/python -m pytest tests/carry/test_economics_funding.py -q -k direction
```

Expected: import failure for the new functions.

- [ ] **Step 3: Implement exact nearest-rank median and direction functions**

Use sorted `Decimal` values and integer indexes only. For an even count, define the median as the
exact arithmetic mean of the two central values. `orient_funding` returns the input for short
Lighter and negates every value for short dYdX.

- [ ] **Step 4: Write failing contiguous rolling-window tests**

Construct 14 days of hourly `(effective_at, oriented_rate)` data with one missing hour. Assert a
7-day window never bridges the gap, uses exactly 168 observations, and yields only windows whose
timestamps differ by one hour throughout.

- [ ] **Step 5: Implement rolling sums and nearest-rank tails**

Use exact signatures:

```python
def rolling_funding_sums(
    rows: tuple[tuple[datetime, Decimal], ...], holding_days: Literal[7, 14, 28]
) -> tuple[Decimal, ...]: ...

def nearest_rank(values: tuple[Decimal, ...], percentile: Decimal) -> Decimal: ...

def maximum_funding_drawdown(
    rows: tuple[tuple[datetime, Decimal], ...], holding_days: Literal[7, 14, 28]
) -> Decimal: ...
```

Reject empty distributions and percentile values outside `(0, 1]`. Calculate maximum cumulative
drawdown with an initial zero and reset at every timestamp gap. Never interpolate percentiles.

- [ ] **Step 6: Implement and test `funding_horizon_statistics`**

Return a frozen dataclass with holding days, complete-window count, fifth-percentile sum, and
nonnegative maximum drawdown. Require at least one complete rolling window. Add exact tests for
7/14/28-day counts, current-seven-day median, negative paths, zero values, and a gap at each possible
boundary.

- [ ] **Step 7: Add monotonic Hypothesis tests**

Generate 168 exact four-place Decimal hourly values and a nonnegative worsening amount. Assert
subtracting the amount from every value cannot improve the fifth percentile, cannot reduce maximum
drawdown, and cannot turn a nonpositive seven-day median positive.

- [ ] **Step 8: Verify and commit Task 2**

```bash
.venv/bin/python -m pytest tests/carry/test_economics_funding.py -q
.venv/bin/ruff check src/polytrading/carry/economics_funding.py tests/carry/test_economics_funding.py
.venv/bin/ruff format --check src/polytrading/carry/economics_funding.py tests/carry/test_economics_funding.py
git add src/polytrading/carry/economics_funding.py tests/carry/test_economics_funding.py
git commit -m "feat(carry): calculate conservative funding horizons"
```

---

### Task 3: Deterministic depth, basis, latency, and capital math

**Files:**

- Create: `src/polytrading/carry/economics_execution.py`
- Create: `tests/carry/test_economics_execution.py`

**Interfaces:**

- Consumes: `Level2BookSnapshot`, `InstrumentSpec`, `FeeSchedule`, `EconomicsPolicy`,
  `FundingDirection`, and historical paired books.
- Produces: `WalkedQuote`, `walk_book`, `compatible_base_quantity`, `basis_reserve`,
  `latency_reserve`, `size_shadow_position`, `margin_stress`, and `quote_observation_counts`.

- [ ] **Step 1: Write failing level-walking tests**

```python
def test_walk_book_consumes_levels_in_side_order_with_exact_wap() -> None:
    quote = walk_book(
        levels=(
            BookLevel(price=Decimal("100"), quantity=Decimal("1"), order_count=1),
            BookLevel(price=Decimal("101"), quantity=Decimal("2"), order_count=1),
        ),
        quantity=Decimal("2"),
    )

    assert quote.quantity == Decimal("2")
    assert quote.notional == Decimal("201")
    assert quote.weighted_average_price == Decimal("100.5")
```

Assert insufficient depth raises `InsufficientDepthError` without returning a partial quote.

- [ ] **Step 2: Run and observe failure**

```bash
.venv/bin/python -m pytest tests/carry/test_economics_execution.py -q -k walk
```

Expected: missing module or symbol.

- [ ] **Step 3: Implement `WalkedQuote` and `walk_book`**

Use Decimal sums, reject nonpositive quantity, preserve supplied level order, and calculate
`notional / quantity` exactly. The caller supplies bids or asks already ordered by the domain model.

- [ ] **Step 4: Write and implement compatible-quantity and sizing tests**

Test coarser quantity steps, minimum notionals, asymmetric prices, top-20 depth, 10%-of-equity and
USD-500 caps, the 10% incomplete-leg shock, and the post-rounding 0.25% maximum net-base-delta
ratio. Implement:

```python
def compatible_base_quantity(
    lighter: InstrumentSpec,
    dydx: InstrumentSpec,
    maximum_quantity: Decimal,
) -> Decimal | None: ...

def size_shadow_position(
    *, policy: EconomicsPolicy, direction: FundingDirection,
    lighter_book: Level2BookSnapshot, dydx_book: Level2BookSnapshot,
    lighter_instrument: InstrumentSpec, dydx_instrument: InstrumentSpec,
) -> ShadowPosition | None: ...
```

Round down to a common valid multiple, require both minimum notionals, keep aggregate absolute
entry notional no greater than assigned capital, and expose unused cash and incomplete-leg loss.

- [ ] **Step 5: Write and implement basis-reserve tests**

Build hourly paired midpoint fixtures for both directions. Assert favorable convergence contributes
zero, adverse widening uses `max(0, ending - starting)`, gaps are never bridged, and the reserve is
the exact nearest-rank 99th percentile for 7/14/28-day windows.

- [ ] **Step 6: Write and implement latency-reserve tests**

Use consecutive paired executable quotes separated by 500 milliseconds, 1 second, 5 seconds, and 6
seconds. Include only positive gaps no greater than five seconds, calculate adverse fixed-direction
quote changes, and return the larger of the documented latency-floor observation and empirical
99th percentile. No valid sample must return `None`, not zero.

- [ ] **Step 7: Write and implement margin and quote-count tests**

`margin_stress` must subtract the 10% one-leg move, stressed taker close, fees, and liquidation
penalty from per-leg collateral, compare the result strictly against the larger maintenance and
close-out requirements, and report a boolean for each venue. `quote_observation_counts` must count
positive 7-day quoted counterfactuals under normal and five-second costs without creating fill or
position records.

- [ ] **Step 8: Add execution monotonic properties**

Use Hypothesis to prove that removing a tail level cannot increase supported quantity; increasing a
fee, operational cost, slippage multiplier, or latency reserve cannot improve quoted net P&L; and
lower equity cannot increase allowed assigned capital.

- [ ] **Step 9: Verify and commit Task 3**

```bash
.venv/bin/python -m pytest tests/carry/test_economics_execution.py -q
.venv/bin/ruff check src/polytrading/carry/economics_execution.py tests/carry/test_economics_execution.py
.venv/bin/ruff format --check src/polytrading/carry/economics_execution.py tests/carry/test_economics_execution.py
git add src/polytrading/carry/economics_execution.py tests/carry/test_economics_execution.py
git commit -m "feat(carry): model conservative quote economics"
```

---

### Task 4: Append-only economic report storage and point-in-time readers

**Files:**

- Create: `src/polytrading/storage/schema/004_economic_evaluations.sql`
- Modify: `src/polytrading/storage/store.py`
- Modify: `tests/storage/test_store.py`
- Modify: `tests/test_package.py`

**Interfaces:**

- Consumes: `CandidateEconomicsReport`, `BookCollectionCycle`, and `Level2BookSnapshot`.
- Produces: `append_economic_evaluation`, `latest_economic_evaluation_as_of`,
  `book_collection_cycles_between`, and `books_for_cycle` methods on `DuckDBStore`.

- [ ] **Step 1: Write failing migration and report round-trip tests**

Require packaged migration `004_economic_evaluations.sql`, contiguous versions `(1, 2, 3, 4)`,
and this schema:

```sql
CREATE TABLE economic_evaluations (
    evaluation_id UUID PRIMARY KEY,
    asset VARCHAR NOT NULL,
    known_as_of TIMESTAMPTZ NOT NULL,
    evaluated_at TIMESTAMPTZ NOT NULL,
    decision VARCHAR NOT NULL,
    direction VARCHAR,
    policy_hash VARCHAR NOT NULL,
    report_json JSON NOT NULL,
    schema_version INTEGER NOT NULL,
    record_hash VARCHAR NOT NULL
);
```

Round-trip one insufficient report, one rejected report, and one shadow report.

- [ ] **Step 2: Run storage tests and observe failure**

```bash
.venv/bin/python -m pytest tests/storage/test_store.py tests/test_package.py -q -k 'economic or migration'
```

Expected: migration resource and store methods are absent.

- [ ] **Step 3: Add migration and report persistence**

Implement:

```python
def append_economic_evaluation(self, record: CandidateEconomicsReport) -> bool: ...

def latest_economic_evaluation_as_of(
    self, asset: Asset, as_of: datetime
) -> CandidateEconomicsReport | None: ...
```

Use `_canonical_json`, `_record_hash`, and `_normalized_retry`. The latest reader must filter both
`known_as_of <= ?` and `evaluated_at <= ?`, order by `evaluated_at DESC, evaluation_id DESC`, and
validate stored JSON back into the strict report model.

- [ ] **Step 4: Write failing cycle/book reader tests**

Insert cycles before, at, and after the cutoff, including failed, skewed, complete pair-only, and
complete all-venue cycles. Assert `book_collection_cycles_between(start, end, known_as_of)` returns
ordered cycles whose request completion is known by the cutoff and whose effective range overlaps
the requested range. Assert
`books_for_cycle(cycle_id)` reconstructs every level, order count, timestamp, and source hash in
canonical venue/symbol order.

- [ ] **Step 5: Implement cycle and book readers with shared reconstruction**

Factor the existing latest-book row reconstruction into a private `_book_snapshot_from_rows`
helper so both old and new readers use the same validation path. Normalize and validate all cutoff
timestamps and reject reversed ranges before querying.

- [ ] **Step 6: Add immutability, rollback, and historical-cutoff tests**

Assert identical report retry returns false, conflicting retry raises `ConflictingRecordError`, a
transaction rollback removes the report, a report evaluated after a dashboard cutoff is invisible
even with an earlier `known_as_of`, and old databases migrate without changing counts or hashes in
existing tables.

- [ ] **Step 7: Verify and commit Task 4**

```bash
.venv/bin/python -m pytest tests/storage/test_store.py tests/test_package.py -q
.venv/bin/ruff check src/polytrading/storage/store.py tests/storage/test_store.py tests/test_package.py
.venv/bin/ruff format --check src/polytrading/storage/store.py tests/storage/test_store.py tests/test_package.py
git add src/polytrading/storage/schema/004_economic_evaluations.sql \
  src/polytrading/storage/store.py tests/storage/test_store.py tests/test_package.py
git commit -m "feat(storage): persist shadow economics reports"
```

---

### Task 5: Strict reviewed-fee import

**Files:**

- Create: `src/polytrading/carry/fee_import.py`
- Create: `tests/carry/test_fee_import.py`

**Interfaces:**

- Produces: `ReviewedFeeDocument`, `parse_reviewed_fee_document(payload: bytes)`, and
  `record_reviewed_fees(store: DuckDBStore, document: ReviewedFeeDocument) -> int`.
- Consumers: Task 7 CLI.

- [ ] **Step 1: Write failing exact-document tests**

Use a top-level object with an exact schema. The full happy-path fixture contains one dYdX and one
Lighter schedule in canonical venue/tier order; this is one representative element:

```json
{
  "schema_version": 1,
  "reviewed_at": "2026-08-13T17:00:00Z",
  "fees": [
    {
      "schema_version": 1,
      "venue": "dydx",
      "tier_name": "reviewed-tier",
      "maker_rate": "0",
      "taker_rate": "0.0005",
      "effective_from": "2026-08-13T00:00:00Z",
      "observed_at": "2026-08-13T16:00:00Z",
      "source_url": "https://help.dydx.trade/en/articles/166995-trading-fees-on-dydx",
      "source_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    }
  ]
}
```

Assert Decimal preservation, UTC timestamps, official domain validation, and deterministic venue/tier
ordering.

- [ ] **Step 2: Run and observe failure**

```bash
.venv/bin/python -m pytest tests/carry/test_fee_import.py -q
```

- [ ] **Step 3: Implement strict parsing**

Decode UTF-8 strictly, reject duplicate JSON keys with an `object_pairs_hook`, require an object,
and validate with Pydantic. Accept only Lighter and dYdX fee records and their respective official
domains. Require `observed_at <= reviewed_at`, unique `(venue, tier_name, effective_from,
observed_at)`, canonical order, and nonnegative taker rates. Do not fetch URLs or infer missing
fields.

- [ ] **Step 4: Implement transactional recording and failure tests**

Record all fee schedules in one `store.transaction()`. Assert count returned for inserted rows,
idempotent retry count, rollback on a conflicting middle record, malformed UTF-8/JSON, duplicate
keys, unknown fields, floats, unofficial URLs, unsupported venues, naive timestamps, future
observations, and errors that do not echo document contents.

- [ ] **Step 5: Verify and commit Task 5**

```bash
.venv/bin/python -m pytest tests/carry/test_fee_import.py -q
.venv/bin/ruff check src/polytrading/carry/fee_import.py tests/carry/test_fee_import.py
.venv/bin/ruff format --check src/polytrading/carry/fee_import.py tests/carry/test_fee_import.py
git add src/polytrading/carry/fee_import.py tests/carry/test_fee_import.py
git commit -m "feat(carry): import reviewed fee evidence"
```

---

### Task 6: Point-in-time evidence assembly

**Files:**

- Create: `src/polytrading/carry/economics_assembler.py`
- Create: `tests/carry/test_economics_assembler.py`

**Interfaces:**

- Consumes: `EconomicsPolicy`, `DuckDBStore`, bundled dossier loader, and Task 4 readers.
- Produces: frozen `EconomicsEvidenceBundle`, `EconomicsAssemblyResult`, and
  `EconomicsEvidenceAssembler.assemble(policy: EconomicsPolicy) -> EconomicsAssemblyResult`.

- [ ] **Step 1: Write failing complete-assembly test**

Seed exactly 90 days of paired one-hour Lighter/dYdX funding, canonical instrument specs, reviewed
fees, compatible margin/execution assumptions, representative complete book cycles, dense
five-second samples, and the bundled dossier. Assert the bundle contains canonical venue order,
30/60-day boundaries, latest book, ordered funding/book histories, exact fee tiers, and sorted
unique lineage.

- [ ] **Step 2: Run and observe failure**

```bash
.venv/bin/python -m pytest tests/carry/test_economics_assembler.py -q -k complete
```

- [ ] **Step 3: Implement deterministic selectors**

Use exact symbols `BTC|ETH|SOL` for Lighter and `BTC-USD|ETH-USD|SOL-USD` for dYdX. Query funding
revisions from `study_end - 90 days` through `study_end`, select the latest revision known at the
cutoff for each venue/effective-hour identity, reject conflicting immutable revisions, require
one-hour intervals, and pair by effective timestamp. Query book cycles starting five minutes before
the first historical boundary so that boundary can select eligible prior evidence; select the
latest eligible cycle completed at or before each hourly boundary with a five-minute age limit.
Select the latest current cycle separately within 30 seconds of `known_as_of`. Require cycles to be
complete and pair skew no greater than 1,000 milliseconds.

- [ ] **Step 4: Implement dossier, fee, instrument, and lineage gates**

Evaluate only `lighter-dydx-core-v1` known by the cutoff. A dossier missing-evidence judgment makes
assembly incomplete; a blocking judgment remains complete evidence and becomes a compatibility
rejection in Task 7; `MODEL_REQUIRED` is eligible only when blocking and missing-evidence counts are
zero because this evaluator supplies the named model. Require active compatible linear instrument
records, exact tier-name fee schedules, and policy assumption observation times no later than
cutoff. Combine every used dossier-source, instrument, selected funding-revision, selected book,
fee, execution, margin, and operational source hash in sorted unique order.

- [ ] **Step 5: Add canonical missing-evidence tests**

Delete or future-date each required input in turn. Cover coverage ratios exactly at and below 99%,
wrong funding interval, stale latest book, failed/skewed cycles, pair skew, hourly-book age, duplicate
cycle books, incompatible symbols/assets/kinds/collateral, unavailable fee tier, future policy
assumption, dossier blocker/missing item, and missing five-second samples. Assert no incomplete
result exposes a bundle and reason codes are sorted unique.

- [ ] **Step 6: Add no-future-leakage and removal monotonic tests**

Build an assembly at cutoff, add better records observed one microsecond later, and assert the first
assembly is unchanged. For each evidence family, remove one required record and assert a complete
result can become incomplete but an incomplete result never becomes complete.

- [ ] **Step 7: Verify and commit Task 6**

```bash
.venv/bin/python -m pytest tests/carry/test_economics_assembler.py -q
.venv/bin/ruff check src/polytrading/carry/economics_assembler.py tests/carry/test_economics_assembler.py
.venv/bin/ruff format --check src/polytrading/carry/economics_assembler.py tests/carry/test_economics_assembler.py
git add src/polytrading/carry/economics_assembler.py tests/carry/test_economics_assembler.py
git commit -m "feat(carry): assemble point-in-time economics evidence"
```

---

### Task 7: Deterministic evaluator, rendering, and CLI

**Files:**

- Create: `src/polytrading/carry/economics.py`
- Create: `src/polytrading/carry/economics_report.py`
- Create: `tests/carry/test_economics.py`
- Create: `tests/carry/test_economics_report.py`
- Modify: `src/polytrading/carry/__init__.py`
- Modify: `src/polytrading/cli.py`
- Modify: `tests/test_cli.py`

**Interfaces:**

- Consumes: Tasks 1–6.
- Produces: `CandidateEconomicsEvaluator.evaluate(result, evaluated_at, evaluation_id)`,
  `render_economics_text`, `render_economics_json`, `fees import`, and `carry economics`.

- [ ] **Step 1: Write failing insufficient and zero-median evaluator tests**

Assert an incomplete assembly becomes an insufficient report with no economics, canonical reasons,
available lineage, and actual evaluation time. Assert a complete bundle with zero training median
becomes rejected with `TRAINING_FUNDING_MEDIAN_ZERO`, no direction, and no fabricated net results.

- [ ] **Step 2: Run and observe failure**

```bash
.venv/bin/python -m pytest tests/carry/test_economics.py -q -k 'insufficient or median'
```

- [ ] **Step 3: Implement the evaluator pipeline**

Perform these stages in order without early exit after evidence completeness:

```text
validate evaluated_at and UUID
return typed insufficient report when assembly is incomplete
select direction from only the 30-day training differential
orient only the 60-day evaluation differential
calculate current seven-day regime
size equal-base legs from latest depth
calculate 7/14/28 funding, basis, fee, exit, latency, and operational components
calculate margin, incomplete-leg, stress-loss, drawdown, and quote-count gates
collect every applicable rejection reason
emit SHADOW_CANDIDATE only when the reason set is empty
```

Use Task 1 constructors so model validators independently verify every identity.

- [ ] **Step 4: Add complete rejected and shadow-candidate tests**

Cover each rejection gate individually: current reversal, insufficient compatible depth, minimum
notional, nonpositive horizon, 0.30%/USD-3 profit, annualized threshold, doubled-cost 28-day result,
0.25% stress loss, 8% drawdown, modeled liquidation, 25 normal observations, and 10 stressed
observations. Assert the 0.25% stress numerator includes 28-day funding reversal, forced exit, and
latency, while the drawdown numerator additionally includes 28-day basis reserve. Construct one
fixture whose exact metrics pass every gate and assert the only positive decision is
`SHADOW_CANDIDATE` with no reasons.

- [ ] **Step 5: Add evaluator monotonic properties**

Starting from the passing fixture, independently increase taker fees, operational cost, forced-exit
cost, latency reserve, funding-reversal reserve, and basis reserve. Assert conservative net never
increases and decision ordering never promotes
`INSUFFICIENT_EVIDENCE < REJECTED < SHADOW_CANDIDATE`. Remove evidence and assert the report becomes
insufficient rather than rejected or positive.

- [ ] **Step 6: Write failing report-renderer tests and implement renderers**

Text must include warning, decision, direction or unavailable, coverage, capital, every cost,
7/14/28 results, stress gates, and sorted reasons. Canonical JSON must sort keys, serialize Decimal
as strings, UUID as string, UTC with `Z`, and match `CandidateEconomicsReport` on parse. Prohibit
`buy`, `sell`, `enter`, `execute`, `guaranteed`, and `expected profit` action copy.

- [ ] **Step 7: Write failing parser and CLI workflow tests**

Require:

```text
polytrading fees import --input reviewed-fees.json --db research.duckdb
polytrading carry economics --policy policy.json --db research.duckdb \
  --evaluated-at 2026-08-13T17:00:00Z --evaluation-id <uuid> --format json
```

Patch storage/evaluator boundaries. Assert strict parsing, one transactional fee import, one report
insert, stable output, no network/client construction, and valid insufficient/rejected results exit
zero. Malformed policy, UUID, fee file, stale database, or conflict must raise `CliUsageError` with
sanitized text and exit two through `main()`.

- [ ] **Step 8: Implement CLI routing**

Add a top-level `fees` parser with required `import` subcommand and a `carry economics` parser.
Load bytes once, reject duplicate JSON keys in both fee and policy documents, parse through strict
models, and use `DuckDBStore` write mode only for fee or report persistence. Instantiate no public
HTTP client. Use a caller-supplied evaluation UUID and time for reproducibility.

- [ ] **Step 9: Add legacy command regressions**

Assert existing parser choices and outputs for carry audit/study/dossier/discovery, funding health,
funding cycle, and public/book collection are unchanged. Reassert the legacy expected venue tuple is
exactly `(Venue.BYBIT, Venue.HYPERLIQUID)`. Add an import-boundary regression proving the evaluator
module imports no database, clock, filesystem, environment, network, random, AI, or venue-client
module and accepts all nondeterministic values as arguments.

- [ ] **Step 10: Verify and commit Task 7**

```bash
.venv/bin/python -m pytest tests/carry/test_economics.py \
  tests/carry/test_economics_report.py tests/test_cli.py -q
.venv/bin/ruff check src/polytrading/carry/economics.py \
  src/polytrading/carry/economics_report.py src/polytrading/cli.py \
  tests/carry/test_economics.py tests/carry/test_economics_report.py tests/test_cli.py
.venv/bin/ruff format --check src/polytrading/carry/economics.py \
  src/polytrading/carry/economics_report.py src/polytrading/cli.py \
  tests/carry/test_economics.py tests/carry/test_economics_report.py tests/test_cli.py
git add src/polytrading/carry/economics.py src/polytrading/carry/economics_report.py \
  src/polytrading/carry/__init__.py src/polytrading/cli.py \
  tests/carry/test_economics.py tests/carry/test_economics_report.py tests/test_cli.py
git commit -m "feat(cli): expose shadow economics evaluation"
```

---

### Task 8: Point-in-time dashboard economics view

**Files:**

- Modify: `src/polytrading/web/models.py`
- Modify: `src/polytrading/web/dashboard.py`
- Modify: `src/polytrading/web/assets/index.html`
- Modify: `src/polytrading/web/assets/app.js`
- Modify: `src/polytrading/web/assets/app.css`
- Modify: `tests/web/test_models.py`
- Modify: `tests/web/test_dashboard.py`
- Modify: `tests/web/test_assets.py`
- Modify: `tests/web/test_server.py`

**Interfaces:**

- Consumes: `DuckDBStore.latest_economic_evaluation_as_of` from Task 4.
- Produces: `EconomicsSummaryRow` and `DashboardSnapshot.economics_rows` in BTC/ETH/SOL order.

- [ ] **Step 1: Write failing dashboard-model tests**

Define a three-row summary contract with asset, decision/report availability, direction, primary
reason, assigned capital, conservative 7/14/28 net values, evidence cutoff, evaluation time, and
stress pass flag. Require rows exactly `(BTC, ETH, SOL)`, coherent nullable groups, and no report
timestamp after dashboard cutoff.

- [ ] **Step 2: Run and observe failure**

```bash
.venv/bin/python -m pytest tests/web/test_models.py tests/web/test_dashboard.py -q -k economics
```

- [ ] **Step 3: Implement point-in-time builder selection**

For each asset, call `latest_economic_evaluation_as_of(asset, as_of)`. Map absent reports to one
unavailable row, insufficient reports to decision/reason with absent net values, and complete
reports to exact horizon values. Do not recompute economics inside the dashboard.

- [ ] **Step 4: Add historical leakage and decision rendering tests**

Persist older insufficient, later rejected, and future shadow reports. Assert each dashboard cutoff
selects only artifacts whose `known_as_of` and `evaluated_at` are both known. Assert canonical JSON
contains three rows and Decimal strings, while legacy 12 market rows, three carry rows, 24 health
boundaries, and discovery output remain unchanged.

- [ ] **Step 5: Write failing local-asset tests**

Require a semantic section with IDs `economics` and `economics-rows`, navigation link, three rows,
visible research warning, explicit unavailable values, and no form or mutating control. Extend the
JavaScript validator to require exactly three economics rows without changing the 12-market-row
contract.

- [ ] **Step 6: Implement HTML, safe DOM rendering, and CSS**

Render with `textContent` and `replaceChildren` only. Add tone mappings for insufficient, rejected,
and shadow candidate; keep shadow visually informational rather than a green trade action. Support
horizontal table overflow, the existing 720px mobile breakpoint, focus visibility, and reduced
motion. Add no remote URLs, WebSocket, EventSource, form, or new fetch endpoint.

- [ ] **Step 7: Verify server behavior and browser assets**

Assert the existing single read-only API response includes economics rows, all mutating methods
remain 405, non-loopback hosts remain rejected, CSP remains local-only, and database failures stay
sanitized.

- [ ] **Step 8: Verify and commit Task 8**

```bash
.venv/bin/python -m pytest tests/web -q
.venv/bin/ruff check src/polytrading/web tests/web
.venv/bin/ruff format --check src/polytrading/web tests/web
git add src/polytrading/web tests/web
git commit -m "feat(web): show conservative shadow economics"
```

---

### Task 9: Operator documentation and final verification

**Files:**

- Modify: `README.md`
- Modify: `tests/test_package.py` only if a package assertion needs the new migration resource named
  explicitly.

**Interfaces:**

- Consumes: all previous tasks.
- Produces: a documented, packaged, browser-reviewed milestone with no authority expansion.

- [ ] **Step 1: Document the exact operator workflow**

Add reviewed fee JSON and policy JSON examples using string decimals and official source URLs. Show
the two commands from Task 7 and explain every decision. State that 90 prospective days, dense book
evidence, actual fee tiers, documented latency/margin facts, and operational costs are mandatory;
an empty or young database should normally report `INSUFFICIENT_EVIDENCE`.

- [ ] **Step 2: Document interpretation and deferred authority**

Explain the 30/60 split, fixed direction, fully collateralized equal-base sizing, USD/equity caps,
lower-tail funding, additive stress reserves, no basis credit, and assigned-versus-account return.
State explicitly that a shadow candidate is not a recommendation, simulated fill, paper order, or
live authorization and that KYC/custody/transfer/tax eligibility remains deferred.

- [ ] **Step 3: Run focused feature verification**

```bash
.venv/bin/python -m pytest tests/carry/test_economics_models.py \
  tests/carry/test_economics_funding.py tests/carry/test_economics_execution.py \
  tests/carry/test_fee_import.py tests/carry/test_economics_assembler.py \
  tests/carry/test_economics.py tests/carry/test_economics_report.py \
  tests/storage/test_store.py tests/test_cli.py tests/web -q
```

Expected: all tests pass.

- [ ] **Step 4: Run the full repository quality gates**

```bash
.venv/bin/python -m pytest --cov=polytrading --cov-report=term-missing --cov-fail-under=90 -q
.venv/bin/ruff check src tests
.venv/bin/ruff format --check src tests
git diff --check
```

Expected: zero failures, coverage at least 90%, no lint or formatting errors, and no whitespace
errors.

- [ ] **Step 5: Build and smoke-test a clean wheel**

Build sequentially so the package test's clean-tree assertion cannot race the build directory:

```bash
.venv/bin/python -m build --wheel --no-isolation
```

Install the wheel into a fresh temporary virtual environment and assert imports for the economics
models/evaluator, packaged migrations through `004`, bundled dossiers, dashboard assets, and CLI
`--help`. Confirm no `build/` or `.coverage` artifact remains in the Git status afterward.

- [ ] **Step 6: Run the read-only browser review**

Create a temporary current-schema DuckDB, persist one insufficient and one rejected report, serve
the dashboard on loopback, and verify desktop and narrow layouts. Confirm three economics rows,
unavailable fields, warning copy, refresh behavior, no console errors, no mutating controls, and no
remote assets. Stop the server and remove only temporary artifacts created by this step.

- [ ] **Step 7: Run the final scope and semantic audit**

Inspect the complete diff and search new production files for private endpoints, credentials,
wallets, signers, balances, positions, transfers, orders, fills, cancellations, paper execution, or
live execution. Re-run explicit regressions proving Bybit–Hyperliquid expected venue tuples and
legacy report protocol versions remain unchanged. Treat any authority-bearing path or fabricated
zero as a blocking defect.

- [ ] **Step 8: Commit documentation and verification corrections**

```bash
git add README.md tests/test_package.py
git commit -m "docs: explain shadow economics research gate"
```

If `tests/test_package.py` did not change, add only `README.md`. Do not create an empty commit.

- [ ] **Step 9: Request code review and finish the branch**

Use `superpowers:requesting-code-review`, address every critical or important finding, rerun the
full gates, then use `superpowers:finishing-a-development-branch`. Do not merge, push, delete, or
discard without the branch-disposition authorization required by that workflow.
