# Lighter–dYdX Conservative Shadow Economics Design

**Status:** Approved design  
**Date:** 2026-08-13  
**Scope:** Deterministic, point-in-time research for BTC, ETH, and SOL  
**Authority boundary:** Read-only evidence evaluation; no accounts, balances, positions, fills,
orders, transfers, paper execution, or live execution

## 1. Objective

Build a conservative economic gate for the already admitted Lighter–dYdX research candidate. The
gate answers one narrow question:

> Did a fixed, delta-matched Lighter–dYdX direction have positive conservative economics in
> prospectively recorded public evidence after explicit costs and stress reserves?

It does not predict profit, recommend a trade, infer a fill, or authorize paper or live execution.
The only positive outcome is `SHADOW_CANDIDATE`, meaning the hypothesis may advance to a separately
specified forward paper-execution gate.

The design preserves the existing Bybit–Hyperliquid funding study and carry audit unchanged. The
new evaluator is a separate layer because gross funding persistence and executable economics have
different evidence, failure, and decision semantics.

## 2. Fixed Research Scope

- Venues: Lighter and dYdX only.
- Assets: BTC, ETH, and SOL only.
- Instruments: active linear perpetuals already normalized by the public adapters.
- Holding horizons: 7, 14, and 28 calendar days.
- Account equity: an explicit decimal amount from USD 3,000 through USD 10,000 inclusive.
- Position: one long leg and one short leg with equal base quantity.
- Execution assumption: taker entry and taker exit only.
- Direction: selected once from a training window and frozen throughout the evaluation window.
- Return basis: total assigned capital and total account equity; never margin-only capital.
- Rewards: staking yield, points, rebates, token appreciation, affiliate revenue, and uncertain maker
  rebates are excluded.
- Basis convergence credit: zero. Historical basis changes may create a reserve but never an
  expected-profit credit.
- Cash: always a valid outcome. Thresholds may not be relaxed to force a candidate.

This milestone adds no private API, key, signer, wallet, custody, deposit, withdrawal, balance,
position, transfer, order, fill, cancellation, or execution dependency.

## 3. Why the Model Must Remain Point-in-Time

Current venue rules and account economics can change. Lighter currently documents different fee
and latency treatment for Standard and Premium accounts. dYdX documents a governance-adjustable
maker–taker schedule whose applicable tier depends on trailing volume. Neither venue's current
headline fee is a timeless constant.

The evaluator therefore never hard-codes a promotional fee, infers a tier, treats a rebate as
cash, or reads the latest record without an evaluation cutoff. Every mutable fact must satisfy both:

1. it was effective no later than the evaluation cutoff; and
2. it was observed no later than the evaluation cutoff.

Records learned later cannot enter an earlier report. Missing data remains missing rather than
becoming zero.

## 4. Approaches Considered

### 4.1 Separate conservative evaluator — selected

A new assembler builds an immutable evidence bundle and a pure evaluator converts that bundle into
a typed report. This keeps venue I/O, storage, evidence selection, and economic math separate. It
also prevents the established Bybit–Hyperliquid study from acquiring new semantics.

### 4.2 Generalize the legacy carry study — rejected

This would reuse more code, but it would mix an eight-hour gross-funding replication protocol with
one-hour Lighter–dYdX funding, depth, fees, basis, latency, and capital constraints. It creates a
high risk of silent semantic drift in an already frozen research protocol.

### 4.3 Event-driven paper simulator — deferred

REST books with local receipt timestamps cannot establish queue position, continuous sequence, or
actual fill probability. A simulator built now would express more execution certainty than the
evidence supports. Paper fills require their own future design and forward clock.

## 5. Architecture

### 5.1 Typed policy

`EconomicsPolicy` freezes all researcher-selected inputs and thresholds. It includes:

- `protocol_version = "lighter-dydx-shadow-economics-v1"`;
- account equity in USD;
- approved annual cash-benchmark rate;
- fee-tier name for each venue;
- account type and documented taker-latency floor for each venue;
- reviewed point-in-time initial-margin, maintenance-margin, close-out-margin, and liquidation-penalty
  assumptions for each venue and asset;
