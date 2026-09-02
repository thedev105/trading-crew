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

### Local Polymarket pilot console

A separate, loopback-only console can authorize tightly bounded Polymarket execution for one
locally present operator:

```bash
.venv/bin/polytrading predictions pilot polymarket \
  --db var/predictions.duckdb \
  --port 8788
```

`--db` and `--port` are the entire CLI surface: there is no credential, order, activation,
capability, or kill-clearance flag. Every launch starts killed. Secrets live only in the macOS
Keychain or systemd's private runtime credential directory and in the signer process; only the
operator can trigger a live action, from the UI, after a typed confirmation and a platform-passkey
ceremony. The evidence dashboard above stays observation-only and cannot reach the pilot, the
signer, or any credential.

See `docs/predictions/polymarket-live-pilot.md` for setup, the immutable ceilings, the three
authorization modes, presence rules, staged activation, and the recovery playbooks.

### macOS CLOB credential readiness and one-time creation

This is a local, macOS-only Keychain ceremony for the designated operator. It is not a CI step,
and it must not be run by an agent. Before any creation attempt, the operator must first run:

```bash
.venv/bin/polytrading predictions pilot credentials check
```

Its only successful output categories are `wallet_ready=true|false` and
`credentials=PRESENT|ABSENT|PARTIAL`; failures return a stable public code and never print a
secret. The wallet Keychain item must contain exactly 64 hexadecimal characters, with an optional
`0x` prefix. The command does not take a wallet, credential, URL, or network flag.

Only when `check` reports a ready wallet and absent credentials, the operator may deliberately run:

```bash
.venv/bin/polytrading predictions pilot credentials create --confirm
```

The successful output categories are `result=CREATED` and a public credential fingerprint. This
makes one real external CLOB credential request, stores the returned values only in the macOS
Keychain, and never trades. It fails rather than overwriting, rotating, or recovery-deriving an
existing or partial credential set. Credential creation does not change the killed-by-default
posture or satisfy any eligibility, legal/KYC/terms/geoblock, funding/allowance, shadow-evidence,
separate-activation, passkey, or manual action gate.

The command holds a lock across concurrent `polytrading` creation attempts, so do not run a second
creation command while one is active. That lock cannot coordinate an external Keychain editor or
another program that changes the same items. Close Keychain Access and do not modify the four
reviewed items during the ceremony. If it reports a failure, run `check` and leave the pilot killed;
there is no automatic repair or recovery command.

### Ubuntu 24.04 headless pilot

Ubuntu support requires Ubuntu 24.04 LTS, systemd 255 or newer, `/usr/bin/systemd-creds`, and the
dedicated unprivileged `polytrading` account. It is supported only through the three fixed units in
`deploy/systemd/`; a direct shell launch has no systemd credential directory and fails closed. The
units use host-key encryption because this deployment has no TPM 2.0. That protects credential
blobs at rest, but it does not protect secrets from root or from compromise of the running service.

Install the application at `/opt/polytrading`, create `/var/lib/polytrading/credentials` owned by
`polytrading:polytrading` with mode `0700`, and provision `wallet-private-key.cred` outside the
application. Feed the wallet key to this exact encryption operation from a trusted non-terminal
secret source—never by pasting it into an interactive shell—and make the resulting blob mode
`0600`, owned by `polytrading`:

```bash
sudo /usr/bin/systemd-creds encrypt \
  --with-key=host --name=wallet-private-key - \
  /var/lib/polytrading/credentials/wallet-private-key.cred
sudo chown polytrading:polytrading \
  /var/lib/polytrading/credentials/wallet-private-key.cred
sudo chmod 0600 /var/lib/polytrading/credentials/wallet-private-key.cred
```

Copy the fixed units to `/etc/systemd/system/`, run `sudo systemctl daemon-reload`, and invoke
exactly one absent-only ceremony:

```bash
sudo systemctl start polytrading-credentials-create.service
# Recovery alternative only; do not run after create succeeds:
sudo systemctl start polytrading-credentials-derive.service
```

The ceremony unit temporarily exposes the root-only systemd host key, read-only and only inside its
private mount namespace, so its unprivileged process can encrypt the new blobs. The long-running
pilot never receives that key. On success, start or restart `polytrading-pilot.service`; a new service invocation is required so
systemd can load the three newly encrypted CLOB blobs. Credential success does not clear the kill,
create trading eligibility, satisfy legal/KYC/terms or geoblock review, fund the wallet, set venue
allowance, satisfy evidence/passkey gates, or authorize an action. For compromise or ambiguity,
stop the pilot, keep it killed, revoke the CLOB credentials at the venue outside this application,
preserve the encrypted blobs for incident review, and rotate the wallet and host as required.

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

## 9. Prospective Lighter–dYdX trial operations

The trial commands collect the candidate-specific, append-only public evidence that the shadow
economics gate needs. They use exactly dYdX and Lighter, exactly BTC, ETH, and SOL, and one shared
database. They do not access accounts or install a scheduler. Run this manual smoke only when the
current hourly funding boundary is inside its five-minute collection window:

```bash
mkdir -p var
.venv/bin/polytrading trial funding --current \
  --db var/lighter-dydx-trial.duckdb --format json
.venv/bin/polytrading trial books --duration-seconds 60 --interval-seconds 5 \
  --db var/lighter-dydx-trial.duckdb
.venv/bin/polytrading trial health --recent-hours 24 \
  --db var/lighter-dydx-trial.duckdb --format text
.venv/bin/polytrading dashboard \
  --db var/lighter-dydx-trial.duckdb --port 8787
```

