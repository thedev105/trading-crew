# Offline AI Semantic Scout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the manually reviewed gold corpus, model registry, deterministic retrieval baseline, untrusted-artifact boundary, source-span validation, and offline evaluation report required to judge whether an AI semantic scout is worth adding.

**Architecture:** This increment extends the shared Python research package after the foundation plan. A corpus layer preserves point-in-time rule text and adjudicated labels; a deterministic TF-IDF scout supplies the declared retrieval baseline; strict Pydantic artifacts and source-span checks create a one-way research-to-decision boundary; and an evaluator reports every semantic activation metric without claiming Class G eligibility. No model provider is called, and no artifact can create a proposal or reach execution code.

**Tech Stack:** Python 3.12–3.14, the foundation plan's pinned dependencies, scikit-learn 1.9.0, pytest 9.1.1, pytest-cov 7.1.0, Hypothesis 6.160.0, Ruff 0.15.22, JSON Lines datasets, SHA-256 content addressing.

## Global Constraints

- Approved specification: [`docs/superpowers/specs/2026-08-12-ai-augmentation-design.md`](../specs/2026-08-12-ai-augmentation-design.md), especially Sections 4–6, 11–17.
- Prerequisite: complete Tasks 1–4 of [`2026-08-12-research-foundation-and-carry-audit.md`](2026-08-12-research-foundation-and-carry-audit.md) so strict records, append-only storage, experiment registration, and the research ledger exist.
- Scope ends at gold data, model cards, deterministic offline retrieval/extraction baselines, prompt packets, strict import of untrusted candidate artifacts, and evaluation. Do not add provider SDKs, API keys, external inference calls, authenticated venue access, balances, proposals, payout proof, risk approval, sizing, or orders.
- The semantic scout has `research_only` authority. It may suggest candidates and fields; it cannot assign evidence class G, certify equivalence, or mark anything trade-eligible.
- Treat source text as hostile data. Preserve exact raw bytes and hash, strip active content into a separate canonical text representation, flag Unicode/confusable/hidden-content concerns, and never interpolate source text into shell, SQL, or templates.
- Every known critical field requires at least one exact, validated span in the same canonical source text. Unsupported or ambiguous fields are `unknown`; no default or inference fills missing evidence.
- All corpus splits are frozen by event family and time, not random row split. A contract, duplicate, revision, event family, or deterministic derivative of one may occur in only one split.
- The manually reviewed target is at least 500 contracts, 20 rule templates, 250 relationship pairs/sets, and 200 adversarial examples. Two independent reviews plus adjudication are required for critical fields and relationship labels.
- Promotion thresholds are reported exactly: critical-field exact match at least 99.5%, known-relationship candidate recall at least 90%, 100% span validity for non-unknown fields, malformed output fails closed, mutation invalidation passes, and manual-review load falls at least 50% without any false Class G eligibility.
- Because the deterministic payoff compiler is outside this plan, full-pipeline false Class G eligibility is reported as `BLOCKED_BY_DEPENDENCY`, never zero or pass. Therefore this increment cannot authorize read-only production promotion even if every measurable offline metric passes.
- The default future inference budget is recorded as `min(USD 25/month, 0.3125% of equity)`, but this plan spends zero provider inference dollars and performs no paid model call.
- Automated tests are offline and deterministic. Corpus and model versions are content hashes; the evaluator emits identical bytes for identical inputs.
- Every task follows red-green-refactor: add the named failing test, observe the stated failure, implement the smallest behavior, rerun the focused test, then run the task's regression command.

---

## Task 1: Add the Offline Semantic-Scout Package Boundary

**Files:**

- Modify: `pyproject.toml`
- Create: `src/polytrading/ai/__init__.py`
- Create: `src/polytrading/ai/cli.py`
- Create: `tests/ai/test_package.py`

- [ ] **Step 1: Write the failing AI package test**

```python
# tests/ai/test_package.py
from importlib.util import find_spec

from polytrading.ai import AUTHORITY


def test_ai_package_is_research_only_and_has_no_provider_sdk() -> None:
    assert AUTHORITY == "research_only"
    assert find_spec("openai") is None
    assert find_spec("anthropic") is None
```

- [ ] **Step 2: Run the test and verify the expected failure**

Run:

```bash
.venv/bin/python -m pytest tests/ai/test_package.py -q
```

Expected: FAIL during collection because `polytrading.ai` does not exist.

- [ ] **Step 3: Add the one direct ML dependency and package constant**

Add `scikit-learn==1.9.0` to the main dependency list in `pyproject.toml`; do not add a hosted-model SDK. Create:

```python
# src/polytrading/ai/__init__.py
from typing import Final

AUTHORITY: Final = "research_only"
```

`src/polytrading/ai/cli.py` exports `add_ai_subcommands(subparsers)` but initially adds only an `ai --help` parser and returns no network client.

- [ ] **Step 4: Install and verify the boundary**

Run:

```bash
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest tests/ai/test_package.py -q
.venv/bin/python -m pip freeze | rg -i 'openai|anthropic|cohere|gemini'
```

Expected: the test passes and the provider-SDK scan prints no matches.

