# SDD ledger — plan: docs/superpowers/plans/2026-08-24-prediction-market-shadow-engine.md

Branch start: 19b4b75
Approved plan amendment: e81f65d

## Preflight interface scan

| Tasks | Producer → consumer / shared surface | Finding |
|---|---|---|
| 1 self | Shadow state, plan, fill, event contracts | Internally consistent after the approved structured-fill and payout-floor amendment; validators must enforce both directions of the sequence-0/from-state invariant. |
| 2 self | Migration 006 and store APIs | Conflict: ledger/reconciliation append/read methods name models not created until Task 6. |
| 3 self | Frozen risk policy and pure gate | Task 8 later requires `starting_equity_usd`, although Task 3's field list omitted it. |
| 4 self | Planner inputs and frozen output | The candidate model exposes no venue-native event grouping, and the pure signature exposes no current-rule registry despite two requested refusal checks. |
| 5 self | Pure event-time simulator | The text requires post-partial-fill economics recomputation but the listed signature has no fees or economics policy; structured fills now resolve the ledger/replay evidence gap. |
| 6 self | Ledger, conservation, reconciliation, P&L | The approved amendment supplies exact fill ladders and payout floor; COMPLETE/UNWOUND reconciliation still needs a RECONCILED state event at orchestration time. |
| 7 self | Trial families, experiments, migration 007 | Internally consistent; failed/losing rows remain unfiltered. |
| 8 self | Transactional run and pure replay CLI | Depends on all earlier artifacts; a successful reconciliation must append the final state transition, and replay must validate frozen evidence hashes. |
| 9 self | Dashboard summary and assets | Internally consistent if current state is derived from the complete event chain and P&L is gated by reconciliation. |
| 10 self | Family registration, README, verification | Internally consistent; family registration must be implemented if Task 8 does not expose it. |
| 1 → 2 | `ShadowPlan`/`ShadowEvent` JSON persistence | Compatible; Task 2 owns plan/event persistence. |
| 1 → 4 | Planner constructs `ShadowPlan`/`ShadowLegPlan` | Compatible after freezing `minimum_basket_payout`. |
| 1 → 5 | Simulator emits `ShadowEvent`/`ShadowFill` | Compatible; events retain authoritative executions instead of prose parsing. |
| 1 → 6 | Ledger consumes plans/events/fills | Compatible after approved amendment. |
| 1 → 7 | Experiments cite `ShadowState` | Compatible. |
| 1 → 8 | CLI orchestrates plans/events | Compatible, subject to final RECONCILED event ruling. |
| 1 → 9 | Dashboard derives event-sourced state | Compatible. |
| 2 → 6 | `store.py`, ledger/reconciliation tables and APIs | Sequencing conflict; model-typed APIs move to Task 6 while Task 2 still creates all migration-006 tables. |
| 2 → 7 | `store.py`, sequential migrations and package fixtures | Compatible; Task 7 advances the count from 006 to 007. |
| 2 → 8 | CLI uses cutoff-safe shadow persistence | Compatible after Task 6 completes the deferred typed APIs. |
| 2 → 9 | Dashboard reads shadow persistence | Compatible. |
| 3 → 4 | Planner calls frozen risk gate | Compatible after `starting_equity_usd` is added in Task 3. |
| 3 → 8 | CLI derives portfolio against starting equity | Compatible after the Task 3 ruling. |
| 4 → 5 | Frozen plan drives simulator | Partial-fill economics inputs are missing from the listed simulator signature. |
| 4 → 8 | CLI loads evidence and plans proposals | Current-proof and event-cluster checks need orchestration inputs absent from the candidate record. |
| 5 → 6 | Structured simulated fills drive postings | Compatible after approved amendment. |
| 5 → 8 | CLI runs/replays deterministic scenarios | Compatible if stored point-in-time fees and policy are supplied to partial-fill recomputation and hash-validated. |
| 5 → 9 | Terminal events drive listings | Compatible. |
| 6 → 7 | Reconciled P&L populates experiments | Compatible; unreconciled experiments carry no P&L. |
| 6 → 8 | CLI persists postings/reconciliation/final state | Compatible after deferred store APIs and RECONCILED event orchestration. |
| 6 → 9 | Dashboard gates paper P&L on reconciliation | Compatible. |
| 7 → 8 | CLI requires family and appends experiment | Compatible; CLI enforces registration. |
| 7 → 9 | Dashboard groups experiments by family | Compatible. |
| 8 → 10 | CLI command surface and README recipes | Compatible; Task 10 fills only the family-registration gap left by Task 8. |
| 9 → 10 | Dashboard behavior and documentation | Compatible. |