- a reviewed source URL and SHA-256 source hash for each execution assumption;
- a nonnegative fixed transfer/rebalance/operational cost;
- the training and evaluation boundaries;
- book age, cycle-skew, and historical sampling limits; and
- the frozen risk and decision thresholds in this specification.

The policy is canonicalized to JSON and hashed. Unknown fields, floating-point numbers, naive
timestamps, non-finite decimals, unsupported assets or venues, and thresholds different from this
protocol are rejected. An operator can choose equity, benchmark, tiers, documented latencies, and
operational cost; the operator cannot weaken the protocol's evidence or risk gates.

### 5.2 Point-in-time evidence assembler

`EconomicsEvidenceAssembler` queries a `DuckDBStore` at an explicit `known_as_of` and constructs one
`EconomicsAssemblyResult` per asset. A result contains either a complete
`EconomicsEvidenceBundle` or canonical missing-evidence reasons plus the lineage that was available.
The complete bundle contains:

- the bundled `lighter-dydx-core-v1` compatibility report known by the cutoff;
- latest compatible Lighter and dYdX instrument specifications;
- exactly aligned settled hourly funding observations for the full study window;
- historical pair-complete book cycles selected without looking forward;
- the latest pair-complete book cycle for current depth;
- point-in-time fee schedules for the two exact tier names;
- the complete policy; and
- sorted, unique source hashes for every external evidence record.

The assembler performs no economic inference and never returns a partial bundle as evaluable.

### 5.3 Pure evaluator

`CandidateEconomicsEvaluator` accepts a validated assembly result. An incomplete result becomes an
`INSUFFICIENT_EVIDENCE` report whose dependent headline values are absent. A complete result enters
the deterministic Decimal calculations. The evaluator has no database, clock, filesystem,
environment, network, random, AI, or venue dependency.

### 5.4 Immutable report storage

A new append-only `economic_evaluations` table stores:

- evaluation UUID;
- asset, `known_as_of`, and `evaluated_at`;
- decision and selected direction;
- policy hash;
- canonical report JSON;
- report hash; and
- schema version.

`evaluated_at` is the actual UTC time at which the artifact was created and must not precede
`known_as_of`. The immutable identity is `(evaluation_id)`. Repeating the same identity with
identical content is idempotent; different content raises the existing conflicting-record error.
Reports are never updated in place.

Corrected reports use report, complete-economics, and horizon schema version 2. Development
schema-one reports may already exist in an append-only store, but their missing per-venue
components cannot be reconstructed. Readers preserve their top-level identity and timestamps as a
minimal legacy summary; the dashboard displays `LEGACY_ECONOMICS_SCHEMA_UNSUPPORTED` as
`INSUFFICIENT_EVIDENCE` and withholds every economic value. It never rewrites the stored JSON,
silently drops the record, or synthesizes the missing components.

### 5.5 Interfaces

The CLI adds two read/research workflows:

1. import reviewed `FeeSchedule` records from a local JSON document; and
2. run `carry economics` for an asset, policy document, database, and explicit cutoff.

The command renders stable text or canonical JSON and persists the report transactionally. It exits
nonzero for malformed input or unavailable storage, but `INSUFFICIENT_EVIDENCE` and `REJECTED` are
valid research results. Fee imports require the venue's official documentation URL, exact effective
and observation times, and a reviewed content hash; a numeric rate without provenance is rejected.

The local dashboard adds a read-only **Conservative economics** section. It shows the latest report
known by the dashboard cutoff for BTC, ETH, and SOL: decision, frozen direction, limiting reason,
assigned capital, evidence age, conservative 7/14/28-day P&L, and stress status. Missing reports
render as unavailable. The page cannot create a policy, run an evaluation, or start another process.

## 6. Evidence Windows and Direction Freezing

Each evaluation uses 90 continuous UTC days ending no later than `known_as_of`:

- the first 30 days are the training window; and
- the final 60 days are the evaluation window.

The study end is the most recent whole UTC hour and may precede `known_as_of` by no more than 65
minutes. This prevents a stale historical regime from being presented as current. Each paired hour
must have exactly one immutable settled Lighter observation and one immutable settled dYdX
observation, each normalized to a one-hour interval. At least 99% of all requested hours and at
least 99% of each individual window must be paired. Duplicate revisions with the same immutable
identity and different values fail closed. A row observed after `known_as_of` is unavailable.

The adapters normalize a positive funding rate to mean longs pay shorts. For each paired hour:

```text
raw_differential = lighter_hourly_rate - dydx_hourly_rate
```

The nearest-rank median of training-window differentials selects the fixed direction:

- positive median: short Lighter and long dYdX;
- negative median: short dYdX and long Lighter; and
- zero median: `REJECTED` with `TRAINING_FUNDING_MEDIAN_ZERO`.

The oriented hourly funding return is:

```text
short Lighter / long dYdX:  lighter_rate - dydx_rate
short dYdX / long Lighter:  dydx_rate - lighter_rate
```

The selected direction is not recomputed inside the evaluation window. The current-regime input is
exactly the final 168 consecutive hourly boundaries ending at `evaluation_end`; it cannot select the
last 168 available rows across a gap. A missing or displaced boundary makes the report
`INSUFFICIENT_EVIDENCE` with `CURRENT_FUNDING_WINDOW_INSUFFICIENT`. When complete, its median must
remain positive in the frozen direction. Otherwise the report is `REJECTED` with
`CURRENT_FUNDING_REGIME_REVERSED`.

## 7. Book Selection and Base-Quantity Matching

Only stored cycles containing both venues and the requested asset are eligible. A selected cycle
must:

- have status `complete`;
- contain exactly one Lighter and one dYdX book for the asset and cycle UUID;
- have all source lineage present;
- have nonempty, ordered, uncrossed sides;
- have maximum pair effective-time skew no greater than 1,000 milliseconds; and
- for the latest cycle, have request completion no more than 30 seconds before `known_as_of`.

For each historical UTC hour, the assembler selects the latest eligible cycle completed at or
before the boundary and no more than five minutes old. It never selects a future cycle. At least
99% of evaluation hours must have an eligible representative pair. The representative pair's
window timestamp is the exact UTC boundary so consecutive windows cannot be split by harmless
source-time jitter. The nested venue snapshots retain their original effective timestamps for
age, skew, and lineage checks.

The two legs use equal base quantity `q`. This provides exact base-asset delta matching before
rounding. The quantity must satisfy both venue quantity steps and minimum notionals. A common
quantity is rounded down to the coarser compatible step. After rounding:

```text
absolute_net_base_delta / total_base_quantity <= 0.25%
```

If the instruments have no compatible positive quantity within depth and capital limits, the
report is rejected.

The evaluator walks levels in deterministic price order:

- a long entry consumes asks;
- a short entry consumes bids;
- a long exit consumes bids; and
- a short exit consumes asks.

It calculates exact quantity-weighted average prices. It never calls this result a fill and never
models queue position. Position sizing must also prove that both opposite-side forced-exit walks
can consume `forced_exit_depth_multiplier * q`. When exit depth is the tighter constraint, the
evaluator rounds the quantity down to supported capacity before rejecting it.

## 8. Capital and Capacity

`assigned_capital_usd` includes both venue legs. Aggregate absolute entry notional cannot exceed
assigned capital. No leg notional can exceed the collateral notionally assigned to that leg; the
model is fully collateralized and assumes no leverage benefit.

The maximum assigned capital is the minimum of:

- 10% of total account equity;
- USD 500;
- capital supportable by both books at the compatible base quantity; and
- capital permitted by the incomplete-leg stress gate.

