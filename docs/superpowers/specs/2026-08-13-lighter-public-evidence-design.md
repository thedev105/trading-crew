# Lighter Public Evidence Adapter Design

Date: 2026-08-13

Status: Approved under the operator's standing autonomous-review instruction

Companion architecture: [Market-Neutral Opportunity Router Design](2026-08-12-market-neutral-opportunity-router-design.md)

## 1. Objective

Add a strictly public, unauthenticated Lighter adapter for BTC, ETH, and SOL perpetual-market
evidence. The adapter will collect exact raw instrument metadata, settled hourly funding, and REST
order-book depth; normalize only fields that the public responses and official documentation can
support; expose evidence in the existing local dashboard; and preserve deterministic lineage.

This is an evidence milestone, not a trading strategy. It adds no keys, accounts, KYC handling,
balances, signing, orders, transfers, paper fills, execution simulation, or capital allocation.

## 2. Decision and Alternatives

Three approaches were compared:

| Approach | Immediate value | Main weakness | Decision |
|---|---|---|---|
| Bounded REST adapter first | Creates auditable instrument, settled-funding, and executable-depth evidence with the existing architecture | REST books have local receipt time and no sequence | Selected |
| REST adapter plus economic scorer | Produces an apparent carry estimate sooner | A first collection has no synchronized history, persistence estimate, or defensible slippage model | Defer |
| WebSocket reconstruction plus scorer | Offers stronger event timing and sequence continuity | Requires reconnect state, delta reconstruction, new operational health logic, and broader storage semantics | Defer |

The selected approach is deliberately smaller than the full Lighter/dYdX research path. A net
return model must follow sufficient collection, not precede it.

## 3. Evidence Observed Before Design

The official Lighter SDK and API reference document unauthenticated endpoints at
`https://mainnet.zklighter.elliot.ai`, including:

- `GET /api/v1/orderBooks` for market metadata;
- `GET /api/v1/orderBookOrders` for public order-book orders;
- `GET /api/v1/fundings` for historical funding; and
- `GET /api/v1/funding-rates` for a multi-exchange current-rate response.

Read-only mainnet probes on 2026-08-13 confirmed:

- `orderBooks?filter=perp` returns symbol, integer market ID, active/inactive status, fee fields,
  minimum base and quote amounts, and supported size and price decimal counts;
- `orderBookOrders?market_id=...&limit=...` returns individual orders on each side, including exact
  string price and remaining base amount, but no venue snapshot timestamp or sequence;
- `fundings` at `1h` resolution returns settled rows with Unix-second timestamp, absolute rate, and
  `direction`; `long` represents a positive signed rate and `short` a negative signed rate; and
- `funding-rates` includes Lighter and reference-exchange rows and represents current rates rather
  than the same settled historical evidence.

Official funding documentation states that funding occurs each hour, positive funding means longs
pay shorts, and negative funding means shorts pay longs. The implementation therefore uses the
settled `fundings` endpoint and never substitutes a current estimate for a realized payment.

## 4. Scope and Non-Goals

### 4.1 In scope

- Add `Venue.LIGHTER` with serialized value `lighter`.
- Implement `LighterPublicAdapter` through the existing `PublicVenueAdapter` protocol and injected
  `httpx.AsyncClient`, wall clock, and monotonic clock.
- Support only BTC, ETH, and SOL linear perpetual markets discovered from current metadata.
- Store exact successful response bytes in `RawEnvelope` and require same-venue SHA-256 lineage.
- Normalize active instrument metadata, settled hourly funding, and up to 20 aggregated book price
  levels per side.
- Resolve integer market IDs from current public metadata instead of hard-coding them.
- Add deterministic warnings for unavailable mark/index snapshots and locally timed REST books.
- Add `lighter` to generic public and book collection commands, including `--venue all`.
- Add Lighter BTC/ETH/SOL rows to the existing local dashboard and command recipes.
- Add fixture-driven unit, CLI, storage, and dashboard regression tests.

### 4.2 Out of scope

- The official SDK, signer binaries, private endpoints, API keys, wallet addresses, accounts,
  authentication, balances, positions, orders, withdrawals, transfers, or execution.
- WebSocket connection management, delta application, sequence continuity, queue position, or
  replay-complete book claims.
- The current `funding-rates` endpoint, projected funding, or reference-exchange rates embedded in
  that response.
- Inventing mark or index values from midpoint, last trade, or unrelated fields.
- A profitability score, annualized return, backtest, forward test, or trading recommendation.
- Changing the legacy Bybit/Hyperliquid funding cycle, health report, carry audit, or persistence
  study.
