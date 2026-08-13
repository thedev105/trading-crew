# Point-in-Time Funding Cycle v1

Date: 2026-08-13

Status: Self-reviewed and approved for planning under the user's standing instruction to proceed
and review autonomously

Companion to:

- [Market-Neutral Opportunity Router Design](2026-08-12-market-neutral-opportunity-router-design.md)
- [Carry Persistence Study v1](2026-08-13-carry-persistence-study-design.md)

## 1. Decision

The next increment is an append-only, settlement-aligned **point-in-time funding cycle** for BTC,
ETH, and SOL on Bybit and Hyperliquid. It captures one explicit hourly settlement boundary per
invocation and records both successful evidence and collection failures. The command is suitable
for an external scheduler but does not install a scheduler, run indefinitely, authenticate, or
trade.

This solves the most important evidence gap in the carry persistence study: historical funding
endpoints reveal settled rates, but Bybit's historical response does not carry the historical
funding interval that applied to each payment. The existing collector correctly refuses to
backdate today's instrument specification. Version 1 continues that fail-closed behavior and
starts building an honest prospective record instead.

## 2. Alternatives considered

### 2.1 Backdate the current Bybit interval — rejected

Bybit's funding-history response contains the symbol, rate, and settlement timestamp, while its
separate current instrument endpoint contains `fundingInterval`. Bybit also publishes interval
change announcements, including a historical change affecting SOL and later changes for other
contracts. Applying today's interval to older settlements can therefore manufacture complete
eight-hour blocks that were not actually supported by point-in-time rules.

### 2.2 Reconstruct rules from announcements — rejected for binding evidence

Announcement search can provide useful corroboration, but search completeness, edits, removals,
translations, and emergency changes cannot establish a complete immutable rule timeline. Such a
reconstruction may be quarantined later as non-binding research; it cannot enter the prospective
activation record.

### 2.3 Replace Bybit with another venue — deferred

A venue with a protocol-fixed funding interval could simplify history, but changing venues would
replace the preregistered hypothesis before it is tested. A new venue pair needs its own
compatibility review, cost model, data-use review, and trial-family identifier.

### 2.4 Prospective settlement cycles — selected

An hourly one-shot collector uses only rules that were already present in the local store at the
settlement boundary. The first run records current specifications as bootstrap evidence; a current
Bybit specification is never used retroactively for that same boundary. Later cycles can use the
previously observed specification. This costs calendar time but preserves the meaning of
point-in-time evidence.

## 3. Scope and non-goals

Version 1:

- captures one UTC-hour settlement boundary per command;
- supports exactly BTC, ETH, and SOL and exactly Bybit and Hyperliquid;
- fetches public instrument specifications and exact-boundary funding history;
- stores validated raw responses before normalized records in one transaction;
- records a durable cycle result even when an expected item is missing or a request fails;
- distinguishes instrument capture from on-time, late, bootstrap, no-settlement,
  missing-expected, and failed funding outcomes;
- emits stable text or JSON diagnostics; and
- preserves every existing no-credentials and no-execution boundary.

Version 1 does not:

- backfill historical Bybit intervals;
- infer a payment interval from gaps between historical funding rows;
- install cron, systemd, container orchestration, or a cloud scheduler;
- run a long-lived daemon or retry beyond the five-minute point-in-time window;
- collect synchronized books, fees, account balances, positions, or private data;
- model net profit or recommend a trade; or
- change the carry-study gates or the master Class C activation requirements.

Synchronized books remain a separate high-frequency collection concern. Combining a five-second
book process with an hourly funding boundary would make failure recovery and evidence semantics
harder, not safer.

## 4. Timing protocol

Each invocation names an exact `cycle_end` aligned to a whole UTC hour. The collection clock is
normalized to UTC and evaluated as follows:

- before `cycle_end`: reject the invocation without opening network sessions or writing;
- from `cycle_end` through `cycle_end + 5 minutes`, inclusive: perform the cycle;
- after `cycle_end + 5 minutes`: perform no venue requests and append a `late_not_collected`
  cycle so the missed boundary remains visible.

Each funding request uses `start=cycle_end` and `end=cycle_end`. This prevents a delayed response
from silently importing older settlements. Returned observations must match the requested venue,
asset, symbol, and exact effective timestamp.

The five-minute window matches the persistence study's existing point-in-time classification.
Actual adapter response time remains authoritative. A request that starts on time but completes
after the boundary is stored and labeled late; it cannot qualify as point-in-time evidence.

No internal sleep is used. An external scheduler can invoke the command once shortly after every
UTC hour. A second invocation inside the window is an independent append-only attempt, not an
update or overwrite.

## 5. Evidence model