The incomplete-leg shock is a 10% adverse move on one unhedged leg. Modeled incomplete-leg loss
must not exceed 0.25% of total account equity. The evaluator sizes down rather than lowering the
shock or loss limit. If the resulting assigned capital cannot satisfy both venue minimum notionals
or the USD 3 profit gate, it rejects the candidate.

The report shows both return on assigned capital and return on total account equity. Unassigned
equity remains cash and stays in the account-return denominator.

For each venue leg, stressed collateral after the 10% adverse move, stressed taker close, fees, and
documented liquidation penalty must remain strictly greater than the larger of the point-in-time
maintenance- and close-out-margin requirements. Missing risk parameters make the report
insufficient. A failed inequality sets `modeled_liquidation = true` and rejects the report; the
evaluator cannot replace a missing parameter with a current web value or a zero.

## 9. Conservative Economic Calculation

All calculations use exact Decimal values. No binary float enters a financial calculation.

### 9.1 Funding lower tail

For each evaluation hour, calculate each venue leg once from its exact entry notional. A positive
venue rate means longs pay shorts, so the signed USD components are:

```text
short Lighter / long dYdX:
    lighter_funding_usd =  lighter_entry_notional_usd * lighter_rate
    dydx_funding_usd    = -dydx_entry_notional_usd * dydx_rate

short dYdX / long Lighter:
    lighter_funding_usd = -lighter_entry_notional_usd * lighter_rate
    dydx_funding_usd    =  dydx_entry_notional_usd * dydx_rate
```

Construct every complete rolling 7-, 14-, and 28-day window of the aggregate signed USD cashflow
in the 60-day evaluation period. A rolling window may not bridge a missing hour. Select the
nearest-rank fifth-percentile window by aggregate USD cashflow and retain that actual window's
Lighter and dYdX rate sums and USD components. The portfolio-level conservative funding rate is
`gross_funding_usd / assigned_capital_usd`; the venue-rate differential is not applied to both legs
a second time.

### 9.2 Funding-reversal reserve

For each horizon, calculate the maximum cumulative drawdown of the aggregate signed hourly USD
cashflow path inside any complete evaluation subwindow no longer than that horizon. The
nonnegative USD result is the funding-reversal reserve. It is charged in addition to the lower-tail
estimate intentionally; this is a conservative shadow gate, not an unbiased expected-value
estimator.

### 9.3 Basis-divergence reserve

For an hourly representative pair, define the midpoint at each venue and the fixed-direction basis:

```text
short Lighter: (lighter_mid - dydx_mid) / pair_mid
short dYdX:    (dydx_mid - lighter_mid) / pair_mid
pair_mid:      (lighter_mid + dydx_mid) / 2
```

For every complete 7-, 14-, and 28-day book window, adverse basis change is
`max(0, ending_basis - starting_basis)`. The reserve is the nearest-rank 99th percentile adverse
change multiplied by the average exact entry notional,
`(lighter_entry_notional_usd + dydx_entry_notional_usd) / 2`. Favorable convergence is set to zero
and never credited. The two-leg assigned-capital total is not used as a one-leg basis notional.

### 9.4 Entry and exit costs

Entry slippage is the adverse difference between each walked entry price and its venue midpoint.
The normal forced-exit reserve uses the current opposite-side walked price with a 2× adverse depth
multiplier. Taker fees are charged on both legs at entry and exit from the exact point-in-time fee
schedules. Negative maker rates are irrelevant because no maker completion is assumed.

The doubled-transaction-cost scenario multiplies entry slippage, exit slippage, taker fees, and the
fixed operational cost by two. It does not reduce funding or risk reserves.

### 9.5 Latency reserve

Normal evidence requires pair-cycle skew no greater than one second. Five-second stress uses
prospectively recorded consecutive pair cycles to calculate the adverse change in the two-leg
executable quote over intervals no longer than five seconds. The reserve is the larger of:

- the policy's documented venue-latency floor converted through observed adverse price movement;
  and
- the nearest-rank 99th percentile empirical five-second adverse move.

