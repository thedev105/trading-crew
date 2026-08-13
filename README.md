# polytrading

## 1. Research purpose and no-profit disclaimer

`polytrading` is point-in-time, market-neutral market research software. This first increment
collects authenticated-free public evidence for BTC, ETH, and SOL linear perpetuals on Bybit and
Hyperliquid, then produces deterministic carry diagnostics. It does not promise profit, prevent
loss, forecast returns, or recommend a trade. A large displayed spread is an instantaneous
observation, not an investable return.

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

## 5. Prospective hourly funding-cycle collection

The prospective collector records one named UTC-hour boundary across both venues. An external
scheduler must pass the hour that just ended and invoke the command no later than five minutes
after that boundary:

```bash
.venv/bin/polytrading collect funding-cycle \
  --db var/forward.duckdb \
  --assets BTC,ETH,SOL \
  --cycle-end 2026-08-13T17:00:00Z \
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

## 6. Synchronized 20-level book collection

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

## 7. Carry audit interpretation

Every audit requires an explicit point-in-time cutoff. Text and canonical JSON always order assets
as BTC, ETH, and SOL and report research-only warnings. `DIAGNOSTIC_ONLY` means current compatible
funding and book evidence was present; it is not permission to trade. `STALE`, `INSUFFICIENT_DATA`,
and `INELIGIBLE` explain why the evidence fails closed.

A large raw annualized spread can still be `INELIGIBLE`. Annualization is only
`hourly_rate × 8760`, not a forecast. Missing contract semantics, different collateral or P&L
assets, funding timing, stale observations, book gaps, or excessive effective-time skew can all
invalidate comparison. The current public metadata intentionally leaves several compatibility
fields unknown, while Hyperliquid uses USDC and Bybit settles the selected contracts in USDT.

## 8. Cross-venue funding persistence study

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

## 9. Database backup, replay, and schema versions

DuckDB files are append-only research stores. Close collectors before copying a database file for
backup, retain the source JSONL beside it, and test restoration by replaying into a new database
rather than overwriting an existing one. An exact replay is idempotent; a conflicting immutable
identity is rejected.

Every raw, normalized, registry, experiment, cycle, journal, and report record carries a schema
version. SQL migrations are embedded in the installed package, recorded in `schema_migrations`,
and applied forward-only when a store opens. Older facts are not rewritten to impersonate a newer
schema. Preserve the original database before upgrading code across schema versions.

## 10. Explicit read-only boundary

The package contains no credentials, account authentication, private-key or wallet handling,
balance or position access, signing, deposit, withdrawal, transfer, order placement, order
cancellation, allocation, or execution methods. Venue adapters expose public instruments, funding,
market snapshots, and order-book snapshots only. Do not add account or trading surfaces to this
research increment.

## 11. Evidence still required by the Class C activation gate

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

## 12. Offline semantic scout experiment

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
