# polytrading

## 1. Research purpose and no-profit disclaimer

`polytrading` is point-in-time, market-neutral market research software. This first increment
collects authenticated-free public evidence for BTC, ETH, and SOL linear perpetuals on Bybit,
Hyperliquid, dYdX, and Lighter, then produces deterministic carry diagnostics for the separately defined
legacy venue pair. It does not promise profit, prevent loss, forecast returns, or recommend a
trade. A large displayed spread is an instantaneous observation, not an investable return.

## 2. Setup and pinned environment

Use Python 3.12 through 3.14. Runtime and development dependencies are exactly pinned in
`pyproject.toml` so a fresh editable environment is reproducible:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
mkdir -p var
.venv/bin/python -m pytest
.venv/bin/ruff check .
```

All prices, rates, quantities, and diagnostic arithmetic use decimal values. Timestamps must be
timezone-aware and are normalized to UTC.

## 3. Deterministic fixture replay

The checked-in JSONL fixture contains exact public payload text plus normalized records. Replay
validates the whole file, verifies every raw SHA-256 hash and same-venue normalized lineage, then
stores each batch raw-first in one transaction. A malformed later row aborts the entire file;
replaying the same exact file is idempotent.

```bash
.venv/bin/polytrading replay \
  --input tests/fixtures/replay/public_snapshot.jsonl \
  --db var/replay.duckdb
.venv/bin/polytrading carry audit \
  --db var/replay.duckdb \
  --as-of 2026-08-12T12:00:00Z \
  --format text
```

## 4. Public-network smoke collection

The following manual smoke command uses only unauthenticated public HTTP endpoints. Collection has
a maximum span of seven days; the default is the seven-day span ending when the command starts.
HTTP requests identify this package, use a 10-second connect timeout and a
30-second read timeout, and retry only rate limiting and selected transient server responses with
a bounded deterministic backoff.

```bash
.venv/bin/polytrading collect public \
  --venue all \
  --assets BTC,ETH,SOL \
  --start 2026-08-05T12:00:00Z \
  --end 2026-08-12T12:00:00Z \
  --db var/public.duckdb
```

This public-network smoke is manual and is not a CI or completion gate. Exchange availability and
response changes are external conditions; automated tests use checked-in responses and mock HTTP
transports.

Bybit funding intervals are resolved only from instrument specifications already known at each
funding event's point in time. A fresh database therefore stores current Bybit instruments and
snapshots but skips a requested historical Bybit funding range when no Bybit specification was
known at the range start. The command remains successful and prints a stderr warning naming the
venue, asset, requested range, and the fact that funding was not collected; other eligible venue
data is still stored. Never backdate a newly observed specification. Build this basis by retaining
the database across public collections over time, or replay previously captured raw-first batches;
once a specification exists at the requested start, later Bybit collections normalize that range.

dYdX can also be collected separately:

```bash
.venv/bin/polytrading collect public \
  --venue dydx \
  --assets BTC,ETH,SOL \
  --db var/dydx-public.duckdb
```

This records exact public market metadata and realized hourly funding. The observed dYdX market
response exposes an oracle price but no documented mark-price field, while this project's
`MarketSnapshot` requires both. The adapter therefore emits a structured
`DYDX_MARK_PRICE_UNAVAILABLE` warning and stores no dYdX market snapshot; it never substitutes the
oracle, midpoint, or last trade as a mark. dYdX and Hyperliquid both documenting USDC margin makes
them a compatibility-research candidate, not proof that their contracts, failure domains, costs,
or access rules are compatible—and not evidence of profit or live eligibility.

Lighter settled public evidence can be collected separately without credentials:

```bash
.venv/bin/polytrading collect public \
  --venue lighter \
  --assets BTC,ETH,SOL \
  --db var/lighter-public.duckdb