- Declaring Lighter/dYdX contract compatibility, venue independence, account eligibility, or legal,
  tax, KYC, sanctions, residency, or terms-of-use approval.

## 5. Identity and Market Resolution

`Venue` gains `LIGHTER = "lighter"`. Existing serialized venue values do not change and DuckDB's
string venue columns require no schema migration.

Requested assets map to exact Lighter symbols:

| Asset | Symbol | Instrument ID |
|---|---|---|
| BTC | `BTC` | `lighter:BTC` |
| ETH | `ETH` | `lighter:ETH` |
| SOL | `SOL` | `lighter:SOL` |

Every method that needs a market ID first fetches `/api/v1/orderBooks?filter=perp`, validates the
response, and resolves the requested symbols. IDs are not assumed to remain constant. Missing,
duplicate, inactive, non-perpetual, or contradictory requested markets fail the whole method call.
Unrequested markets are ignored.

## 6. HTTP and Raw-Evidence Boundary

The adapter uses only:

- `/api/v1/orderBooks` with `filter=perp`;
- `/api/v1/fundings` with explicit market ID, `1h` resolution, range, and count; and
- `/api/v1/orderBookOrders` with explicit market ID and a bounded order limit.

Each successful response records exact UTF-8 bytes, the endpoint path without query values, source
version `mainnet-v1-public`, local wall-clock receipt time taken after the complete payload arrives,
monotonic start/completion times, and SHA-256. `venue_timestamp` remains null unless a response
provides a documented response-level event time; the selected endpoints do not.

HTTP errors propagate through `httpx`. Invalid UTF-8, invalid or duplicate-key JSON, non-200 API
codes inside a successful HTTP response, missing fields, wrong types, invalid decimals, malformed
timestamps, and contradictory duplicates fail closed. Exception text never includes response bodies.

## 7. Instrument Normalization

One accepted active `perp` row becomes one `InstrumentSpec`:

- `kind = linear_perpetual`;
- `contract_multiplier` from `multiplier` when present and otherwise `1`, requiring a positive exact
  decimal;
- `collateral_asset = USDC` and `pnl_asset = USDC` from the official perpetual contract semantics;
- `funding_interval_hours = 1`;
- `quantity_step = 10 ** -supported_size_decimals`;
- `price_tick = 10 ** -supported_price_decimals`;
- `min_notional` from the positive exact `min_quote_amount`;
- `is_inverse = false` and `is_prelaunch = false`.

The response does not establish a stable normalized index family, oracle family, mark method,
liquidation method, funding formula identifier, cap, or payment offset for this point-in-time record.
Those fields remain null. Fee fields remain raw evidence because account tier and enabled flags make
a single universal `FeeSchedule` claim unsafe.

## 8. Settled Funding History

`fetch_funding_history` normalizes aware inputs to UTC, rejects `end < start`, and limits one call to
at most seven days. The generic CLI already has the same seven-day cap. A bounded range requires at
most 169 possible hourly timestamps, so the adapter sends one request with:

- current resolved market ID;
- `resolution=1h`;
- Unix-second `start_timestamp` and `end_timestamp`; and
- `count_back` equal to the possible hourly observations, capped at 169.

The response must report `resolution=1h`. Each row must contain an integer Unix-second timestamp, an
exact decimal string rate, and direction `long` or `short`. The normalized signed rate is positive
for `long`, negative for `short`, and exactly zero for a zero rate regardless of direction. Rows are
filtered to the closed interval. Identical duplicate timestamps are accepted once; different signed
rates at one timestamp fail. Rows after response receipt fail. Output is chronologically sorted and
each record links to the funding response, while the metadata response remains retained as raw
market-ID evidence.

The `value` field stays in raw evidence. The current-rate endpoint is never queried.

## 9. REST Order-Book Evidence

`fetch_order_books` resolves requested markets once, then requests
`/api/v1/orderBookOrders?market_id=...&limit=100` in stable asset order. The endpoint returns orders,
not aggregated levels. For each side the adapter:

1. parses positive exact `price` and `remaining_base_amount` values;
2. rejects duplicate order IDs within the response and invalid or zero remaining amounts;
3. groups orders by price, sums remaining quantity, and counts contributing orders;
4. sorts bids descending and asks ascending; and
5. retains at most the best 20 price levels per side.

