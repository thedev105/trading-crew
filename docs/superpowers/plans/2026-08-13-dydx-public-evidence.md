# dYdX Public Evidence Adapter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add deterministic, raw-first, unauthenticated dYdX instrument, realized-funding, and REST order-book evidence for BTC, ETH, and SOL without enabling trading or fabricating unavailable mark prices.

**Architecture:** Implement one `PublicVenueAdapter` backed by the existing bounded `httpx` client. Keep venue-specific parsing, finite backward pagination, raw envelopes, and warnings in a focused `dydx.py`; add only the stable venue enum and explicit CLI routing elsewhere. Pass structured warnings through both public-snapshot recording and synchronized-book collection so evidence limitations remain visible.

**Tech Stack:** Python 3.12–3.14, `httpx` 0.28.1, Pydantic 2.13.4 domain records, DuckDB 1.5.4 persistence, pytest 9.1.1, Ruff 0.15.22.

## Global Constraints

- Support only `BTC-USD`, `ETH-USD`, and `SOL-USD` on `https://indexer.dydx.trade`.
- Add no SDK, dependency, credential, authentication, wallet, signing, account, transfer, or order code.
- Store the exact UTF-8 bytes of every successful response and preserve same-venue SHA-256 lineage.
- Use the actual post-response wall-clock reading for `observed_at`; never backdate it to the method argument.
- Normalize only `ACTIVE` linear perpetuals; a missing, inactive, duplicate, or malformed requested ticker fails the whole method call.
- Do not create `MarketSnapshot` records without a documented dYdX mark price; emit `DYDX_MARK_PRICE_UNAVAILABLE` instead.
- Label every REST book with `DYDX_REST_BOOK_LOCAL_TIMESTAMP`; use local receipt time and no sequence.
- Funding pagination is newest-to-oldest, finite, closed-interval, and conflict-detecting.
- Keep the point-in-time funding cycle, funding health, carry audit, and carry persistence study fixed to their existing Bybit/Hyperliquid semantics.
- Use test-driven development, make small commits, and preserve at least 90% total repository coverage.

## File Map

- Create `src/polytrading/venues/dydx.py`: public HTTP requests, exact raw envelopes, dYdX parsing, pagination, instruments, funding, books, and missing-mark warnings.
- Create `tests/venues/test_dydx.py`: deterministic adapter behavior and failure-boundary tests.
- Create `tests/fixtures/dydx/perpetual_markets.json`: checked-in public market response fixture.
- Create `tests/fixtures/dydx/funding_history_page_1.json`: newest funding page fixture.
- Create `tests/fixtures/dydx/funding_history_page_2.json`: older funding page fixture.
- Create `tests/fixtures/dydx/orderbook.json`: public REST book fixture with more than 20 unsorted levels.
- Modify `src/polytrading/domain/models.py`: add the stable `Venue.DYDX` value.
- Modify `src/polytrading/venues/__init__.py`: export `DydxPublicAdapter`.
- Modify `src/polytrading/venues/synchronized.py`: optionally send validated batch warnings to the CLI.
- Modify `src/polytrading/cli.py`: add parser choices, explicit adapter construction, all-venue expansion, and stable warning rendering.
- Modify `tests/domain/test_models.py`: assert dYdX enum serialization.
- Modify `tests/venues/test_synchronized.py`: warning-sink acceptance and failure isolation.
- Modify `tests/test_cli.py`: venue expansion, factory routing, warning output, and legacy-pair regression coverage.
- Modify `README.md`: explain dYdX's public-evidence limits and commands.

---

### Task 1: Venue identity, exact-response boundary, instruments, and unavailable-mark evidence

**Files:**

- Create: `src/polytrading/venues/dydx.py`
- Create: `tests/venues/test_dydx.py`
- Create: `tests/fixtures/dydx/perpetual_markets.json`
- Modify: `src/polytrading/domain/models.py`
- Modify: `src/polytrading/venues/__init__.py`
- Modify: `tests/domain/test_models.py`

**Interfaces:**

- Consumes: `PublicVenueAdapter`, `AdapterBatch`, `AdapterWarning`, `make_raw_envelope`, `normalize_utc_timestamp`, and `httpx.AsyncClient`.
- Produces:
  - `Venue.DYDX = "dydx"`.
  - `DydxPublicAdapter(client, wall_clock, monotonic_ns, *, max_funding_pages=10_000)`.
  - `fetch_instruments(assets, observed_at) -> AdapterBatch`.
  - `fetch_market_snapshots(assets, observed_at) -> AdapterBatch`.
  - private `_get(endpoint: str, params: Mapping[str, str | int] | None = None) -> _ReceivedResponse`.