Before configuring unattended scheduling, complete several successful manual funding and book
cycles and inspect `trial health`. Treat that as a binding rollout checkpoint: verify host clock
synchronization, writable and free disk capacity, scheduler log paths and monitoring, the exact
shared database path used by every command, and loopback dashboard access from the operator host.
Resolve every failed check before scheduling. Prospective timing failures cannot be repaired later.

The funding command validates whole-hour UTC timing. From the boundary through minute 5 inclusive,
it may request public venue data; after that cutoff it makes no venue request and persists a `late`
diagnostic. Every invocation receives a new cycle UUID, including two attempts for the same hourly
boundary. The second attempt is therefore an independent append-only observation, not an in-place
retry: health can choose a later complete attempt while retaining the first attempt and reporting
the duplicate-attempt count.

Exit codes are intended for external monitoring:

- `trial funding` exits `0` when one immutable cycle was durably appended, even when its status is
  `degraded` or `late`; it exits `1` when collection, locking, or persistence prevents a durable
  cycle, and `2` for invalid input.
- `trial books` exits `0` when at least one cycle was durably appended, `1` when no cycle became
  durable, and `2` for invalid input.
- `trial health` exits `0` for `COLLECTING` or `READY_FOR_ECONOMICS_EVALUATION`, `1` for
  `NOT_STARTED` or `DEGRADED`, and `2` for invalid input or an unavailable/non-current database.

These four portable cron entries use the same database and keep the one-minute book burst separate
from the funding attempts. They are documentation only. Replace `/absolute/path/poly-trading` with
the absolute path to the checkout; the project does not install or modify cron or any other
scheduler. The scheduler trigger timezone must be UTC for the minute fields below. Alternatively,
explicitly translate all four minute fields to UTC-aligned wall time while preserving their UTC
minute 1, 4, 6, and 58 relationship.

```cron
1 * * * * cd /absolute/path/poly-trading && .venv/bin/polytrading trial funding --current --db var/lighter-dydx-trial.duckdb --format json >> var/trial-funding.log 2>&1
4 * * * * cd /absolute/path/poly-trading && .venv/bin/polytrading trial funding --current --db var/lighter-dydx-trial.duckdb --format json >> var/trial-funding.log 2>&1
6 * * * * cd /absolute/path/poly-trading && .venv/bin/polytrading trial health --recent-hours 24 --db var/lighter-dydx-trial.duckdb --format json >> var/trial-health.log 2>&1
58 * * * * cd /absolute/path/poly-trading && .venv/bin/polytrading trial books --duration-seconds 60 --interval-seconds 5 --db var/lighter-dydx-trial.duckdb >> var/trial-books.log 2>&1
```

`--current` computes the funding boundary from an aware UTC clock, but it cannot change when cron
starts the process. Local `:01` and `:04` on a whole-hour UTC offset remain aligned; on a half-hour
or quarter-hour offset they occur at different UTC minutes and can fall outside the boundary's
five-minute collection window. Configure cron with UTC triggers or perform and verify the explicit
wall-time translation before enabling the schedule.

Do not point unrelated legacy writer commands at this trial database while scheduled collection is
active. The local writer lease reduces brief write collisions; it is not a distributed lock and it
does not make a network filesystem safe for concurrent writers.

### Storage volume, gaps, and retention

At a five-second interval, a 60-second burst produces at most 12 synchronized book cycles per hour.
For two venues, three assets, two sides, and 20 levels, that is up to 2,880 normalized level rows per
hour, or approximately 4.15 million normalized book levels over 60 days, before evaluator
eligibility exclusions. Exact raw public payloads are also retained; their size is venue-dependent,
so monitor actual database growth, free disk capacity, and collector logs on the target host.

The system performs no automatic retention or deletion. Disk provisioning, backups, retention
policy, and tested restoration remain operator responsibilities. Close collectors before copying a
DuckDB file, preserve the original database before schema upgrades, and never delete a gap merely
to improve a coverage percentage.

Historical collection cannot repair prospective trial lineage. Missing boundaries, late attempts,
and generic historical funding or book rows remain visible for diagnosis, but they are ineligible
for trial readiness and the shadow-economics windows. The health report and dashboard expose those
gaps; neither inserts synthetic evidence nor moves the trial start to hide an eligible-hour miss.

### Dashboard and economics handoff

The loopback dashboard renders this trial's status, per-asset coverage, recent boundary matrix,
fees, projection, and immutable economics evidence from the same database. Its recipes are
copy-only; the page has no collection or execution controls. A transient writer collision returns
`DATABASE_BUSY`; the browser retries within a bounded budget and preserves its prior snapshot with
a visible stale label if refresh still cannot complete.

Immediately before an economics evaluation, persist a fresh synchronized book cycle, inspect trial
health, and then run the separately frozen evaluator:

```bash
.venv/bin/polytrading trial books --once --db var/lighter-dydx-trial.duckdb
.venv/bin/polytrading trial health --recent-hours 24 --db var/lighter-dydx-trial.duckdb
.venv/bin/polytrading carry economics --policy policy/BTC.json \
  --db var/lighter-dydx-trial.duckdb --evaluated-at 2026-11-12T17:59:10Z \
  --evaluation-id 00000000-0000-0000-0000-000000000001 --format json
```

