# Contract Compatibility Dossier Design

**Date:** 2026-08-13

**Status:** autonomously approved under the operator's standing instruction to continue and
self-review routine research milestones

**Scope:** read-only research evidence; no trading authority

## 1. Decision

Add a versioned, source-backed contract-compatibility dossier for Hyperliquid and dYdX core
BTC, ETH, and SOL perpetuals. A deterministic evaluator will summarize the dossier in the CLI and
the local dashboard.

The first dossier must return `ineligible`. Hyperliquid's official contract specification says
that its ordinary BTC, ETH, and SOL perpetuals are USDC-margined but use a USDT-denominated oracle
without a USDC/USDT conversion and are therefore technically quanto contracts. The master Class C
design excludes quanto contracts from its initial universe. Building a net-carry simulator for this
pair before representing that fact would optimize a structurally rejected candidate.

This is a negative but economically valuable result: it prevents more data collection, modeling,
and eventual capital from being committed to a pair that violates the approved strategy boundary.

## 2. Alternatives considered

| Approach | Benefit | Cost or risk | Decision |
|---|---|---|---|
| Source-backed dossier and deterministic evaluator | Creates a reusable admission gate and records the current structural rejection | Does not yet estimate return | Selected |
| Net-carry simulator now | Produces fee and break-even numbers sooner | Would model an excluded quanto pair and could make a rejected pair look actionable | Rejected |
| Add another venue immediately | May discover a compatible pair | Repeats adapter work without a rigorous admission filter | Deferred until the evaluator can screen it |

## 3. Goals

The increment must:

- preserve short, exact supporting excerpts from official sources with retrieval timestamps and
  SHA-256 hashes of the exact stored excerpt bytes;
- separate documented facts from the deterministic compatibility decision;
- cover every compatibility dimension required by the master Class C design;
- fail closed on unsupported structures, hard mismatches, missing evidence, and differences that
  require a model;
- expose one stable JSON model through both a CLI report and the local web dashboard;
- respect point-in-time reads: a dashboard snapshot before the dossier's observation timestamp
  must not show the later dossier;
- remain useful for future venue pairs without embedding Hyperliquid/dYdX logic in the evaluator;
- make the current primary blocker and every remaining evidence gap easy to inspect.

## 4. Non-goals

This increment will not:

- estimate expected return, annualize a current spread, recommend a trade, or claim profitability;
- waive the initial exclusion of quanto, inverse, or unmatched-collateral structures;
- fetch documentation at CLI or dashboard runtime;
- treat a URL as immutable evidence or claim that an excerpt hash represents a hash of the full
  remote page;
- infer compatibility from shared ticker text or shared use of USDC;
- add account authentication, credentials, wallets, balances, positions, orders, transfers,
  signing, paper fills, sizing, allocation, or execution;
- decide jurisdiction, KYC, entity, tax, custody, or legal eligibility;
- allow AI output to approve or alter a compatibility result.

## 5. Source findings that drive the design

The official sources observed on 2026-08-13 establish the following research facts:

- Hyperliquid describes its standard perpetuals as one unit of the underlying, USDC-margined,
  USDT-denominated contracts for ordinary external-liquidity assets. It explicitly calls them
  technically quanto because no USDC/USDT exchange-rate conversion is applied.
- Hyperliquid uses a validator-published oracle for funding and a separate composite mark price for
  margin, liquidation, unrealized P&L, and triggers. It funds hourly, uses a documented premium and
  clamped-interest formula, and documents a 4% hourly cap.
- dYdX's protocol definition represents base-quantity perpetual positions against quote quantums
  and associates each perpetual with a market used as its oracle for collateral, margin, and
  funding. Its current help documentation describes hourly funding, governance-adjustable sampling
  and caps, oracle-based collateral/liquidation controls, order-book liquidation, and deleveraging.
- Both venues document USDC collateral or quote accounting, but that fact alone does not remove
  Hyperliquid's documented quote-oracle/settlement exposure.