### 5.1 Funding cycle item

Every requested venue/asset pair produces one `FundingCycleItem` with:

- venue and asset;
- expected symbol;
- an instrument outcome;
- a funding outcome;
- zero or one instrument observation timestamp;
- zero or one funding effective timestamp;
- zero or one funding observation timestamp;
- sorted unique instrument source hashes;
- sorted unique funding source hashes; and
- sorted unique reason codes for every non-successful component.

Instrument outcomes are `captured`, `failed`, or `late_not_collected`. Funding outcomes are:

- `captured`: exactly one exact-boundary funding observation was returned;
- `no_settlement`: a Bybit exact-boundary query succeeded and returned no funding row;
- `missing_expected`: Hyperliquid, whose funding is hourly, returned no exact-boundary row;
- `bootstrap_required`: Bybit had no instrument specification known at `cycle_end` and its
  funding request was intentionally skipped;
- `failed`: a request, response, lineage, or shape validation failed; or
- `late_not_collected`: the command started outside the prospective window and made no request.

Hyperliquid documents hourly funding, so an empty response is not treated as a valid
no-settlement result. Bybit intervals are instrument-specific, and version 1 deliberately does
not guess a historical offset; an empty exact-boundary result is recorded without claiming a gap.
The carry study remains responsible for block completeness.

An instrument response can fail while funding succeeds from a previously known specification, or
the reverse. Keeping the two outcomes separate prevents a single label from hiding either fact.
When a venue-level instrument batch fails, the same sanitized instrument failure code is attached
to every requested asset for that venue. Separate item hash fields include every valid instrument
and funding raw response that supports that venue/asset item; a shared instrument raw hash may
therefore appear in several items. Keeping the fields separate makes component lineage verifiable
without interpreting endpoint names.

### 5.2 Funding collection cycle

`FundingCollectionCycle` contains:

- a random cycle UUID;
- schema and protocol versions;
- exact `cycle_end`;
- ordered requested assets and venues;
- request start and completion timestamps;
- one item for every venue/asset pair;
- overall status;
- sorted unique source hashes; and
- explicit research-only warnings.

Allowed statuses are:

- `complete`: every instrument item was captured, every Hyperliquid funding item was captured,
  every Bybit funding item was either captured or a successful no-settlement response, and every
  captured instrument and funding item was observed within five minutes;
- `degraded`: at least one item failed, was missing, or required bootstrap; or
- `late`: no request was made because the command started late, or at least one captured
  instrument or funding component was observed after the five-minute cutoff.

Model validators enforce canonical item order, exact pair coverage, timestamp order, outcome
field consistency, status derivation, and equality between cycle source hashes and the union of
all item instrument and funding source hashes.

## 6. Collection data flow

The CLI validates timing before constructing its public-adapter session. The on-time collector
receives constructed public adapters, a writable `DuckDBStore`, an exact cycle boundary, and an
injectable UTC clock. A separate pure late-cycle constructor requires no adapter.

1. Validate assets, unique venues, hourly alignment, and start timing.
2. If late, construct and append the late cycle without opening an HTTP client or adapter session.
3. For an on-time cycle, query instrument specifications concurrently by venue.
4. Validate each successful instrument batch, but do not make newly fetched Bybit specifications
   visible to the same boundary's normalization.
5. For each asset, inspect only specifications already committed before or at `cycle_end`.
6. Skip Bybit funding when that historical basis is absent; query every other exact-boundary
   venue/asset pair concurrently.
7. Convert exceptions to stable failure codes, re-raising cancellation.
8. Validate successful batches, exact-boundary identities, raw lineage, and cardinality.
9. Build the cycle and append every successful raw response, normalized instrument, normalized
   funding observation, and the cycle itself in one DuckDB transaction.
10. Render the immutable cycle from the value returned by the collector.

Atomic persistence is important: a completed cycle record must never claim source hashes or
normalized observations that were not committed with it. Conversely, a failed transaction must
leave none of that cycle's records behind.

## 7. Storage and migrations

Migration `003_forward_funding_cycles.sql` adds:

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

Attempts are append-only. Multiple cycle UUIDs may share the same `cycle_end`, allowing an
on-time retry without mutating the earlier diagnostic. Store helpers support exact idempotent
retry by UUID, conflict rejection, ordered cycle queries, and read-only schema verification.

No raw or normalized venue schema is changed.

## 8. CLI

The new command is:

```text
polytrading collect funding-cycle \
  --db var/forward.duckdb \
  --assets BTC,ETH,SOL \
  --cycle-end 2026-08-13T17:00:00Z \
  --format json
```

