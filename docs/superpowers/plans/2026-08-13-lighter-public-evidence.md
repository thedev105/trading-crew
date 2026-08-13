# Lighter Public Evidence Adapter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this
> plan task-by-task. Use test-driven development for every behavior change.

**Goal:** Add deterministic, raw-first, unauthenticated Lighter instrument, settled-funding, and
REST book evidence for BTC, ETH, and SOL, and expose it in the existing local dashboard without any
trading authority.

**Architecture:** Implement one venue-specific `PublicVenueAdapter` using the existing bounded
`httpx` client and recorder contracts. Resolve Lighter market IDs from public metadata per method,
retain every response as exact raw evidence, and normalize only documented settled data. Extend
generic CLI collection and the dashboard grid while preserving the legacy Bybit/Hyperliquid carry
semantics.

**Tech Stack:** Python 3.12–3.14, `httpx` 0.28.1, Pydantic 2.13.4, DuckDB 1.5.4, pytest 9.1.1,
Hypothesis 6.160.0, Ruff 0.15.22.

## Global Constraints

- Use only public GET endpoints at `https://mainnet.zklighter.elliot.ai`.
- Add no SDK, signer binary, credential, account, wallet, private endpoint, balance, position,
  transfer, withdrawal, order, paper fill, or execution code.
- Support only BTC, ETH, and SOL active perpetuals.
- Never hard-code Lighter market IDs; resolve them from `/api/v1/orderBooks?filter=perp`.
- Preserve exact UTF-8 response bytes and same-venue SHA-256 lineage.
- Treat `/api/v1/fundings` at `1h` as settled evidence; do not call `/api/v1/funding-rates`.
- Convert funding direction to sign: `long` positive, `short` negative, zero unchanged.
- Cap each funding-history method call at seven days and each book request at 100 orders per side.
- Aggregate individual orders by price and retain at most 20 price levels per side.
- Use local post-response time and no sequence for REST books; always emit the local-time warning.
- Emit no `MarketSnapshot` when mark and index are unavailable.
- Keep point-in-time funding cycle, funding health, carry audit, and carry study fixed to
  Bybit/Hyperliquid.
- Keep total repository coverage at or above 90%.

## File Map

- Create `src/polytrading/venues/lighter.py`: public HTTP boundary, strict response parsing, market
  resolution, instruments, settled funding, books, and warnings.
- Create `tests/venues/test_lighter.py`: adapter happy paths and failure boundaries.
- Create `tests/fixtures/lighter/order_books.json`: minimal exact public metadata fixture for the
  three supported markets plus one ignored row.
- Create `tests/fixtures/lighter/fundings.json`: signed-direction and closed-range funding fixture.
- Create `tests/fixtures/lighter/order_book_orders.json`: unsorted individual-order fixture with
  duplicate prices for aggregation.
- Modify `src/polytrading/domain/models.py`: add `Venue.LIGHTER`.
- Modify `src/polytrading/venues/__init__.py`: export `LighterPublicAdapter`.
- Modify `src/polytrading/cli.py`: parser choices, all-venue expansion, and explicit adapter factory.
- Modify `src/polytrading/web/models.py`: canonical Lighter rows and symbol mapping.
- Modify `src/polytrading/web/dashboard.py`: query Lighter evidence in the market grid.
- Modify `tests/domain/test_models.py`: enum serialization regression.
- Modify `tests/test_cli.py`: parser, expansion, routing, warning, and legacy-pair regressions.
- Modify `tests/web/test_models.py`: twelve-row canonical model tests.
- Modify `tests/web/test_dashboard.py`: point-in-time Lighter evidence selection.
- Modify `README.md`: public commands, evidence limits, and dashboard behavior.

---

### Task 1: Venue identity, strict public boundary, and instrument metadata

**Files:**

- Create: `src/polytrading/venues/lighter.py`
- Create: `tests/venues/test_lighter.py`
- Create: `tests/fixtures/lighter/order_books.json`
- Modify: `src/polytrading/domain/models.py`
- Modify: `src/polytrading/venues/__init__.py`
- Modify: `tests/domain/test_models.py`

- [ ] **Step 1: Write failing identity and instrument tests**

Add tests that require `Venue.LIGHTER.value == "lighter"` and a `LighterPublicAdapter` with:

```python
venue = Venue.LIGHTER

async def fetch_instruments(
    assets: frozenset[Asset], observed_at: datetime
) -> AdapterBatch: ...
```

