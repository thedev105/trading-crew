# dYdX Public Evidence Adapter Design

Date: 2026-08-13

Status: Approved under the operator's standing autonomous-review instruction

Companion architecture: [Market-Neutral Opportunity Router Design](2026-08-12-market-neutral-opportunity-router-design.md)

## 1. Objective

Add a read-only dYdX public-data adapter for BTC, ETH, and SOL perpetual markets. The adapter will collect exact raw market metadata, realized hourly funding, and order-book depth with deterministic normalization and lineage.

This milestone creates a plausible same-collateral research path with Hyperliquid: the currently implemented Bybit contracts settle in USDT, while dYdX and Hyperliquid expose USDC-margined contracts. It does **not** prove that the two contracts are economically compatible, that the venues are independent failure domains, that a spread is profitable, or that the user may trade either venue.

The result remains research-only. No credentials, accounts, balances, orders, signing, transfers, or live-trading controls are added.

## 2. Why This Milestone Comes Next

The existing carry path has a structural evidence gap. Its Bybit and Hyperliquid instruments do not have matching collateral and P&L currencies, and the master design excludes unmatched collateral from the initial Class C universe. Implementing a net-carry scorer over that pair now would mostly automate a known rejection.

Three next steps were compared:

| Candidate | Value now | Main limitation | Decision |
|---|---|---|---|
| Add dYdX public evidence | Creates a USDC/USDC venue pair and adds a distinct public funding history | Contract semantics, venue eligibility, costs, and operational independence still need proof | Selected |
| Add OKX public evidence | Creates a USDT/USDT pair with Bybit | Funding intervals can vary and the path remains dependent on two centralized custodians | Defer |
| Build the net-carry evaluator | Advances economic scoring infrastructure | Existing supported pair already fails the collateral-compatibility rule | Defer until a plausible pair has evidence |

Class G corpus work is not selected for this autonomous milestone because its activation protocol intentionally requires a genuine human-reviewed contract corpus. The required review labels must not be fabricated to make progress appear complete.

## 3. Evidence Observed Before Design

On 2026-08-13, unauthenticated mainnet response-shape probes confirmed:

- `GET /v4/perpetualMarkets?ticker=BTC-USD` returns an `ACTIVE` market with `oraclePrice`, `nextFundingRate`, `openInterest`, `tickSize`, `stepSize`, margin fractions, and market identifiers.
- `GET /v4/historicalFunding/BTC-USD?limit=2` returns timestamped realized `rate`, `price`, `effectiveAtHeight`, and `effectiveAt` rows.
- `GET /v4/orderbooks/perpetualMarket/BTC-USD` returns price/size bid and ask arrays but no exchange timestamp or sequence identifier.
- The market response does not expose a field documented as the venue's mark price.

The implementation must be driven by recorded fixtures and official public API contracts, not by assuming the response will always retain this shape.

## 4. Scope and Non-Goals

### 4.1 In scope

- Add `Venue.DYDX`.
- Implement `DydxPublicAdapter` using the existing `httpx` transport and `PublicVenueAdapter` protocol.
- Support only `BTC-USD`, `ETH-USD`, and `SOL-USD`.
- Capture exact UTF-8 response bytes in `RawEnvelope` records and link every normalized record to a same-venue raw hash.
- Normalize active linear-perpetual instrument metadata.
- Retrieve realized hourly funding over a bounded closed interval with finite backward pagination.
- Normalize the best 20 valid levels on each side of a REST order book.
- Surface missing mark-price and local-timestamp limitations as structured adapter warnings.
- Add `dydx` to the generic `collect public` and `collect books` CLI venue choices.
- Print all adapter warnings deterministically to standard error when CLI collection records a batch.
- Add deterministic unit and CLI tests using local HTTP fixtures.

### 4.2 Out of scope

- SDK installation, wallet creation, keys, credentials, authentication, signing, accounts, balances, orders, transfers, or deposits.
- WebSocket sequencing, incremental book reconstruction, or claims that REST snapshots are replay-complete.
- Computing or inventing a dYdX mark price.
- Extending the legacy Bybit/Hyperliquid funding-cycle completeness report, carry audit, or carry-persistence study to dYdX.
- Declaring dYdX and Hyperliquid contract-compatible.
- Fee, slippage, liquidation, oracle, auto-deleveraging, governance, or failure-domain approval.
- Legal, tax, KYC, sanctions, residency, terms-of-use, or venue-availability approval.
- Historical backtest, forward paper-trading activation, or live capital.

## 5. Domain and Venue Identity

`Venue` gains the stable serialized value `dydx`. No existing serialized values change.

The supported symbol mapping is explicit:

| Asset | dYdX ticker | Instrument ID |
|---|---|---|
| BTC | `BTC-USD` | `dydx:BTC-USD` |
| ETH | `ETH-USD` | `dydx:ETH-USD` |
| SOL | `SOL-USD` | `dydx:SOL-USD` |

Ticker strings are never inferred by accepting arbitrary assets. Requested assets missing from the current active response fail the batch; the adapter must not silently return a partial requested universe.

## 6. HTTP Boundary and Raw Evidence

The adapter uses the public indexer base URL `https://indexer.dydx.trade` and these endpoints:

- `/v4/perpetualMarkets`
- `/v4/historicalFunding/{ticker}`
- `/v4/orderbooks/perpetualMarket/{ticker}`

Each request records:

- exact response bytes decoded strictly as UTF-8;
- endpoint path without query values in `RawEnvelope.endpoint`;
- source version `indexer-v4-public`;
- local wall-clock receipt timestamp, recorded after the complete payload is received;
- monotonic start and completion times;
- SHA-256 of the exact response bytes.

HTTP status errors propagate through `httpx`. Invalid UTF-8, invalid JSON, unexpected top-level types, missing required keys, invalid decimals, symbol mismatches, and contradictory duplicates fail closed with sanitized errors. Response bodies are never copied into exception messages.

The adapter does not send secrets or use private endpoints. The shared client retains bounded timeouts and the existing finite retry policy for 429 and selected 5xx statuses.

## 7. Instrument Normalization

`fetch_instruments` requests the market metadata once, selects exactly the supported requested tickers, and accepts only rows whose status is `ACTIVE`.

Each accepted row becomes an `InstrumentSpec` with:

- `kind = linear_perpetual`;
- `contract_multiplier = 1`;
- `collateral_asset = USDC` and `pnl_asset = USDC`, based on the protocol's USDC-margin contract documentation rather than ticker text;
- `funding_interval_hours = 1`;
- `quantity_step` from `stepSize`;
- `price_tick` from `tickSize`;
- `is_inverse = false` and `is_prelaunch = false`;
- `min_notional = null` unless the exact response supplies a documented minimum-notional field.

The current metadata response is insufficient to establish the master architecture's exact index family, oracle family, mark method, liquidation method, funding formula identifier, symmetric funding cap, or payment offset. Those fields remain `null`; documentation prose must not be converted into a false point-in-time response claim.

The adapter validates the metadata decimals it relies on, including positive tick and step sizes. It rejects duplicate requested tickers, invalid statuses for requested assets, and incomplete requested sets.

## 8. Realized Funding History

`fetch_funding_history` accepts an aware `start`, aware `end`, and collection-context timestamp. It normalizes the range to UTC and rejects `end < start`.

The dYdX endpoint paginates newest-to-oldest with `effectiveBeforeOrAt`. The adapter therefore:

1. Requests the target ticker with a conservative fixed page limit of 100 and an upper-bound cursor initially equal to `end`.
2. Parses every row's exact decimal `rate`, ticker, and aware ISO-8601 `effectiveAt`.
3. Retains only observations inside the requested closed interval `[start, end]`.
4. Stores each response as its own raw envelope and links each first-seen timestamp to the raw page that supplied it.
5. Accepts an identical duplicate timestamp/rate and rejects a conflicting duplicate.
6. Stops when the page is empty or its oldest timestamp is at or before `start`.
7. Otherwise moves the upper-bound cursor to one microsecond before the oldest returned timestamp.

The request budget allows one request per possible hourly observation plus one terminal request, capped by a configurable positive hard maximum. It therefore remains finite without assuming that the server always fills each requested page. Repeated cursors, a non-empty page with no temporal progress, rows newer than the requested cursor, or exhaustion of the budget raises `PaginationStalledError`.

Every normalized `FundingObservation` uses:

- ticker symbol such as `BTC-USD`;
- exact realized `rate`;
- `interval_hours = 1`;
- `effective_at` from the response;
- actual response receipt time as `observed_at`.

The `price` and block-height fields remain in raw evidence. They are not forced into unrelated domain fields.

## 9. Order-Book Evidence

`fetch_order_books` performs one REST request per requested asset in stable asset order. It validates the response as two non-empty sides of price/size objects, converts values with `Decimal`, requires positive prices and sizes, rejects duplicate prices, sorts bids descending and asks ascending, takes at most the best 20 levels per side, and rejects a locked or crossed top of book.

The public REST response has no venue timestamp or sequence. Consequently:

- `effective_at` equals the local post-response receipt timestamp;
- `observed_at` equals the same receipt timestamp;
- `sequence = null`;
- `RawEnvelope.venue_timestamp = null`;
- each returned book has a `DYDX_REST_BOOK_LOCAL_TIMESTAMP` warning.

This warning means the book is suitable for coarse, synchronized executable-depth studies using the collector's receipt-skew gate, but not for claims about exact exchange-time simultaneity, dropped deltas, or queue position.