```

The adapter resolves current integer market IDs from public metadata instead of hard-coding them.
It records exact active instrument metadata and the `/api/v1/fundings` settled hourly history. A
settled row whose direction is `long` is normalized as a positive rate because longs paid shorts;
`short` is negative because shorts paid longs. The multi-exchange `/api/v1/funding-rates` current
estimate is not used as realized evidence. Lighter's selected REST evidence does not expose a
response-timestamped mark/index pair, so collection emits `LIGHTER_MARK_INDEX_UNAVAILABLE` and does
not create a `MarketSnapshot` from a midpoint, last trade, or other substitute.

## 5. Prospective hourly funding-cycle collection

The prospective collector records one UTC-hour boundary across both venues. For a reproducible
manual invocation, name that boundary explicitly:

```bash
.venv/bin/polytrading collect funding-cycle \
  --db var/forward.duckdb \
  --assets BTC,ETH,SOL \
  --cycle-end 2026-08-13T17:00:00Z \
  --format json
```

For hourly operation, an external scheduler can invoke current mode shortly after the hour. The
command captures one aware UTC clock value, floors it to the current whole hour, and cannot shift
to a newer boundary if collection crosses an hour:

```bash
.venv/bin/polytrading collect funding-cycle \
  --db var/forward.duckdb \
  --current \
  --assets BTC,ETH,SOL \
  --format json
```

The command does not install or configure a scheduler. Before the named hour it rejects the
invocation without creating a database or opening a network session. After the five-minute cutoff
it makes no venue requests and appends an explicit `late_not_collected` cycle. A delayed historical
download cannot be relabeled as point-in-time evidence.

The first cycle in a fresh database can be `degraded` because Bybit funding intervals must come
from instrument specifications already known at the boundary. That cycle stores the newly observed
specifications for later hours but never backdates them. `degraded` and `late` are successfully
persisted diagnostics, not complete evidence; process exit zero means a cycle was recorded, so
operators must inspect its status.

Each attempt appends a new cycle UUID, even when another attempt names the same boundary. Valid raw
responses, normalized records, and the cycle are committed atomically. Successful empty funding
responses retain their raw source hashes and response-observation time so a missing or delayed
response cannot appear on time. Keep raw data local and review venue terms for the intended use.
This command neither authorizes live collection nor enables trading, account access, credentials,
or redistribution.

Audit recent collection health separately after the five-minute collection window has closed:

```bash
.venv/bin/polytrading funding health \
  --db var/forward.duckdb \
  --hours 24 \
  --format text
```

The health command opens an existing current-schema database read-only, makes no network requests,
and evaluates exactly the requested hourly boundaries. At `17:05:00Z`, the `17:00:00Z` boundary
is auditable; before that instant it is not. Exit `0` means every audited boundary has at least one
complete attempt. Exit `1` means health is `degraded` or `critical`; exit `2` means invalid input or
an unavailable/non-current database. When `--as-of` is supplied, attempts completed after that
cutoff are excluded so later retries cannot leak into a historical report. A first Bybit boundary
can be degraded while its observed instrument specification establishes the non-backdated basis
for later hours. Retries append new cycle UUIDs and health selects the best attempt without hiding
the earlier attempts.

For example, these portable cron entries collect at minute 1 and audit at minute 6. They are
documentation only; this project does not install or modify a scheduler. Another installation must
replace `/Volumes/WORK/poly-trading` with its own absolute checkout path:

```cron
1 * * * * cd /Volumes/WORK/poly-trading && .venv/bin/polytrading collect funding-cycle --db var/forward.duckdb --current --format json >> var/funding-cycle.log 2>&1
6 * * * * cd /Volumes/WORK/poly-trading && .venv/bin/polytrading funding health --db var/forward.duckdb --hours 24 --format json >> var/funding-health.log 2>&1
```

Cron's configured timezone is not used to calculate the boundary: `--current` derives it from an
aware UTC clock internally. Collection status and health coverage measure prospective evidence
continuity only. They do not measure strategy quality, expected returns, or profitability, and a
health alert is not a reason to backfill an old boundary as though it were collected on time.

## 6. Local evidence dashboard

Start the loopback-only operator console against an existing current-schema database:

```bash
.venv/bin/polytrading dashboard \
  --db var/forward.duckdb \
  --port 8787
