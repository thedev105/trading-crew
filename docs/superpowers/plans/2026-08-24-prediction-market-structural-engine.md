# Prediction-Market Increment 3: Deterministic Structural Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement increment 3 of the multi-venue prediction-market system: rule attestations
(the human-review path proofs depend on), the four deterministic payoff/equivalence proof
compilers (binary complement, exhaustive multi-outcome, logical implication, cross-venue
equivalence), the depth-aware conservative economics engine with capital fragmentation, the
`predictions prove` / `predictions scan` CLI, dashboard proof/economics panels — plus the two
items increment 2 parked (rule-relevant rule-version identity; scout-bridge same-venue guard)
and the book/fee collection wiring the economics engine needs.

**Architecture:** Everything extends `src/polytrading/predictions/`. Proof compilers are pure
deterministic libraries (spec §11.4: no network, model, clock, account, or storage-write
dependency) that consume typed inputs: a `CandidateRelationship`, its `RuleVersion`s, and
human-reviewed `RuleAttestation` records. Attestations are the only bridge from natural-language
rules to typed terminal-state facts — AI has no code path to create or approve one (spec §5),
and a missing attestation yields a typed `INSUFFICIENT_EVIDENCE`, never an inferred default.
Proof artifacts (§6.5) and scan reports are append-only rows in migrations 003–005. The
economics engine (§11.5) joins one proof with point-in-time books/fees and a frozen
`EconomicsPolicy`; it walks depth and never infers fills. The scan's only possible outcomes are
`REJECTED`, `INSUFFICIENT_EVIDENCE`, or `SHADOW_CANDIDATE` (spec §2/§7) — no live path exists.

**Tech Stack:** Python 3.12–3.14, Pydantic 2.13.4 strict/frozen models, DuckDB 1.5.4, argparse,
pytest 9.1.1, Ruff 0.15.22. All arithmetic in `Decimal`; timestamps aware-UTC.

**Spec:** `docs/superpowers/specs/2026-08-14-multi-venue-prediction-market-structural-opportunity-system-design.md`
(sections 4, 5, 6.5, 7, 11.4, 11.5, 13, 15.2, 15.3, 16 increment 3, 17.5–17.6). Scope ruling in
force: code-complete, fail-closed; no authenticated or live-execution code.

## Global Constraints

- Proof compilers and the economics engine are pure: inputs in, typed result out; no I/O.
- Nothing in this increment may create an execution state, an order, or an authenticated call.
- AI output can never create or approve an attestation, equivalence proof, payoff proof, or
  proposal (spec §5, §17.9). Attestations carry a human `review_identity` and are entered only
  via the `predictions attest` CLI from an operator-authored file.
- `unknown` in any binding proof or equivalence dimension rejects the proof (spec §6.5); missing
  books, fees, stale/crossed books make economics `INSUFFICIENT_EVIDENCE` (spec §14).
- Proof templates are versioned; only templates in the approved registry may emit `proof_ready`
  (spec §6.5 "novel proof templates require manual approval").
- The UI/CLI never prints `risk-free`, `guaranteed profit`, `approved to trade`, or
  `live eligible` (spec §13); a `SHADOW_CANDIDATE` is a research artifact, not an opportunity.
- Every new model extends `PredictionRecord`; SHA-256 lowercase hex; migrations continue at 003.
- All money/price/size arithmetic uses `Decimal`; no floats in economics.
- Run `.venv/bin/python -m pytest` (never bare pytest); `graphify update .` after code changes.

**Interfaces inherited from increments 1–2 (exact):** `PredictionRegistry.markets_by_venue_as_of /
rule_history`, `PredictionMarketStore.latest_book_as_of(venue, market_id, outcome_token_id,
as_of)`, `latest_fee_rate_as_of(venue, market_id, as_of)`, `append_book_snapshot`,
`append_fee_rate`, `candidate_relationships_as_of`, `existing_candidate_ids`,
`CandidateRelationship` / `CandidateLeg` / `RelationshipType` / `CandidateDisposition` in
`candidates_models.py`, `TypedProposition`/`PropositionSpan` in `propositions.py`,
`nominate_cross_venue_candidates` in `scout_bridge.py`, adapters
`PolymarketAdapter`/`KalshiAdapter` with `fetch_book_snapshot(market_id, outcome_token_id,
observed_at, cycle_id)` and `fetch_fee_rate(market_id, observed_at)`.