If no valid five-second sample exists, the report is insufficient. At least 25 historical quote
observations must pass normal costs and at least 10 must remain positive after the five-second
reserve. These are executable-quote observations, not simulated fills.

For the observation counts, each eligible evaluation-hour book pair is treated as a quoted
counterfactual: the already-frozen 7-day funding lower tail is combined with that pair's walked
depth and point-in-time costs. Positive quoted economics count once under normal costs and once
under five-second stress when applicable. No fill, queue priority, balance, or position state is
created.

### 9.6 Operational cost

The policy supplies one reviewed, nonnegative USD cost covering the intended pre-funding,
rebalance, transfer, and operational path. Zero is accepted only when the policy explicitly states
that both venues are already prefunded and still supplies a reviewed source hash. The evaluator
does not infer custody, route, gas, bridge, withdrawal, tax, or KYC costs.

### 9.7 Stress-loss and drawdown identities

The two portfolio stress gates use the 28-day components:

```text
funding_and_forced_exit_loss_rate
    = (funding_reversal_reserve_usd(28d)
       + forced_exit_cost_usd
       + latency_reserve_usd) / account_equity_usd

modeled_drawdown_rate
    = (funding_reversal_reserve_usd(28d)
       + basis_divergence_reserve_usd(28d)
       + forced_exit_cost_usd
       + latency_reserve_usd) / assigned_capital_usd
```

The first rate must not exceed 0.25% of total account equity. The second must be below 8% of
assigned capital. These are deliberately additive stress charges and are not described as a
probabilistic loss forecast.

### 9.8 Horizon identity

For horizon `H`, let `L` and `D` be the exact Lighter and dYdX entry notionals. The selected signed
rate sums already include direction:

```text
lighter_funding_usd(H) = L * lighter_funding_rate_sum(H)
dydx_funding_usd(H)    = D * dydx_funding_rate_sum(H)
gross_funding_usd(H)
    = lighter_funding_usd(H) + dydx_funding_usd(H)

conservative_funding_rate(H)
    = gross_funding_usd(H) / assigned_capital_usd

basis_divergence_reserve_usd(H)
    = ((L + D) / 2) * basis_divergence_rate(H)

conservative_net_usd(H)
    = gross_funding_usd(H)
    - entry_cost_usd
    - forced_exit_cost_usd
    - fee_cost_usd
    - operational_cost_usd
    - latency_reserve_usd
    - funding_reversal_reserve_usd(H)
    - basis_divergence_reserve_usd(H)

assigned_capital_return(H) = conservative_net_usd(H) / C
account_return(H) = conservative_net_usd(H) / account_equity_usd
```

Per-hour venue funding is calculated separately before aggregation so notional normalization and
direction remain auditable. The implementation exposes both signed venue rate sums, both signed USD
components, their aggregate, and the portfolio-normalized rate.

Annualization is simple, not compounded:

```text
annualized_conservative_return(H)
    = assigned_capital_return(H) * 365 / holding_days(H)
```

## 10. Decisions and Gates

The decision enum contains exactly:

- `INSUFFICIENT_EVIDENCE`;
- `REJECTED`; and
- `SHADOW_CANDIDATE`.

### 10.1 Insufficient evidence

Examples include:

- missing or future-dated fee, instrument, funding, book, dossier, or policy evidence;
- funding or hourly book coverage below 99%;
- fewer than 90 continuous point-in-time days;
- stale latest books or excessive pair-cycle skew;
- fewer than 25 normal quote observations or 10 five-second stress observations;
- conflicting immutable revisions;
- missing lineage; or
- a missing operational-cost or latency assumption.

An insufficient report withholds every headline net return that depends on the missing input.

### 10.2 Rejected

Evidence-complete reports are rejected when any of these holds:

- compatibility has a blocking or missing-evidence check;
- training funding median is zero;
- current seven-day funding regime is nonpositive in the frozen direction;
- no compatible delta-matched size clears minimum notional and depth;
- any 7-, 14-, or 28-day conservative net result is nonpositive;
- planned-hold profit is less than 0.30% of assigned capital or USD 3;
- annualized conservative return is below the greater of 12% or the approved cash benchmark plus
  five percentage points;
