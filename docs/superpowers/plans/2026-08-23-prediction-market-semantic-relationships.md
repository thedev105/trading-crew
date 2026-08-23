# Prediction-Market Increment 2: Conditional Venue and Semantic Relationships Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement increment 2 of the multi-venue prediction-market system: the conditional
Limitless venue (identity, source-use gate, read-only public adapter, CLI), the shared typed
proposition schema, append-only candidate-relationship artifacts with deterministic and AI
provenance, deterministic candidate generators, a gated AI cross-venue nomination bridge, hard
negatives, and closure of the semantic-scout 95%-vs-99.5% evaluation-gate gap.

**Architecture:** Everything extends the increment-1 `src/polytrading/predictions/` package and its
DuckDB store (`var/prediction-markets.duckdb`, migration sequence continuing at `002`). Limitless
joins `PredictionVenue`/`PredictionSource` as a third member whose collection fails closed with a
typed manifest-gate reason until an operator appends an affirmative `VenueManifest` — no code path
special-cases it beyond its own adapter. Candidate relationships are a new append-only artifact
family (spec section 6.4) written idempotently under content-derived UUIDv5 identities; AI
provenance can never create a `proof_ready` disposition (spec section 5), and the AI nomination
bridge abstains with a typed reason unless a `SemanticEvaluation` with `gate_status == "PASS"` at
the raised 99.5% threshold is supplied. Proof compilation itself is increment 3 and is out of
scope: every candidate this increment produces is `quarantined` or `rejected`.

**Tech Stack:** Python 3.12–3.14, Pydantic 2.13.4 (`extra="forbid", frozen=True, strict=True`),
DuckDB 1.5.4, httpx 0.28.1 with mock transports in tests, argparse, pytest 9.1.1, Ruff 0.15.22.

**Spec:** `docs/superpowers/specs/2026-08-14-multi-venue-prediction-market-structural-opportunity-system-design.md`
(sections 1, 3.1, 4, 5, 6.1, 6.4, 11.3, 13, 15.1, 15.2, 16 increment 2). Approved scope decision
for this delivery: code-complete through increment 5 with live execution permanently fail-closed
behind credentials and the spec's activation gates; this plan is increment 2 only.

## Global Constraints

- No live credentials, wallet funding, order submission, or geographic circumvention anywhere in
  this plan (spec authority boundary). The Limitless adapter is public-data, read-only.
- Never reuse perpetual-futures types (`Venue`, `Asset`, `RawEnvelope`, `DuckDBStore`, …) in the
  predictions package; never write prediction data into a perpetual-futures database file.
- Raw responses are persisted before normalized records; every normalized record's lineage hash
  must resolve to a same-venue raw source hash in its batch
  (`validate_prediction_adapter_batch`).
- Every new model extends `PredictionRecord` (or `StrictRecord` in the `ai` package):
  `extra="forbid"`, `frozen=True`, `strict=True`; timestamps timezone-aware, normalized UTC;
  hashes exact lowercase-hex SHA-256.
- Unsupported venue fields remain `unknown`/`None` with a structured warning; adapters never
  substitute a Polymarket-like default (spec section 6.2).
- AI may nominate and extract; it may never declare exhaustiveness, produce `proof_ready`, or
  supply a missing rule field from general knowledge (spec section 5).
- All collection fails closed on a missing or non-permitting venue manifest before any network
  request (spec section 6.1).
- Migrations are sequential numbered SQL files under
  `src/polytrading/predictions/storage/schema/`; the next is `002`.
- Run `.venv/bin/python -m pytest` (never bare `pytest`) so `tests.*` helper imports resolve.
- After code changes land, run `graphify update .` (project CLAUDE.md).

---

### Task 1: Limitless venue identity

**Files:**

- Modify: `src/polytrading/predictions/domain.py` (`PredictionVenue`, `PredictionSource`,
  `MarketRecord._require_matching_token_count` docstring only — the Kalshi-specific
  `negative_risk` restriction already permits Limitless by construction)
- Modify: `src/polytrading/predictions/cli.py:105`, `src/polytrading/predictions/dashboard.py:34`,
  `src/polytrading/predictions/health.py:64` (venue iteration)
- Test: `tests/predictions/test_domain.py`, `tests/predictions/test_cli.py`,
  `tests/predictions/test_health.py`, `tests/predictions/test_dashboard.py`

**Interfaces:**

- Consumes: increment-1 `PredictionVenue`, `PredictionSource`, `MarketRecord`.
- Produces: `PredictionVenue.LIMITLESS = "limitless"` and `PredictionSource.LIMITLESS =
  "limitless"`. Every venue-enumeration site iterates `tuple(PredictionVenue)` instead of a
  hardcoded two-venue tuple, so `venues status`, health, and the dashboard automatically show
  Limitless as manifest-missing/fail-closed.
- Consumers: Tasks 2, 3, 6, 8 (adapter, CLI, generators, scout bridge), and the corpus-intake
  source-use gate (`source: PredictionSource`), which gains Limitless coverage with no code change.

- [ ] **Step 1: Write failing enum and iteration tests**

In `tests/predictions/test_domain.py`:

```python
def test_limitless_is_a_prediction_venue_and_source() -> None:
    assert PredictionVenue("limitless") is PredictionVenue.LIMITLESS
    assert PredictionSource("limitless") is PredictionSource.LIMITLESS


def test_limitless_market_may_carry_negative_risk() -> None:
    market = market_record(venue=PredictionVenue.LIMITLESS, negative_risk=True)
    assert market.negative_risk is True


def test_kalshi_market_still_rejects_negative_risk() -> None:
    with pytest.raises(ValidationError, match="negative_risk"):
        market_record(venue=PredictionVenue.KALSHI, negative_risk=False)
```