Both sides must remain non-empty and the best bid must be below the best ask. Every normalized book
uses the caller's cycle ID, `depth_limit=20`, `sequence=null`, and local post-response receipt time
for both `effective_at` and `observed_at`. It emits `LIGHTER_REST_BOOK_LOCAL_TIMESTAMP`, explaining
that the response supplies no venue snapshot time or sequence. Metadata and per-market book
responses all remain in the raw batch; each book links to its own depth response.

## 10. Market-Snapshot Boundary

The normalized `MarketSnapshot` requires bid, ask, mark, and index. The selected REST evidence does
not provide a documented, response-timestamped set of all four fields. `fetch_market_snapshots`
therefore captures and validates current market metadata, returns no normalized snapshots, and emits
one `LIGHTER_MARK_INDEX_UNAVAILABLE` warning per requested symbol. It does not substitute last trade,
midpoint, current funding input, or a locally calculated value.

## 11. CLI, Dashboard, and Legacy Boundaries

Generic commands accept `lighter`:

```text
polytrading collect public --venue lighter ...
polytrading collect books --venue lighter ...
```

For generic collection only, `--venue all` expands deterministically to Bybit, Hyperliquid, dYdX,
and Lighter. The adapter-session factory has an explicit Lighter branch. Existing deterministic
warning rendering is reused.

The local dashboard expands its canonical market grid from nine to twelve rows and maps Lighter
symbols to BTC, ETH, and SOL. It shows latest settled funding and locally timed book evidence using
the existing fields and research warning. It does not add profit, signal, or execution controls.

The prospective funding-cycle command, funding health report, carry audit, and carry persistence
study remain fixed to their existing Bybit/Hyperliquid semantics. Adding an enum value must not
silently change those research definitions.

## 12. Failure and Transaction Semantics

An adapter method fails without partial recording when requested market resolution is incomplete or
contradictory; a response code is not 200; required structures are absent; decimals or timestamps are
invalid; duplicate funding timestamps conflict; duplicate order IDs occur; a book is empty, locked,
or crossed; or raw lineage validation fails.

The existing recorder validates and persists a complete `AdapterBatch` transactionally. A failed
method may have completed network requests, but its caller receives no recordable partial batch.
Different assets are never silently dropped.

## 13. Test Strategy

Tests use `httpx.MockTransport`, deterministic clocks, fixed UUIDs, and checked-in exact-byte
fixtures. They cover:

- enum serialization and no DuckDB venue migration;
- market-ID resolution and instrument normalization for BTC, ETH, and SOL;
- missing, inactive, duplicate, wrong-type, and malformed metadata;
- exact response hashes, receipt timing, API-code validation, duplicate JSON keys, and sanitized
  failures;
- signed settled funding, closed-range filtering, duplicate handling, direction validation,
  seven-day bounding, timestamp checks, and proof that current-rate endpoints are unused;
- order aggregation, counts, sorting, top-20 truncation, stable request order, duplicate order IDs,
  invalid quantities, empty/crossed books, and local-time warnings;
- CLI parser choices, all-venue expansion, explicit factory routing, and warning output;
- preservation of the legacy two-venue funding and carry paths;
- twelve canonical dashboard rows and Lighter point-in-time evidence selection; and
- absence of credential, private-endpoint, signer, account, transfer, and order paths.

The full suite, Ruff, package build, and a browser smoke review of the dashboard must pass before
integration. Live public calls are diagnostic only and never replace fixtures.

## 14. Completion and Next Gate

This milestone is complete when the adapter, CLI, storage path, and dashboard integration pass all
tests and a final diff review finds no hidden execution scope or fabricated evidence.

The next milestone is an economic-research specification using accumulated Lighter/dYdX evidence.
It must define point-in-time joins, signed funding alignment, fees by actual account tier, marketable
depth, transfer/rebalance costs, latency stress, missing-data rules, persistence windows, and a cash
reserve. It may produce research candidates only after minimum sample-size and coverage gates pass.
No positive return is assumed, and no automated execution follows from a research score.

## 15. Official Sources

- Lighter public Python SDK and generated API contract: <https://github.com/elliottech/lighter-python>
- Lighter API get-started guide: <https://apidocs.lighter.xyz/docs/get-started>
- Lighter funding endpoint reference: <https://apidocs.lighter.xyz/reference/funding-rates>
- Lighter funding mechanics: <https://docs.lighter.xyz/trading/funding>
- Lighter contract specifications: <https://docs.lighter.xyz/trading/contract-specifications>
- Lighter trading fees: <https://docs.lighter.xyz/trading/trading-fees>
- Lighter WebSocket reference: <https://apidocs.lighter.xyz/docs/websocket-reference>
