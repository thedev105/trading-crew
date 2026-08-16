# Multi-Venue Prediction-Market Shared Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the append-only, raw-first prediction-market evidence core for increment 1 only:
venue manifests, a shared venue-neutral domain model, committed Polymarket and Kalshi public
adapters, an immutable market/rule registry, per-venue continuity health, a new `predictions` CLI
command group, and a separately-namespaced `predictions dashboard`. This plan implements
`docs/superpowers/specs/2026-08-14-multi-venue-prediction-market-structural-opportunity-system-design.md`
section 16 increment 1 exactly and nothing beyond it: no semantic scout, no equivalence/payoff
compilers, no economics engine, no replay/shadow engine, no risk engine, and no Limitless adapter
(conditional, deferred per section 1). Increments 2-5 receive their own later plans.

**Architecture:** Prediction markets do not fit the existing perpetual-futures domain model. The
existing `Venue` enum (`hyperliquid`/`bybit`/`dydx`/`lighter`) and `Asset` enum (`BTC`/`ETH`/`SOL`)
are structurally fixed to a three-asset, four-venue perpetuals universe; the existing `RawEnvelope`,
`InstrumentSpec`, `FundingObservation`, and `DuckDBStore`/its migration sequence are all typed
directly against that `Venue`/`Asset` pair. Polymarket and Kalshi markets are an open, growing,
per-venue-arbitrary set of conditions with no fixed "asset" axis at all. Rather than distort the
existing enums or interleave a second domain's tables into the same migration sequence, this plan
adds a fully parallel, equally strict package: `src/polytrading/predictions/`, with its own
`PredictionVenue` enum, its own `StrictRecord`-based domain contracts, its own DuckDB store and
migration sequence (opened as a distinct database file, e.g. `var/prediction-markets.duckdb`, never
the same file as `var/forward.duckdb` or other existing databases), and its own dashboard entry
point. This keeps the existing, already-shipped perpetual-futures system provably untouched (per
spec section 1: "must not be silently deleted, rewritten") while reusing every architectural
*pattern* already proven in this codebase: raw-first append-only persistence, `StrictRecord`
(`extra=forbid, frozen=True, strict=True`) contracts, SHA-256 source-hash lineage, one-transaction
batch persistence, sequential numbered SQL migrations, and `argparse` subcommand-group CLI
registration. The existing `src/polytrading/corpus_intake/source_policy.py` gate is generalized
in place (its `Literal["polymarket"]` types become a shared `PredictionSource` enum) rather than
duplicated, per the spec's section 6.1 revision.

**Tech Stack:** Python 3.12-3.14, Pydantic 2.13.4, DuckDB 1.5.4, httpx 0.28.1 (REST) plus a
WebSocket-capable dependency for Polymarket CLOB market-channel continuity (see Task 5 for the
exact dependency decision), argparse, pytest 9.1.1, Hypothesis 6.160.0, Ruff 0.15.22, and the
existing local HTML/CSS/JavaScript dashboard stack.

## Global Constraints

- Implement only increment 1 of
  `docs/superpowers/specs/2026-08-14-multi-venue-prediction-market-structural-opportunity-system-design.md`
  (as revised 2026-08-15): venue manifests, shared domain contracts, Polymarket adapter, Kalshi
  adapter, market/rule registry, executable book/trade/fee evidence, continuity health, the
  `predictions` CLI group, and `predictions dashboard`. Limitless, semantic scout, equivalence/
  payoff compilers, economics, replay/shadow, and risk engine are explicitly out of scope.
- Never reuse the existing perpetual-futures `Venue`, `Asset`, `RawEnvelope`, `InstrumentSpec`,
  `FundingObservation`, `MarketSnapshot`, `Level2BookSnapshot`, `FeeSchedule`, or `DuckDBStore`
  types or migration sequence for prediction-market data. Build a parallel, equally strict set.
- Never write prediction-market data into an existing perpetual-futures database file, and never
  write perpetual-futures data into a prediction-market database file. The two systems use
  disjoint `--db` paths and disjoint migration sequences; nothing in this plan's storage layer may
  make the two files schema-compatible or interchangeable.
- Persist raw responses before normalized records in every collection path, exactly as the
  existing venue adapters do; a raw source hash always resolves to the exact bytes received.
- Every model is a `StrictRecord` (or an equally strict local base): `extra="forbid"`,
  `frozen=True`, `strict=True`, aware-UTC timestamps only, exact SHA-256 lowercase-hex source
  hashes. No adapter may substitute a missing venue field with an inferred or Polymarket-like
  default; unsupported fields are `unknown`/`None` and explicitly warned.
- Collection fails closed unless a venue manifest permits the exact requested source and purpose
  (spec section 6.1). No adapter opens a network client until its manifest check passes.
- Generalize `src/polytrading/corpus_intake/source_policy.py`'s `Literal["polymarket"]` typing to
  a shared `PredictionSource` enum covering `polymarket` and `kalshi` (Limitless stays out of this
  increment) as explicit, tested work — not a silent, unplanned side effect of another task.