- [ ] **Step 5: Commit the package boundary**

```bash
git add pyproject.toml src/polytrading/ai tests/ai/test_package.py
git commit -m "build: add offline semantic scout boundary"
```

---

## Task 2: Define Gold Labels, Model Cards, and Untrusted Artifact Schemas

**Files:**

- Create: `src/polytrading/ai/models.py`
- Create: `src/polytrading/ai/model_registry.py`
- Create: `src/polytrading/storage/schema/002_ai_registry.sql`
- Modify: `src/polytrading/storage/store.py`
- Create: `tests/ai/test_models.py`
- Create: `tests/ai/test_model_registry.py`

- [ ] **Step 1: Write failing schema tests**

Test exact rejection of unknown fields, naive timestamps, mutable inputs, invalid hashes, unregistered model versions, expired artifacts, missing spans for known values, spans attached to unknown values, negative inference cost, and authority other than `research_only`.

Include the critical invariant:

```python
def test_known_critical_field_requires_evidence() -> None:
    with pytest.raises(ValidationError, match="known field requires supporting spans"):
        CriticalField(status="known", value=">=", supporting_spans=())


def test_unknown_critical_field_cannot_carry_a_guess() -> None:
    with pytest.raises(ValidationError, match="unknown field cannot have a value"):
        CriticalField(status="unknown", value="UTC", supporting_spans=())
```

- [ ] **Step 2: Run tests and verify the expected failure**

Run:

```bash
.venv/bin/python -m pytest tests/ai/test_models.py tests/ai/test_model_registry.py -q
```

Expected: FAIL during collection because the schema modules do not exist.

- [ ] **Step 3: Implement the corpus and critical-field schemas**

Create strict/frozen records with these public fields:

```python
class SourceSpan(StrictRecord):
    start_char: int
    end_char: int
    exact_text: str
    canonical_text_hash: str


class CriticalField(StrictRecord):
    status: Literal["known", "unknown"]
    value: str | None
    supporting_spans: tuple[SourceSpan, ...]


class GoldContract(StrictRecord):
    schema_version: Literal[1]
    contract_id: str
    source_url: str
    source_retrieved_at: datetime
    information_cutoff: datetime
    raw_text: str
    raw_text_hash: str
    canonical_text: str
    canonical_text_hash: str
    event_family: str
    sampling_stratum: str
    split: Literal["train", "validation", "test"]


class GoldRelationship(StrictRecord):
    schema_version: Literal[1]
    relationship_id: str
    member_contract_ids: tuple[str, ...]
    split: Literal["train", "validation", "test"]


class GoldContractLabel(StrictRecord):
    schema_version: Literal[1]
    contract_id: str
    label_version: int
    rule_template: str
    adversarial_tags: tuple[str, ...]
    fields: RuleFieldSet
    review_ids: tuple[str, ...]
    adjudication_id: str | None


class GoldRelationshipLabel(StrictRecord):
    schema_version: Literal[1]
    relationship_id: str
    label_version: int
    label: Literal[
        "complement",
        "exhaustive_set",
        "implication",
        "nested_threshold",
        "nested_deadline",
        "range_identity",
        "non_equivalent",
    ]
    supported_template: bool
    adversarial_tags: tuple[str, ...]
    review_ids: tuple[str, ...]
    adjudication_id: str | None
```

`RuleFieldSet` contains the approved design's subject/scope, oracle/source instrument, observation date/time/timezone/window, operator/inclusivity/threshold/unit/precision/rounding, set membership, cancellation/postponement/substitution/dispute/clarification/fallback clauses, payout asset, collateral asset, document version, and rule hash. Every member is a `CriticalField`.

Source and membership rows never change. Label corrections append a higher positive `label_version`;
the current label is the highest version visible before the dataset freeze. During construction,
`review_ids` may contain zero, one, or two unique IDs. Reject more than two; the corpus freezer
requires exactly two and an adjudication ID whenever their proposed label hashes differ.

- [ ] **Step 4: Implement model cards and candidate artifacts**

```python
class ModelCard(StrictRecord):
    schema_version: Literal[1]
    model_id: str
    version: str
    owner: str
    intended_use: str
    prohibited_uses: tuple[str, ...]
    authority: Literal["research_only"]
    implementation_kind: Literal["deterministic_baseline", "external_artifact_import"]
    training_cutoff: datetime | None
    prompt_version: str | None
    feature_version: str
    validation_dataset_hash: str | None
    status: Literal["draft", "validated", "revoked", "expired"]
    approved_at: datetime | None
    expires_at: datetime | None


class RuleExtractionArtifact(StrictRecord):
    schema_version: Literal[1]
    artifact_id: UUID
    contract_id: str
    information_cutoff: datetime
    source_hashes: tuple[str, ...]
    model_id: str
    model_version: str
    prompt_version: str
    inference_parameters_hash: str
    extracted_fields: RuleFieldSet
    uncertainty: Decimal
    abstention_reason: str | None
    inference_latency_ms: Decimal
    inference_cost_usd: Decimal
    created_at: datetime
    expires_at: datetime
    invalidation_conditions: tuple[str, ...]


class ContractSpanEvidence(StrictRecord):
    contract_id: str
    supporting_spans: tuple[SourceSpan, ...]


class RelationshipCandidateArtifact(StrictRecord):
    schema_version: Literal[1]
    artifact_id: UUID
    member_contract_ids: tuple[str, ...]
    proposed_relationship: str
    supporting_evidence: tuple[ContractSpanEvidence, ...]
    model_id: str
    model_version: str
    information_cutoff: datetime
    uncertainty: Decimal
    abstention_reason: str | None
    created_at: datetime
    expires_at: datetime
```