After the fresh immutable book cycle is stored, update the explicit policy `known_as_of` to a UTC
timestamp at or after that cycle's completion and no later than `evaluated-at`; do not weaken the
30-second latest-book limit or backdate the policy cutoff. Confirm the frozen `study_end`, reviewed
fee tier, margin facts, latency facts, operating cost, and source hashes before evaluation.

READY_FOR_ECONOMICS_EVALUATION is not trading authorization. It means only that the collection
windows can be handed to the separate research evaluator. `INSUFFICIENT_EVIDENCE`, `REJECTED`, and
`SHADOW_CANDIDATE` are research results; none can authorize, create, size, submit, cancel, or manage
an order. Account access, credentials, capital allocation, custody, KYC, residency, sanctions, tax,
and legal eligibility remain outside this system.

## 10. Synchronized 20-level book collection

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

## 11. Carry audit interpretation

Every audit requires an explicit point-in-time cutoff. Text and canonical JSON always order assets
as BTC, ETH, and SOL and report research-only warnings. `DIAGNOSTIC_ONLY` means current compatible
funding and book evidence was present; it is not permission to trade. `STALE`, `INSUFFICIENT_DATA`,
and `INELIGIBLE` explain why the evidence fails closed.

A large raw annualized spread can still be `INELIGIBLE`. Annualization is only
`hourly_rate × 8760`, not a forecast. Missing contract semantics, different collateral or P&L
assets, funding timing, stale observations, book gaps, or excessive effective-time skew can all
invalidate comparison. The current public metadata intentionally leaves several compatibility
fields unknown, while Hyperliquid uses USDC and Bybit settles the selected contracts in USDT.

## 12. Cross-venue funding persistence study

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

## 13. Database backup, replay, and schema versions

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

## 14. Explicit read-only boundary

The package contains no credentials, account authentication, private-key or wallet handling,
balance or position access, signing, deposit, withdrawal, transfer, order placement, order
cancellation, allocation, or execution methods. Venue adapters expose public instruments, funding,
market snapshots, and order-book snapshots only. Do not add account or trading surfaces to this
research increment.

## 15. Evidence still required by the Class C activation gate

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

## 16. Offline semantic scout experiment

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

## 17. Multi-venue prediction-market evidence (increment 1)

This is a separate, parallel research system for Polymarket and Kalshi prediction markets,
implementing increment 1 of
`docs/superpowers/specs/2026-08-14-multi-venue-prediction-market-structural-opportunity-system-design.md`:
venue manifests, committed public adapters, an immutable market/rule registry, per-venue continuity
health, and a loopback-only dashboard. It shares no domain types, storage schema, or database file
with the perpetual-futures/carry system described in the rest of this README. A prediction-market
database (for example `var/prediction-markets.duckdb`) is never the same file as `var/forward.duckdb`
or any other existing database, and opening one system's database with the other's tooling fails
closed rather than silently misreading it.

This increment is public-data collection and read-only presentation only; it has no proof,
economics, or shadow-execution surface, and no credential, account, order, or trading authority of
any kind. The Limitless conditional adapter and deterministic candidate discovery are covered in
section 18. Deterministic equivalence and payoff proofs, conservative economics, replay and forward
shadow execution, and any execution adapter remain unimplemented and are increments 3–5.

### Venue manifests and the collection gate

Every collection or CLI command first checks a stored venue manifest through the exact same gate:
`WATCHLIST` and a missing manifest both fail closed before any network request. Neither Polymarket
nor Kalshi ships a bundled "already approved" manifest in this increment, so on a fresh database
`predictions collect` exits `2` until an operator has explicitly recorded a manifest with
`implementation_state` of `READ_ONLY` or later. This is the same fail-closed posture as the rest of
this project: recording a manifest is a deliberate, auditable step, never an implicit default.

The corpus-intake source-use gate (`polytrading.corpus_intake.source_policy`) is generalized to the
same `PredictionSource` vocabulary rather than duplicated; it remains the separate review path for
semantic-scout corpus text provenance and is not itself a venue-level collection gate.

### Collecting public evidence

```bash
mkdir -p var
.venv/bin/polytrading predictions venues status --db var/prediction-markets.duckdb --format json
.venv/bin/polytrading predictions collect polymarket --db var/prediction-markets.duckdb
.venv/bin/polytrading predictions collect kalshi --db var/prediction-markets.duckdb
.venv/bin/polytrading predictions health --db var/prediction-markets.duckdb --format text
```

`predictions collect polymarket` pages Polymarket's public Gamma `/markets` endpoint, decodes its
stringified-JSON `outcomes`/`outcomePrices`/`clobTokenIds` fields exactly once, and stores one
immutable market and rule version per condition. `predictions collect kalshi` pages Kalshi's public
`/markets` endpoint by cursor and normalizes every binary market to the fixed `("yes", "no")`
outcome pair; Kalshi's own multivariate/event-group mechanics are retained in the raw record but not
further interpreted in this increment. Both adapters persist raw responses before normalized
records and never substitute a missing venue field with a value from the other venue.