```

Then open `http://127.0.0.1:8787` in a browser. The page presents one point-in-time snapshot of
24-hour funding collection health, the latest public instrument/funding/book evidence, the existing
Bybit/Hyperliquid carry research gate, the ranked contract-dossier catalog, the selected candidate's
complete check matrix, evidence counts, and copyable CLI recipes. Every refresh opens the selected
database read-only and uses one captured UTC `as_of` across the complete screen. A dossier appears
only when its observation time is no later than that cutoff, so later venue research cannot leak
into a historical dashboard view.

The market grid contains twelve canonical rows: BTC, ETH, and SOL for each of Bybit, Hyperliquid,
dYdX, and Lighter. Lighter rows show settled signed funding and locally timed REST depth when those
records exist. The economics table contains exactly one BTC, ETH, and SOL row selected from reports
known by the same dashboard cutoff; it displays unavailable values rather than substituting zero.
These rows do not display an execution action.

The database must already exist and have the current schema. A temporary database lock or other
availability conflict can make a refresh fail; the browser retains its last successful snapshot and
marks it stale. Missing evidence remains visibly unavailable rather than appearing as zero.

The recipes are text copied to the clipboard and are never executed by the page. The server binds
only to `127.0.0.1` and has no remote mode, authentication surface, collection controls, credentials,
accounts, positions, orders, or trading authority. Stop it with `Ctrl-C`.

## 7. Venue discovery and contract compatibility dossiers

Rank the bundled official-source research catalog without a database or network request, or inspect
either immutable dossier directly:

```bash
.venv/bin/polytrading carry discovery --format text
.venv/bin/polytrading carry discovery --format json
.venv/bin/polytrading carry dossier --format text
.venv/bin/polytrading carry dossier --id lighter-dydx-core-v1 --format text
```

At the 2026-08-13 research cutoff, discovery selects `lighter-dydx-core-v1` for the next modeling
stage. It has exactly four matched checks, ten `model_required` checks, zero blocking checks, and
zero missing-evidence checks. The documented matches are base-quantity semantics, linear USD
payoff, USDC accounting, and hourly funding. The remaining checks still require explicit models or
reviews for oracle and mark construction, liquidation and deleveraging, funding formulas and caps,
order constraints, effective costs, failure domains, and access eligibility.

`model_required` means only that the pair passed the initial structural screen with enough official
evidence to define those next investigations. It is not a conclusion that the venues are equivalent,
the trade is profitable, drawdown is bounded, an account is eligible, or paper or live execution is
authorized. In particular, Lighter's documented Standard Account may advertise zero maker and taker
fees while imposing maker, taker, and cancellation latency; Premium Account fees and latency vary by
tier. The economic model must use observed executable conditions and the intended account type, not
the headline fee.

Hyperliquid/dYdX remains visible at rank two as `ineligible` with primary reason
`quanto_structure_excluded`. Hyperliquid's official specification describes its ordinary BTC, ETH,
and SOL perpetuals as USDC-margined while using a USDT-denominated oracle without a USDC/USDT
conversion, and characterizes the contracts as technically quanto. Shared USDC margin or P&L
accounting with dYdX therefore does not make this pair admissible: the approved initial Class C
universe excludes quanto structures.

`ineligible` is a successful research outcome, not a command failure. It prevents fee or return
modeling from making a structurally rejected pair appear actionable. The report also preserves
fourteen canonical checks across quantity, payoff, collateral/P&L, oracle/mark, liquidation/ADL,
funding, constraints, fees, failure domains, and access. Blocking reasons take precedence without
hiding model differences or missing evidence.

