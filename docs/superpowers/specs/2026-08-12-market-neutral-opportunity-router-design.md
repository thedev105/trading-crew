# Market-Neutral Opportunity Router Design

Date: 2026-08-12

Status: Approved architecture; revised specification ready for final user review

Supersedes: `2026-08-12-payoff-graph-trading-system-design.md`

## 1. Objective

Design an automated trading and research system for an initial portfolio below USD 10,000. The system seeks returns from structural pricing inconsistencies and compensated market-neutral carry rather than depending on unhedged market-direction forecasts.

The system is an **opportunity router**, not one monolithic strategy. It evaluates independent proposals using common data, cost, capital, failure-domain, execution, and evidence standards. It may hold cash when no proposal clears those standards.

The initial live-eligible engines are:

1. **Class G — Verified payoff arbitrage:** a deterministic verifier proves that a completed portfolio's minimum terminal payoff exceeds its conservative all-in acquisition cost.
2. **Class C — Market-neutral carry:** a delta-matched portfolio seeks funding or basis income, with explicit exposure to funding reversal, basis divergence, margin, collateral, venue, and execution risk.

**Class S — Statistical relative value** remains research-only. No Class S signal may authorize live risk until it passes a later design review and activation gate.

AI-assisted discovery, forecasting, and model governance are defined in the companion [AI Augmentation Design](2026-08-12-ai-augmentation-design.md). AI components are sidecars and have no order, credential, proof, or risk-limit authority.

The design does not promise profit or a target return. The first goal is to determine whether either engine has repeatable, executable economics after all costs and failure risks.

## 2. Constraints and Decisions

- Initial capital: assume USD 8,000; the design scales from USD 3,000 to USD 10,000.
- User loss tolerance: below 30% peak-to-trough. Operational controls stop substantially earlier.
- Expected holding period: several days to four weeks for carry; completed Class G baskets may remain open through settlement.
- Initial carry universe: BTC, ETH, and SOL only.
- Initial instrument universe: spot, linear perpetuals, dated futures, and deterministic binary-event contracts.
- Venue adapters may support centralized and self-custodial venues, but strategies may use only venues that pass separate eligibility and failure-domain reviews.
- Jurisdiction, KYC, tax, sanctions, and venue eligibility are postponed during public-data research and simulation, but they are mandatory before authentication, funding, or live orders.
- Automation uses official APIs. It must not disguise automation, mimic a human identity, evade geographic or account controls, manipulate markets, or automate identity verification.
- Initial live trading is fully collateralized at the strategy level. No leg's notional may exceed the capital assigned to support it.
- Cash is a valid position. Strategy thresholds are not relaxed to force capital deployment.

## 3. Research Conclusion and Strategy Selection

### 3.1 Selected barbell

The system combines two economically different return sources:

- **Sparse structural surplus:** payoff-identical or payoff-bounded contracts are priced inconsistently.
- **Recurring balance-sheet compensation:** leveraged traders pay funding or futures basis to market-neutral capital.

Class G can offer strong payoff certainty after completion but may be infrequent and capital-locked. Class C can use capital more regularly but is not arbitrage in the risk-free sense. The barbell preserves the payoff engine while avoiding dependence on it as the sole business.

### 3.2 Strategies not selected for initial live trading

| Strategy | Initial role | Reason |
|---|---|---|
| Cross-venue funding and cash-and-carry | Class C engine | Compatible with days-to-weeks holding and small, pre-funded capital |
| Deterministic prediction-contract relationships | Class G engine | Formal payoff verification can reject semantic and boundary mismatches |
| Options and binary state-price relative value | Class S shadow research | Model, smile, liquidity, and settlement-basis uncertainty |
| Momentum and trend | Regime and risk feature only | Directional crash exposure and unstable results after realistic assumptions |
| Liquidation and order-flow signals | Entry haircut and danger filter only | Reactive, latency-sensitive, and mechanism-dependent |
| DeFi lending and staking | Possible treasury benchmark after separate review | Yield compensates protocol, issuer, slashing, liquidity, and governance risk |
| AMM liquidity provision | Excluded initially | Fee income is offset by adverse selection and loss-versus-rebalancing |
| Naked volatility selling | Excluded | Negative convexity and jump risk conflict with capital preservation |
| Cross-chain arbitrage | Excluded initially | Non-atomic bridge, finality, smart-contract, and operational tail risk |
| Market making and liquidation sniping | Excluded | Latency, inventory, private-information, and infrastructure competition |
| Copy trading | Non-goal | Selection, timing, capacity, and follower slippage obscure the underlying edge |

Reconsidering an excluded strategy requires a separate approved specification. It must not enter through a feature flag in this system.

## 4. Evidence Classes and Common Proposal Model

Every candidate is an immutable `TradeProposal` containing:

- evidence class: `G`, `C`, or `S`;
- instruments, sides, quantities, contract multipliers, and hedge ratios;
- venue and custodial failure domains;
- price, mark, index, oracle, and funding timestamps;
- executable price by depth, not midpoint;
- entry, expected holding, funding, borrow, settlement, transfer, gas, and exit costs;
- expected and stressed cash-flow schedule;
- capital required per venue and estimated lock duration;
- modeled net delta, basis, collateral, and currency exposures;
- worst modeled incomplete-leg and forced-exit losses;
- invalidation conditions, time-to-live, and exit policy;
- data, fee-schedule, model, parser, and code versions;
- complete proof for Class G or hypothesis identifier for Classes C and S.

Class labels cannot be upgraded by a predictive model:

- `G` requires deterministic proof over all normalized terminal states.
- `C` requires offsetting economic exposure but makes no guaranteed-payoff claim.
- `S` includes every proposal whose profit depends materially on a fitted forecast.

The **approved cash benchmark** is the point-in-time net annualized yield of a user-accessible, same-currency insured deposit or short-duration sovereign instrument. It excludes crypto lending, token incentives, and unsecured yield. If no eligible alternative is documented, the benchmark is zero rather than an invented rate.

## 5. Class G: Verified Payoff Arbitrage

### 5.1 Formal model

For related contracts define:

- `S`: all valid terminal states, including boundary, cancellation, postponement, and exceptional cases;
- `P[s, i]`: terminal payout of instrument `i` in state `s`;
- `x[i]`: acquired quantity;
- `C(x)`: executable acquisition cost at current depth;
- `F(x)`: taker fees, gas, settlement, and unavoidable operating costs;
- `R(x)`: conservative slippage and incomplete-execution reserve.

A completed Class G proposal is valid only when:

```text
guaranteed_surplus(x) =
    min over s in S of sum(P[s, i] * x[i])
    - C(x)
    - F(x)
    - R(x)
    > entry_hurdle
```

The optimizer may maximize surplus or capital velocity subject to depth, capital, event, venue, and incomplete-leg constraints. It cannot remove a terminal state or lower a cost reserve to make a candidate pass.

### 5.2 Supported relationships

The initial proof library supports only exhaustively testable templates:

1. YES/NO complements.
2. Mutually exclusive and exhaustive outcome sets.
3. Deterministic logical implications.
4. Nested price thresholds with identical oracle and observation rules.
5. Nested deadlines with otherwise identical resolution rules.
6. Range/threshold identities whose inclusivity, precision, timestamps, and cancellation behavior match exactly.
7. Manually approved cross-event implications with enumerated states.

A language model may propose relationships or extract draft rule fields. Only deterministic code over reviewed normalized rules may approve a Class G proposal.

### 5.3 Rule normalization

Each contract records:

- underlying event or asset;
- exact settlement oracle and source instrument;
- observation timestamp and timezone;
- comparison operator, threshold, precision, and rounding;
- interval-boundary inclusivity;
- cancellation, postponement, substitution, dispute, and fallback behavior;
- payout asset and collateral;
- current rule hash and source retrieval timestamp.

Missing, changed, or contradictory fields make the contract ineligible and cancel affected orders.

### 5.4 Entry hurdles

- Non-atomic immediate completion: net surplus at least 1.00% of deployed basket capital and USD 2.
- Completed or truly atomic-equivalent basket: net surplus at least 0.35% and USD 1.
- Maker-first execution: after the first fill, immediate conservative completion must still meet the non-atomic hurdle.
- Uncertain rebates and liquidity rewards are excluded from entry economics.

## 6. Class C: Market-Neutral Carry

### 6.1 Supported structures

The first release supports:

1. **Spot-perpetual carry:** long spot and short the same asset's perpetual when positive funding adequately compensates the short.
2. **Spot-future cash-and-carry:** long spot and short a dated future trading above spot. Initially, the future must expire within 30 days and the position is intended to be held through cash settlement.
3. **Perpetual-perpetual funding spread:** long the lower-funding venue and short the higher-funding venue in delta-matched contracts.

Reverse cash-and-carry, token borrowing, options, inverse contracts, hyperps/pre-launch contracts, and unmatched collateral currencies are excluded initially.

### 6.2 Contract compatibility

Two legs are compatible only after deterministic comparison of:

- underlying asset and contract multiplier;
- linear versus inverse or quanto payoff;
- index and oracle construction;
- mark-price and liquidation mechanism;
- collateral and P&L currency;
- funding calculation, sampling window, cap, interval, and payment timestamp;
- minimum size, tick size, margin mode, and position limits;
- settlement and auto-deleveraging rules.

Same ticker text is insufficient. For example, a USDT-indexed contract with USDC-denominated P&L introduces collateral and quanto effects that must be modeled.

### 6.3 Carry economics

For each planned holding horizon `H`, the engine estimates:

```text
conservative_net_carry(H) =
    conservative_funding_received(H)
    + conservative_basis_convergence(H)
    - funding_paid(H)
    - entry_and_exit_fees
    - executable_slippage
    - borrow_and_financing_cost
    - transfer_and_gas_cost
    - collateral_currency_reserve
    - failure_and_forced_exit_reserve
```

Current funding annualized from one observation is diagnostic only. The conservative forecast uses point-in-time history, regime duration, percentiles, and mean-reversion behavior. No entry may assume the current rate persists unchanged.

### 6.4 Entry and exit gates

A Class C proposal must satisfy all of the following:

- only BTC, ETH, or SOL;
- planned holding period between 7 and 30 days;
- conservative annualized net return on assigned capital at least the greater of 12% or the current approved cash benchmark plus 5 percentage points;
- conservative net profit over the planned hold at least 0.30% of assigned capital and USD 3;
- estimated normal-cost break-even time no more than one quarter of the planned maximum holding period;
- positive expected result under doubled entry and exit costs;
- net portfolio delta after rounding no more than 0.25% of proposal notional at entry;
- stressed net delta no more than 1% after a 10% one-minute price move and normal execution latency;
- modeled funding-reversal plus forced-exit loss no more than 0.25% of total account equity;
- no liquidation under the collateral and price stresses in Section 12;
- pre-funded exit or hedge capacity on every required venue;
- no required bridge or withdrawal to survive an adverse event.

Exit occurs when the conservative remaining carry falls below the cash benchmark, compatibility breaks, delta or margin limits fail, funding changes sign beyond tolerance, the planned horizon expires, or a venue-health trigger fires.

### 6.5 What “market-neutral” means here

Market-neutral means the portfolio begins with closely offsetting first-order asset exposure. It does not mean risk-free. Residual risks include:

- funding reversal before costs are recovered;
- basis divergence and mark/index differences;
- asynchronous fills and delta drift;
- liquidation or auto-deleveraging on one leg;
- collateral depeg or currency mismatch;
- exchange insolvency, withdrawal freeze, API outage, or rule change;
- borrow recall and settlement differences;
- profits trapped on one venue while margin is needed on another.

These risks are measured independently and may not be netted away merely because the price delta is near zero.

## 7. Class S: Shadow Research

Class S can examine:

- option-implied digital probabilities versus binary-contract ladders;
- state-price and volatility-surface dislocations;
- medium-horizon time-series momentum;
- liquidation, open-interest, order-flow, and liquidity regimes;
- cross-sectional and cross-venue lead-lag effects.

Initially these models may only:

- produce counterfactual paper proposals;
- adjust a Class C proposal's uncertainty reserve upward;
- veto or delay entry during a dangerous regime.

They may not reduce a cost reserve, increase size, create leverage, or turn a failing Class G or C proposal into a passing one. Live activation requires a new approved specification rather than a configuration change.

## 8. Opportunity Router

The router consumes approved proposals and allocates scarce capital across failure domains. It optimizes **conservative capital efficiency**, not headline annualized yield.

For reporting:

```text
capital_velocity = conservative_net_profit / (capital_at_risk * expected_lock_days)
```

The router also applies class-specific risk charges. Class G uses verified surplus after the incomplete-leg reserve. Class C uses conservative cash flows after funding, basis, forced-exit, collateral, and failure-domain reserves. Scores from optimistic and guaranteed fields are never mixed.

Allocation rules:

1. Reject any proposal failing its own class gate.
2. Reserve capital for incomplete-leg and emergency-hedge scenarios before allocation.
3. Apply event, asset, venue, collateral, and custodial concentration limits.
4. Prefer the higher conservative capital velocity when proposals compete for the same capital.
5. Prefer Class G when scores are economically indistinguishable.
6. Leave funds in the approved cash position when no trade clears its benchmark.

## 9. System Components

### 9.1 Venue adapters

Adapters expose normalized discovery, contract metadata, fee schedules, funding history, books, trades, balances, margin, positions, orders, cancellations, transfers, and health. Strategy logic does not live in adapters.

### 9.2 Point-in-time data recorder

The recorder stores raw append-only public and private messages with exchange timestamps, local monotonic receipt times, sequence numbers, request/response latency, and source versions. Historical revisions never overwrite what the system could have known at decision time.

### 9.3 Instrument and rule registry

The registry versions event rules and derivatives specifications, including oracles, margin tables, funding intervals, fee tiers, and symbol mappings. Compatibility and payoff proofs reference immutable versions.

### 9.4 Payoff compiler and graph

The compiler produces explicit state predicates and payout vectors. The graph records proven complement, exclusion, implication, and equivalence edges with templates and invalidation conditions.

### 9.5 Carry analyzer

The analyzer normalizes funding per hour, reconstructs realized payments, measures funding and basis regimes, estimates holding-period cash flows, calculates hedge ratios, and runs margin and reversal stresses.

### 9.6 Opportunity optimizer and router

The optimizer walks executable depth and constructs class-valid proposals. The router ranks approved proposals and applies portfolio constraints without changing their evidence class or underlying assumptions.

### 9.7 Execution planner

The planner prepares both legs and all failure paths before the first submission. It selects maker-first, synchronized taker, or rejection. Batch submission is never assumed to provide multi-venue or multi-leg atomicity.

### 9.8 Independent risk engine

The risk engine approves orders independently of strategy code. It tracks capital per failure domain, delta, incomplete legs, margin distance, collateral, guaranteed payout, event and asset concentration, drawdown, data health, and reconciliation state.

### 9.9 Ledger and attribution

The double-entry ledger records deposits, transfers, fills, fees, rebates, funding, borrow, gas, settlement, marks, collateral conversions, and realized P&L. It separates:

- Class G verified surplus;
- Class C funding, basis, and hedge P&L;
- Class S counterfactual results;
- incentives and rebates;
- accidental directional exposure;
- unexplained reconciliation differences.

### 9.10 Experiment registry

Before a backtest, the registry records the hypothesis, permitted features, parameters, evaluation window, benchmark, and success criteria. Every run records code, data, model, fee, and trial-family identifiers, including failed experiments.

### 9.11 Monitoring and kill switch

Monitoring alerts on stale data, rule changes, funding anomalies, hedge rejection, delta drift, margin deterioration, collateral depeg, balance mismatch, venue outage, withdrawal suspension, and unknown orders. Local and remote controls cancel orders and disable new submissions.

### 9.12 AI sidecars

AI components may generate structured candidate evidence, conservative risk forecasts, execution forecasts, and research records. They communicate through versioned schemas and cannot emit an approved `TradeProposal`. Deterministic services independently validate every AI-derived field before it can affect a proposal. The companion AI design defines permissions, validation gates, prompt-injection defenses, model governance, and rollout scope.

## 10. Data Requirements

### 10.1 Common data

- Level-2 books and trades with sequence integrity;
- exchange and local receipt timestamps;
- point-in-time fee tiers, rebates, and minimum sizes;
- instrument specifications and their changes;
- balances, fills, positions, margin, and funding payments;
- venue-health and API-error events;
- reference spot, index, oracle, and collateral prices.

### 10.2 Class G data

- complete rule text and rule hashes;
- market grouping and resolution metadata;
- settlement, dispute, and clarification history;
- executable books for every leg;
- payout, merge, redemption, and settlement records.

### 10.3 Class C data

- at least twelve months of point-in-time funding, mark, index, spot, basis, and open-interest history where available;
- exact funding intervals and realized account payments;
- at least 45 continuous days of locally synchronized executable books before paper activation;
- collateral, margin, liquidation, and auto-deleveraging parameters;
- deposit, withdrawal, transfer, and outage status;
- borrow availability and rate history whenever borrowing could affect economics.

Missing historical depth must be represented as uncertainty, not silently replaced by midpoint execution.

## 11. Execution and Reconciliation Policy

- Use ordinary, API-native deterministic execution; do not imitate manual clicking or evade platform classifications.
- Prefer post-only placement on the bottleneck leg only when queue-aware completion remains attractive.
- Pre-calculate protected IOC/FOK completion or unwind orders where supported.
- Re-read every book and rerun risk after any first-leg fill.
- Never assume orders sent in one batch are atomic.
- Never transfer collateral after entry as the only way to prevent liquidation.
- Never average into an incomplete position merely to improve displayed basis.
- Reconcile after every execution sequence and on a periodic timer.
- Stop new orders on private-stream disconnect, unknown order state, excessive clock drift, balance mismatch, or unconfirmed cancellation.
- A venue-health event triggers proposal-specific cancel, hedge, reduce, or hold logic; it must not trigger indiscriminate market orders.

## 12. Capital and Risk Policy

For an assumed USD 8,000 portfolio:

- 30% external safety reserve, not deposited to a trading or DeFi venue;
- 10% operational liquidity and emergency-hedge reserve;
- up to 30% assigned to completed Class G exposure;
- up to 40% assigned to Class C capital and collateral;
- 0% assigned to live Class S exposure.

Unallocated strategy sleeves return to the approved cash position. Percentages are ceilings, not allocation targets.

Limits:

- one venue or custodial failure domain: 20% of total equity;
- one stablecoin or collateral issuer, including venue balances denominated in it: 25%;
- one Class G basket: 5%;
- one real-world event cluster: 10%;
- one Class C pair: 10% capital and collateral;
- one underlying asset across Class C: 20%;
- one incomplete leg: USD 40 or 0.50% of equity, whichever is lower;
- aggregate absolute notional of an initial Class C portfolio: no more than its assigned Class C capital;
- initial net delta across Class C: 1% of equity or less;
- no naked short options, unlimited-loss positions, martingale sizing, shared cross-margin between strategies, or rehypothecation chains.

Class C stress suite:

- instantaneous underlying moves of plus or minus 10%, 25%, and 50%;
- collateral depeg of 5% and 15%;
- funding reversal to an adverse historical 99th-percentile rate;
- exit spread and fees at two and four times normal;
- one-leg freeze for 1 hour, 8 hours, and 48 hours;
- loss of one API/private stream while positions remain open;
- withdrawal suspension and inability to move collateral;
- simultaneous venue outage and large underlying move.

No scenario may create a modeled liquidation in the initial live configuration. If a venue cannot provide enough rule or margin data to demonstrate this, it is ineligible.

Drawdown controls:

- 2% loss in 24 hours: halt new entries and reconcile;
- 5% peak-to-trough: cut permitted new size by half after review;
- 8%: stop all new trading and require manual investigation;
- 12%: close non-guaranteed exposure through proposal-specific safe exits;
- 15%: capital-preservation mode; no live resumption without a new approval;
- 30% remains a catastrophe boundary, not an operational stop.

A venue failure can exceed modeled market-risk limits. External reserves and failure-domain caps reduce but do not eliminate that possibility.

## 13. Error Handling and Security

- Fail closed on stale data, sequence gaps, parser uncertainty, clock drift, authentication failures, unknown order state, or balance mismatch.
- Use trading-only API keys with withdrawals disabled where supported.
- Separate trading, signing, withdrawal, and administrative authority.
- Store secrets outside source control and redact them from logs.
- Use idempotent client order identifiers and deterministic recovery.
- Require manual approval for new proof templates, instrument types, venues, collateral, or risk-limit increases.
- Pin and review dependencies used for signing and order submission.
- Keep an append-only audit trail sufficient to reconstruct each proposal, approval, order, fill, funding payment, and exit.
- Complete jurisdiction, KYC, tax, sanctions, and venue-terms review before any live authentication or funding.

## 14. Testing Strategy

### 14.1 Deterministic tests

- exhaustive payout states for every Class G proof template;
- property and mutation tests for boundaries, timestamps, oracles, rule changes, and payout claims;
- contract compatibility tests for multipliers, collateral, inverse/linear structures, funding intervals, and indices;
- funding normalization and realized-payment reconciliation;
- hedge-ratio, rounding, margin, and liquidation calculations;
- all capital, concentration, and state-machine transitions.

### 14.2 Replay and fault tests

- point-in-time book replay without future information;
- queue-aware fills and partial executions;
- latency at 250 milliseconds, 1 second, and 5 seconds;
- rejected orders, duplicate messages, reconnects, and unknown states;
- fee, funding, borrow, gas, and slippage shocks;
- funding-timestamp boundary cases;
- collateral depeg, venue outage, transfer freeze, and cancel failure;
- process restart during every execution state;
- ledger reconciliation to balances and venue statements.

### 14.3 Research-integrity tests

- immutable train, validation, and untouched test windows;
- walk-forward evaluation with embargo around overlapping holding periods;
- survivorship-aware instrument universes;
- delisted and unavailable assets retained in historical tests;
- explicit accounting for every tried model and parameter family;
- Deflated Sharpe Ratio or equivalent multiple-testing correction;
- doubled-cost and leave-one-asset/venue-out tests;
- profit-concentration checks by time, asset, venue, and market regime.

## 15. Validation and Activation Gates

### 15.1 Class G read-only audit

Collect at least 45 continuous days of synchronized rules and executable books. Proceed to paper execution only if:

- at least 25 opportunities survive current fees, executable depth, and 1-second latency;
- at least 10 survive 5-second latency;
- median net surplus is at least 0.75%;
- median capacity is at least USD 100;
- projected annual contribution is at least 2% of total portfolio equity;
- conservative return on assigned capital exceeds the approved cash benchmark by 5 percentage points;
- manual review finds zero false guaranteed-payoff claims;
- simulated 99th-percentile incomplete-leg loss is below 0.25% of equity;
- simulated drawdown is below 8%.

Then run at least 30 additional days of queue-aware paper execution. Require positive net results without rewards, no risk breach, and 100% reconciliation before a live pilot.

### 15.2 Class C historical and forward audit

Use at least twelve months of point-in-time historical data and 90 continuous days of forward paper execution. The forward clock starts only after the strategy and parameters are frozen.

Proceed to a live pilot only if:

- out-of-sample net annualized Sharpe ratio exceeds 1.5;
- Deflated Sharpe probability exceeds 95% after counting all related trials;
- maximum simulated drawdown is below 8%;
- results remain positive with doubled transaction costs;
- no modeled liquidation occurs in the full stress suite;
- 99th-percentile one-day loss is below 1% of total equity;
- profit remains positive after removing the best asset, best venue pair, and best month separately;
- profit is not dependent on rebates, incentives, token appreciation, or unsecured lending;
- at least two of BTC, ETH, and SOL contribute positive out-of-sample results;
- funding, fees, and balances reconcile exactly in the forward ledger;
- conservative net return exceeds both the approved cash benchmark and actual operational costs.

The current live funding snapshot is evidence that spreads exist, not evidence that this gate passes.

### 15.3 Live pilots

- Class G: maximum USD 250 total deployed capital.
- Class C: the lesser of USD 500 and 6.25% of equity in total capital and collateral.
- No risk limits increase until at least 100 fully reconciled Class G baskets or 90 live Class C calendar days, respectively.
- Each increase is at most 50% of the previous limit and requires a written review.
- Failure by one engine does not block research on the other, and success by one does not validate the other.

## 16. Rollout and Project Boundaries

This specification defines the complete research architecture but intentionally decomposes implementation:

1. **Foundation:** instrument registry, point-in-time recorder, fee model, ledger, experiment registry, and read-only venue adapters.
2. **Class C audit:** funding normalization, compatibility checks, historical replay, synchronized book collection, and paper positions for BTC/ETH/SOL.
3. **Class G audit:** rule compiler, payoff graph, executable-depth optimizer, and opportunity report.
4. **Independent risk and execution simulation:** failure states, reconciliation, and kill switches shared by both engines.
5. **Eligibility review:** jurisdiction, KYC, tax, sanctions, terms, custody, and venue approval.
6. **Single-engine pilot:** activate only whichever engine independently passes its gate first.
7. **Router activation:** allocate between engines only after both have independent live evidence.
8. **Class S review:** consider a separate design only after the barbell has stable attribution.

The first implementation plan must cover only Step 1 and the read-only portion of Step 2. It must not include live credentials, deposits, or order submission.

The first AI implementation plan is separately limited to gold-dataset construction, the model registry, and offline semantic-scanner evaluation. It may share read-only schemas with Step 1 but must not expand the live-trading scope.

## 17. Success and Stop Conditions

Research succeeds when it establishes, with point-in-time executable evidence, that at least one engine has positive conservative economics and passes its independent validation gate.

Research stops or narrows when:

- opportunities disappear after fees and synchronized depth;
- return depends on one venue, token, month, reward program, or parameter choice;
- required capital fragmentation makes the strategy inferior to cash;
- failure-domain loss dominates expected annual return;
- data quality cannot support exact reconstruction;
- legal or venue eligibility prevents compliant execution;
- operational burden is unreasonable for expected dollar profit.

Thresholds must not be relaxed merely because a strategy fails.

## 18. Primary Evidence and Dated Observations

The architecture is based on the following findings:

- Crypto carry can become very large and reflects leveraged trend-chasing demand plus scarcity of arbitrage capital; high carry can also precede crashes. This supports Class C while requiring regime and deleveraging controls.
- Hyperliquid funding is peer-to-peer, calculated from a premium and fixed interest component, paid hourly, and exposed through official historical and predicted-funding endpoints. Funding mechanisms and intervals differ across venues and must be normalized.
- A public predicted-funding snapshot collected on 2026-08-12 showed approximate raw annualized cross-venue spreads of 5.1% for BTC, 3.8% for ETH, and 9.3% for SOL if the instantaneous rates persisted. These are non-executable, pre-cost point estimates and do not satisfy the Class C gate.
- Automated market makers expose liquidity providers to loss-versus-rebalancing when informed arbitrageurs trade against stale AMM prices. LP fees are therefore not treated as free yield.
- DeFi liquidation price effects depend on the mechanism and participation cost. Liquidation data is used as a risk feature rather than a standalone edge.
- Crypto returns exhibit momentum and attention effects, but more realistic studies emphasize liquidation, skew, and fat-tail problems. Directional momentum is not part of the initial live system.
- Latency arbitrage profits are concentrated among technically dominant firms. The project does not compete in microsecond races.
- Backtest selection and non-normal returns inflate conventional Sharpe ratios. Every experiment and trial family is registered and evaluated with selection-bias correction.
- Prediction-market research documents structural arbitrage, but current fees, non-atomic execution, semantic differences, and boundary rules can erase apparent profits. Class G requires current executable books and deterministic rule proof.

Sources:

- https://www.bis.org/publ/work1087.htm
- https://hyperliquid.gitbook.io/hyperliquid-docs/trading/funding
- https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals
- https://hyperliquid.gitbook.io/hyperliquid-docs/trading/contract-specifications
- https://hyperliquid.gitbook.io/hyperliquid-docs/trading/robust-price-indices
- https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees
- https://arxiv.org/abs/2208.06046
- https://www.bankofcanada.ca/2025/03/staff-working-paper-2025-12/
- https://www.bankofcanada.ca/2026/04/staff-analytical-paper-2026-13/
- https://academic.oup.com/rfs/article-abstract/34/6/2689/5912024
- https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4675565
- https://www.nber.org/papers/w29011
- https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551
- https://arxiv.org/abs/2501.17335
- https://arxiv.org/abs/2508.03474
- https://arxiv.org/abs/2607.26245
- https://papers.ssrn.com/sol3/papers.cfm?abstract_id=6748186
- https://docs.polymarket.com/api-reference/introduction
- https://docs.polymarket.com/trading/orderbook
- https://docs.polymarket.com/api-reference/trade/post-multiple-orders
- https://docs.polymarket.com/market-makers/maker-rebates
- https://docs.polymarket.com/concepts/resolution