- [ ] **Step 1: Add failing enum and adapter contract tests**

Add a model assertion:

```python
def test_dydx_venue_has_stable_serialized_value() -> None:
    assert Venue.DYDX.value == "dydx"
```

In `test_dydx.py`, build a deterministic adapter around `httpx.MockTransport`, wall-clock values
`2026-08-13T12:00:01Z` onward, and monotonic values `1_000_000`, `2_000_000`. Assert that a market
fixture containing BTC, ETH, and SOL yields three `InstrumentSpec` values with exact ticker IDs,
USDC collateral/P&L, one-hour funding, quantity step, price tick, and nullable unproven semantic
fields. Assert the raw `payload_json` is the fixture text byte-for-byte and its hash equals
`sha256(fixture_bytes).hexdigest()`.

- [ ] **Step 2: Run the focused tests and observe failure**

Run:

```bash
.venv/bin/python -m pytest tests/domain/test_models.py -q -k dydx
.venv/bin/python -m pytest tests/venues/test_dydx.py -q -k 'instrument or market'
```

Expected: FAIL because the enum and module do not exist.

- [ ] **Step 3: Add the venue and public response shell**

Use these constants and response wrapper:

```python
_BASE_URL = "https://indexer.dydx.trade"
_MARKETS_ENDPOINT = "/v4/perpetualMarkets"
_FUNDING_ENDPOINT_PREFIX = "/v4/historicalFunding"
_ORDERBOOK_ENDPOINT_PREFIX = "/v4/orderbooks/perpetualMarket"
_SOURCE_VERSION = "indexer-v4-public"
_FUNDING_PAGE_LIMIT = 100
_SYMBOL_BY_ASSET = {
    Asset.BTC: "BTC-USD",
    Asset.ETH: "ETH-USD",
    Asset.SOL: "SOL-USD",
}

@dataclass(frozen=True)
class _ReceivedResponse:
    endpoint: str
    payload: bytes
    document: Mapping[str, object]
    observed_at: datetime
    monotonic_started_ns: int
    monotonic_completed_ns: int

    def raw_envelope(self) -> RawEnvelope:
        return make_raw_envelope(
            venue=Venue.DYDX,
            payload=self.payload,
            endpoint=self.endpoint,
            source_version=_SOURCE_VERSION,
            venue_timestamp=None,
            monotonic_started_ns=self.monotonic_started_ns,
            monotonic_completed_ns=self.monotonic_completed_ns,
            observed_at=self.observed_at,
        )
```

`_get` captures monotonic start, awaits `client.get`, captures bytes, completion, and wall clock,
then calls `raise_for_status`, decodes JSON, and requires a mapping. Its errors name only the
endpoint and structural problem, never response content.

- [ ] **Step 4: Implement strict market selection and instrument normalization**

Request `_MARKETS_ENDPOINT` once without relying on arbitrary symbol interpolation. Require a
top-level `markets` mapping. For every mapping entry, require its key to equal the row's `ticker`.
Reject duplicate represented tickers even if JSON construction is injected directly in a unit
test. Select requested symbols and require exact coverage and `status == "ACTIVE"`.

Construct:

```python
InstrumentSpec(
    schema_version=1,
    instrument_id=f"dydx:{symbol}",
    venue=Venue.DYDX,
    symbol=symbol,
    asset=asset,
    kind=InstrumentKind.LINEAR_PERPETUAL,
    contract_multiplier=Decimal(1),
    index_family=None,
    oracle_family=None,
    mark_method=None,
    liquidation_method=None,
    collateral_asset="USDC",
    pnl_asset="USDC",
    funding_formula_id=None,
    funding_cap=None,
    funding_interval_hours=Decimal(1),
    funding_payment_offset_minutes=None,
    min_notional=None,
    quantity_step=_positive_decimal(row, "stepSize", context),
    price_tick=_positive_decimal(row, "tickSize", context),
    is_inverse=False,
    is_prelaunch=False,
    observed_at=received.observed_at,
    source_hash=raw.source_hash,
)
```

- [ ] **Step 5: Add failing incomplete-universe and mark-boundary tests**

