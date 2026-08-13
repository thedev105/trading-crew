# Funding Cycle Operations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make hourly point-in-time funding collection scheduler-safe and expose a deterministic read-only health audit over recent closed collection windows.

**Architecture:** Keep collection as a stateless one-shot command supervised by an external scheduler. Add pure boundary arithmetic and strict health models, a read-only auditor over persisted `FundingCollectionCycle` attempts, stable renderers, and CLI wiring for `collect funding-cycle --current` plus `funding health`.

**Tech Stack:** Python 3.12–3.14, Pydantic 2.13.4, DuckDB 1.5.4, argparse, pytest 9.1.1, Hypothesis 6.160.0, Ruff 0.15.22.

## Global Constraints

- Preserve the existing read-only public-data and no-trading boundary.
- Do not install a scheduler, add a long-running loop, or add cloud/provider credentials.
- `--current` must resolve its boundary from one captured aware UTC clock value and never catch up an older hour.
- A boundary is auditable only when `cycle_end + 5 minutes <= as_of`.
- Health windows contain 1 through 2,160 exact hourly boundaries.
- Health auditing opens an existing current-schema DuckDB read-only and performs no network or writes.
- Every missing, late-only, degraded-only, duplicate, and complete outcome remains explicit.
- Canonical outputs use RFC 3339 `Z`, enum strings, UUID strings, Decimal strings, sorted JSON keys, and stable boundary order.
- Health is collection evidence only; it cannot imply strategy quality, expected profit, or live eligibility.
- Use test-driven development and preserve at least 90% repository coverage.

## File Map

- Create `src/polytrading/venues/funding_health_models.py`: strict health records, enums, warnings, and boundary arithmetic.
- Create `src/polytrading/venues/funding_health.py`: read-only attempt aggregation and health audit.
- Create `src/polytrading/venues/funding_health_report.py`: deterministic JSON and text rendering.
- Create `tests/venues/funding_health_helpers.py`: cycle fixtures shared by health tests and CLI tests.
- Create `tests/venues/test_funding_health_models.py`: model and time-window tests.
- Create `tests/venues/test_funding_health.py`: aggregation, selection, and property tests.
- Create `tests/venues/test_funding_health_report.py`: stable renderer and authority-boundary tests.
- Modify `src/polytrading/venues/funding_cycle_models.py`: add current-boundary resolution.
- Modify `src/polytrading/venues/__init__.py`: export the public health interfaces.
- Modify `src/polytrading/cli.py`: register `--current` and `funding health`.
- Modify `tests/venues/test_funding_cycle_models.py`: current-boundary tests.
- Modify `tests/test_cli.py`: parser, dispatch, exit-code, read-only, and no-network coverage.
- Modify `README.md`: portable scheduling and monitoring guidance.

---

### Task 1: Boundary arithmetic and strict health evidence models

**Files:**

- Create: `src/polytrading/venues/funding_health_models.py`
- Create: `tests/venues/test_funding_health_models.py`
- Modify: `src/polytrading/venues/funding_cycle_models.py`
- Modify: `tests/venues/test_funding_cycle_models.py`

**Interfaces:**

- Consumes: `StrictRecord`, `normalize_utc_timestamp`, `FUNDING_POINT_IN_TIME_LAG`, and SHA-256/UTC conventions already used by funding cycle models.
- Produces:
  - `resolve_current_cycle_end(now: datetime) -> datetime`.
  - `resolve_health_window(as_of: datetime, requested_hours: int) -> tuple[datetime, datetime, datetime]`, returning normalized `as_of`, first boundary, and last/latest auditable boundary.
  - `FUNDING_HEALTH_PROTOCOL_VERSION = "funding-collection-health-v1"`.
  - `FUNDING_HEALTH_WARNINGS: tuple[str, str]`.
  - `FundingBoundaryStatus`: `MISSING`, `LATE`, `DEGRADED`, `COMPLETE`.
  - `FundingCollectionHealthStatus`: `HEALTHY`, `DEGRADED`, `CRITICAL`.
  - `FundingBoundaryHealth(StrictRecord)`.
  - `FundingCollectionHealthReport(StrictRecord)`.

- [ ] **Step 1: Write failing current-boundary tests**

Add tests proving timezone normalization and one-hour flooring:

```python
def test_current_cycle_end_is_the_floor_of_one_aware_clock_value() -> None:
    eastern = timezone(-timedelta(hours=4))
    now = datetime(2026, 8, 13, 13, 59, 59, 999999, tzinfo=eastern)

    assert resolve_current_cycle_end(now) == datetime(2026, 8, 13, 17, tzinfo=UTC)


def test_current_cycle_end_rejects_naive_time() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        resolve_current_cycle_end(datetime(2026, 8, 13, 17))
```

- [ ] **Step 2: Run the current-boundary tests and observe failure**

Run: `PYTHONPATH=. .venv/bin/pytest tests/venues/test_funding_cycle_models.py -q -k current_cycle`

Expected: FAIL because `resolve_current_cycle_end` does not exist.

- [ ] **Step 3: Implement current-boundary resolution**

In `funding_cycle_models.py`, normalize first and then replace minute/second/microsecond with zero:

```python
def resolve_current_cycle_end(now: datetime) -> datetime:
    normalized_now = normalize_utc_timestamp(now)
    return normalized_now.replace(minute=0, second=0, microsecond=0)
```

- [ ] **Step 4: Write failing health-window tests**

Cover the exact cutoff and bounded hours:

```python
@pytest.mark.parametrize(
    ("as_of", "expected_last"),
    [
        (datetime(2026, 8, 13, 17, 4, 59, 999999, tzinfo=UTC), datetime(2026, 8, 13, 16, tzinfo=UTC)),
        (datetime(2026, 8, 13, 17, 5, tzinfo=UTC), datetime(2026, 8, 13, 17, tzinfo=UTC)),
    ],
)
def test_health_window_uses_only_closed_collection_windows(
    as_of: datetime, expected_last: datetime
) -> None:
    normalized, first, last = resolve_health_window(as_of, 24)
    assert normalized == as_of
    assert last == expected_last
    assert first == last - timedelta(hours=23)
```

Parametrize `requested_hours` with `True`, `0`, `2161`, and `1.5`; each must fail with a stable
integer/range message. Test naive `as_of` and a value before `1970-01-01T00:05:00Z`.

- [ ] **Step 5: Run the health-model tests and observe failure**

Run: `PYTHONPATH=. .venv/bin/pytest tests/venues/test_funding_health_models.py -q`

Expected: FAIL during import because `funding_health_models.py` does not exist.

- [ ] **Step 6: Implement enums, warnings, and health-window arithmetic**

Use these exact constants and range:

```python
FUNDING_HEALTH_PROTOCOL_VERSION = "funding-collection-health-v1"
FUNDING_HEALTH_MAX_HOURS = 2_160
FUNDING_HEALTH_WARNINGS = (
    "Research only: collection health is not strategy or return evidence.",
    "No credentials, accounts, positions, or orders were accessed.",
)
```

Reject `bool` separately because it is an `int` subclass. Compute `candidate` by flooring `as_of`;
subtract one hour when `as_of < candidate + FUNDING_POINT_IN_TIME_LAG`; compute
`first = candidate - timedelta(hours=requested_hours - 1)`. Reject windows whose first boundary is
before the Unix epoch.

- [ ] **Step 7: Write failing strict-record consistency tests**

Define `FundingBoundaryHealth` fields exactly:

```python
class FundingBoundaryHealth(StrictRecord):
    schema_version: Literal[1]
    cycle_end: datetime
    status: FundingBoundaryStatus
    attempt_count: Annotated[int, Field(ge=0)]
    complete_attempt_count: Annotated[int, Field(ge=0)]
    degraded_attempt_count: Annotated[int, Field(ge=0)]
    late_attempt_count: Annotated[int, Field(ge=0)]
    selected_cycle_id: UUID | None
    selected_request_completed_at: datetime | None
    selected_source_hashes: tuple[Sha256, ...]
    reason_codes: tuple[str, ...]
```

Define `FundingCollectionHealthReport` fields exactly:

```python
class FundingCollectionHealthReport(StrictRecord):
    schema_version: Literal[1]
    protocol_version: Literal["funding-collection-health-v1"]
    as_of: datetime
    latest_auditable_boundary: datetime
    first_boundary: datetime
    last_boundary: datetime
    requested_hours: Annotated[int, Field(ge=1, le=2_160)]
    boundaries: tuple[FundingBoundaryHealth, ...]
    status: FundingCollectionHealthStatus
    complete_boundary_count: Annotated[int, Field(ge=0)]
    degraded_boundary_count: Annotated[int, Field(ge=0)]
    late_boundary_count: Annotated[int, Field(ge=0)]
    missing_boundary_count: Annotated[int, Field(ge=0)]
    complete_coverage: Annotated[Decimal, Field(ge=0, le=1)]
    current_complete_streak: Annotated[int, Field(ge=0)]
    source_hashes: tuple[Sha256, ...]
    warnings: tuple[str, str]
```

