# Task 2 Report: Gold Labels, Model Cards, and Untrusted Artifact Schemas

## Implementation

- Added strict, frozen AI records for source spans, critical fields, gold contracts and relationships, versioned labels, rule fields, model cards, and both candidate artifact types.
- Validated timezone-aware UTC normalization, lowercase SHA-256 hashes, source-span bounds, known/unknown evidence invariants, positive label versions, bounded unique review IDs, mandatory model safety prohibitions, finite nonnegative artifact decimals, and increasing model/artifact lifetimes.
- Added an append-only `ModelRegistry` backed by `DuckDBStore`. Card and artifact retries are idempotent only when the canonical serialized record bytes match exactly; a changed immutable identity raises `ConflictingRecordError`.
- Added `002_ai_registry.sql` with `model_cards` and `ai_artifacts`, plus store methods to append and retrieve cards and append artifacts. Migration 001 was not changed.
- Updated the existing migration regression to require migrations 1 and 2 exactly once after reopen.

## TDD Evidence

RED command:

```text
.venv/bin/python -m pytest tests/ai/test_models.py tests/ai/test_model_registry.py -q
2 collection errors: ModuleNotFoundError for polytrading.ai.models and polytrading.ai.model_registry
```

GREEN focused command:

```text
.venv/bin/python -m pytest tests/ai/test_models.py tests/ai/test_model_registry.py -q
13 passed in 0.53s
```

## Verification

```text
.venv/bin/python -m pytest -q
292 passed in 6.93s

.venv/bin/ruff check src/polytrading/ai tests/ai
All checks passed!

git diff --check
exit 0 (no output)
```

Migration verification is covered by reopening a fresh DuckDB database and asserting `schema_migrations` contains exactly `[(1, 1), (2, 1)]`. Registry tests also instantiate a fresh store, exercising application of migration 002 before card/artifact writes.

## Self-review

- Confirmed no provider SDK, provider call, credential access, trading proposal, risk approval, sizing, payout proof, or order path was added.
- Confirmed all declared timestamp-bearing AI records normalize to UTC and reject naive values; all declared hash fields validate lowercase SHA-256 syntax.
- Confirmed registry validates absent, revoked, expired, and version-mismatched cards and enforces `information_cutoff <= created_at < expires_at`.
- Confirmed only task-scoped production code, tests, migration coverage, and this report changed.

## Concerns

None. `RuleFieldSet` uses direct snake-case names for every approved category because the approved design declared categories, rather than separate literal Python identifiers.

## Code Review Corrections

- Changed artifact validation to require an explicitly `validated` model card. Existing absent, revoked, and expired diagnostics remain unchanged; draft cards now fail with `model card is not validated`.
- Bound relationship evidence structurally to the relationship members. Evidence contract IDs must be unique and set-equal to `member_contract_ids`; missing members, duplicate evidence IDs, and non-member evidence each have a diagnostic regression.

Review RED command and evidence:

```text
.venv/bin/python -m pytest tests/ai/test_model_registry.py::test_registry_rejects_draft_model_cards tests/ai/test_models.py::test_relationship_artifact_rejects_missing_member_evidence tests/ai/test_models.py::test_relationship_artifact_rejects_duplicate_evidence_contract_ids tests/ai/test_models.py::test_relationship_artifact_rejects_non_member_evidence -q
4 failed in 0.38s: each invalid object was accepted instead of raising its expected error
```

Review GREEN and final verification:

```text
same four-test command: 4 passed in 0.39s
focused Task 2 tests: 17 passed in 0.53s
full suite: 296 passed in 5.54s
Ruff: All checks passed!
git diff --check: exit 0 (no output)
```