Require prohibited uses to contain `trade_approval`, `order_submission`, `risk_limit_changes`, and `credential_access`. Relationship values are suggestions, not the Class G relationship enumeration.

- [ ] **Step 5: Implement an append-only model registry**

Add `model_cards` and `ai_artifacts` tables through the next forward migration, not by editing an already-applied migration. `ModelRegistry.register` is idempotent only for byte-identical cards. `validate_artifact` rejects absent, revoked, expired, or version-mismatched cards and requires `artifact.information_cutoff <= artifact.created_at < artifact.expires_at`.

- [ ] **Step 6: Run focused and regression tests**

Run:

```bash
.venv/bin/python -m pytest tests/ai/test_models.py tests/ai/test_model_registry.py -q
.venv/bin/python -m pytest -q
.venv/bin/ruff check src/polytrading/ai tests/ai
```

Expected: all tests pass and Ruff exits 0.

- [ ] **Step 7: Commit schemas and registry**

```bash
git add src/polytrading/ai src/polytrading/storage tests/ai
git commit -m "feat: define AI artifact and model registry schemas"
```

---

## Task 3: Build Corpus Import, Review, Adjudication, and Split Validation

**Files:**

- Create: `src/polytrading/ai/corpus.py`
- Create: `src/polytrading/ai/review.py`
- Create: `tests/ai/test_corpus.py`
- Create: `tests/fixtures/ai/corpus/contracts.jsonl`
- Create: `tests/fixtures/ai/corpus/relationships.jsonl`
- Create: `tests/fixtures/ai/corpus/labels.jsonl`
- Create: `tests/fixtures/ai/corpus/reviews.jsonl`
- Create: `data/gold/README.md`
- Create: `data/gold/contracts.jsonl`
- Create: `data/gold/relationships.jsonl`
- Create: `data/gold/labels.jsonl`
- Create: `data/gold/reviews.jsonl`
- Create: `data/gold/policy.json`
- Create: `data/gold/progress.jsonl`
- Create: `data/gold/manifest.json`

- [ ] **Step 1: Write failing canonicalization and leakage tests**

Test that importer input must include source URL, retrieval/cutoff times, exact rule text, event family, rule-template label, and provenance. Assert:

- raw text hash changes for any byte change;
- canonical text removes `<script>`, event handlers, style blocks, and zero-width formatting characters while retaining visible rule language;
- canonicalization warnings retain the raw hash and identify removed active content or suspicious Unicode;
- duplicate raw hashes, revisions of one contract, shared event families, and relationship members cannot cross splits;
- relationships reference existing contracts all in the same split;
- a reviewer cannot review the same item twice under two IDs;
- equal independent reviews close automatically, while a disagreement requires a third adjudication record;
- changing labels after a manifest freeze creates a new dataset version rather than mutating the old manifest.

- [ ] **Step 2: Run tests and verify the expected failure**

Run:

```bash
.venv/bin/python -m pytest tests/ai/test_corpus.py -q
```

Expected: FAIL during collection because `polytrading.ai.corpus` does not exist.

- [ ] **Step 3: Implement deterministic sanitization and content addressing**

Use the standard library's `html.parser.HTMLParser` to exclude script/style/template contents and active attributes. Apply Unicode NFC normalization to canonical text, remove format-control characters while recording their code points and offsets, normalize CRLF to LF, and preserve ordinary whitespace and punctuation so source offsets remain reproducible. Do not translate confusable characters; flag them for review.

`CorpusManifest` records schema version, dataset ID, creation time, information cutoff,
contract/relationship/label/review file hashes, split-family hashes, counts, rule-template counts,
adversarial-tag counts, review completion, and frozen state.

- [ ] **Step 4: Implement review and adjudication rules**

```python
class ReviewRecord(StrictRecord):
    schema_version: Literal[1]
    review_id: str
    item_type: Literal["contract", "relationship"]
    item_id: str
    reviewer_id: str
    reviewer_role: Literal["reviewer", "adjudicator"]
    input_hash: str
    proposed_label_hash: str
    decision: Literal["accept", "correct", "reject"]
    corrections_json: str | None
    reviewed_at: datetime
```

Require two distinct reviewer IDs. When their proposed label hashes differ, require one adjudicator who is distinct from both. `freeze_manifest` rejects any unresolved item or cross-split leakage.

- [ ] **Step 5: Add exact CLI contracts**

Add commands:

```text
polytrading ai corpus preregister --policy data/gold/policy.json --dir data/gold
polytrading ai corpus import --input var/contract-import.jsonl --output data/gold/contracts.jsonl
polytrading ai corpus review --item-type contract --item-id contract-0001 --review-file var/contract-0001-review-a.json
polytrading ai corpus review --item-type relationship --item-id relationship-0001 --review-file var/relationship-0001-review-a.json
polytrading ai corpus adjudicate --item-type contract --item-id contract-0001 --review-file var/contract-0001-adjudication.json
polytrading ai corpus validate --dir data/gold
polytrading ai corpus freeze --dir data/gold
```

`preregister` validates the sampling counts, split policy, information cutoff, reviewer roles, and
template taxonomy, then generates one immutable progress row per required contract, review,
relationship, and adjudication queue state. Commands write new content-addressed files atomically
through a temporary file and rename; they never edit an existing frozen manifest in place.

- [ ] **Step 6: Seed only the small synthetic test corpus**

The checked-in test fixture has six contracts: one valid complement, one nested threshold, one oracle-mismatch negative, and one direct prompt-injection string. It is intentionally below production gates and exists only for unit tests. Leave the production `data/gold/*.jsonl` files valid but empty until Task 4's human-reviewed construction.

- [ ] **Step 7: Run tests and commit corpus tooling**

Run:

```bash
.venv/bin/python -m pytest tests/ai/test_corpus.py -q
.venv/bin/ruff check src/polytrading/ai tests/ai
git add src/polytrading/ai tests/ai tests/fixtures/ai data/gold
git commit -m "feat: add reviewed semantic corpus workflow"
```

Expected: tests pass, Ruff exits 0, and the commit contains no production gold labels yet.

---

## Task 4: Construct and Freeze the 500-Contract Gold Corpus

**Files:**

- Modify: `data/gold/contracts.jsonl`
- Modify: `data/gold/relationships.jsonl`
- Modify: `data/gold/labels.jsonl`
- Modify: `data/gold/reviews.jsonl`
- Modify: `data/gold/policy.json`
- Modify: `data/gold/progress.jsonl`
- Modify: `data/gold/manifest.json`
- Modify: `data/gold/README.md`

- [ ] **Step 1: Pre-register the exact corpus composition**

Before labeling, record this frozen sampling policy in `data/gold/policy.json`,
`data/gold/README.md`, and experiment `019b3b42-0000-7000-8000-000000000001`:

- 500 contracts, 25 from each of 20 templates;
- 250 relationship pairs/sets: 40 complements, 20 exhaustive sets, 20 nested threshold/deadline/range positives, 20 manually reviewed cross-event implications, and 150 non-equivalent near misses;
- at least 50 of the positive relationships and all 150 negative relationships carry adversarial tags, giving at least 200 adversarial relationship examples;
- split by event family: 60% train, 20% validation, 20% untouched test; source retrieval and information cutoff precede any known outcome used in review;
- the test split remains hidden from parser/ranking threshold selection until the experiment is frozen.

The 20 templates are: YES/NO complement, multi-outcome exhaustive set, inclusive price threshold, exclusive price threshold, nested deadline, inclusive range, exclusive range, operator-boundary near miss, oracle near miss, timezone near miss, observation-window near miss, cancellation near miss, postponement near miss, substitution near miss, dispute near miss, precision/rounding near miss, payout/collateral near miss, entity-scope near miss, cross-event implication, and superficially similar unrelated event.

Run `polytrading ai corpus preregister --policy data/gold/policy.json --dir data/gold` and verify
that it creates 500 contract capture units, 1,000 contract review units, 250 relationship capture
units, 500 relationship review units, and initially empty adjudication queues. Expected: exit 0 and
the resulting policy hash is stored in the registered experiment before any label is written.

- [ ] **Step 2: Capture one source record without outcome knowledge; repeat as 500 tracked work units**

For one contract at a time, copy exact public rule text and record source URL, retrieval timestamp,
the pre-registered information cutoff, event family, and template stratum in a one-row import file.
Import it, inspect the emitted raw/canonical hashes and canonicalization warnings, then remove the
one-row working file. Reject a record whose public terms do not permit research retention. Do not
select it because an apparent relationship was profitable. Mark this step complete separately for
each contract in the corpus work log generated by `corpus preregister`.

- [ ] **Step 3: Validate one completed 50-contract tranche; repeat for 10 tranches**

Run:

```bash
.venv/bin/polytrading ai corpus validate --dir data/gold
```

Expected before the tenth tranche: exit 1 with exact count deficits but zero schema, hash,
provenance, review, or leakage errors for the records already present. Correct a structural error
before importing the next tranche.

- [ ] **Step 4: Reviewer A labels one contract; repeat for all 500 contracts**

Using only the point-in-time source packet, label every critical field, attach exact spans for known
fields, mark missing evidence `unknown`, and assign adversarial tags from the fixed taxonomy. Submit
one review record under Reviewer A's stable pseudonymous ID, then mark that contract's Reviewer A
work unit complete.

- [ ] **Step 5: Reviewer B labels the same contract independently; repeat for all 500 contracts**