- Kalshi live and historical partitions must be joined at the exact official cutoff timestamps
  (`GET /historical/cutoff`: `market_settled_ts`, `trades_created_ts`) without duplicating or
  dropping records; an adapter may not claim continuity stronger than each venue's own API
  provides (Polymarket CLOB WebSocket sequencing vs. Kalshi's REST-only public market data).
- A changed question, rule text, resolution source, outcome set, or critical metadata flag creates
  a new immutable market/rule version; the old version remains queryable at historical cutoffs.
  Nothing in this registry may rewrite or delete a prior version.
- The `predictions` CLI group and `predictions dashboard` are new top-level surfaces; neither may
  register a subcommand name, `--db`/`--port` combination, or dashboard route that collides with
  or is ambiguous against the existing `replay`, `dashboard`, `carry`, `trial`, `ai`, `collect`,
  or `funding` commands already in `src/polytrading/cli.py`.
- `predictions dashboard` opens its own store read-only, presents one captured point-in-time
  `as_of` snapshot, and has no mutation, collection, or credential surface, exactly like the
  existing dashboard's own constraints.
- No credential, signer, wallet, order, cancellation, position, balance, transfer, or execution
  dependency anywhere in this increment. Kalshi's optional authenticated demo WebSocket evidence
  (spec section 3.1) is out of scope for increment 1 unless a later task explicitly re-adds it
  with its own isolated, non-funded credential handling; this plan does not implement it.
- Use exact `Decimal` values for prices, sizes, and rates; never round monetary or probability
  values through `float`.
- Preserve the complete existing test suite green and the existing 90%+ coverage bar; this plan's
  new package must reach the same bar independently.

## Scope decomposition

The specification crosses domain modeling, storage, two independent external adapters, a shared
registry, health auditing, CLI, and dashboard presentation. The two adapters (Polymarket, Kalshi)
are independently testable against each other and are ordered as separate tasks so either can be
reviewed and merged on its own; every other subsystem is shared and ordered so each later task
consumes only exact interfaces already committed by an earlier task. Each task ends in a
reviewable, independently tested commit.

## File Structure

- Create `src/polytrading/predictions/__init__.py`: public package boundary for the prediction-
  market core.
- Create `src/polytrading/predictions/domain.py`: `PredictionVenue`, `PredictionSource`,
  `PredictionRawEnvelope`, `MarketRecord`, `RuleVersion`, `TradeRecord`, `PredictionBookLevel`,
  `PredictionBookSnapshot`, `PredictionFeeRate`, and shared strict-record conventions.
- Create `src/polytrading/predictions/manifest.py`: venue manifest contracts and the
  `WATCHLIST`/`READ_ONLY`/`SHADOW`/`LIVE_DISABLED`/`LIVE_ELIGIBLE` gate (spec section 6.1).
- Create `src/polytrading/predictions/adapter.py`: `PredictionVenueAdapter` protocol,
  `PredictionAdapterBatch`, `PredictionAdapterWarning`, and batch-lineage validation, mirroring
  `src/polytrading/venues/public.py`'s pattern for the prediction-market domain.
- Create `src/polytrading/predictions/storage/__init__.py` and
  `src/polytrading/predictions/storage/store.py`: `PredictionMarketStore` with its own connect,
  migrate, transaction, and append/read API, independent of `polytrading.storage.store`.
- Create `src/polytrading/predictions/storage/schema/001_prediction_core.sql`: raw envelopes,
  venue manifests, markets/rule versions, books, trades, and fees tables.
- Create `src/polytrading/predictions/polymarket.py`: committed Polymarket adapter (Gamma
  markets/rules, CLOB REST book snapshots, CLOB WebSocket continuity, fee-rate endpoint).
- Create `src/polytrading/predictions/kalshi.py`: committed Kalshi adapter (public REST market
  data, historical-partition joining at the official cutoff).
- Create `src/polytrading/predictions/registry.py`: immutable market/rule registry read layer over
  `PredictionMarketStore`.
- Create `src/polytrading/predictions/health.py` and `src/polytrading/predictions/health_report.py`:
  per-venue collection/continuity health audit and text/JSON renderers.
- Create `src/polytrading/predictions/cli.py`: `predictions venues status`, `predictions collect
  polymarket|kalshi`, and `predictions health` argument parsing and dispatch, registered from
  `cli.py`.
- Create `src/polytrading/predictions/dashboard.py`, `dashboard_models.py`: point-in-time
  prediction-market dashboard snapshot builder and its own `DashboardSnapshot`-equivalent models.
- Modify `src/polytrading/cli.py`: register the top-level `predictions` command group and its
  `predictions dashboard` entry point (a distinct server instance from the existing `dashboard`
  command, sharing only the underlying HTTP server plumbing where it is venue-neutral).
- Modify `src/polytrading/corpus_intake/source_policy.py`: replace every
  `source: Literal["polymarket"]` with `source: PredictionSource` imported from
  `polytrading.predictions.domain`, and update `IntendedUseScope`, `SourceEvidence`,
  `SourceUseAssessment`, and `SourceUseApproval` accordingly.
- Modify `README.md`: add a prediction-market collection, health, and dashboard section mirroring
  the existing perpetual-futures sections' rigor and disclaimers.
- Create `tests/predictions/` with one test module per new source module; modify
  `tests/corpus_intake/test_source_policy.py` and `tests/test_cli.py` where their public contracts
  grow.

## Requirement-to-Task Map

- Venue-neutral domain contracts, raw envelope, market/rule/book/trade/fee models: Task 1.
- Venue manifest gate and its five-state adapter-implementation vocabulary: Task 2.
- Generalized `source_policy.py` source vocabulary shared with the manifest: Task 3.
- Prediction-market storage: migration, raw-first append, transaction, cutoff-safe reads: Task 4.
- Shared adapter protocol, batch, and lineage validation: Task 5.
- Committed Polymarket adapter (Gamma, CLOB REST, CLOB WebSocket, fee rate): Task 6.
- Committed Kalshi adapter (public REST, historical-partition join): Task 7.
- Immutable market/rule registry read layer: Task 8.
- Per-venue continuity/collection health audit, reports: Task 9.
- `predictions` CLI command group (venues status, collect, health): Task 10.
- `predictions dashboard`: snapshot, models, server entry point, markup: Task 11.
- README, full verification, package, and browser smoke: Task 12.

---

### Task 1: Venue-neutral domain contracts

**Files:**

- Create: `src/polytrading/predictions/__init__.py`
- Create: `src/polytrading/predictions/domain.py`
- Create: `tests/predictions/__init__.py`
- Create: `tests/predictions/domain_helpers.py`
- Create: `tests/predictions/test_domain.py`

**Interfaces:**

- Consumes: nothing from the existing perpetual-futures domain except the general pattern of
  `polytrading.domain.models.StrictRecord` (do not import it directly; define a local
  `PredictionRecord(BaseModel)` base with the same `ConfigDict(extra="forbid", frozen=True,
  strict=True)` and the same aware-UTC/SHA-256 validators, so this package has zero import
  dependency on the perpetual-futures domain module).
- Produces:
  - `PredictionRecord(BaseModel)` strict base with `require_utc` (applied to
    `observed_at`/`effective_at`/`retrieved_at`/`start_date`/`end_date`/`created_at`/
    `resolution_date` via `check_fields=False`) and `require_sha256` (applied to `source_hash`).
  - `PredictionVenue(StrEnum)`: `POLYMARKET = "polymarket"`, `KALSHI = "kalshi"`.
  - `PredictionSource(StrEnum)`: `POLYMARKET = "polymarket"`, `KALSHI = "kalshi"` (Task 3 imports
    this exact enum into `corpus_intake/source_policy.py`; do not create two parallel vocabularies).
  - `Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]`.
  - `PredictionRawEnvelope(PredictionRecord)`: `schema_version: Literal[1]`, `event_id: UUID`,
    `venue: PredictionVenue`, `endpoint: str`, `venue_timestamp: datetime | None`,
    `observed_at: datetime`, `received_monotonic_ns: int`, `request_latency_ms:
    Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]`, `source_version: str`,
    `payload_json: str`, `source_hash: Sha256`.
  - `MarketRecord(PredictionRecord)`: `schema_version: Literal[1]`, `market_id: str` (venue-native,
    e.g. Polymarket `conditionId` or Kalshi `ticker`), `venue: PredictionVenue`,
    `underlying_exchange: str | None` (for a broker/frontend venue; `None` for an independent
    venue), `event_id: str | None`, `question: str`, `slug: str | None`, `outcomes: tuple[str, ...]`,
    `outcome_token_ids: tuple[str, ...] | None` (Polymarket CLOB token IDs; `None` for Kalshi),
    `negative_risk: bool | None` (Polymarket/Limitless-specific per spec section 6.2; always `None`
    for Kalshi), `active: bool`, `closed: bool`, `restricted: bool`, `order_book_enabled: bool`,
    `start_at: datetime | None`, `end_at: datetime | None`, `resolution_source: str | None`,
    `rule_version_id: UUID`, `information_cutoff: datetime`, `source_url: str`,
    `retrieved_at: datetime`, `raw_hash: Sha256`, `normalized_hash: Sha256`.
  - `RuleVersion(PredictionRecord)`: `schema_version: Literal[1]`, `rule_version_id: UUID`,
    `market_id: str`, `venue: PredictionVenue`, `question: str`, `description: str`,
    `resolution_source: str | None`, `outcomes: tuple[str, ...]`, `superseded_rule_version_id:
    UUID | None`, `effective_at: datetime`, `source_hash: Sha256`.
  - `TradeRecord(PredictionRecord)`: `schema_version: Literal[1]`, `venue: PredictionVenue`,
    `market_id: str`, `outcome_token_id: str | None`, `trade_id: str`, `price:
    Annotated[Decimal, Field(gt=0, lt=1, allow_inf_nan=False)]` (a probability-priced outcome
    share), `size: Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]`, `side: Literal["buy",
    "sell"] | None`, `effective_at: datetime`, `observed_at: datetime`, `source_hash: Sha256`.
  - `PredictionBookLevel(PredictionRecord)`: `price: Annotated[Decimal, Field(gt=0, lt=1,
    allow_inf_nan=False)]`, `size: Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]`.
  - `PredictionBookSnapshot(PredictionRecord)`: `schema_version: Literal[1]`, `cycle_id: UUID`,
    `venue: PredictionVenue`, `market_id: str`, `outcome_token_id: str | None`,
    `bids: tuple[PredictionBookLevel, ...]`, `asks: tuple[PredictionBookLevel, ...]`,
    `sequence: str | None`, `effective_at: datetime`, `observed_at: datetime`,
    `source_hash: Sha256`, with the same non-crossed, strictly-ordered book validation as
    `polytrading.domain.models.Level2BookSnapshot` (reimplemented locally, not imported).
  - `PredictionFeeRate(PredictionRecord)`: `schema_version: Literal[1]`, `venue: PredictionVenue`,
    `market_id: str | None` (Polymarket per-token fee; `None` for a venue-wide Kalshi rate),
    `maker_rate: Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]`, `taker_rate:
    Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]`, `observed_at: datetime`,
    `source_hash: Sha256`.
- Consumers: every later task in this plan.

- [x] **Step 1: Write failing strict-base and enum tests**

Prove the local strict base rejects extra fields and mutation, and requires aware UTC:

```python
def test_prediction_record_forbids_extra_and_mutation_and_requires_aware_utc() -> None:
    class _Probe(PredictionRecord):
        observed_at: datetime

    with pytest.raises(ValidationError, match="extra"):
        _Probe(observed_at=NOW, unexpected=1)

    probe = _Probe(observed_at=NOW)
    with pytest.raises(ValidationError):
        probe.observed_at = NOW

    with pytest.raises(ValidationError, match="aware"):
        _Probe(observed_at=datetime(2026, 8, 15, 12, 0, 0))


def test_prediction_venue_and_source_share_exact_two_values() -> None:
    assert {member.value for member in PredictionVenue} == {"polymarket", "kalshi"}
    assert {member.value for member in PredictionSource} == {"polymarket", "kalshi"}
```

- [x] **Step 2: Run the focused test and observe the missing module**

Run:

```bash
.venv/bin/python -m pytest tests/predictions/test_domain.py -q
```

Expected: collection fails because `polytrading.predictions.domain` does not exist.

- [x] **Step 3: Implement the strict base and enums**

Do not import `polytrading.domain.models`; this package must have zero dependency on the
perpetual-futures domain so the two systems can never accidentally share a type. Copy the UTC and
SHA-256 validator *pattern*, not the class.

- [x] **Step 4: Write failing model construction and invariant tests**

Cover, at minimum:

```python
def test_market_record_negative_risk_is_none_for_kalshi() -> None:
    market = market_record(venue=PredictionVenue.KALSHI, negative_risk=None)
    assert market.negative_risk is None

    with pytest.raises(ValidationError, match="negative_risk"):
        market_record(venue=PredictionVenue.KALSHI, negative_risk=False)


def test_prediction_book_snapshot_rejects_crossed_or_misordered_book() -> None:
    with pytest.raises(ValidationError, match="descending"):
        prediction_book_snapshot(bids=(level("0.40", "10"), level("0.45", "10")))
    with pytest.raises(ValidationError, match="cross"):
        prediction_book_snapshot(
            bids=(level("0.60", "10"),), asks=(level("0.55", "10"),)
        )


def test_trade_and_book_prices_are_bounded_probabilities() -> None:
    with pytest.raises(ValidationError):
        trade_record(price=Decimal("1.01"))
    with pytest.raises(ValidationError):
        trade_record(price=Decimal("0"))
```

Add `tests/predictions/domain_helpers.py` factories (`market_record`, `rule_version`,
`trade_record`, `prediction_book_snapshot`, `level`, `fee_rate`, `raw_envelope`) with the same
deterministic-default-plus-`**overrides` shape used by
`tests/ai/test_corpus.py::contract` and the trial-plan's `trial_funding_item` helper.

- [x] **Step 5: Implement the Kalshi-specific `negative_risk` invariant and book/price validators**

`MarketRecord` requires `negative_risk is None` whenever `venue is PredictionVenue.KALSHI` (per
spec section 6.2's revision — the flag is Polymarket/Limitless-specific only). `TradeRecord` and
`PredictionBookLevel` require `0 < price < 1` since these are outcome-share probability prices, not
generic asset prices; reject exactly at the boundaries.

- [x] **Step 6: Add Hypothesis property coverage for canonical round-tripping**

Prove that every accepted `MarketRecord`, `RuleVersion`, `TradeRecord`, and `PredictionBookSnapshot`
round-trips through `model_dump(mode="json")` / `model_validate` unchanged, and that a naive
datetime is rejected for every timestamp field across randomized field combinations.

- [x] **Step 7: Verify and commit Task 1**

Run:

```bash
.venv/bin/python -m pytest tests/predictions/test_domain.py -q
.venv/bin/ruff check src/polytrading/predictions tests/predictions
.venv/bin/ruff format --check src/polytrading/predictions tests/predictions
git add src/polytrading/predictions tests/predictions
git commit -m "feat(predictions): define venue-neutral domain contracts"
```

Expected: focused domain tests and static checks pass.

---

### Task 2: Venue manifest and collection/execution gate

**Files:**

- Create: `src/polytrading/predictions/manifest.py`
- Create: `tests/predictions/test_manifest.py`

**Interfaces:**

- Consumes: `PredictionVenue`, `PredictionRecord`, `Sha256` from Task 1.
- Produces:
  - `AdapterImplementationState(StrEnum)`: `WATCHLIST`, `READ_ONLY`, `SHADOW`, `LIVE_DISABLED`,
    `LIVE_ELIGIBLE` (spec section 6.1's exact five values).
  - `VenueManifest(PredictionRecord)`: `schema_version: Literal[1]`, `venue: PredictionVenue`,
    `underlying_exchange: str | None`, `is_independent_liquidity: bool`, `official_sources:
    tuple[str, ...]` (non-empty), `public_capability: bool`, `authenticated_demo_capability: bool`,
    `authenticated_live_capability: bool`, `data_retention_status: Literal["permitted",
    "restricted", "unknown"]`, `automated_use_status: Literal["permitted", "restricted",
    "unknown"]`, `commercial_use_status: Literal["permitted", "restricted", "unknown"]`,
    `redistribution_status: Literal["permitted", "restricted", "unknown"]`,
    `model_training_status: Literal["permitted", "restricted", "unknown"]`,
    `implementation_state: AdapterImplementationState`, `jurisdiction_review_status:
    Literal["UNREVIEWED", "BLOCKED", "ELIGIBILITY_REVIEWED"]`, `review_identity: str`,
    `reviewed_at: datetime`, `source_hashes: tuple[Sha256, ...]` (sorted, non-empty),
    `invalidation_conditions: tuple[str, ...]`.
  - `ManifestGateReason(Literal)`: `"MANIFEST_NOT_FOUND"`, `"COLLECTION_NOT_PERMITTED"`,
    `"AUTOMATED_USE_RESTRICTED"`, `"JURISDICTION_BLOCKED"`, `"JURISDICTION_UNREVIEWED"`,
    `"LIVE_NOT_ELIGIBLE"`.
  - `ManifestGateDecision(PredictionRecord)`: `allowed: bool`, `reason: ManifestGateReason | None`,
    `venue: PredictionVenue`, `manifest_source_hashes: tuple[Sha256, ...]`.
  - `evaluate_collection_gate(manifest: VenueManifest | None, *, venue: PredictionVenue) ->
    ManifestGateDecision`: fails closed (`allowed=False`, `"MANIFEST_NOT_FOUND"`) when no manifest
    exists; requires `automated_use_status == "permitted"` and `implementation_state in
    (READ_ONLY, SHADOW, LIVE_DISABLED, LIVE_ELIGIBLE)` (not `WATCHLIST`, which never collects).
  - `evaluate_execution_gate(manifest: VenueManifest | None, *, venue: PredictionVenue) ->
    ManifestGateDecision`: additionally requires `implementation_state == LIVE_ELIGIBLE` and
    `jurisdiction_review_status == "ELIGIBILITY_REVIEWED"`; this function has no caller in
    increment 1 (no execution adapter exists yet) but is specified now so its exact fail-closed
    contract is fixed and tested before any future execution work depends on it.
- Consumers: Tasks 6, 7 (adapters gate on `evaluate_collection_gate` before opening any client),
  Task 10 (CLI `venues status`), Task 11 (dashboard venue capability panel).

- [x] **Step 1: Write failing gate-decision tests**

```python
def test_watchlist_venue_never_permits_collection() -> None:
    manifest = venue_manifest(implementation_state=AdapterImplementationState.WATCHLIST)
    decision = evaluate_collection_gate(manifest, venue=PredictionVenue.POLYMARKET)
    assert decision.allowed is False
    assert decision.reason == "COLLECTION_NOT_PERMITTED"


def test_missing_manifest_fails_closed_before_any_request() -> None:
    decision = evaluate_collection_gate(None, venue=PredictionVenue.KALSHI)
    assert decision.allowed is False
    assert decision.reason == "MANIFEST_NOT_FOUND"


def test_read_only_manifest_with_permitted_automated_use_allows_collection() -> None:
    manifest = venue_manifest(
        implementation_state=AdapterImplementationState.READ_ONLY,
        automated_use_status="permitted",
    )
    assert evaluate_collection_gate(manifest, venue=manifest.venue).allowed is True


def test_execution_gate_requires_live_eligible_and_reviewed_jurisdiction() -> None:
    manifest = venue_manifest(
        implementation_state=AdapterImplementationState.LIVE_DISABLED,
        jurisdiction_review_status="ELIGIBILITY_REVIEWED",
    )
    assert evaluate_execution_gate(manifest, venue=manifest.venue).allowed is False
    assert evaluate_execution_gate(manifest, venue=manifest.venue).reason == "LIVE_NOT_ELIGIBLE"
```

Also test restricted/unknown automated-use rejection, unreviewed/blocked jurisdiction rejection for
the execution gate, canonical (sorted, unique) `source_hashes`, non-empty `official_sources`, and
that `manifest_source_hashes` on the decision always equals the manifest's own hashes (or empty
when no manifest exists) so a decision can be audited without re-deriving it.

- [x] **Step 2: Run manifest tests and observe the missing module**

Run:

```bash
.venv/bin/python -m pytest tests/predictions/test_manifest.py -q
```

Expected: collection fails because `polytrading.predictions.manifest` does not exist.

- [x] **Step 3: Implement `VenueManifest`, gate reasons, and both gate functions**

Both gate functions are pure and take no I/O; a manifest is always loaded and passed in by the
caller (Task 4/8's storage read, or a bundled fixture at CLI/test time), never fetched internally.

- [x] **Step 4: Verify and commit Task 2**

Run:

```bash
.venv/bin/python -m pytest tests/predictions/test_manifest.py -q
.venv/bin/ruff check src/polytrading/predictions/manifest.py tests/predictions/test_manifest.py
.venv/bin/ruff format --check src/polytrading/predictions/manifest.py tests/predictions/test_manifest.py
git add src/polytrading/predictions/manifest.py tests/predictions/test_manifest.py
git commit -m "feat(predictions): define venue manifest collection and execution gates"
```

Expected: gate-decision tests pass, including the fail-closed missing-manifest and watchlist cases.

---

### Task 3: Generalize the corpus-intake source-use gate to `PredictionSource`

**Files:**

- Modify: `src/polytrading/corpus_intake/source_policy.py`
- Modify: `tests/corpus_intake/test_source_policy.py`
- Modify: every caller that constructs `IntendedUseScope`/`SourceEvidence`/`SourceUseAssessment`/
  `SourceUseApproval` with a bare `"polymarket"` literal (grep the full call-site list before
  editing; expect `src/polytrading/corpus_intake/polymarket.py`, `evidence.py`, and their tests).

**Interfaces:**

- Consumes: `PredictionSource` from Task 1.
- Produces: the same four class names and the same `evaluate_source_gate` function, with
  `source: Literal["polymarket"]` replaced by `source: PredictionSource` on
  `IntendedUseScope`, `SourceEvidence`, `SourceUseAssessment`, and `SourceUseApproval`. No other
  field, validator, or the `GateDecision`/`GateReason` vocabulary changes. This is a widening
  change only: every existing Polymarket-only call site continues to pass
  `PredictionSource.POLYMARKET` and behaves identically; Kalshi becomes a legal value for the
  first time.
- Consumers: Task 6 and Task 7's adapters do not call this module directly (Task 2's venue
  manifest is the venue-level gate per the spec's section 6.1 revision); this task exists so the
  corpus-intake review path and the venue manifest share one venue-identity vocabulary instead of
  two, per that same revision. `polytrading.corpus_intake` modules that already depend on this file
  are the only direct consumers.

- [x] **Step 1: Write failing widened-type tests**

```python
def test_source_use_scope_accepts_kalshi_and_polymarket() -> None:
    for source in (PredictionSource.POLYMARKET, PredictionSource.KALSHI):
        scope = intended_use_scope(source=source)
        assert scope.source is source


def test_source_use_scope_rejects_a_bare_string_literal() -> None:
    with pytest.raises(ValidationError):
        IntendedUseScope(**{**intended_use_scope().model_dump(), "source": "polymarket"})
```

The second test pins the exact behavior change: `source` is now a real enum member, not a bare
string literal, even though `PredictionSource.POLYMARKET.value == "polymarket"` and existing JSON
fixtures using the string `"polymarket"` still parse correctly through Pydantic's enum coercion.
Add a fixture-compatibility test confirming exactly that:

```python
def test_existing_polymarket_string_fixtures_still_parse() -> None:
    scope = IntendedUseScope.model_validate_json(POLYMARKET_FIXTURE_JSON)
    assert scope.source is PredictionSource.POLYMARKET
```

- [x] **Step 2: Run source-policy tests and observe the failure**

Run:

```bash
.venv/bin/python -m pytest tests/corpus_intake/test_source_policy.py -q
```

Expected: FAIL because `source_policy.py` still hardcodes `Literal["polymarket"]` and rejects
`PredictionSource.KALSHI`.

- [x] **Step 3: Widen the four `source` fields and their cross-checks**

Change `source: Literal["polymarket"]` to `source: PredictionSource` on all four classes. The
existing cross-field checks (`assessment.source == scope.source`, `approval.source == scope.source`)
are unchanged since they already compare by equality, not by literal identity.

- [x] **Step 4: Update every existing call site to the enum member**

Grep `corpus_intake/*.py` and `tests/corpus_intake/*.py` for `"polymarket"` used as a `source=`
argument and replace each with `PredictionSource.POLYMARKET`. Do not touch unrelated string uses
(e.g. URLs containing the word "polymarket").

- [x] **Step 5: Verify the complete corpus-intake suite and commit Task 3**

Run:

```bash
.venv/bin/python -m pytest tests/corpus_intake -q
.venv/bin/ruff check src/polytrading/corpus_intake tests/corpus_intake
.venv/bin/ruff format --check src/polytrading/corpus_intake tests/corpus_intake
git add src/polytrading/corpus_intake tests/corpus_intake
git commit -m "feat(corpus-intake): generalize source-use gate to the shared prediction-source vocabulary"
```

Expected: full existing corpus-intake suite remains green with zero behavior change for Polymarket,
plus new Kalshi-acceptance coverage.

---

### Task 4: Prediction-market storage — migration, raw-first append, cutoff-safe reads

**Files:**

- Create: `src/polytrading/predictions/storage/__init__.py`
- Create: `src/polytrading/predictions/storage/store.py`
- Create: `src/polytrading/predictions/storage/schema/001_prediction_core.sql`
- Create: `tests/predictions/test_store.py`
- Modify: `pyproject.toml` (`[tool.setuptools.package-data]` — add
  `"polytrading.predictions.storage.schema" = ["*.sql"]`, matching the existing
  `"polytrading.storage.schema" = ["*.sql"]` entry)

**Interfaces:**

- Consumes: every model from Task 1 and `VenueManifest` from Task 2.
- Produces:
  - `PredictionMarketStore(path: Path, *, read_only: bool = False)`, matching
    `polytrading.storage.store.DuckDBStore`'s constructor shape exactly but connecting to its own
    migration sequence under `polytrading.predictions.storage.schema` — never the existing
    `polytrading.storage.schema` package.
  - `.close()`, `.transaction() -> ContextManager[PredictionMarketStore]` (identical
    begin/commit/rollback/no-nested-transactions semantics to the existing store).
  - `.append_raw(record: PredictionRawEnvelope) -> bool`,
    `.append_venue_manifest(record: VenueManifest) -> bool`,
    `.append_market(record: MarketRecord) -> bool`,
    `.append_rule_version(record: RuleVersion) -> bool`,
    `.append_trade(record: TradeRecord) -> bool`,
    `.append_book_snapshot(record: PredictionBookSnapshot) -> bool`,
    `.append_fee_rate(record: PredictionFeeRate) -> bool` — each with the existing
    identical-retry-returns-`False`/conflicting-content-raises-`ConflictingRecordError` contract.
  - `.latest_venue_manifest_as_of(venue: PredictionVenue, as_of: datetime) -> VenueManifest | None`.
  - `.markets_as_of(venue: PredictionVenue, as_of: datetime) -> tuple[MarketRecord, ...]` (one row
    per `market_id`: the latest `rule_version_id` known by `as_of`, never a later revision).
  - `.rule_versions_for_market(market_id: str, as_of: datetime) -> tuple[RuleVersion, ...]`
    (full immutable version history up to the cutoff, oldest first).
  - `.latest_book_as_of(venue, market_id, outcome_token_id, as_of) -> PredictionBookSnapshot | None`.
  - `.trades_between(venue, market_id, start, end, known_as_of) -> tuple[TradeRecord, ...]`.
  - `.latest_fee_rate_as_of(venue, market_id, as_of) -> PredictionFeeRate | None`.
  - `.evidence_counts_as_of(as_of: datetime) -> dict[str, int]` (mirrors the existing store's
    evidence-count convention for dashboard/health consumption).
- Consumers: Tasks 6-11.

- [x] **Step 1: Write failing schema and round-trip tests**

```python
def test_current_schema_contains_prediction_core_tables(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    tables = {row[0] for row in store._connection.execute("SHOW TABLES").fetchall()}
    store.close()
    assert {
        "prediction_raw_envelopes", "venue_manifests", "markets", "rule_versions",
        "trades", "prediction_books", "prediction_fee_rates", "schema_migrations",
    } <= tables


def test_raw_envelope_round_trip_is_idempotent_and_conflict_safe(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    envelope = raw_envelope()

    assert store.append_raw(envelope) is True
    assert store.append_raw(envelope) is False
    with pytest.raises(ConflictingRecordError):
        store.append_raw(envelope.model_copy(update={"payload_json": "different"}))
```

Also assert: read-only opening rejects a database at an older/newer schema version than currently
installed (mirroring the existing store's `_verify_current_schema` test); a fresh
`predictions.duckdb` never contains any table name from `polytrading.storage.schema`'s existing
perpetual-futures migrations, proving the two systems are structurally disjoint even if opened via
the same DuckDB engine.

- [x] **Step 2: Run storage tests and observe the missing module**

Run:

```bash
.venv/bin/python -m pytest tests/predictions/test_store.py -q
```

Expected: collection fails because `polytrading.predictions.storage.store` does not exist.

- [x] **Step 3: Add migration 001 and the store's connect/migrate/transaction scaffold**

Copy the exact `DuckDBStore.__init__`/`.close()`/`.transaction()`/`._apply_migrations()`/
`._verify_current_schema()`/`._migration_entries()`/`._applied_migration_versions()` structure from
`polytrading.storage.store`, pointed at `polytrading.predictions.storage.schema` instead. Define:

```sql
CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL);

CREATE TABLE prediction_raw_envelopes (
    event_id UUID PRIMARY KEY,
    venue VARCHAR NOT NULL,
    endpoint VARCHAR NOT NULL,
    venue_timestamp TIMESTAMPTZ,
    observed_at TIMESTAMPTZ NOT NULL,
    received_monotonic_ns BIGINT NOT NULL,
    request_latency_ms DECIMAL(18,6) NOT NULL,
    source_version VARCHAR NOT NULL,
    payload_json VARCHAR NOT NULL,
    source_hash VARCHAR NOT NULL,
    schema_version INTEGER NOT NULL
);

CREATE TABLE venue_manifests (
    venue VARCHAR NOT NULL,
    reviewed_at TIMESTAMPTZ NOT NULL,
    record_json JSON NOT NULL,
    record_hash VARCHAR NOT NULL,
    PRIMARY KEY (venue, reviewed_at)
);

CREATE TABLE markets (
    venue VARCHAR NOT NULL,
    market_id VARCHAR NOT NULL,
    rule_version_id UUID NOT NULL,
    retrieved_at TIMESTAMPTZ NOT NULL,
    record_json JSON NOT NULL,
    record_hash VARCHAR NOT NULL,
    PRIMARY KEY (venue, market_id, rule_version_id)
);

CREATE TABLE rule_versions (
    rule_version_id UUID PRIMARY KEY,
    venue VARCHAR NOT NULL,
    market_id VARCHAR NOT NULL,
    effective_at TIMESTAMPTZ NOT NULL,
    record_json JSON NOT NULL,
    record_hash VARCHAR NOT NULL
);

CREATE TABLE trades (
    venue VARCHAR NOT NULL,
    market_id VARCHAR NOT NULL,
    trade_id VARCHAR NOT NULL,
    effective_at TIMESTAMPTZ NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    record_json JSON NOT NULL,
    record_hash VARCHAR NOT NULL,
    PRIMARY KEY (venue, market_id, trade_id)
);

CREATE TABLE prediction_books (
    cycle_id UUID NOT NULL,
    venue VARCHAR NOT NULL,
    market_id VARCHAR NOT NULL,
    outcome_token_id VARCHAR,
    observed_at TIMESTAMPTZ NOT NULL,
    record_json JSON NOT NULL,
    record_hash VARCHAR NOT NULL,
    PRIMARY KEY (cycle_id, venue, market_id, outcome_token_id)
);

CREATE TABLE prediction_fee_rates (
    venue VARCHAR NOT NULL,
    market_id VARCHAR,
    observed_at TIMESTAMPTZ NOT NULL,
    record_json JSON NOT NULL,
    record_hash VARCHAR NOT NULL,
    PRIMARY KEY (venue, market_id, observed_at)
);
```

Every append method follows the existing store's exact pattern: look up by primary key, compare
the full reconstructed record for an exact-content check, return `False` on an identical retry,
raise `ConflictingRecordError` on divergent content for the same key, otherwise insert and return
`True`. Store each record's canonical `record_json`/`record_hash` alongside its indexed key columns
(mirroring how `append_book_collection_cycle` etc. already do this in the existing store) rather
than exploding every field into its own column, since these records are read back whole far more
often than queried by an inner field.

- [x] **Step 4: Write failing cutoff-safe read tests**

```python
def test_markets_as_of_never_leaks_a_later_rule_version(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    seed_two_rule_versions_for_one_market(store)

    early = store.markets_as_of(PredictionVenue.POLYMARKET, EARLY_CUTOFF)
    late = store.markets_as_of(PredictionVenue.POLYMARKET, LATE_CUTOFF)

    assert early[0].rule_version_id == FIRST_RULE_VERSION_ID
    assert late[0].rule_version_id == SECOND_RULE_VERSION_ID
```

Also test: `latest_venue_manifest_as_of` excludes a manifest reviewed after the cutoff;
`trades_between` excludes a trade whose `observed_at` (not `effective_at`) is after `known_as_of`,
matching the existing store's revision-safety convention; `latest_book_as_of` and
`latest_fee_rate_as_of` reject a future-observed row; `evidence_counts_as_of` sums exactly the
seven table counts as of the cutoff.

- [x] **Step 5: Implement the cutoff-safe readers**

Use the same `WHERE ... <= ?` cutoff-filtering and `ORDER BY ... DESC LIMIT 1`-per-key pattern
already used throughout `polytrading.storage.store`'s readers (e.g. `latest_instrument_as_of`,
`reviewed_fee_schedules_as_of`).

- [x] **Step 6: Verify and commit Task 4**

Run:

```bash
.venv/bin/python -m pytest tests/predictions/test_store.py -q
.venv/bin/ruff check src/polytrading/predictions/storage tests/predictions/test_store.py
.venv/bin/ruff format --check src/polytrading/predictions/storage tests/predictions/test_store.py
git add src/polytrading/predictions/storage pyproject.toml tests/predictions/test_store.py
git commit -m "feat(predictions): persist prediction-market evidence raw-first"
```

Expected: schema, round-trip, conflict, and cutoff-safety tests pass; no perpetual-futures table
name appears in a fresh prediction-market database.

---

### Task 5: Shared adapter protocol, batch, and lineage validation

**Files:**

- Create: `src/polytrading/predictions/adapter.py`
- Create: `tests/predictions/test_adapter.py`
- Modify: `pyproject.toml` (`[project.dependencies]` — add a pinned WebSocket client; see Step 3)

**Interfaces:**

- Consumes: Task 1 models, Task 2's `evaluate_collection_gate`/`VenueManifest`.
- Produces:
  - `type PredictionNormalizedRecord = MarketRecord | RuleVersion | TradeRecord |
    PredictionBookSnapshot | PredictionFeeRate`.
  - `PredictionAdapterBatchIntegrityError(ValueError)` with a `.code` attribute, mirroring
    `polytrading.venues.public.AdapterBatchIntegrityError`.
  - `PredictionAdapterWarning` frozen dataclass: `code: str`, `venue: PredictionVenue`,
    `endpoint: str`, `market_id: str`, `message: str`.
  - `PredictionAdapterBatch` frozen dataclass: `raw: tuple[PredictionRawEnvelope, ...]`,
    `normalized: tuple[PredictionNormalizedRecord, ...]`,
    `warnings: tuple[PredictionAdapterWarning, ...] = ()`.
  - `validate_prediction_adapter_batch(batch: PredictionAdapterBatch) -> None`: verifies every raw
    envelope's `source_hash` against its exact UTF-8 `payload_json`, and every normalized record's
    `(venue, source_hash)` against the batch's own raw lineage — identical rules to
    `polytrading.venues.public.validate_adapter_batch`, reimplemented for the prediction domain's
    type union.
  - `class PredictionVenueAdapter(Protocol)`: `venue: PredictionVenue`; `async def
    fetch_manifest_gated(self, manifest: VenueManifest) -> None` (raises
    `PredictionCollectionGateError` if `evaluate_collection_gate` disallows collection — every
    concrete adapter method below calls this first and constructs no HTTP client if it raises);
    `async def fetch_markets(self, *, information_cutoff: datetime) -> PredictionAdapterBatch`
    (markets + their current rule version); `async def fetch_book_snapshot(self, market_id: str,
    outcome_token_id: str | None, observed_at: datetime, cycle_id: UUID) ->
    PredictionAdapterBatch`; `async def fetch_trades(self, market_id: str, start: datetime, end:
    datetime, observed_at: datetime) -> PredictionAdapterBatch`; `async def fetch_fee_rate(self,
    market_id: str | None, observed_at: datetime) -> PredictionAdapterBatch`.
  - `PredictionCollectionGateError(RuntimeError)`.
- Consumers: Tasks 6, 7 implement this Protocol; Tasks 9, 10 consume it generically.

- [x] **Step 1: Write failing lineage-validation tests**

Mirror the existing venue-public test shape exactly, substituting prediction types:

```python
def test_validate_prediction_adapter_batch_rejects_hash_mismatch() -> None:
    batch = PredictionAdapterBatch(raw=(raw_envelope(payload_json="{}"),), normalized=())
    tampered = replace(batch.raw[0], source_hash="0" * 64)
    with pytest.raises(PredictionAdapterBatchIntegrityError, match="raw_source_hash_mismatch"):
        validate_prediction_adapter_batch(PredictionAdapterBatch(raw=(tampered,), normalized=()))


def test_validate_prediction_adapter_batch_rejects_orphaned_normalized_lineage() -> None:
    raw = raw_envelope()
    orphan_market = market_record(venue=raw.venue, raw_hash="f" * 64)
    with pytest.raises(PredictionAdapterBatchIntegrityError, match="normalized_lineage_mismatch"):
        validate_prediction_adapter_batch(
            PredictionAdapterBatch(raw=(raw,), normalized=(orphan_market,))
        )
```

- [x] **Step 2: Run adapter tests and observe the missing module**

Run:

```bash
.venv/bin/python -m pytest tests/predictions/test_adapter.py -q
```

Expected: collection fails because `polytrading.predictions.adapter` does not exist.

- [x] **Step 3: Choose and pin the Polymarket CLOB WebSocket dependency**

The existing project has no WebSocket client dependency (its perpetual-futures adapters are
REST-only). Polymarket's CLOB market channel (spec section 19.1) requires one for continuity
reconciliation in Task 6. Add exactly one new pinned runtime dependency to `pyproject.toml`:
`websockets==<pin the current stable release at implementation time>` — a small, dependency-free,
asyncio-native client already common in this ecosystem. Do not add a heavier full-exchange SDK; this
project's existing adapters are all hand-rolled against raw REST/WebSocket endpoints and this
should follow the same convention. Record the exact chosen version in the commit message.

- [x] **Step 4: Implement the batch, warning, integrity validator, and Protocol**

Reuse the existing `polytrading.venues.public` validator's exact two-check structure (raw hash
self-consistency, then normalized-to-raw lineage), rewritten against
`PredictionNormalizedRecord`'s five-member union instead of the perpetual-futures four-member union.

- [x] **Step 5: Verify and commit Task 5**

Run:

```bash
.venv/bin/python -m pytest tests/predictions/test_adapter.py -q
.venv/bin/ruff check src/polytrading/predictions/adapter.py tests/predictions/test_adapter.py
.venv/bin/ruff format --check src/polytrading/predictions/adapter.py tests/predictions/test_adapter.py
git add src/polytrading/predictions/adapter.py pyproject.toml tests/predictions/test_adapter.py
git commit -m "feat(predictions): define the shared adapter protocol and lineage validator"
```

Expected: lineage-validation tests pass; `pip install -e '.[dev]'` resolves cleanly with the new
WebSocket dependency pinned.

---

### Task 6: Committed Polymarket adapter

**Files:**

- Create: `src/polytrading/predictions/polymarket.py`
- Create: `tests/fixtures/predictions/polymarket/gamma_markets_page_1.json`
- Create: `tests/fixtures/predictions/polymarket/clob_book.json`
- Create: `tests/fixtures/predictions/polymarket/clob_market_channel_book_update.json`
- Create: `tests/fixtures/predictions/polymarket/fee_rate.json`
- Create: `tests/predictions/test_polymarket.py`

**Interfaces:**

- Consumes: Task 1 models, Task 2 gate, Task 5 `PredictionVenueAdapter`/`PredictionAdapterBatch`.
- Produces: `PolymarketAdapter(client: httpx.AsyncClient, websocket_factory: ...,
  wall_clock, monotonic_ns)` implementing `PredictionVenueAdapter` with `venue =
  PredictionVenue.POLYMARKET`.
- Consumers: Tasks 9 (health), 10 (CLI `predictions collect polymarket`).

Before writing fixtures, fetch one live, current example response from each cited endpoint in spec
section 19.1 (Gamma `/markets`, CLOB `/book`, the CLOB market-channel WebSocket, and the fee-rate
endpoint) and confirm exact field names/casing/nesting against that live response — the field names
below are grounded in Polymarket's public documentation and known integrations as of this plan's
writing, but a live check before the first fixture is committed is mandatory, since third-party API
documentation drifts.

- [x] **Step 1: Write failing markets/rule-version parsing tests against a recorded fixture**

Record one real Gamma `/markets` page (redact nothing but authentication, since this is a public
endpoint) as `gamma_markets_page_1.json`. Test:

```python
def test_fetch_markets_normalizes_gamma_page_into_market_and_rule_version() -> None:
    adapter = make_adapter(handler_returning(GAMMA_FIXTURE))
    batch = asyncio.run(adapter.fetch_markets(information_cutoff=CUTOFF))

    market = next(item for item in batch.normalized if isinstance(item, MarketRecord))
    assert market.venue is PredictionVenue.POLYMARKET
    assert market.market_id  # exact conditionId from the fixture
    assert market.outcome_token_ids is not None and len(market.outcome_token_ids) == len(
        market.outcomes
    )
    assert market.negative_risk in (True, False)
    rule_version = next(item for item in batch.normalized if isinstance(item, RuleVersion))
    assert rule_version.market_id == market.market_id
```

Also test: `outcomes`/`outcomePrices`/`clobTokenIds` are Polymarket-documented as *stringified*
JSON arrays inside the outer JSON object — the adapter must `json.loads` that inner string exactly
once and fail closed (not silently drop the market) if that inner parse fails or the three arrays'
lengths disagree; `active`/`closed`/`archived`/`enableOrderBook` map to `MarketRecord`'s
`active`/`closed`/`order_book_enabled` (an archived-but-not-closed market is `restricted=True`);
pagination follows Gamma's own cursor/offset convention (confirm the exact parameter name against
the live docs) with the same stalled-pagination guard already used by
`BybitPublicAdapter.fetch_instruments`; a market missing `conditionId` or with duplicate
`conditionId` values on one page fails closed with a named error, never silently deduplicated.

- [x] **Step 2: Run and observe the missing module**

Run:

```bash
.venv/bin/python -m pytest tests/predictions/test_polymarket.py -q -k markets
```

Expected: collection fails because `polytrading.predictions.polymarket` does not exist.

- [x] **Step 3: Implement `fetch_markets`**

Gate on `evaluate_collection_gate` first (Task 2); construct no client if disallowed. Page through
Gamma `/markets`, parse the two inner stringified-JSON arrays exactly once each, construct one
`MarketRecord` and one `RuleVersion` per market with `rule_version_id` deterministically derived
(e.g. `uuid5` over `venue:market_id:raw_hash`, following the existing `uuid5(NAMESPACE_URL, ...)`
convention already used in `src/polytrading/ai/cli.py`), and set `negative_risk` from `negRisk`/
`neg_risk` (confirm exact key name against the live response — sources disagree on casing).

- [x] **Step 4: Write failing CLOB REST book-snapshot tests**

Record one real `GET /book?token_id=<id>` response. Test exact bid/ask ordering, non-crossed-book
validation reuse from Task 1, and that a market lacking `enableOrderBook` is never requested for a
book snapshot (the adapter must check this itself, not merely trust the caller).

- [x] **Step 5: Implement `fetch_book_snapshot` (REST path)**

- [x] **Step 6: Write failing CLOB WebSocket continuity tests**

Using the `websockets` dependency from Task 5, test: a `book` channel message updates the maintained
in-memory book; a heartbeat/ping timeout or unexpected close forces a fresh REST snapshot rather
than silently continuing a stale book (spec section 6.3's WebSocket-vs-REST reconciliation rule); an
out-of-order or gapped sequence invalidates the affected interval rather than being carried forward,
mirroring the existing Bybit/Hyperliquid book-continuity tests' structure.

- [x] **Step 7: Implement WebSocket continuity reconciliation**

- [x] **Step 8: Write failing fee-rate tests and implement `fetch_fee_rate`**

Record one real fee-rate response; test that a per-token fee is stored with `market_id` set and a
venue-wide fallback (if the documented endpoint returns one) is stored with `market_id=None`.

- [x] **Step 9: Add gate-rejection and collection-context tests**

Assert `fetch_markets`/`fetch_book_snapshot`/`fetch_trades`/`fetch_fee_rate` each raise
`PredictionCollectionGateError` and open no `httpx`/`websockets` connection when the manifest's
`evaluate_collection_gate` disallows collection (e.g. `WATCHLIST`), matching the existing
adapters' collection-context guard pattern.

- [x] **Step 10: Verify and commit Task 6**

Run:

```bash
.venv/bin/python -m pytest tests/predictions/test_polymarket.py -q
.venv/bin/ruff check src/polytrading/predictions/polymarket.py tests/predictions/test_polymarket.py
.venv/bin/ruff format --check src/polytrading/predictions/polymarket.py tests/predictions/test_polymarket.py
git add src/polytrading/predictions/polymarket.py tests/fixtures/predictions/polymarket tests/predictions/test_polymarket.py
git commit -m "feat(predictions): collect committed Polymarket public evidence"
```

Expected: markets, book (REST and WebSocket), and fee-rate tests pass against recorded fixtures.

---

### Task 7: Committed Kalshi adapter

**Files:**

- Create: `src/polytrading/predictions/kalshi.py`
- Create: `tests/fixtures/predictions/kalshi/markets_page_1.json`
- Create: `tests/fixtures/predictions/kalshi/historical_cutoff.json`
- Create: `tests/fixtures/predictions/kalshi/historical_markets_page_1.json`
- Create: `tests/fixtures/predictions/kalshi/orderbook.json`
- Create: `tests/predictions/test_kalshi.py`

**Interfaces:**

- Consumes: Task 1 models, Task 2 gate, Task 5 `PredictionVenueAdapter`/`PredictionAdapterBatch`.
- Produces: `KalshiAdapter(client: httpx.AsyncClient, wall_clock, monotonic_ns)` implementing
  `PredictionVenueAdapter` with `venue = PredictionVenue.KALSHI`. Kalshi's `negative_risk` is
  always `None` on every `MarketRecord` this adapter produces, and its own multivariate/event-group
  mechanics (spec section 4.2) are carried in a Kalshi-specific extension the plan does not need to
  design for increment 1 (no engine consumes it yet); record `event_ticker`/`series_ticker` in
  `MarketRecord.event_id`/`underlying_exchange` respectively so the information is retained, not
  discarded, even though it isn't yet interpreted.
- Consumers: Tasks 9 (health), 10 (CLI `predictions collect kalshi`).

Kalshi's public market-data API separates live data (target retention: 3 months) from historical
data behind `GET /historical/cutoff`, which returns per-resource cutoff timestamps including
`market_settled_ts` and `trades_created_ts`. Confirm the exact response field names against
`https://docs.kalshi.com/getting_started/historical_data` before writing fixtures, since this is
the single most safety-critical piece of this adapter (spec section 6.3: "Kalshi live and
historical partitions are joined at the official cutoff without duplicating or dropping records").

- [x] **Step 1: Write failing markets tests against a recorded fixture**

Record one real public markets-list response. Test market/rule-version normalization analogous to
Task 6 Step 1, using Kalshi's own field names (`ticker`, `event_ticker`, `series_ticker`, `title`,
`status`, `yes_bid`/`yes_ask` or the exact documented current-price field names, `open_time`,
`close_time`, `expiration_time`). Assert `negative_risk` is always `None`.

- [x] **Step 2: Run and observe the missing module**

Run:

```bash
.venv/bin/python -m pytest tests/predictions/test_kalshi.py -q -k markets
```

Expected: collection fails because `polytrading.predictions.kalshi` does not exist.

- [x] **Step 3: Implement `fetch_markets` against the live-data endpoint**

- [x] **Step 4: Write failing historical-partition join tests**

This is the core safety property of this adapter:

```python
def test_join_never_duplicates_a_market_settled_exactly_at_the_cutoff() -> None:
    adapter = make_adapter(fixed_cutoff_handler(market_settled_ts=CUTOFF))
    batch = asyncio.run(adapter.fetch_markets(information_cutoff=CUTOFF + timedelta(minutes=5)))

    settled_market_ids = [m.market_id for m in normalized_markets(batch) if m.closed]
    assert len(settled_market_ids) == len(set(settled_market_ids))


def test_join_never_drops_a_market_settled_one_second_before_the_cutoff() -> None:
    ...


def test_join_routes_strictly_by_the_returned_cutoff_not_a_hardcoded_window() -> None:
    # A cutoff the adapter has never seen before (simulating Kalshi moving its retention window)
    # must still be honored exactly, proving the adapter reads `GET /historical/cutoff` on every
    # invocation rather than assuming a fixed 3-month boundary.
    ...
```

Also test: a market whose settlement lands exactly at `market_settled_ts` is requested from
exactly one of the two endpoints, never both (the adapter must pick a side of the boundary and
document which — e.g. treat the cutoff as "historical if settled at or before, live otherwise" —
and this choice must be a single named constant, not duplicated logic in two places); a
`GET /historical/cutoff` request failure aborts the whole `fetch_markets` call rather than silently
falling back to live-only data, since silently narrowing scope after previously routing wider would
itself create a duplicate/drop risk on the next run.

- [x] **Step 5: Implement the cutoff-routed join**

Fetch `GET /historical/cutoff` first on every `fetch_markets`/`fetch_trades` call (no internal
caching across calls, so a moved cutoff is always honored); route each requested time range to
`/historical/*` or the live endpoint by comparing against the returned cutoff; merge results
without deduplication logic beyond the single boundary rule above (the API contract, once correctly
routed, should not produce overlapping records — if it does, fail closed with a named error rather
than silently deduplicating, since a real duplicate is exactly the kind of integrity problem this
system must surface, not hide).

- [x] **Step 6: Write failing order-book tests and implement `fetch_book_snapshot`**

Kalshi's public order book is REST-only (no documented public WebSocket for market data as of this
plan's writing — confirm this before implementation, since it changes whether Task 9's continuity
health has a WebSocket-gap dimension for Kalshi at all). Test bid/ask ordering and non-crossed-book
reuse from Task 1, and that Kalshi's own two-sided (YES/NO) book representation is normalized into
this system's single `bids`/`asks` shape per outcome token without inventing a synthetic price the
venue did not provide.

- [x] **Step 7: Write failing trades and fee tests, implement `fetch_trades`/`fetch_fee_rate`**

Kalshi publishes maker/taker fee schedules in its documentation rather than a live per-market fee
endpoint in all cases — confirm the exact documented mechanism at implementation time and adjust
`fetch_fee_rate`'s source (API call vs. a reviewed static schedule reference) accordingly; if no
live endpoint exists, this method may return an empty batch with a structured
`PredictionAdapterWarning` rather than fabricate a rate, exactly as the existing dYdX/Lighter
adapters emit structured warnings for fields their APIs do not expose (e.g.
`DYDX_MARK_PRICE_UNAVAILABLE`).

- [x] **Step 8: Add gate-rejection tests matching Task 6 Step 9**

- [x] **Step 9: Verify and commit Task 7**

Run:

```bash
.venv/bin/python -m pytest tests/predictions/test_kalshi.py -q
.venv/bin/ruff check src/polytrading/predictions/kalshi.py tests/predictions/test_kalshi.py
.venv/bin/ruff format --check src/polytrading/predictions/kalshi.py tests/predictions/test_kalshi.py
git add src/polytrading/predictions/kalshi.py tests/fixtures/predictions/kalshi tests/predictions/test_kalshi.py
git commit -m "feat(predictions): collect committed Kalshi public evidence"
```

Expected: markets, historical-partition join, book, and trades/fee tests pass; the join tests in
particular must demonstrably never duplicate or drop a boundary-adjacent record.

---

### Task 8: Immutable market/rule registry read layer

**Files:**

- Create: `src/polytrading/predictions/registry.py`
- Create: `tests/predictions/test_registry.py`

**Interfaces:**

- Consumes: `PredictionMarketStore` from Task 4.
- Produces:
  - `PredictionRegistry(store: PredictionMarketStore)`.
  - `.market_as_of(venue: PredictionVenue, market_id: str, as_of: datetime) -> MarketRecord | None`.
  - `.rule_history(venue: PredictionVenue, market_id: str, as_of: datetime) ->
    tuple[RuleVersion, ...]` (oldest first, never including a version whose `effective_at` is
    after `as_of`).
  - `.markets_by_venue_as_of(venue: PredictionVenue, as_of: datetime) -> tuple[MarketRecord, ...]`
    (sorted by `market_id` for stable iteration).
  - `.has_rule_changed_since(venue, market_id, known_rule_version_id, as_of) -> bool` — the exact
    invalidation primitive spec section 6.2 requires ("a changed question, description, resolution
    source, outcome set, market grouping, or critical flag creates a new immutable version").
  - This layer performs typed read-only queries only; per spec section 11.2 ("never performs
    semantic inference"), it must contain no ranking, scoring, matching, or text-similarity logic
    of any kind — that is explicitly out of scope for increment 1 and belongs to a later semantic-
    scout increment.
- Consumers: Task 9 (health cross-checks rule continuity), Task 11 (dashboard market/rule panel).

- [x] **Step 1: Write failing point-in-time and invalidation tests**

```python
def test_rule_history_never_includes_a_version_effective_after_the_cutoff(...) -> None:
    ...


def test_has_rule_changed_since_is_true_only_for_a_genuinely_new_version(...) -> None:
    registry = PredictionRegistry(seeded_store_with_two_rule_versions())
    assert registry.has_rule_changed_since(
        PredictionVenue.POLYMARKET, MARKET_ID, FIRST_RULE_VERSION_ID, LATE_CUTOFF
    ) is True
    assert registry.has_rule_changed_since(
        PredictionVenue.POLYMARKET, MARKET_ID, SECOND_RULE_VERSION_ID, LATE_CUTOFF
    ) is False
```

- [x] **Step 2: Run and observe the missing module**

Run:

```bash
.venv/bin/python -m pytest tests/predictions/test_registry.py -q
```

Expected: collection fails because `polytrading.predictions.registry` does not exist.

- [x] **Step 3: Implement the registry as a thin typed layer over Task 4's readers**

No new SQL beyond what Task 4 already exposes; this class composes those readers into the exact
named queries above and adds no caching, since every call must reflect the current `as_of` cutoff
precisely.

- [x] **Step 4: Verify and commit Task 8**

Run:

```bash
.venv/bin/python -m pytest tests/predictions/test_registry.py -q
.venv/bin/ruff check src/polytrading/predictions/registry.py tests/predictions/test_registry.py
.venv/bin/ruff format --check src/polytrading/predictions/registry.py tests/predictions/test_registry.py
git add src/polytrading/predictions/registry.py tests/predictions/test_registry.py
git commit -m "feat(predictions): expose the immutable market and rule registry"
```

Expected: point-in-time and invalidation-primitive tests pass.

---

### Task 9: Per-venue continuity and collection health

**Files:**

- Create: `src/polytrading/predictions/health.py`
- Create: `src/polytrading/predictions/health_report.py`
- Create: `tests/predictions/test_health.py`
- Create: `tests/predictions/test_health_report.py`

**Interfaces:**

- Consumes: `PredictionMarketStore`, `PredictionRegistry`, `VenueManifest`/gate decisions.
- Produces:
  - `VenueEvidenceStatus(StrEnum)`: `NOT_COLLECTED`, `STALE`, `DEGRADED`, `CURRENT`.
  - `VenueHealth(PredictionRecord)`: `schema_version: Literal[1]`, `venue: PredictionVenue`,
    `manifest_state: AdapterImplementationState | None`, `collection_gate: ManifestGateDecision`,
    `market_count: NonnegativeInt`, `latest_market_retrieved_at: datetime | None`,
    `latest_book_observed_at: datetime | None`, `latest_book_age_seconds:
    Annotated[Decimal, Field(ge=0, allow_inf_nan=False)] | None`, `websocket_gap_count:
    NonnegativeInt` (Task 6's WebSocket reconciliation increments a stored counter; `0` for a venue
    with no public WebSocket, e.g. Kalshi per Task 7), `status: VenueEvidenceStatus`,
    `reason_codes: tuple[str, ...]`.
  - `PredictionHealthReport(PredictionRecord)`: `schema_version: Literal[1]`, `as_of: datetime`,
    `venues: tuple[VenueHealth, ...]` (canonical `POLYMARKET`, `KALSHI` order), `warnings:
    tuple[str, str, str]` (the same three research-only/no-authority/no-credentials warnings used
    throughout the existing perpetual-futures trial/funding health reports, reworded for
    prediction markets).
  - `PredictionHealthAuditor(store: PredictionMarketStore).audit(as_of: datetime) ->
    PredictionHealthReport` — read-only, opens no network client.
  - `render_prediction_health_text(report) -> str`, `render_prediction_health_json(report) -> str`.
- Consumers: Task 10 (CLI `predictions health`), Task 11 (dashboard venue-health panel).

- [x] **Step 1: Write failing status-classification tests**

```python
def test_venue_with_no_evidence_is_not_collected() -> None:
    report = PredictionHealthAuditor(empty_store()).audit(AS_OF)
    assert all(v.status is VenueEvidenceStatus.NOT_COLLECTED for v in report.venues)


def test_stale_book_evidence_degrades_status() -> None:
    store = seeded_store_with_book_age(hours=6)
    report = PredictionHealthAuditor(store).audit(AS_OF)
    polymarket = next(v for v in report.venues if v.venue is PredictionVenue.POLYMARKET)
    assert polymarket.status is VenueEvidenceStatus.STALE
    assert "BOOK_STALE" in polymarket.reason_codes


def test_watchlist_venue_reports_gate_reason_not_a_data_gap() -> None:
    report = PredictionHealthAuditor(store_with_watchlist_manifest()).audit(AS_OF)
    kalshi = next(v for v in report.venues if v.venue is PredictionVenue.KALSHI)
    assert kalshi.collection_gate.allowed is False
    assert kalshi.status is VenueEvidenceStatus.NOT_COLLECTED
```

Pick and document exact staleness thresholds now (e.g. book age over some fixed number of minutes
is `STALE`, over a larger fixed number is `DEGRADED`) as named constants, matching this codebase's
convention of never leaving a threshold implicit; do not reuse the perpetual-futures 30-second/
five-minute constants without re-justifying them for prediction-market book collection cadence.

- [x] **Step 2: Run and observe the missing module**

Run:

```bash
.venv/bin/python -m pytest tests/predictions/test_health.py -q
```

Expected: collection fails because `polytrading.predictions.health` does not exist.

- [x] **Step 3: Implement the auditor**

For each of the two venues, load the latest manifest and gate decision, count markets and latest
book/market timestamps as of the cutoff, and classify status conservatively: a manifest gate
rejection is never conflated with a data gap (different reason vocabulary, per the second test
above); genuinely missing evidence beneath a permitted gate is `NOT_COLLECTED`; present but stale
evidence is `STALE`/`DEGRADED` by the named thresholds; otherwise `CURRENT`.

- [x] **Step 4: Write failing renderer tests**

Assert canonical two-space sorted JSON, RFC 3339 `Z` timestamps, stable venue order, and the exact
three warnings; assert neither renderer contains an uppercase `APPROVED`, `LIVE_ELIGIBLE`, or a
return/profit claim, matching the existing report renderers' forbidden-string tests.

- [x] **Step 5: Implement the renderers**

- [x] **Step 6: Verify and commit Task 9**

Run:

```bash
.venv/bin/python -m pytest tests/predictions/test_health.py tests/predictions/test_health_report.py -q
.venv/bin/ruff check src/polytrading/predictions/health.py src/polytrading/predictions/health_report.py tests/predictions/test_health.py tests/predictions/test_health_report.py
.venv/bin/ruff format --check src/polytrading/predictions/health.py src/polytrading/predictions/health_report.py tests/predictions/test_health.py tests/predictions/test_health_report.py
git add src/polytrading/predictions/health.py src/polytrading/predictions/health_report.py tests/predictions/test_health.py tests/predictions/test_health_report.py
git commit -m "feat(predictions): audit per-venue collection and continuity health"
```

Expected: classification and renderer tests pass.

---

### Task 10: `predictions` CLI command group

**Files:**

- Create: `src/polytrading/predictions/cli.py`
- Modify: `src/polytrading/cli.py` (register the `predictions` subparser group and dispatch)
- Modify: `tests/test_cli.py`

**Interfaces:**

- Consumes: Tasks 2 (manifest), 4 (store), 6/7 (adapters), 9 (health).
- Produces, registered under a new top-level `predictions` subparser with its own
  `predictions_command` dest (mirroring `trial`/`carry`'s `add_subparsers(dest=...,
  required=True)` pattern) and a nested `predictions_collect_command` dest under `collect`:
  - `predictions venues status --db <path> [--format text|json]`: opens the store read-only (no
    network), renders each venue's latest manifest and gate decision. Works even before any
    manifest has ever been collected (reports `MANIFEST_NOT_FOUND` for each venue) so an operator
    can run this as the very first command against a fresh database.
  - `predictions collect polymarket --db <path>` and `predictions collect kalshi --db <path>`:
    each is its own subcommand (not a `--venue` flag), matching the existing `collect
    public|funding-cycle|books|corpus|source-use|review-queue` subcommand-tree convention. Each
    acquires a writer lease (reuse `polytrading.trial.writer_lease.database_writer_lease` — it is
    already venue-neutral, keyed only on the database path, so it needs no prediction-specific
    fork) before opening `PredictionMarketStore`, gates on the current manifest via
    `evaluate_collection_gate`, and exits `2` with a typed message before any network request if
    the gate disallows collection (e.g. Kalshi is still `WATCHLIST`).
  - `predictions health --db <path> [--as-of <UTC>] [--format text|json]`: opens the store
    read-only, calls `PredictionHealthAuditor.audit`, same exit-code convention as the existing
    `funding health`/`trial health` commands (`0` healthy, `1` degraded, `2` invalid input or
    unavailable database).
  - Wire all three into `src/polytrading/cli.py`'s `build_parser()`/`main()` under `if
    arguments.command == "predictions": ...`, alongside the existing `carry`/`trial`/`ai` blocks.
- Consumers: the operator; Task 11's dashboard does not go through this CLI layer (it calls the
  same underlying store/health/registry APIs directly, exactly as the existing dashboard does).

- [x] **Step 1: Write failing parser-shape tests**

```python
def test_predictions_collect_is_a_subcommand_tree_not_a_venue_flag() -> None:
    parsed = build_parser().parse_args([
        "predictions", "collect", "polymarket", "--db", "var/predictions.duckdb",
    ])
    assert parsed.command == "predictions"
    assert parsed.predictions_command == "collect"
    assert parsed.predictions_collect_command == "polymarket"
    assert not hasattr(parsed, "venue")


def test_predictions_command_does_not_collide_with_existing_top_level_names() -> None:
    existing = {"replay", "dashboard", "carry", "fees", "funding", "trial", "collect", "ai"}
    parsed = build_parser().parse_args(["predictions", "venues", "status", "--db", "x.duckdb"])
    assert parsed.command == "predictions"
    assert "predictions" not in existing
```

- [x] **Step 2: Run and observe the missing parser branch**

Run:

```bash
.venv/bin/python -m pytest tests/test_cli.py -q -k predictions
```

Expected: FAIL because `predictions` is not a registered subcommand.

- [x] **Step 3: Register the `predictions` subparser tree in `build_parser()`**

- [x] **Step 4: Write failing gate-rejection and exit-code tests**

```python
def test_collect_polymarket_exits_two_before_any_network_call_when_watchlisted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def reject_network(*_a: object, **_k: object) -> None:
        raise AssertionError("collect must not open a network client when gate-rejected")

    monkeypatch.setattr(cli, "make_public_http_client", reject_network)
    database = seeded_watchlist_database(tmp_path)

    assert main(["predictions", "collect", "polymarket", "--db", str(database)]) == 2
```

Also test: `predictions venues status` on a brand-new (unmigrated-by-this-command) database path
still exits cleanly with `MANIFEST_NOT_FOUND` for both venues rather than crashing; `predictions
health` follows the existing `--as-of`-omitted-captures-one-clock-value convention; a database
locked by a concurrent writer surfaces as a sanitized, non-zero exit rather than a raw DuckDB
traceback, matching the existing CLI's error-sanitization convention.

- [x] **Step 5: Implement dispatch, writer-lease acquisition, and gate-checked collection**

- [x] **Step 6: Verify and commit Task 10**

Run:

```bash
.venv/bin/python -m pytest tests/test_cli.py -q -k predictions
.venv/bin/ruff check src/polytrading/predictions/cli.py src/polytrading/cli.py tests/test_cli.py
.venv/bin/ruff format --check src/polytrading/predictions/cli.py src/polytrading/cli.py tests/test_cli.py
git add src/polytrading/predictions/cli.py src/polytrading/cli.py tests/test_cli.py
git commit -m "feat(predictions): add the predictions CLI command group"
```

Expected: parser-shape, gate-rejection, and exit-code tests pass; no existing CLI test regresses.

---

### Task 11: `predictions dashboard`

**Files:**

- Create: `src/polytrading/predictions/dashboard_models.py`
- Create: `src/polytrading/predictions/dashboard.py`
- Create: `src/polytrading/predictions/dashboard_server.py`
- Create: `src/polytrading/predictions/web_assets/index.html`, `app.css`, `app.js`
- Modify: `src/polytrading/cli.py` (`predictions dashboard --db <path> --port <port>`)
- Modify: `pyproject.toml` (`[tool.setuptools.package-data]` — add
  `"polytrading.predictions.web_assets" = ["*.html", "*.css", "*.js"]`)
- Create: `tests/predictions/test_dashboard_models.py`
- Create: `tests/predictions/test_dashboard.py`
- Create: `tests/predictions/test_dashboard_server.py`

**Interfaces:**

- Consumes: `PredictionMarketStore`, `PredictionHealthAuditor`, `PredictionRegistry`.
- Produces:
  - `PredictionDashboardSnapshot(PredictionRecord)`: `schema_version: Literal[1]`, `as_of:
    datetime`, `health: PredictionHealthReport`, `markets: tuple[MarketRecord, ...]` (bounded, most
    recently retrieved first, matching the existing dashboard's bounded-row convention), `evidence_counts:
    dict[str, int]`, `recipes: tuple[str, ...]` (copyable CLI examples, same "text only, never
    executed" contract as the existing dashboard's recipes).
  - `PredictionDashboardBuilder(store: PredictionMarketStore).build(as_of: datetime) ->
    PredictionDashboardSnapshot`.
  - `validate_prediction_dashboard_database(path: Path) -> None` — opens read-only and verifies
    the *prediction-market* schema version, reusing the exact fail-closed shape of
    `polytrading.web.server.validate_dashboard_database` but against `PredictionMarketStore`, so a
    perpetual-futures database path passed here fails closed immediately rather than being
    silently misread.
  - `serve_prediction_dashboard(database_path: Path, port: int, *, clock=None) -> None`: a
    dedicated function, not a parameterization of the existing `serve_dashboard` — it constructs
    its own `HTTPServer` bound to its own request handler and `PredictionDashboardBuilder`, reusing
    only the safe, venue-neutral scaffolding *pattern* (loopback-only host validation, GET-only
    handler, `DATABASE_BUSY` sanitized retry response, `DashboardLifecycleError`-equivalent
    cleanup) copied into `dashboard_server.py`. The existing `serve_dashboard` function, its
    `DashboardApplication` class, and every existing web test are not modified by this task,
    eliminating any risk of the existing dashboard behavior changing (this is a deliberate
    duplication-over-shared-abstraction choice: the two dashboards' request/response shapes are
    different enough, and the existing dashboard's blast radius important enough, that a shared
    generic HTTP layer is not worth the coupling risk in this increment).
  - CLI: `polytrading predictions dashboard --db var/prediction-markets.duckdb --port 8787`.
- Consumers: the operator only; no other task consumes this dashboard's output.

- [x] **Step 1: Write failing snapshot-builder tests**

```python
def test_snapshot_never_shows_a_market_retrieved_after_its_own_cutoff() -> None:
    store = seeded_store_with_markets_around(CUTOFF)
    snapshot = PredictionDashboardBuilder(store).build(CUTOFF)
    assert all(market.retrieved_at <= CUTOFF for market in snapshot.markets)


def test_snapshot_recipes_are_copy_only_text() -> None:
    snapshot = PredictionDashboardBuilder(empty_store()).build(NOW)
    assert all(isinstance(recipe, str) for recipe in snapshot.recipes)
```

- [x] **Step 2: Run and observe the missing module**

Run:

```bash
.venv/bin/python -m pytest tests/predictions/test_dashboard_models.py tests/predictions/test_dashboard.py -q
```

Expected: collection fails because the new modules do not exist.

- [x] **Step 3: Implement the snapshot models and builder**

- [x] **Step 4: Write failing schema-lock and loopback-only server tests**

```python
def test_prediction_dashboard_rejects_a_perpetual_futures_database(tmp_path: Path) -> None:
    perp_futures_db = tmp_path / "forward.duckdb"
    DuckDBStore(perp_futures_db).close()  # existing perpetual-futures schema

    with pytest.raises(ValueError, match="schema"):
        validate_prediction_dashboard_database(perp_futures_db)


def test_prediction_dashboard_binds_loopback_only(tmp_path: Path) -> None:
    ...  # mirror the existing dashboard's exact loopback/host-validation test
```

Add a companion test asserting the *existing* `validate_dashboard_database` equally rejects a fresh
`PredictionMarketStore` database, proving the two schema-lock functions are symmetric and neither
system can silently misread the other's database file.

- [x] **Step 5: Implement `validate_prediction_dashboard_database` and `serve_prediction_dashboard`**

- [x] **Step 6: Write failing markup/asset tests**

Mirror the existing dashboard's browser-rendering test structure (empty, collecting, watchlisted-
venue, and stale states) at the level this codebase's existing web tests already operate (DOM
structure and content assertions via the existing test harness, not a real browser, matching
`tests/web/test_dashboard.py`'s and the local-dashboard companion spec's existing conventions).

- [x] **Step 7: Implement the markup, styling, and client script**

- [x] **Step 8: Register the CLI command**

Add `predictions dashboard --db <path> --port <port>` to `predictions.cli`, calling
`validate_prediction_dashboard_database` then `serve_prediction_dashboard`, mirroring the existing
`dashboard` command's exact two-call structure in `cli.py`'s `main()`.

- [x] **Step 9: Verify and commit Task 11**

Run:

```bash
.venv/bin/python -m pytest tests/predictions -q
.venv/bin/ruff check src/polytrading/predictions src/polytrading/cli.py tests/predictions
.venv/bin/ruff format --check src/polytrading/predictions src/polytrading/cli.py tests/predictions
git add src/polytrading/predictions pyproject.toml src/polytrading/cli.py tests/predictions
git commit -m "feat(predictions): add the loopback-only predictions dashboard"
```

Expected: full `tests/predictions` package green; both schema-lock functions demonstrably reject
the other system's database file.

---

### Task 12: README, full verification, package, and browser smoke

**Files:**

- Modify: `README.md`
- Modify: `tests/test_package.py` (if a wheel-content test enumerates package-data globs, add the
  new `polytrading.predictions.storage.schema` and `polytrading.predictions.web_assets` entries)

**Interfaces:** none new; this task verifies and documents everything Tasks 1-11 produced.

- [x] **Step 1: Write the README section**

Add a new top-level section (after the existing Lighter-dYdX trial section, before "Explicit
read-only boundary") titled to match this codebase's numbered-section convention, covering: the
`predictions venues status`/`collect polymarket`/`collect kalshi`/`health`/`dashboard` commands
with example invocations against `var/prediction-markets.duckdb`; an explicit statement that this
database is never the same file as any existing perpetual-futures database; the manifest gate
(Kalshi and Polymarket both `WATCHLIST` by default until a reviewed manifest is recorded — state
plainly that this increment ships no bundled "already-approved" manifest, so `predictions collect`
exits `2` on a fresh database until an operator records one, matching this project's existing
"never silently authorize" posture); the exact same research-only, no-execution-authority
disclaimers already used throughout the existing README sections; and a note that increments 2-5
(Limitless, candidate discovery, proofs, economics, replay/shadow, execution) are not yet
implemented.

- [x] **Step 2: Run the complete test suite**

Run:

```bash
mkdir -p var
.venv/bin/python -m pytest -q
```

Expected: the complete existing suite plus every new `tests/predictions` and modified
`tests/corpus_intake`/`tests/test_cli.py` test passes; zero regressions in the perpetual-futures
suite.

- [x] **Step 3: Run static checks and coverage**

Run:

```bash
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/python -m pytest --cov=polytrading --cov-report=term-missing -q
```

Confirm total coverage has not regressed below the existing repository bar.

- [x] **Step 4: Build and smoke-test the wheel**

Run the same wheel-build-and-import smoke the existing `tests/test_package.py` already performs, or
extend it, to confirm `polytrading.predictions` and its two new package-data globs are present in
the built wheel and importable from a clean install:

```bash
.venv/bin/python -m pip wheel --no-deps -w dist .
.venv/bin/python -m pip install --force-reinstall dist/polytrading-*.whl
python -c "from polytrading.predictions.cli import build_predictions_parser"
```

- [x] **Step 5: Manual browser smoke of `predictions dashboard`**

Following this codebase's existing manual-verification convention (not a CI gate): collect a small
amount of fixture-replayed or live public evidence into a fresh `var/prediction-markets.duckdb`,
start `polytrading predictions dashboard --db var/prediction-markets.duckdb --port 8787`, and
visually confirm venue health, market/rule counts, and the watchlisted-Kalshi state render without
a mutation control anywhere on the page.

- [x] **Step 6: Final commit**

Run:

```bash
git add README.md tests/test_package.py
git commit -m "docs(predictions): document the multi-venue prediction-market shared core"
```

Expected: full suite, lint, format, coverage, packaging, and manual dashboard smoke all pass. This
closes increment 1; increments 2-5 each require their own plan per the specification's own
sequencing rule.

