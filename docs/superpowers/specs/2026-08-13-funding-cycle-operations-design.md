# Funding Cycle Operations and Health Design

**Date:** 2026-08-13
**Status:** Approved under the operator's standing autonomous-review instruction
**Scope:** Portable hourly invocation and read-only monitoring for prospective public funding cycles

## 1. Objective

The point-in-time funding collector can already record one explicitly named UTC-hour boundary. The
next operational risk is not strategy selection; it is silently losing the prospective record
through scheduler mistakes, late starts, or unnoticed degraded cycles.

This increment makes hourly use safer and makes gaps visible. It adds:

- a current-boundary mode that an external scheduler can invoke without constructing timestamps;
- a read-only health audit over the most recent fully auditable hourly boundaries;
- deterministic JSON and text reports suitable for humans and simple monitors; and
- documented portable scheduling examples.

It does not install a scheduler, keep a process alive, retry an old hour as new evidence, call
private APIs, access credentials or accounts, submit orders, or authorize any trading activity.

## 2. Chosen architecture

### 2.1 External scheduler with stateless commands

An external scheduler invokes a short-lived command shortly after each UTC hour. The command
derives the just-ended boundary from its own UTC start time and then uses the existing one-shot
collector. A separate command opens the same DuckDB read-only and audits recent cycles.

This is preferred over an internal daemon because operating-system or hosted schedulers already
handle restarts and process supervision. It is preferred over a job queue because one hourly job
and one local database do not justify another stateful service.

### 2.2 Alternatives rejected for this increment

- **Internal async loop:** easier to demonstrate but harder to supervise, upgrade, and recover
  without double runs. It also duplicates scheduler behavior inside research code.
- **Database job queue or cloud scheduler integration:** useful for a multi-worker deployment, but
  adds credentials, leases, provider coupling, and failure modes before the evidence protocol is
  proven.

## 3. Current-boundary collection

The existing command gains a required mutually exclusive choice:

```text
polytrading collect funding-cycle \
  --db var/forward.duckdb \
  (--cycle-end 2026-08-13T17:00:00Z | --current) \
  --assets BTC,ETH,SOL \
  --format json
```

`--cycle-end` preserves the explicit deterministic interface. `--current` captures `_utc_now()`
exactly once for routing and floors that aware UTC time to the current whole hour. For example,
`17:01:30Z` resolves to `17:00:00Z`.

The same captured time is used for the early/on-time/late routing decision. The collector can use
subsequent response clocks, but the boundary cannot shift if the wall clock crosses an hour while
the command is running.

Current mode has the same fail-closed timing behavior as explicit mode:

- through five minutes after the resolved boundary, inclusive, venue requests are allowed;
- after five minutes, no venue session is opened and a `late_not_collected` cycle is appended; and
- a later invocation never requests an older boundary or relabels historical data.

Repeated invocations remain independent append-only attempts. Current mode does not suppress a
duplicate scheduler run because retaining every attempt is more auditable than a hidden lockout.

## 4. Health audit window

The new command is:

```text
polytrading funding health \
  --db var/forward.duckdb \
  --hours 24 \
  --as-of 2026-08-14T17:06:00Z \
  --format json
```

`--hours` is an integer from 1 through 2,160, allowing up to 90 days without an unbounded query.
`--as-of` is optional and defaults to the command's UTC clock; supplying it makes the report
reproducible.

A boundary becomes auditable only after its five-minute collection window has closed. The audit
computes the latest whole-hour boundary `B` such that `B + 5 minutes <= as_of`, then evaluates
exactly `hours` boundaries ending at `B`. Thus at `17:02Z` the `17:00Z` cycle is still open and the
latest auditable boundary is `16:00Z`; at `17:06Z`, `17:00Z` is included.

The database must already exist and is opened read-only. The health command performs no migration,
network request, collection, repair, or write.

## 5. Boundary assessment

Every expected boundary produces one `FundingBoundaryHealth` record with:

- schema version and exact boundary;
- `missing`, `late`, `degraded`, or `complete` effective status;
- total attempt count;
- complete, degraded, and late attempt counts;
- the selected cycle UUID, selected completion time, and selected source hashes when an attempt
  exists; and
- sorted reason codes.