- Fee schedules, risk parameters, and some market constraints are tiered or governance/dynamic
  state. Documentation can identify their mechanisms, but an effective account-tier schedule and
  point-in-time parameter capture are still required before a cost model.

The dossier records these as source-bound research statements, not as timeless facts.

## 6. Artifact and model

The package will contain one immutable JSON artifact in a dedicated package-data directory. Runtime
code loads it through `importlib.resources`, so installed wheels and source checkouts use the same
bytes.

### 6.1 Source record

Each source record contains:

- stable `source_id`;
- venue;
- HTTPS URL on an official documentation or official protocol-source domain;
- title;
- UTC `observed_at`;
- a short exact `evidence_excerpt`;
- `excerpt_sha256`, calculated only over the UTF-8 bytes of `evidence_excerpt`.

Validation rejects non-HTTPS URLs, unknown venues, blank identifiers or text, timestamps after the
dossier observation time, and hash mismatches. The field name deliberately says `excerpt_sha256`:
the system must not imply that it archived or hashed the full remote page.

### 6.2 Canonical checks

Exactly one check, in canonical order, is required for each dimension:

1. `asset_and_quantity`
2. `payoff_and_quote`
3. `collateral_and_pnl`
4. `oracle_construction`
5. `mark_and_margin`
6. `liquidation`
7. `auto_deleveraging`
8. `funding_interval`
9. `funding_formula`
10. `funding_cap`
11. `order_constraints`
12. `fee_schedule`
13. `venue_failure_domain`
14. `access_eligibility`

Each check contains the left and right fact summaries, a stable reason code, supporting source IDs,
and one of four judgments:

- `matched`: the evidence supports equivalence for this research dimension;
- `blocking`: the evidence proves a hard mismatch or a structure excluded by the master design;
- `model_required`: the facts differ in a way that needs a separately validated risk or economic
  model before admission;
- `missing_evidence`: the required point-in-time evidence is absent, dynamic, unresolved, or
  intentionally deferred.

`matched` does not mean the pair is globally compatible. Every source ID must resolve, every source
must be cited by at least one check, and check order must be exact. The first dossier applies to
BTC, ETH, and SOL only and names Hyperliquid as left and dYdX as right.

### 6.3 Evaluation status

The evaluator uses a fixed precedence:

1. any `blocking` check -> `ineligible`;
2. otherwise any `missing_evidence` check -> `evidence_incomplete`;
3. otherwise any `model_required` check -> `model_required`;
4. otherwise all checks are `matched` -> `compatible`.

The report includes status, venues, assets, dossier and observation identifiers, count by judgment,
the first blocking reason in canonical order, and all checks. The evaluator performs no semantic
inference and has no venue-specific conditional.

For the initial dossier, `payoff_and_quote` is `blocking` with reason
`quanto_structure_excluded`. The report may also contain `model_required` and `missing_evidence`
checks; the blocking status takes precedence without hiding those additional gaps.

## 7. CLI

Add:

```bash
.venv/bin/polytrading carry dossier --format text
.venv/bin/polytrading carry dossier --format json
```

The command loads the bundled artifact, validates every source and check, evaluates it, and renders
deterministic output. It requires no database and makes no network request. A successfully rendered
`ineligible` research decision exits zero because the command itself succeeded; malformed evidence
uses the existing user-error path and exits two.

The text report begins with the research-only warning and status, names the primary blocker, prints
one stable row per check, and ends by stating that no cost model or trading authority exists. JSON
uses sorted keys, compact UTC timestamps, decimal-safe values, and the same report schema.

## 8. Dashboard

Extend the existing dashboard snapshot with `compatibility_dossier`, either a summary or `null`.
The summary contains the complete report because fourteen short checks remain manageable and allow
the UI to explain the decision without a second endpoint.

The builder loads the bundled dossier only when its `observed_at` is no later than the dashboard
`as_of`. Earlier historical snapshots return `null`; they never leak later research knowledge.

Add a compatibility section between the existing market-evidence table and legacy carry gate. It
shows:

- Hyperliquid -> dYdX and the BTC/ETH/SOL scope;
- the overall `ineligible` status;
- the primary `quanto_structure_excluded` blocker;
- counts for matched, blocking, model-required, and missing-evidence checks;
- a compact fourteen-row matrix with check, judgment, reason, and both venue summaries.

The section is display-only. It does not fetch sources, run a command, mutate evidence, or turn a
compatible result into an activation control. Existing dashboard stale-data behavior remains.

## 9. Error handling and security boundary

- Artifact parse, schema, hash, source-reference, or canonical-order failures are fatal to the CLI.
- The dashboard treats such a bundled-artifact failure as a snapshot refresh failure and preserves
  the last successful browser snapshot under the existing stale-state behavior.
- All displayed content is inserted with `textContent`; source summaries cannot become HTML.
- No remote artifact path, URL fetch, upload, editor, or mutation endpoint is added.
- The dashboard remains bound to `127.0.0.1` with GET/HEAD only and the existing method rejection.
- Compatibility is an admission research result, never an activation result.

## 10. Tests

Test-driven implementation must cover:

- strict source URL, timestamp, excerpt-hash, and source-reference validation;
- exact canonical check coverage and order;
- all four evaluation outcomes and precedence when several judgments coexist;
- successful loading of the packaged artifact from an installed-style resource;
- stable text and JSON rendering with the current primary blocker;
- CLI parsing, no-database behavior, exit codes, and malformed-artifact failure;
- dashboard point-in-time inclusion and exclusion around the dossier observation timestamp;
- dashboard JSON serialization and browser rendering of all judgments with text-only insertion;
- wheel contents for the dossier JSON;
- regression of the full repository test, lint, formatting, and coverage gates.

## 11. Completion and next gate

The milestone is complete when the bundled evidence validates, CLI and dashboard independently show
`ineligible` with `quanto_structure_excluded`, the full quality suite passes, and a fresh diff review
finds no live-trading or profit-model scope.

The next rational milestone is not a Hyperliquid/dYdX return simulator. It is venue discovery using
this dossier schema to find at least one BTC/ETH/SOL pair whose payoff, quote, collateral, and P&L
semantics are not structurally excluded. Only a pair that reaches at least `model_required` without
a blocking or missing-evidence check should proceed to executable fee, depth, forced-exit, and
funding-reversal economics.

## 12. Official sources

- Hyperliquid contract specifications: <https://hyperliquid.gitbook.io/hyperliquid-docs/trading/contract-specifications>
- Hyperliquid robust price indices: <https://hyperliquid.gitbook.io/hyperliquid-docs/trading/robust-price-indices>
- Hyperliquid funding: <https://hyperliquid.gitbook.io/hyperliquid-docs/trading/funding>
- Hyperliquid liquidations: <https://hyperliquid.gitbook.io/hyperliquid-docs/trading/liquidations>
- Hyperliquid auto-deleveraging: <https://hyperliquid.gitbook.io/hyperliquid-docs/trading/auto-deleveraging>
- Hyperliquid fees: <https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees>
- dYdX perpetual protocol definition: <https://github.com/dydxprotocol/v4-chain/blob/main/proto/dydxprotocol/perpetuals/perpetual.proto>
- dYdX oracle prices: <https://help.dydx.trade/en/articles/166990-oracle-prices-on-dydx-chain>
- dYdX funding: <https://help.dydx.trade/en/articles/166992-default-funding-rates-on-dydx>
- dYdX liquidity tiers: <https://help.dydx.trade/en/articles/166993-default-liquidity-tiers-on-dydx-chain>
- dYdX liquidations: <https://help.dydx.trade/en/articles/166991-liquidations-on-dydx-chain>
- dYdX contract loss mechanisms: <https://help.dydx.trade/en/articles/166973-contract-loss-mechanisms-on-dydx-chain>
- dYdX fees: <https://help.dydx.trade/en/articles/166995-trading-fees-on-dydx>