Use `httpx.MockTransport` to assert one GET to `/api/v1/orderBooks` with `filter=perp`. The fixture
must contain active BTC, ETH, and SOL rows with deliberately non-canonical response order. Assert
canonical asset order, exact instrument IDs/symbols, multiplier, USDC collateral/P&L, one-hour
funding, decimal-derived tick/step, minimum notional, exact raw hash, null venue timestamp, and
actual post-response receipt time.

- [ ] **Step 2: Run the focused tests and observe failure**

Run:

```bash
.venv/bin/python -m pytest tests/domain/test_models.py tests/venues/test_lighter.py -q
```

Expected: FAIL because the enum and adapter do not exist.

- [ ] **Step 3: Implement the minimal strict HTTP and metadata path**

Add constants for the public base URL, endpoint paths, source version, symbol map, funding limit,
and book request limit. Mirror the existing adapters' `_ReceivedResponse` pattern and
`make_raw_envelope`. Decode UTF-8 strictly and use an `object_pairs_hook` that rejects duplicate
JSON keys. Require top-level mapping, integer `code == 200`, and list `order_books`.

Select each requested symbol exactly once; require `market_type == "perp"` and
`status == "active"`. Reject bools where integers are required. Parse financial strings directly
with `Decimal`, derive powers of ten without float conversion, and construct immutable domain
records.

- [ ] **Step 4: Add failing metadata boundary tests**

Cover missing, duplicated, inactive, spot, and contradictory symbols; non-200 API code; missing or
wrong top-level fields; bool/integer confusion; negative or excessive decimal counts; zero/invalid
multiplier, min quote, tick, or step; malformed UTF-8/JSON; duplicate JSON keys; HTTP errors;
naive collection context; distinct method-context versus actual receipt timestamps; and errors that
do not echo payloads.

- [ ] **Step 5: Make metadata tests pass and lint**

Run:

```bash
.venv/bin/python -m pytest tests/domain/test_models.py tests/venues/test_lighter.py -q -k 'venue or instrument or response'
.venv/bin/ruff check src/polytrading/domain/models.py src/polytrading/venues/lighter.py tests/venues/test_lighter.py
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/polytrading/domain/models.py src/polytrading/venues/__init__.py \
  src/polytrading/venues/lighter.py tests/domain/test_models.py \
  tests/fixtures/lighter/order_books.json tests/venues/test_lighter.py
git commit -m "feat(venues): add lighter public instruments"
```

---

### Task 2: Unavailable mark/index evidence and settled funding

**Files:**

- Modify: `src/polytrading/venues/lighter.py`
- Modify: `tests/venues/test_lighter.py`
- Create: `tests/fixtures/lighter/fundings.json`

- [ ] **Step 1: Write failing unavailable-snapshot test**

Call `fetch_market_snapshots` for a deliberately unordered asset set. Assert one metadata raw
response, no normalized records, and warnings in canonical asset order:

```text
LIGHTER_MARK_INDEX_UNAVAILABLE
```

Each warning must name the metadata endpoint and exact Lighter symbol. Missing requested markets
must fail instead of emitting partial warnings.

- [ ] **Step 2: Write failing settled-funding test**

Request a closed two-hour UTC window. Have the mock assert metadata resolution followed by:

```text
/api/v1/fundings?market_id=<resolved>&resolution=1h&start_timestamp=<seconds>&end_timestamp=<seconds>&count_back=3
```

The fixture includes a `long` row, a `short` row, zero, an identical duplicate, and rows just
outside the requested range. Assert signed chronological output, one-hour intervals, funding raw
lineage, retention of both metadata and funding raw envelopes, actual funding receipt time, and no
request to `/api/v1/funding-rates`.

- [ ] **Step 3: Run the tests and observe failure**

Run:

```bash
.venv/bin/python -m pytest tests/venues/test_lighter.py -q -k 'market_snapshot or funding'
```

Expected: FAIL because these methods are not implemented.

- [ ] **Step 4: Implement minimal snapshot warnings and funding normalization**

Normalize aware timestamps to UTC, reject reversed and greater-than-seven-day ranges, calculate
the inclusive possible hourly count with a 169 hard cap, and send Unix seconds. Require exact
`resolution == "1h"`. Require each funding timestamp to be an integer but not bool, parse to UTC,
require it not to follow response receipt, parse rate from a string, and apply direction sign.
Deduplicate by timestamp and signed rate, rejecting conflicts. Filter the closed range and sort.

- [ ] **Step 5: Add funding failure-boundary tests**