The short excerpts, official URLs, observation timestamps, and hashes of the exact stored excerpt
bytes are bundled with the package. The command never fetches remote documentation at runtime, and
the excerpt hashes are not presented as hashes of the full remote pages. The dashboard shows the
same typed report only when its observation timestamp is no later than the screen's `as_of` cutoff.

Germany or Estonia not appearing in a displayed restriction excerpt is not legal approval. User,
entity, sanctions, interface, API, KYC, tax, and jurisdiction eligibility remain a separate documented
review before any activation decision. The implemented read-only Lighter adapter supplies the next
evidence stream; the following engineering gate is a separate point-in-time model of fees, latency,
marketable depth, forced exits, basis, and funding reversals after enough observations accumulate.
No account or execution surface belongs in that gate.

## 8. Lighter–dYdX shadow economics gate

This deterministic gate asks whether one fixed Lighter–dYdX funding direction survives explicit
costs and stress reserves using only evidence already stored in the local database. It does not
connect to an account or venue. Before evaluation, import a human-reviewed fee document whose
rates are JSON strings and whose URLs and hashes identify the exact point-in-time evidence used:

```json
{
  "schema_version": 1,
  "reviewed_at": "2026-08-13T17:00:00Z",
  "fees": [
    {
      "schema_version": 1,
      "venue": "dydx",
      "tier_name": "reviewed-tier",
      "maker_rate": "0",
      "taker_rate": "0.0005",
      "effective_from": "2026-08-13T00:00:00Z",
      "observed_at": "2026-08-13T16:00:00Z",
      "source_url": "https://help.dydx.trade/en/articles/166995-trading-fees-on-dydx",
      "source_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    },
    {
      "schema_version": 1,
      "venue": "lighter",
      "tier_name": "reviewed-tier",
      "maker_rate": "0",
      "taker_rate": "0",
      "effective_from": "2026-08-13T00:00:00Z",
      "observed_at": "2026-08-13T16:00:00Z",
      "source_url": "https://docs.lighter.xyz/trading/trading-fees",
      "source_hash": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    }
  ]
}
```

The dates, tier names, account types, URLs, hashes, rates, margin fractions, and operational-cost
URL in these JSON blocks are illustrative placeholders, not reviewed evidence. Replace every one
with values and SHA-256 hashes from an actual human review at the intended point in time; copying
the examples does not establish a valid fee, margin, latency, or operational assumption.

Save that document as `reviewed-fees.json`, then import it atomically:

```bash
.venv/bin/polytrading fees import \
  --input reviewed-fees.json \
  --db var/forward.duckdb
```

The evaluation policy freezes all protocol thresholds while making the account equity, approved
cash benchmark, actual fee tier/account type, operational cost, and cited evidence explicit. Every
decimal is a JSON string:

```json
{
  "schema_version": 1,
  "protocol_version": "lighter-dydx-shadow-economics-v1",
  "asset": "BTC",
  "study_end": "2026-08-13T16:00:00Z",
  "known_as_of": "2026-08-13T17:00:00Z",
  "account_equity_usd": "8000",
  "cash_benchmark_annual_rate": "0.04",
  "operational_cost_usd": "2",
  "prefunded": false,
  "operational_source_url": "https://example.com/reviewed-operational-cost",
  "operational_source_hash": "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
  "execution_assumptions": [
    {
      "schema_version": 1,
      "venue": "dydx",
      "fee_tier_name": "reviewed-tier",
      "account_type": "standard",
      "taker_latency_ms": "300",
      "observed_at": "2026-08-13T16:00:00Z",
      "source_url": "https://help.dydx.trade/en/articles/166995-trading-fees-on-dydx",
      "source_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    },
    {
      "schema_version": 1,
      "venue": "lighter",
      "fee_tier_name": "reviewed-tier",
      "account_type": "standard",
      "taker_latency_ms": "300",
      "observed_at": "2026-08-13T16:00:00Z",
      "source_url": "https://docs.lighter.xyz/trading/trading-fees",
      "source_hash": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    }
  ],
  "margin_assumptions": [
    {
      "schema_version": 1,
      "venue": "dydx",
      "asset": "BTC",
      "initial_margin_fraction": "1",
      "maintenance_margin_fraction": "0.05",
      "close_out_margin_fraction": "0.04",
      "liquidation_penalty_fraction": "0.01",
      "observed_at": "2026-08-13T16:00:00Z",
      "source_url": "https://help.dydx.trade/en/articles/166991-liquidations-on-dydx-chain",
      "source_hash": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
    },
    {
      "schema_version": 1,
      "venue": "lighter",
      "asset": "BTC",
      "initial_margin_fraction": "1",
      "maintenance_margin_fraction": "0.05",
      "close_out_margin_fraction": "0.04",
      "liquidation_penalty_fraction": "0.01",
      "observed_at": "2026-08-13T16:00:00Z",
      "source_url": "https://docs.lighter.xyz/trading/contract-specifications",
      "source_hash": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
    }
  ],
  "training_days": 30,
  "evaluation_days": 60,
  "minimum_coverage": "0.99",
  "maximum_book_age_seconds": "30",
  "maximum_cycle_skew_ms": "1000",
  "maximum_hourly_book_age_seconds": "300",
  "maximum_assigned_equity_fraction": "0.10",
  "maximum_assigned_usd": "500",
  "incomplete_leg_shock": "0.10",
  "maximum_incomplete_loss_equity_fraction": "0.0025",
  "minimum_hold_return": "0.003",
  "minimum_profit_usd": "3",
  "minimum_annualized_return": "0.12",
  "cash_benchmark_spread": "0.05",
  "maximum_stress_loss_equity_fraction": "0.0025",
  "maximum_drawdown_fraction": "0.08",
  "forced_exit_depth_multiplier": "2",
  "doubled_cost_multiplier": "2",
  "minimum_normal_quote_observations": 25,
  "minimum_stress_quote_observations": 10
}
```

Save it as `policy.json`. Use a caller-created UUID and evaluation timestamp so the result is
reproducible:

```bash
.venv/bin/polytrading carry economics \
  --policy policy.json \
  --db var/forward.duckdb \
  --evaluated-at 2026-08-13T17:00:07Z \
  --evaluation-id 00000000-0000-0000-0000-000000000801 \
  --format json
```

The command returns zero for all three research decisions and persists exactly one immutable
report. `INSUFFICIENT_EVIDENCE` means a required point-in-time input is absent, stale, conflicting,
or below coverage. `REJECTED` means evidence is complete enough to evaluate but one or more
compatibility, economics, capacity, or stress gates failed. `SHADOW_CANDIDATE` means the complete
report passed the frozen numeric gates; it is still research only. Invalid input, an unavailable
database, or an immutable-record conflict exits with code 2.

The database needs 90 prospective days: the first 30 select one direction from only the training
funding median, and the next 60 evaluate that fixed direction without hindsight switching. The
current-regime check requires exactly the final 168 consecutive hourly boundaries ending at the
evaluation end; it never bridges a missing hour by taking the last 168 available rows. The database
also needs at least 99% paired hourly funding and book coverage, recent synchronized depth,
consecutive dense book samples for latency, the actual reviewed fee tiers, documented latency and
margin facts, and a reviewed operational-cost amount. An empty or young database should normally
produce `INSUFFICIENT_EVIDENCE`, not zero-valued economics.

Sizing uses equal base quantity on both legs, rounds down to a compatible step, assumes both legs
are fully collateralized, and caps assigned capital at the smallest of 10% of account equity,
USD 500, available entry and doubled forced-exit depth, and the incomplete-leg loss limit. If exit
depth is tighter, sizing rounds down before rejecting. Funding is calculated once per exact venue
leg with the correct long/short sign; the fifth-percentile rolling 7-, 14-, and 28-day aggregate USD
cashflow is divided by total assigned capital for portfolio return. Funding reversal, adverse basis
divergence, forced exit, latency, fees, and operations are additive reserves. Basis divergence is
charged against the average one-leg entry notional, and favorable convergence receives no credit.
Reports show both venue funding components and return on assigned capital and total account equity,
so unused cash is never removed from the account denominator.