- the 28-day result is nonpositive with doubled transaction costs;
- modeled funding-reversal plus forced-exit loss exceeds 0.25% of total equity;
- modeled drawdown is 8% or more;
- any modeled liquidation occurs; or
- the normal or five-second quote-observation count misses its threshold.

All applicable rejection reasons are returned in sorted unique order. The evaluator does not stop
at the first economic failure after it has complete evidence.

### 10.3 Shadow candidate

`SHADOW_CANDIDATE` requires complete evidence and every gate above to pass. The report must display:

> Research only — shadow candidate, not a fill, recommendation, or trading authorization.

It cannot be consumed by an execution interface. Advancing further requires a new specification,
frozen parameters, at least 90 continuous days of queue-aware forward paper execution, complete
ledger reconciliation, and a distinct user approval.

## 11. Report Contract

`CandidateEconomicsReport` contains:

- report schema version 2 and the protocol version;
- evaluation UUID, asset, cutoff, evaluation time, training window, and evaluation window;
- policy and source hashes;
- decision and canonical reason codes;
- selected short and long venues;
- evidence coverage and freshness;
- fee and execution assumptions;
- compatible base quantity and per-leg notionals;
- assigned capital, account equity, and unused cash;
- entry, exit, fee, latency, operational, funding-reversal, and basis reserves;
- one typed horizon result for 7, 14, and 28 days;
- normal and five-second observation counts;
- incomplete-leg loss, drawdown, and liquidation outcomes;
- doubled-cost 28-day result; and
- the fixed research warning.

Validators enforce chronological windows, exact component identities, canonical ordering,
direction consistency, capital conservation, decision/reason coherence, and source-hash format.

## 12. Stable Reason-Code Families

Reason codes are machine-readable uppercase identifiers. Initial families are:

- `COMPATIBILITY_*`;
- `FEE_*`;
- `FUNDING_*`;
- `BOOK_*`;
- `LATENCY_*`;
- `POLICY_*`;
- `DEPTH_*`;
- `DELTA_*`;
- `CAPITAL_*`;
- `NET_*`;
- `STRESS_*`; and
- `LINEAGE_*`.

Messages may improve without changing the codes. Error messages never include response bodies,
credentials, filesystem secrets, or unbounded external text.

## 13. Failure and Transaction Semantics

- Validation happens before report persistence.
- Evidence assembly is read-only and uses one explicit cutoff.
- Report insertion is one DuckDB transaction.
- A failed evaluation cannot write a partial report.
- Database errors produce a stable CLI failure without leaking paths in dashboard responses.
- Unknown enum values, duplicate JSON keys, non-finite decimals, and unexpected fields fail closed.
- A valid insufficient or rejected report is still persisted because failed hypotheses are research
  evidence and must not disappear from trial history.
- The dashboard selects reports only when both `known_as_of <= dashboard_as_of` and
  `evaluated_at <= dashboard_as_of`. A later reconstruction cannot appear in an earlier dashboard
  snapshot merely because it used an earlier evidence cutoff.

## 14. Testing Strategy

Development follows red-green-refactor with exact fixtures and deterministic UUIDs, clocks, and
hashes.

### 14.1 Model and policy tests

- strict schemas, exact versions, timestamp awareness, and Decimal-only inputs;
- canonical JSON, policy hash, source hashes, and reason ordering;
- capital, direction, chronology, and component identities;
- invalid thresholds cannot weaken the protocol; and
- decision fields cannot contradict evidence or horizon results.

### 14.2 Assembler and storage tests

- latest-known point-in-time fee and instrument selection;
- no future-record leakage;
- exact hourly funding pairing and 99% coverage boundaries;
- same-cycle book identity, age, skew, and historical selection;
- missing, conflicting, duplicate, crossed, or lineage-free records fail closed;
- immutable report insert, idempotent retry, conflict rejection, and migration packaging; and
- existing databases migrate without rewriting prior records; schema-one economics remain visible
  as unsupported legacy summaries rather than being parsed as corrected economics.