Cover naive/reversed/too-long ranges; non-hour-aligned valid ranges; wrong resolution; missing or
non-list rows; invalid timestamp, rate, or direction; future rows; conflicting duplicates; malformed
market resolution; empty valid history; and exact 7-day acceptance versus one-second-over rejection.

- [ ] **Step 6: Verify and commit**

```bash
.venv/bin/python -m pytest tests/venues/test_lighter.py -q -k 'market_snapshot or funding'
.venv/bin/ruff check src/polytrading/venues/lighter.py tests/venues/test_lighter.py
git add src/polytrading/venues/lighter.py tests/venues/test_lighter.py \
  tests/fixtures/lighter/fundings.json
git commit -m "feat(venues): collect lighter settled funding"
```

---

### Task 3: Aggregated, bounded REST order books

**Files:**

- Modify: `src/polytrading/venues/lighter.py`
- Modify: `tests/venues/test_lighter.py`
- Create: `tests/fixtures/lighter/order_book_orders.json`

- [ ] **Step 1: Write the failing aggregation and timing test**

Resolve markets once, then assert depth requests occur in BTC, ETH, SOL order regardless of input
set order, each with `limit=100`. Use individual orders whose response order is unsorted and whose
prices repeat. Assert quantity sums, contributing `order_count`, descending bids, ascending asks,
top-20 truncation, exact decimals, cycle UUID, Lighter symbol, null sequence, local post-response
effective/observed time, per-response lineage, metadata raw retention, and one stable warning per
book:

```text
LIGHTER_REST_BOOK_LOCAL_TIMESTAMP
```

- [ ] **Step 2: Run and observe failure**

```bash
.venv/bin/python -m pytest tests/venues/test_lighter.py -q -k book
```

Expected: FAIL because book collection is not implemented.

- [ ] **Step 3: Implement minimal aggregation**

Require integer totals and ask/bid lists. For every order require a unique string `order_id`, exact
positive price, and exact positive `remaining_base_amount`. Group with `dict[Decimal, tuple[Decimal,
int]]`, sort, truncate, and build `BookLevel` records. Require both sides and a non-locked,
non-crossed top of book. Link each normalized record to the corresponding depth raw response, not
the metadata raw response.

- [ ] **Step 4: Add book failure-boundary tests**

Cover duplicate order IDs across and within sides, missing/wrong lists, negative totals, malformed
price/quantity, zero remaining quantity, empty sides, locked/crossed top, wrong metadata market ID
type, single-asset behavior, and actual per-response clock/monotonic sampling.

- [ ] **Step 5: Verify and commit**

```bash
.venv/bin/python -m pytest tests/venues/test_lighter.py -q -k book
.venv/bin/ruff check src/polytrading/venues/lighter.py tests/venues/test_lighter.py
git add src/polytrading/venues/lighter.py tests/venues/test_lighter.py \
  tests/fixtures/lighter/order_book_orders.json
git commit -m "feat(venues): collect lighter public books"
```

---

### Task 4: Generic CLI integration without legacy semantic drift

**Files:**

- Modify: `src/polytrading/cli.py`
- Modify: `tests/test_cli.py`

- [ ] **Step 1: Write failing CLI tests**

Require `lighter` in both generic parser choices, and require `all` expansion exactly:

```python
(Venue.BYBIT, Venue.HYPERLIQUID, Venue.DYDX, Venue.LIGHTER)
```

Patch client construction and assert `public_adapter_session` builds `LighterPublicAdapter` only
for the Lighter branch and closes every client. Assert public and book commands record Lighter
batches, print stable Lighter warnings, and keep collection ordering deterministic.

Add explicit regressions proving the funding-cycle adapter tuple, funding health expectations,
carry audit, and carry study remain Bybit/Hyperliquid only.

- [ ] **Step 2: Run and observe failure**

```bash
.venv/bin/python -m pytest tests/test_cli.py -q -k 'lighter or generic_collection or funding_cycle'
```

- [ ] **Step 3: Implement explicit routing**

Import `LighterPublicAdapter`, add parser choices, extend only generic `_parse_venues("all")`, and
add one explicit factory branch. Reuse the existing warning rendering and transactional recorder.
Do not modify the explicit Bybit/Hyperliquid funding-cycle tuple.

- [ ] **Step 4: Verify and commit**

```bash
.venv/bin/python -m pytest tests/test_cli.py -q -k 'lighter or generic_collection or funding_cycle'
.venv/bin/ruff check src/polytrading/cli.py tests/test_cli.py
git add src/polytrading/cli.py tests/test_cli.py
git commit -m "feat(cli): expose lighter public collection"
```

---

### Task 5: Dashboard integration and operator documentation

