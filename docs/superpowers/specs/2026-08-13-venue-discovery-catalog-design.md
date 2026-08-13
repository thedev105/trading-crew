# Venue Discovery Catalog Design

**Date:** 2026-08-13

**Status:** autonomously approved under the operator's standing instruction to continue, review,
and complete routine research milestones without pausing for confirmation

**Scope:** read-only venue research and local display; no account or trading authority

## 1. Decision

Turn the single bundled contract dossier into a deterministic venue-discovery catalog and add a
source-backed Lighter/dYdX dossier for core BTC, ETH, and SOL perpetuals.

The new dossier is expected to evaluate to `model_required` with four `matched` checks, ten
`model_required` checks, zero `blocking` checks, and zero `missing_evidence` checks. This means the
pair is the first candidate allowed to advance from structural screening into public-data and
economic modeling. It does not mean the pair is compatible without reservations, profitable,
legally approved, or authorized for paper or live execution.

The existing Hyperliquid/dYdX dossier remains immutable and `ineligible`. A discovery evaluator
ranks every bundled dossier without changing its underlying judgment, selects only a
`compatible` or `model_required` candidate, and exposes the same result through a database-free
CLI command and the loopback dashboard.

## 2. Why Lighter/dYdX

Official documentation observed on 2026-08-13 supports four unusually strong structural matches:

- both venues represent BTC, ETH, and SOL positions in base-asset quantities;
- Lighter documents linear P&L as exit minus entry price times position, denominated in USDC,
  while dYdX's protocol represents base positions against quote quantums;
- both use USDC-denominated collateral or quote accounting for the selected core markets; and
- both settle funding hourly.

The differences are material but modelable: oracle and mark construction, liquidation and
deleveraging waterfalls, funding formulas and caps, account and order constraints, fees and
latencies, independent-chain failure modes, and venue/interface eligibility. None of the reviewed
official sources identifies an inverse, quanto, unmatched-collateral, prelaunch, or unsupported
asset structure for this pair.

Lighter is selected ahead of the two other reviewed candidates:

| Candidate | Benefit | Research cost or uncertainty | Decision |
|---|---|---|---|
| Lighter/dYdX | Linear USDC accounting, hourly funding, core assets, sequence-aware public books | New Lighter adapter and failure-domain model still required | Selected |
| Drift/dYdX | Self-custodial core markets and rich public protocol data | DLOB, JIT auction, and AMM fallback make displayed depth different from the complete fill path | Defer |
| Paradex/dYdX | Linear USDC contracts and public order books | Eight-hour versus hourly funding adds payment-timing and reversal assumptions | Defer |

Adding a Lighter public adapter before this gate was rejected because it would collect data for an
unscreened pair. Modeling the already-rejected Hyperliquid/dYdX pair was rejected because its
documented quanto structure violates the initial Class C universe.

## 3. Goals

The increment must:

- preserve the existing Hyperliquid/dYdX artifact and its deterministic result;
- add an immutable Lighter/dYdX artifact with short exact excerpts, observation timestamps, and
  SHA-256 hashes of the exact excerpt bytes;
- distinguish research-only venue identities from venues that already have live public-data
  adapters;
- load an explicit, deterministic catalog of bundled dossiers from source and installed wheels;
- rank reports without reinterpreting their checks or hiding rejected candidates;
- select Lighter/dYdX as the only candidate ready to advance to modeling;
- retain the existing `carry dossier` behavior while allowing an explicit dossier identifier;
- add a database-free `carry discovery` command in deterministic text and JSON forms;
- show the ranked catalog and selected candidate's complete check matrix in the local dashboard;
- preserve point-in-time dashboard behavior when only older dossiers existed; and
- leave every execution and activation boundary unchanged.

## 4. Non-goals

This increment will not:

- add a Lighter HTTP or WebSocket adapter, fetch network data at CLI or dashboard runtime, or
  backdate live market parameters;
- estimate funding income, annualized return, spread persistence, fees, slippage, basis risk,
  forced-exit cost, or expected profit;
- treat advertised zero fees as zero execution cost or ignore Standard Account latency;
- determine a user's legal, tax, KYC, sanctions, corporate, or interface eligibility;
- create a credential, API key, wallet, account, balance, position, order, transfer, signing,
  deposit, withdrawal, paper-fill, or execution surface;
- let AI content alter a source excerpt, dossier judgment, ranking, or selection; or
- revise the master Class C risk limits or activation evidence requirements.

## 5. Venue identity boundary

The current `Venue` enum identifies venues supported by normalized market-data records and public
adapters. Adding `LIGHTER` to it before an adapter exists would falsely advertise collection
support and could widen exhaustive loops throughout the dashboard and storage layer.

Add a dossier-local `ResearchVenue` enum with `hyperliquid`, `dydx`, and `lighter`. Dossier sources,
artifacts, and reports use `ResearchVenue`; normalized market records continue to use `Venue`.
Existing JSON values remain unchanged, so the old artifact needs no content migration. Official
source-prefix validation becomes exhaustive over `ResearchVenue` and accepts only:

- Hyperliquid's official GitBook documentation;
- dYdX official help, documentation, community documentation, and protocol GitHub organization;
- Lighter's official documentation, API documentation, website, and published asset domains.

The boundary is intentionally one-way: a research venue may later gain an adapter, but its
appearance in a dossier never implies adapter or execution support.

## 6. Lighter/dYdX dossier

The artifact ID is `lighter-dydx-core-v1`. Lighter is the left venue, dYdX is the right venue, and
the asset order is BTC, ETH, SOL. The existing 14-check order and four judgments remain unchanged.

Expected checks:

| Check | Judgment | Reason |
|---|---|---|
| asset and quantity | `matched` | Core positions use base-asset quantities with documented precision. |
| payoff and quote | `matched` | Both selected products have linear USD price exposure rather than inverse or quanto treatment. |
| collateral and P&L | `matched` | Selected-account accounting is USDC-denominated on both venues. |
| oracle construction | `model_required` | Lighter's multi-provider index construction differs from dYdX validator-consensus prices. |
| mark and margin | `model_required` | Lighter's composite fair mark differs from dYdX's oracle and liquidity-tier controls. |
| liquidation | `model_required` | Both may use order-book liquidity, but triggers, partial-close logic, and backstops differ. |
| auto-deleveraging | `model_required` | Counterparty selection and loss waterfalls differ. |
| funding interval | `matched` | Both document hourly payments for the selected markets. |
| funding formula | `model_required` | Premium sampling, interest, impact notional, and clamps differ. |
| funding cap | `model_required` | Lighter documents per-hour clamps while dYdX caps derive from governance-controlled risk parameters. |
| order constraints | `model_required` | Current specs and sequence semantics exist but require point-in-time capture and fillability validation. |
| fee schedule | `model_required` | Lighter account type changes fees and latency; dYdX fees remain tiered and governance-controlled. |
| venue failure domain | `model_required` | Lighter's Ethereum L2 and dYdX Chain are distinct but retain shared USDC, oracle, interface, and infrastructure risks. |
| access eligibility | `model_required` | Official restrictions can be screened, but independent user/entity and jurisdiction review is still mandatory. |

`model_required` is clarified to mean a separately validated risk, economic, operational, or
eligibility review is required. It is not a positive compatibility judgment. `missing_evidence`
remains reserved for a check with no adequate current official basis to define that next review.
This clarification preserves the evaluator and makes the access check honest: the official terms
define restrictions, while final legal eligibility remains outside this research increment.

Every source must be cited and every source ID must resolve exactly as in the existing model. The
new artifact may reuse an official URL but never a source ID from another artifact as an implicit
cross-artifact reference. Runtime remains offline.

## 7. Catalog and discovery evaluation

Replace the single hard-coded resource name with an explicit ordered tuple:

1. `hyperliquid-dydx-core-v1.json`
2. `lighter-dydx-core-v1.json`

`load_bundled_dossiers()` validates all artifacts and then rejects duplicate dossier IDs or
duplicate directed venue pairs. `load_bundled_dossier(dossier_id=...)` remains available; when no
ID is supplied it returns the original Hyperliquid/dYdX dossier for backward compatibility.
Unknown IDs and any invalid catalog member use a sanitized `ValueError` naming only the stable
dossier ID.

`evaluate_discovery()` consumes already evaluated reports and sorts them by:

1. `compatible`;
2. `model_required`;
3. `evidence_incomplete`;
4. `ineligible`;
5. dossier ID as a deterministic tie-breaker.

It never changes a report status. `selected_dossier_id` is the first `compatible` or
`model_required` report, or `null` when none exists. The report includes the common research
warning, observation cutoff, all ranked reports, candidate counts by status, selection reason
`best_nonblocking_complete_evidence`, and `activation_status=not_authorized`.

For the two bundled artifacts, Lighter/dYdX is selected and Hyperliquid/dYdX remains visible as
rejected. A selected report with any blocking or missing-evidence count is a validation error,
preventing a future ranking change from bypassing the gate.

## 8. CLI

Preserve:

```bash
.venv/bin/polytrading carry dossier --format text
.venv/bin/polytrading carry dossier --format json
```

Add explicit selection:

```bash
.venv/bin/polytrading carry dossier --id lighter-dydx-core-v1 --format text
```

Add catalog discovery:

```bash
.venv/bin/polytrading carry discovery --format text
.venv/bin/polytrading carry discovery --format json
```

All commands are deterministic, database-free, and offline. A valid rejected, incomplete, or
model-required research result exits zero. Invalid identifiers or malformed evidence follow the
existing sanitized user-error path and exit two.

Text discovery output begins with the research-only warning, identifies the selected dossier and
the fact that activation is not authorized, then prints one stable row per ranked candidate with
status and judgment counts. It ends by naming the next gate: public Lighter evidence and economic
modeling. JSON preserves complete nested reports with sorted keys and decimal-safe timestamps.