Tests must reject unsorted hashes/reasons, mismatched attempt sums, missing records with selected
cycles, non-missing records without selected cycles, incorrect reason codes, non-hour boundaries,
wrong boundary coverage/order, wrong counts/coverage/streak/status/hash union, and altered warnings.

- [ ] **Step 8: Implement record validators**

Derive boundary status from attempt counts:

```text
complete_count > 0 => complete
else degraded_count > 0 => degraded
else late_count > 0 => late
else => missing
```

Require exact reasons based on the derived state and duplicate counts. In the report validator,
call `resolve_health_window`, regenerate every hourly boundary, count statuses, calculate
`Decimal(complete_count) / Decimal(requested_hours)`, walk reversed boundaries until the first
non-complete status for the current streak, and union selected hashes.

- [ ] **Step 9: Run model tests and static checks**

Run:

```bash
PYTHONPATH=. .venv/bin/pytest tests/venues/test_funding_cycle_models.py tests/venues/test_funding_health_models.py -q
.venv/bin/ruff check src/polytrading/venues/funding_cycle_models.py src/polytrading/venues/funding_health_models.py tests/venues/test_funding_cycle_models.py tests/venues/test_funding_health_models.py
```

Expected: PASS.

- [ ] **Step 10: Commit strict models**

```bash
git add src/polytrading/venues/funding_cycle_models.py src/polytrading/venues/funding_health_models.py tests/venues/test_funding_cycle_models.py tests/venues/test_funding_health_models.py
git commit -m "feat(forward): define collection health evidence"
```

---

### Task 2: Read-only funding health auditor

**Files:**

- Create: `src/polytrading/venues/funding_health.py`
- Create: `tests/venues/funding_health_helpers.py`
- Create: `tests/venues/test_funding_health.py`

**Interfaces:**

- Consumes: `FundingCollectionCycle`, `FundingCycleStatus`, `FundingBoundaryHealth`,
  `FundingCollectionHealthReport`, `resolve_health_window`, and the store query
  `funding_collection_cycles_between(start, end)`.
- Produces:
  - `FundingCycleHistory(Protocol)` with that exact query method.
  - `FundingCollectionHealthAuditor(store: FundingCycleHistory)`.
  - `FundingCollectionHealthAuditor.audit(as_of: datetime, requested_hours: int) -> FundingCollectionHealthReport`.

- [ ] **Step 1: Create deterministic cycle fixtures**

In `funding_health_helpers.py`, provide:

```python
HEALTH_AS_OF = datetime(2026, 8, 14, 17, 6, tzinfo=UTC)
LATEST_BOUNDARY = datetime(2026, 8, 14, 17, tzinfo=UTC)

def funding_cycle(
    cycle_end: datetime,
    status: FundingCycleStatus,
    *,
    cycle_int: int,
    completed_offset: timedelta = timedelta(minutes=2),
) -> FundingCollectionCycle: ...
```

Construct valid complete cycles, valid degraded cycles using Bybit bootstrap, and valid late cycles
with every component `late_not_collected`. Hashes must be deterministic SHA-256 values derived from
boundary/status/cycle UUID labels.

- [ ] **Step 2: Write failing empty and mixed-window audit tests**

Use a simple fake history store that records query arguments. Assert an empty three-hour history
produces three `missing` rows and `critical`. Then supply complete, degraded, and late cycles for
three boundaries and assert exact counts, zero current streak when the latest is late, selected
cycle IDs, coverage `Decimal("0.3333333333333333333333333333")`, query start/end, and warnings.

- [ ] **Step 3: Run auditor tests and observe failure**

Run: `PYTHONPATH=. .venv/bin/pytest tests/venues/test_funding_health.py -q`

Expected: FAIL because `FundingCollectionHealthAuditor` does not exist.

- [ ] **Step 4: Implement boundary aggregation and report construction**

Group cycles by exact `cycle_end`. Select attempts using:

```python
rank = {
    FundingCycleStatus.COMPLETE: 0,
    FundingCycleStatus.DEGRADED: 1,
    FundingCycleStatus.LATE: 2,
}
selected = min(attempts, key=lambda cycle: (
    rank[cycle.status], cycle.request_completed_at, str(cycle.cycle_id)
))
```

