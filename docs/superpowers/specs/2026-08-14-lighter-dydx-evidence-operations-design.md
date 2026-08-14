# Lighter-dYdX Prospective Evidence Operations v1

**Date:** 2026-08-14
**Status:** Approved in conversation; self-reviewed and approved for planning
**Scope:** Unattended public-data collection, point-in-time readiness auditing, and a read-only
operator view for the Lighter-dYdX shadow-economics candidate
**Authority boundary:** Research evidence only; no accounts, credentials, balances, positions,
orders, fills, transfers, custody, paper execution, or live execution

Companion to:

- [Market-Neutral Opportunity Router Design](2026-08-12-market-neutral-opportunity-router-design.md)
- [Lighter-dYdX Conservative Shadow Economics Design](2026-08-13-lighter-dydx-shadow-economics-design.md)
- [Funding Cycle Operations and Health Design](2026-08-13-funding-cycle-operations-design.md)
- [Local Operator Dashboard Design](2026-08-13-local-operator-dashboard-design.md)

## 1. Decision

The next milestone is a scheduler-friendly prospective evidence pipeline for the already selected
Lighter-dYdX research candidate. It starts the calendar-dependent evidence record needed by the
conservative economics gate and makes missed or unusable hours visible.

The system uses short-lived commands under a new `trial` CLI group:

```text
polytrading trial funding --current --db var/lighter-dydx-trial.duckdb
polytrading trial books \
  --duration-seconds 60 \
  --interval-seconds 5 \
  --db var/lighter-dydx-trial.duckdb
polytrading trial health \
  --recent-hours 24 \
  --db var/lighter-dydx-trial.duckdb
```

An external scheduler or process supervisor invokes them. The project documents a safe schedule
but does not install, edit, or control cron, systemd, Docker, a cloud scheduler, or another host
service.

This milestone does not add a trading engine. Its strongest positive status is
`READY_FOR_ECONOMICS_EVALUATION`, which means only that the public collection record can be passed
to the separately frozen economics evaluator.

## 2. Why this increment comes next

The shadow-economics implementation can already evaluate a complete point-in-time bundle, but a
complete bundle requires facts that cannot be manufactured later:

- a fixed 90-day UTC window of paired hourly Lighter and dYdX funding evidence;
- hourly representative synchronized books across the final 60 days;
- dense consecutive book samples for quote-change and latency stress research;
- a current synchronized depth pair at the evaluation cutoff; and
- explicit visibility into failed, late, missing, and skewed collection attempts.

Historical downloads can help diagnose adapter behavior, but they cannot replace a prospective
operations record. Starting reliable collection is therefore more valuable now than adding
another strategy, AI model, execution simulator, or account integration.

## 3. Approaches considered

### 3.1 External scheduler with bounded commands — selected

Funding, book collection, and health auditing remain independently restartable. The operating
system owns process supervision, while immutable database records own evidence truth. A failed
process cannot silently advance the readiness clock because the corresponding boundary remains
missing.

This approach matches the established funding-cycle operations model, keeps deployment portable,
and allows every command to be tested with injected clocks and fake public adapters.

### 3.2 One internal coordinator daemon — rejected for v1

A single daemon could coordinate books, funding, health, and the dashboard, but it would introduce
process lifecycle, state restoration, internal scheduling, signal handling, and upgrade semantics
before the collection protocol is proven. It would also make one process failure affect every
evidence stream.

### 3.3 Unrelated existing cron recipes — rejected

Invoking the generic public collector and generic all-venue book collector would produce records,
but it would not create candidate-specific funding attempt records, distinguish late from missing
boundaries, or show whether the Lighter-dYdX economics windows are actually complete. It would also
collect two unrelated venues during every book cycle.

### 3.4 Bundled Docker Compose stack — deferred

Packaging is useful after the command contract is stable. Making container lifecycle part of v1
would couple collection correctness to one deployment mechanism and would not solve evidence
semantics. A later deployment increment may wrap the same commands without changing their records.

## 4. Fixed research and operational scope

Version 1 supports exactly:

- candidate identifier `lighter-dydx-core-v1`;
- venues dYdX and Lighter;
- BTC, ETH, and SOL;
- active linear perpetual instruments normalized by the existing public adapters;
- exact whole-hour UTC funding boundaries;
- synchronized 20-level public REST books;
- a five-second dense-book target;
- a five-minute maximum age for an hourly representative book;
- a one-second maximum cross-venue effective-time skew;
- a 30-day training funding window and 60-day evaluation funding window;
- a 60-day evaluation book window;
- a final 168-consecutive-hour current funding window; and
- the existing 99% coverage thresholds.