`SHADOW_CANDIDATE` is not a recommendation, simulated fill, paper order, promise of profit, or live
authorization. The system has no wallet, signer, balance, position, transfer, order, cancellation,
or venue-account client. KYC, residency, entity, sanctions, custody, transfer-route, tax, and legal
eligibility—including Germany or Estonia—remain deferred reviews outside this model.

## 9. Synchronized 20-level book collection

Book cycles start the selected venue requests concurrently and request exactly 20 levels for each
asset. Run one cycle with `--once`, or collect for a bounded duration:

```bash
.venv/bin/polytrading collect books \
  --venue all \
  --assets BTC,ETH,SOL \
  --duration-seconds 3600 \
  --interval-seconds 5 \
  --db var/public.duckdb
```

Every cycle records request timing, source hashes, failures, gaps, and maximum exchange-effective
timestamp skew. A failed venue is never filled from an older cycle, and collection continues with
bounded backoff. Bybit exposes snapshot sequence evidence; Hyperliquid's public REST response does
not expose a REST sequence number, so its sequence is recorded as absent. Even where a REST
snapshot includes an identifier, repeated snapshots do not prove continuous sequence integrity,
atomic cross-venue state, or the absence of unseen book changes between requests.

For a dYdX-only REST snapshot:

```bash
.venv/bin/polytrading collect books \
  --venue dydx \
  --assets BTC,ETH,SOL \
  --once \
  --db var/dydx-books.duckdb
```

The dYdX REST book response exposes neither a venue timestamp nor a sequence. Its normalized
`effective_at` is therefore the local post-response receipt time, its sequence is absent, and the
CLI prints `DYDX_REST_BOOK_LOCAL_TIMESTAMP`. These snapshots can support coarse receipt-skew and
depth research, but not exchange-time simultaneity, continuous-sequence, or queue-position claims.

For Lighter-only REST depth:

```bash
.venv/bin/polytrading collect books \
  --venue lighter \
  --assets BTC,ETH,SOL \
  --once \
  --db var/lighter-books.duckdb
```

Lighter's REST response contains individual public orders. The adapter sums remaining quantity at
each price, records the number of contributing orders, and retains the best 20 aggregated price
levels per side. The response provides no venue snapshot timestamp or sequence, so the normalized
book uses local post-response receipt time and the CLI prints
`LIGHTER_REST_BOOK_LOCAL_TIMESTAMP`. This is executable-depth research evidence, not queue-position
or continuous-book proof.

## 10. Carry audit interpretation

Every audit requires an explicit point-in-time cutoff. Text and canonical JSON always order assets
as BTC, ETH, and SOL and report research-only warnings. `DIAGNOSTIC_ONLY` means current compatible
funding and book evidence was present; it is not permission to trade. `STALE`, `INSUFFICIENT_DATA`,
and `INELIGIBLE` explain why the evidence fails closed.

A large raw annualized spread can still be `INELIGIBLE`. Annualization is only
`hourly_rate × 8760`, not a forecast. Missing contract semantics, different collateral or P&L
assets, funding timing, stale observations, book gaps, or excessive effective-time skew can all
invalidate comparison. The current public metadata intentionally leaves several compatibility
fields unknown, while Hyperliquid uses USDC and Bybit settles the selected contracts in USDT.

## 11. Cross-venue funding persistence study

The read-only study tests one frozen direction: long the Bybit perpetual and short the
Hyperliquid perpetual for the same asset. It sums native settlements into common eight-hour UTC
blocks and never fills a missing funding interval. The database must already exist; the command
opens it in read-only mode, performs no network requests, and does not collect or modify data.