(`market_record` is the existing factory in `tests/predictions/domain_helpers.py`; extend it with
`venue`/`negative_risk` keyword overrides if it lacks them.) In `tests/predictions/test_cli.py`,
extend the existing `venues status` test to assert the JSON output contains a
`"venue": "limitless"` row with `"collection_allowed": false` and `"reason":
"MANIFEST_NOT_FOUND"` when no Limitless manifest exists.

- [ ] **Step 2: Run tests to verify they fail**

```bash
.venv/bin/python -m pytest tests/predictions/test_domain.py tests/predictions/test_cli.py -q
```

Expected: FAIL — `"limitless" is not a valid PredictionVenue`, and the venues-status JSON has no
limitless row.

- [ ] **Step 3: Add the enum members and generalize the three iteration sites**

Add `LIMITLESS = "limitless"` to both enums. Replace each hardcoded
`for venue in (PredictionVenue.POLYMARKET, PredictionVenue.KALSHI):` with
`for venue in PredictionVenue:` in `cli.py`, `dashboard.py`, and `health.py`. Health and dashboard
must treat a venue with zero evidence and no manifest as its existing "absent" state, not an
error; if the health auditor currently reports absent venues as failures, keep the CLI exit-code
rule unchanged by only asserting on Polymarket/Kalshi rows in exit-code tests and update fixtures
so an absent Limitless row renders as its existing no-evidence status.

- [ ] **Step 4: Run the full predictions test directory**

```bash
.venv/bin/python -m pytest tests/predictions -q
```

Expected: PASS, including previously existing health/dashboard tests updated for the third row.

- [ ] **Step 5: Commit**

```bash
git add src/polytrading/predictions tests/predictions
git commit -m "feat(predictions): add the conditional Limitless venue identity"
```

---

### Task 2: Limitless read-only public adapter

**Files:**

- Create: `src/polytrading/predictions/limitless.py`
- Test: `tests/predictions/test_limitless.py`

**Interfaces:**

- Consumes: `PredictionVenueAdapter` protocol, `PredictionAdapterBatch`,
  `PredictionAdapterWarning`, `PredictionCollectionGateError` from
  `polytrading.predictions.adapter`; `MarketRecord`, `RuleVersion`, `PredictionRawEnvelope` from
  `polytrading.predictions.domain`; `evaluate_collection_gate` from
  `polytrading.predictions.manifest`.
- Produces: `LimitlessAdapter` with the same constructor signature the CLI already uses for the
  other adapters: `LimitlessAdapter(client, utc_now, monotonic_ns)`. `venue =
  PredictionVenue.LIMITLESS`. Implements `fetch_markets(information_cutoff=...)` fully;
  `fetch_book_snapshot`, `fetch_trades`, and `fetch_fee_rate` raise a structured
  `PredictionAdapterBatchIntegrityError`-style `NotImplementedError` carrying code
  `"limitless_endpoint_not_collected"` (book/trade/fee collection is deferred until the venue
  manifest ever moves past READ_ONLY review; markets and rules are the increment-2 deliverable).
- Consumers: Task 3 CLI registration.

**Behavioral contract (spec sections 6.2, 15.1):**

- Paginated GET of `https://api.limitless.exchange/markets` (page-numbered pagination; the exact
  query-parameter names and payload field names MUST be verified against
  https://docs.limitless.exchange/developers/programmatic-api at implementation time and encoded
  once in module-level constants plus the test fixture — the adapter fails closed on any field it
  does not recognize rather than guessing).
- Each response page is wrapped as one `PredictionRawEnvelope` (exact payload text, SHA-256,
  monotonic receipt) before any normalization, matching the Polymarket adapter's pattern
  (`src/polytrading/predictions/polymarket.py`).
- Normalization: one `MarketRecord` + one `RuleVersion` per market. CLOB/AMM distinction: only
  markets the payload identifies as central-limit-order-book markets set
  `order_book_enabled=True`; AMM markets are stored with `order_book_enabled=False` plus a
  `PredictionAdapterWarning(code="limitless_amm_market")`. Negative-risk grouping fields are
  preserved into `negative_risk`/`event_id` when present; absent fields stay `None` with a
  warning, never a default.
- A market payload missing its question, outcomes, or identifier is skipped with warning code
  `"limitless_market_incomplete"`; the batch still validates.

- [ ] **Step 1: Write failing adapter tests with a mock transport**

Follow the exact structure of `tests/predictions/test_polymarket.py` (mock `httpx` transport,
canned JSON pages). Minimum cases:

```python
async def test_fetch_markets_persists_raw_before_normalized_lineage() -> None:
    batch = await adapter.fetch_markets(information_cutoff=CUTOFF)
    validate_prediction_adapter_batch(batch)  # raw-first lineage holds
    assert all(record.venue is PredictionVenue.LIMITLESS for record in batch.raw)


async def test_amm_market_is_not_order_book_enabled_and_warns() -> None:
    batch = await adapter.fetch_markets(information_cutoff=CUTOFF)
    amm = next(m for m in batch.normalized
               if isinstance(m, MarketRecord) and m.market_id == "amm-market-1")
    assert amm.order_book_enabled is False
    assert any(w.code == "limitless_amm_market" for w in batch.warnings)


async def test_incomplete_market_is_skipped_with_warning_not_defaulted() -> None:
    ...
    assert any(w.code == "limitless_market_incomplete" for w in batch.warnings)


async def test_book_trade_fee_endpoints_are_explicitly_not_collected() -> None:
    with pytest.raises(NotImplementedError, match="limitless_endpoint_not_collected"):
        await adapter.fetch_book_snapshot("m", None, NOW, uuid4())
```