## Preflight rulings

Ruling: Persist `ShadowFill` price ladders on events and `minimum_basket_payout` on plans — exact double-entry P&L and replay cannot be reconstructed from prose or identifiers alone, and the user approved this amendment — if wrong, the v1 record schema carries two extra immutable fields.

Ruling: Task 2 creates all migration-006 tables but defers model-typed ledger/reconciliation store methods and their tests to Task 6 — TDD forbids importing production models before their Task 6 tests define the behavior — if wrong, the Task 2 commit is narrower and Task 6 owns a slightly larger store diff.

Ruling: Task 3 adds `starting_equity_usd=Decimal("10000")` immediately — Task 8 explicitly consumes it and policy schema should not change halfway through integration — if wrong, the risk model exposes one field before its first CLI consumer.

Ruling: Planner proof currency means proof/scan/candidate identity agreement, `proof_ready`, cutoff safety, and the caller supplying the latest proof; current rule-version lookup remains a CLI/store responsibility because it is absent from the pure planner inputs — if wrong, direct library callers could pass a superseded proof that still matches an old scan.

Ruling: Planner uses `candidate_id` as the event-cluster fallback in the pure library; Task 8 derives a venue-native event ID from stored markets when available and passes it through an optional `event_cluster_id` argument — the candidate artifact itself has no event field — if wrong, direct planner calls are more conservative about concentration but may under-group related candidates.

Ruling: Task 5 accepts frozen fee evidence and the economics policy as explicit pure inputs so a post-partial-fill completion can recompute conservative surplus over the later books; replay hash-validates those inputs — the spec explicitly requires recomputation and the original listed signature cannot perform it — if wrong, two pure parameters broaden the simulator API and callers must supply them.

Ruling: Task 5 also accepts the cited `ProofArtifact` and `CandidateRelationship` as explicit pure inputs — `evaluate_basket_economics` requires them, the plan stores their immutable IDs, and Task 8 can load/hash-check them for replay — if wrong, two additional pure parameters broaden the simulator API but avoid duplicating the economics formula.

Ruling: `ShadowEvent` gains sorted-unique `evidence_hashes` for runtime books consulted by simulation — later latency/continuation/unwind snapshots cannot truthfully be frozen on the pre-submission `ShadowPlan`, while exact event-time replay must retain their lineage on the event that used them — if wrong, v1 events carry one additional immutable tuple and their content-derived IDs incorporate it.

Ruling: Task 6 broadens reconciliation to `reconcile_proposal(plan, events, postings, fees)` and validates the same frozen fee mapping used for posting — fills and opaque fee hashes cannot reconstruct exact taker fees, so the original three-argument signature would either accept balanced fee tampering or duplicate unverifiable assumptions — if wrong, callers pass one additional already-loaded pure input.

Ruling: `ShadowReconciliation` records `terminal_event_id` and `terminal_state`, and `proposal_paper_pnl` accepts the authoritative event chain — postings alone cannot distinguish no-fill EXPIRED from no-fill UNKNOWN, so a reconciliation/P&L identity that omits terminal evidence permits cross-outcome substitution — if wrong, reconciliation v1 carries two additional immutable fields and P&L callers pass the already-loaded events.

Ruling: A complete reconciliation appends a deterministic terminal→RECONCILED `ShadowEvent`; UNKNOWN remains UNKNOWN with `complete=False` — otherwise the declared state machine can never reach its final state — if wrong, successful proposals have one additional append-only event and dashboard terminal counts report RECONCILED rather than COMPLETE/UNWOUND.

Ruling: `deterministic_proposal_id` accepts an already JSON-compatible `Mapping[str, object]`, canonicalizes it with sorted compact JSON beside `scan_report_id`, and uses a fixed UUID5 namespace — Task 4 can pass proposal content without the circular `proposal_id`, while Task 1 need not invent a generic Decimal/datetime serializer — if wrong, direct callers must first convert rich Python/Pydantic values to JSON mode.

## Added performance investigation