Build every expected boundary even when absent. Build reasons from effective state plus duplicate
counts, sorted lexicographically. Pass calculated fields into the strict report and let its
validator independently confirm them.

- [ ] **Step 5: Add repeated-attempt and tie-break tests**

For one boundary, supply late, degraded, and complete attempts in every input order. Assert the
complete cycle is selected, all attempt counts are retained, `MULTIPLE_ATTEMPTS` is present, and
the status is complete. Supply two complete attempts with different completion times and UUIDs;
assert earliest completion wins, UUID breaks exact-time ties, and `MULTIPLE_COMPLETE_ATTEMPTS` is
present.

- [ ] **Step 6: Add bounded property tests**

With `@settings(max_examples=50)`, generate 1..24 boundary statuses and shuffled attempt lists.
Prove input permutation does not change the report, every boundary is present exactly once in
hourly order, counts sum to requested hours, coverage conserves complete boundaries, the streak is
the complete suffix length, and selected source hashes equal the sorted union in the report.

- [ ] **Step 7: Run auditor/model tests and static checks**

Run:

```bash
PYTHONPATH=. .venv/bin/pytest tests/venues/test_funding_health_models.py tests/venues/test_funding_health.py -q
.venv/bin/ruff check src/polytrading/venues/funding_health.py tests/venues/funding_health_helpers.py tests/venues/test_funding_health.py
```

Expected: PASS.

- [ ] **Step 8: Commit the auditor**

```bash
git add src/polytrading/venues/funding_health.py tests/venues/funding_health_helpers.py tests/venues/test_funding_health.py
git commit -m "feat(forward): audit hourly collection health"
```

---

### Task 3: Stable health report renderers

**Files:**

- Create: `src/polytrading/venues/funding_health_report.py`
- Create: `tests/venues/test_funding_health_report.py`

**Interfaces:**

- Consumes: `FundingCollectionHealthReport`.
- Produces:
  - `render_funding_health_json(report: FundingCollectionHealthReport) -> str`.
  - `render_funding_health_text(report: FundingCollectionHealthReport) -> str`.

- [ ] **Step 1: Write failing JSON snapshot tests**

Build a three-boundary mixed report through the real auditor. Assert JSON parses, is byte-stable,
equals `json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)`, uses `Z`, encodes UUIDs
and enums as strings, keeps coverage as a Decimal string, preserves ordered boundaries, and ends
with the exact warnings in the payload.

- [ ] **Step 2: Write failing text snapshot tests**

Require this header shape:

```text
Funding collection health v1 | 2026-08-14T17:06:00Z | critical
Boundaries: 2026-08-14T15:00:00Z..2026-08-14T17:00:00Z | hours=3
Coverage: 1/3 (0.3333333333333333333333333333) | current_complete_streak=0
```

Each following boundary line must include the UTC boundary, status, `attempts=`,
`complete/degraded/late=` counts, selected UUID or `none`, and comma-separated reasons or `none`.
The exact warnings must end the output.

- [ ] **Step 3: Run renderer tests and observe failure**

Run: `PYTHONPATH=. .venv/bin/pytest tests/venues/test_funding_health_report.py -q`

Expected: FAIL because `funding_health_report.py` does not exist.

- [ ] **Step 4: Implement canonical JSON and explicit text**

Use the recursive conversion pattern already used in `funding_cycle_report.py`: `BaseModel`, UTC
`datetime`, `Decimal`, `UUID`, `Enum`, dictionary, and tuple/list. Do not convert Decimal through
float. Build text by explicit field access and ordered boundary iteration.

- [ ] **Step 5: Add forbidden-authority assertions**

Case-fold both outputs and assert they contain none of `recommended`, `expected profit`,
`live_eligible`, `api key`, `private key`, or `place order`. The exact negative warning containing
`orders were accessed` is allowed.

- [ ] **Step 6: Run renderer tests and checks**

Run:

```bash
PYTHONPATH=. .venv/bin/pytest tests/venues/test_funding_health_report.py -q
.venv/bin/ruff check src/polytrading/venues/funding_health_report.py tests/venues/test_funding_health_report.py
```

Expected: PASS.

- [ ] **Step 7: Commit renderers**

```bash
git add src/polytrading/venues/funding_health_report.py tests/venues/test_funding_health_report.py
git commit -m "feat(forward): render collection health"
```