Also test: pagination follows to the second page and stops; malformed JSON fails closed with an
exception (no partial normalized output); `negative_risk` present in the fixture round-trips.

- [ ] **Step 2: Run to verify failure**

```bash
.venv/bin/python -m pytest tests/predictions/test_limitless.py -q
```

Expected: FAIL — `polytrading.predictions.limitless` does not exist.

- [ ] **Step 3: Implement `LimitlessAdapter`**

Mirror `PolymarketAdapter`'s construction (`client`, `utc_now`, `monotonic_ns`), raw-envelope
wrapping, and normalization helpers. Keep every payload field name in module constants next to a
comment citing the docs URL. No retry logic beyond what `make_public_http_client` already
provides.

- [ ] **Step 4: Run tests and lint**

```bash
.venv/bin/python -m pytest tests/predictions/test_limitless.py -q
.venv/bin/ruff check src/polytrading/predictions/limitless.py tests/predictions/test_limitless.py
.venv/bin/ruff format --check src/polytrading/predictions/limitless.py tests/predictions/test_limitless.py
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/polytrading/predictions/limitless.py tests/predictions/test_limitless.py
git commit -m "feat(predictions): add the conditional Limitless read-only adapter"
```

---

### Task 3: CLI `predictions collect limitless`

**Files:**

- Modify: `src/polytrading/predictions/cli.py` (`_ADAPTER_BY_VENUE`, the collect subparser loop)
- Test: `tests/predictions/test_cli.py`

**Interfaces:**

- Consumes: `LimitlessAdapter` (Task 2), existing `_run_collect` gate flow.
- Produces: `polytrading predictions collect limitless --db <path>` registered exactly like the
  other two collect subcommands. With no Limitless manifest in the database it exits via
  `PredictionsUsageError("limitless collection is not permitted: MANIFEST_NOT_FOUND")` before any
  HTTP request; with a permitting manifest it runs the Task 2 adapter.
- Consumers: operators; spec section 13's command contract.

- [ ] **Step 1: Write failing CLI tests**

```python
def test_collect_limitless_fails_closed_without_manifest(tmp_path) -> None:
    db = tmp_path / "predictions.duckdb"
    PredictionMarketStore(db).close()  # migrated, empty
    with pytest.raises(SystemExit):  # or the repo's established CLI error assertion pattern
        main(["predictions", "collect", "limitless", "--db", str(db)])
    # assert stderr mentions MANIFEST_NOT_FOUND, matching existing collect-gate test style


def test_collect_limitless_with_permitting_manifest_stores_markets(tmp_path, monkeypatch) -> None:
    # append a READ_ONLY, automated_use permitted Limitless VenueManifest, monkeypatch the
    # HTTP client factory with the Task 2 mock transport, run the command, then assert
    # markets_as_of(PredictionVenue.LIMITLESS, now) is non-empty.
```

Copy the assertion mechanics (exit codes, stderr capture, manifest factory) from the existing
Polymarket/Kalshi collect tests in `tests/predictions/test_cli.py` — do not invent a new pattern.

- [ ] **Step 2: Run to verify failure**

```bash
.venv/bin/python -m pytest tests/predictions/test_cli.py -q
```

Expected: FAIL — argparse rejects `limitless` as a collect subcommand.

- [ ] **Step 3: Register the subcommand and adapter**

In `cli.py`: change the collect loop to `for name in ("polymarket", "kalshi", "limitless"):` and
add `PredictionVenue.LIMITLESS: LimitlessAdapter` to `_ADAPTER_BY_VENUE`. `_run_collect` already
evaluates the manifest gate first, so the fail-closed path needs no new code.

- [ ] **Step 4: Run, lint, commit**

```bash
.venv/bin/python -m pytest tests/predictions/test_cli.py -q
.venv/bin/ruff check src/polytrading/predictions/cli.py tests/predictions/test_cli.py
git add src/polytrading/predictions/cli.py tests/predictions/test_cli.py
git commit -m "feat(predictions): register gated limitless collection in the CLI"
```

---

### Task 4: Shared typed proposition schema

**Files:**

- Create: `src/polytrading/predictions/propositions.py`
- Test: `tests/predictions/test_propositions.py`

**Interfaces:**

- Consumes: `PredictionRecord`, `Sha256` from `polytrading.predictions.domain`.
- Produces (spec sections 4.3, 6.4 "typed propositions and exact supporting source spans"):
  - `PropositionSpan(PredictionRecord)`: `start_char: int`, `end_char: int`,
    `exact_text: str` (non-empty), `rule_source_hash: Sha256` — the SHA-256 of the exact rule
    text the span indexes into (a `RuleVersion.source_hash`), so a span is only meaningful
    against one immutable rule version. Validator: `0 <= start_char < end_char`.
  - `PropositionKind = Literal["binary_condition", "threshold", "deadline", "scope",
    "outcome_membership"]`.
  - `TypedProposition(PredictionRecord)`: `schema_version: Literal[1]`,
    `kind: PropositionKind`, `subject: str` (non-empty), `predicate: str` (non-empty),
    `value: str | None`, `status: Literal["extracted", "unknown"]`,
    `supporting_spans: tuple[PropositionSpan, ...]`.
    Validators mirror `ai.models.CriticalField`: `status == "extracted"` requires at least one
    supporting span; `status == "unknown"` forbids both `value` and spans. No proposition field
    is ever inferred — an extractor that cannot support a field emits `unknown`.
