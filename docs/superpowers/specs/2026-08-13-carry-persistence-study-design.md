# Carry Persistence Study v1

Date: 2026-08-13

Status: Self-reviewed and approved for planning under the user's standing instruction to proceed
and review autonomously

Companion to:

- [Market-Neutral Opportunity Router Design](2026-08-12-market-neutral-opportunity-router-design.md)
- [AI Augmentation Design](2026-08-12-ai-augmentation-design.md)

## 1. Decision

The next research mechanism is a deterministic, rights-gated **cross-venue funding persistence
study** for BTC, ETH, and SOL. It tests whether the funding paid by Hyperliquid perpetual longs
has remained persistently greater than the funding paid by equivalent Bybit perpetual longs.
The economic position being investigated is therefore:

- long the lower-funding Bybit perpetual;
- short the higher-funding Hyperliquid perpetual;
- equal asset notional on both legs;
- collateral pre-funded on both venues; and
- no transfer, bridge, or withdrawal assumed during a position.

This is Class C market-neutral carry, not arbitrage and not a profit promise. Funding can reverse,
basis can diverge, one leg can fail, and venue or collateral losses can dominate accumulated carry.

The first implementation produces research evidence only. It does not place orders, create API
keys, authenticate, recommend a trade, or mark any strategy live-eligible.

## 2. Why this path was selected

Three paths were compared.

| Path | Learning value now | Main constraint | Decision |
|---|---:|---|---|
| Licensed prediction-market corpus | Medium | Reuse rights require external confirmation | Pause |
| Self-authored synthetic semantic corpus | Low for economics; high for software mechanics | Cannot establish real alpha | Maintain as test infrastructure |
| Direct quantitative carry evidence | High | Venue-data use and point-in-time quality must be gated | Select |

The selected path fits the existing recorder, immutable DuckDB store, funding normalizer,
experiment registry, and read-only boundary. It also fits a sub-USD 10,000 account better than
latency-sensitive market making: the research horizon is days to weeks and capacity is not the
binding constraint.

The strategy is intentionally not made more complicated at this stage. A June 2026 working paper
reports a persistent Hyperliquid-versus-centralized-venue funding spread and reports that several
more elaborate refinements did not improve on a naive baseline. That result is suggestive, not
independent validation for this project. The correct response is a preregistered replication and a
new forward test, not more feature mining.

## 3. Source and rights boundary

Technical accessibility and permission are separate facts.

- Hyperliquid documents a public API, historical funding retrieval, hourly funding, L2 books, and
  request limits.
- Bybit documents historical funding and snapshot order-book endpoints.
- Bybit's API agreement dated 2026-03-18 grants a limited license to develop, test, and support the
  user's use, while also prohibiting resale, repackaging, and commercial exploitation of the API.
- No official Hyperliquid market-data license granting unrestricted proprietary reuse was located
  during this review.

Consequently:

1. bundled fixtures and author-generated data may be used for implementation and tests;
2. a user-provided database may be analyzed locally without copying records into a distributable
   artifact;
3. no collected raw or normalized venue data enters Git, model training, a public dataset, or a
   commercial data product;
4. reports contain aggregates and source hashes, never raw venue payloads;
5. expanded collection and proprietary deployment remain subject to a separately recorded venue
   use decision; and
6. live eligibility remains subject to jurisdiction, KYC, tax, sanctions, venue access, and terms
   review.

An ambiguous license is not treated as approval. The study may say that evidence is unavailable;
it may not invent a permission record.

## 4. Hypothesis and trial family

### 4.1 Frozen hypothesis

For each of BTC, ETH, and SOL:

> Over complete common eight-hour settlement blocks, the realized funding received by a short
> Hyperliquid perpetual and paid by a long Bybit perpetual has a positive median and positive
> lower-tail holding-period sum before trading costs.

The signed block spread is:

```text
block_spread = sum(Hyperliquid periodic funding rates in block)
             - sum(Bybit periodic funding rates in block)
```

A positive value benefits the fixed long-Bybit/short-Hyperliquid direction. The implementation
does not select whichever historical direction looks best.

### 4.2 Single registered trial family

Version 1 uses one trial family, `hl-bybit-funding-persistence-v1`, with:

- assets: BTC, ETH, SOL;
- common block: eight hours aligned to UTC epoch boundaries;
- holding windows: 7, 14, and 28 days;
- empirical lower-tail statistic: fifth percentile of overlapping holding-window sums;
- no predictive features;
- no parameter search;
- no incentives, rebates, leverage, token appreciation, or lending yield; and
- a fixed direction of long Bybit and short Hyperliquid.