Only attempts whose `request_completed_at` is at or before `as_of` participate in a report, so a
reproducible historical audit cannot see a later retry. Eligible attempts are ranked `complete`,
then `degraded`, then `late`. The selected attempt is the earliest completion within the best
available rank, with cycle UUID as a stable tie-breaker. A later successful retry can therefore
establish complete evidence in subsequent reports without erasing the earlier failed or late
attempt; all eligible counts remain visible.

Reason codes are deterministic:

- `BOUNDARY_MISSING` when no attempt exists;
- `BOUNDARY_LATE_ONLY` when every attempt is late;
- `BOUNDARY_DEGRADED_ONLY` when no complete attempt exists but at least one is degraded;
- `MULTIPLE_ATTEMPTS` when more than one cycle names the boundary; and
- `MULTIPLE_COMPLETE_ATTEMPTS` when more than one complete cycle names the boundary.

Duplicate attempts are warnings, not evidence failures by themselves. A boundary is complete when
at least one immutable cycle is complete.

## 6. Overall health model

`FundingCollectionHealthReport` contains:

- schema and protocol versions;
- `as_of`, latest auditable boundary, requested hours, and exact first/last boundaries;
- one canonically ordered boundary record per requested hour;
- overall status;
- counts by effective boundary status;
- complete coverage ratio as an exact Decimal string in JSON;
- current complete streak measured backward from the latest auditable boundary;
- sorted union of selected source hashes; and
- exact research-only warnings.

Overall statuses are:

- `healthy`: every expected boundary is complete;
- `degraded`: there are no missing or late-only boundaries, but at least one is degraded-only; or
- `critical`: at least one boundary is missing or late-only.

The precedence intentionally distinguishes a source problem (`degraded`) from a prospective record
gap (`critical`). Coverage is `complete_boundary_count / requested_hours`; it is not a profitability
or strategy-quality metric.

## 7. CLI and exit semantics

The health command prints exactly one report. It returns:

- `0` for `healthy`;
- `1` for `degraded` or `critical`, so a scheduler can alert without parsing prose; and
- `2` for invalid input or an unavailable/non-current database through the existing sanitized
  usage-error path.

Collection retains its current semantics: zero means an immutable cycle was written, including a
degraded or late diagnostic. Operators must inspect that collection output or run the health
audit; process success alone does not mean evidence completeness.

## 8. Rendering

Canonical JSON uses sorted keys, two-space indentation, RFC 3339 `Z` timestamps, enum values, UUID
strings, Decimal strings, and arrays for tuples.

Text begins with overall status, audit time, exact boundary span, coverage, and current complete
streak. It then prints one stable line per hour with status, attempt counts, selected cycle, and
reasons. The report ends with exact warnings that say:

- this is collection health, not strategy or return evidence; and
- no credentials, accounts, positions, or orders were accessed.

Neither renderer may recommend a trade, claim expected profit, or imply live eligibility.

## 9. Error handling and integrity

- Naive timestamps, non-integer/bool hours, and hours outside 1..2,160 fail before database use.
- `as_of` before the Unix epoch is rejected rather than producing invalid boundary arithmetic.
- Stored cycles are revalidated by `FundingCollectionCycle` when read.
- The report validator recomputes expected boundaries, counts, coverage, streak, source hashes, and
  overall status; callers cannot construct a contradictory report.
- Missing boundaries remain explicit. The auditor never inserts synthetic cycles or zero funding.
- Read-only schema verification prevents an old database from being silently interpreted under the
  current model.

## 10. Testing

Unit and property tests cover:

- exact five-minute auditable-boundary transitions;
- one-hour and 90-day bounded windows, including leap-day/calendar transitions;
- missing, late-only, degraded-only, complete, and repeated-attempt aggregation;
- deterministic best-attempt selection and reason ordering;
- exact count, coverage, streak, and source-hash conservation;
- canonical JSON/text and forbidden authority claims;
- `--current` resolving once without an explicit date;
- current-mode on-time and late dispatch without accidental older-boundary requests;
- health CLI exit 0/1/2 and read-only/no-network behavior; and
- the complete existing suite with at least 90% total coverage.

## 11. Operator guidance

Documentation will show a generic cron example running current collection shortly after each hour
and a separate health check after the five-minute cutoff. The examples redirect logs but do not
install cron or assume a particular server, timezone, cloud, or notification provider.

The first Bybit cycle may remain degraded for bootstrap. Continuous complete health can begin only
after a prior point-in-time instrument specification exists. A monitor alert is evidence of an
operational or source gap, not a command to trade, retry historically, or weaken the protocol.