- Consumers: Task 5 (`CandidateRelationship.propositions`), Task 6 generators (emit
  `outcome_membership` propositions), Task 8 scout bridge, increment 3's proof compilers.

- [ ] **Step 1: Write failing proposition tests**

```python
def test_extracted_proposition_requires_supporting_spans() -> None:
    with pytest.raises(ValidationError, match="supporting"):
        TypedProposition(
            schema_version=1, kind="threshold", subject="BTC price", predicate=">=",
            value="100000", status="extracted", supporting_spans=(),
        )


def test_unknown_proposition_forbids_value_and_spans() -> None:
    with pytest.raises(ValidationError):
        TypedProposition(
            schema_version=1, kind="deadline", subject="event", predicate="resolves_by",
            value="2026-12-31", status="unknown", supporting_spans=(),
        )


def test_span_bounds_must_be_nonempty_and_ordered() -> None:
    with pytest.raises(ValidationError):
        PropositionSpan(start_char=5, end_char=5, exact_text="x", rule_source_hash=HASH)
```

Plus a round-trip test that a valid extracted proposition with one span serializes and
re-validates via `model_validate_json`.

- [ ] **Step 2: Run to verify failure; Step 3: implement; Step 4: run and lint**

```bash
.venv/bin/python -m pytest tests/predictions/test_propositions.py -q
.venv/bin/ruff check src/polytrading/predictions/propositions.py tests/predictions/test_propositions.py
```

- [ ] **Step 5: Commit**

```bash
git add src/polytrading/predictions/propositions.py tests/predictions/test_propositions.py
git commit -m "feat(predictions): add the shared typed proposition schema"
```

---

### Task 5: Candidate relationship artifact, migration 002, and store APIs

**Files:**

- Create: `src/polytrading/predictions/candidates_models.py`
- Create: `src/polytrading/predictions/storage/schema/002_candidate_relationships.sql`
- Modify: `src/polytrading/predictions/storage/store.py`
- Test: `tests/predictions/test_candidates_models.py`, `tests/predictions/test_store.py`

**Interfaces:**

- Consumes: `PredictionRecord`, `PredictionVenue`, `Sha256`, `TypedProposition` (Task 4).
- Produces (spec section 6.4, all eight bullets):
  - `RelationshipType(StrEnum)`: `BINARY_COMPLEMENT = "binary_complement"`,
    `EXHAUSTIVE_OUTCOME_SET = "exhaustive_outcome_set"`, `LOGICAL_IMPLICATION =
    "logical_implication"`, `CROSS_VENUE_EQUIVALENCE = "cross_venue_equivalence"`.
  - `CandidateDisposition(StrEnum)`: `QUARANTINED = "quarantined"`, `REJECTED = "rejected"`,
    `PROOF_READY = "proof_ready"`, `SUPERSEDED = "superseded"`.
  - `DeterministicProvenance(PredictionRecord)`: `kind: Literal["deterministic"]`,
    `generator: str` (non-empty), `generator_version: str` (non-empty), `code_revision: str`
    (non-empty).
  - `AIProvenance(PredictionRecord)`: `kind: Literal["ai"]`, `model_id: str`,
    `model_version: str`, `feature_version: str`, `prompt_version: str | None`,
    `evaluation_request_hash: Sha256`, `gate_status: Literal["PASS"]` (a nomination may only be
    persisted when its backing evaluation passed; Task 8 enforces the runtime check).
  - `CandidateLeg(PredictionRecord)`: `venue: PredictionVenue`, `market_id: str`,
    `outcome_index: int | None` (`>= 0` when set), `outcome_token_id: str | None`,
    `rule_version_id: UUID`, `rule_source_hash: Sha256`.
  - `CandidateRelationship(PredictionRecord)`: `schema_version: Literal[1]`,
    `candidate_id: UUID`, `trial_family_id: str` (non-empty), `relationship_type:
    RelationshipType`, `legs: tuple[CandidateLeg, ...]` (at least 2),
    `information_cutoff: datetime`, `observed_at: datetime`,
    `provenance: DeterministicProvenance | AIProvenance`,
    `propositions: tuple[TypedProposition, ...]`, `unresolved_fields: tuple[str, ...]`,
    `contradictions: tuple[str, ...]`, `invalidation_conditions: tuple[str, ...]`,
    `review_status: Literal["unreviewed", "in_review", "reviewed"]`,
    `disposition: CandidateDisposition`, `superseded_by_candidate_id: UUID | None`.
    Model validators:
    1. `provenance.kind == "ai"` ⇒ `disposition in (QUARANTINED, REJECTED)` — AI can never
       create or approve a proof-ready candidate (spec section 5).
    2. `disposition is PROOF_READY` ⇒ `review_status == "reviewed"` and `unresolved_fields ==
       ()` and `contradictions == ()`.
    3. `disposition is SUPERSEDED` ⇔ `superseded_by_candidate_id is not None`.
    4. `CROSS_VENUE_EQUIVALENCE` ⇒ legs span at least two distinct venues; every other type ⇒
       all legs share one venue.
  - `deterministic_candidate_id(relationship_type, legs) -> UUID`: UUIDv5 over a fixed module
    namespace UUID and the canonical JSON of
    `[relationship_type.value, sorted (venue, market_id, outcome_index, str(rule_version_id))]`
    — same markets at the same rule versions always produce the same identity, so regenerating
    candidates is append-idempotent, and a rule change produces a new candidate.
  - Store methods on `PredictionMarketStore`:
    - `append_candidate_relationship(record) -> bool` via `_append_keyed` with
      `where="candidate_id = ?"` (idempotent for byte-identical content; conflicting content
      raises `ConflictingRecordError`).
    - `candidate_relationships_as_of(as_of) -> tuple[CandidateRelationship, ...]` — rows with
      `observed_at <= as_of`, ordered by `observed_at, candidate_id`.