---

### Task 4: Scheduler-safe and monitoring CLI

**Files:**

- Modify: `src/polytrading/cli.py`
- Modify: `src/polytrading/venues/__init__.py`
- Modify: `tests/test_cli.py`

**Interfaces:**

- Consumes: `_parse_timestamp`, `_utc_now`, `resolve_current_cycle_end`,
  `FundingCollectionHealthAuditor`, both health renderers, and `DuckDBStore`.
- Produces:
  - `collect funding-cycle --db PATH (--cycle-end ISO | --current) --assets ... --format ...`.
  - `funding health --db PATH --hours INT [--as-of ISO] --format {text,json}`.

- [ ] **Step 1: Write failing parser tests for mutually exclusive collection mode**

Assert explicit `--cycle-end` still parses and `--current` parses without a timestamp. Assert
neither mode and both modes return exit 2 with sanitized messages, and `--venue` remains rejected.

- [ ] **Step 2: Write failing current-mode routing tests**

Patch `_utc_now` with a sequence spanning an hour and patch `public_adapter_session` with the
existing fake adapters. Assert boundary resolution uses the first time exactly once for routing,
every funding call uses the floored boundary, and the boundary cannot shift during collection.

For a first clock value six minutes after the hour, assert exit 0, no public session, and one late
cycle for that same hour. Assert no earlier boundary is queried or added.

- [ ] **Step 3: Write failing health CLI tests**

Create a current-schema database with known cycles. Patch `make_public_http_client` and
`public_adapter_session` to raise if touched. Assert:

- healthy JSON exits 0;
- degraded and critical text exit 1;
- omitted `--as-of` calls `_utc_now` once;
- explicit `--as-of` is deterministic;
- invalid hours exit 2 before opening the database;
- a missing database and an old-schema database exit 2 with `polytrading: error:`; and
- the database bytes and all table row counts are unchanged after audit.

- [ ] **Step 4: Run focused CLI tests and observe failure**

Run: `PYTHONPATH=. .venv/bin/pytest tests/test_cli.py -q -k "funding_cycle or funding_health"`

Expected: FAIL because the parser and health dispatch are absent.

- [ ] **Step 5: Register parser and explicit dispatch**

Replace the required `--cycle-end` argument with a required mutually exclusive group containing
`--cycle-end` and `--current`. Add a top-level `funding` parser with required `health` subcommand:

```python
funding = commands.add_parser("funding", help="prospective funding evidence operations")
funding_commands = funding.add_subparsers(dest="funding_command", required=True)
health = funding_commands.add_parser("health", help="audit hourly funding collection health")
health.add_argument("--db", required=True, type=Path)
health.add_argument("--hours", type=int, default=24)
health.add_argument("--as-of")
health.add_argument("--format", choices=("text", "json"), default="text")
```

Dispatch `arguments.command == "funding"` before any access to `collect_command`.

- [ ] **Step 6: Update collection routing**

Capture `now = _utc_now()` once. Resolve `cycle_end` from `now` when `arguments.current`; otherwise
parse the explicit timestamp. Call `validate_cycle_timing(cycle_end, now)` before opening the
database or session. Preserve the existing late and on-time paths exactly.

- [ ] **Step 7: Implement read-only `_funding_health`**

Validate `hours` and resolve the window before opening the database. Require `arguments.db` to
exist and be a regular file. Open `DuckDBStore(arguments.db, read_only=True)`, audit, and close in
`finally`. Convert DuckDB/current-schema open failures to `CliUsageError` with no traceback. Render
once and return zero only for `FundingCollectionHealthStatus.HEALTHY`; return one otherwise.

- [ ] **Step 8: Export public interfaces**

Add `FundingCollectionHealthAuditor`, `FundingBoundaryHealth`, and
`FundingCollectionHealthReport` to `polytrading.venues.__all__`. Keep internal store protocols,
rank mappings, and conversion helpers private.

- [ ] **Step 9: Run CLI and adjacent regressions**

Run:

```bash
PYTHONPATH=. .venv/bin/pytest tests/test_cli.py tests/venues/test_funding_cycle.py tests/venues/test_funding_health.py tests/storage/test_store.py -q
.venv/bin/ruff check src/polytrading/cli.py src/polytrading/venues/__init__.py tests/test_cli.py
```

Expected: PASS.

- [ ] **Step 10: Commit CLI integration**