---

### Task 1: Rule-relevant rule-version identity (parked ruling from increment 2)

**Files:**
- Modify: `src/polytrading/predictions/polymarket.py`, `kalshi.py`, `limitless.py` (the
  `rule_version_id` derivation only)
- Test: `tests/predictions/test_polymarket.py`, `test_kalshi.py`, `test_limitless.py`

**Interfaces:**
- Consumes: each adapter's existing normalization internals.
- Produces: `rule_version_id = uuid5(_RULE_VERSION_NAMESPACE, canonical_json([venue, market_id,
  question, description, resolution_source, sorted-or-source-ordered outcomes, iso(end_at)]))`
  — derived from rule-relevant fields, NOT the whole page hash, so an unrelated byte change
  elsewhere on a page no longer mints new rule versions (and therefore no longer churns
  candidate identities). `RuleVersion.source_hash` stays the raw page hash (lineage unchanged).
  A shared helper `rule_relevant_version_id(...)` lives in `domain.py` so the three adapters
  cannot drift.
- Consumers: every proof compiler (rule-change invalidation becomes signal, not noise).

- [ ] **Step 1: Write failing tests** — per adapter: two fixture pages whose OTHER markets
  differ but whose target market's rule-relevant fields are identical → same `rule_version_id`
  across both fetches; a changed `resolution_source` (or question/outcomes/end_at) → new id.
- [ ] **Step 2: Observe failures; Step 3: implement the shared helper + three call sites;
  Step 4: run the three adapter test files, then full `tests/predictions`.**
- [ ] **Step 5: Commit** — `fix(predictions): derive rule versions from rule-relevant fields`

---

### Task 2: Scout-bridge same-venue guard and helper promotion (parked from increment 2)

**Files:**
- Modify: `src/polytrading/predictions/scout_bridge.py`, `candidates.py`
- Test: `tests/predictions/test_scout_bridge.py`

**Interfaces:**
- Produces: `ScoutAbstention.reason` gains `"SAME_VENUE"`; `nominate_cross_venue_candidates`
  returns it (before any retrieval) when `venue_a is venue_b`. `candidates.py` promotes
  `_all_legs_or_none` → `all_legs_or_none` and `_is_eligible_open_market` →
  `is_eligible_open_market` (old underscore names removed, both call sites updated).
- [ ] **Step 1: failing test** — equal venues abstain typed, never raise `ValidationError`.
- [ ] **Steps 2–4: implement, run `tests/predictions/test_scout_bridge.py` +
  `test_candidates.py`, full `tests/predictions`.**
- [ ] **Step 5: Commit** — `fix(predictions): abstain typed on same-venue nomination requests`

---

### Task 3: Book and fee collection wiring

**Files:**
- Modify: `src/polytrading/predictions/cli.py` (`_run_collect`)
- Test: `tests/predictions/test_cli.py`

**Interfaces:**
- Produces: `predictions collect polymarket|kalshi` gains `--books N` (default 0, meaning
  markets/rules only — existing behavior unchanged). With `--books N`: after persisting
  markets/rules in the same transaction, fetch one book snapshot per outcome token and one fee
  rate for up to N order-book-enabled, active, open markets (deterministic order: sorted
  market_id), under one fresh `cycle_id` (uuid4 is fine here — CLI, not workflow), all via the
  venue adapter's existing `fetch_book_snapshot`/`fetch_fee_rate`; append via
  `append_book_snapshot`/`append_fee_rate`. Per-market fetch errors become stderr warnings
  naming the market (collection continues; spec §14 isolates asset errors); `limitless
  --books N>0` is a usage error (`limitless_endpoint_not_collected` — books are not collected
  for the conditional venue by increment-2 ruling).