**Files:**

- Modify: `src/polytrading/web/models.py`
- Modify: `src/polytrading/web/dashboard.py`
- Modify: `tests/web/test_models.py`
- Modify: `tests/web/test_dashboard.py`
- Modify: `README.md`

- [ ] **Step 1: Write failing twelve-row model tests**

Extend canonical venues with Lighter and require exactly twelve rows ordered by venue then BTC,
ETH, SOL. Require `_symbol(Venue.LIGHTER, Asset.BTC) == "BTC"` behavior through model validation,
not a direct private-function assertion. Retain strict rejection of missing, duplicated, or
misordered market grids.

- [ ] **Step 2: Write failing point-in-time dashboard test**

Persist older and newer Lighter instrument/funding/book records around an `as_of` cutoff. Assert
the dashboard selects only the latest pre-cutoff records, preserves the native signed hourly rate,
computes spread from the selected top of book, and renders Lighter in JSON. Assert the carry rows and
funding health remain their legacy three-asset/two-venue interpretation.

- [ ] **Step 3: Run and observe failure**

```bash
.venv/bin/python -m pytest tests/web/test_models.py tests/web/test_dashboard.py -q
```

- [ ] **Step 4: Implement the minimal grid extension**

Add Lighter to both canonical `_VENUES` tuples and map its symbol to `asset.value`. Do not add UI
controls or mutate the read-only dashboard API. Existing operation recipes already use `--venue
all` and therefore automatically include Lighter after CLI expansion.

- [ ] **Step 5: Update README**

Document Lighter public and book commands, settled-funding sign semantics, local REST book timing,
missing mark/index evidence, the twelve-row dashboard, and the fact that this creates no trading or
profit authority.

- [ ] **Step 6: Verify and commit**

```bash
.venv/bin/python -m pytest tests/web/test_models.py tests/web/test_dashboard.py -q
.venv/bin/ruff check src/polytrading/web tests/web
git add src/polytrading/web/models.py src/polytrading/web/dashboard.py \
  tests/web/test_models.py tests/web/test_dashboard.py README.md
git commit -m "feat(web): show lighter public evidence"
```

---

### Task 6: Full verification, browser review, and scope audit

**Files:**

- Modify only files required by discovered defects.

- [ ] **Step 1: Run focused adapter and integration suites**

```bash
.venv/bin/python -m pytest tests/venues/test_lighter.py tests/test_cli.py \
  tests/web/test_models.py tests/web/test_dashboard.py -q
```

- [ ] **Step 2: Run full quality gates**

```bash
.venv/bin/python -m pytest --cov=polytrading --cov-report=term-missing --cov-fail-under=90
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/python -m build --wheel
```

Expected: all tests pass, total coverage is at least 90%, Ruff reports no issues, and both sdist and
wheel build successfully.

- [ ] **Step 3: Verify the built wheel in a clean temporary environment**

Install the wheel into a new temporary virtual environment, import `LighterPublicAdapter`, run
`polytrading --help`, and verify packaged dashboard assets and dossier resources still load.

- [ ] **Step 4: Run read-only dashboard browser QA**

Create a temporary DuckDB fixture with Lighter evidence, serve the local dashboard, and inspect the
overview at desktop and narrow widths. Confirm twelve market rows, readable Lighter values, research
warning visibility, no overflow, and no trading controls. Capture a screenshot only as temporary QA
evidence; do not add it to the repository.

- [ ] **Step 5: Perform a no-trading and legacy-boundary diff audit**

Inspect the full branch diff and search production changes for `credential`, `private_key`,
`sign`, `account`, `wallet`, `withdraw`, `transfer`, `position`, `order/create`, and private API
paths. Any occurrence must be existing documentation/context or removed. Confirm the only order
endpoint added is the public read-only `orderBookOrders` GET. Confirm explicit legacy two-venue
tuples are unchanged.

- [ ] **Step 6: Commit any verification-only corrections**

```bash
git add <only corrected files>
git commit -m "fix: harden lighter public evidence"
```

Skip this commit when no corrections are needed.

## Completion Criteria

- All focused and full tests pass with at least 90% coverage.
- Ruff and package builds pass.
- Clean-wheel import and CLI checks pass.
- Browser QA confirms a usable twelve-row read-only dashboard.
- Exact raw lineage and settled funding sign behavior are tested.
- The current-rate endpoint, credentials, accounts, and execution paths are absent.
- Legacy funding/carry semantics remain unchanged.
- Final autonomous diff review finds no unresolved correctness or scope issue.