Parametrize missing BTC, `status="PAUSED"`, mismatched ticker key/field, zero step, invalid decimal,
non-mapping rows, invalid UTF-8, and invalid JSON. Each must raise without returning a partial
batch. For market snapshots, assert one raw market response, no normalized rows, and exact stable
warnings sorted by asset:

```python
AdapterWarning(
    code="DYDX_MARK_PRICE_UNAVAILABLE",
    venue=Venue.DYDX,
    endpoint="/v4/perpetualMarkets",
    symbol="BTC-USD",
    message="dYdX public market evidence has no documented mark-price field",
)
```

- [ ] **Step 6: Implement unavailable-mark behavior and run focused verification**

Reuse the strict requested-market selector so `fetch_market_snapshots` cannot warn over a partial
universe. Return the exact market raw, an empty normalized tuple, and one warning per asset in
asset-value order. Run:

```bash
.venv/bin/python -m pytest tests/domain/test_models.py tests/venues/test_dydx.py -q -k 'dydx or instrument or market'
.venv/bin/ruff check src/polytrading/domain/models.py src/polytrading/venues/dydx.py tests/domain/test_models.py tests/venues/test_dydx.py
```

Expected: PASS.

- [ ] **Step 7: Commit the adapter foundation**

```bash
git add src/polytrading/domain/models.py src/polytrading/venues/__init__.py \
  src/polytrading/venues/dydx.py tests/domain/test_models.py \
  tests/fixtures/dydx/perpetual_markets.json tests/venues/test_dydx.py
git commit -m "feat(venues): add dydx public instrument evidence"
```

---

### Task 2: Finite backward realized-funding collection

**Files:**

- Modify: `src/polytrading/venues/dydx.py`
- Modify: `tests/venues/test_dydx.py`
- Create: `tests/fixtures/dydx/funding_history_page_1.json`
- Create: `tests/fixtures/dydx/funding_history_page_2.json`

**Interfaces:**

- Consumes: `_get`, `_SYMBOL_BY_ASSET`, exact response envelopes, and the configured positive `max_funding_pages`.
- Produces:
  - `PaginationStalledError`.
  - `fetch_funding_history(asset, start, end, observed_at) -> AdapterBatch`.
  - `_parse_iso_timestamp(value: object, context: str) -> datetime`.
  - `_format_query_timestamp(value: datetime) -> str`, emitting UTC ISO-8601 with `Z`.

- [ ] **Step 1: Write the failing two-page lineage test**

Have the mock transport assert these requests in order:

```text
/v4/historicalFunding/BTC-USD?limit=100&effectiveBeforeOrAt=2026-08-13T12%3A00%3A00Z
/v4/historicalFunding/BTC-USD?limit=100&effectiveBeforeOrAt=2026-08-13T10%3A59%3A59.999999Z
```

The first fixture contains 12:00, 11:00, and an identical duplicate 11:00 observation; the second
contains 10:00 and 09:00. Request `[10:00, 12:00]`. Assert ordered normalized output 10:00, 11:00,
12:00; the duplicate is included once; 10:00 points to page two's hash while 11:00 and 12:00 point
to page one's hash; 09:00 is filtered out; both raw responses remain. Keeping the duplicate within
one page is consistent with moving the next inclusive upper-bound cursor one microsecond before
the first page's oldest timestamp.

- [ ] **Step 2: Run the funding test and observe failure**

Run: `.venv/bin/python -m pytest tests/venues/test_dydx.py -q -k funding_history_paginates`

Expected: FAIL because funding collection is not implemented.

- [ ] **Step 3: Implement closed-range validation and pagination**

Normalize all input timestamps and reject `end < start`. Calculate:

```python
possible_hourly_rows = math.ceil(
    (normalized_end - normalized_start) / timedelta(hours=1)
) + 1
request_budget = min(possible_hourly_rows + 1, self._max_funding_pages)
cursor = normalized_end
observations: dict[datetime, tuple[FundingObservation, Decimal]] = {}
```

For each page, send `limit=100` and the formatted cursor. Require a top-level
`historicalFunding` list. Reject rows whose ticker differs from the requested symbol, whose
effective time is after the cursor or response receipt, or whose timestamp/rate conflicts with an
earlier row. Keep only the closed requested range. Break on an empty page or when the oldest row is
at/before `start`; otherwise set `next_cursor = oldest - timedelta(microseconds=1)` and require it
to be strictly earlier. If the loop exhausts, raise `PaginationStalledError`.

- [ ] **Step 4: Write failing funding edge tests**