Baseline full-suite run started from 19b4b75. Initial evidence: the module-scoped complete economics database costs about 30.46s to seed and one full assembly costs about 9.99s; two earlier tests independently reseed the same 90-day/60-day fixture. Root-cause work remains in progress.

Task 1: fix round 1/5 (2 addressed, 1 open — zero price/size boundary tests missing; commits 05fb7a8..d87d834)
Task 1: fix round 2/5 (1 addressed, 0 open — explicit zero price/size mutation coverage; commits d87d834..b25ab16)
Task 1: complete (commits e81f65d..b25ab16, review clean)
Task 2: Ruling: equal core/prediction migration counts require an explicit v1 sentinel-table family check before accepting applied versions — numeric `schema_migrations` values alone accepted the wrong database family after prediction migration 006 — if wrong, a deliberately hybrid database containing both sentinel families is rejected rather than opened ambiguously.
Task 2: complete (commits b25ab16..c305008, review clean)
Task 3: fix round 1/5 (1 addressed, 2 open — unchecked Pydantic copies and dict-subclass mutation bypasses; commits e481c06..e52bc48)
Task 3: fix round 2/5 (2 addressed, 0 open — evaluator revalidation and true immutable Mapping; commits e52bc48..61529cd)
Task 3: complete (commits c305008..61529cd, review clean)
Task 4: fix round 1/5 (3 addressed, 1 open — surplus evidence keys entered frozen lineage; commits 0d44624..b9cad8c)
Task 4: fix round 2/5 (1 addressed, 0 open — exact book/fee key topology; commits b9cad8c..da77e6b)
Task 4: complete (commits 61529cd..da77e6b, review clean)
Task 5: fix round 1/5 (2 addressed, 0 open — runtime book lineage and chronological expiry/UNKNOWN precedence; commits e08b59b..37ac930)
Task 5: complete (commits da77e6b..37ac930, review clean)
Task 6: fix round 1/5 (4 addressed, 1 open — UNKNOWN P&L cross-terminal substitution remains because terminal evidence is absent; commits f79ecd9..f79ac88)
Task 6: fix round 2/5 (1 addressed, 0 open — reconciliation and P&L now bind authoritative terminal evidence; commits f79ac88..254e840)
Task 6: complete (commits 37ac930..254e840, review clean)
Task 7: fix round 1/5 (2 addressed, 0 open — database keys and terminal-state classification; commits 3425cb8..eb0cbe9)
Task 7: complete (commits 254e840..eb0cbe9, review clean)
Task 8: fix round 1/5 (5 addressed, 2 open — market/family/experiment/reconciliation risk inputs remain unverified; extra reconciliation rows are ignored; commits b06f699..32dffd0)
Task 8: fix round 2/5 (2 addressed, 0 open — all binding risk/reconciliation inputs verified and multiplicity rejected; commits 32dffd0..cdbd4fa)
Task 8: complete (commits eb0cbe9..cdbd4fa, review clean)
Task 9: fix round 1/5 (3 addressed, 0 open — paper P&L is recomputed from the canonical ledger bundle, event/posting hashes and indexed columns are verified, and plan/event cutoff leakage fails closed; starts after 9428e2d)
Task 9: fix round 2/5 (3 addressed, 0 open — frozen fee evidence and fill-derived ledger bundles are verified exactly, reconciliation/experiment indexes cannot hide decoded records, and valid first-leg historical prefixes remain unreconciled; starts after 4b44587)
Task 9: fix round 3/5 (2 addressed, 0 open — repeated fee logical identities fail closed without row-order dependence, and proposal identity uniqueness is enforced across the full cutoff-safe plan set; starts after 350b774)
Task 9: fix round 4/5 (1 addressed, 0 open — all cutoff-safe venue/market fee siblings are verified for append-identity conflicts and tamper before frozen source-hash selection; starts after eb2320d)
Task 9: complete (commits cdbd4fa..24845da, review clean)
Task 10: Ruling: the repository-wide pytest gate is sequenced after the separately approved performance changes — the parent measured the pre-optimization suite at about 8m45s and explicitly reserved that final run for the optimized branch; Task 10 instead ran its focused and complete prediction suites without weakening coverage — if wrong, the final repository-wide tally is delayed to the parent verification pass rather than duplicated here.
Task 10: implementation ready for independent review (base 2153c6a; registration CLI, strict trial-family JSON, and README §20; not yet complete)