### 14.3 Economic identity tests

- funding signs for both fixed directions;
- training selection never reads evaluation observations;
- rolling windows never bridge missing hours, and the current-regime window is the exact final 168
  consecutive boundaries;
- exact nearest-rank fifth and 99th percentiles;
- deterministic level walking, compatible quantity rounding, and forced-exit-aware downsizing;
- per-venue funding cashflow, aggregate funding, entry, exit, fee, reserve, return, and annualization
  identities;
- favorable basis movement receives zero credit;
- doubled transaction costs change only cost components; and
- insufficient inputs withhold dependent headline values.

### 14.4 Monotonic property tests

Property tests prove that:

- increasing a fee, latency, operational cost, slippage, or reserve cannot improve net P&L or a
  decision;
- removing depth cannot increase capacity;
- lowering equity cannot increase the allowed USD position;
- removing evidence cannot promote a report;
- worsening a funding observation in the frozen direction cannot improve a horizon lower tail; and
- adding a future record cannot change an earlier cutoff.

### 14.5 Interface and regression tests

- CLI fee import and text/JSON evaluation output;
- transactional persistence and stable exit behavior;
- dashboard three-row canonical economics view and historical cutoff selection;
- no mutating browser control, remote asset, credential, or execution surface;
- responsive and reduced-motion browser checks;
- bundled resource and wheel verification;
- legacy Bybit–Hyperliquid carry, health, and funding-cycle semantics; and
- full-suite Ruff, formatting, coverage of at least 90%, package build, clean-wheel smoke, and browser
  review.

## 15. AI Boundary

No AI model participates in direction selection, fee parsing, evidence completion, calculation,
reason codes, sizing, or decisions. A later offline AI reviewer may summarize a completed report or
flag contradictions, but its output cannot change a numeric field or promote a decision. The exact
deterministic evaluator remains the authority for this research artifact.

## 16. Completion Criteria

This milestone is complete when:

- typed policy, bundle, report, and storage contracts exist;
- the point-in-time assembler and pure evaluator implement every identity and gate;
- CLI workflows import reviewed fees and persist/render evaluations;
- the dashboard renders latest-known economics for all three assets;
- insufficient data and negative economics are visible and stable;
- no account or execution capability has entered the dependency graph;
- all focused and repository-wide verification gates pass; and
- a final scope audit confirms the output cannot authorize paper or live trading.

## 17. Deferred Work

- automated retrieval or attestation of account-specific fee tiers;
- KYC, entity, tax, sanctions, and jurisdiction eligibility;
- custody, wallet, signing, balances, deposits, withdrawals, and transfers;
- queue-position or fill-probability models;
- paper execution and its forward ledger;
- live order state machines and risk controls;
- dynamic allocation across strategy classes; and
- any claim of expected or guaranteed profit.

## 18. Official Sources

- Lighter trading fees and account latency:
  <https://docs.lighter.xyz/trading/trading-fees>
- Lighter funding mechanics and hourly direction:
  <https://docs.lighter.xyz/trading/funding>
- Lighter contract specifications:
  <https://docs.lighter.xyz/trading/contract-specifications>
- Lighter public API client and generated schemas:
  <https://github.com/elliottech/lighter-python>
- dYdX trading fees:
  <https://help.dydx.trade/en/articles/166995-trading-fees-on-dydx>
- dYdX funding mechanics:
  <https://help.dydx.trade/en/articles/166992-default-funding-rates-on-dydx>
- dYdX liquidation mechanics:
  <https://help.dydx.trade/en/articles/166991-liquidations-on-dydx-chain>
- dYdX perpetual and liquidity-tier protocol definitions:
  <https://github.com/dydxprotocol/v4-chain/blob/main/proto/dydxprotocol/perpetuals/perpetual.proto>
