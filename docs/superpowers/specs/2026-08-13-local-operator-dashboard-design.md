# Local Operator Dashboard Design

**Date:** 2026-08-13  
**Status:** Approved by delegated product judgment; implementation may proceed after plan review  
**Scope:** A local, read-only web interface for the existing evidence and research system

## 1. Goal

Make the current research system understandable at a glance without weakening its evidence or
authority boundaries. An operator should be able to start one local command, open a browser, and
answer these questions:

- Is prospective funding collection healthy?
- When did each venue and asset last produce instrument, funding, and book evidence?
- What are the latest realized funding rates and top-of-book values actually stored?
- Is the existing Bybit/Hyperliquid carry audit ready, and why is an asset failing closed?
- How much evidence is in this database?
- Which existing CLI command should be run next?

This dashboard does not predict returns, recommend a trade, collect data, run a shell command, or
control an account. It is an operator console for local evidence already present in DuckDB.

## 2. Decision and Alternatives

### Selected: standard-library local server

Add a small Python package that uses `http.server.HTTPServer` to serve packaged HTML, CSS, and
JavaScript plus one typed JSON snapshot endpoint. The CLI starts it with:

```bash
polytrading dashboard --db var/forward.duckdb --port 8787
```

The server binds only to `127.0.0.1`. It adds no runtime dependency and keeps the current Python
deployment model intact.

### Rejected for this increment: FastAPI plus templates

FastAPI would make routing familiar, but it adds a framework, template engine, and ASGI server for
four GET routes. Those dependencies are not justified until the UI needs authentication, remote
access, streaming, or a larger API.

### Rejected for this increment: static report export

A generated HTML file has the smallest server attack surface, but it goes stale immediately and
does not give the operator a reliable view of an hourly collector. It may be useful later for
portable experiment reports, not for operations.

## 3. Trust and Authority Boundary

The dashboard is intentionally less powerful than the CLI:

- The address is fixed to IPv4 loopback. There is no `--host` escape hatch.
- All routes are `GET`; every other method returns `405 Method Not Allowed`.
- The database path is fixed at process startup and never accepted from an HTTP request.
- Every API refresh opens the database with `read_only=True`, builds one snapshot, and closes it.
- The web package imports no venue adapter, HTTP client, credential provider, wallet, signer,
  subprocess runner, scheduler, or execution module.
- The server has no collection, order, balance, position, transfer, or configuration mutation
  endpoint.
- It emits no permissive CORS header and accepts only loopback/localhost `Host` values.
- HTML contains no database-derived interpolation. Client code creates nodes with `textContent`;
  it never assigns evidence to `innerHTML`.
- Responses use a restrictive Content Security Policy, `X-Content-Type-Options: nosniff`,
  `Referrer-Policy: no-referrer`, and `Cache-Control: no-store` for HTML and JSON.

The UI always displays “Research only — no trading authority.” A healthy collector or an eligible
diagnostic cannot be styled or worded as a buy/sell signal.

## 4. Operator Experience

The interface is one responsive page with four sections and no external fonts, scripts, analytics,
or CDNs.

### Header

The header shows the product name, the read-only badge, the database filename, the snapshot `as_of`
time, and refresh state. The browser refreshes every 15 seconds and provides a manual refresh
button. A failed refresh preserves the last good snapshot and changes the status to “stale” with
the error code.

### Overview

Four large status cards show:

1. 24-hour funding collection health;
2. complete boundary coverage and current streak;
3. latest funding-cycle status and completion time;
4. latest book-cycle status, completion time, and effective-time skew.

A 24-cell boundary strip shows complete, degraded, late, or missing hours. The exact UTC hour and
reason codes are available as accessible text/tooltips; color is never the only signal.

### Markets

A deterministic venue/asset grid covers Bybit, Hyperliquid, and dYdX for BTC, ETH, and SOL. Each
row contains:

- instrument symbol and observation time;
- latest realized native funding rate, interval, effective time, and observation time;
- best bid, best ask, spread in basis points when both sides are present, and book observation time;
- an explicit unavailable state instead of zero or a fabricated value.

Rates remain decimal strings from storage. The browser may format a display percentage, but the
original native rate remains present in accessible detail. Funding values are not annualized.

### Research gate

The existing Bybit/Hyperliquid carry audit is evaluated at the same dashboard `as_of`. For BTC,
ETH, and SOL the UI shows audit status, funding readiness, book readiness, the native hourly spread
when available, and every fail-closed reason code. The section is labeled “Legacy pair diagnostic,”
because dYdX is not silently inserted into a compatibility decision that has not been designed.

### Evidence and operations

Fixed-table counts show raw envelopes, instruments, funding observations, market snapshots, book
snapshots, book cycles, and funding cycles. A recipe panel shows copyable commands for public
collection, one book cycle, the current funding cycle, and CLI health inspection. Paths are rendered
as text using shell-safe quoting; the browser never executes them.

## 5. Snapshot Contract