Hide Reviewer A's output. Reviewer B performs the same one-contract action under a distinct stable
pseudonymous ID. One person may not occupy both reviewer slots. Mark the work unit complete only
after the tool confirms whether the two label hashes agree or require adjudication.

- [ ] **Step 6: Adjudicate one flagged contract disagreement; repeat until the queue is empty**

A third reviewer, distinct from both original reviewers, resolves one disagreement using only the
point-in-time source packet. Record the original reviews, corrections, final label hash, and
adjudication time. Do not overwrite either disagreeing review.

- [ ] **Step 7: Define one relationship pair or set; repeat as 250 tracked work units**

Choose members only from the same split and according to the pre-registered counts. Record the
proposed label and adversarial tags without exposing either reviewer to the other review. The corpus
validator must reject a relationship that crosses an event-family split or references an unknown
contract.

- [ ] **Step 8: Independently review and, when required, adjudicate one relationship; repeat for all 250**

Reviewer A and Reviewer B independently label one pair/set. A third distinct reviewer adjudicates
different label hashes. Mark the per-relationship work unit complete only when two reviews exist
and any disagreement has a final adjudication record.

- [ ] **Step 9: Freeze and verify the production gold manifest**

Run:

```bash
.venv/bin/polytrading ai corpus validate \
  --dir data/gold \
  --require-contracts 500 \
  --require-templates 20 \
  --require-relationships 250 \
  --require-adversarial 200 \
  --require-two-reviews
.venv/bin/polytrading ai corpus freeze \
  --dir data/gold
```

The freeze command reads the immutable cutoff written by `corpus preregister`. Expected: both
commands exit 0, manifest `frozen` is true, counts meet or exceed all four minima, all items have two
reviews, all disagreements are adjudicated, and cross-split leakage count is zero.

- [ ] **Step 10: Commit the immutable corpus version**

```bash
git add data/gold
git commit -m "data: freeze semantic scout gold corpus v1"
```

Record the commit SHA and manifest hash in the experiment registry before tuning any retrieval or extraction behavior.

---

## Task 5: Implement the Deterministic Candidate-Retrieval Baseline

**Files:**

- Create: `src/polytrading/ai/retrieval.py`
- Create: `tests/ai/test_retrieval.py`

- [ ] **Step 1: Write failing metadata-filter and ranking tests**

Test that candidates are first grouped by compatible event family, settlement family, asset/entity, and overlapping date window. Unknown metadata widens retrieval but adds `missing_metadata` warnings. Assert the retriever never compares a contract to itself, never crosses corpus splits during evaluation, ranks exact title paraphrases above unrelated items, breaks equal scores by contract ID, and returns the same ordering in repeated runs.

Add a gold-metric test where 9 of 10 known positive relationships appear in the top-k candidate set and assert recall is exactly `Decimal("0.9")`.

- [ ] **Step 2: Run tests and verify the expected failure**

Run:

```bash
.venv/bin/python -m pytest tests/ai/test_retrieval.py -q
```

Expected: FAIL during collection because `polytrading.ai.retrieval` does not exist.

- [ ] **Step 3: Implement the declared baseline**

```python
class TfidfCandidateRetriever:
    def __init__(self, top_k: int = 50) -> None:
        if top_k < 1:
            raise ValueError("top_k must be positive")
        self._top_k = top_k
        self._vectorizer = TfidfVectorizer(
            analyzer="char_wb",
            ngram_range=(3, 5),
            lowercase=True,
            min_df=1,
            norm="l2",
            dtype="float64",
        )
```

Fit vocabulary and inverse-document-frequency weights on the train split only, then transform the
requested train, validation, or test split without refitting. Compute cosine similarity with
`linear_kernel`, exclude self, apply deterministic metadata filters, and emit
`RetrievalCandidate(query_contract_id, candidate_contract_id, similarity_decimal, rank, warnings,
feature_version)`. Quantize similarity to 12 decimal places using `ROUND_HALF_EVEN` solely for
stable serialization; ranking uses the original `float64` score plus contract-ID tie-break.

- [ ] **Step 4: Register the baseline model card**

Register `semantic-tfidf-char35` version `1.0.0` with `implementation_kind=deterministic_baseline`, `authority=research_only`, the corpus manifest hash, the exact vectorizer parameters, and all prohibited uses. The feature version is the SHA-256 of canonical parameter JSON plus code revision.

- [ ] **Step 5: Run tests and commit retrieval**

Run:

```bash
.venv/bin/python -m pytest tests/ai/test_retrieval.py -q
.venv/bin/python -m pytest tests/ai -q
.venv/bin/ruff check src/polytrading/ai tests/ai
git add src/polytrading/ai/retrieval.py tests/ai/test_retrieval.py
git commit -m "feat: add deterministic semantic retrieval baseline"
```

Expected: all AI tests pass and Ruff exits 0.

---

## Task 6: Implement a Fail-Closed Rule-Extraction Baseline and Source-Span Validator

**Files:**

- Create: `src/polytrading/ai/extraction.py`
- Create: `src/polytrading/ai/spans.py`
- Create: `src/polytrading/ai/security.py`
- Create: `tests/ai/test_extraction.py`
- Create: `tests/ai/test_spans.py`
- Create: `tests/ai/test_security.py`