```bash
.venv/bin/polytrading carry study \
  --db var/public.duckdb \
  --asset BTC \
  --start 2025-08-13T00:00:00Z \
  --end 2026-08-13T00:00:00Z \
  --known-as-of 2026-08-13T00:05:00Z \
  --format json
```

`known-as-of` is the local knowledge cutoff: revisions observed later are excluded. A historical
API download is labeled `historical_reconstruction` when it was learned more than five minutes
after settlement. It can support a 365-day gross replication but cannot count as the required
forward record. A genuinely point-in-time study needs at least 90 days. Both require at least 99%
paired block coverage and complete 7-, 14-, and 28-day windows.

`FORWARD_TEST_REQUIRED` means only that a gross historical replication passed its fixed median,
lower-tail, and best-month-concentration checks. `NET_FORWARD_GATE_REQUIRED` means point-in-time
gross funding passed; it still does not recommend or approve a trade. Neither state models fees,
slippage, basis P&L, collateral effects, financing, taxes, or venue and forced-exit losses.

A displayed 10% annualized funding spread is a rate on one matched leg's notional, not a 10%
account return. A fully collateralized two-venue position ties up capital on both legs, and four
executions plus reserves can consume the gross spread. Live use remains disabled and requires
separate data-use, eligibility, net-cost, stress, execution, and reconciliation approval.

Technical access is not a data license. Do not redistribute venue records or use this tool to
create a commercial data product. The current Bybit API agreement and any applicable Hyperliquid
terms must be reviewed for the exact intended use before expanding collection or proprietary
deployment.

## 12. Database backup, replay, and schema versions

DuckDB files are append-only research stores. Close collectors before copying a database file for
backup, retain the source JSONL beside it, and test restoration by replaying into a new database
rather than overwriting an existing one. An exact replay is idempotent; a conflicting immutable
identity is rejected.

Every raw, normalized, registry, experiment, cycle, journal, and report record carries a schema
version. SQL migrations are embedded in the installed package, recorded in `schema_migrations`,
and applied forward-only when a store opens. Older facts are not rewritten to impersonate a newer
schema. Preserve the original database before upgrading code across schema versions.

Corrected shadow-economics reports use schema 2. If a development database contains a schema-one
economics report from before the per-venue cashflow correction, the reader preserves its identity
and timestamps but the dashboard labels it `LEGACY_ECONOMICS_SCHEMA_UNSUPPORTED`, treats it as
insufficient evidence, and withholds all economic values. Those missing venue components cannot be
safely reconstructed from the old aggregate.

## 13. Explicit read-only boundary

The package contains no credentials, account authentication, private-key or wallet handling,
balance or position access, signing, deposit, withdrawal, transfer, order placement, order
cancellation, allocation, or execution methods. Venue adapters expose public instruments, funding,
market snapshots, and order-book snapshots only. Do not add account or trading surfaces to this
research increment.

## 14. Evidence still required by the Class C activation gate

This increment does not activate automated trading. A separate reviewed activation decision still
requires all of the following evidence:

- 12 months of point-in-time history;
- 45 continuous days of synchronized books and explicit gap accounting;
- independently validated fee and slippage models;
- a reversal and forced-exit reserve model;
- the complete stress suite;
- 90 forward days without look-ahead leakage;
- ledger reconciliation;
- a documented eligibility and legal/compliance review.

Until that gate is satisfied, the only valid output is read-only research evidence and diagnostics.

## 15. Offline semantic scout experiment

The semantic scout is an offline research layer. AI-like components may retrieve similar rule
text and propose structured interpretations; only deterministic schema, source-span, corpus,
registry, and evaluation code validates those proposals. Nothing in this package proves payoff
equivalence, approves a proposal, changes a risk limit, accesses an account, or submits an order.

This phase deliberately uses two local baselines: character 3–5 gram TF-IDF for candidate
retrieval and conservative regular expressions for rule extraction. It also writes provider-neutral
prompt packets for a separately authorized human-operated runner, but contains no hosted-model SDK,
provider credentials, browsing tool, or inference call. In-repository baseline inference cost is
exactly USD 0.

