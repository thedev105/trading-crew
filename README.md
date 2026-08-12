# polytrading

## 1. Research purpose and no-profit disclaimer

`polytrading` is point-in-time, market-neutral market research software. This first increment
collects authenticated-free public evidence for BTC, ETH, and SOL linear perpetuals on Bybit and
Hyperliquid, then produces deterministic carry diagnostics. It does not promise profit, prevent
loss, forecast returns, or recommend a trade. A large displayed spread is an instantaneous
observation, not an investable return.

## 2. Setup and pinned environment

Use Python 3.12 through 3.14. Runtime and development dependencies are exactly pinned in
`pyproject.toml` so a fresh editable environment is reproducible:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
mkdir -p var
.venv/bin/python -m pytest
.venv/bin/ruff check .
```

All prices, rates, quantities, and diagnostic arithmetic use decimal values. Timestamps must be
timezone-aware and are normalized to UTC.

## 3. Deterministic fixture replay

The checked-in JSONL fixture contains exact public payload text plus normalized records. Replay
validates the whole file, verifies every raw SHA-256 hash and same-venue normalized lineage, then
stores each batch raw-first in one transaction. A malformed later row aborts the entire file;
replaying the same exact file is idempotent.

```bash
.venv/bin/polytrading replay \
  --input tests/fixtures/replay/public_snapshot.jsonl \
  --db var/replay.duckdb
.venv/bin/polytrading carry audit \
  --db var/replay.duckdb \
  --as-of 2026-08-12T12:00:00Z \
  --format text
```

## 4. Public-network smoke collection

The following manual smoke command uses only unauthenticated public HTTP endpoints. Collection has
a maximum span of seven days; the default is the seven-day span ending when the command starts.
HTTP requests identify this package, use a 10-second connect timeout and a
30-second read timeout, and retry only rate limiting and selected transient server responses with
a bounded deterministic backoff.

```bash
.venv/bin/polytrading collect public \
  --venue all \
  --assets BTC,ETH,SOL \
  --start 2026-08-05T12:00:00Z \
  --end 2026-08-12T12:00:00Z \
  --db var/public.duckdb
```

This public-network smoke is manual and is not a CI or completion gate. Exchange availability and
response changes are external conditions; automated tests use checked-in responses and mock HTTP
transports.

Bybit funding intervals are resolved only from instrument specifications already known at each
funding event's point in time. A fresh database therefore stores current Bybit instruments and
snapshots but skips a requested historical Bybit funding range when no Bybit specification was
known at the range start. The command remains successful and prints a stderr warning naming the
venue, asset, requested range, and the fact that funding was not collected; other eligible venue
data is still stored. Never backdate a newly observed specification. Build this basis by retaining
the database across public collections over time, or replay previously captured raw-first batches;
once a specification exists at the requested start, later Bybit collections normalize that range.

## 5. Synchronized 20-level book collection

Book cycles start the selected venue requests concurrently and request exactly 20 levels for each
asset. Run one cycle with `--once`, or collect for a bounded duration:

```bash
.venv/bin/polytrading collect books \
  --venue all \
  --assets BTC,ETH,SOL \
  --duration-seconds 3600 \
  --interval-seconds 5 \
  --db var/public.duckdb
```

Every cycle records request timing, source hashes, failures, gaps, and maximum exchange-effective
timestamp skew. A failed venue is never filled from an older cycle, and collection continues with
bounded backoff. Bybit exposes snapshot sequence evidence; Hyperliquid's public REST response does
not expose a REST sequence number, so its sequence is recorded as absent. Even where a REST
snapshot includes an identifier, repeated snapshots do not prove continuous sequence integrity,
atomic cross-venue state, or the absence of unseen book changes between requests.

## 6. Carry audit interpretation

Every audit requires an explicit point-in-time cutoff. Text and canonical JSON always order assets
as BTC, ETH, and SOL and report research-only warnings. `DIAGNOSTIC_ONLY` means current compatible
funding and book evidence was present; it is not permission to trade. `STALE`, `INSUFFICIENT_DATA`,
and `INELIGIBLE` explain why the evidence fails closed.

A large raw annualized spread can still be `INELIGIBLE`. Annualization is only
`hourly_rate × 8760`, not a forecast. Missing contract semantics, different collateral or P&L
assets, funding timing, stale observations, book gaps, or excessive effective-time skew can all
invalidate comparison. The current public metadata intentionally leaves several compatibility
fields unknown, while Hyperliquid uses USDC and Bybit settles the selected contracts in USDT.

## 7. Database backup, replay, and schema versions

DuckDB files are append-only research stores. Close collectors before copying a database file for
backup, retain the source JSONL beside it, and test restoration by replaying into a new database
rather than overwriting an existing one. An exact replay is idempotent; a conflicting immutable
identity is rejected.

Every raw, normalized, registry, experiment, cycle, journal, and report record carries a schema
version. SQL migrations are embedded in the installed package, recorded in `schema_migrations`,
and applied forward-only when a store opens. Older facts are not rewritten to impersonate a newer
schema. Preserve the original database before upgrading code across schema versions.

## 8. Explicit read-only boundary

The package contains no credentials, account authentication, private-key or wallet handling,
balance or position access, signing, deposit, withdrawal, transfer, order placement, order
cancellation, allocation, or execution methods. Venue adapters expose public instruments, funding,
market snapshots, and order-book snapshots only. Do not add account or trading surfaces to this
research increment.

## 9. Evidence still required by the Class C activation gate

This increment does not activate automated trading. A separate reviewed activation decision still
requires all of the following evidence:

- 12 months of point-in-time history;
- 45 continuous days of synchronized books and explicit gap accounting;
- independently validated fee and slippage models;
- a reversal and forced-exit reserve model;
- the complete stress suite;
- 90 forward days without look-ahead leakage;
- ledger reconciliation;
- a documented eligibility and legal/compliance review.

Until that gate is satisfied, the only valid output is read-only research evidence and diagnostics.