- [ ] **Step 1: Write failing exact-span and abstention tests**

Cover ISO dates, named timezones, `>`, `>=`, `<`, `<=`, currency/percentage thresholds, inclusivity, precision phrases, and explicit cancellation/fallback clauses. Every parser output must be either an exact known value with span or unknown.

Mutate one character in the source after extraction and assert span validation fails. Change an operator, timestamp, oracle name, or fallback clause and assert the old extraction becomes invalid. Feed contradictory rules and assert the baseline abstains rather than selecting one clause.

- [ ] **Step 2: Write failing hostile-input tests**

Fixtures include direct instructions, indirect instructions quoted from another page, `<script>`, HTML event handlers, zero-width characters, right-to-left overrides, Cyrillic/Latin confusables, malicious URL text, triple backticks, SQL fragments, shell fragments, and contradictory clauses. Assert none becomes an action, URL fetch, SQL, shell, or populated critical field without an exact source span.

- [ ] **Step 3: Run tests and verify the expected failure**

Run:

```bash
.venv/bin/python -m pytest tests/ai/test_extraction.py tests/ai/test_spans.py tests/ai/test_security.py -q
```

Expected: FAIL during collection because the extraction, span, and security modules do not exist.

- [ ] **Step 4: Implement strict span validation**

```python
def validate_span(span: SourceSpan, canonical_text: str) -> None:
    actual_hash = sha256(canonical_text.encode("utf-8")).hexdigest()
    if span.canonical_text_hash != actual_hash:
        raise SourceHashMismatchError(actual_hash)
    if not 0 <= span.start_char < span.end_char <= len(canonical_text):
        raise SourceSpanBoundsError(span.start_char, span.end_char)
    if canonical_text[span.start_char : span.end_char] != span.exact_text:
        raise SourceSpanTextMismatchError(span.start_char, span.end_char)
```

`validate_rule_fields` checks every known field and returns no partially validated object; one invalid span rejects the complete artifact.

- [ ] **Step 5: Implement the deterministic extraction baseline**

Use explicit regex/token rules with named groups and exact offsets. Only emit values the parser can normalize without semantic guessing. If zero or multiple conflicting matches exist, emit unknown and an abstention reason. The baseline is expected to underperform on nuanced clauses; that failure is measured and retained rather than hidden.

Register `rule-regex-baseline` version `1.0.0` with its parser-pattern hash and corpus manifest. Do not encode an `eligible`, `equivalent`, or `guaranteed` field anywhere in its output.

- [ ] **Step 6: Run focused mutation tests and commit**

Run:

```bash
.venv/bin/python -m pytest tests/ai/test_extraction.py tests/ai/test_spans.py tests/ai/test_security.py -q
.venv/bin/python -m pytest tests/ai -q
.venv/bin/ruff check src/polytrading/ai tests/ai
git add src/polytrading/ai tests/ai
git commit -m "feat: add fail-closed rule extraction baseline"
```

Expected: all tests pass, including all four required mutation categories.

---

## Task 7: Build Provider-Neutral Prompt Packets and Strict Artifact Import

**Files:**

- Create: `src/polytrading/ai/prompt_packets.py`
- Create: `src/polytrading/ai/artifact_import.py`
- Create: `tests/ai/test_prompt_packets.py`
- Create: `tests/ai/test_artifact_import.py`

- [ ] **Step 1: Write failing prompt-packet isolation tests**

Assert a packet contains fixed system policy, exact artifact schema, content hashes, canonical rule text as a JSON data value, explicit `unknown` semantics, forbidden actions, and no tools. Inject strings that resemble role messages or tool calls and prove they remain escaped inside the data field. Identical input and prompt version must yield an identical packet hash.

- [ ] **Step 2: Write failing artifact-import tests**

Reject prose around JSON, multiple JSON values, unknown fields, invalid enums, action/tool fields, unknown model cards, version aliases such as `latest`, invalid/expired spans, changed source hashes, future information cutoffs, duplicate artifact IDs with conflicting content, cost exceeding the supplied budget ledger, and any field named `trade_proposal`, `eligible`, `order`, `size`, `leverage`, or `risk_limit` at any nesting depth.

- [ ] **Step 3: Run tests and verify the expected failure**

Run:

```bash
.venv/bin/python -m pytest tests/ai/test_prompt_packets.py tests/ai/test_artifact_import.py -q
```

Expected: FAIL during collection because both modules are absent.

- [ ] **Step 4: Implement immutable prompt packets**

```python
class PromptPacket(StrictRecord):
    schema_version: Literal[1]
    packet_id: str
    task: Literal["rule_extraction", "relationship_adversarial_review"]
    prompt_version: str
    system_policy: str
    output_schema_json: str
    source_documents_json: str
    source_hashes: tuple[str, ...]
    information_cutoff: datetime
    tools_enabled: Literal[False] = False
    browsing_enabled: Literal[False] = False
```

Build source JSON with `json.dumps`, never string interpolation. A packet is a saved evaluation input for a human-operated or separately approved provider runner; this repository does not transmit it.

