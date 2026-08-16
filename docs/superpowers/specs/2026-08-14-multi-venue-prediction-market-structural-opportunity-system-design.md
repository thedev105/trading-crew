# Multi-Venue Prediction-Market Structural Opportunity System

**Date:** 2026-08-14  
**Status:** Direction approved; written specification awaiting user review  
**Scope:** Shared prediction-market research infrastructure, committed Polymarket and Kalshi
adapters, a conditional Limitless adapter, deterministic structural-opportunity proofs, replay,
shadow execution, and separately gated venue-specific live pilots  
**Authority boundary:** No live credentials, wallet funding, order submission, or geographic
circumvention is authorized by this specification

Companion specifications:

- [Market-Neutral Opportunity Router Design](2026-08-12-market-neutral-opportunity-router-design.md)
- [AI Augmentation Design](2026-08-12-ai-augmentation-design.md)
- [Public Corpus Acquisition Design](2026-08-12-public-corpus-acquisition-design.md)
- [Local Operator Dashboard Design](2026-08-13-local-operator-dashboard-design.md)

## 1. Decision

The active product direction is a shared prediction-market core with two committed venue adapters,
Polymarket and Kalshi, plus a conditional Limitless adapter. Limitless moves from conditional to
committed only after its source-use, API-access, account, and jurisdiction gates pass. The initial
strategy class remains deterministically provable structural opportunities in prediction-market
contracts. The system will collect public market rules and executable books, preserve exact
venue-specific semantics, build candidate relationships, prove minimum terminal payout across all
valid states, calculate conservative economics, replay non-atomic execution, and show the complete
evidence trail in the local dashboard.

Opinion, SX Bet, and ForecastEx/CME through Interactive Brokers are research-watchlist venues, not
version-1 execution commitments. Gemini Predictions, Crypto.com Predictions, Rothera, Betfair,
PredictIt, Manifold, and OKX Outcomes remain monitoring or exclusion candidates until official
evidence establishes a suitable public-data license, supported automated execution, compatible
contract mechanics, and a compliant account path.

The previous perpetual-futures and Lighter-dYdX work remains valid research infrastructure and is
retained in the repository, but it is paused as an active delivery track. It must not be silently
deleted, rewritten, or presented as Polymarket evidence.

The architecture intentionally supports more than one venue without pretending their contracts are
interchangeable. Venue adapters preserve raw identities, rules, fees, order semantics, collateral,
settlement, and access state. The shared core receives normalized facts only after lineage is
retained. This does not remove semantic risk, resolution risk, partial-fill risk, capital
fragmentation, transfer risk, market-access restrictions, or the need to prove that an apparent
price inconsistency remains profitable after all costs.

## 2. Objective and Success Definition

The system answers one narrow question:

> Does a set of currently executable outcome-contract orders, on one approved venue or across a
> proven-equivalent set of venues, have a deterministically proven minimum payout greater than its
> conservative all-in acquisition, fragmentation, settlement, and failure cost?

Research success means the system can identify, reproduce, and independently validate such
opportunities without look-ahead and without relying on a directional forecast. Product success
for the first code-complete release means an operator can:

1. collect and inspect public Polymarket and Kalshi markets, rule versions, fees, books, and trades;
2. inspect conditional Limitless evidence only when its source-use gate permits collection;
3. see why a candidate relationship was proposed;
4. inspect or reproduce the deterministic payoff and cross-venue equivalence proofs;
5. replay executable entry under latency, depth, transfer, and partial-fill stress;
6. run forward shadow execution without a wallet or funded trading account;
7. monitor per-venue coverage, failures, proof invalidations, capital fragmentation, and paper P&L
   in the web UI; and
8. obtain a fail-closed `INSUFFICIENT_EVIDENCE`, `REJECTED`, or `SHADOW_CANDIDATE` result.

The project does not promise an earning date, target return, or positive strategy result. A stable
rejection is a valid result when fees, liquidity, semantics, access, or risk erase the opportunity.

## 3. Scope and Non-Goals

### 3.1 Initial scope

- Shared venue-neutral domain contracts with exact raw venue evidence retained alongside every
  normalized record.
- Committed public-data adapters for Polymarket and Kalshi.
- A conditional public-data adapter for Limitless, disabled until source-use and eligibility review
  records permit it.
- Polymarket Gamma, Data, CLOB REST, CLOB WebSocket, and relevant public on-chain evidence.
- Kalshi public REST market data, historical partitions, and authenticated demo WebSocket evidence
  only when a non-funded demo account is separately configured.
- Public Limitless REST and WebSocket evidence only after its collection gate passes.
- Binary YES/NO markets and explicitly identified mutually exclusive multi-outcome events.
- Immutable rule and metadata versions.
- Full executable depth, not midpoint-only pricing.
- Deterministic payout proofs across enumerated terminal states.
- Point-in-time replay and forward shadow execution.
- Initial portfolio below USD 10,000.
- Read-only, loopback-only operator dashboard.
- AI-assisted candidate discovery with no capital or proof authority.
- Cross-venue candidate matching with deterministic contract-equivalence validation.
- Generalizing `src/polytrading/corpus_intake/source_policy.py`'s `IntendedUseScope`,
  `SourceEvidence`, `SourceUseAssessment`, and `SourceUseApproval` records beyond their current
  `source: Literal["polymarket"]` typing to the full venue set, so the existing corpus-intake
  source-use gate and the new venue manifest (section 6.1) share one venue-identity vocabulary
  instead of diverging.