Cover:

- empty range-at-a-valid-instant response;
- aware non-UTC input normalization;
- naive input and reversed ranges;
- ticker mismatch;
- missing/non-list history;
- invalid, naive, and future `effectiveAt`;
- invalid or over-precision Decimal rates rejected by the domain;
- identical duplicate acceptance and conflicting duplicate rejection;
- rows newer than the cursor;
- a server that repeats the same page;
- `max_funding_pages` rejecting bool, non-int, zero, and negative constructor values;
- explicit request-budget exhaustion with `max_funding_pages=1`.

- [ ] **Step 5: Implement the minimal validation and run funding verification**

Use `datetime.fromisoformat(value.replace("Z", "+00:00"))`, require a string, catch parse errors,
and call `normalize_utc_timestamp`. Parse all financial values directly with `Decimal`; do not
round floats. Run:

```bash
.venv/bin/python -m pytest tests/venues/test_dydx.py -q -k funding
.venv/bin/ruff check src/polytrading/venues/dydx.py tests/venues/test_dydx.py
```

Expected: PASS.

- [ ] **Step 6: Commit funding evidence**

```bash
git add src/polytrading/venues/dydx.py tests/venues/test_dydx.py \
  tests/fixtures/dydx/funding_history_page_1.json \
  tests/fixtures/dydx/funding_history_page_2.json
git commit -m "feat(venues): collect dydx realized funding"
```

---

### Task 3: Strict 20-level REST order books and local-time warnings

**Files:**

- Modify: `src/polytrading/venues/dydx.py`
- Modify: `tests/venues/test_dydx.py`
- Create: `tests/fixtures/dydx/orderbook.json`

**Interfaces:**

- Consumes: `_get`, `_SYMBOL_BY_ASSET`, `BookLevel`, `Level2BookSnapshot`, and a caller-supplied `cycle_id`.
- Produces: `fetch_order_books(assets, observed_at, cycle_id) -> AdapterBatch` with one raw,
  one book, and one `DYDX_REST_BOOK_LOCAL_TIMESTAMP` warning per requested asset.

- [ ] **Step 1: Write the failing normalization and timing test**

Use a fixture whose sides are deliberately unsorted and contain 22 valid levels. Assert:

- request order is BTC, ETH, SOL regardless of frozenset order;
- bids are the best 20 in strictly descending price order;
- asks are the best 20 in strictly ascending price order;
- quantities are exact `Decimal` values and `order_count is None`;
- every book retains the caller's cycle UUID and dYdX ticker;
- `sequence is None`, `effective_at == observed_at ==` that response's wall-clock receipt;
- raw venue timestamp is `None` and lineage points to the corresponding response;
- warnings have stable asset order and exact code, endpoint, symbol, and message.

- [ ] **Step 2: Run the book test and observe failure**

Run: `.venv/bin/python -m pytest tests/venues/test_dydx.py -q -k order_books_normalize`

Expected: FAIL because book collection is not implemented.

- [ ] **Step 3: Implement strict book parsing**

For each side, require a list of mappings with exactly usable `price` and `size` fields, parse
positive Decimals, reject duplicate prices before sorting, sort the full side, then slice 20.
Construct levels as:

```python
BookLevel(price=price, quantity=size, order_count=None)
```

Reject empty sides and `best_bid >= best_ask`. Use the response receipt for both normalized times
and emit:

```python
AdapterWarning(
    code="DYDX_REST_BOOK_LOCAL_TIMESTAMP",
    venue=Venue.DYDX,
    endpoint=f"/v4/orderbooks/perpetualMarket/{symbol}",
    symbol=symbol,
    message="dYdX REST book has no venue timestamp or sequence; local receipt time was used",
)
```

- [ ] **Step 4: Add failing structural and semantic book tests**

Parametrize missing sides, non-list sides, non-mapping levels, missing price/size, zero/negative
price or size, invalid decimals, duplicate prices, one empty side, locked book, and crossed book.
Assert a failure returns no partial batch even when an earlier requested asset response was valid.

- [ ] **Step 5: Run all adapter tests and static checks**

Run:

```bash
.venv/bin/python -m pytest tests/venues/test_dydx.py -q
.venv/bin/ruff format --check src/polytrading/venues/dydx.py tests/venues/test_dydx.py
.venv/bin/ruff check src/polytrading/venues/dydx.py tests/venues/test_dydx.py
```

Expected: PASS.