## 10. Market-Snapshot Boundary

The current domain requires bid, ask, mark, and index in every `MarketSnapshot`. dYdX's public market response supplies an oracle price and the REST book supplies bid and ask, but the observed response does not supply a documented mark-price field.

`fetch_market_snapshots` will therefore:

- capture one exact market-metadata response;
- validate that every requested active ticker is present;
- emit no `MarketSnapshot` records;
- emit one `DYDX_MARK_PRICE_UNAVAILABLE` warning per requested asset.

It must not substitute the oracle, midpoint, last trade, or funding impact price into `mark`. A later domain change may introduce a mark-optional evidence type or a formally verified dYdX mark calculation. Until then, absence is more accurate than a fabricated normalized record.

## 11. CLI Integration

The generic collection commands accept `dydx`:

```text
polytrading collect public --venue dydx ...
polytrading collect books --venue dydx ...
```

`--venue all` expands deterministically to Bybit, Hyperliquid, and dYdX for these generic commands. The adapter-session factory uses explicit branches for all venue enum members and raises for an unsupported member; it must not treat every non-Bybit venue as Hyperliquid.

The point-in-time funding-cycle command and its health report remain exactly the legacy Bybit/Hyperliquid pair. Carry audit and persistence study remain unchanged. This prevents a new venue enum value from silently altering established evidence semantics.

Before recording any batch, CLI collection prints each structured `AdapterWarning` to standard error in stable order with venue, code, symbol, endpoint, and message. Warnings do not turn valid raw evidence into a failed transaction, but they remain visible to an operator and testable.

## 12. Failure Semantics

The adapter fails the current method call without partial recording when:

- any requested asset is missing, duplicated, inactive, or mapped to a contradictory ticker;
- a required value is missing, has the wrong type, is non-finite, or violates positivity;
- timestamps are naive, invalid, after local response receipt where the source purports to be historical, or make pagination move backward incorrectly;
- funding duplicates conflict;
- a book is empty, malformed, contains duplicate prices, or remains locked/crossed after normalization;
- pagination stalls or exceeds its finite budget;
- raw lineage validation fails.

Different assets are not silently dropped to make a multi-asset batch succeed. The caller can retry a narrower explicit request if desired.

## 13. Test Strategy

Tests use `httpx.MockTransport`, deterministic wall and monotonic clocks, and exact byte fixtures. They cover:

- venue serialization and CLI choices;
- active instrument normalization for all three supported assets;
- missing, inactive, duplicate, malformed, and partial instrument responses;
- exact raw-byte hashes and receipt timing;
- one-page and multi-page backward funding history;
- closed-interval filtering, identical duplicates, conflicting duplicates, stalled cursors, request-budget exhaustion, and malformed timestamps;
- stable book request order, Decimal parsing, sorting, truncation to 20, duplicate levels, empty sides, crossed books, and local-timestamp warnings;
- mark-unavailable behavior with raw evidence and explicit warnings;
- explicit adapter-factory routing and preservation of the legacy funding-cycle venue pair;
- deterministic CLI warning rendering;
- proof that the module contains no private URL, credential, wallet, signing, or order path.

The full repository test and static-quality commands must pass before integration. A live public smoke call is optional diagnostic evidence and never a substitute for fixtures.

## 14. Completion and Next Evidence Gate

This milestone is complete when the adapter and CLI integration pass their focused tests, the full repository suite passes, and an autonomous diff review finds no hidden live-trading scope, silent evidence fabrication, unbounded loops, or accidental changes to legacy two-venue studies.

The next rational milestone is a **contract-compatibility dossier and evaluator** for Hyperliquid/dYdX. Before either venue can enter a profit model, that work must establish from point-in-time sources:

- collateral and P&L semantics;
- linear/quanto exposure and multiplier equivalence;
- oracle, mark, liquidation, and auto-deleveraging rules;
- funding formula, caps, sampling, and timestamps;
- fee schedules and realistic executable slippage;
- venue eligibility and operational/failure-domain assumptions.

Only after compatibility passes should the system collect sufficient history and evaluate conservative net carry. Public data support is an evidence capability, not an investment recommendation.

## 15. Official Sources

- dYdX integration and Indexer API documentation: <https://docs.dydx.xyz/>
- dYdX Indexer HTTP API reference: <https://docs.dydx.xyz/indexer-client/http>
- dYdX default funding-rate documentation: <https://help.dydx.trade/en/articles/166992-default-funding-rates-on-dydx>
- dYdX liquidity-tier and mark-price context: <https://help.dydx.trade/en/articles/166993-default-liquidity-tiers-on-dydx-chain>
- Hyperliquid contract specifications: <https://hyperliquid.gitbook.io/hyperliquid-docs/trading/contract-specifications>