- Consumers: Tasks 6, 8, 9, 10, 11; increment 3's proof compiler reads `candidate_id` +
  `rule_source_hash` lineage.

Migration `002_candidate_relationships.sql`:

```sql
CREATE TABLE candidate_relationships (
    candidate_id UUID NOT NULL,
    relationship_type VARCHAR NOT NULL,
    trial_family_id VARCHAR NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    information_cutoff TIMESTAMPTZ NOT NULL,
    record_json VARCHAR NOT NULL,
    record_hash VARCHAR NOT NULL
);
```

- [ ] **Step 1: Write failing model tests**

```python
def test_ai_provenance_can_never_be_proof_ready() -> None:
    with pytest.raises(ValidationError, match="proof"):
        candidate_relationship(provenance=ai_provenance(), disposition=CandidateDisposition.PROOF_READY)


def test_proof_ready_requires_review_and_no_unresolved_fields() -> None:
    with pytest.raises(ValidationError):
        candidate_relationship(
            provenance=deterministic_provenance(),
            disposition=CandidateDisposition.PROOF_READY,
            review_status="unreviewed",
        )


def test_cross_venue_type_requires_two_distinct_venues() -> None:
    with pytest.raises(ValidationError, match="venue"):
        candidate_relationship(
            relationship_type=RelationshipType.CROSS_VENUE_EQUIVALENCE,
            legs=(leg(venue=PredictionVenue.POLYMARKET), leg(venue=PredictionVenue.POLYMARKET)),
        )


def test_deterministic_candidate_id_is_stable_and_leg-order-invariant() -> None:
    assert deterministic_candidate_id(t, (leg_a, leg_b)) == deterministic_candidate_id(t, (leg_b, leg_a))
```

Add factories `candidate_relationship`, `leg`, `ai_provenance`, `deterministic_provenance` to a
new `tests/predictions/candidate_helpers.py`. Store tests (in `test_store.py`): append is
idempotent for identical content, raises `ConflictingRecordError` on divergent content under the
same `candidate_id`, `candidate_relationships_as_of` respects the cutoff, and the
migration-count fixture (see the increment-1 pattern used by `0fbf46f` for `006_paper_positions`)
is updated for migration 002.

- [ ] **Step 2: Run to verify failure; Step 3: implement models + migration + store methods**

```bash
.venv/bin/python -m pytest tests/predictions/test_candidates_models.py tests/predictions/test_store.py -q
```

- [ ] **Step 4: Run predictions suite and lint; Step 5: Commit**

```bash
.venv/bin/python -m pytest tests/predictions -q
.venv/bin/ruff check src/polytrading/predictions tests/predictions
git add src/polytrading/predictions tests/predictions
git commit -m "feat(predictions): add append-only candidate relationship artifacts"
```

---

### Task 6: Deterministic candidate generators

**Files:**

- Create: `src/polytrading/predictions/candidates.py`
- Test: `tests/predictions/test_candidates.py`

**Interfaces:**

- Consumes: `PredictionRegistry` (increment 1), Task 5 models, Task 4 propositions.
- Produces:
  - `propose_binary_complements(registry, venue, as_of, *, trial_family_id, code_revision) ->
    tuple[CandidateRelationship, ...]`: for every market at `as_of` with exactly two outcomes,
    `active=True`, `closed=False`, `order_book_enabled=True`, emit one `BINARY_COMPLEMENT`
    candidate whose two legs are the market's outcome indices 0 and 1 at the market's current
    `rule_version_id`, provenance `DeterministicProvenance(generator="binary_complement",
    generator_version="1", code_revision=...)`, disposition `QUARANTINED`, review
    `unreviewed`, `unresolved_fields=("terminal_partition_unproven",)` — the generator proposes;
    increment 3 proves. One `outcome_membership` proposition per leg, `status="unknown"` (no
    extractor runs here; spans arrive with the scout or manual review).
  - `propose_venue_native_outcome_sets(registry, venue, as_of, *, trial_family_id,
    code_revision) -> tuple[CandidateRelationship, ...]`: group the venue's open order-book
    markets by `event_id`; for groups of two or more markets emit one `EXHAUSTIVE_OUTCOME_SET`
    candidate with one leg per member market (outcome_index `None` — member markets are the
    outcomes). Polymarket groups additionally require every member's `negative_risk is True`;
    Kalshi groups use `event_id` alone and never consult `negative_risk` (spec section 6.2);
    Limitless follows the Polymarket rule. Always include
    `unresolved_fields=("outcome_set_exhaustiveness_unproven",)` — an outcome list that merely
    looks complete is not proof (spec section 4.2).
  - Both functions are pure over the registry snapshot: no network, no clock (callers pass
    `as_of`; `observed_at = as_of`), no storage writes.
- Consumers: Task 10 CLI persists their output; Task 9 hard-negative tests.

- [ ] **Step 1: Write failing generator tests**