The checked-in `tests/fixtures/ai/corpus` corpus is synthetic, frozen, and intentionally unresolved.
It exists only for deterministic tests and demonstrations; its manifest records zero reviews and
nine unresolved items. These commands therefore exercise mechanics, not model validation:

```bash
.venv/bin/polytrading ai retrieve \
  --corpus tests/fixtures/ai/corpus \
  --split validation \
  --top-k 50 \
  --output var/ai-retrieval.jsonl

.venv/bin/polytrading ai extract-baseline \
  --corpus tests/fixtures/ai/corpus \
  --split validation \
  --output var/ai-extractions.jsonl

.venv/bin/polytrading ai prompt-packets \
  --corpus tests/fixtures/ai/corpus \
  --split validation \
  --output var/ai-prompt-packets.jsonl

.venv/bin/polytrading ai evaluate \
  --corpus tests/fixtures/ai/corpus \
  --experiment-id 019b3b42-0000-7000-8000-000000000001 \
  --output var/ai-report
```

`import-artifacts` is a stricter boundary than baseline extraction. It requires an exact validated,
unexpired model card already registered in the target DuckDB, a frozen matching corpus, exact
source hashes and spans, an exact semantic version, and an explicit budget. A clean database has no
such validated card, and the command correctly rejects the draft baseline. The synthetic CLI test
registers a fixture-only card to exercise import mechanics; that is not production validation.

### Corpus construction and preregistration

Production evidence belongs only under `data/gold`. Preregister the sampling policy, import exact
public rule text with source URL/retrieval/cutoff provenance, obtain two genuinely independent
reviews for every contract and relationship, adjudicate disagreements with a distinct third person,
then freeze the corpus. Never copy fixture reviews, invent reviewers, or label synthetic text as
public evidence. The production gate can be checked without weakening it:

```bash
.venv/bin/polytrading ai corpus validate \
  --dir data/gold \
  --require-contracts 500 \
  --require-templates 20 \
  --require-relationships 250 \
  --require-adversarial 200 \
  --require-two-reviews
```

Until genuine Task 4 evidence exists, this command exits 1 and names every deficit. Evaluation also
refuses an unfrozen or file-inconsistent manifest. Trial-family, code, model, feature, prompt, and
split-family identities are fixed before the untouched test split; train diagnostics run first,
validation second, and test at most once per registered experiment.

### Reading the report

- Critical-field exact match compares both known/unknown status and exact normalized values.
- Candidate recall is retrieved known-positive relationships divided by all known positives.
- Span validity is valid exact source-backed known fields divided by all known fields.
- Malformed and hostile fail-closed rates must be 1; an accepted malformed item is a breach.
- Mutation invalidation is reported independently for operator, timestamp, oracle, and fallback.
- Review reduction is `1 - routed_manual_count / retrieval_candidate_count`; routing everything to
  review honestly produces zero reduction.
- A zero denominator is `NOT_MEASURABLE`, never NaN, and thresholds use unrounded Decimal values.

JSON and Markdown reports retain raw numerators/denominators, failure IDs, abstentions, corpus and
adversarial counts, hashes, and case results. Their top-level status remains
`RESEARCH_ONLY_NOT_PROMOTABLE`. The fixture report explicitly labels its evaluation basis
`synthetic_fixture_self_consistency`; it is not adjudicated-gold accuracy. Class G
false-eligibility remains
`BLOCKED_BY_DEPENDENCY` until a deterministic payoff compiler and graph exist and pass; semantic
similarity or extraction accuracy cannot substitute for payoff proof.

If a future provider runner is separately approved, its monthly inference budget is exactly the
smaller of USD 25 and 0.3125% of supplied equity. Prompt packets themselves enable no tools or
browsing. Imported artifacts can never contain eligibility, order, size, leverage, risk-limit,
credential, wallet, tool-call, or trade-proposal authority fields.