The three assets and three holding horizons are nine related views in one trial family, not nine
independent discoveries. Every later variation receives a new trial-family identifier.

## 5. Data semantics

### 5.1 Realized payment blocks

Each `FundingObservation.rate` is the dimensionless payment rate for its native interval. Records
are assigned to half-open blocks `(block_start, block_end]` by `effective_at`. Rates are summed,
not averaged, within a block.

A venue contributes a complete block only when the sum of its distinct native interval lengths in
that block equals exactly eight hours. A paired block is usable only when both venues are complete.
This handles the expected Hyperliquid hourly versus Bybit instrument-specific funding clocks
without pretending the formulas, caps, or payment boundaries are identical.

Duplicate observations with the same immutable identity are not double counted. Conflicting
revisions remain a data-quality failure rather than being resolved in favor of the convenient row.

### 5.2 Availability classification

Historical API retrieval can describe past settled funding but does not by itself prove what this
system knew at each historical decision time. Each series is classified as:

- `point_in_time`: every included value was observed within five minutes after its effective time;
- `historical_reconstruction`: at least one included value was observed later; or
- `insufficient_data`: the requested range or paired coverage is inadequate.

Historical reconstruction may support replication and distribution estimation. It may not count
toward the 90-day forward activation clock or support a claim of executable historical P&L.
An observation timestamp before the corresponding settlement's effective time is invalid for this
realized-payment study.

### 5.3 Coverage

The report includes requested blocks, complete blocks per venue, paired complete blocks, coverage
ratio, first and last paired timestamps, and every incomplete block reason. Economic statistics
are withheld when:

- a `historical_reconstruction` contains fewer than 365 requested calendar days;
- a `point_in_time` forward study contains fewer than 90 requested calendar days;
- paired block coverage is below 99%;
- fewer than two holding windows exist for a requested horizon; or
- asset, venue, symbol, or interval fields are inconsistent.

Missing periods are never forward-filled and never treated as zero funding.

## 6. Statistics

For every asset with sufficient data, the report calculates using deterministic decimal
arithmetic:

- mean and median signed eight-hour spread;
- fifth and ninety-fifth empirical percentiles;
- fraction of positive, zero, and negative blocks;
- sign persistence and sign-reversal count;
- longest consecutive adverse run;
- cumulative gross funding per unit of matched leg notional;
- maximum drawdown of that cumulative gross funding series;
- calendar-month contribution totals and cumulative funding after removing the best month;
- gross annualization of the mean spread; and
- for 7-, 14-, and 28-day overlapping holding windows: count, mean, median, fifth percentile,
  minimum, and positive fraction.

Percentiles use a documented nearest-rank method. No interpolation or floating-point conversion is
allowed in the binding report.

The report labels all values `gross_funding_only`. It does not subtract fees, slippage, basis P&L,
collateral effects, financing, taxes, or failure reserves, because the historical database does
not yet contain synchronized point-in-time evidence for all of them.

## 7. Decision states

The study emits one of four states:

- `INSUFFICIENT_DATA`: coverage or holding-window requirements fail;
- `REPLICATION_FAILED`: sufficient data exists but the fixed-direction median or any required
  holding-horizon fifth percentile is non-positive, or removing the best month makes cumulative
  gross funding non-positive;
- `FORWARD_TEST_REQUIRED`: the gross historical replication passes, but executable forward
  evidence is not complete; or
- `NET_FORWARD_GATE_REQUIRED`: the 90-day point-in-time evidence is complete, but the separate
  fee, depth, basis, reserve, stress, and ledger gate has not yet been evaluated.

Version 1 can never emit `TRADE`, `APPROVED`, or `LIVE_ELIGIBLE`.

## 8. Forward experiment

After the implementation and parameters are frozen, a separate forward collector runs for at
least 90 continuous days. It records:

- funding observations as published;
- synchronized executable books at a fixed cadence;
- mark, index, and basis observations;
- point-in-time instrument specifications and funding rules;
- effective fee schedules for the actual account tier;
- API errors, gaps, latency, and venue-health events; and
- counterfactual fills and double-entry paper ledger entries.

The forward strategy may enter only after a separate net-cost design defines its entry and exit
state machine. At minimum, a candidate's lower-tail expected funding over the planned hold must
pay:

```text
two venues * (entry taker cost + exit taker cost)
+ measured depth slippage
+ basis-divergence reserve
+ funding-reversal reserve
+ incomplete-leg and forced-exit reserve
```

For a fully collateralized pair, gross position notional and return-on-capital denominators must be
reported separately. No annualized funding rate may be presented as account return.

The master Class C gate remains binding: 90 forward days, exact ledger reconciliation, positive
results with doubled costs, no modeled stress liquidation, maximum simulated drawdown below 8%,
and diversification across at least two assets. The initial live pilot, if ever approved, remains
capped at the lesser of USD 500 and 6.25% of equity.

## 9. AI role

AI is optional and outside the numerical path. It may:

- summarize incomplete-block and anomaly clusters;
- compare a report with the preregistration;
- draft a human-readable experiment review;
- flag documentation or terms changes for deterministic recapture; and
- propose a new, separately registered hypothesis.

AI may not alter observations, fill gaps, select a profitable direction, choose a percentile,
calculate binding P&L, lower a reserve, or authorize collection, execution, or deployment. AI
output is never input to the version-1 statistics.

## 10. Interfaces and artifacts

The implementation adds a pure study service plus a read-only CLI:

```text
polytrading carry study \
  --db PATH \
  --asset BTC \
  --start 2025-08-13T00:00:00Z \
  --end 2026-08-13T00:00:00Z \
  --known-as-of 2026-08-13T00:05:00Z \
  --format json
```

The command:

1. reads funding observations whose settlement is inside `(start, end]` and whose local
   observation time is no later than `known-as-of`;
2. validates the fixed study protocol;
3. computes a stable, versioned report;
4. writes nothing to the database;
5. performs no network requests; and
6. prints JSON or concise text.

The JSON contains the exact protocol version, requested window, asset, source hashes, availability
class, coverage, statistics, decision state, and explicit omitted-cost list. Output ordering is
stable so that the report can be hashed and independently reproduced.

## 11. Error handling and tests

The implementation fails closed on:

- non-UTC or non-increasing windows;
- start or end timestamps that are not aligned to an eight-hour UTC epoch boundary;
- a `known-as-of` timestamp earlier than the requested end;
- unsupported assets or venues;
- mixed asset/symbol records;
- non-positive interval lengths;
- observations outside the requested window;
- duplicate settlement identities with conflicting content;
- overfilled native intervals within a common block; and
- arithmetic or schema errors.

Tests cover:

- exact eight-hour alignment and boundary inclusion;
- one Bybit eight-hour payment versus eight Hyperliquid hourly payments;
- missing, duplicated, changing, and overfilled intervals;
- historical-reconstruction classification;
- no lookahead through the requested end time;
- nearest-rank percentile boundaries;
- adverse-run and drawdown calculations;
- all four research decision states that are reachable from study inputs;
- stable JSON and text rendering;
- CLI validation and read-only behavior; and
- property tests for input-order invariance and cumulative-spread conservation.

## 12. Stop conditions

Do not proceed to the net forward engine if any of the following occurs:

- the replication fails for two of three assets;
- results rely on one exceptional month;
- fifth-percentile 7-, 14-, or 28-day gross funding is non-positive;
- data rights cannot be documented for the intended private proprietary use;
- collection gaps prevent a 90-day continuous forward record;
- realistic four-leg round-trip costs consume the conservative funding estimate; or
- venue, collateral, liquidation, or jurisdiction review makes either leg ineligible.

An honest negative result is a successful research outcome: it prevents a small account from
paying fees to discover that a headline annualized spread was not executable.

## 13. Primary sources

- BIS, *Crypto carry* (revised October 2025):
  https://www.bis.org/publ/work1087.htm
- Hyperliquid funding mechanics:
  https://hyperliquid.gitbook.io/hyperliquid-docs/trading/funding
- Hyperliquid historical funding API:
  https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals
- Hyperliquid API rate limits:
  https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits
- Bybit historical funding API:
  https://bybit-exchange.github.io/docs/v5/market/history-fund-rate
- Bybit order-book API:
  https://bybit-exchange.github.io/docs/v5/market/orderbook
- Bybit API Terms & Conditions dated 2026-03-18:
  https://www.bybit.com/common-static/compliance/legal/BYBIT/df1923006718fbba8ba70d7d762b9866.pdf
- Tony Lau, *The Funding Carry and a Cross-Venue Spread on Perpetual Futures* (working paper,
  June 2026; treated as a replication target, not authoritative evidence):
  https://papers.ssrn.com/sol3/papers.cfm?abstract_id=6993978
