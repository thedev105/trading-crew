# Payoff-Graph Automated Trading System Design

Date: 2026-08-12

Status: Ready for user review

## 1. Objective

Design an automated trading system for an initial account below USD 10,000 that seeks returns from inconsistent prices among economically related contracts rather than from unhedged market-direction forecasts.

The system has two strictly separated modes:

1. **Mode A — Verified payoff arbitrage:** trades portfolios with a formally verified minimum terminal payoff greater than their conservative all-in acquisition cost.
2. **Mode B — Hedged state-price relative value:** trades statistically significant discrepancies between equivalent or closely related state prices inferred from binary contracts, options, futures, perpetuals, and spot markets.

Mode A is the capital-preserving core. Mode B remains disabled until it passes its own longer paper-validation gate. A Mode B signal must never weaken, resize, or delay a verified Mode A hedge.

## 2. Constraints and Decisions

- Initial capital: assume USD 8,000 for sizing; the design scales down to USD 3,000 and up to USD 10,000.
- User loss tolerance: below 30% peak-to-trough. The system stops substantially earlier.
- Holding period: intraday through 30 days initially; completed Mode A baskets may remain open through settlement.
- Venue universe: adapters may support centralized and self-custodial venues, but no venue is required by the core design.
- Jurisdiction, KYC, tax, and venue eligibility are mandatory live-trading gates but are deliberately postponed during research and paper trading.
- Automation must use official APIs and must not disguise automated activity, evade geographic controls, manipulate markets, or automate identity verification.
- Phase 1 is fully collateralized. Mode B may later use capped derivatives exposure solely for hedging or defined relative-value positions.

## 3. Economic Thesis

Markets frequently represent the same future state through different contracts. Examples include:

- YES/NO complements;
- mutually exclusive and exhaustive outcome buckets;
- nested thresholds, such as BTC above USD 65,000 and BTC above USD 70,000;
- nested dates, such as an event occurring by August versus by December;
- ranges replicated by adjacent thresholds;
- binary price contracts approximated by option spreads;
- event implications across nomination, election, control, margin, and policy markets;
- spot, futures, perpetual, lending, and option positions that produce closely related exposures.

Humans and conventional scanners often compare titles or mid-prices. This system instead compiles settlement rules into explicit payoff states, verifies the relationship formally, reads synchronized executable depth, and prices the entire portfolio after fees and execution risk.

The intended edge is operational and structural: many low-capacity contracts, irregular rules, boundary conditions, multiple order books, and patient multi-leg execution. This edge is more compatible with small capital than latency competition in highly liquid markets.

## 4. Formal Model

For a group of related contracts, define:

- `S`: all valid terminal states after accounting for explicit boundary and cancellation cases;
- `P[s, i]`: terminal payout of instrument `i` in state `s`;
- `x[i]`: non-negative quantity acquired in Mode A;
- `C(x)`: executable acquisition cost at current book depth;
- `F(x)`: taker fees, gas, funding, borrow costs, and settlement costs;
- `R(x)`: conservative reserve for slippage, legging, and explicitly modeled basis risk.

Mode A accepts a candidate only when:

```text
min over s in S of sum(P[s, i] * x[i]) - C(x) - F(x) - R(x) > entry_hurdle
```

The optimizer maximizes this conservative surplus subject to order-book depth, capital, venue, event, and incomplete-leg limits.

Mode B additionally estimates a probability distribution `q[s]` and hedge vector. It may trade positive expected value only when the result remains positive across conservative model, volatility, latency, and transaction-cost perturbations. Mode B opportunities are relative-value trades, not guaranteed arbitrage, and are reported separately.

## 5. Mode A: Verified Payoff Arbitrage

### 5.1 Supported relationship classes

The first release supports only deterministic templates that can be exhaustively enumerated:

1. **Complements:** YES + NO pays one unit.
2. **Exhaustive exclusive sets:** buying every YES pays one unit; buying every NO pays `N - 1` units.
3. **Logical implication:** if A implies B, `YES(B) + NO(A)` pays at least one unit.
4. **Nested thresholds:** a higher threshold implies every lower threshold for the same oracle and observation rule.
5. **Nested deadlines:** occurrence by an earlier deadline implies occurrence by a later deadline when all other rules match.
6. **Range/threshold identities:** enabled only when inclusivity, precision, oracle, timestamp, and cancellation behavior produce identical payoff vectors in every enumerated state.
7. **User-approved cross-event relationships:** permitted only after deterministic rule review and state enumeration.

No free-form model assertion may authorize a Mode A trade. A language model may suggest candidate relationships, but a deterministic verifier must prove the payoff bound from an approved template and normalized rule fields.

### 5.2 Rule normalization

Each contract is normalized into:

- underlying event or asset;
- settlement oracle and exact source instrument;
- observation timestamp and timezone;
- comparison operator and threshold;
- numerical precision and rounding;
- interval boundary inclusivity;
- cancellation, postponement, substitution, and 50/50 rules;
- dispute and clarification mechanism;
- payout currency and collateral;
- earliest safe order-cancellation time.

Missing or conflicting fields make the contract ineligible. Rule changes invalidate all derived relationships and cancel affected open orders.

### 5.3 Opportunity hurdle

- Non-atomic taker completion: minimum modeled net surplus of 1.00% of deployed basket capital and USD 2, whichever is greater.
- Fully completed or atomic-equivalent basket: minimum locked surplus of 0.35% and USD 1.
- Maker-first basket: completion must still pass the non-atomic hurdle after the first fill.
- Liquidity rewards and uncertain rebates are recorded as upside and excluded from the entry decision.

## 6. Mode B: Hedged State-Price Relative Value

Mode B extends the state representation to instruments that are economically close but not payoff-identical.

### 6.1 Pricing inputs

- arbitrage-consistent option volatility surface;
- option-implied digital probabilities from tight call spreads;
- spot, futures basis, perpetual funding, and borrow rates;
- binary-contract threshold and range ladders;
- order-flow, liquidation, open-interest, and liquidity-state changes;
- oracle-to-venue and settlement-index basis history.

### 6.2 Signal

The system compares the executable state price of a target contract with a hedged replication interval rather than a single fair-value point. A trade is eligible only when the target price lies outside that interval after:

- bid/ask spreads and full depth;
- all venue fees;
- option smile and interpolation uncertainty;
- settlement-index basis;
- funding and borrow stress;
- hedge slippage;
- a multiple-testing penalty;
- forced-flow regime uncertainty.

Forced-flow information changes the required margin of safety and execution timing. It does not create a trade by itself.

### 6.3 Activation gate

Mode B requires at least 60 days of walk-forward paper results, a net annualized Sharpe ratio above 1.5, maximum simulated drawdown below 10%, positive performance under doubled transaction costs, and profit distributed across at least three independent contract families. It begins live with no more than 5% of account equity in gross exposure.

## 7. System Components

### 7.1 Venue adapters

Each adapter exposes normalized methods for discovery, rules, balances, books, trades, orders, cancellations, and health. Adapters do not contain strategy logic.

### 7.2 Rule compiler

Transforms raw contract rules into normalized predicates and explicit terminal states. It records the original rule text, parser version, normalized representation, and verification result.

### 7.3 Payoff graph

Stores instruments as nodes and proven complement, implication, exclusion, and equivalence relationships as typed edges. Every edge includes its proof template and invalidation conditions.

### 7.4 Synchronized market-data service

Maintains Level-2 books, sequence numbers, exchange timestamps, local receipt timestamps, and staleness status. It retains raw append-only data so every decision can be replayed.

### 7.5 Opportunity optimizer

Enumerates valid instrument groups, walks executable depth, solves the payoff-constrained portfolio problem, and returns a sized opportunity with a complete cost and risk decomposition.

### 7.6 Execution planner

Chooses maker-first, immediate completion, or rejection. It prepares every hedge before submitting the first order and never assumes batch submission is atomic.

### 7.7 Independent risk engine

Approves or rejects orders without relying on strategy code. It tracks completed baskets, incomplete legs, event concentration, venue exposure, collateral, drawdown, stale feeds, and unresolved reconciliation errors.

### 7.8 Ledger and attribution

Records fills, fees, rebates, funding, gas, marked value, guaranteed terminal value, realized profit, and profit source. Mode A, Mode B, incentives, and accidental directional exposure are reported separately.

### 7.9 Monitoring and kill switch

Provides alerts for stale books, rule changes, rejected hedges, balance mismatches, venue outages, abnormal settlement status, and risk-limit breaches. A local and remote manual kill switch cancels orders and prevents new submissions.

## 8. Data Flow

1. Venue adapters discover contracts and fetch full rules.
2. The rule compiler emits normalized predicates or marks the contract ineligible.
3. The payoff graph groups contracts and proves supported relationships.
4. The market-data service supplies synchronized executable books.
5. The optimizer constructs and sizes candidate portfolios.
6. The risk engine validates capital, concentration, freshness, and incomplete-leg loss.
7. The execution planner submits the selected order sequence.
8. Private order streams and periodic reconciliation confirm the actual position.
9. Completed baskets are held, opportunistically closed, merged, or redeemed.
10. The ledger attributes realized results and feeds validation reports.

Any missing, stale, or contradictory input causes a fail-closed rejection.

## 9. Execution Policy

- Prefer post-only maker orders on the bottleneck leg.
- Pre-calculate protected FOK completion orders for the liquid legs.
- After a first-leg fill, re-read all books and recompute the portfolio before completion.
- If completion fails, hedge or unwind according to the precomputed loss-minimizing path.
- Cancel quotes before known information events, settlement cutoffs, or rule-dependent danger windows.
- Never average down an incomplete basket merely to improve its displayed cost.
- Reconcile balances and positions after every execution sequence and on a periodic timer.
- Disable new trading when a user stream disconnects, order status is unknown, or cancellation cannot be confirmed.

## 10. Capital and Risk Policy

For a USD 8,000 account after live activation:

- 25% untouched safety reserve;
- up to 50% in completed Mode A baskets;
- up to 15% reserved for temporary hedges and leg completion;
- up to 10% in Mode B after its activation gate.

Limits:

- one completed Mode A basket: 5% of equity;
- one real-world event cluster: 10%;
- one venue or custodial failure domain: 25%;
- one incomplete leg: USD 50 or 0.625% of equity, whichever is lower;
- Mode B initial gross exposure: 5%, later capped at 10%;
- Mode B gross leverage: 1.5 times its allocated capital;
- no naked short options, unlimited-loss positions, martingale sizing, or cross-collateral contagion.

Drawdown controls:

- 3% loss in 24 hours: halt new orders and reconcile;
- 10% peak-to-trough: halve all future size after investigation;
- 15%: stop all new trading and require manual review;
- 20%: close non-guaranteed exposure and enter capital-preservation mode;
- 30% is a catastrophe boundary, not an operational stop target.

A venue insolvency or frozen withdrawal can exceed modeled limits; venue exposure caps and the untouched reserve mitigate but cannot eliminate this risk.

## 11. Error Handling and Security

- Fail closed on stale data, parser uncertainty, clock drift, authentication errors, unknown order state, or balance mismatch.
- Use trading-only API keys with withdrawals disabled where supported.
- Keep signing and withdrawal authority separate.
- Store secrets outside source control and redact them from logs.
- Use idempotent client order identifiers and deterministic reconciliation.
- Maintain heartbeats and cancel-on-disconnect where supported.
- Require manual approval for new rule templates, new venues, and changes to risk limits.
- Complete jurisdiction, KYC, tax, sanctions, and venue-terms review before enabling live orders.

## 12. Testing Strategy

### 12.1 Rule and payoff tests

- unit tests for strict and inclusive boundaries, rounding, timezones, cancellations, delays, and 50/50 resolutions;
- exhaustive state tests for each approved proof template;
- property tests confirming the optimizer's claimed minimum payoff;
- mutation tests proving that a changed operator, timestamp, or oracle invalidates a relationship.

### 12.2 Market-data and execution tests

- recorded-book replay with sequence gaps and stale updates;
- depth and fee calculations at multiple sizes;
- partial fills, rejected FOK orders, duplicate messages, reconnects, and unknown order states;
- simulated latency at 250 milliseconds, 1 second, and 5 seconds;
- doubled fees and slippage stress;
- venue outage and cancel-failure drills.

### 12.3 Risk and accounting tests

- portfolio payout reconciliation in every state;
- incomplete-leg worst-case loss;
- concentration and drawdown transitions;
- mark-to-market versus guaranteed terminal value;
- fee, funding, rebate, merge, redemption, and settlement attribution.

## 13. Validation Program

### 13.1 Mode A read-only validation

Collect at least 30 continuous days of synchronized rules and books. No funds or paper fills are required for raw opportunity detection.

Proceed only if all of the following hold:

- at least 25 opportunities survive simultaneous books, executable depth, current fees, and 1-second latency;
- at least 10 survive 5-second latency;
- median net surplus is at least 0.75%;
- median executable capacity is at least USD 100;
- projected net return on total USD 8,000 capital is at least 1.5% per month;
- manually reviewed relationships have zero false claims of a guaranteed minimum payout;
- simulated 99th-percentile incomplete-leg loss is below 0.25% of equity;
- simulated drawdown remains below 10%.

If the program fails, Mode A is rejected or narrowed. Thresholds are not relaxed to rescue it.

### 13.2 Mode A paper execution

Run 30 additional days using live order-placement simulation with queue-aware fills. Require positive net profit with rewards excluded, no risk-limit breaches, and reconciliation accuracy of 100% before a small live pilot.

### 13.3 Mode B validation

Collect and paper trade for at least 60 days using walk-forward models only. Training data must precede every decision. Results must pass the activation gate in Section 6.3 under normal and doubled costs.

## 14. Rollout

1. Read-only data and rule normalization.
2. Mode A payoff graph and opportunity audit.
3. Mode A queue-aware paper execution.
4. Mode A live pilot with USD 250 total allocation.
5. Gradual Mode A sizing after 100 reconciled live baskets.
6. Mode B research running in shadow mode.
7. Mode B live pilot only after its independent activation gate.

Each step requires a written validation report. Failure at one step prevents progression but does not force modification of the prior stable step.

## 15. Non-Goals

- copying leaderboard traders;
- generic price-direction machine learning;
- ultra-low-latency competition or colocation;
- market manipulation, self-trading, spoofing, or front-running private orders;
- relying on platform rewards to hide negative trading economics;
- supporting arbitrary contracts before their rule templates are verified;
- guaranteeing profit or promising a target return.

## 16. Primary Evidence

- Polymarket APIs expose public market, trade, position, and Level-2 order-book data, with authenticated order management.
- Current batch order submission reduces latency but processes orders in parallel and does not provide multi-leg atomicity.
- Current crypto taker fees are probability-dependent and can consume small apparent discrepancies; maker fees are zero and eligible makers may receive rebates.
- Empirical research documents historical market-rebalancing and combinatorial arbitrage on Polymarket, while newer high-frequency research shows that simple cross-venue price prediction can fail after costs.
- Crypto binary contracts can be analyzed as digital options, supporting Mode B's state-price framework while also introducing option-model and settlement-basis risk.

Sources:

- https://docs.polymarket.com/api-reference/introduction
- https://docs.polymarket.com/trading/orderbook
- https://docs.polymarket.com/api-reference/trade/post-multiple-orders
- https://docs.polymarket.com/market-makers/maker-rebates
- https://docs.polymarket.com/concepts/resolution
- https://arxiv.org/abs/2508.03474
- https://arxiv.org/abs/2607.26245
- https://papers.ssrn.com/sol3/papers.cfm?abstract_id=6748186