Executable book and trade collection use the same adapters' `fetch_book_snapshot`/`fetch_trades`
methods directly; a dedicated `predictions collect books`/`predictions collect trades` CLI surface
is deferred to a later plan, consistent with keeping this increment's scope to what the spec commits
to. Kalshi's public order book exposes only resting bids on each side (`yes_dollars`/`no_dollars`);
the adapter derives the opposite side's asks as `1 - price` from the other side's bids, since a bid
to buy one outcome at price `p` is equivalent to an offer to sell the other outcome at `1 - p`.
Kalshi's live and historical trade partitions are joined at the exact cutoff from its own
`/historical/cutoff` endpoint, never duplicating or dropping a boundary-adjacent trade. Kalshi has no
documented live public fee-rate endpoint; `fetch_fee_rate` therefore records no fee evidence and
emits a structured `KALSHI_FEE_RATE_ENDPOINT_UNAVAILABLE` warning rather than fabricate a rate.
Polymarket's `/fee-rate` endpoint returns a flat basis-point reference value, not the
price-dependent curve Polymarket's own documentation says it actually applies; the adapter records
the flat value with a `POLYMARKET_FEE_RATE_IS_FLAT_REFERENCE_NOT_APPLIED_CURVE` warning so it is
never mistaken for the exact applied fee.

### Health and the dashboard

```bash
.venv/bin/polytrading predictions dashboard --db var/prediction-markets.duckdb --port 8787
```

Then open `http://127.0.0.1:8787`. The page shows per-venue collection health (gate status, market
count, latest book age), the most recently retrieved markets, evidence counts, and copyable CLI
recipes, all from one captured `as_of` cutoff, exactly like the existing perpetual-futures dashboard.
It is GET-only, loopback-only, and has no collection or mutation control. `predictions health` and
the dashboard classify a venue as `NOT_COLLECTED` both when its manifest gate rejects collection and
when no evidence has been collected yet under a permitted gate; either way, the operator has
something to investigate. Book staleness thresholds (`STALE` after 5 minutes, `DEGRADED` after 1
hour) are a deliberately conservative starting point for this increment and are independent of the
perpetual-futures system's own staleness constants, since this system's real collection cadence has
not yet been established through operation.

## 18. Conditional Limitless collection and candidate discovery (increment 2)

All candidate relationships this increment produces are quarantined research artifacts, not
trading opportunities. They record that two or more market legs might describe the same or
complementary real-world outcomes, in the same append-only, disposition-labeled, review-gated way
this project already treats every other unproven observation. Deterministic equivalence proof, a
payoff compiler, and conservative economics were future increments as of this section; section 19
covers what increment 3 built on top of them. Nothing `predictions candidates` itself produces
approves a candidate, sizes a position, or authorizes execution — only an operator-authored
attestation followed by `predictions prove` and `predictions scan` (section 19) can ever move a
candidate past `quarantined`.

### Limitless as a third, conditionally-gated venue

Limitless is registered as a third `PredictionVenue`, but it ships with no bundled manifest, so it
is unreachable until an operator explicitly appends one. `predictions collect limitless` runs the
same `evaluate_collection_gate` check as Polymarket and Kalshi (section 17): a missing manifest
fails closed with the typed reason `MANIFEST_NOT_FOUND` before any network request, and only an
operator-recorded manifest whose `implementation_state` is `READ_ONLY` or later and whose
`automated_use_status` is `permitted` opens the gate:

```bash
.venv/bin/polytrading predictions venues status --db var/prediction-markets.duckdb --format text
.venv/bin/polytrading predictions collect limitless --db var/prediction-markets.duckdb
```

On a fresh database the second command exits `2` with that gate reason; it starts succeeding only
after the affirmative manifest step below, exactly like the Polymarket and Kalshi gates it reuses.

The Limitless adapter is read-only and markets/rules only. It pages Limitless's public
`/markets/active` endpoint, keeps only `single`-type markets in the documented `clob`/`amm`
trade types, and stores one immutable market and rule version per market. Book-snapshot,
trade, and fee-rate collection are not implemented for Limitless in this increment; calling those
adapter methods raises a `limitless_endpoint_not_collected` error rather than returning
approximate or synthesized evidence. A negative-risk (event-group) flag, outcome-set membership,
or other field the listing endpoint does not document is recorded `unknown` with a structured
warning rather than guessed. Extending Limitless past markets/rules is a separate, later,
manifest-gated step.

### Deterministic candidate discovery

`predictions candidates` runs two deterministic generators over one or more already-collected,
already-gated venues and persists their output as append-only `CandidateRelationship` records:

```bash
.venv/bin/polytrading predictions candidates \
  --db var/prediction-markets.duckdb \
  --venues polymarket,kalshi,limitless \
  --format text
```

`binary_complement` proposes one candidate per eligible two-outcome market, pairing its two legs
as presumptive complements. `venue_native_outcome_set` proposes one candidate per eligible
multi-market `event_id` group (Polymarket and Limitless groups additionally require every member's
`negative_risk` to be `true`; Kalshi groups purely by `event_id`). Both generators are pure
functions of the registry snapshot at `--as-of` (default: command start): no network access, no
AI inference, and no non-deterministic input. Every emitted candidate carries an
`unresolved_fields` entry (`terminal_partition_unproven` or `outcome_set_exhaustiveness_unproven`)
and starts `review_status="unreviewed"` with `disposition="quarantined"` — a candidate is never
disposed `proof_ready` by a generator. Candidate identity is a UUIDv5 derived deterministically from
the relationship type and its sorted legs, so re-running `predictions candidates` against unchanged
markets is idempotent: previously appended candidates report `already_known` rather than
duplicating. `--trial-family` groups candidates for later evaluation batches and defaults to
`increment-2-structural`; `--as-of` fixes the registry snapshot's information cutoff for a
reproducible run.