- [ ] **Step 6: Commit book evidence**

```bash
git add src/polytrading/venues/dydx.py tests/venues/test_dydx.py tests/fixtures/dydx/orderbook.json
git commit -m "feat(venues): collect dydx public order books"
```

---

### Task 4: Warning propagation and explicit CLI routing

**Files:**

- Modify: `src/polytrading/venues/synchronized.py`
- Modify: `src/polytrading/cli.py`
- Modify: `tests/venues/test_synchronized.py`
- Modify: `tests/test_cli.py`

**Interfaces:**

- Consumes: `DydxPublicAdapter`, `AdapterWarning`, `PublicRecorder`, and existing synchronized-book transactions.
- Produces:
  - `_parse_venues("all") == (Venue.BYBIT, Venue.HYPERLIQUID, Venue.DYDX)`.
  - `_render_adapter_warning(warning: AdapterWarning) -> str`.
  - `_record_public_batch(recorder: PublicRecorder, batch: AdapterBatch) -> None`.
  - `SynchronizedBookCollector(..., warning_sink: Callable[[AdapterWarning], None] | None = None)`.

- [ ] **Step 1: Write failing parser and factory-routing tests**

Assert both `collect public` and `collect books` accept `dydx`, reject an unknown venue through the
existing usage-error path, and expand `all` in the exact order Bybit, Hyperliquid, dYdX. Patch the
three adapter constructors and `make_public_http_client`, enter `public_adapter_session`, and
assert one separate client and the correct concrete adapter for each enum. Assert the funding-cycle
path still supplies exactly `(Venue.BYBIT, Venue.HYPERLIQUID)`.

- [ ] **Step 2: Run the CLI routing tests and observe failure**

Run: `.venv/bin/python -m pytest tests/test_cli.py -q -k 'dydx or public_adapter_session or funding_cycle_uses'`

Expected: FAIL because the parser and factory do not know dYdX.

- [ ] **Step 3: Implement explicit parser and factory routing**

Import `DydxPublicAdapter`. Add `dydx` to both argparse choice tuples. Implement:

```python
def _parse_venues(value: str) -> tuple[Venue, ...]:
    if value == "all":
        return (Venue.BYBIT, Venue.HYPERLIQUID, Venue.DYDX)
    return (Venue(value),)
```

In `public_adapter_session`, use `if`/`elif` for all three concrete venues and an explicit
`raise ValueError(f"unsupported public venue: {venue.value}")` fallback. Do not change the fixed
pair passed by `_collect_funding_cycle`.

- [ ] **Step 4: Write failing public and synchronized warning tests**

For public collection, return a valid batch with one `DYDX_MARK_PRICE_UNAVAILABLE` warning and
assert standard error is exactly one stable line:

```text
polytrading: warning: dydx DYDX_MARK_PRICE_UNAVAILABLE BTC-USD /v4/perpetualMarkets: dYdX public market evidence has no documented mark-price field
```

For synchronized books, return a valid book batch with one local-time warning and assert the
injected sink receives it only after batch validation. An invalid-lineage batch's warnings must not
reach the sink. Existing callers that omit `warning_sink` must behave unchanged.

- [ ] **Step 5: Implement deterministic warning propagation**

Render without response content:

```python
def _render_adapter_warning(warning: AdapterWarning) -> str:
    return (
        f"polytrading: warning: {warning.venue.value} {warning.code} "
        f"{warning.symbol} {warning.endpoint}: {warning.message}"
    )
```

`_record_public_batch` validates/persists through `PublicRecorder.record` and then prints warnings
sorted by `(venue.value, code, symbol, endpoint, message)`. Replace direct public recorder calls
with this helper. Add `warning_sink` to `SynchronizedBookCollector`; after structural and lineage
validation, emit each successful batch's warnings in the same stable key order. Pass a CLI sink
that prints `_render_adapter_warning` to standard error from `collect_book_cycles`.

- [ ] **Step 6: Update all-venue expectations and run regression tests**

Update generic public/book CLI tests from two to three adapters and expected record counts. Keep
all funding-cycle and funding-health fixtures at two venues. Run:

```bash
.venv/bin/python -m pytest tests/test_cli.py tests/venues/test_synchronized.py -q
.venv/bin/ruff check src/polytrading/cli.py src/polytrading/venues/synchronized.py tests/test_cli.py tests/venues/test_synchronized.py
```

Expected: PASS.

- [ ] **Step 7: Commit CLI and warning integration**