```python
def test_two_outcome_open_market_yields_quarantined_complement_candidate() -> None:
    candidates = propose_binary_complements(registry, PredictionVenue.POLYMARKET, AS_OF,
                                            trial_family_id="tf-1", code_revision="abc")
    (candidate,) = candidates
    assert candidate.relationship_type is RelationshipType.BINARY_COMPLEMENT
    assert candidate.disposition is CandidateDisposition.QUARANTINED
    assert "terminal_partition_unproven" in candidate.unresolved_fields


def test_closed_or_bookless_or_three_outcome_markets_are_skipped() -> None: ...


def test_polymarket_group_without_negative_risk_is_not_an_outcome_set() -> None: ...


def test_kalshi_groups_by_event_id_without_consulting_negative_risk() -> None: ...


def test_single_market_event_groups_are_skipped() -> None: ...


def test_regeneration_is_identity_stable() -> None:
    a = propose_binary_complements(...)
    b = propose_binary_complements(...)
    assert [c.candidate_id for c in a] == [c.candidate_id for c in b]
```

Build registry fixtures with the existing `tests/predictions/store_helpers.py` +
`domain_helpers.py` factories.

- [ ] **Step 2: Verify failure; Step 3: implement; Step 4: run and lint; Step 5: Commit**

```bash
.venv/bin/python -m pytest tests/predictions/test_candidates.py -q
.venv/bin/ruff check src/polytrading/predictions/candidates.py tests/predictions/test_candidates.py
git add src/polytrading/predictions/candidates.py tests/predictions/test_candidates.py
git commit -m "feat(predictions): add deterministic complement and outcome-set candidate generators"
```

---

### Task 7: Raise the semantic-scout gate to 99.5%

**Files:**

- Modify: `src/polytrading/ai/evaluate.py:36`
- Test: `tests/ai/test_evaluate.py` (existing threshold expectations)

**Interfaces:**

- Consumes: nothing new.
- Produces: `_THRESHOLDS["critical_field_exact_match"] = Decimal("0.995")` — the shipped gate
  now matches the AI Augmentation Design's approved 99.5% requirement (spec section 5's explicit
  gap-closure obligation). Consequence, asserted by test: the existing synthetic fixture corpus
  does NOT clear the raised gate, so `SemanticEvaluation.gate_status` cannot reach `"PASS"` from
  synthetic data alone, and the Task 8 bridge therefore abstains until a genuine adjudicated
  gold corpus is evaluated. That operational validation is future evidence work, not code.
- Consumers: Task 8's runtime check.

- [ ] **Step 1: Write/adjust the failing test**

```python
def test_critical_field_gate_requires_995_per_thousand() -> None:
    assert _THRESHOLDS["critical_field_exact_match"] == Decimal("0.995")


def test_synthetic_fixture_corpus_does_not_pass_the_raised_gate() -> None:
    evaluation = evaluate_fixture_corpus()  # reuse the existing fixture-evaluation helper
    assert evaluation.gate_status != "PASS"
```

Locate and update any existing test that asserts the old `0.95` value or asserts a synthetic
PASS; those assertions are the gap being closed, so they change with justification, not
deletion — keep them as the two tests above.

- [ ] **Step 2: Run, observe failure; Step 3: change the Decimal; Step 4: run the ai suite**

```bash
.venv/bin/python -m pytest tests/ai -q
```

Expected: PASS after the threshold change and test updates. If other ai tests assumed a passing
gate, update their fixtures to assert the new fail-closed reality rather than weakening the gate.

- [ ] **Step 5: Commit**

```bash
git add src/polytrading/ai/evaluate.py tests/ai
git commit -m "fix(ai): raise the critical-field gate to the approved 99.5% threshold"
```

---

### Task 8: Gated AI cross-venue nomination bridge

**Files:**

- Create: `src/polytrading/predictions/scout_bridge.py`
- Test: `tests/predictions/test_scout_bridge.py`

**Interfaces:**

- Consumes: `TfidfCandidateRetriever`, `RetrievalDocument` from `polytrading.ai.retrieval`;
  `SemanticEvaluation` from `polytrading.ai.evaluate`; `PredictionRegistry`; Task 5 models.
- Produces:
  - `ScoutAbstention(PredictionRecord)`: `reason: Literal["SCOUT_GATE_UNMET",
    "NO_EVALUATION_SUPPLIED", "NO_ELIGIBLE_MARKETS"]`, `evaluation_request_hash: Sha256 | None`,
    `as_of: datetime`.
  - `nominate_cross_venue_candidates(registry, evaluation: SemanticEvaluation | None,
    venue_a, venue_b, as_of, *, trial_family_id, top_k: int = 3) ->
    tuple[CandidateRelationship, ...] | ScoutAbstention`:
    1. `evaluation is None` → `ScoutAbstention(reason="NO_EVALUATION_SUPPLIED", ...)`.
    2. `evaluation.gate_status != "PASS"` → `ScoutAbstention(reason="SCOUT_GATE_UNMET", ...)`.
    3. Otherwise index venue B's open order-book market questions as `RetrievalDocument`s,
       query with venue A's, and emit one `CROSS_VENUE_EQUIVALENCE` candidate per retrieved
       pair: AI provenance carrying the retriever's model card identity and
       `evaluation.request_hash`; disposition `QUARANTINED`; `unresolved_fields` listing every
       Engine D equivalence dimension verbatim from spec section 4.4:
       `("proposition_threshold_inclusivity", "observation_period_timezone",
       "resolution_sources", "void_dispute_behavior", "outcome_completeness",
       "denomination_collateral_rounding", "settlement_finality_timing",
       "venue_access_custody_rules")` — the equivalence compiler (increment 3) is the only
       thing that may empty this list; `invalidation_conditions` includes
       `"any participating rule_version change"`.
  - The bridge never writes storage and never mutates dispositions — pure nomination.