Every invocation of `predictions candidates` also prints one fixed line:

```
cross-venue nomination: abstained (SCOUT_GATE_UNMET: no adjudicated gold evaluation)
```

Cross-venue candidate nomination is implemented and tested (the semantic-scout bridge that would
propose `cross_venue_equivalence` candidates from AI-assisted rule-text comparison), but it is not
reachable from this CLI. It stays disabled — and every run reports that explicit abstention rather
than silently doing nothing — until a genuine adjudicated gold evaluation clears the
semantic-scout's critical-field gate.

### The 99.5% scout gate and why it cannot pass yet

The semantic-scout critical-field exact-match threshold (section 16) is `0.995`, raised from an
earlier `0.95` as part of this increment's threshold-gap closure. The checked-in synthetic corpus
under `tests/fixtures/ai/corpus` cannot pass this threshold and is not expected to: it exists only
to exercise retrieval, extraction, and evaluation mechanics deterministically, exactly as section 16
already states. Cross-venue AI nomination stays disabled until a real, two-independently-reviewed,
adjudicated `data/gold` corpus (section 16) is evaluated and clears `0.995` — a bar the spec sets
deliberately high because a false cross-venue equivalence is the failure mode this whole layer
exists to prevent.

`tests/fixtures/predictions/hard_negatives.json` checks in six gold hard-negative pairs: markets
with near-identical titles that TF-IDF retrieval would plausibly nominate across venues, but whose
rule text diverges on exactly one critical dimension (threshold inclusivity, deadline timezone,
resolution-source authority, observation window, same-underlying-exchange/frontend, or scope).
Each pair's two markets are deliberately separate events, so the deterministic generators above
never relate them. These fixed pairs are the concrete cases increment 3's equivalence-compiler
mutation tests and this increment's generator-determinism tests both reuse: a scout or compiler
that cannot tell the two members of a
hard-negative pair apart is not ready to nominate anything across venues.

### Reading candidates on the dashboard

The dashboard's candidates panel (`predictions dashboard`, section 17) lists the most recently
observed candidates alongside their relationship type, participating venues, and disposition, and
tags AI-provenance candidates with an explicit AI-nominated badge — currently unreachable, since
cross-venue nomination is disabled as described above. The panel's own labels never use the words
"risk-free," "guaranteed," or "approved": `quarantined` is the only disposition a generator or the
scout bridge can currently produce, and only a separate, explicit human review can ever move a
candidate to `proof_ready`.

## 19. Rule attestations, deterministic proofs, and the conservative scan (increment 3)

This increment adds the layer between a quarantined candidate relationship (section 18) and a
research-only shadow finding: operator-authored typed rule facts, four deterministic payoff
proof templates compiled from those facts, and a conservative depth-aware economics scan.
Nothing here approves a candidate, sizes a position, or authorizes execution. A
`SHADOW_CANDIDATE` scan decision is a research finding for further human review, never an
opportunity or an instruction to trade — and a stable rejection across repeated scans is just as
valid a result as a `SHADOW_CANDIDATE`, not a failure to find one.

### Rule-relevant rule-version identity

A market's `rule_version_id` is now derived from only its rule-relevant fields — venue,
`market_id`, question, description, resolution source, outcomes, and end time — rather than the
raw collected page's hash. An unrelated byte change elsewhere on a collected page (page layout,
an unrelated field, whitespace) no longer mints a new rule version for a market whose actual
rule content hasn't changed, since a new id would otherwise churn every downstream candidate and
attestation identity that folds in `rule_version_id`. This is a one-time, non-retroactive
transition: only markets normalized after this change get a rule-relevant id, and previously
persisted rule-version rows keep whatever id they were minted with.

### Executable book and fee collection (`--books`)

```bash
.venv/bin/polytrading predictions collect polymarket --db var/prediction-markets.duckdb --books 5
.venv/bin/polytrading predictions collect kalshi --db var/prediction-markets.duckdb --books 5
```

`--books N` (default `0`, markets/rules only) additionally collects executable order-book
snapshots and a fee-rate record for up to `N` order-book-enabled, active, open markets from that
same collection run, selected deterministically by ascending `market_id`. This is the CLI
surface increment 1 deferred (section 17): the same `fetch_book_snapshot`/`fetch_fee_rate`
adapter methods, now reachable without hand-calling them. A single market's book/fee collection
failure is isolated and logged as a warning rather than aborting the whole run or the markets
already collected.

Kalshi exposes no public live fee-rate endpoint, so `--books` collection against Kalshi records
no fee evidence for any market — this is by design, not a gap the adapter tries to paper over.
`predictions scan` (below) therefore cannot evaluate economics for a Kalshi leg until an operator
supplies fee evidence some other way: it stays `INSUFFICIENT_EVIDENCE` with reason `MISSING_FEE`.
Limitless has no book/fee collection at all in this increment (section 18); passing `--books`
greater than zero for `limitless` is rejected outright rather than silently collecting nothing.

