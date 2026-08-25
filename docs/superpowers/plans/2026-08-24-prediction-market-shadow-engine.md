# Prediction-Market Increment 4: Replay and Shadow Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement increment 4 of the multi-venue prediction-market system: the spec-§8
non-atomic shadow state machine and frozen plans, the §9 independent risk policy engine, the
event-time simulator with latency/partial-fill/venue-failure stress, the double-entry shadow
ledger with reconciliation, the §15.4 experiment registry, the `predictions shadow replay` /
`predictions shadow run` CLI, and dashboard shadow panels. No authenticated endpoint, wallet, or
live order exists anywhere in this increment (spec §11.6: the engine "cannot call authenticated
endpoints").

**Architecture:** Everything extends `src/polytrading/predictions/` and its DuckDB store
(migrations continue at 006). The shadow lifecycle is event-sourced: an immutable `ShadowPlan`
(the §8 freeze list) plus an append-only sequence of `ShadowEvent` state transitions per
proposal; current state is derived from events, never stored mutably. The simulator is a pure
library over injected point-in-time evidence (books at offset times) — no clock, no network —
and the risk engine is an independent module whose limits strategy code cannot raise (§11.7).
The ledger is double-entry: every event posts balanced debits/credits across per-venue cash,
position, fee, and reserve accounts, and a proposal's paper result is invalid until every
participating venue reconciles (§8). Stress is expressed as deterministic `StressScenario`
records applied at replay time. All established conventions bind: strict/frozen Pydantic
records, Decimal arithmetic, UTC, SHA-256 lineage, content-derived uuid5 identities,
`_append_keyed` idempotency, cutoff-safe reads, sanitized CLI errors, forbidden-vocabulary
rules (§13).

**Tech Stack:** Python 3.12–3.14, Pydantic 2.13.4, DuckDB 1.5.4, argparse, pytest, Ruff.

**Spec:** `docs/superpowers/specs/2026-08-14-multi-venue-prediction-market-structural-opportunity-system-design.md`
(sections 8, 9, 11.6, 11.7, 13, 14, 15.3, 15.4, 16 increment 4, 17.7–17.8). Scope ruling in
force: code-complete, fail-closed; nothing live.

## Global Constraints

- The shadow engine can never call authenticated endpoints, sign, or submit anything; `shadow
  run` and `shadow replay` read/write the local DB only.
- No multi-leg atomicity is ever assumed; any first-leg fill is temporary directional exposure
  until completion or unwind (§8). The engine never averages into a broken basket and never
  assumes an unconfirmed cancellation; unknown order state halts new simulated entries (§8).
- Risk limits live in `predictions/risk.py` and are enforced independently; no strategy code
  path may raise a limit (§11.7). §9 numbers are binding: basket ≤ 5% of equity, event cluster
  ≤ 10%, incomplete-leg modeled loss < 0.25%, drawdown ladder 2%/5%/8%/12%/15%, no leverage.
- Ledger postings balance exactly (Σ debits == Σ credits per event, Decimal); a proposal's
  paper P&L is invalid until reconciled (§8); rewards/rebates receive zero credit (§7).
- Trial families are preregistered before results exist; failed experiments are recorded, never
  deleted (§15.4); no future evidence leaks into an earlier cutoff (§12).
- UI/CLI vocabulary rules hold (no `risk-free`/`guaranteed`/`approved`/`live eligible`);
  shadow results are research artifacts, and the dashboard must "not label an opportunity …
  approved to trade" (§13).
- Migrations sequential from 006; migration-count fixtures updated (tests/predictions/test_store.py
  + tests/test_package.py precedents).
- `.venv/bin/python -m pytest` (never bare pytest); ruff check + format clean per task.
  `graphify` is unavailable on this machine — do not invoke it.

**Inherited interfaces (exact):** `ScanReport`/`EconomicsResult`/`LegExecutionPlan`/
`PredictionEconomicsPolicy`/`DEFAULT_RESEARCH_POLICY` (`economics_models.py`),
`evaluate_basket_economics` (`economics.py`), `ProofArtifact`/`latest_proof_for_candidate`,
`CandidateRelationship`/`CandidateLeg`, `PredictionBookSnapshot` (bids/asks levels),
store reads `scan_reports_as_of`, `latest_book_as_of(venue, market_id, outcome_token_id, as_of)`,
`candidate_relationships_as_of`, writer lease + sanitized-error CLI patterns in `cli.py`,
dashboard summary/panel conventions (`dashboard_models.py`, `dashboard.py`, `web_assets/`).

---

### Task 1: Shadow domain models — states, events, frozen plan

**Files:**
- Create: `src/polytrading/predictions/shadow_models.py`
- Test: `tests/predictions/test_shadow_models.py`

**Interfaces (spec §8 verbatim):**
- `ShadowState(StrEnum)`: `DISCOVERED`, `PROOF_VALIDATED`, `ECONOMICS_VALIDATED`,
  `SHADOW_PLANNED`, `FIRST_LEG_SIMULATED`, `COMPLETE`, `UNWOUND`, `EXPIRED`, `UNKNOWN`,
  `RECONCILED`.
- `ALLOWED_TRANSITIONS: frozenset[tuple[ShadowState, ShadowState]]` — exactly §8's chain:
  DISCOVERED→PROOF_VALIDATED→ECONOMICS_VALIDATED→SHADOW_PLANNED→FIRST_LEG_SIMULATED→
  {COMPLETE, UNWOUND, EXPIRED, UNKNOWN}→RECONCILED; plus UNKNOWN→RECONCILED only after
  reconciliation (same edge), no self-loops, nothing else.
- `ShadowLegPlan(PredictionRecord)`: `leg_index: int (ge=0)`, `venue: PredictionVenue`,
  `market_id`, `outcome_token_id: str | None`, `sequence_position: int (ge=0)` (venue-qualified
  leg order), `limit_price_levels: tuple[tuple[Decimal, Decimal], ...]` (price, max size per
  level — per-level limit prices from the frozen books), `max_quantity: Decimal (gt=0)`.
- `ShadowPlan(PredictionRecord)` — the §8 freeze list, every bullet:
  `schema_version: Literal[1]`, `proposal_id: UUID`, `candidate_id: UUID`, `proof_id: UUID`,
  `scan_report_id: UUID`, `legs: tuple[ShadowLegPlan, ...]` (≥2), `bottleneck_leg_index: int`,
  `max_quantity: Decimal (gt=0)`, `order_policy: Literal["taker_cross_only"]` (v1),
  `expires_at: datetime`, `completion_path: str`, `cancellation_path: str`, `unwind_path: str`
  (non-empty prose from the planner), `max_incomplete_exposure_usd: Decimal (ge=0)`,
  `max_incomplete_loss_usd: Decimal (ge=0)`, `frozen_hashes: tuple[Sha256, ...]` (sorted
  unique: rule/fee/book/policy lineage), `policy_id: str`, `policy_version: str`,
  `risk_policy_version: str`, `minimum_basket_payout: Decimal (gt=0)` (the proof's frozen
  per-unit conservative payout floor, required for deterministic ledger valuation),
  `kill_conditions: tuple[str, ...]` (non-empty),
  `information_cutoff: datetime`, `observed_at: datetime`.
  Validators: `bottleneck_leg_index` within legs; leg `sequence_position`s are a permutation
  of 0..N-1; `expires_at > observed_at`.
- `ShadowFill(PredictionRecord)`: immutable structured execution evidence retained for ledger and
  replay: `leg_index: int (ge=0)`, `side: Literal["buy", "sell"]`,
  `price_levels: tuple[tuple[Decimal, Decimal], ...]` (non-empty positive price/quantity pairs),
  `quantity: Decimal (gt=0)`; validator requires quantity to equal the summed level quantities.
- `ShadowEvent(PredictionRecord)`: `schema_version: Literal[1]`, `event_id: UUID`,
  `proposal_id: UUID`, `sequence: int (ge=0)`, `from_state: ShadowState | None` (None only for
  sequence 0), `to_state: ShadowState`, `occurred_at: datetime`, `detail: str`,
  `quantity_filled: Decimal | None`, `leg_index: int | None`, `scenario_id: str | None`,
  `fills: tuple[ShadowFill, ...] = ()`. Structured fills are authoritative machine-readable
  execution evidence; `detail` remains prose and is never parsed by the ledger.
  Validator: `(from_state, to_state) in ALLOWED_TRANSITIONS` when from_state is not None;
  sequence 0 ⇒ from_state None and to_state DISCOVERED.
- `derive_current_state(events: Sequence[ShadowEvent]) -> ShadowState` — pure; validates the
  chain is contiguous (sequences 0..n, each from_state == previous to_state) else raises
  `ValueError` (a broken chain is a data-integrity error).
- `deterministic_proposal_id(scan_report_id, plan-content) -> UUID` uuid5 content-derived.
- [ ] TDD steps per convention (failing tests incl. every illegal transition direction, chain
  derivation, permutation validator), implement, run `tests/predictions`, commit
  `feat(predictions): add the shadow proposal state machine models`.

---

### Task 2: Migration 006 and shadow store APIs

**Files:**
- Create: `src/polytrading/predictions/storage/schema/006_shadow_core.sql`
- Modify: `src/polytrading/predictions/storage/store.py`
- Test: `tests/predictions/test_store.py`

**Interfaces:**
- Migration 006 creates four append-only tables, same column conventions as 004/005
  (`record_json`, `record_hash` + typed key columns):
  `shadow_plans (proposal_id UUID NOT NULL, candidate_id UUID NOT NULL, observed_at TIMESTAMPTZ
  NOT NULL, information_cutoff TIMESTAMPTZ NOT NULL, record_json VARCHAR NOT NULL, record_hash
  VARCHAR NOT NULL);`
  `shadow_events (event_id UUID NOT NULL, proposal_id UUID NOT NULL, sequence INTEGER NOT NULL,
  occurred_at TIMESTAMPTZ NOT NULL, record_json VARCHAR NOT NULL, record_hash VARCHAR NOT NULL);`
  `shadow_ledger_postings (posting_id UUID NOT NULL, proposal_id UUID NOT NULL, event_id UUID
  NOT NULL, occurred_at TIMESTAMPTZ NOT NULL, record_json VARCHAR NOT NULL, record_hash VARCHAR
  NOT NULL);`
  `shadow_reconciliations (reconciliation_id UUID NOT NULL, proposal_id UUID NOT NULL,
  observed_at TIMESTAMPTZ NOT NULL, record_json VARCHAR NOT NULL, record_hash VARCHAR NOT NULL);`
- Store methods (all `_append_keyed`-idempotent by their id, cutoff-safe ordered reads):
  `append_shadow_plan`, `shadow_plans_as_of(as_of)`, `shadow_plan_by_proposal(proposal_id)`;
  `append_shadow_event` — MUST additionally reject (raise `ConflictingRecordError`) a second
  event with the same `(proposal_id, sequence)` but different `event_id` (the same probe-first
  pattern, keyed on the pair), `shadow_events_for_proposal(proposal_id, as_of)` ordered by
  sequence; `append_ledger_posting`, `ledger_postings_for_proposal(proposal_id, as_of)`;
  `append_reconciliation`, `latest_reconciliation_for_proposal(proposal_id, as_of)`.
- Migration-count fixtures updated (006 joins the contiguity list in test_store.py and the wheel
  member list in tests/test_package.py).
- [ ] TDD, implement, full `tests/predictions`, commit
  `feat(predictions): add shadow storage with event-sequence integrity`.

---

### Task 3: Risk policy engine (§9)

**Files:**
- Create: `src/polytrading/predictions/risk.py`
- Test: `tests/predictions/test_risk.py`

**Interfaces:**
- `PredictionRiskPolicy(PredictionRecord)` frozen, with `policy_version: str` and §9's exact
  numbers as defaulted fields: `max_basket_fraction_of_equity: Decimal = 0.05`,
  `max_event_cluster_fraction: Decimal = 0.10`, `max_incomplete_loss_fraction: Decimal =
  0.0025`, `drawdown_halt_new_entries: Decimal = 0.02` (24h), `drawdown_halve_size: Decimal =
  0.05`, `drawdown_stop_all: Decimal = 0.08`, `drawdown_close_nonguaranteed: Decimal = 0.12`,
  `drawdown_capital_preservation: Decimal = 0.15`, `max_live_venues: int = 2`,
  `pilot_cap_usd: Decimal = 250`. `DEFAULT_RISK_POLICY` module constant.
- `ShadowPortfolioState(PredictionRecord)`: `total_equity_usd: Decimal (gt=0)`,
  `open_exposure_usd_by_cluster: dict[str, Decimal]`, `peak_equity_usd: Decimal`,
  `equity_24h_ago_usd: Decimal`, `open_proposal_count: int`.
- `RiskRefusalReason = Literal["BASKET_TOO_LARGE", "CLUSTER_CONCENTRATION",
  "INCOMPLETE_LOSS_TOO_LARGE", "DRAWDOWN_HALT", "DRAWDOWN_STOP_ALL",
  "CAPITAL_PRESERVATION_MODE"]`
- `RiskGateDecision(PredictionRecord)`: `allowed: bool`, `reason: RiskRefusalReason | None`,
  `size_multiplier: Decimal` (1 normally; 0.5 when the 5% peak-to-trough halving applies),
  `policy_version: str`.
- `evaluate_risk_gate(*, basket_cost_usd, max_incomplete_loss_usd, event_cluster_id,
  portfolio, policy) -> RiskGateDecision` — pure; checks in fixed order: capital-preservation
  (≥15% peak-to-trough) → stop-all (≥8%) → 24h halt (≥2% daily loss) → basket fraction →
  cluster fraction (including this basket) → incomplete-loss fraction; the 5% halving sets
  `size_multiplier=0.5` without refusing. Strategy code cannot raise limits: the function takes
  the policy as a frozen value and nothing in `predictions/` mutates it (no setter exists).
- [ ] TDD (each refusal reason both directions; halving; ordering when multiple trip — most
  severe wins per the fixed order), implement, full suite, commit
  `feat(predictions): add the independent shadow risk policy engine`.

---

### Task 4: Shadow planner

**Files:**
- Create: `src/polytrading/predictions/shadow_planner.py`
- Test: `tests/predictions/test_shadow_planner.py`

**Interfaces:**
- `PlanRefusal(PredictionRecord)`: `reason: Literal["SCAN_NOT_SHADOW_CANDIDATE",
  "PROOF_NOT_CURRENT", "RISK_REFUSED", "MISSING_EVIDENCE"]`, `detail: str`,
  `risk: RiskGateDecision | None`.
- `plan_shadow_proposal(*, scan_report, candidate, proof, books, fees, economics_policy,
  risk_policy, portfolio, as_of, expiry_window_seconds: int) -> ShadowPlan | PlanRefusal` —
  pure. Requires `scan_report.decision == "SHADOW_CANDIDATE"` (else SCAN_NOT_SHADOW_CANDIDATE);
  re-runs `evaluate_basket_economics` on the supplied books/fees and refuses MISSING_EVIDENCE
  if not evaluated or surplus ≤ 0 (evidence may have moved since the scan — fail closed);
  risk gate on the basket cost (cluster id = candidate's event grouping: first leg's market
  event or candidate_id string when absent) — refusal wraps the decision; quantity =
  `economics.quantity * risk.size_multiplier` (Decimal). Freezes: legs ordered smallest-depth
  first (bottleneck leg first = venue-qualified leg order; document), per-level limit prices
  copied from each leg's `LegExecutionPlan.depth_walked levels`, expiration `as_of +
  expiry_window_seconds`, completion/cancellation/unwind prose naming the exact leg sequence,
  `max_incomplete_exposure_usd` = first-leg acquisition cost, `max_incomplete_loss_usd` =
  first-leg cost × economics_policy.partial_fill_reserve_rate + unwind spread reserve (document
  formula), `minimum_basket_payout` = `proof.minimum_basket_payout`, frozen_hashes = sorted unique
  of proof source hashes + book/fee source hashes + canonical economics/risk-policy hashes,
  kill_conditions ⊇ {"any participating rule_version change", "book evidence older than policy
  max age", "risk drawdown threshold breached"}.
- Proposal id via `deterministic_proposal_id`; re-planning identical inputs is id-stable.
- [ ] TDD (happy path freezing every field deterministically, each refusal, halved sizing),
  implement, full suite, commit `feat(predictions): add the deterministic shadow planner`.

---

### Task 5: Simulator core — non-atomic event-time fills

**Files:**
- Create: `src/polytrading/predictions/shadow_simulator.py`
- Test: `tests/predictions/test_shadow_simulator.py`

**Interfaces:**
- `SimulatedBookProvider = Callable[[int, datetime], PredictionBookSnapshot | None]` — the
  caller supplies point-in-time books per (leg_index, at-time); the simulator NEVER reads
  storage or clocks.
- `StressScenario(PredictionRecord)`: `scenario_id: str` (non-empty),
  `latency_seconds: int (ge=0)`, `fill_fraction: Decimal (gt=0, le=1)` (fraction of visible
  depth actually fillable), `failing_leg_index: int | None` (this leg's venue rejects/outages
  after the first leg fills), `unknown_after_leg: int | None` (order state becomes UNKNOWN
  after this leg submits — no fill/cancel confirmation). Module constants:
  `BASELINE = StressScenario(scenario_id="baseline", latency_seconds=0, fill_fraction=1,
  failing_leg_index=None, unknown_after_leg=None)`, `LATENCY_1S`, `LATENCY_5S`,
  `PARTIAL_FILL_50` (fill_fraction 0.5), `SECOND_LEG_REJECT` (failing_leg_index=1),
  `UNKNOWN_AFTER_FIRST` (unknown_after_leg=0).
- `simulate_shadow_proposal(plan, *, books, scenario, started_at) -> tuple[ShadowEvent, ...]`
  — pure; returns the FULL ordered event chain from sequence 0 (DISCOVERED, at plan
  information_cutoff) through PROOF_VALIDATED/ECONOMICS_VALIDATED/SHADOW_PLANNED (planning
  provenance events) then leg-by-leg simulation in `sequence_position` order:
  - Each leg submits at `started_at + leg_position * latency` and fills against
    `books(leg_index, submit_time + latency)` at plan limit prices: executable quantity =
    min(plan leg max, visible depth at-or-under limit × fill_fraction). Partial (< planned)
    fill on the FIRST leg → FIRST_LEG_SIMULATED event with `quantity_filled`, then the engine
    re-reads later legs' books before proceeding (documented; the re-read is the provider call
    at the later time) and completes at the reduced common quantity, or unwinds when a later
    leg cannot fill the first leg's quantity (UNWOUND with the unwind loss noted in detail).
  - `failing_leg_index` leg returns no fill (venue failure) → UNWOUND (first-leg exposure
    closed at its own book's bid side, conservatively) or EXPIRED when past `expires_at`.
  - `unknown_after_leg` → UNKNOWN event; NO further legs simulate (halt, §8); terminal UNKNOWN
    awaits reconciliation.
  - All legs filled at common quantity → COMPLETE.
  - Book provider returning None for a needed read → UNKNOWN (evidence gap is uncertainty,
    not success).
- Every confirmed buy or unwind is captured as structured `ShadowFill` evidence on the event that
  represents it. `FIRST_LEG_SIMULATED` contains the first acquisition; terminal events contain
  subsequent confirmed acquisitions and any unwind sells. The ledger must never parse `detail`.
- Determinism: identical inputs → identical event tuples (event_id = uuid5 over proposal_id +
  sequence + content).
- [ ] TDD: baseline completes with hand-computed fills; each scenario produces its exact
  terminal state and event chain (hand-verified quantities); partial-fill reduced-quantity
  completion; unwind loss arithmetic hand-checked; provider-None → UNKNOWN; determinism.
  Implement, full suite, commit `feat(predictions): add the non-atomic shadow fill simulator`.

---

### Task 6: Double-entry shadow ledger and reconciliation

**Files:**
- Create: `src/polytrading/predictions/shadow_ledger.py`
- Test: `tests/predictions/test_shadow_ledger.py`

**Interfaces:**
- `LedgerAccount = Literal["venue_cash", "venue_position", "fees_paid", "reserve",
  "opportunity_cost"]`; `LedgerPosting(PredictionRecord)`: `posting_id: UUID`, `proposal_id`,
  `event_id`, `venue: PredictionVenue | None`, `account: LedgerAccount`,
  `debit_usd: Decimal (ge=0)`, `credit_usd: Decimal (ge=0)` (exactly one non-zero),
  `occurred_at`, `detail: str`.
- `postings_for_events(plan, events, fees) -> tuple[LedgerPosting, ...]` — pure translation from
  structured `ShadowEvent.fills` plus the plan's frozen `minimum_basket_payout`:
  a fill event posts position debit / cash credit (+ fee posting) on that leg's venue;
  UNWOUND posts the unwind loss; COMPLETE+resolution posts realized payout using the proof's
  minimum floor (conservative — document); every event's postings balance
  (Σ debit == Σ credit). `verify_conservation(postings) -> None | raises ValueError`.
- `ShadowReconciliation(PredictionRecord)`: `reconciliation_id: UUID`, `proposal_id`,
  `venues_reconciled: tuple[PredictionVenue, ...]`, `complete: bool` (all plan venues
  present), `unexplained_difference_usd: Decimal`, `observed_at`.
  `reconcile_proposal(plan, events, postings) -> ShadowReconciliation` — pure; `complete` only
  when every plan venue's cash+position postings net to the event-implied outcome exactly
  (unexplained == 0) AND the terminal event is COMPLETE/UNWOUND/EXPIRED (UNKNOWN reconciles
  `complete=False` — §8's invalid-until-reconciled).
- `proposal_paper_pnl(postings, reconciliation) -> Decimal | None` — None unless
  `reconciliation.complete` (paper result invalid until reconciled).
- [ ] TDD (balanced postings per event type, conservation raises on imbalance, UNKNOWN never
  yields P&L, hand-computed complete-lifecycle P&L), implement, full suite, commit
  `feat(predictions): add the double-entry shadow ledger and reconciliation`.

---

### Task 7: Experiment registry (§15.4)

**Files:**
- Create: `src/polytrading/predictions/experiments.py`,
  `src/polytrading/predictions/storage/schema/007_experiments.sql`
- Modify: `storage/store.py`
- Test: `tests/predictions/test_experiments.py`, `test_store.py`

**Interfaces:**
- `TrialFamily(PredictionRecord)`: `family_id: str` (non-empty), `hypothesis: str`,
  `preregistered_at: datetime`, `thresholds_json: str` (frozen Class-G-style thresholds text),
  `venues: tuple[PredictionVenue, ...]`, `registered_by: str`.
- `ShadowExperiment(PredictionRecord)`: `experiment_id: UUID`, `family_id: str`,
  `proposal_id: UUID`, `scenario_id: str`, `terminal_state: ShadowState`,
  `paper_pnl_usd: Decimal | None`, `reconciled: bool`, `as_of`, `observed_at`.
  Validator: `paper_pnl_usd` non-None ⇒ `reconciled=True`. Failed/losing experiments are
  ordinary rows — nothing filters them out.
- Migration 007 (`trial_families` keyed by family_id+preregistered_at; `shadow_experiments`
  keyed by experiment_id); store appends + cutoff reads; an experiment referencing an
  unregistered family is rejected at the CLI layer (Task 8), not the store.
- [ ] TDD, implement, full suite, commit
  `feat(predictions): add the preregistered shadow experiment registry`.

---

### Task 8: CLI — `predictions shadow run` and `predictions shadow replay`

**Files:**
- Modify: `src/polytrading/predictions/cli.py`
- Test: `tests/predictions/test_cli.py`

**Interfaces (spec §13 command shapes):**
- `predictions shadow run --db <path> --trial-family <id> [--as-of <UTC>]
  [--expiry-seconds N] [--scenario baseline|latency_1s|latency_5s|partial_fill_50|
  second_leg_reject|unknown_after_first] [--format text|json]`:
  under the writer lease, in one transaction — for every SHADOW_CANDIDATE scan report at
  as_of (skipping proposals already planned by id): load candidate/proof/books/fees, run
  planner (portfolio state derived from prior reconciled experiments' P&L against a
  policy-declared starting equity — add `starting_equity_usd` to `PredictionRiskPolicy`,
  default 10_000), refuse-or-plan, simulate under the chosen scenario with `started_at =
  as_of`, translate ledger postings, reconcile, and append plan/events/postings/
  reconciliation/experiment rows idempotently. The trial family must exist (usage error
  otherwise — preregistration first, §15.4). Output: per-decision tally
  (planned/refused-by-reason/terminal-state counts, reconciled paper P&L sum over THIS run's
  proposals); forbidden vocabulary never printed.
- `predictions shadow replay --db <path> --proposal-id <uuid> [--scenario <id>] [--format]`:
  re-derives the stored plan, re-runs the simulator against STORED book evidence at the
  plan's frozen times (event-time replay, §11.6), and prints the event chain + a verdict line:
  `replay MATCHES stored events` or `replay DIVERGES at sequence N` (divergence exits 1) —
  the §17.7 reproducibility check. `--scenario` reruns under a different stress scenario
  WITHOUT persisting anything (pure what-if output, no writes; document).
- Both commands: sanitized error wrappers (`PredictionShadowError`), `_parse_timestamp`,
  loopback-free (no network).
- [ ] TDD: end-to-end seeded run (market→rules→attestation→candidate→proof→books→fees→scan
  SHADOW_CANDIDATE→shadow run) producing a COMPLETE reconciled proposal with hand-checked
  paper P&L; refusal paths (missing family, risk refusal via small starting equity); replay
  match + divergence (tamper a stored event in the test to force divergence); idempotent
  re-run. Implement, full suite, commit
  `feat(predictions): add shadow run and event-time replay commands`.

---

### Task 9: Dashboard shadow panels

**Files:**
- Modify: `dashboard.py`, `dashboard_models.py`, `web_assets/*`
- Test: `test_dashboard.py`, `test_dashboard_models.py`

**Interfaces:**
- Snapshot gains `shadow: ShadowSummary`: `proposals_total`, `by_terminal_state: dict[str,int]`
  (derived from each proposal's latest event at as_of), `reconciled_count`,
  `reconciled_paper_pnl_usd: Decimal`, `unreconciled_count`,
  `latest: tuple[ShadowListing, ...]` (≤20 newest-first: proposal_id, candidate_id,
  current_state, scenario_id, quantity, paper_pnl (None until reconciled), observed_at) and
  `experiments_by_family: dict[str, int]`.
- Rendering: states verbatim; UNKNOWN rows visibly flagged `awaiting reconciliation — paper
  result invalid`; P&L only shown for reconciled rows; forbidden-words test still passes
  (reuse the served-bytes pattern); `_recipes()` gains `shadow run` / `shadow replay` examples;
  empty states; cutoff-safety.
- [ ] TDD per the panel precedent, implement, full suite, commit
  `feat(predictions): render shadow proposals and paper results on the dashboard`.

---

### Task 10: README §20 and full verification

- [ ] README section in the established register: preregister a trial family (document the
  operator step — a JSON via a new tiny `predictions shadow register-family --db --input`
  subcommand IF Task 8 did not already need one; otherwise document the existing path — the
  planner of this task must check Task 8's final shape and document what exists), shadow run
  (scenarios, risk gates, ledger, reconciliation), replay reproducibility check, the §8
  invalid-until-reconciled rule, and the §16 forward-activation clock reminder (45-day
  evidence + 30-day shadow are calendar gates no code shortcuts). State plainly: shadow
  results are simulations against recorded public evidence; nothing here trades.
  NOTE: if Task 8 lands without a family-registration subcommand, ADD one in this task
  (mirroring `predictions attest`: operator-authored JSON, strict parse, idempotent append)
  — a registry nobody can register into is dead weight.
- [ ] Full verification: `.venv/bin/python -m pytest` (exact tally), `ruff check .`,
  `ruff format --check .` — all clean. Commit
  `docs(predictions): document the shadow engine and replay workflow`.

---

## Self-review notes

- §16.4 coverage: non-atomic multi-venue state machine (T1/T5), latency/partial-fill/
  venue-failure stress (T5 scenarios; §15.3's 1s/5s latencies are first-class constants),
  ledger (T6), experiment registry (T7), paper dashboard (T9); replay (T8) implements §11.6's
  event-time replay and §17.7. The §9 risk engine (T3) rides in this increment because shadow
  sizing is its first consumer; §11.7 independence is honored by module boundary and frozen
  policy values.
- Deliberately absent: authenticated anything, order signing, venue credentials, automatic
  transfer modeling (capital pre-positioned per §7), reward credits, directional forecasting.
- The simulator's conservative choices (fill only at-or-under frozen limit prices, provider
  gaps → UNKNOWN, unwind at bid side) all fail toward worse paper results, never better.
- Increment-5 seam: the state machine, plan freeze list, ledger, and reconciliation records are
  the exact artifacts §11.9's future execution adapters must reuse — nothing here will need
  rework to add a live adapter behind its gates.