- [ ] **Step 1: failing CLI tests** — mock transport returning a book + fee for one market:
  assert stored via `latest_book_as_of`/`latest_fee_rate_as_of`; a failing second market warns
  but exits 0; `--books` on limitless is a usage error; default 0 fetches none.
- [ ] **Steps 2–4: implement, run `tests/predictions/test_cli.py`, full `tests/predictions`.**
- [ ] **Step 5: Commit** — `feat(predictions): collect executable books and fees per venue`

---

### Task 4: Rule attestations — models, migration 003, store, `predictions attest`

**Files:**
- Create: `src/polytrading/predictions/attestations.py`
- Create: `src/polytrading/predictions/storage/schema/003_rule_attestations.sql`
- Modify: `src/polytrading/predictions/storage/store.py`, `cli.py`
- Test: `tests/predictions/test_attestations.py`, `test_store.py`, `test_cli.py`

**Interfaces:**
- Produces:
  - `VoidBehavior = Literal["refund_at_cost", "resolve_to_rules_price", "unknown"]`
  - `RuleAttestation(PredictionRecord)`: `schema_version: Literal[1]`,
    `attestation_id: UUID`, `venue: PredictionVenue`, `market_id: str`,
    `rule_version_id: UUID`, `rule_source_hash: Sha256`,
    `payout_unit: Literal["usdc_1_per_share", "usd_1_per_contract"]`,
    `winner_payout_per_share: Decimal (gt=0)`, `loser_payout_per_share: Decimal (ge=0)`,
    `outcome_set_exhaustive: bool`, `void_or_invalid_possible: bool`,
    `void_behavior: VoidBehavior`, `tie_possible: bool`, `tie_behavior: str | None`,
    `resolution_source_attested: str`, `deadline_utc: datetime | None`,
    `threshold_text: str | None`, `threshold_inclusive: bool | None`,
    `supporting_spans: tuple[PropositionSpan, ...]` (≥1, each span's `rule_source_hash` must
    equal the attestation's), `review_identity: str` (non-empty), `reviewed_at: datetime`.
    Validators: `void_or_invalid_possible=True` ⇒ `void_behavior != "unknown"` is NOT required
    (unknown is representable) but proofs will reject it; `tie_possible=True` ⇒ `tie_behavior`
    non-None.
  - Migration `003_rule_attestations.sql`: table `rule_attestations (attestation_id UUID NOT
    NULL, venue VARCHAR NOT NULL, market_id VARCHAR NOT NULL, rule_version_id UUID NOT NULL,
    reviewed_at TIMESTAMPTZ NOT NULL, record_json VARCHAR NOT NULL, record_hash VARCHAR NOT
    NULL);`
  - Store: `append_rule_attestation(record) -> bool` (`_append_keyed`, key `attestation_id`);
    `latest_attestation_for_rule_version(rule_version_id, as_of) -> RuleAttestation | None`
    (newest `reviewed_at <= as_of`).
  - CLI: `predictions attest --db <path> --input <json-file>` — parses a JSON array of
    attestation objects strictly (`RuleAttestation.model_validate`), verifies each
    `rule_version_id` exists in the registry and its `rule_source_hash` matches the stored
    `RuleVersion.source_hash` (mismatch = usage error naming the id), appends under the writer
    lease in one transaction, prints appended/known counts. There is deliberately no path that
    generates attestation content — the file is operator-authored (spec §5).
- [ ] **Step 1: failing model + store + CLI tests** (validator branches both ways; hash-binding
  mismatch rejected; idempotent re-import; migration-count fixtures updated for 003).
- [ ] **Steps 2–4: implement; run `tests/predictions` fully.**
- [ ] **Step 5: Commit** — `feat(predictions): add human-reviewed rule attestations`

---

### Task 5: Proof artifact models, migration 004, store APIs

**Files:**
- Create: `src/polytrading/predictions/proofs_models.py`
- Create: `src/polytrading/predictions/storage/schema/004_proof_artifacts.sql`
- Modify: `src/polytrading/predictions/storage/store.py`
- Test: `tests/predictions/test_proofs_models.py`, `test_store.py`

**Interfaces (spec §6.5, every bullet):**
- `APPROVED_PROOF_TEMPLATES: frozenset[str] = frozenset({"binary_complement@1",
  "exhaustive_outcome_set@1", "logical_implication@1", "cross_venue_equivalence@1"})`
- `TerminalState(PredictionRecord)`: `state_id: str` (non-empty, unique within a proof),
  `description: str`, `leg_payouts: tuple[Decimal, ...]` (one per candidate leg, ge=0).
- `ProofAssumption(PredictionRecord)`: `claim: str`, `attestation_id: UUID`,
  `supporting_spans: tuple[PropositionSpan, ...]`.
- `ExcludedState(PredictionRecord)`: `description: str`, `exclusion_reason: str`,
  `attestation_id: UUID`.
- `EquivalenceDimensionResult = Literal["compatible", "incompatible", "unknown"]`;
  `EquivalenceMatrix(PredictionRecord)`: one `EquivalenceDimensionResult` field per Engine-D
  dimension using exactly the eight increment-2 unresolved-field names as field names, plus
  `basis_attestation_ids: tuple[UUID, ...]`.
- `ProofStatus = Literal["proof_ready", "rejected", "insufficient_evidence"]`
- `ProofRejectionReason = Literal["MISSING_ATTESTATION", "OUTCOME_SET_NOT_EXHAUSTIVE",
  "VOID_BEHAVIOR_UNKNOWN", "TIE_UNMODELED", "IMPLICATION_INVALID",
  "EQUIVALENCE_DIMENSION_UNKNOWN", "EQUIVALENCE_DIMENSION_INCOMPATIBLE",
  "TEMPLATE_NOT_APPROVED", "RULE_VERSION_CHANGED", "PROPOSITIONS_NOT_EXTRACTED"]`
- `ProofArtifact(PredictionRecord)`: `schema_version: Literal[1]`, `proof_id: UUID`,
  `candidate_id: UUID`, `template: str`, `compiler_version: str`,
  `status: ProofStatus`, `rejection_reason: ProofRejectionReason | None`,
  `terminal_states: tuple[TerminalState, ...]`, `minimum_basket_payout: Decimal | None`,
  `maximum_basket_payout: Decimal | None`, `assumptions: tuple[ProofAssumption, ...]`,
  `excluded_states: tuple[ExcludedState, ...]`,
  `equivalence_matrix: EquivalenceMatrix | None` (required iff template is
  `cross_venue_equivalence@1`), `rule_version_ids: tuple[UUID, ...]`,
  `source_hashes: tuple[Sha256, ...]` (sorted unique lineage),
  `review_identity: str`, `invalidation_conditions: tuple[str, ...]` (must include
  `"any participating rule_version change"`), `information_cutoff: datetime`,
  `observed_at: datetime`.
  Validators: `proof_ready` ⇒ `rejection_reason is None`, ≥1 terminal state, min/max set,
  `template in APPROVED_PROOF_TEMPLATES`, every `TerminalState.leg_payouts` length equal;
  non-`proof_ready` ⇒ `rejection_reason` set (for `insufficient_evidence` use
  `MISSING_ATTESTATION`/`PROPOSITIONS_NOT_EXTRACTED` etc.), `minimum_basket_payout is None`.
- Migration `004_proof_artifacts.sql`: `proof_artifacts (proof_id UUID NOT NULL, candidate_id
  UUID NOT NULL, template VARCHAR NOT NULL, status VARCHAR NOT NULL, observed_at TIMESTAMPTZ
  NOT NULL, information_cutoff TIMESTAMPTZ NOT NULL, record_json VARCHAR NOT NULL, record_hash
  VARCHAR NOT NULL);`
- Store: `append_proof_artifact(record) -> bool` (key `proof_id`);
  `proof_artifacts_for_candidate(candidate_id, as_of)` (all, ordered by observed_at);
  `latest_proof_for_candidate(candidate_id, as_of) -> ProofArtifact | None`.
- [ ] **Step 1: failing model/store tests** (validator branches both directions; unapproved
  template cannot be `proof_ready`; migration fixtures updated for 004).
- [ ] **Steps 2–4: implement; full `tests/predictions`.** — **Step 5: Commit**
  `feat(predictions): add deterministic proof artifact models and storage`

---

### Task 6: Binary complement payoff compiler

**Files:**
- Create: `src/polytrading/predictions/proofs.py` (shared compiler entry + this template)
- Test: `tests/predictions/test_proofs_complement.py`

**Interfaces:**
- Produces: `compile_proof(candidate, rule_versions, attestations, *, as_of, review_identity)
  -> ProofArtifact` — dispatches on `candidate.relationship_type`; this task implements the
  `BINARY_COMPLEMENT` branch (`binary_complement@1`, compiler_version `"1"`), pure function.
  Logic: both legs are one market's two outcomes. Require the market's attestation (else
  `insufficient_evidence`/`MISSING_ATTESTATION`); require `outcome_set_exhaustive` (else
  `rejected`/`OUTCOME_SET_NOT_EXHAUSTIVE`); require the candidate's `rule_version_id`s to still
  be each market's rule version in `rule_versions` (else `rejected`/`RULE_VERSION_CHANGED`).
  States: `outcome_0_wins` (leg payouts `(winner, loser)`), `outcome_1_wins` (`(loser,
  winner)`); if `void_or_invalid_possible`: `void_behavior == "refund_at_cost"` adds an
  excluded state (exclusion valid: refund returns cost, documented via attestation);
  `"resolve_to_rules_price"` adds a modeled `void` terminal state with payouts
  `(loser_payout_per_share, loser_payout_per_share)` only if that is what the attested behavior
  implies — otherwise `rejected`/`VOID_BEHAVIOR_UNKNOWN`; `tie_possible` without a modeled
  behavior → `rejected`/`TIE_UNMODELED`. `minimum_basket_payout = min over states of
  sum(leg_payouts)`; maximum analogous.
- [ ] **Step 1: failing tests** — happy path emits `proof_ready` with min payout =
  `winner+loser` per attested values; each mutation (missing attestation, non-exhaustive,
  void-unknown, tie-unmodeled, changed rule version) yields the exact
  status/rejection_reason; proof is deterministic (two calls → identical artifact minus
  `proof_id`, so derive `proof_id` = uuid5 over canonical content to make it fully identical
  and idempotent to persist).
- [ ] **Steps 2–4: implement; full `tests/predictions`.** — **Step 5: Commit**
  `feat(predictions): compile exhaustive binary complement payoff proofs`

---

### Task 7: Exhaustive multi-outcome payoff compiler

**Files:**
- Modify: `src/polytrading/predictions/proofs.py`
- Test: `tests/predictions/test_proofs_outcome_set.py`

**Interfaces:**
- Produces: the `EXHAUSTIVE_OUTCOME_SET` branch (`exhaustive_outcome_set@1`). N legs = N member
  markets; requires EVERY member's attestation with `outcome_set_exhaustive=True` at the group
  level — the group-level exhaustiveness claim is its own explicit `ProofAssumption` backed by
  each attestation; any member missing → `MISSING_ATTESTATION`. States: `member_i_wins` with
  leg payouts (winner for i's YES-side leg, loser for others — legs here are one-per-market as
  built by the increment-2 generator, so a "basket" is buying each member's YES; payout vector
  = winner for exactly one leg per state). Void/tie handling identical in shape to Task 6, but
  ANY member with unmodeled void/tie rejects the whole proof.
- [ ] **Step 1: failing tests** — 3-member happy path (`min = winner + 2*loser` per attested
  values), each mutation exact, determinism/idempotent proof_id.
- [ ] **Steps 2–4; Step 5: Commit** `feat(predictions): compile exhaustive multi-outcome payoff proofs`

---

### Task 8: Logical implication compiler

**Files:**
- Modify: `src/polytrading/predictions/proofs.py`
- Test: `tests/predictions/test_proofs_implication.py`

**Interfaces:**
- Produces: the `LOGICAL_IMPLICATION` branch (`logical_implication@1`) for two-leg
  `LOGICAL_IMPLICATION` candidates (constructible via `candidate_helpers`; no generator emits
  them yet — that is fine, the compiler is the deliverable). Inputs: both legs' attestations
  AND both legs' `TypedProposition`s with `status="extracted"` and kind `threshold` or
  `deadline` (else `insufficient_evidence`/`PROPOSITIONS_NOT_EXTRACTED`). Implication check
  (deterministic, no NL): thresholds — same subject string, same attested resolution source,
  same deadline, numeric values with attested inclusivity such that `A ⇒ B` (e.g. `price >=
  110k` ⇒ `price >= 100k`; inclusivity handled exactly: `> 100k` ⇒ `>= 100k` valid, converse
  invalid); deadlines — same subject/threshold, `deadline_A <= deadline_B` for "by-deadline"
  events, using attested `deadline_utc` (timezone-explicit). Any mismatch in subject, source,
  timezone, or inclusivity that breaks the implication → `rejected`/`IMPLICATION_INVALID`.
  Basket = `NO(A) + YES(B)` (leg 0 = NO on A, leg 1 = YES on B): states `neither`, `B_only`,
  `both` (A-without-B excluded BECAUSE the implication holds — recorded as the proof's
  excluded state with the implication assumption); min payout = min(winner_A_no + loser_B,
  winner_A_no + winner_B, loser_A_no + winner_B) computed from the two attestations.
- [ ] **Step 1: failing tests** — nested-threshold happy path; inclusivity flip rejects;
  timezone/source mismatch rejects; unknown propositions insufficient; determinism.
- [ ] **Steps 2–4; Step 5: Commit** `feat(predictions): compile logical implication payoff proofs`

---

### Task 9: Cross-venue equivalence compiler

**Files:**
- Modify: `src/polytrading/predictions/proofs.py`
- Test: `tests/predictions/test_proofs_equivalence.py`

**Interfaces:**
- Produces: the `CROSS_VENUE_EQUIVALENCE` branch (`cross_venue_equivalence@1`). Builds the
  8-dimension `EquivalenceMatrix` by comparing the two legs' attestations field-by-field:
  threshold text+inclusivity, deadline_utc, resolution_source_attested, void/tie behaviors,
  outcome exhaustiveness, payout unit + per-share values, settlement facts derivable from
  attestations (dimensions with no attested basis stay `"unknown"`). Every dimension must be
  `"compatible"`: any `"unknown"` → `rejected`/`EQUIVALENCE_DIMENSION_UNKNOWN`; any
  `"incompatible"` → `rejected`/`EQUIVALENCE_DIMENSION_INCOMPATIBLE`; the artifact's matrix
  records exactly which. A compatible pair yields the two-state payoff table (same proposition
  true/false across both venues) with min basket payout from buying opposite sides.
- [ ] **Step 1: failing tests** — a fully-compatible synthetic pair reaches `proof_ready`;
  EVERY hard-negative fixture pair from `tests/fixtures/predictions/hard_negatives.json`,
  attested faithfully to its texts, is `rejected` with a matrix naming its divergent dimension
  (table-driven test over all six pairs — spec §15.2's mutation/hard-negative requirement);
  an unattested dimension rejects as `unknown`, never guessed.
- [ ] **Steps 2–4; Step 5: Commit** `feat(predictions): compile cross-venue equivalence proofs`

---

### Task 10: Conservative economics engine

**Files:**
- Create: `src/polytrading/predictions/economics.py`, `economics_models.py`
- Test: `tests/predictions/test_economics.py`

**Interfaces (spec §7 verbatim formulas):**
- `PredictionEconomicsPolicy(PredictionRecord)`: frozen reserve parameters —
  `gas_conversion_redemption_reserve_usd: Decimal`, `currency_basis_reserve_rate: Decimal`,
  `transfer_cost_usd: Decimal`, `capital_lockup_rate_per_day: Decimal`,
  `operational_cost_usd: Decimal`, `partial_fill_reserve_rate: Decimal`,
  `latency_reserve_rate: Decimal`, `dispute_delay_reserve_rate: Decimal`,
  `venue_failure_reserve_rate: Decimal`, `max_book_age_seconds: int`, plus
  `policy_id: str`, `policy_version: str`. A module-level
  `DEFAULT_RESEARCH_POLICY` constant with conservative documented values.
- `LegExecutionPlan(PredictionRecord)`: leg index, venue, market_id, outcome_token_id,
  depth-walked levels `tuple[(price, size), ...]`, `filled_quantity`, `acquisition_cost_usd`.
- `EconomicsResult(PredictionRecord)`: `status: Literal["evaluated",
  "insufficient_evidence"]`, `insufficiency_reason: Literal["MISSING_BOOK", "STALE_BOOK",
  "CROSSED_BOOK", "MISSING_FEE", "ZERO_EXECUTABLE_DEPTH"] | None`, quantity, per-leg plans,
  `proven_floor_usd`, `all_in_cost_usd`, `failure_reserve_usd`, `conservative_surplus_usd`,
  `return_on_assigned_capital`, `capacity_usd_at_current_depth`,
  `stranded_collateral_by_venue: dict[str, Decimal]`, `max_capital_lock_days: Decimal`,
  `doubled_cost_surplus_usd` (surplus with every cost and reserve doubled), all `Decimal`.
- `evaluate_basket_economics(proof, candidate, store_reads, policy, as_of) ->
  EconomicsResult` — pure over injected reads (caller passes the books/fees already loaded;
  signature takes `books: mapping leg→PredictionBookSnapshot | None`, `fees: mapping
  leg→PredictionFeeRate | None`): walks each leg's ask side to the max quantity executable
  across ALL legs (bottleneck leg determines q; incompatible quantity increments floor to the
  smaller), computes §7's three quantities exactly as specified, assumes capital pre-positioned
  (no just-in-time transfer credit), gives zero credit for rewards. Missing/stale
  (`observed_at` older than `policy.max_book_age_seconds` before `as_of`)/crossed book or
  missing fee → `insufficient_evidence` with the exact reason.
- [ ] **Step 1: failing tests** — two-leg complement with seeded books/fees: hand-computed
  `conservative_surplus_usd` matches to the cent; multi-level depth walk verified against a
  hand-walked book; each insufficiency reason; doubled-costs stress strictly lower; zero depth.
- [ ] **Steps 2–4; Step 5: Commit** `feat(predictions): add the depth-aware conservative economics engine`

---

### Task 11: Scan reports, migration 005, CLI `prove` + `scan`

**Files:**
- Create: `src/polytrading/predictions/storage/schema/005_scan_reports.sql`
- Modify: `src/polytrading/predictions/storage/store.py`, `cli.py`,
  `src/polytrading/predictions/economics_models.py` (report record)
- Test: `tests/predictions/test_cli.py`, `test_store.py`

**Interfaces:**
- `ScanDecision = Literal["SHADOW_CANDIDATE", "REJECTED", "INSUFFICIENT_EVIDENCE"]`
- `ScanReport(PredictionRecord)`: `report_id: UUID` (uuid5 over canonical content),
  `candidate_id`, `proof_id: UUID | None`, `decision: ScanDecision`, `reason: str`,
  `economics: EconomicsResult | None`, `policy_id`, `policy_version`, `as_of`, `observed_at`.
  Validator: `SHADOW_CANDIDATE` ⇒ proof present + `economics.status == "evaluated"` +
  `conservative_surplus_usd > 0`.
- Migration `005_scan_reports.sql` (same column pattern as 004, key `report_id`); store
  `append_scan_report`, `scan_reports_as_of(as_of)`.
- CLI `predictions prove --db --candidate-id <uuid> [--as-of] [--review-identity <str>]
  [--format]`: loads the candidate (error if absent), its rule versions and latest
  attestations at as_of, runs `compile_proof`, persists idempotently, prints
  status/reason/min-payout. CLI `predictions scan --db [--as-of] [--format]`: for every
  candidate at as_of — latest proof: none/non-ready → `REJECTED` (reason from proof) or
  `INSUFFICIENT_EVIDENCE` (insufficient proofs and missing attestations); proof_ready → load
  each leg's latest book+fee from the store, run economics with
  `DEFAULT_RESEARCH_POLICY`; surplus > 0 → `SHADOW_CANDIDATE` else `REJECTED`; persist
  reports idempotently; print a per-decision tally and per-SHADOW_CANDIDATE one-liners
  (candidate id, surplus, capacity — no forbidden words). Both commands: writer lease, single
  transaction, sanitized failure wrapper (Task-10-of-increment-2 pattern), and the sanitized
  error class reused.
- [ ] **Step 1: failing CLI/store tests** — prove happy + missing-candidate + idempotent
  re-prove; scan end-to-end on a seeded store (market, rules, attestation, candidate, books,
  fees) producing one SHADOW_CANDIDATE with hand-checked surplus; scan without books →
  INSUFFICIENT_EVIDENCE; re-scan idempotent; validator branch tests.
- [ ] **Steps 2–4; Step 5: Commit** `feat(predictions): add prove and scan commands with append-only reports`

---

### Task 12: Dashboard proofs and scan panels

**Files:**
- Modify: `src/polytrading/predictions/dashboard.py`, `dashboard_models.py`, `web_assets/*`
- Test: `tests/predictions/test_dashboard.py`, `test_dashboard_models.py`

**Interfaces:**
- Snapshot gains `proofs: ProofSummary` (`total`, `by_status`, `by_template`,
  `latest: tuple[ProofListing, ...]` ≤20 newest-first — proof_id, candidate_id, template,
  status, rejection_reason, min payout, observed_at) and `scans: ScanSummary` (`total`,
  `by_decision`, `latest: tuple[ScanListing, ...]` ≤20 — candidate_id, decision, reason,
  surplus, capacity, as_of). Rendering: statuses/decisions rendered verbatim; the words
  `risk-free`, `guaranteed`, `approved`, `live eligible` never appear (extend the existing
  forbidden-words test with `live eligible`); SHADOW_CANDIDATE rows carry the caption
  `research decision — not an instruction to trade`; empty states render; `_recipes()` gains
  `predictions attest`, `prove`, `scan`, and `collect --books` examples.
- [ ] **Step 1: failing tests** (mirror the increment-2 candidates-panel test set: counts,
  cutoff-safety, forbidden words, empty states). **Steps 2–4; Step 5: Commit**
  `feat(predictions): render proof and scan evidence on the dashboard`

---

### Task 13: README §19, full verification, graph update

- [ ] **Step 1:** README section documenting: attestation workflow (operator-authored JSON →
  `predictions attest`), the four proof templates and their fail-closed reasons, `--books`
  collection, `prove`/`scan`, and the three-state scan outcome — in the established no-profit
  register ("a stable rejection is a valid result"; SHADOW_CANDIDATE is a research artifact,
  not an opportunity or instruction). Update any §18 text made stale.
- [ ] **Step 2:** `.venv/bin/python -m pytest` (full), `.venv/bin/ruff check .`,
  `.venv/bin/ruff format --check .` — exact tallies in the report.
- [ ] **Step 3:** `graphify update .` (commit README only — graphify-out is gitignored).
- [ ] **Step 4: Commit** — `docs(predictions): document attestations, proofs, and the scan`

---

## Self-review notes

- Spec §16.3 coverage: complement and multi-outcome proofs (T6, T7), implication templates
  (T8), cross-venue equivalence compiler (T9), depth-aware economics (T10), capital
  fragmentation (`stranded_collateral_by_venue`, T10), and reports (T11) — plus §6.5's
  artifact contract (T5), the §5 human-review path proofs require (T4), and §13's `prove`/
  `scan` commands (T11). Replay/shadow (§16.4) and anything authenticated (§16.5) are
  deliberately absent.
- The two increment-2 parked items are Tasks 1–2; book/fee CLI wiring (T3) closes the gap
  between increment-1 storage capabilities and the economics engine's evidence needs.
- Attestations are the deliberate answer to "where do typed rule facts come from without AI
  authority": operator-authored, hash-bound to exact rule versions, span-backed, append-only.
  Proof compilers reject rather than infer whenever attestations are missing or incomplete.
- Proof/report ids are content-derived (uuid5) so `prove`/`scan` re-runs are idempotent by the
  same mechanism increment 2 settled for candidates (pre-check + `already_known`).