### Operator-authored rule attestations

```bash
.venv/bin/polytrading predictions attest \
  --db var/prediction-markets.duckdb \
  --input attestation.json
```

`attestation.json` is an operator-authored JSON array of rule attestations — the *only* bridge
from a market's natural-language rule text to the typed payout facts a proof compiler consumes.
There is deliberately no code path that generates an attestation's content; a human reviewer
reads the rule text and records it. Each attestation is hash-bound to one exact
`rule_version_id`/`rule_source_hash` pair and must cite at least one supporting span indexed into
that exact rule text. One entry looks like:

```json
[
  {
    "schema_version": 1,
    "attestation_id": "8f14e45f-ceea-4d6f-a5c5-f7c1a52b8b2e",
    "venue": "polymarket",
    "market_id": "0xcondition",
    "rule_version_id": "b1946ac9-2f8e-4d0a-9a5b-2e6f5e8c9a1b",
    "rule_source_hash": "a1b2c3...64 hex chars",
    "payout_unit": "usdc_1_per_share",
    "winner_payout_per_share": "1",
    "loser_payout_per_share": "0",
    "outcome_set_exhaustive": true,
    "void_or_invalid_possible": false,
    "void_behavior": "unknown",
    "tie_possible": false,
    "tie_behavior": null,
    "resolution_source_attested": "https://example.test/rules",
    "deadline_utc": null,
    "threshold_text": null,
    "threshold_inclusive": null,
    "supporting_spans": [
      {"start_char": 0, "end_char": 12, "exact_text": "resolves YES", "rule_source_hash": "a1b2c3...64 hex chars"}
    ],
    "review_identity": "reviewer@example.test",
    "reviewed_at": "2026-08-15T12:00:00Z"
  }
]
```

`predictions attest` cross-checks every attestation against the immutable rule-version registry
before appending any of them: an unknown `rule_version_id`, a `rule_source_hash` mismatch, or a
venue/`market_id` mismatch against the stored rule version fails the *entire* import as a usage
error rather than partially persisting a batch. Import is append-only and idempotent — re-running
the same input reports `already_known` rather than duplicating.

### Deterministic proof templates (`predictions prove`)

```bash
.venv/bin/polytrading predictions prove \
  --db var/prediction-markets.duckdb \
  --candidate-id <candidate-id> \
  --format json
```

`predictions prove` compiles one candidate's attested facts into a `ProofArtifact`: either
`proof_ready` (a fully bounded basket payout with cited terminal states) or `rejected`/
`insufficient_evidence` (a typed reason, no payout bounds) — never a partial mix of the two.
Four templates exist, one per `RelationshipType`:

- **`binary_complement@1`** — a market's two outcomes as one basket; requires an attestation
  affirming the pair is exhaustive.
- **`exhaustive_outcome_set@1`** — every member of a venue-native event group as one basket;
  requires an independent attestation from *every* member affirming group exhaustiveness.
- **`logical_implication@1`** — NO(A) + YES(B) across two distinct markets, where a
  deterministically verified implication A ⇒ B (compared over typed `threshold`/`deadline`
  propositions, never inferred from prose) excludes the one impossible combination.
- **`cross_venue_equivalence@1`** — an 8-dimension equivalence matrix comparing two legs' attested
  rule facts field-by-field across two venues, reusing the exact dimension names increment 2's
  scout bridge left unresolved.

Every template is fail-closed and rejects rather than infers whenever an attested fact is
missing, contradictory, or unmodeled — reported through one of a fixed set of typed reasons:
`MISSING_ATTESTATION` (no attestation for a leg), `OUTCOME_SET_NOT_EXHAUSTIVE`,
`VOID_BEHAVIOR_UNKNOWN` (a possible void whose settlement the attestation doesn't pin down),
`TIE_UNMODELED`, `IMPLICATION_INVALID` (the implication doesn't deterministically hold, or the
two propositions aren't comparable), `RULE_VERSION_CHANGED` (a leg's attested rule version has
since been superseded), `PROPOSITIONS_NOT_EXTRACTED`, and `EQUIVALENCE_DIMENSION_UNKNOWN`/
`EQUIVALENCE_DIMENSION_INCOMPATIBLE` for cross-venue equivalence specifically.

`cross_venue_equivalence@1` cannot reach `proof_ready` in this increment, and that is deliberate,
not a bug: two of its eight dimensions — `settlement_finality_timing` and
`venue_access_custody_rules` — have no attested basis anywhere in the current `RuleAttestation`
model, so they are unconditionally `unknown`, and every compiled artifact rejects at minimum on
those two dimensions (`EQUIVALENCE_DIMENSION_UNKNOWN`, or `EQUIVALENCE_DIMENSION_INCOMPATIBLE`
whenever another dimension also diverges — a proven divergence takes precedence in the reported
reason). Cross-venue equivalence across two independent venues is a strong claim; this increment
simply doesn't yet attest enough to support it, and fails closed rather than softening the bar.
The six gold hard-negative pairs checked in for the semantic scout (section 18) double as this
template's own fixtures: every pair — markets whose titles a retriever would plausibly conflate,
but whose rules diverge on exactly one dimension — reject through `cross_venue_equivalence@1`
with that pair's own divergent dimension, never a false pass.

### The conservative scan (`predictions scan`)

```bash
.venv/bin/polytrading predictions scan --db var/prediction-markets.duckdb --format json
```

`predictions scan` re-evaluates every candidate against its latest proof and the freshest
available books/fees, under the frozen `DEFAULT_RESEARCH_POLICY` (policy id `research-v1`), and
persists exactly one append-only `ScanReport` per candidate per run. A scan never compiles a new
proof itself — it only reads what `predictions prove` has already established. Every candidate's
outcome is exactly one of three states:

- **`SHADOW_CANDIDATE`** — the candidate has a `proof_ready` proof, and the depth-walked
  conservative economics evaluation is positive at current book depth. This is a research shadow
  finding for further review, not a trading signal, position size, or execution instruction.
- **`REJECTED`** — a proof rejected the candidate, or the economics evaluation came out
  non-positive. A stable, repeated `REJECTED` result is a valid, useful outcome, not a dead end.
- **`INSUFFICIENT_EVIDENCE`** — no proof exists yet, the proof itself was insufficient evidence,
  or the economics evaluation couldn't run: `MISSING_BOOK`, `STALE_BOOK` (older than the policy's
  5-second freshness gate), `CROSSED_BOOK`, `MISSING_FEE` (this is where a Kalshi leg lands absent
  the fee endpoint above), or `ZERO_EXECUTABLE_DEPTH`.