### 3.2 Explicit non-goals for version 1

- Copying top wallets or treating a leaderboard as alpha.
- Directional betting based on news, sentiment, an LLM, or a probability forecast.
- Latency races, mempool exploitation, oracle manipulation, or deceptive trading.
- Unbounded market making or inventory accumulation.
- VPN, proxy, remote-server, entity, or custodial arrangements intended to bypass geographic
  restrictions.
- Live authentication, deposits, bridging, approvals, signing, orders, cancellations, merging,
  conversion, redemption, or withdrawal before a separate eligibility and execution gate.
- Treating rewards, points, rebates, or token appreciation as required strategy profit.
- Treating a broker or consumer frontend as a separate source of liquidity when it routes to the
  same underlying exchange.
- Implementing Opinion, SX Bet, ForecastEx/CME, Gemini Predictions, Crypto.com Predictions,
  Rothera, Betfair, PredictIt, Manifold, or OKX Outcomes in version 1.
- Moving collateral between venues automatically or assuming transfers are instantaneous.

## 4. Strategy Hierarchy

Version 1 prioritizes mechanisms whose payoff can be proved without forecasting.

### 4.1 Engine A: binary complement baskets

For one binary condition, YES and NO form a complete terminal partition only when the exact
contract, collateral, resolution, dispute, invalidation, and redemption rules establish it. The
engine may propose acquiring both sides when their depth-weighted all-in cost is below the proven
minimum redeemable value after fees, gas, operational reserves, capital lockup, and execution-risk
reserves. The first implementation evaluates complements within one venue; it never combines two
venues merely because both display YES and NO labels.

The engine does not assume that displayed best asks are simultaneously fillable. It walks both
books, applies a non-atomic sequence, and treats any first-leg fill as temporary directional
exposure until completion or unwind.

### 4.2 Engine B: mutually exclusive multi-outcome baskets

For an event with exactly one winning outcome, the engine may evaluate a basket across every
exhaustive outcome. Polymarket or Limitless negative-risk relationships may reduce capital or
enable conversion, but only when the exact venue, event, and market metadata identify the
supported relationship and the contract behavior is independently verified. Kalshi multivariate
or event-group mechanics are modeled from Kalshi's own rules rather than mapped onto a
negative-risk token abstraction.

Missing `Other`, tie, cancellation, substitution, disqualification, or invalid-resolution states
make the outcome set non-exhaustive unless the rules explicitly account for them. An outcome list
that merely looks complete is not proof.

### 4.3 Engine C: logical implication baskets

The semantic scout may propose implications such as nested thresholds, deadlines, or scopes. If
proposition `A` deterministically implies proposition `B` under every allowed resolution state,
the proof compiler can evaluate a basket such as `NO(A) + YES(B)`, whose minimum payout is at least
one unit only when the implication is valid under the exact rules.

Examples include successively broader thresholds, earlier/later deadlines, and subset/superset
event definitions. Differences in oracle, timezone, inclusivity, rounding, cancellation, or
resolution source invalidate the proof unless explicitly modeled.

### 4.4 Engine D: cross-venue equivalent-contract baskets

The semantic scout may propose that contracts on different venues encode the same terminal
proposition. A proposal becomes proof-ready only when a deterministic equivalence compiler shows
that every valid real-world state produces compatible payouts after comparing:

- exact proposition and threshold inclusivity;
- observation period, deadline, timezone, and publication or revision policy;
- primary resolution source and fallback sources;
- postponement, cancellation, void, dispute, correction, and invalid-market behavior;
- outcome completeness and any non-binary payout state;
- contract denomination, collateral, payout unit, and rounding;
- settlement finality, timing, redemption, and withdrawal constraints; and
- venue-specific position, account, access, and custody rules relevant to realizing the payout.

Title similarity, shared keywords, or matching displayed probabilities are never sufficient. AI
can nominate a pair, but deterministic software must either produce an exhaustive equivalence
artifact or reject it. A valid cross-venue proof still receives reserves for non-atomic fills,
separate collateral, inability to transfer funds during entry, different settlement times, and
independent venue or account failure.

### 4.5 Deferred engines

- **Passive market making:** deferred until the system has queue-aware fill, adverse-selection,
  inventory, cancellation, and resolution-risk evidence. It is not a guaranteed-payoff engine.
- **Directional AI forecasting:** remains research-only Class S. It cannot fund or validate the
  initial business case.
- **Wallet-copying:** rejected as a primary strategy because public fills do not reveal private
  information, full intent, hedges elsewhere, or reproducible future advantage.
- **Statistical cross-venue convergence:** deferred because similar contracts that are not
  deterministically equivalent create directional basis risk.

## 5. Governing Principle for AI

The existing principle remains binding:

> AI scouts and extracts; deterministic software proves, prices, constrains, and eventually
> executes.

AI may:

- retrieve semantically similar or potentially related markets;
- extract typed propositions and critical rule fields with exact source spans;
- propose complements, exhaustive sets, implications, cross-venue matches, and hard negative
  examples;
- identify possible ambiguity, changed rules, or missing terminal states; and
- prioritize manual review.

AI may not:

- declare a guaranteed payoff;
- decide that an outcome set is exhaustive;
- supply a missing rule, oracle, deadline, or exception from general knowledge;
- calculate binding fees, depth, payout, size, or risk;
- approve a proposal or relax a threshold; or
- receive wallet, API, signing, deployment, or withdrawal authority.

Every model artifact records the source hashes, cutoff, exact model and prompt versions,
uncertainty, and abstention state. Unsupported fields are `unknown`, never inferred defaults.

This system reuses the existing semantic scout rather than building a new one. That scout has a
known, pre-existing gap this specification does not silently inherit: the AI Augmentation Design
requires critical-field exact-match of at least 99.5% before read-only production use, but the
shipped threshold in `src/polytrading/ai/evaluate.py` is 95%, and it has never been measured against
real adjudicated-gold data (only against a synthetic, intentionally unresolved fixture corpus). This
gap must be closed — either by raising the shipped threshold and validating against a genuine gold
corpus, or by an explicit, separately reviewed decision to lower the approved gate — before the
scout's output is trusted for this system's candidate discovery, not carried forward unresolved.

## 6. Evidence Model

### 6.1 Venue manifest and source-use gate

Every venue has a versioned manifest containing:

- canonical venue and underlying-exchange identity;
- official API, WebSocket, rulebook, terms, fee, and eligibility sources;
- public, authenticated-demo, and authenticated-live capabilities;
- data retention, automated-use, commercial-use, redistribution, and model-training status;
- supported order, position, collateral, settlement, redemption, and withdrawal semantics;
- current jurisdiction and account-review status;
- adapter implementation state: `WATCHLIST`, `READ_ONLY`, `SHADOW`, `LIVE_DISABLED`, or
  `LIVE_ELIGIBLE`;
- exact source hashes, review identity, review date, and invalidation conditions; and
- whether the venue is independent liquidity or a broker/frontend for another exchange.

Collection fails closed unless the manifest permits the requested source and purpose. Execution
fails closed unless a separate live manifest version permits the specific venue, account, operator,
location, credential scope, and strategy. A frontend routed to Kalshi, ForecastEx, or Rothera is
not counted as an independent venue without evidence of independent liquidity and settlement.

The repository already has a partial source-use gate for corpus intake
(`src/polytrading/corpus_intake/source_policy.py`), whose `IntendedUseScope`, `SourceEvidence`,
`SourceUseAssessment`, and `SourceUseApproval` records use a three-state `review_required` /
`approved` / `rejected` vocabulary and are typed `source: Literal["polymarket"]`. The approved
Public Corpus Acquisition Design already anticipated widening this exact mechanism to Kalshi. This
venue manifest supersedes that mechanism for venue-level collection and execution gating: the
`Literal["polymarket"]` types generalize to the full venue set as explicit increment-1 scope (see
section 3.1), and the existing three-state vocabulary maps onto this manifest's `WATCHLIST` /
`READ_ONLY` state pair rather than being run alongside it as a second, independent gate. The
corpus-intake source-use gate remains the review path specifically for semantic-scout corpus text
provenance (Public Corpus Acquisition Design); it is not replaced, only no longer the sole gate for
venue-level collection.

### 6.2 Market and rule registry

Every tradable condition has a point-in-time record containing:

- venue, underlying exchange, event, condition, question, contract, token, and ticker identifiers
  as applicable;
- exact question, description, resolution source, and rule text;
- outcomes in source order;
- active, closed, restricted, order-book-enabled, and negative-risk flags;
- start, end, resolution, and update timestamps;
- collateral, payout, conversion, and redemption identifiers when documented;
- source URL, retrieval time, information cutoff, exact raw hash, and normalized record hash; and
- deterministic warnings for missing or inconsistent fields.

The normalized record is deliberately lossy only for query convenience. Binding proof and
execution always retain and reference the raw venue record, adapter version, and venue manifest.
Unsupported fields remain `unknown`; venue-specific mechanics are never silently replaced with a
Polymarket-like default.

The `negative-risk` flag is a Polymarket- and Limitless-specific field; it is always `unknown` (not
`false`) for Kalshi, whose own multivariate and event-group mechanics (section 4.2) are represented
in a separate, Kalshi-specific extension of this record rather than mapped onto the negative-risk
flag. A shared field existing for query convenience must never become the only place Kalshi's
grouping semantics could be recorded.

A changed question, description, resolution source, outcome set, market grouping, or critical flag
creates a new immutable version. The old version remains queryable at historical cutoffs.

### 6.3 Executable market evidence

The collector stores:

- initial full order-book snapshots and subsequent price-level changes;
- exchange timestamps, hashes, local receipt times, reconnects, and sequence or continuity gaps;
- best bid/ask and at least the depth needed by every evaluated basket;
- public trades and their lifecycle evidence where available;
- token-specific fee rates and the time they were observed;
- tick sizes, minimum order sizes, and relevant market status changes; and
- resolution, dispute, settlement, conversion, and redemption evidence.

Kalshi live and historical partitions are joined at the official cutoff without duplicating or
dropping records. Polymarket and Limitless book reconstruction follows each venue's own continuity
and snapshot semantics. An adapter cannot claim a common continuity guarantee stronger than the
official source provides.

WebSocket state is periodically reconciled against an independent REST book snapshot. A gap,
reconnect without a fresh snapshot, hash inconsistency, or out-of-order update invalidates the
affected interval rather than carrying the prior book forward.

### 6.4 Candidate relationship artifact