- Consumers: Task 10 CLI (`--nominate-cross-venue`), Task 9 hard-negative tests.

- [ ] **Step 1: Write failing bridge tests**

```python
def test_missing_evaluation_abstains_typed() -> None:
    result = nominate_cross_venue_candidates(registry, None, PV.POLYMARKET, PV.KALSHI, AS_OF,
                                             trial_family_id="tf-1")
    assert isinstance(result, ScoutAbstention)
    assert result.reason == "NO_EVALUATION_SUPPLIED"


def test_failed_gate_abstains_typed() -> None:
    result = nominate_cross_venue_candidates(registry, evaluation(gate_status="FAIL"), ...)
    assert isinstance(result, ScoutAbstention) and result.reason == "SCOUT_GATE_UNMET"


def test_passing_gate_nominates_quarantined_candidates_with_all_equivalence_dimensions() -> None:
    result = nominate_cross_venue_candidates(registry, evaluation(gate_status="PASS"), ...)
    assert all(c.disposition is CandidateDisposition.QUARANTINED for c in result)
    assert all(c.provenance.kind == "ai" for c in result)
    assert all(len(c.unresolved_fields) == 8 for c in result)
```

Build a minimal passing `SemanticEvaluation` via a test factory (its strict model permits
constructing `gate_status="PASS"` directly — the runtime check is the bridge's, not the
model's). Also test that similar-title markets across venues are retrieved and that a candidate
pair's legs really span both venues.

- [ ] **Step 2: Verify failure; Step 3: implement; Step 4: run and lint; Step 5: Commit**

```bash
.venv/bin/python -m pytest tests/predictions/test_scout_bridge.py -q
.venv/bin/ruff check src/polytrading/predictions/scout_bridge.py tests/predictions/test_scout_bridge.py
git add src/polytrading/predictions/scout_bridge.py tests/predictions/test_scout_bridge.py
git commit -m "feat(predictions): add the evaluation-gated AI cross-venue nomination bridge"
```

---

### Task 9: Hard negatives

**Files:**

- Create: `tests/fixtures/predictions/hard_negatives.json` (checked-in gold pairs)
- Create: `tests/predictions/test_hard_negatives.py`

**Interfaces:**

- Consumes: Tasks 6 and 8.
- Produces: a checked-in gold set of at least six market pairs whose titles appear related while
  one critical rule differs (spec section 15.2): differing threshold inclusivity (`>= 100000` vs
  `> 100000`), differing deadline timezone (ET vs UTC midnight), differing resolution source
  (venue oracle A vs B), differing observation window, a consumer frontend routed to the same
  underlying exchange (must not count as cross-venue), and a subset/superset scope mismatch.
  Each fixture entry carries both market records' full rule text and the divergent field's name.
- Consumers: increment 3's equivalence compiler reuses this fixture for its mutation tests.

- [ ] **Step 1: Write the failing hard-negative tests**

```python
def test_deterministic_generators_never_pair_across_markets() -> None:
    # complement candidates only ever have two legs of ONE market; outcome sets only group by
    # exact event_id — feed the hard-negative pairs in as separate events and assert no
    # generator output relates them.


def test_scout_nominated_hard_negatives_remain_fully_unresolved_and_quarantined() -> None:
    # index the hard-negative pairs, run the bridge with a passing evaluation, and assert every
    # nomination is QUARANTINED with all 8 equivalence dimensions unresolved — title similarity
    # produced a nomination but nothing reduced its unresolved surface.


def test_same_underlying_exchange_frontend_pair_is_flagged_in_fixture() -> None:
    # the fixture entry's divergent_field == "underlying_exchange"; assert the fixture parses
    # and its two members declare the same underlying exchange so increment 3 can reject it.
```

- [ ] **Step 2: Verify failure (fixture missing); Step 3: author the fixture + make tests pass;
  Step 4: run and lint; Step 5: Commit**

```bash
.venv/bin/python -m pytest tests/predictions/test_hard_negatives.py -q
git add tests/fixtures/predictions/hard_negatives.json tests/predictions/test_hard_negatives.py
git commit -m "test(predictions): add title-similar rule-divergent hard-negative gold pairs"
```

---

### Task 10: CLI `predictions candidates`

**Files:**

- Modify: `src/polytrading/predictions/cli.py`
- Test: `tests/predictions/test_cli.py`

**Interfaces:**

- Consumes: Tasks 5, 6, 8; `database_writer_lease`; `PredictionRegistry`.
- Produces: `polytrading predictions candidates --db <path> --venues polymarket,kalshi
  [--as-of <UTC>] [--trial-family <id>] [--format text|json]` (spec section 13). Behavior:
  1. Parse `--venues` into `PredictionVenue` members (usage error on unknown names);
     `--as-of` defaults to now (UTC, parsed with the existing `_parse_timestamp`);
     `--trial-family` defaults to `"increment-2-structural"`.
  2. Under the writer lease, run `propose_binary_complements` and
     `propose_venue_native_outcome_sets` per requested venue over a `PredictionRegistry` at
     `--as-of`, then `append_candidate_relationship` for each in one transaction (idempotent
     re-runs by deterministic candidate id).
  3. Cross-venue nomination is NOT wired into this command in increment 2: the bridge abstains
     until a genuine adjudicated gold evaluation exists (Task 7), so the command prints a fixed
     line `cross-venue nomination: abstained (SCOUT_GATE_UNMET: no adjudicated gold
     evaluation)` — visible, typed, and honest (spec section 2's fail-closed results).
  4. Output: per-venue counts by relationship type, newly-appended vs already-known counts, and
     the abstention line; `--format json` emits the same as one JSON object.
- Consumers: operators; Task 11 dashboard shows the same artifacts.

- [ ] **Step 1: Write failing CLI tests**

```python
def test_candidates_command_persists_deterministic_candidates_idempotently(tmp_path) -> None:
    # seed a store with one two-outcome open polymarket market + rule version,
    # run: predictions candidates --db ... --venues polymarket --as-of <fixed>
    # assert exit 0, output counts 1 binary_complement, and store holds 1 candidate;
    # run again and assert 0 newly appended, 1 already known.


def test_candidates_command_reports_cross_venue_abstention(tmp_path, capsys) -> None:
    ...
    assert "SCOUT_GATE_UNMET" in captured.out


def test_candidates_rejects_unknown_venue_name(tmp_path) -> None:
    ...  # usage error, exit code per existing CLI usage-error pattern
```

- [ ] **Step 2: Verify failure; Step 3: implement subcommand; Step 4: run and lint; Step 5: Commit**

```bash
.venv/bin/python -m pytest tests/predictions/test_cli.py -q
.venv/bin/ruff check src/polytrading/predictions/cli.py tests/predictions/test_cli.py
git add src/polytrading/predictions/cli.py tests/predictions/test_cli.py
git commit -m "feat(predictions): add the candidates CLI with typed cross-venue abstention"
```

---

### Task 11: Dashboard candidate panel

**Files:**

- Modify: `src/polytrading/predictions/dashboard.py`, `dashboard_models.py`, `dashboard_server.py`,
  `web_assets/` (whichever asset files the existing dashboard pattern uses)
- Test: `tests/predictions/test_dashboard.py`, `test_dashboard_models.py`, `test_dashboard_server.py`

**Interfaces:**

- Consumes: `candidate_relationships_as_of` (Task 5).
- Produces: the snapshot model gains `candidates: CandidateSummary` where
  `CandidateSummary(PredictionRecord)` carries `total: int`, `by_relationship_type: dict[str,
  int]`, `by_disposition: dict[str, int]`, `by_provenance_kind: dict[str, int]`, and
  `latest: tuple[CandidateListing, ...]` (at most 20, newest first) with `CandidateListing`
  exposing `candidate_id`, `relationship_type`, `venues`, `disposition`, `provenance_kind`,
  `unresolved_field_count`, `observed_at`. The rendered panel labels every candidate exactly as
  its disposition — never as an opportunity, and never with the words `risk-free`, `guaranteed`,
  or `approved` (spec section 13). AI-provenance rows show an explicit `AI-nominated —
  quarantined` badge; the abstention state renders when zero candidates exist.
- Consumers: operators.

- [ ] **Step 1: Write failing snapshot and rendering tests** (mirror the existing paper-positions
  dashboard tests added in `23dee13`): snapshot counts match seeded candidates; cutoff-safety
  (a candidate observed after `as_of` is invisible); the empty state renders; forbidden words
  never appear in the panel's rendered HTML.

- [ ] **Step 2: Verify failure; Step 3: implement snapshot + panel; Step 4: run and lint; Step 5: Commit**

```bash
.venv/bin/python -m pytest tests/predictions/test_dashboard.py tests/predictions/test_dashboard_models.py tests/predictions/test_dashboard_server.py -q
git add src/polytrading/predictions tests/predictions
git commit -m "feat(predictions): render candidate relationships on the dashboard"
```

---

### Task 12: README, full verification, and graph update

**Files:**

- Modify: `README.md` (new section: conditional Limitless collection and candidate discovery,
  with copyable commands and the explicit statement that all increment-2 candidates are
  quarantined research artifacts, not opportunities)

- [ ] **Step 1: Write the README section** — document `predictions collect limitless` (fail-closed
  by default), `predictions candidates`, the 99.5% scout gate and why cross-venue nomination
  abstains, and the hard-negative fixture's purpose. Match the README's existing no-profit
  register exactly.

- [ ] **Step 2: Full verification**

```bash
.venv/bin/python -m pytest
.venv/bin/ruff check .
.venv/bin/ruff format --check .
```

Expected: entire suite passes; zero lint findings.

- [ ] **Step 3: Update the knowledge graph**

```bash
graphify update .
```

- [ ] **Step 4: Commit**

```bash
git add README.md graphify-out
git commit -m "docs(predictions): document conditional Limitless and candidate discovery"
```

---

## Self-review notes

- Spec coverage: section 16 increment 2 names four deliverables — Limitless gate + read-only
  adapter (Tasks 1–3), shared proposition schema (Task 4), candidate provenance (Tasks 5, 6, 8,
  10, 11), hard negatives (Task 9) — plus the section 5 threshold-gap closure (Task 7).
  Equivalence/payoff proofs, economics, replay/shadow, and any authenticated endpoint are
  increments 3–5 and intentionally absent.
- Limitless book/trade/fee collection is deliberately deferred behind
  `limitless_endpoint_not_collected` rather than half-implemented: increment 2's spec text
  commits to the source-use gate and read-only adapter, and markets/rules are the artifacts the
  candidate layer consumes. Extending Limitless depth collection is a later, manifest-gated step.
- The scout bridge is implemented and tested but not reachable from the CLI until a genuine
  adjudicated gold evaluation passes the 99.5% gate — this is the spec's own sequencing, made
  visible as a typed abstention instead of silence.