```bash
git add src/polytrading/cli.py src/polytrading/venues/__init__.py tests/test_cli.py
git commit -m "feat(forward): expose scheduler health commands"
```

---

### Task 5: Operator documentation and complete verification

**Files:**

- Modify: `README.md`
- Modify: `docs/superpowers/specs/2026-08-13-funding-cycle-operations-design.md` only if implementation review reveals a justified semantic correction.
- Modify: tests from Tasks 1–4 only when final review exposes a concrete uncovered invariant.

**Interfaces:**

- Consumes: every interface from Tasks 1–4.
- Produces: portable scheduling guidance and final reproducibility evidence.

- [ ] **Step 1: Document current collection and health monitoring**

Add examples:

```bash
.venv/bin/polytrading collect funding-cycle \
  --db var/forward.duckdb \
  --current \
  --assets BTC,ETH,SOL \
  --format json

.venv/bin/polytrading funding health \
  --db var/forward.duckdb \
  --hours 24 \
  --format text
```

Explain the exact five-minute health delay, exit 0/1 distinction, bootstrap behavior, append-only
retries, and that collection/health do not measure profitability.

- [ ] **Step 2: Add portable cron examples without installing them**

Document collection shortly after the hour and health after the cutoff with this concrete example,
while stating that another installation must replace `/Volumes/WORK/poly-trading` with its own
absolute checkout path:

```cron
1 * * * * cd /Volumes/WORK/poly-trading && .venv/bin/polytrading collect funding-cycle --db var/forward.duckdb --current --format json >> var/funding-cycle.log 2>&1
6 * * * * cd /Volumes/WORK/poly-trading && .venv/bin/polytrading funding health --db var/forward.duckdb --hours 24 --format json >> var/funding-health.log 2>&1
```

State that cron timezone is not trusted for boundary calculation because `--current` derives an
aware UTC boundary internally. Do not create crontab files or run scheduler commands.

- [ ] **Step 3: Run formatting and lint**

Run:

```bash
PYTHONPATH=. .venv/bin/ruff format --check .
PYTHONPATH=. .venv/bin/ruff check .
git diff --check
```

Expected: PASS with every file formatted and no whitespace errors.

- [ ] **Step 4: Run the complete suite with coverage**

Run: `PYTHONPATH=. .venv/bin/pytest --cov=polytrading --cov-report=term-missing -q`

Expected: all tests pass and total coverage is at least 90%.

- [ ] **Step 5: Audit the authority and mutation boundaries**

Run:

```bash
rg -n -i "private.?key|api.?key|place.?order|submit.?order|cancel.?order|LIVE_ELIGIBLE|recommended trade" src/polytrading README.md
rg -n "DuckDBStore\(.*read_only=True|public_adapter_session|make_public_http_client" src/polytrading/cli.py tests/test_cli.py
git status --short
```

Every authority match must be an explicit prohibition or negative test. Verify the health path
uses read-only storage and never reaches either network constructor.

- [ ] **Step 6: Perform a complete local code review**

Review `git diff main...HEAD` against every spec section. Fix critical and important findings with
new failing regression tests first. Specifically inspect UTC flooring, five-minute inclusivity,
attempt ranking, strict model recomputation, Decimal coverage, CLI exit classification, database
immutability, and no-network behavior.

- [ ] **Step 7: Re-run exact final verification after review fixes**

Repeat formatter, lint, full coverage tests, diff check, authority scan, and clean status. Do not
reuse output from before a review fix.

- [ ] **Step 8: Commit documentation or review corrections**

```bash
git add README.md docs/superpowers/specs/2026-08-13-funding-cycle-operations-design.md \
  src/polytrading/cli.py src/polytrading/venues/__init__.py \
  src/polytrading/venues/funding_cycle_models.py \
  src/polytrading/venues/funding_health.py \
  src/polytrading/venues/funding_health_models.py \
  src/polytrading/venues/funding_health_report.py \
  tests/test_cli.py tests/venues/funding_health_helpers.py \
  tests/venues/test_funding_cycle_models.py tests/venues/test_funding_health.py \
  tests/venues/test_funding_health_models.py tests/venues/test_funding_health_report.py
git commit -m "docs(forward): explain funding health operations"
```

- [ ] **Step 9: Record reproducibility evidence**

Run:

```bash
git log -10 --oneline
git status --short --branch
git show --stat --oneline HEAD
```

Expected: a clean feature branch containing models, auditor, renderer, CLI, docs, and review fixes.