`GET /api/v1/dashboard` returns one `DashboardSnapshot` with schema version 1. The server captures
one aware UTC clock value before opening the store. Every query and audit uses that exact value.
The JSON encoder renders timestamps as canonical UTC strings, decimals as strings, UUIDs as
strings, and enums as their values.

The top-level payload contains:

- `schema_version`, `as_of`, `database_name`, and the fixed research warning;
- `funding_health` for exactly 24 auditable hourly boundaries;
- nullable `latest_funding_cycle` and `latest_book_cycle` summaries;
- nine canonically ordered `markets` rows;
- three canonically ordered `carry_rows`;
- fixed-key `evidence_counts`;
- fixed-key, shell-quoted `operation_recipes`.

Ordering is server-defined: venues are Bybit, Hyperliquid, dYdX; assets are BTC, ETH, SOL. JSON
object key ordering is not semantic, but arrays are deterministic for testing and visual stability.

## 6. Components and Data Flow

### `polytrading.web.models`

Strict Pydantic response models define the public snapshot contract and validate nonnegative counts,
canonical ordering, aware UTC timestamps, crossed-book impossibility, and complete venue/asset
coverage. These models contain no methods that access storage or the network.

### `polytrading.web.dashboard`

`DashboardBuilder` accepts the existing store interface, a database display name, and an `as_of`.
It composes existing point-in-time store reads, `FundingCollectionHealthAuditor`, and `CarryAuditor`.
It computes only presentation facts such as top-of-book spread basis points; it does not create a
strategy score.

The store gains two narrow read methods:

- `latest_funding_collection_cycle_as_of(as_of)`;
- `evidence_counts_as_of(as_of)` using a fixed query for each approved table.

Existing latest-instrument, latest-funding, latest-book, and latest-book-cycle methods remain the
source of domain records.

### `polytrading.web.server`

The server owns routing, serialization, security headers, static-resource loading, and graceful
shutdown. A store is opened per API request so a long-lived read connection does not pin a stale
view. The handler never exposes exception details to the browser; expected database lock/unavailable
conditions return a stable `503` error document and unexpected failures return a stable `500` code.

### Packaged assets

`index.html`, `app.css`, and `app.js` live below `polytrading.web.assets` and are included as package
data. The page uses semantic landmarks, actual tables, visible focus rings, screen-reader labels,
and a mobile stacked layout. JavaScript has one state store: the last successful snapshot plus the
current refresh state.

## 7. HTTP Surface

The complete route set is:

- `GET /` → dashboard document;
- `GET /assets/app.css` → stylesheet;
- `GET /assets/app.js` → module script;
- `GET /api/v1/dashboard` → current snapshot;
- `GET /healthz` → `{"status":"ok"}` without database details.

Unknown routes return `404`. Query strings are rejected for the dashboard API instead of becoming
an undocumented control surface. Asset media types and UTF-8 encoding are explicit.

## 8. Error Handling

- A missing database, directory in place of a database, or non-current schema fails before binding
  a port and exits through the CLI's existing user-error path.
- An invalid port fails through the same path.
- A per-refresh database conflict produces `503 DATABASE_UNAVAILABLE`; the page keeps its last good
  data and labels it stale.
- A malformed internal snapshot is an internal error, never partially serialized.
- The browser times out a refresh after ten seconds and schedules the next normal poll; there is no
  aggressive retry loop.
- Missing evidence is normal data and renders as “Unavailable,” not as a server failure.

## 9. Testing and Acceptance

Implementation follows red-green-refactor and keeps repository coverage at or above 90%.

Automated tests prove:

- response-model invariants and canonical ordering;
- one shared `as_of` across store lookups and auditors;
- exact empty-database behavior without invented zeros;
- deterministic populated snapshots built from checked-in/replayed evidence;
- evidence counts cannot include observations learned after `as_of`;
- shell-safe recipe display for paths containing spaces and quotes;
- route allowlist, method rejection, host validation, media types, security headers, stable errors,
  and no database-path request parameter;
- CLI parsing, startup validation, and server-runner handoff;
- package installation includes all three assets;
- static source contains no `innerHTML`, remote URL, form, credential field, or mutation control;
- existing collection and research commands remain unchanged.

Browser verification uses a temporary populated DuckDB and checks desktop and narrow-mobile layouts,
keyboard focus, loading, healthy/degraded/missing states, manual refresh, and stale-error treatment.

Acceptance requires clean formatting and lint, the full test suite, at least 90% total coverage, a
loopback HTTP smoke, and a source scan confirming the web package has no trading or credential
authority.

## 10. Explicitly Deferred

- starting/stopping collectors or schedulers from the browser;
- editing configuration or database contents;
- authenticated remote access, TLS, multi-user roles, or cloud hosting;
- WebSockets or streaming books;
- price/funding charts and arbitrary query builders;
- alerts, email, Slack, or mobile notifications;
- positions, balances, P&L, orders, or any live-trading control;
- adding dYdX to the carry compatibility/audit pair;
- AI-generated trading recommendations.

Those are separate authority and product decisions. The local dashboard creates an honest operating
surface first, which makes later automation easier to review without pretending that visibility is
permission to trade.