```bash
git add src/polytrading/cli.py src/polytrading/venues/synchronized.py \
  tests/test_cli.py tests/venues/test_synchronized.py
git commit -m "feat(cli): expose dydx public evidence"
```

---

### Task 5: Operator documentation, autonomous review, and full verification

**Files:**

- Modify: `README.md`
- Modify: prior task files only when a failing regression test justifies a review correction.

**Interfaces:**

- Consumes: every interface from Tasks 1–4.
- Produces: an operator-visible research command and reproducible evidence that the implementation respects the approved boundary.

- [ ] **Step 1: Document the dYdX research path and limitations**

Add these examples near the existing public and book collection sections:

```bash
.venv/bin/polytrading collect public \
  --venue dydx \
  --assets BTC,ETH,SOL \
  --db var/dydx-public.duckdb

.venv/bin/polytrading collect books \
  --venue dydx \
  --assets BTC,ETH,SOL \
  --once \
  --db var/dydx-books.duckdb
```

State that dYdX adds raw instruments, realized hourly funding, and locally timestamped REST books;
the current schema deliberately emits no dYdX `MarketSnapshot` because no documented mark field
is present. State that same-USDC collateral is only a reason to investigate compatibility, not
proof of compatibility, profitability, access, or live eligibility.

- [ ] **Step 2: Run focused coverage and fill only real gaps**

Run:

```bash
.venv/bin/python -m pytest tests/venues/test_dydx.py tests/venues/test_synchronized.py tests/test_cli.py \
  --cov=polytrading.venues.dydx --cov=polytrading.venues.synchronized \
  --cov=polytrading.cli --cov-report=term-missing -q
```

Inspect uncovered branches. Add tests only for reachable semantic branches such as malformed
input, cursor progress, partial-universe rejection, or warning isolation; do not add coverage-only
exclusions.

- [ ] **Step 3: Run formatting, lint, whitespace, and full coverage gates**

Run:

```bash
.venv/bin/ruff format --check .
.venv/bin/ruff check .
git diff --check
.venv/bin/python -m pytest --cov=polytrading --cov-report=term-missing --cov-fail-under=90 -q
```

Expected: every command exits zero and repository coverage remains at least 90%.

- [ ] **Step 4: Audit authority, endpoints, and legacy semantics**

Run:

```bash
rg -n -i "api.?key|private.?key|wallet|place.?order|submit.?order|cancel.?order|withdraw|transfer|authenticate|sign" src/polytrading/venues/dydx.py tests/venues/test_dydx.py
rg -n "Venue\.BYBIT, Venue\.HYPERLIQUID|FundingCollectionCycle|CarryPersistenceStudy|CarryAuditor" src/polytrading tests
rg -n "https://" src/polytrading/venues/dydx.py
```

The first scan may match negative assertions only; the adapter must define no such capability. The
only dYdX host must be the public indexer. Review every two-venue production match and confirm it is
the intentionally unchanged prospective-funding or carry-study path, not a generic venue loop.

- [ ] **Step 5: Perform a fresh autonomous diff review**

Review `git diff 9de1135...HEAD` against all 15 design sections. Specifically inspect exact-byte
hashing, response-receipt timing, Decimal parsing, requested-asset completeness, raw lineage,
funding cursor direction and bounds, duplicate conflict behavior, book sorting/truncation,
unavailable-mark behavior, warning visibility, client closure, and unchanged live-authority
boundaries. For every critical or important finding, write a failing regression test first, fix the
smallest cause, and rerun its focused suite.

- [ ] **Step 6: Re-run final gates after every review correction**

Repeat formatter check, lint, diff check, authority scan, and the full coverage suite. Evidence from
before a correction is not final evidence.

- [ ] **Step 7: Commit documentation and review corrections**

```bash
git add README.md src/polytrading/domain/models.py src/polytrading/venues/__init__.py \
  src/polytrading/venues/dydx.py src/polytrading/venues/synchronized.py src/polytrading/cli.py \
  tests/domain/test_models.py tests/fixtures/dydx tests/venues/test_dydx.py \
  tests/venues/test_synchronized.py tests/test_cli.py
git commit -m "docs: explain dydx public evidence boundary"
```

- [ ] **Step 8: Record completion evidence**

Run:

```bash
git log --oneline 9de1135..HEAD
git diff --stat 9de1135...HEAD
git status --short --branch
```

Expected: the feature branch contains only the approved public-evidence work and is clean.