The economics evaluation itself (spec section 7) walks each leg's live ask ladder to the basket's
bottleneck fillable quantity, then charges every named friction against that depth-walked
acquisition cost before calling anything a surplus: a gas/conversion/redemption reserve, a
currency-basis reserve, a flat transfer cost, a capital-lockup charge over an assumed lock
period, an ordinary operational cost, and four failure reserves (partial-fill/unwind, latency,
dispute delay, venue/custody divergence) — all deliberately small-but-nonzero, conservative
research-mode defaults, never tuned to make a particular candidate look attractive. It also
reports a `doubled_cost_surplus_usd` stress figure — the same surplus recomputed with every cost
component doubled — so a `SHADOW_CANDIDATE` finding that only survives at the policy's exact,
undoubled cost assumptions is visibly fragile rather than hidden behind a single pass/fail
number. `predictions scan` is idempotent at a fixed `--as-of`: re-running it over unchanged
evidence reproduces the same `ScanReport` ids rather than appending duplicates.

### The real end-to-end reachable surface

Despite four proof templates and three venues, in this increment only **Polymarket
binary-complement candidates** can actually traverse the whole
collect → attest → prove → scan pipeline through to a persisted `SHADOW_CANDIDATE`. Every other
combination is structurally blocked before economics ever runs, for reasons specific to each
venue and template rather than one shared gap:

- **Kalshi legs never reach `proof_ready` economics.** Kalshi exposes no public live fee-rate
  endpoint, so `--books` collection against Kalshi never records fee evidence (above); a scan
  stays `INSUFFICIENT_EVIDENCE`/`MISSING_FEE` for any Kalshi leg by design. Independently, Kalshi's
  order books are keyed by the literal outcome strings `"yes"`/`"no"`, while Kalshi's
  `MarketRecord.outcome_token_ids` is always `None` — so a Kalshi candidate leg's
  `outcome_token_id` is always `None` too (candidates derive it from `outcome_token_ids[index]`,
  which doesn't exist for Kalshi). A scan's book lookup is keyed on that per-leg
  `outcome_token_id`, so it can never match a Kalshi book snapshot regardless of fee evidence —
  `MISSING_BOOK` either way.
- **`exhaustive_outcome_set@1` legs carry no per-side token identity either**, for the same
  underlying reason (outcome-set members are grouped by market, not by a per-side token), so this
  template's proofs face the identical book-lookup gap once compiled.
- **`cross_venue_equivalence@1` cannot reach `proof_ready` at all in this increment** (above) —
  two of its eight equivalence dimensions have no attested basis yet, so every compiled artifact
  rejects before a scan would even need book/fee evidence.
- **`logical_implication@1`** proofs can reach `proof_ready` on Polymarket-only baskets, but the
  same Polymarket-only per-leg token identity requirement applies to its books as it does to
  `binary_complement@1`.

None of this is an oversight this increment tries to hide: it's the direct, provable consequence
of per-side token identity existing only on Polymarket legs today. Widening that surface — giving
Kalshi and outcome-set legs their own per-side token identity, and closing the Kalshi fee gap — is
scheduled as next-increment work, not a silent limitation of the scan or proof compilers
themselves.

### Dashboard and recipes

The dashboard (`predictions dashboard`, section 17) now also shows a proofs panel (status and
template counts, the most recently compiled proofs) and a scans panel (decision counts, the most
recent `SHADOW_CANDIDATE` findings with their conservative surplus and capacity), and its copyable
recipes list now includes `--books`, `attest`, `prove`, and `scan`. Both panels are read-only,
loopback-only, and present exactly what is already persisted — the dashboard compiles nothing and
scans nothing itself.

## 20. Prediction-market shadow engine and event-time replay (increment 4)

Shadow results are simulations against recorded public evidence; nothing here trades. This
increment has no authenticated venue client, wallet, signer, order submission, or automatic
capital-transfer path. Its append-only plans, state transitions, ledger postings, reconciliations,
and experiment rows are local research evidence.

### Preregister a trial family

Before a shadow run, write one operator-authored trial-family object. For example,
`shadow-family.json` can contain:

```json
{
  "family_id": "structural-shadow-2026-08",
  "hypothesis": "Positive conservative basket surplus persists across the named stress scenarios.",
  "preregistered_at": "2026-08-25T14:00:00Z",
  "thresholds_json": "{\"minimum_complete\":30,\"minimum_days\":30}",
  "venues": ["kalshi", "polymarket"],
  "registered_by": "research-operator"
}
```

The venue list must be nonempty, sorted, and unique. `thresholds_json` is itself a JSON object
encoded as a string so the exact preregistered threshold document remains immutable. The importer
rejects duplicate keys, non-standard JSON constants, missing or extra fields, wrong JSON types,
naive timestamps, blank text, and malformed threshold objects. Timezone-aware offsets are
normalized to UTC before persistence.

Register it into an existing, current prediction-market database:

```bash
.venv/bin/polytrading predictions shadow register-family \
  --db var/prediction-markets.duckdb \
  --input shadow-family.json
```

Registration appends one `TrialFamily` atomically. Repeating the exact same file reports it as
already known and adds no row; different content with the same `family_id` and
`preregistered_at` identity fails closed.

### Run recorded evidence through the shadow state machine

```bash
.venv/bin/polytrading predictions shadow run \
  --db var/prediction-markets.duckdb \
  --trial-family structural-shadow-2026-08 \
  --as-of 2026-08-25T14:00:00Z \
  --expiry-seconds 30 \
  --scenario baseline \
  --format json
```

`--as-of` is an optional UTC evidence cutoff, `--expiry-seconds` defaults to `30`, and
`--scenario` accepts `baseline`, `latency_1s`, `latency_5s`, `partial_fill_50`,
`second_leg_reject`, or `unknown_after_first`. The run considers the latest effective
`SHADOW_CANDIDATE` scan per candidate at that cutoff, verifies the complete recorded lineage,
freezes the plan, applies the shadow risk gates, and simulates each leg in venue-qualified order.
The risk gates include basket-size, event-cluster concentration, incomplete-leg loss, daily-loss,
drawdown, and capital-preservation limits. A refused proposal is counted by its typed reason and
is not simulated.

Every attempted proposal follows the deterministic non-atomic state machine and records the
simulated acknowledgements/fills, point-in-time book hashes, fees, collateral and payout postings,
terminal state, and reconciliation. A complete reconciliation balances the double-entry ledger
and binds its result to the terminal event. Paper P&L is shown only for that reconciled bundle. A
partial fill triggers a fresh conservative economics check against the remaining recorded books;
missing or ambiguous venue evidence fails toward `UNKNOWN` rather than being inferred.

The dashboard's shadow panel reports proposal state counts, reconciled and unreconciled totals,
reconciled paper P&L, recent proposal rows, and experiment counts by trial family. For `UNKNOWN`,
the operator meaning is exactly: **awaiting reconciliation — paper result invalid**. More
generally, a paper result is invalid until every participating venue reconciles completely.

### Verify exact replay or run a read-only what-if

Replay the stored scenario without `--scenario` to perform the reproducibility check:

```bash
.venv/bin/polytrading predictions shadow replay \
  --db var/prediction-markets.duckdb \
  --proposal-id <proposal-id> \
  --format json
```

Exact replay is read-only. It verifies the frozen scan, candidate, proof, policy, risk policy,
books, fees, event chain, ledger reconciliation, and deterministic plan identity at their original
event-time cutoffs. It reports `MATCHES` only when the regenerated execution and reconciliation
equal the stored records; otherwise it reports the first divergent sequence and exits nonzero.

Passing a scenario selects read-only what-if mode:

```bash
.venv/bin/polytrading predictions shadow replay \
  --db var/prediction-markets.duckdb \
  --proposal-id <proposal-id> \
  --scenario latency_5s \
  --format json
```

What-if replay keeps the frozen proposal and original event-time cutoff, substitutes the named
stress scenario, reads the available recorded point-in-time books, and prints
`"persisted": false`; it never changes the stored proposal, events, ledger, reconciliation, or
experiment registry. A what-if result is a sensitivity result, not the exact reproducibility
verdict for the stored run.

### Calendar gates remain outside the code path

The forward-activation clock has no code shortcut: first collect at least **45 continuous calendar
days** of synchronized rules and executable books and satisfy the fixed evidence thresholds; then
run at least **30 additional calendar days** of queue-aware shadow execution with complete
reconciliation. A dense fixture, repeated replay, more proposals in one day, or a favorable paper
result cannot compress either elapsed-time gate. Any later execution work remains a separate
increment requiring its own evidence checkpoint and explicit authorization.

### Polymarket execution hardening remains `LIVE_DISABLED`

The authenticated Polymarket protocol is implemented for offline conformance and recovery proof,
but production has no capability issuer, live action, activation command, or kill-clear path. Run
the offline check with `.venv/bin/polytrading predictions execution conformance polymarket --db
var/prediction-markets.duckdb --format json`; inspect the loopback-only read-only Market Atlas with
`.venv/bin/polytrading predictions dashboard --db var/prediction-markets.duckdb --port 8787`; and
see the full [execution-hardening and recovery runbook](docs/predictions/polymarket-execution-hardening.md)
for boundaries, recovery semantics, and the still-unsatisfied evidence and approval gates.