- [ ] **Step 5: Implement strict one-way import**

Parse exactly one JSON object with Pydantic strict mode, recursively scan keys against the prohibited-field set, resolve the exact model card, verify timestamps and source hashes against the frozen corpus, validate every span, enforce the caller-provided monthly budget, and append the immutable artifact plus validation disposition. Free-form reasoning may be retained as an opaque string in a separate audit field and is never parsed.

Define the budget with exact decimal arithmetic:

```python
def monthly_inference_budget_usd(equity_usd: Decimal) -> Decimal:
    if equity_usd < 0:
        raise ValueError("equity_usd cannot be negative")
    return min(Decimal("25"), equity_usd * Decimal("0.003125"))
```

- [ ] **Step 6: Run tests and commit the artifact boundary**

Run:

```bash
.venv/bin/python -m pytest tests/ai/test_prompt_packets.py tests/ai/test_artifact_import.py -q
.venv/bin/python -m pytest tests/ai -q
.venv/bin/ruff check src/polytrading/ai tests/ai
git add src/polytrading/ai tests/ai
git commit -m "feat: add provider-neutral AI artifact boundary"
```

Expected: all tests pass and no provider SDK or network client exists under `src/polytrading/ai`.

---

## Task 8: Implement the Frozen Offline Evaluation and Gate Report

**Files:**

- Create: `src/polytrading/ai/metrics.py`
- Create: `src/polytrading/ai/evaluate.py`
- Create: `src/polytrading/ai/report.py`
- Create: `tests/ai/test_metrics.py`
- Create: `tests/ai/test_evaluate.py`
- Create: `tests/ai/test_report.py`

- [ ] **Step 1: Write failing metric tests with hand-calculated examples**

Test:

- critical-field exact match counts unknown/known status and exact normalized value across every critical field;
- candidate recall counts only known positive relationships and is `retrieved_positive / all_positive`;
- span validity is `valid_non_unknown_fields / all_non_unknown_fields`;
- malformed fail-closed rate counts every malformed sample and must equal 1;
- mutation invalidation rate covers operator, timestamp, oracle, and fallback groups independently;
- review reduction is `1 - routed_manual_count / retrieval_candidate_count`;
- decimal division by zero yields a named `NOT_MEASURABLE` status rather than NaN;
- metric thresholds use `>=`, never rounded display values.

- [ ] **Step 2: Write failing gate-state tests**

The overall semantic gate must be `BLOCKED_BY_DEPENDENCY` when payoff-compiler results are absent, even if every measurable metric passes. A metric below threshold yields `FAIL`; absent test-split evaluation yields `NOT_EVALUATED`; any source-span invalidity or malformed acceptance yields `FAIL_CLOSED_BREACH`.

- [ ] **Step 3: Run tests and verify the expected failure**

Run:

```bash
.venv/bin/python -m pytest tests/ai/test_metrics.py tests/ai/test_evaluate.py tests/ai/test_report.py -q
```

Expected: FAIL during collection because the evaluator modules do not exist.

- [ ] **Step 4: Implement exact metrics and frozen evaluation order**

`SemanticEvaluator.run` accepts a frozen manifest hash, registered model versions, split, retrieval top-k, artifacts, malformed-case results, mutation-case results, and optional payoff-compiler results. It rejects train/test family overlap, model cards validated on the same untouched test output, changed prompt/feature versions, and unregistered trial-family IDs.

The evaluator computes train diagnostics first, validation metrics second, and untouched test metrics once per registered experiment. It stores every attempted configuration under the experiment's trial family, including failed runs.

- [ ] **Step 5: Implement a canonical, candid report**

The JSON and Markdown report must contain:

- manifest, code, experiment, model, feature, and prompt hashes;
- exact corpus/split/template/adversarial counts;
- each raw numerator, denominator, exact decimal metric, threshold, and status;
- failure examples keyed by contract/relationship ID without hiding abstentions;
- malformed, hostile-input, and mutation results;
- inference cost of exactly USD 0 for in-repo baselines;
- `class_g_false_eligibility.status = BLOCKED_BY_DEPENDENCY` and dependency `deterministic payoff compiler and graph`;
- overall status `RESEARCH_ONLY_NOT_PROMOTABLE` until every gate, including payoff proof, is measurable and passing.

Do not emit accuracy anecdotes, projected profit, or a promotion recommendation.

- [ ] **Step 6: Run focused and regression tests**

Run:

```bash
.venv/bin/python -m pytest tests/ai/test_metrics.py tests/ai/test_evaluate.py tests/ai/test_report.py -q
.venv/bin/python -m pytest -q
.venv/bin/ruff check .
```

Expected: all tests pass and report snapshots are byte-stable.

- [ ] **Step 7: Commit evaluation**

```bash
git add src/polytrading/ai tests/ai
git commit -m "feat: evaluate offline semantic scout gates"
```

---

## Task 9: Wire the Offline AI CLI, Documentation, and Final Verification

**Files:**

- Modify: `src/polytrading/ai/cli.py`
- Modify: `src/polytrading/cli.py`
- Modify: `README.md`
- Modify: `data/gold/README.md`
- Create: `tests/ai/test_cli.py`

- [ ] **Step 1: Write failing CLI and authority-boundary tests**

Test these commands against the small test corpus and a temporary registry/store:

```text
polytrading ai retrieve --corpus tests/fixtures/ai/corpus --split validation --top-k 50 --output /tmp/retrieval.jsonl
polytrading ai extract-baseline --corpus tests/fixtures/ai/corpus --split validation --output /tmp/extractions.jsonl
polytrading ai prompt-packets --corpus tests/fixtures/ai/corpus --split validation --output /tmp/prompt-packets.jsonl
polytrading ai import-artifacts --input /tmp/extractions.jsonl --corpus tests/fixtures/ai/corpus --db /tmp/ai-test.duckdb --equity-usd 8000
polytrading ai evaluate --corpus tests/fixtures/ai/corpus --experiment-id 019b3b42-0000-7000-8000-000000000001 --output /tmp/ai-report
```

Add AST/import tests proving modules under `polytrading.ai` import no venue authenticated client, execution module, credential module, shell runner, browser tool, or network-capable provider SDK. Recursively assert public artifacts have no prohibited authority fields.

- [ ] **Step 2: Run tests and verify the expected failure**

Run:

```bash
.venv/bin/python -m pytest tests/ai/test_cli.py -q
```

Expected: FAIL because the AI subcommands are not wired.

- [ ] **Step 3: Wire commands to pure services**

Each command accepts explicit paths, split, version, and experiment identifiers; it never reads current time or current market state implicitly. `evaluate` refuses an unfrozen manifest and writes `semantic-scout-report.json` plus `semantic-scout-report.md` atomically. Exit codes are 0 for a completed evaluation even when gates fail, 1 for invalid corpus/artifact/evaluation input, and 2 for CLI usage errors.

- [ ] **Step 4: Document the experiment honestly**

README content must explain:

1. AI scouts and estimates; deterministic software proves and controls;
2. this phase uses deterministic TF-IDF/regex baselines and provider-neutral packets, not a hosted LLM;
3. how to construct, review, freeze, and hash the gold corpus;
4. how to pre-register and run validation/test evaluation;
5. how to interpret abstention, exact match, recall, span validity, mutation invalidation, and review reduction;
6. why the gate remains `RESEARCH_ONLY_NOT_PROMOTABLE` without the payoff compiler;
7. the zero-provider-cost status and future USD 25/0.3125% budget rule;
8. prohibited uses and the absence of credentials, orders, proposal approval, and risk authority.

- [ ] **Step 5: Run final verification**

Run:

```bash
.venv/bin/python -m pytest --cov=polytrading --cov-report=term-missing --cov-fail-under=90
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/polytrading ai corpus validate --dir data/gold --require-contracts 500 --require-templates 20 --require-relationships 250 --require-adversarial 200 --require-two-reviews
.venv/bin/polytrading ai evaluate --corpus data/gold --experiment-id 019b3b42-0000-7000-8000-000000000001 --output var/ai-evaluation
.venv/bin/python -m pip freeze | rg -i 'openai|anthropic|cohere|gemini'
rg -n -i 'place[_ -]?order|cancel[_ -]?order|withdraw|deposit|wallet|signing|risk[_ -]?limit|trade[_ -]?proposal' src/polytrading/ai tests/ai
git diff --check
```

Expected: tests pass at 90% or higher coverage; lint, format, corpus validation, evaluation, and `git diff --check` exit 0; provider scan prints no matches; authority scan matches only prohibited-use declarations and boundary tests. The generated report's overall status remains `RESEARCH_ONLY_NOT_PROMOTABLE` because payoff proof is not present.

- [ ] **Step 6: Commit the completed offline AI increment**

```bash
git add src/polytrading/ai src/polytrading/cli.py tests/ai README.md data/gold/README.md
git commit -m "feat: complete offline semantic scout evaluation"
```

---

## Plan Completion Checks

- [ ] The frozen corpus has at least 500 contracts, 20 templates, 250 relationship pairs/sets, and 200 adversarial examples, with two reviews, complete adjudication, source hashes, exact spans, and zero family leakage.
- [ ] Model cards and artifacts are immutable, version-pinned, research-only, costed, expiring, source-bound, and rejected on malformed/extra/prohibited fields.
- [ ] The deterministic retriever and extractor are declared baselines and their failures remain visible in the report.
- [ ] Every non-unknown critical field validates against exact canonical source text; unsupported values become unknown rather than guessed.
- [ ] Hostile documents remain inert data; no AI package code can browse, call a hosted model, access credentials, create proposals, change risk, or submit orders.
- [ ] The report computes exact-match, recall, span validity, malformed fail-closed behavior, mutation invalidation, review reduction, and cost with raw numerators and denominators.
- [ ] Full Class G false-eligibility remains explicitly blocked on the deterministic payoff compiler; no successful offline metric is misrepresented as permission to trade.
- [ ] All tests, coverage, lint, format, corpus validation, evaluation, dependency scan, authority scan, and `git diff --check` commands pass before the plan is declared implemented.