## 9. Dashboard

Add an optional `venue_discovery` report to `DashboardSnapshot`; retain
`compatibility_dossier` as the original Hyperliquid/dYdX report for backward compatibility. The
builder includes only artifacts whose `observed_at` is no later than the single dashboard `as_of`.
It evaluates discovery over that historical subset:

- before the first artifact, both fields are `null`;
- between the two artifact observations, the legacy dossier is present and discovery shows no
  selected candidate;
- at or after the second observation, discovery selects Lighter/dYdX.

Replace the single-pair visual section with a venue-discovery section containing:

- a selected-candidate card that says `model_required`, `not authorized`, and names the next
  evidence gate;
- a ranked row for every available dossier with pair, assets, status, and four judgment counts;
- the selected candidate's complete 14-row matrix; and
- the legacy rejected pair's primary blocker in its ranked row.

The UI remains display-only. It uses `textContent`, adds no source fetches or controls, and retains
the last successful snapshot under existing stale-refresh handling.

## 10. Error handling and boundaries

- Artifact parse, UTF-8, source prefix, timestamp, hash, source-reference, canonical-order,
  duplicate-catalog, and discovery-invariant failures are fatal to CLI use.
- Dashboard refresh failure preserves the last successful snapshot and marks it stale.
- No selection can be converted into an activation control.
- No URL is treated as immutable; hashes cover only the exact stored excerpts.
- Current documentation and terms are dated observations, not permanent product or legal facts.
- Germany or Estonia not appearing in a displayed restriction excerpt is not itself a legal
  approval. The activation gate still requires an independent documented eligibility decision.

## 11. Tests

Test-driven implementation must cover:

- `ResearchVenue` isolation and exhaustive official-prefix validation;
- unchanged validation and evaluation of the original artifact;
- exact expected counts and `model_required` status for Lighter/dYdX;
- repeatable hashes and complete citation coverage for both artifacts;
- deterministic catalog loading, explicit selection, unknown ID errors, and duplicate rejection;
- all discovery rank states, stable tie-breaking, no-candidate behavior, and selection invariants;
- backward-compatible dossier CLI behavior plus new `--id` and `carry discovery` text/JSON output;
- dashboard behavior before, between, and after artifact observation timestamps;
- browser rendering of both ranked pairs, the selected badge, the primary rejection, and all 14
  selected checks using text-only insertion;
- installed-wheel inclusion of exactly both dossier resources; and
- full repository tests, coverage, Ruff lint/format, wheel build, and database-free CLI smokes.

## 12. Completion and next gate

This milestone is complete when both bundled artifacts validate, Lighter/dYdX is deterministically
selected as `model_required` with no blocking or missing checks, Hyperliquid/dYdX remains
`ineligible`, CLI and dashboard agree point-in-time, and the complete quality suite passes.

The next milestone is a read-only Lighter public-evidence adapter. It must capture current core
instrument parameters, realized hourly funding, mark/index state, and sequence-aware executable
books without credentials. Only after sufficient point-in-time evidence exists may the project
build fee, latency, depth, funding-reversal, and forced-exit economics. Paper and live trading
remain later, independently reviewed gates.

## 13. Official sources reviewed

Lighter:

- Contract specifications: <https://docs.lighter.xyz/trading/contract-specifications>
- P&L and account value: <https://docs.lighter.xyz/trading/pnl-and-total-account-value>
- Fair-price marking: <https://docs.lighter.xyz/perpetual-futures/fair-price-marking>
- Liquidations and LLP: <https://docs.lighter.xyz/trading/liquidations-and-llp-insurance-fund>
- Funding: <https://docs.lighter.xyz/trading/funding>
- Trading fees and account latency: <https://docs.lighter.xyz/trading/trading-fees>
- WebSocket order-book semantics: <https://apidocs.lighter.xyz/docs/websocket-reference>
- Terms and Layer 2 description: <https://lighter.xyz/terms>

dYdX:

- Perpetual protocol definition: <https://github.com/dydxprotocol/v4-chain/blob/main/proto/dydxprotocol/perpetuals/perpetual.proto>
- Oracle prices: <https://help.dydx.trade/en/articles/166990-oracle-prices-on-dydx-chain>
- Funding: <https://help.dydx.trade/en/articles/166992-default-funding-rates-on-dydx>
- Liquidity tiers: <https://help.dydx.trade/en/articles/166993-default-liquidity-tiers-on-dydx-chain>
- Liquidations: <https://help.dydx.trade/en/articles/166991-liquidations-on-dydx-chain>
- Contract loss mechanisms: <https://help.dydx.trade/en/articles/166973-contract-loss-mechanisms-on-dydx-chain>
- Trading fees: <https://help.dydx.trade/en/articles/166995-trading-fees-on-dydx>
- Geo restrictions and site access: <https://help.dydx.trade/en/articles/166970-geo-restrictions-site-access>