Version 1 does not:

- collect private or account-specific data;
- infer fills, queue position, continuous exchange sequence, or atomic cross-venue state;
- backfill a missed boundary and call it point-in-time evidence;
- import or approve fees, margin assumptions, latency assumptions, or operating costs;
- generate a policy containing researcher-selected assumptions;
- automatically run an economics evaluation;
- alter an economics threshold to make a candidate pass;
- recommend position size, capital allocation, or a trade; or
- resolve KYC, residency, entity, sanctions, tax, custody, or venue-access eligibility.

The existing `carry economics` command remains the only economics-evaluation entry point. The
operations UI may display its latest immutable result and a copyable recipe, but cannot invoke it.

## 5. Operating model

### 5.1 Recommended hourly schedule

The documented default schedule is:

1. At minute 1, invoke current-boundary funding collection.
2. At minute 4, invoke the same command again as an independent append-only attempt.
3. At minute 6, audit the now-closed funding boundary and recent operational health.
4. At minute 58 of each hour, start a 60-second book burst with a five-second interval.
5. The burst stops at approximately minute 59, leaving no intended overlap with the next hour's
   funding jobs.

The second funding call is not an in-place retry. It receives a new cycle UUID and preserves the
first attempt. Health selection can use a later complete attempt without hiding the earlier one.

This burst produces up to 12 synchronized cycles per hour. That is sufficient to provide an
hourly representative and thousands of within-burst consecutive samples across a 60-day window
without collecting a nearly continuous 20-level book that the frozen evaluator does not require.

The final eligible book cycle before a UTC boundary can represent that boundary only when it
completed at or before the boundary and is no more than five minutes old. A cycle completed after
the boundary can never be selected for that boundary. This retains the economics assembler's
no-look-ahead rule.

The schedule is guidance, not part of data validity. Commands validate timestamps and stored
records rather than trusting that a scheduler ran at the documented minute.

### 5.2 No catch-up mode

Current mode captures one aware UTC time, floors it to a whole hour, and fixes that boundary for
the invocation. An explicit `--cycle-end` mode exists for deterministic testing and carefully
controlled manual operation, but it has the same five-minute admissibility window.

After the window closes, the command makes no venue request and appends a `late` diagnostic. It
does not walk backward through missing hours. A generic historical public collection can remain
available for exploratory analysis, but those observations do not repair trial-cycle health or
qualify for shadow economics.

### 5.3 Database ownership

The existing generic bounded book command keeps DuckDB open for its complete duration. The new
trial book command must not do that because even a scheduled one-minute outage would make the
independent read-only dashboard unnecessarily unreliable and the same lifecycle would scale poorly
if the burst length changes later.

The trial book process instead:

- keeps its public HTTP clients alive for the bounded session;
- fetches and validates one synchronized cycle in memory;
- acquires a bounded local writer lease for the database;
- opens the writable store only for one atomic persistence transaction;
- closes the store and releases the lease before sleeping; and
- repeats until the monotonic session deadline.

The funding command uses the same writer lease. Book persistence yields rather than delaying an
hourly funding attempt beyond its point-in-time window. The recommended non-overlapping schedule
is the primary collision control; the lease is a final guard against duplicate scheduler or
manual invocations.

The lease is local coordination, not evidence. Failure to acquire it produces a sanitized
nonzero command result or failed sample diagnostic; it never modifies an existing record. The
operating documentation states that unrelated legacy writer commands should not target the trial
database while scheduled collection is active.

The dashboard opens the database read-only per request and closes it immediately. A request that
collides with the brief write transaction returns `503 DATABASE_BUSY`. Browser code retries with
bounded backoff and continues to show the previous successful snapshot while labeling it stale.

## 6. Candidate funding-cycle protocol

### 6.1 Separate protocol identity

The existing `point-in-time-funding-cycle-v1` is deliberately fixed to Bybit and Hyperliquid and
must not be generalized silently. This increment introduces:

```text
protocol_version = "lighter-dydx-prospective-funding-v1"
```

Its expected venues, symbols, and settlement semantics are specific to Lighter and dYdX. Both
selected funding series are hourly, so a successful empty exact-boundary response is
`missing_expected`, not a valid no-settlement observation.

### 6.2 Timing

For a named `cycle_end`:

- before `cycle_end`, reject before database creation or network access;
- from `cycle_end` through `cycle_end + 5 minutes`, inclusive, allow public requests;
- after the cutoff, perform no network request and append a `late` cycle; and
- require every captured funding observation to have `effective_at == cycle_end`.

Instrument and funding responses observed after the five-minute cutoff remain durably visible but
make the attempt late. They cannot qualify as point-in-time evidence.

### 6.3 Item outcomes

Every requested venue/asset pair creates one item containing:

- venue, asset, and exact expected symbol;
- instrument outcome and observation time;
- funding outcome, effective time, and observation time;
- separate canonical instrument and funding source-hash sets; and
- sorted stable reason codes.

Instrument outcomes are:

- `captured`;
- `failed`; or
- `late_not_collected`.

Funding outcomes are:

- `captured`;
- `missing_expected`;
- `failed`; or
- `late_not_collected`.

There is no `no_settlement` or `bootstrap_required` outcome in this protocol. Newly observed
instrument metadata is not used to invent an older funding interval; a funding row must itself be
returned for the exact requested hour and normalize to one hour.

### 6.4 Cycle status

A `LighterDydxFundingCycle` contains:

- schema and protocol versions;
- random cycle UUID;
- exact cycle boundary;
- canonical assets and venues;
- request start and completion times;
- exactly one item per venue/asset pair;
- status;
- union of source hashes; and
- exact research-only warnings.

Statuses are:

- `complete`: every instrument and funding component was captured within the window;
- `degraded`: an on-time attempt has at least one failed or missing component; or
- `late`: the invocation began after the window or any successful response was observed after it.

Process exit zero means a valid immutable cycle was appended, including a degraded or late
diagnostic. Consumers must inspect the cycle status. Invalid input returns two; a network,
persistence, or unexpected collection failure that prevents a cycle from being stored returns
one.

### 6.5 Collection and persistence

For an on-time attempt:

1. Validate assets, boundary alignment, and timing before opening clients.
2. Fetch instrument batches concurrently by venue.
3. Request exact-boundary funding concurrently for every venue/asset pair.
4. Re-raise cancellation and convert isolated adapter failures to stable component reason codes.
5. Validate venue ownership, symbols, asset coverage, timestamps, cardinality, interval, and raw
   lineage.
6. Construct the complete typed cycle in memory.
7. Under one writer lease and one DuckDB transaction, append every valid raw response, normalized
   instrument and funding record, and the cycle.
8. Render the immutable value returned by the collector.

An adapter batch that claims success without raw lineage fails validation. A transaction failure
commits none of the cycle. Exception messages, response bodies, local paths, and machine details
are not persisted in reason codes.

### 6.6 Binding prospective lineage

A normalized funding row is eligible for trial readiness or shadow economics only when an on-time
candidate cycle contains a matching `captured` item for the same venue, asset, symbol, exact
boundary, funding source hash, and observation time. A cycle may be degraded because another asset
failed; its individually captured items remain eligible when all of their own invariants pass.

The economics assembler is tightened to use this candidate-cycle linkage. Funding rows imported by
`collect public`, replayed from a fixture, downloaded after the five-minute window, or otherwise
unlinked to an eligible candidate item remain available for exploratory queries but cannot enter
the 30/60-day economics windows.

If multiple eligible attempts contain the same immutable funding value, the earliest observation
and then cycle UUID select the representative while all attempts remain counted. If eligible
attempts disagree on the normalized value for the same venue, asset, and hour, that hour fails
closed with `FUNDING_REVISION_CONFLICT`; the assembler does not choose the newest, highest, or most
favorable rate.

## 7. Synchronized trial books

The generic `BookCollectionCycle` and book snapshot schema remain authoritative. The trial command
constructs exactly the dYdX and Lighter adapters, rather than using `--venue all`, and always
requests all three fixed assets.

Each cycle:

- starts the two venue requests concurrently;
- requests exactly 20 aggregated levels per side and asset;
- validates complete venue and asset coverage;
- retains local receipt timestamps when the REST response has no venue timestamp;
- records raw hashes, request timing, effective timestamps, and maximum skew;
- persists valid books only when the cycle is not failed; and
- records `complete`, `failed`, or `skew_exceeds_research_target` without substituting an older
  snapshot.

The economics assembler continues to apply its stricter per-asset eligibility checks. Operations
health does not reinterpret a failed or excessive-skew cycle as usable merely because another
asset in the response looked valid.

The trial books parser supports mutually exclusive `--once` and `--duration-seconds` modes. The
scheduled operation uses a 60-second burst. Immediately before a later economics evaluation, the
operator runs `--once` and freezes the policy cutoff against that fresh cycle so the evaluator's
30-second latest-book gate is not weakened.

The sampling loop uses a monotonic deadline. Network or persistence failures use bounded backoff,
but a delay never changes a cycle timestamp or causes rapid catch-up requests. Cancellation is
re-raised after in-flight cleanup and database closure.

## 8. Storage

Migration `005_lighter_dydx_trial_operations.sql` adds an append-only table:

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

Multiple UUIDs may name one boundary. Exact identical retry by UUID is idempotent; different
content under the same UUID raises the existing conflicting-record error. Reports are reconstructed
through the typed model, and read-only schema verification rejects an old or partially migrated
database.

No table tracks a mutable scheduler heartbeat. Funding attempts and book cycles are the evidence
of operation. A missing expected boundary remains missing; the health auditor never inserts a
synthetic failure, funding rate, or book.

The existing raw, instrument, funding, book, book-level, book-cycle, reviewed-fee, and economic-
evaluation tables are reused without rewriting prior records.

## 9. Readiness and health audit

### 9.1 Command and cutoff

```text
polytrading trial health \
  --db var/lighter-dydx-trial.duckdb \
  --recent-hours 24 \
  --as-of 2026-11-12T17:05:00Z \
  --format json
```

`--recent-hours` is an integer from 1 through 2,160 and defaults to 24. `--as-of` is optional and
defaults to one captured aware UTC time. Supplying it makes the report reproducible.

The auditor opens an existing current-schema database read-only. It performs no migration,
network request, collection, repair, fee import, policy generation, evaluation, or write.

A funding boundary becomes operationally auditable only when its five-minute collection window
has closed. Cycles, raw records, books, fees, and evaluations observed or completed after `as_of`
are invisible.

### 9.2 Per-boundary assessment

For each expected boundary after trial start, the report evaluates:

- best eligible candidate funding attempt and all attempt counts;
- one exact paired funding row per venue and asset;
- binding on-time candidate-cycle lineage for each selected funding row;
- latest eligible complete synchronized book cycle at or before the boundary;
- book age no greater than five minutes;
- pair skew no greater than one second; and
- source-lineage completeness.

Funding attempts rank `complete`, then `degraded`, then `late`; within a rank the earliest
completion and then UUID provide deterministic tie-breaking. Counts and duplicate warnings retain
the full attempt history.

A book completed after a boundary is never selected for it. A later collection cannot repair a
past book boundary because it did not exist at the cutoff.

### 9.3 Trial start and progress

`trial_started_at` is the earliest auditable UTC boundary with at least one on-time candidate
funding attempt. Hours before it are not counted as scheduler failures. A missing eligible book at
that first boundary remains an explicit book miss; the start is not silently moved forward to
hide it.

The report shows:

- elapsed auditable hours since trial start;
- target 2,160 funding hours;
- target 1,440 evaluation-book hours;
- exact requested, paired, and missing counts by asset and window;
- training, evaluation, total funding, and book coverage;
- the final 168-hour consecutive funding status;
- recent missing, late-only, degraded-only, and duplicate-attempt boundaries;
- latest complete funding and book timestamps;
- dense eligible pair count and observed consecutive samples no more than five seconds apart; and
- a collection-only projected earliest evaluation boundary.

The projected boundary assumes every future boundary is complete and scans forward until the
fixed 30/60-day funding, 60-day book, and 168-hour consecutive-window gates could all pass after
accounting for already known misses. It is labeled as a projection, not a promise or return date.

### 9.4 Coverage identities

Readiness uses the same immutable windows as the economics assembler:

- training funding: 720 requested hours and at least 99% paired;
- evaluation funding: 1,440 requested hours and at least 99% paired;
- total funding: 2,160 requested hours and at least 99% paired;
- evaluation books: 1,440 requested hourly representatives and at least 99% paired; and
- current funding regime: exactly the final 168 consecutive paired hours.

Coverage is reported separately for BTC, ETH, and SOL. Overall readiness is the conservative
minimum across the three assets; one complete asset cannot hide a gap in another.

Dense book counts are operational evidence, not proof that execution quote-change thresholds
pass. Those thresholds depend on a frozen direction, position quantity, latency assumptions, and
stress walk and remain the evaluator's responsibility.

### 9.5 Statuses

The top-level collection status is:

- `NOT_STARTED`: no on-time candidate funding attempt exists by the cutoff;
- `COLLECTING`: a trial has started, recent operational boundaries are usable, but one or more
  fixed evidence windows are not yet mature;
- `DEGRADED`: at least one recent auditable boundary is missing, late-only, degraded-only, or lacks
  an eligible hourly book; or
- `READY_FOR_ECONOMICS_EVALUATION`: all three assets satisfy the fixed funding, hourly-book,
  current-window, lineage, age, and skew requirements at the report cutoff. Because scheduled
  books are intentionally sparse, this status normally appears only immediately after an on-demand
  fresh book cycle; mature historical windows remain visible even when latest depth is stale.

`READY_FOR_ECONOMICS_EVALUATION` does not assert that reviewed fees or an operator policy exists,
that economics are positive, or that paper/live trading is eligible. The report exposes those as
separate prerequisites:

- bundled compatibility dossier available at cutoff;
- reviewed fee evidence present, grouped by venue and tier but not auto-selected;
- latest immutable economics evaluation, if any; and
- operator policy not assessed by health because policies are explicit external inputs.

This separation prevents the collection auditor from selecting a fee tier or manufacturing
research assumptions.

### 9.6 Exit semantics

The health command returns:

- `0` for `COLLECTING` or `READY_FOR_ECONOMICS_EVALUATION` when recent operations are healthy;
- `1` for `NOT_STARTED` or `DEGRADED`; and
- `2` for invalid input or an unavailable/non-current database.

Calendar immaturity is not an alert once collection has started and recent boundaries are healthy.
This prevents 90 expected days of future evidence from making every early health check fail.

## 10. Dashboard

The loopback-only dashboard adds a **Lighter-dYdX prospective trial** section using the same single
captured `as_of` as every other section.

It displays:

- top-level collection status and exact research-only warning;
- elapsed evidence time versus the 90-day funding target;
- per-asset training, evaluation, total funding, and book coverage;
- final 168-hour funding-window completeness;
- collection-only projected earliest evaluation boundary;
- latest complete funding and book cycles with age and skew;
- a recent 24-hour boundary matrix for funding and representative books;
- missing, late, degraded, duplicate, failed, and skewed counts;
- recent sanitized reason codes;
- dense book and consecutive-sample counts;
- compatibility-dossier availability;
- reviewed fee evidence inventory without choosing a tier;
- latest economics decision, policy hash, cutoff, and blockers when a report exists; and
- copyable funding, book, health, scheduler, fee-import, and economics recipes.

The section has no start, stop, retry, import, evaluate, configure, login, wallet, or trade button.
The HTTP application remains GET-only and binds only to `127.0.0.1`.

On a transient `DATABASE_BUSY` response, browser code retries after bounded delays of 250, 500,
and 1,000 milliseconds. It preserves the previous rendered snapshot with a visible stale badge.
After the retry budget, it shows an unavailable state without replacing values with zeros.

## 11. Evaluation handoff

When collection readiness passes, the dashboard and text report show the existing deterministic
handoff rather than invoking it. The operator first captures fresh synchronized depth, updates the
explicit policy cutoff from that immutable result, and then evaluates:

```text
polytrading trial books \
  --once \
  --db var/lighter-dydx-trial.duckdb

polytrading carry economics \
  --policy policy/BTC.json \
  --db var/lighter-dydx-trial.duckdb \
  --evaluated-at 2026-11-12T17:59:10Z \
  --evaluation-id <operator-generated-uuid> \
  --format json
```

The operator must provide a complete frozen policy whose `study_end`, `known_as_of`, fee tiers,
latencies, margins, operational costs, and source hashes satisfy the economics protocol. Health
does not edit that document or choose its values.

The evaluator can still return `INSUFFICIENT_EVIDENCE`, `REJECTED`, or `SHADOW_CANDIDATE`.
Collection readiness cannot promote, suppress, or reinterpret the evaluator's decision.

## 12. Failure and safety semantics

- Cancellation is always re-raised after resources close.
- One venue failure does not fabricate evidence for that venue.
- Missing funding is never normalized to zero.
- A stale or post-boundary book never fills an hourly gap.
- A late exact-boundary response remains late even if its value later matches historical data.
- Source hashes are conserved from raw responses through cycles and readiness reports.
- Duplicate attempts are warnings and counts, not silent overwrites.
- Writer-lease contention never weakens timestamp rules or extends the five-minute funding window.
- The dashboard performs no writes and treats database contention as unavailable data.
- Logs and records contain stable error classes, not secrets, response bodies, or machine details.
- No module imports a signer, wallet, private venue client, account client, balance client, order
  client, transfer client, or credential loader.
- AI is not in the collection, health, readiness, or evaluation decision path. The offline AI
  scout remains a separate hypothesis-generation tool whose outputs cannot alter this protocol.

## 13. Rendering

Funding cycles and health reports have canonical text and JSON renderers. JSON uses sorted keys,
two-space indentation, RFC 3339 `Z` timestamps, enum strings, UUID strings, Decimal strings, and
arrays for tuples.

Every renderer includes exact statements that:

- the artifact measures public research-evidence collection, not expected returns;
- readiness does not authorize paper or live trading; and
- no credentials, accounts, balances, positions, orders, fills, or transfers were accessed.

Renderers cannot emit `TRADE`, `APPROVED`, `LIVE_ELIGIBLE`, a profit promise, or a recommendation.

## 14. Testing

Tests use fake adapters, injected wall and monotonic clocks, temporary databases, and deterministic
UUIDs. They cover:

- parser contracts and exact fixed venue/asset scope;
- current-boundary resolution from one captured clock;
- early, exact-boundary, inclusive five-minute, and late timing edges;
- late collection opening no HTTP session;
- exact-boundary Lighter and dYdX funding requests;
- empty hourly responses as `missing_expected`;
- generic historical or replayed funding never qualifying as trial or economics evidence;
- on-time candidate-cycle item linkage required for every economics funding hour;
- conflicting eligible funding revisions failing closed;
- instrument, funding, identity, interval, timestamp, cardinality, and lineage failures;
- partial venue/asset failures with valid evidence retained atomically;
- transaction rollback and UUID idempotency/conflict behavior;
- canonical cycle model invariants and JSON/text output;
- bounded book-session deadline and backoff;
- HTTP clients remaining open while the database closes between book samples;
- writer-lease acquisition, contention, timeout, and release after exceptions/cancellation;
- no overlapping scheduled writes in the documented schedule;
- dashboard reads succeeding between sample transactions;
- `DATABASE_BUSY` retry, stale badge, and exhausted-retry behavior;
- point-in-time health excluding later funding attempts, books, fees, and evaluations;
- deterministic best-attempt selection and duplicate counts;
- no-look-ahead hourly book selection and exact five-minute age/skew edges;
- trial-start semantics without hiding the first missing book;
- 720/1,440/2,160-hour window identities and exact 99% edges;
- final 168 consecutive funding hours;
- per-asset versus overall conservative readiness;
- projected earliest evaluation boundary with known misses;
- `NOT_STARTED`, `COLLECTING`, `DEGRADED`, and
  `READY_FOR_ECONOMICS_EVALUATION` exit behavior;
- dashboard single-cutoff consistency, canonical ordering, and unavailable values;
- forbidden authority and execution dependencies; and
- the complete suite with at least 90% total coverage.

## 15. Documentation and rollout

The README documents:

- creation of one dedicated trial database;
- manual one-cycle funding and book smoke tests;
- the portable external-scheduler example;
- health command and exit-code monitoring;
- the dashboard command and transient busy behavior;
- the bounded one-minute burst's expected maximum of 12 cycles per hour and approximate normalized
  60-day volume of 4.15 million book levels before evaluator eligibility exclusions, plus explicit
  warning that retained raw-response volume depends on venue payload size;
- disk-growth observation and operator-owned backup/retention decisions;
- explicit prohibition on historical repair of prospective gaps; and
- the economics handoff after evidence readiness.

Rollout begins with several manual cycles and health inspection before unattended scheduling. The
operator verifies clock synchronization, writable disk space, scheduler logs, database path, and
dashboard access. This project does not create directories outside the chosen database's parent,
install a scheduler, send alerts, publish the dashboard, upload evidence, or configure retention.

## 16. Acceptance criteria

The increment is complete when:

- Lighter-dYdX funding cycles can run unattended and preserve every attempt;
- synchronized book sessions release DuckDB between atomic samples;
- missed scheduler runs and unusable hours are explicit in health and the dashboard;
- readiness math matches the frozen economics windows and no-look-ahead selection;
- a historical `as_of` cannot see later evidence;
- the dashboard remains usable during bounded book collection, subject only to brief visible busy
  retries;
- the trial database can be restarted without rewriting or relabeling evidence;
- no account, credential, custody, paper-order, or live-order authority is introduced;
- documentation contains portable commands but performs no host configuration; and
- lint, formatting, type/package checks, the full test suite, coverage, and browser smoke tests
  pass.