Each proposed relationship contains:

- candidate and trial-family identifiers;
- relationship type and participating venue/market/contract/token IDs;
- point-in-time cutoff and all rule-version hashes;
- AI or deterministic discovery provenance;
- typed propositions and exact supporting source spans;
- unresolved fields, contradictions, and invalidation conditions;
- review status; and
- disposition: quarantined, rejected, proof-ready, or superseded.

Candidate discovery is not an opportunity and never reaches execution directly.

### 6.5 Deterministic proof artifact

The payoff compiler enumerates all valid terminal states and records:

- one payout vector per state;
- proof template and compiler version;
- minimum and maximum basket payout;
- assumptions and their exact rule support;
- excluded states and why exclusion is valid;
- complete source-hash lineage;
- manual-review identity when a new template is introduced; and
- invalidation rules for metadata or contract changes.

For cross-venue candidates, the proof also contains an equivalence matrix for every field listed in
Engine D, the independent venue-manifest hashes, settlement-timing bounds, and a complete mapping
from real-world states to each venue's terminal payout states. `unknown` in any binding equivalence
dimension rejects the proof.

Unknown, contradictory, or unmodeled states fail the proof. Novel proof templates require manual
approval and exhaustive tests before they can emit `proof_ready`.

## 7. Conservative Opportunity Economics

For a proposed basket at quantity `q`:

```text
proven_floor_usd
  = minimum terminal payout across every valid state

all_in_cost_usd
  = depth-walked leg acquisition cost
  + venue- and contract-specific trading fees
  + gas / conversion / redemption reserve
  + currency conversion and stablecoin basis reserve
  + deposit, withdrawal, and transfer cost
  + capital-lockup benchmark
  + ordinary operational cost

failure_reserve_usd
  = conservative partial-fill completion or unwind loss
  + latency and stale-quote reserve
  + dispute / delayed-resolution reserve where measurable
  + venue, account, custody, and settlement-divergence reserve

conservative_surplus_usd
  = proven_floor_usd - all_in_cost_usd - failure_reserve_usd
```

The report also shows return on assigned capital, return on total account equity, collateral
stranded per venue, maximum capital lock time, capacity at current depth, and results under doubled
costs. Capital is assumed pre-positioned before a cross-venue opportunity; no opportunity may rely
on completing a just-in-time transfer. Favorable maker rebates, points, incentives, and unproven
early conversion receive no required-profit credit.

Displayed-price inconsistencies remain diagnostics. A `SHADOW_CANDIDATE` requires a valid proof,
simultaneously known evidence, positive conservative surplus, frozen policy thresholds, and no
binding risk or access blocker.

The existing Class G validation thresholds remain the activation gate for each strategy and venue
combination, not just for the aggregated portfolio:

- at least 45 continuous days of synchronized rules and executable books;
- at least 25 opportunities surviving current fees, executable depth, and one-second latency;
- at least 10 opportunities surviving five-second latency;
- median conservative net surplus of at least 0.75%;
- median capacity of at least USD 100;
- projected annual contribution of at least 2% of total equity;
- conservative return on assigned capital exceeds the approved cash benchmark by 5 percentage
  points;
- zero false guaranteed-payoff claims in manual review;
- simulated 99th-percentile incomplete-leg loss below 0.25% of equity; and
- simulated drawdown below 8%.

Thresholds are preregistered and cannot be relaxed because observed opportunities are weak.
Polymarket-only, Kalshi-only, Limitless-only, and each cross-venue pair receive separate evidence
reports. Strong results in one combination cannot authorize another.

## 8. Execution Replay and Shadow State Machine

No multi-leg atomicity is assumed. Replay and shadow execution use the same deterministic states:

```text
DISCOVERED
  -> PROOF_VALIDATED
  -> ECONOMICS_VALIDATED
  -> SHADOW_PLANNED
  -> FIRST_LEG_SIMULATED
  -> COMPLETE | UNWOUND | EXPIRED | UNKNOWN
  -> RECONCILED
```

Before the first simulated submission, the planner freezes:

- venue-qualified leg order and bottleneck leg;
- maximum quantity and per-level limit prices;
- venue-specific maker/taker or protected immediate-order policy;
- expiration time;
- completion, cancellation, and unwind paths;
- maximum incomplete exposure and loss;
- current venue-manifest, eligibility, rule, fee, book, policy, and code hashes; and
- proposal-specific kill conditions.

After any partial fill, the engine re-reads the remaining executable books and recomputes the proof
economics. It never averages into a broken basket or assumes an unconfirmed cancellation. Unknown
order state halts new simulated entries and requires reconciliation.

The shadow ledger records intended orders, venue-specific simulated acknowledgements, partial
fills, fees, collateral occupancy, conversion/redemption assumptions, resolution, realized payout,
opportunity cost, and unexplained differences. A paper result is invalid until every participating
venue reconciles completely.

## 9. Risk and Capital Policy

The inherited sub-USD-10,000 policy remains conservative:

- external safety reserve remains outside the trading venue;
- at most two venues may hold live strategy capital during the initial pilot;
- venue allocation is explicit and cannot be swept automatically during an opportunity;
- one proved basket: at most 5% of total equity;
- one real-world event cluster: at most 10% of total equity;
- initial live Class G pilot: at most USD 250 total deployed capital across all venues;
- incomplete-leg modeled loss: below 0.25% of total equity;
- no leverage, borrowing, martingale sizing, naked short exposure, or cross-strategy margin;
- no position whose worst valid terminal state is unknown; and
- cash is always an acceptable outcome.

Drawdown controls remain stricter than the user's 30% catastrophe tolerance:

- 2% loss in 24 hours: halt new entries and reconcile;
- 5% peak-to-trough: halve permitted new size after review;
- 8%: stop all new trading and require manual investigation;
- 12%: close non-guaranteed exposure through proposal-specific safe exits; and
- 15%: capital-preservation mode requiring a new approval before resumption.

Thirty percent is a catastrophe boundary, not a normal stop or design target.

## 10. Geographic, Legal, and Account Gate

As of this specification's date:

- Polymarket's official geographic-restriction documentation lists Germany as blocked for order
  placement. Estonia is not listed as blocked.
- Kalshi's June 17, 2026 Member Agreement does not list Germany or Estonia among its restricted
  jurisdictions, but Kalshi requires identity verification and retains account-approval discretion.
- Limitless's June 19, 2026 Terms of Service do not list Germany or Estonia among its location-based
  exclusions, but they require the user to determine compliance with local law and permit identity
  or eligibility checks.

These are time-sensitive source observations, not legal advice, account approval, or a conclusion
that a German individual, Estonian company, custodian, or server may trade. Eligibility is evaluated
per venue, account holder, beneficial owner, physical operator location, entity, credential, and
order time. Approval on one venue never authorizes another.

The system must:

- remain public-data-only for a venue while the operator or account lacks a current affirmative
  live-eligibility record;
- call a venue's official geoblock or eligibility endpoint, when available, before future
  authentication and again before every future order sequence;
- fail closed when eligibility evidence is unavailable, stale, contradictory, or reports blocked
  access;
- store the eligibility response and applicable terms/rule hashes with the proposal;
- prohibit VPNs, proxies, remote hosts, custodial structures, or entities used to disguise the
  actual operating location or bypass a restriction; and
- require documented review of residency, citizenship, physical operation, entity, KYC, custody,
  sanctions, tax, local prediction-market law, and venue terms before live activation.

A server or custodian in Estonia does not by itself establish that a German operator is eligible.
The research and shadow system can be built and tested without resolving this gate; live orders
cannot.

Primary current references:

- https://docs.polymarket.com/api-reference/geoblock
- https://help.polymarket.com/en/articles/13364163-geographic-restrictions
- https://docs.polymarket.com/api-reference/introduction
- https://help.kalshi.com/en/articles/14026044-can-i-trade-on-kalshi-from-outside-the-united-states
- https://kalshi.com/docs/kalshi-member-agreement.pdf
- https://help.kalshi.com/en/articles/15581140-document-verification-on-kalshi
- https://docs.limitless.exchange/user-guide/terms-of-service

## 11. Architecture and Component Boundaries

### 11.1 Venue adapter layer and public collectors

Each venue adapter owns transport, pagination, identifiers, raw schemas, continuity semantics,
rate limits, and venue-specific normalization. A narrow shared interface exposes capabilities,
market/rule versions, executable books, trades, fees, status, and settlement evidence without
erasing raw lineage. Polymarket and Kalshi are separate packages; the conditional Limitless package
cannot collect until its manifest permits the intended use. Public collectors have no signer or
live credentials. Optional Kalshi demo credentials are isolated from production and cannot fund or
authorize live execution.

### 11.2 Contract registry

Owns point-in-time venue and underlying-exchange identity, market identity, rule versions, outcome
sets, venue-native grouping, and critical metadata. It exposes typed read-only queries and never
performs semantic inference.

### 11.3 Semantic scout

Consumes only approved, sanitized evidence and emits quarantined candidate relationships. It is
asynchronous and offline from the execution-critical path.

### 11.4 Equivalence and payoff compilers

The equivalence compiler validates cross-venue proposition and settlement compatibility. The payoff
compiler converts an approved typed relationship into an exhaustive state table and deterministic
payout proof. Both are pure deterministic libraries with no network, model, clock, account, or
storage-write dependency.

### 11.5 Executable economics engine

Joins one proof with point-in-time books, fees, timing, policy, and reserves. It walks depth and
sizes the basket without inferring fills. Incomplete evidence yields a typed insufficient result.

### 11.6 Replay and shadow engine

Processes event-time evidence, simulates non-atomic orders, maintains the proposal state machine,
and writes a double-entry shadow ledger. It cannot call authenticated endpoints.

### 11.7 Independent risk engine

Validates capital, concentration, incomplete exposure, drawdown, data health, and proposal state
independently of strategy code. Strategy output cannot increase limits.

### 11.8 Operator dashboard

Opens the database read-only and presents one captured point-in-time snapshot. It does not run
collectors, mutate policies, hold secrets, or submit orders.

### 11.9 Future execution adapters

Deferred behind evidence and eligibility gates. If later approved, it will own minimum-scope
venue-specific authentication, signing, order placement, cancellation, user-stream consumption,
and exact reconciliation. Every live venue is a separately enabled package and process boundary.
No execution adapter has an AI dependency or withdrawal authority where venue controls permit that
restriction. A shared coordinator may sequence approved legs but cannot broaden venue permissions
or translate one venue's success into another venue's order authority.

## 12. Data Flow

```text
official venue sources
  | Polymarket | Kalshi | conditional Limitless |
  +----------------------+-----------------------+
                         v
             venue manifest/source-use gate
                         v
          venue adapters + raw immutable recorder
                         v
       normalized registry + executable books/fees
                  +------+------+
                  |             |
                  v             v
       semantic candidates   venue health
                  v
     equivalence proof when cross-venue
                  v
        deterministic terminal-payoff proof
                  v
        conservative economics and sizing
                  v
         replay -> forward shadow ledger
                  v
         read-only dashboard and reports

                  X no live path in version 1
```

Every downstream artifact references immutable upstream identities and hashes. Later rule, book,
fee, model, or code records cannot leak into an earlier cutoff.

## 13. CLI and Dashboard Contract

The existing CLI already owns the top-level `replay` (raw fixture replay into a perpetual-futures
database, README section 3) and `dashboard --db --port` (schema-locked to the perpetual-futures and
carry system, Local Operator Dashboard Design section 8) commands. Neither can be reused for this
system's execution-replay or prediction-market dashboard without colliding: argparse rejects two
subparsers registered under the same name, and the existing dashboard fails closed on any database
whose schema it does not recognize rather than switching views. This system's commands are therefore
grouped under one new top-level `predictions` command, mirroring the existing `carry`/`trial`/`ai`
subcommand-group convention, so no existing name is reused or shadowed:

```text
polytrading predictions venues status --db var/prediction-markets.duckdb
polytrading predictions collect polymarket --db var/prediction-markets.duckdb
polytrading predictions collect kalshi --db var/prediction-markets.duckdb
polytrading predictions collect limitless --db var/prediction-markets.duckdb
polytrading predictions health --venue all --db var/prediction-markets.duckdb --as-of <UTC>
polytrading predictions candidates --venues polymarket,kalshi \
  --db var/prediction-markets.duckdb --as-of <UTC>
polytrading predictions prove --candidate-id <id> --db var/prediction-markets.duckdb
polytrading predictions scan --db var/prediction-markets.duckdb --as-of <UTC>
polytrading predictions shadow replay --run <manifest> --db var/prediction-markets.duckdb
polytrading predictions shadow run --policy <file> --db var/prediction-markets.duckdb
polytrading predictions dashboard --db var/prediction-markets.duckdb --port 8787
```

`collect` takes one venue per invocation as its own subcommand, matching the existing `collect
public|funding-cycle|books|corpus|source-use|review-queue` subcommand tree rather than a `--venue`
selector flag. `shadow replay` performs event-time replay of a recorded run; `shadow run` performs
forward paper execution against current evidence; both share the section 8 state machine and the
section 11.6 replay-and-shadow engine. The Limitless collection command returns a typed gate failure
until its venue manifest authorizes the intended source use. Listing a command does not imply that a
venue is enabled.

Exact names may be refined during implementation planning, but the boundaries are fixed: collect,
health, candidate discovery, proof, economics scan, replay, shadow, and dashboard remain separate
operations, and none of them reuse an existing top-level command name from the perpetual-futures CLI.
`polytrading predictions health` will coexist with the unrelated existing `funding health` and
`trial health` commands, and `predictions shadow` names the same kind of pre-live paper-execution
evidence that the existing Lighter-dYdX system calls `trial`. Both are distinct enough not to
collide, but implementation planning should pick one vocabulary for "pre-live evidence collection"
across the whole CLI rather than leaving `trial` and `shadow` as accidental synonyms.

The prediction-market dashboard shows:

- venue capability, source-use, account, geographic, and implementation state;
- per-venue collector and WebSocket continuity health;
- per-venue market, rule-version, book, trade, and fee coverage;
- unresolved source-use or semantic-review gates;
- candidate relationships with provenance and abstentions;
- deterministic equivalence and payoff-proof status with invalidation reasons;
- current executable capacity and conservative surplus;
- partial-fill, latency, settlement-divergence, and collateral-fragmentation stress results;
- forward shadow opportunities, ledger P&L, drawdown, and reconciliation;
- venue-specific geographic/legal status as `UNREVIEWED`, `BLOCKED`, or
  `ELIGIBILITY_REVIEWED`; and
- copyable research commands.

The UI never labels an opportunity `risk-free`, `guaranteed profit`, `approved to trade`, or
`live eligible`. It distinguishes mathematical payout proof from non-atomic execution risk.

## 14. Error Handling and Integrity

- Raw source data is persisted before normalized records.
- Disabled venue manifests reject collection or execution before any network request.
- Unsupported normalized fields remain `unknown`; adapters cannot synthesize venue semantics.
- Redirects, invalid UTF-8, malformed JSON, schema drift, hash conflicts, and identity conflicts
  fail closed.
- WebSocket heartbeat failure or continuity uncertainty requires a fresh REST snapshot.
- Stale or crossed books, invalid tick sizes, missing fee rates, or missing depth make economics
  unavailable.
- Rule changes invalidate dependent candidates and proofs until recompiled.
- Venue-term, fee, eligibility, API, or underlying-exchange changes invalidate the affected venue
  manifest and dependent artifacts.
- AI failures quarantine candidates; they never manufacture proof fields.
- Unknown terminal states invalidate a proof.
- Unknown order state invalidates paper P&L until reconciliation.
- Cancellation propagates immediately through asynchronous collectors.
- Retry budgets are bounded and never convert a late observation into earlier evidence.
- Error records use sanitized stable codes; secrets and untrusted source bodies do not enter logs.
- Append-only identities are idempotent only for byte-equivalent canonical content; conflicts are
  rejected.

## 15. Testing Strategy

### 15.1 Source and storage tests

- Adapter capability and contract tests proving that venue-specific unknowns are not defaulted.
- Polymarket Gamma pagination, CLOB REST, WebSocket snapshots and deltas, trades, fees, negative
  risk, and market status.
- Kalshi public REST pagination, reciprocal YES/NO book representation, market status, trades,
  fees, and exact live/historical cutoff joining.
- Conditional Limitless public REST, WebSocket, CLOB/AMM distinction, negative-risk grouping,
  split-resolution evidence, and source-use rejection when its manifest is disabled.
- Reconnect, heartbeat, malformed frame, duplicate update, out-of-order update, hash mismatch, and
  venue-specific REST reconciliation.
- Rule-version changes, identity conflicts, raw-first lineage, transaction rollback, and
  point-in-time cutoff selection.

### 15.2 Proof tests

- Exhaustive binary complement states.
- Exhaustive multi-outcome and negative-risk states.
- Implication, subset, nested threshold, and deadline templates.
- Cross-venue equivalence matrices with exhaustive compatible terminal-state mappings.
- Mutation tests for oracle, timezone, boundary inclusivity, rounding, cancellation, postponement,
  fallback, tie, disqualification, `Other`, and non-exhaustive outcome sets.
- Cross-venue hard negatives for different underlying exchanges, resolution sources, revision
  policies, void rules, currencies, payout timing, and consumer frontends sharing one exchange.
- Hard negative examples where titles appear related but one critical rule differs.
- Proof invariance to input order and deterministic canonical output.

### 15.3 Economics and execution tests

- Multi-level depth walks and incompatible quantity increments.
- Venue- and contract-specific fees, gas, currency basis, transfers, capital lockup, doubled costs,
  and zero-reward economics.
- One-second and five-second latency.
- First-leg partial fill, second-leg rejection, cancellation uncertainty, completion, unwind, and
  expiration.
- No multi-leg atomicity assumption.
- No just-in-time collateral-transfer assumption and explicit stranded capital per venue.
- Independent venue outage, account restriction, settlement delay, and stablecoin-depeg stress.
- Capacity, concentration, incomplete-loss, and drawdown gates.
- Ledger conservation and exact reconciliation after every terminal state.

### 15.4 Research-integrity tests

- No future rule, fee, book, resolution, model, or outcome leakage.
- Preregistered trial families and inclusion of failed experiments.
- Event-family separation between training, validation, and test artifacts.
- Profit concentration by market, event, category, time, proof template, and best opportunity.
- Profit concentration by venue and venue pair, including results with each venue removed.
- Results without rewards and under doubled costs.

### 15.5 UI and package tests

- Stable three-state research decisions and explicit missing evidence.
- Dashboard cutoff consistency and no mutation endpoints.
- Browser rendering for empty, collecting, rejected, shadow-candidate, conditional-venue-disabled,
  and venue-specific blocked-access states.
- Full suite, lint, formatting, wheel build, fresh installation, and CLI smoke tests.

## 16. Delivery Sequence and Estimate

The multi-venue codebase is decomposed into independently testable increments:

1. **Shared core plus committed public evidence, 2-3 weeks:** venue manifests, adapter contracts,
   normalized registry, Polymarket and Kalshi public market/rule/books/trades/fees, continuity
   health, storage, and dashboard coverage.
2. **Conditional venue and semantic relationships, 1-2 weeks:** Limitless source-use gate and
   read-only adapter when permitted, shared proposition schema, candidate provenance, and hard
   negatives.
3. **Deterministic structural engine, 2-3 weeks:** complement and multi-outcome proofs,
   implication templates, cross-venue equivalence compiler, depth-aware economics, capital
   fragmentation, and reports.
4. **Replay and shadow engine, 2-3 weeks:** non-atomic multi-venue state machine, latency,
   partial-fill and venue-failure stress, ledger, experiment registry, and paper dashboard.
5. **Execution hardening, 2-4 weeks per separately approved venue:** minimum-scope authenticated
   adapter, restricted signer, order lifecycle, reconciliation, eligibility enforcement, and kill
   switch. Cross-venue live coordination is not enabled until both individual venue adapters pass
   their own live gates.

The first implementation plan must cover only increment 1. Each later increment requires its own
plan and verification checkpoint; approval of this architecture does not collapse all five
increments into one code change or authorize increment 5.

This yields a testable Polymarket-and-Kalshi research UI in approximately three to five focused
development weeks and a technically live-capable single-venue codebase in approximately ten to
sixteen weeks. Each additional approved live venue adds approximately two to four focused weeks.
These are engineering estimates, not evidence, account-access, launch-date, or profit guarantees.

The forward activation clock is separate:

- collect at least 45 continuous days of synchronized rules and executable books;
- pass the fixed Class G evidence thresholds;
- run at least 30 additional days of queue-aware shadow execution;
- demonstrate positive net results without rewards, no risk breach, and complete reconciliation;
- complete eligibility, legal, KYC, custody, tax, and venue-terms review; and
- only then consider the maximum USD 250 live pilot under a new approval.

## 17. Acceptance Criteria for the Testable Release

The first codebase is ready for operator testing when:

1. A clean installation can collect permitted public Polymarket and Kalshi evidence without a
   wallet or funded trading account. Retention and intended-use status remains explicit, and
   unapproved corpus text cannot be promoted into the semantic gold dataset.
2. The Limitless adapter is disabled by default and either returns a typed source-use gate failure
   or collects only under an affirmative versioned manifest.
3. Each adapter survives its documented reconnect and continuity cases and makes uncertainty
   visible without claiming stronger guarantees than the venue provides.
4. Historical dashboard snapshots cannot see later evidence, including data crossing Kalshi's
   live/historical partition.
5. At least the complement, exhaustive-outcome, and cross-venue equivalence templates generate
   exhaustive deterministic proof artifacts from fixtures and reject mutated near-matches.
6. The scanner uses synchronized executable depth and venue-specific current fees rather than
   displayed midpoint arithmetic.
7. Replay exposes partial fills, capital fragmentation, settlement timing, and non-atomic losses
   rather than assuming simultaneous fills or transfers.
8. Shadow operation records every proposal, rejection, venue-qualified simulated fill, failure,
   collateral allocation, and reconciliation.
9. AI output cannot create or approve an equivalence proof, payoff proof, or proposal.
10. Blocked, stale, or unresolved venue-specific geographic/account status cannot reach an
    authenticated execution state.
11. The dashboard clearly separates venue health, collection permission, proof validity,
    conservative economics, shadow results, and live ineligibility.
12. The complete test, lint, formatting, packaging, installation, and browser verification gates
    pass.

## 18. Stop and Pivot Conditions

The multi-venue structural strategy or an individual venue combination is rejected, paused, or
narrowed when:

- fewer than the required opportunities survive fees, synchronized depth, and latency;
- proof validity depends on ambiguous, changing, or unreviewable rules;
- non-atomic incomplete-leg loss dominates the conservative surplus;
- capacity is too small to exceed cash and operational costs for a sub-USD-10,000 portfolio;
- results depend on one market, event, category, reward program, or exceptional period;
- public evidence cannot reconstruct executable conditions reliably;
- terms or geographic eligibility prevent compliant operation;
- capital fragmentation or transfer friction removes the expected contribution;
- one adapter cannot preserve sufficient raw rules, fee, order-book, or settlement evidence;
- reconciliation is incomplete; or
- operational burden is unreasonable for the expected dollar contribution.

Failure of the strategy does not justify adding directional AI, copying wallets, weakening proof
requirements, or bypassing access controls. Any pivot receives a new trial-family identifier and a
new approved specification.

## 19. Current Official Technical References

### 19.1 Committed venue: Polymarket

- Public market data: https://docs.polymarket.com/market-data/overview
- CLOB WebSocket market channel:
  https://docs.polymarket.com/market-data/websocket/market-channel
- API architecture and authentication:
  https://docs.polymarket.com/api-reference/introduction
- Token-specific fee rate: https://docs.polymarket.com/api-reference/market-data/get-fee-rate
- Negative-risk markets: https://docs.polymarket.com/advanced/neg-risk
- Geographic restrictions: https://docs.polymarket.com/api-reference/geoblock
- Official clients and SDKs: https://docs.polymarket.com/api-reference/clients-sdks

### 19.2 Committed venue: Kalshi

- Public market data: https://docs.kalshi.com/getting_started/quick_start_market_data
- Historical data partitions: https://docs.kalshi.com/getting_started/historical_data
- Production and demo environments: https://docs.kalshi.com/getting_started/api_environments
- Authenticated request quick start:
  https://docs.kalshi.com/getting_started/making_your_first_request
- Order creation: https://docs.kalshi.com/api-reference/orders/create-order-v2
- International access:
  https://help.kalshi.com/en/articles/14026044-can-i-trade-on-kalshi-from-outside-the-united-states
- Member Agreement: https://kalshi.com/docs/kalshi-member-agreement.pdf
- Identity verification:
  https://help.kalshi.com/en/articles/15581140-document-verification-on-kalshi

### 19.3 Conditional venue: Limitless

- Developer documentation: https://docs.limitless.exchange/
- Programmatic API: https://docs.limitless.exchange/developers/programmatic-api
- WebSocket events: https://docs.limitless.exchange/developers/websocket-events
- Polymarket migration guide:
  https://docs.limitless.exchange/developers/migrate-from-polymarket
- Terms of Service: https://docs.limitless.exchange/user-guide/terms-of-service
- Changelog: https://docs.limitless.exchange/changelog

### 19.4 Research watchlist

- Opinion CLOB SDK:
  https://docs.opinion.trade/developer-guide/opinion-clob-python-sdk/overview
- SX Bet Developer Hub: https://docs.sx.bet/developers/introduction
- IBKR Event Contracts: https://www.interactivebrokers.com/campus/ibkr-api-page/event-contracts/
- Gemini Predictions API: https://developer.gemini.com/prediction-markets-spec
- Crypto.com Predictions data API: https://data-api.crypto.com/docs
- Rothera event contracts: https://www.rothera.io/event-contracts
- OKX Outcomes API: https://www.okx.com/docs-v5/outcomes_en/

These pages and agreements are mutable evidence, not timeless constants. Binding collection and
future execution must capture point-in-time versions or hashes rather than relying on this
reference list alone. Watchlist inclusion does not authorize collection, implementation, account
creation, or execution.