`--cycle-end` is mandatory. Requiring the scheduler to name the boundary makes accidental catch-up
requests explicit. `--format` defaults to text. Both venues are always requested because a
single-venue run cannot establish the registered cross-venue record.

The command returns zero when a valid cycle record was written, including degraded or late
research outcomes. Input errors return two; an inability to create the cycle record returns one.
Operators and monitors must inspect the reported status rather than treating process success as
evidence completeness.

The CLI uses the existing bounded public HTTP client and retry transport. Request volume is far
below the currently documented Bybit and Hyperliquid limits.

## 9. Errors and failure semantics

- Cancellation is re-raised immediately.
- One venue or asset failure does not erase valid evidence from other items.
- Exception messages are not persisted; stable `venue:asset:ExceptionType` codes avoid leaking
  payloads or machine details.
- Invalid successful batches become `failed` items and their raw data is not persisted.
- Database conflicts or transaction failures fail the command and roll back the entire cycle.
- A late invocation does not attempt a historical repair disguised as forward evidence.
- Missing Bybit point-in-time instrument basis remains `bootstrap_required`, even if the same
  invocation fetched today's specification.

## 10. Rendering

JSON is sorted, versioned, and uses RFC 3339 UTC timestamps. Decimal values, if introduced later,
remain strings. Text begins with the cycle boundary and status, then prints one stable row ordered
by venue and asset, followed by source-hash count and two warnings:

```text
Research only: this cycle does not model costs, basis P&L, or executable returns.
No credentials, accounts, positions, or orders were accessed.
```

No renderer may emit `TRADE`, `APPROVED`, `LIVE_ELIGIBLE`, expected profit, or a recommendation.

## 11. Testing

Tests use fake adapters and an injected clock. They cover:

- naive and non-hour-aligned timestamps;
- before-boundary rejection and the exact five-minute inclusive edge;
- late cycles making zero adapter calls;
- first-cycle Bybit bootstrap followed by a later successful capture;
- exact-boundary requests and rejection of older or newer returned observations;
- Hyperliquid empty response as missing expected versus Bybit empty response as no settlement;
- partial request failure with successful evidence retained;
- cancellation propagation;
- input-order invariance and canonical item ordering;
- source-hash conservation and raw-first lineage;
- transaction rollback on cycle persistence failure;
- migration, exact retry, conflict, and ordered query behavior;
- stable JSON/text output and forbidden-authority strings;
- CLI parsing, dispatch, exit codes, and no credentials or order calls; and
- the complete existing test suite and at least 90% total coverage.

## 12. Source-use and operating boundary

Official documentation confirms technical access; it does not create unrestricted data rights.
Bybit's current API agreement describes a limited, revocable license to develop, test, and support
the user's use while prohibiting repackaging, resale, and commercial exploitation of the API.
Hyperliquid documents public info endpoints and limits, but no official unrestricted proprietary
market-data license was located during this review.

Therefore the implementation and tests use fixtures and fake transports. Running the command
against mainnet remains an operator decision for private research after reviewing the applicable
terms. Raw venue data must stay outside Git and must not be redistributed, sold, used as a hosted
data service, or repurposed for model training without a separately documented right.

## 13. Stop conditions

Do not treat the prospective record as activation evidence when:

- any cycle was collected late or silently omitted;
- Bybit specifications were backdated;
- Hyperliquid hourly rows are missing;
- paired block coverage is below the frozen study threshold;
- raw lineage or cycle hashes fail verification;
- venue terms or user eligibility do not support the intended operation; or
- a downstream report presents gross funding as net account return.

The correct response to a gap is an explicit gap. Calendar time cannot be repaired by relabeling a
later historical download as point-in-time evidence.

## 14. Primary sources

- Bybit funding history: https://bybit-exchange.github.io/docs/v5/market/history-fund-rate
- Bybit instrument information: https://bybit-exchange.github.io/docs/v5/market/instrument
- Bybit rate limits: https://bybit-exchange.github.io/docs/v5/rate-limit
- Bybit API Terms & Conditions dated 2026-03-18:
  https://www.bybit.com/common-static/compliance/legal/BYBIT/df1923006718fbba8ba70d7d762b9866.pdf
- Example Bybit interval change affecting SOL:
  https://announcements.bybit.com/en/article/adjustment-of-funding-rate-interval-for-bnxusdt-egldusdt-enjusdt-flowusdt-slpusdt-solusdt-xemusdt-perpetual-contracts-bltc02b1b4a5cc06b6e/
- Hyperliquid funding mechanics: https://hyperliquid.gitbook.io/hyperliquid-docs/trading/funding
- Hyperliquid historical funding endpoint:
  https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals
- Hyperliquid rate limits:
  https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits
