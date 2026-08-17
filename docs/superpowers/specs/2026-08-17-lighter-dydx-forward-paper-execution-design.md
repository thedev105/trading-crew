# Lighter–dYdX Forward Paper-Execution Design

**Status:** Draft — awaiting review
**Date:** 2026-08-17
**Scope:** Simulated (paper) forward execution of an already-admitted `SHADOW_CANDIDATE`
**Authority boundary:** No accounts, keys, signers, custody, balances, or live orders. Every
simulated fill is a deterministic walk of already-collected public book snapshots. This milestone
adds no venue credential, wallet, or execution dependency of any kind.

## 1. Objective

The conservative shadow-economics gate (`docs/superpowers/specs/2026-08-13-lighter-dydx-shadow-
economics-design.md`) answers whether a fixed Lighter–dYdX direction had positive conservative
economics in already-recorded evidence. Its section 10.3 states that advancing past a
`SHADOW_CANDIDATE` decision requires "a new specification, frozen parameters, at least 90
continuous days of queue-aware forward paper execution, complete ledger reconciliation, and a
distinct user approval." This document is that new specification for the first of those steps:
forward paper execution.

It answers one narrow question, forward instead of backward:

> Starting from one specific, already-persisted `SHADOW_CANDIDATE` report, does a simulated
> taker-entry/taker-exit position in its frozen direction remain solvent and directionally correct
> against live public evidence, tracked with full ledger reconciliation?

It does not predict profit, does not infer a real fill, and cannot be consumed by a live execution
interface. The only artifacts it produces are paper positions, paper ledger transactions, and a
read-only dashboard view of both.

## 2. Why Taker-Only, Not Queue-Aware

The shadow-economics design's section 4.3 deferred an event-driven paper simulator because REST
books with local receipt timestamps cannot establish queue position, continuous sequence, or
actual fill probability. That constraint has not changed: this project has no WebSocket
trade-print ingestion, sequencing, or gap-recovery infrastructure for any venue, and building one
is a materially larger project than this milestone.

The shadow-economics gate itself only ever assumed taker entry and taker exit (its section 2). A
taker order's fill is a deterministic function of visible depth at decision time — no queue
position is involved. This design therefore reuses exactly the book-walk and sizing functions
already built and tested in `economics_execution.py` (`walk_book`, `size_shadow_position`,
`entry_slippage_cost`, `forced_exit_cost`), run forward against the *current* book instead of a
historical evaluation window. This keeps the paper executor's fill assumption identical to the
gate that admitted the candidate, rather than introducing a second, inconsistent execution model.

This remains an explicit simplification, stated on every report this milestone produces: simulated
fills assume immediate taker execution against the recorded snapshot and do not model latency
between decision and the venue's actual matching, beyond the latency reserve already charged by the
shadow-economics gate.

## 3. Fixed Scope

- Venues: Lighter and dYdX only, one asset per paper position (BTC, ETH, or SOL).
- A paper position may only be opened from a persisted `economic_evaluations` row whose decision is
  `SHADOW_CANDIDATE` and whose `evaluated_at` is no older than a policy-fixed freshness window (see
  §5).
- Exactly one open paper position per asset at a time. A second `open` for an asset with an
  existing open position is rejected, not queued or netted.
- Entry and exit are both taker walks of the current book, using the same instrument/quantity
  compatibility rules as the shadow-economics gate.
- No leverage: paper collateral is fully backed by the frozen `assigned_capital_usd` from the
  source report; this milestone does not re-run position sizing at open time beyond re-walking the
  *current* book for cost, using the *same* base quantity the source report sized.
- Hourly funding accrual while a position is open, using the same signed funding math as the
  shadow-economics gate (§9.1 of that design).
- Closure is either automatic (regime reversal or 28-day max horizon) or a distinct manual
  operator command; never silent, never retried automatically after a failure.

## 4. Data Model

Two new append-only DuckDB tables, following the existing project convention that research
artifacts are never updated or deleted in place.

### 4.1 `paper_positions`

One immutable row per opened position:

- `position_id` (UUID, primary identity);
- `source_evaluation_id` (FK to `economic_evaluations.evaluation_id`);
- `asset`, `direction` (copied from the source report, not re-derived);
- `opened_at` (actual UTC time of the open command, must not precede the source report's
  `evaluated_at`);
- `base_quantity` (copied from the source report's compatible sizing — not re-sized);
- per-leg opening walk: `lighter_entry_notional_usd`, `dydx_entry_notional_usd`,
  `lighter_entry_price`, `dydx_entry_price`;
- `opening_book_cycle_id` (the book cycle actually walked to open, for audit);
- `schema_version`.

### 4.2 `paper_position_closures`

One immutable row per closed position:

- `position_id` (FK, unique — a position closes exactly once);
- `closed_at`;
- `close_reason`: `REGIME_REVERSED` | `MAX_HORIZON_REACHED` | `OPERATOR_CLOSED`;
- per-leg closing walk: `lighter_exit_notional_usd`, `dydx_exit_notional_usd`,
  `lighter_exit_price`, `dydx_exit_price`;
- `closing_book_cycle_id`;
- `realized_funding_usd` (sum of accrued funding ledger postings while open);
- `realized_pnl_usd` (funding plus exit-vs-entry notional delta, minus exit costs);
- `schema_version`.

A position is open if and only if it has a `paper_positions` row with no matching
`paper_position_closures` row. There is no separate mutable "status" column to avoid drifting out
of sync with the append-only closure record.

### 4.3 Ledger integration

Every state change books one balanced `JournalTransaction` (reusing `ledger/models.py` exactly as
it exists today — no schema change there) into a `paper:` account namespace:

- **Open:** debit `paper:position:<venue>:<asset>` and credit `paper:cash` for each leg's opening
  notional.
- **Hourly funding accrual (while open):** debit or credit `paper:cash` against
  `paper:pnl:funding`, signed per the same per-venue funding calculation the shadow-economics gate
  uses.
- **Close:** reverse each leg's position posting at the exit notional, and post the realized
  entry-vs-exit delta plus exit costs to `paper:pnl:trading`.

Every posting's `evidence_ids` cites the book-cycle or funding-observation record it was computed
from, so a paper P&L figure is always traceable to the exact evidence used — the same traceability
principle the shadow-economics gate already enforces. Reconciliation is: at any instant, summing
all `paper:` account balances must equal the sum of `paper_positions`/`paper_position_closures`
notionals and accrued P&L; a mismatch is a defect, not a possible outcome, and is covered by a
property test (§8).

## 5. Lifecycle

### 5.1 Open

```bash
polytrading trial paper open \
  --evaluation-id <shadow-candidate-uuid> \
  --db var/forward.duckdb \
  --confirm
```

- Loads the named `economic_evaluations` row. Rejects if its decision is not `SHADOW_CANDIDATE`, if
  its `evaluated_at` is older than the policy's freshness window (proposed: 24 hours — a
  `SHADOW_CANDIDATE` computed against evidence that old should be re-evaluated, not blindly traded
  on), or if an open position already exists for that asset.
- Walks the *current* eligible book cycle (same eligibility rules as the shadow-economics gate's
  §7: complete, paired, skew and age bounded) at the source report's frozen `base_quantity`. If
  either leg's current depth cannot fill that exact quantity (`InsufficientDepthError` from the
  reused `walk_book`), `open` fails closed with a stable reason code and writes nothing — it never
  silently downsizes to whatever depth happens to be available, since that would size the paper
  position off evidence the source `SHADOW_CANDIDATE` report never evaluated.
- `--confirm` is required and not implied by any other flag; omitting it prints what *would* happen
  and exits without writing anything.
- Writes the `paper_positions` row and the opening ledger transaction in one DuckDB transaction.

### 5.2 Monitor (scheduled, read/simulate only)

Runs on the same hourly cadence as `collect funding-cycle`. For every open position:

1. Recompute the current 168-hour funding regime in the frozen direction (identical check to the
   shadow-economics gate's §6 current-regime gate). If it has reversed, close with
   `REGIME_REVERSED`.
2. Else if `now - opened_at >= 28 days`, close with `MAX_HORIZON_REACHED`.
3. Else, if a new hourly funding observation pair exists for both venues, post the funding-accrual
   ledger transaction for that hour.

A closure walks the current book for the exit (same eligibility rules as open) and writes the
`paper_position_closures` row plus closing ledger transaction atomically. If no eligible book cycle
exists at the moment a close is triggered, the monitor retries on the next scheduled run rather than
forcing a close against ineligible evidence — the position simply stays open one cycle longer, and
this is logged, not silently dropped.

### 5.3 Manual close

```bash
polytrading trial paper close --position-id <uuid> --db var/forward.duckdb --confirm
```

Same walk/write path as an automatic close, with `close_reason = OPERATOR_CLOSED`.

## 6. Dashboard: Paper Positions

A new read-only section, structurally consistent with the existing dashboard's `carry-grid` /
`trial-detail-card` / `status-pill` / `count-grid` components (`src/polytrading/web/assets/`) — no
new visual language, no framework, same dark surface (`--base`/`--panel`) and token set
(`--cyan`, `--amber`, `--coral`, `--info`, `--muted`, `--dim`, `--line`).

**Stat tiles** (reusing `.count-grid`, four tiles): open positions, closed — regime-reversed,
closed — max-horizon reached, and one hero-style tile for aggregate mark-to-model P&L across all
open positions (value in `--ink`, proportional figures per the project's existing number
treatment — not `tabular-nums`, which is reserved for the table view below).

**Status badges** reuse the existing `.status-pill` component (border `currentColor`, uppercase,
mono) with three states mapped onto tokens already in the page's palette — no new hues introduced:

| State | Token | Rationale |
|---|---|---|
| `OPEN` | `--cyan` | matches the page's existing accent-for-active-state usage |
| `CLOSED_REGIME_REVERSED` | `--amber` | an unplanned defensive exit — warrants the warning tone |
| `CLOSED_MAX_HORIZON_REACHED` | `--info` | a planned, expected exit — neutral, not a warning |

**Per-position card** (one `trial-detail-card` per position, open or closed): asset, venues,
direction, status badge, opened/closed timestamps, source evaluation ID (linking back to the
Conservative economics section), and one inline SVG sparkline of mark-to-model P&L over time built
from the position's own hourly funding-accrual ledger postings plus the running exit-walk estimate.

The sparkline is a single series, so it carries no legend box (the card header already names what
it plots): a 2px line in `--dim` (de-emphasis, consistent with the "trend" convention for a
single-series figure), a hairline zero baseline in `--line` so the reader can see profit/loss
crossings without a color-flipping line, and one 8px end-marker at the current value, colored
`--cyan` if the current mark-to-model P&L is nonnegative and `--coral` if negative — the only place
color encodes the data, and only at the one point that matters (the current figure). One direct
label at that endpoint states the current USD P&L; no other point on the line is labeled, per the
project's existing sparing-labeling convention elsewhere on the page. Underneath, the existing
`.table-shell` pattern renders the exact hourly funding-accrual rows for anyone who wants the
non-visual, fully tabular source of truth — the chart never gates access to a number the table
doesn't also carry.

The page cannot open, close, or otherwise mutate a position. Every value shown is read from
`paper_positions`/`paper_position_closures`/the paper ledger as of the dashboard's existing single
captured `as_of` cutoff, the same point-in-time consistency rule the rest of the dashboard already
enforces.

## 7. Failure and Transaction Semantics

- Every open/close is one DuckDB transaction: position row, closure row (if applicable), and ledger
  transaction succeed or fail together. A failed attempt writes nothing.
- `--confirm` is mandatory on every mutating command; there is no default-yes path.
- The monitor never opens a position — only closes. Opening is always the distinct human action
  from §5.1.
- A `SHADOW_CANDIDATE` report that has gone stale (past the freshness window) blocks `open` with a
  clear, stable reason code rather than silently opening against outdated evidence.
- Database errors surface as a stable CLI failure; the dashboard never leaks a filesystem path.

## 8. Testing Strategy

Same rigor and structure as the shadow-economics module:

- **Unit tests** for the open/close walk logic, reusing the existing `economics_execution.py`
  fixtures where the book/instrument shapes are identical.
- **Ledger property tests**: for any sequence of open → N funding accruals → close, the sum of all
  `paper:` account postings reconciles exactly to `realized_pnl_usd` in the closure row — this is
  the "complete ledger reconciliation" requirement from the shadow-economics design's section 10.3,
  made concrete and testable.
- **Monotonic tests**: a worse funding observation in the frozen direction cannot improve
  `realized_pnl_usd`; a wider exit spread cannot improve it either.
- **State-machine tests**: cannot open a second position for an asset while one is open; cannot
  close a position twice; cannot open from a non-`SHADOW_CANDIDATE` or stale evaluation.
- **CLI and dashboard tests**: `--confirm` gating, transactional persistence, stable exit codes, and
  a dashboard render test confirming the page never exposes a control that mutates a position.
- All fixtures are exact and offline, exactly as today — no live venue or WebSocket dependency
  anywhere in this module.

## 9. Deferred Work

Unchanged from the shadow-economics design's own deferred list, still fully unaddressed by this
milestone:

- queue-position or fill-probability modeling (would require new WebSocket trade-print
  infrastructure this project does not have);
- any live order state machine, venue credential, wallet, or custody dependency;
- KYC, entity, tax, sanctions, and jurisdiction eligibility;
- dynamic allocation across strategy classes;
- any claim of expected or guaranteed profit.

Advancing from a completed 90-day forward paper-execution track record to anything resembling live
execution requires its own new specification and a distinct user approval, exactly as the
shadow-economics design already states. Nothing in this document authorizes that step.

## 10. Completion Criteria

This milestone is complete when:

- the `paper_positions`/`paper_position_closures` schema and ledger integration exist and pass the
  reconciliation property tests;
- `trial paper open|close` and the hourly monitor implement every rule in §5;
- the dashboard renders the Paper Positions section read-only, consistent with the existing visual
  system;
- no venue credential, wallet, or live-order dependency has entered the dependency graph; and
- a final scope audit confirms the output cannot authorize live trading, matching the same audit
  the shadow-economics design required of itself.
