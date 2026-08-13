# Source-Use and Independent-Review Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a fail-closed source-use evidence gate that keeps the real Polymarket corpus quarantined while producing a verified metadata-only inventory and synthetic-tested blinded review packets.

**Architecture:** A network-facing evidence module streams and hashes allowlisted official pages without retaining full bodies. Pure policy models require a separately supplied human approval covering the exact use, evidence, and intake hashes; an offline queue builder then emits either a safe blocked inventory or two blinded packets. Existing review append commands validate the selected corpus item and immutable input hash before writing.

**Tech Stack:** Python 3.12+, Pydantic 2.13.4, httpx 0.28.1, pytest 9.1.1, Ruff 0.15.22

## Global Constraints

- Automated capture may create only `requires_external_confirmation`; software cannot generate approval.
- Full official-page bodies may exist transiently in memory but are not persisted or logged.
- Each stored excerpt is at most 25 whitespace-delimited words.
- Real source-text packets require exact scope, evidence, and intake-manifest coverage from a separate human approval.
- Every non-exact, inactive, rejected, or absent approval produces zero source-text packets.
- Blocked inventory excludes source text, slugs, and URLs.
- Reviewer identifiers are explicit and distinct; artifacts do not claim to prove human identity.
- Real output stays under ignored `var/source-use` and `var/review-queue`; `data/gold` remains unchanged.
- No message sending, trading, credentials, KYC, corpus promotion, or network dependency inside `polytrading.ai`.

## File Structure

- Create `src/polytrading/corpus_intake/source_policy.py` for strict scope, evidence, approval, and decision models.
- Create `src/polytrading/corpus_intake/evidence.py` for allowlisted streaming capture and source-use run verification.
- Create `src/polytrading/corpus_intake/review_queue.py` for blocked inventory and blinded packet artifacts.
- Modify `src/polytrading/cli.py` for `collect source-use` and `collect review-queue`.
- Modify `.gitignore` so real source-use and review-queue runs remain local artifacts.
- Modify `src/polytrading/ai/corpus.py` and `src/polytrading/ai/cli.py` for append-time review integrity.
- Add focused tests under `tests/corpus_intake/`; update AI and top-level CLI tests.

---

### Task 1: Exact source-use policy boundary

**Files:**
- Create: `src/polytrading/corpus_intake/source_policy.py`
- Create: `tests/corpus_intake/test_source_policy.py`

**Interfaces:**
- Produces: `IntendedUseScope`, `SourceEvidence`, `SourceUseAssessment`, `SourceUseApproval`, `GateDecision`, `canonical_sha256(value)`, and `evaluate_source_gate(assessment, approval, scope, evidence_sha256s, intake_manifest_sha256s, as_of)`.
- Consumes: `StrictRecord` and `normalize_utc_timestamp`.

- [x] **Step 1: Write failing decision tests**

```python
def test_automated_assessment_cannot_claim_approval():
    with pytest.raises(ValidationError):
        SourceUseAssessment(status="approved", **ASSESSMENT_FIELDS)


def test_gate_accepts_only_exact_effective_human_approval():
    result = evaluate_source_gate(
        assessment=assessment(), approval=approval(), scope=scope(),
        evidence_sha256s=(HASH_A,), intake_manifest_sha256s=(HASH_B,), as_of=NOW,
    )
    assert (result.allowed, result.reason_code) == (True, "exact_human_approval")


@pytest.mark.parametrize("change", ["scope", "evidence", "manifest", "expired", "future"])
def test_gate_fails_closed_for_nonexact_or_inactive_approval(change):
    assert decision_for(change).allowed is False
```

- [x] **Step 2: Verify the tests fail because the module is absent**

Run: `pytest tests/corpus_intake/test_source_policy.py -q`

- [x] **Step 3: Implement strict models and the decision function**

`IntendedUseScope` fixes source `polymarket`, maximum records `1..5000`, local retention, derived labels, offline evaluation, and proprietary research to true, and redistribution and generative training to false. `SourceUseAssessment.status` accepts only `requires_external_confirmation` or `rejected`; it embeds the exact scope and validates its `scope_sha256`. `SourceUseApproval` requires a nonempty approver ID, role `source_owner_authorization` or `qualified_legal_review`, approval reference, sorted unique SHA-256 tuples, effective UTC time, and optional later expiry. Every `evidence_sha256` is the canonical hash of a complete `SourceEvidence` record, not merely its downloaded-body hash.

```python
def evaluate_source_gate(*, assessment, approval, scope, evidence_sha256s,
                         intake_manifest_sha256s, as_of):
    if assessment.status == "rejected":
        return GateDecision(allowed=False, reason_code="source_use_rejected")
    if approval is None:
        return GateDecision(allowed=False, reason_code="external_confirmation_required")
    if approval.scope_sha256 != canonical_sha256(scope):
        return GateDecision(allowed=False, reason_code="approval_scope_mismatch")
    if approval.evidence_sha256s != tuple(sorted(evidence_sha256s)):
        return GateDecision(allowed=False, reason_code="approval_evidence_mismatch")
    if approval.intake_manifest_sha256s != tuple(sorted(intake_manifest_sha256s)):
        return GateDecision(allowed=False, reason_code="approval_manifest_mismatch")
    if as_of < approval.effective_at:
        return GateDecision(allowed=False, reason_code="approval_not_effective")
    if approval.expires_at is not None and as_of >= approval.expires_at:
        return GateDecision(allowed=False, reason_code="approval_expired")
    return GateDecision(allowed=True, reason_code="exact_human_approval")
```

- [x] **Step 4: Run tests and commit**

Run: `pytest tests/corpus_intake/test_source_policy.py -q`

```bash
git add src/polytrading/corpus_intake/source_policy.py tests/corpus_intake/test_source_policy.py
git commit -m "feat(corpus): add exact source-use policy gate"
```

---

### Task 2: Hash-only official evidence capture

**Files:**
- Create: `src/polytrading/corpus_intake/evidence.py`
- Create: `tests/corpus_intake/test_evidence.py`

**Interfaces:**
- Consumes Task 1 models.
- Produces `EvidenceTarget`, `POLYMARKET_EVIDENCE_TARGETS`, `capture_evidence(client, target, retrieved_at, max_response_bytes)`, `SourceUseRunWriter`, and `verify_source_use_run(path)`.

- [x] **Step 1: Write failing streaming and artifact tests**

```python
@pytest.mark.asyncio
async def test_capture_hashes_page_without_retaining_body():
    record = await capture_evidence(mock_client(OFFICIAL_HTML), TARGET, NOW, 4096)
    assert record.body_sha256 == sha256(OFFICIAL_HTML).hexdigest()
    assert record.full_body_retained is False
    assert "body_text" not in record.model_fields


def test_writer_records_unresolved_assessment_and_manifest_last(tmp_path):
    writer.complete(evidence=records(), scope=scope())
    verified = verify_source_use_run(output)
    assert verified.assessment.status == "requires_external_confirmation"
    assert verified.evidence_count == 2
```

Also test wrong URL/redirect, status, content type, UTF-8, missing locator, response-size overflow, excerpt length, non-empty output, and symlink escape.

- [x] **Step 2: Verify the tests fail**

Run: `pytest tests/corpus_intake/test_evidence.py -q`

- [x] **Step 3: Implement the two immutable official targets**

Use exact URLs `https://docs.polymarket.com/api-reference/predictions/overview` and `https://institutional.polymarket.com/`. Store only a short allowlisted excerpt for each. Build requests from the target; require exact final URL, HTTP 200, `text/html`, valid UTF-8, locator presence, and the configured byte bound. Hash the exact bytes while iterating `response.aiter_bytes()`, then discard the buffer.

- [x] **Step 4: Implement immutable source-use artifacts**

Restrict output beneath `var/source-use`. Write `evidence.jsonl`, unresolved `assessment.json`, `licensing-inquiry.md` headed `DRAFT — NOT SENT`, then `manifest.json` last. The manifest binds file hashes and the scope hash. Verification rechecks file hashes, URL allowlist, evidence/assessment/scope bindings, and returns typed data.

- [x] **Step 5: Run focused verification and commit**

Run: `pytest tests/corpus_intake/test_evidence.py tests/corpus_intake/test_source_policy.py -q`

Run: `ruff check src/polytrading/corpus_intake/evidence.py tests/corpus_intake/test_evidence.py`

```bash
git add src/polytrading/corpus_intake/evidence.py tests/corpus_intake/test_evidence.py
git commit -m "feat(corpus): capture hash-only source-use evidence"
```

---

### Task 3: Blocked inventory and blinded packets

**Files:**
- Create: `src/polytrading/corpus_intake/review_queue.py`
- Create: `tests/corpus_intake/test_review_queue.py`

**Interfaces:**
- Consumes `verify_run`, `verify_source_use_run`, `SourceUseApproval`, and `evaluate_source_gate`.
- Produces `BlockedInventoryRow`, `ReviewAssignment`, `ReviewQueueResult`, and `prepare_review_queue(intake_directories, source_use_directory, output, project_root, as_of, approval, reviewer_ids, ontology_version)`.

- [x] **Step 1: Write failing blocked-path tests**

```python
def test_unresolved_use_emits_metadata_only_inventory(intake_run, source_use_run, tmp_path):
    result = prepare_review_queue(
        intake_directories=(intake_run,), source_use_directory=source_use_run,
        output=tmp_path / "var/review-queue/blocked", project_root=tmp_path,
        as_of=NOW, approval=None, reviewer_ids=None,
        ontology_version="candidate-triage-v1",
    )
    assert result.allowed is False
    assert result.reviewer_packet_count == 0
    text = (result.output / "blocked_inventory.jsonl").read_text()
    for key in ("question", "description", "resolution_source", "slug", "api_url", "public_event_url"):
        assert key not in text
```

Also test tampered intake, duplicate cross-run identities, non-empty output, and symlink escape.

- [x] **Step 2: Verify blocked tests fail**

Run: `pytest tests/corpus_intake/test_review_queue.py -q`

- [x] **Step 3: Implement verified metadata inventory**

Call `verify_run()` for every intake and hash each exact manifest. `BlockedInventoryRow` contains only schema version, candidate ID, source, source-market ID, event-family ID, routing tags, canonical candidate hash, and intake-manifest hash. Sort deterministically and reject duplicate candidate or source-market identities. Write `decision.json`, inventory, then manifest last.

- [x] **Step 4: Add failing synthetic approved-path tests**

```python
def test_exact_approval_emits_two_blinded_packets(
    intake_run, source_use_run, exact_approval, tmp_path
):
    result = prepare_review_queue(
        intake_directories=(intake_run,), source_use_directory=source_use_run,
        output=tmp_path / "var/review-queue/allowed", project_root=tmp_path,
        as_of=NOW, approval=exact_approval,
        reviewer_ids=("reviewer-a", "reviewer-b"),
        ontology_version="candidate-triage-v1",
    )
    assert result.allowed is True
    assert result.reviewer_packet_count == 2 * result.item_count
    assert "reviewer-b" not in (result.output / "reviewer-a/assignments.jsonl").read_text()
    assert "reviewer-a" not in (result.output / "reviewer-b/assignments.jsonl").read_text()
```

Parameterize scope, evidence, manifest, expiry, and effective-time mismatches and assert zero packets for each.

- [x] **Step 5: Implement blinded packet release**

Only after an allowed decision, require exactly two distinct reviewer IDs. Each `ReviewAssignment` includes one reviewer ID, ontology version, immutable input hash, source identity, event family, question, description, resolution source, category/tags, and source times. It never includes the other reviewer or any decisions. Write each reviewer directory separately; manifest and decision contain hashes/counts but no source text.

- [x] **Step 6: Run tests, lint, and commit**

Run: `pytest tests/corpus_intake/test_review_queue.py tests/corpus_intake/test_source_policy.py tests/corpus_intake/test_evidence.py -q`

Run: `ruff check src/polytrading/corpus_intake/review_queue.py tests/corpus_intake/test_review_queue.py`

```bash
git add src/polytrading/corpus_intake/review_queue.py tests/corpus_intake/test_review_queue.py
git commit -m "feat(corpus): gate blinded review preparation"
```

---

### Task 4: Append-time review integrity

**Files:**
- Modify: `src/polytrading/ai/review.py`
- Modify: `src/polytrading/ai/corpus.py`
- Modify: `src/polytrading/ai/cli.py`
- Modify: `tests/ai/test_corpus.py`
- Modify: `tests/ai/test_cli.py`

**Interfaces:**
- Produces `CorpusReviewAssignment` and `append_corpus_review(directory: Path, candidate: ReviewRecord, assignment: CorpusReviewAssignment | None = None) -> None`.
- Preserves low-level immutable `append_review(path, candidate)`.

- [x] **Step 1: Write failing item/hash/frozen tests**

```python
def test_append_validates_item_and_hash_before_mutation(corpus):
    append_corpus_review(corpus, exact_review)
    before = (corpus / "reviews.jsonl").read_bytes()
    with pytest.raises(ValueError, match="unknown contract"):
        append_corpus_review(corpus, missing_item_review)
    with pytest.raises(ValueError, match="input hash"):
        append_corpus_review(corpus, wrong_hash_review)
    assert (corpus / "reviews.jsonl").read_bytes() == before
```

Add relationship and frozen-manifest cases. Add an assignment case proving item, reviewer, and input hash must all match before append.

- [x] **Step 2: Write failing CLI directory test**

Call `ai corpus review --dir <temporary-corpus> --item-type contract --item-id contract-0001 --review-file <review.json> --assignment-file <assignment.json>`; assert only that directory changes. Assert omission of `--dir` is a usage error. The assignment argument is optional only for legacy corpora that have no assignment artifact.

- [x] **Step 3: Verify focused failures**

Run: `pytest tests/ai/test_corpus.py tests/ai/test_cli.py -q`

- [x] **Step 4: Implement validation and route CLI**

Define `CorpusReviewAssignment` in `ai.review` with schema version, item type, item ID, reviewer ID, and input hash. Load strict contracts and relationships, reject a frozen manifest, find the exact `(item_type, item_id)`, compare `item_input_hash`, validate every supplied assignment field against the review, and only then call `append_review(directory / "reviews.jsonl", candidate)`. Add required `--dir` and optional `--assignment-file` to both review and adjudicate commands; remove hardcoded `data/gold/reviews.jsonl`.

- [x] **Step 5: Run focused tests and commit**

Run: `pytest tests/ai/test_corpus.py tests/ai/test_cli.py -q`

```bash
git add src/polytrading/ai/review.py src/polytrading/ai/corpus.py src/polytrading/ai/cli.py tests/ai/test_corpus.py tests/ai/test_cli.py
git commit -m "fix(corpus): validate reviews before append"
```

---

### Task 5: CLI and real blocked run

**Files:**
- Modify: `src/polytrading/cli.py`
- Modify: `tests/test_cli.py`
- Modify: `tests/corpus_intake/test_evidence.py`
- Modify: `tests/corpus_intake/test_review_queue.py`
- Modify: this plan with actual run evidence.

**Interfaces:**
- Produces `collect source-use` and `collect review-queue` commands.

- [x] **Step 1: Write failing command-contract tests**

Require source, output, explicit UTC times, bounds, repeated `--intake`, source-use run, and ontology version. Allow optional approval and reviewer IDs. Prove a blocked decision exits successfully while malformed evidence fails.

- [x] **Step 2: Verify parser failures**

Run: `pytest tests/test_cli.py tests/corpus_intake/test_evidence.py tests/corpus_intake/test_review_queue.py -q`

- [x] **Step 3: Implement orchestration**

`_collect_source_use()` captures both official targets with the public HTTP client, completes and verifies the run, and prints the unresolved state. `_prepare_review_queue()` validates an optional `SourceUseApproval`, prepares the run, and prints its allow/block reason and counts. Neither function sends the inquiry.

- [x] **Step 4: Run all local checks before network access**

Run: `pytest -q`

Run: `ruff check .`

Run: `ruff format --check .`

Run: `git diff --check`

- [x] **Step 5: Hash `data/gold`, then capture real evidence**

Hash all `data/gold` files. Run `polytrading collect source-use` into `var/source-use/2026-08-13-polymarket-v1` using the actual UTC retrieval time and a 2 MiB body cap.

- [x] **Step 6: Generate and verify real blocked inventory**

Run `polytrading collect review-queue` with the open-v2 and closed-v2 intake directories, the new source-use run, output `var/review-queue/2026-08-13-polymarket-blocked-v1`, actual UTC `--as-of`, and ontology `candidate-triage-v1`.

Expected: 1,000 blocked rows, zero reviewer packets, zero reviews, reason `external_confirmation_required`, an unsent inquiry, verified manifests, no forbidden inventory keys, and unchanged `data/gold` hashes.

- [x] **Step 7: Record evidence and commit**

Record actual retrieval time, evidence body hashes, source-use manifest hash, both intake hashes, inventory count, queue manifest hash, zero packet/review counts, and unchanged-gold result in this plan.

```bash
git add src/polytrading/cli.py tests/test_cli.py tests/corpus_intake/test_evidence.py tests/corpus_intake/test_review_queue.py docs/superpowers/plans/2026-08-12-source-use-review-gate.md
git commit -m "feat(corpus): expose source-use review gate"
```

## Real-Run Execution Record

**Executed:** 2026-08-13
**Outcome:** Successful fail-closed release decision

- The first capture attempt failed before manifest creation because the former Market Data page no longer contained the stale access-language locator. A hash-and-short-neighborhood diagnostic identified the current canonical Predictions API overview. A failing regression fixture was added before the allowlist was corrected.
- Official evidence retrieval time: `2026-08-13T03:28:49Z`.
- Evidence URLs: `https://docs.polymarket.com/api-reference/predictions/overview` and `https://institutional.polymarket.com/`.
- Exact response-body SHA-256 values: `00b5731f401a474c6ebd21496a370e7c468dd34c2ac7ba2dbbfebe40ec7445e5` and `12fad1e31d8950444754af3796db333e7006ec76321b3c15d002a839b2aca0fe`.
- Canonical evidence-record SHA-256 values: `a6103801b17b0636cb8526f36be3f3f9a00ce39a24f2523c3c1cbedf5c3d0b3a` and `ff9525479587d48a3737d25ff0d2702e69b9c2d853507c1784df5d9909a4f265`.
- Source-use manifest SHA-256: `22b4e75ad66dcf744e57df9e59770430271bb1519db54f2a90ff076f5c4b21da`.
- Open intake manifest SHA-256: `172af3c9c3f2d302f01422ae38a6cad321f51aa27c859806df33cd01c65c5c77`.
- Closed intake manifest SHA-256: `53d1de61fb4e35c4a523d4b7adf59fcd8435f7418e9052b6c65bf957067839ea`.
- Review-queue manifest SHA-256: `62a2509d8fd8a814789860627f62a797b14681cd913f9fd41b29939245aaefbb`.
- Gate result: `external_confirmation_required`.
- Counts: `1,000` metadata-only blocked rows, `0` source-text reviewer packets, and `0` review records.
- Verified invariants: no full official-page bodies, no forbidden source-text/URL keys in blocked inventory, no reviewer directories, and the inquiry is marked unsent.
- All eight `data/gold` per-file hashes matched before and after the run; contracts, labels, progress, relationships, and reviews remain empty.

---

### Task 6: Final verification and self-review

**Files:**
- Modify only files that fail an objective check.

**Interfaces:**
- Produces a verified, review-ready branch.

- [ ] **Step 1: Run complete quality gate**

```bash
pytest --cov=polytrading --cov-report=term-missing --cov-fail-under=90
ruff check .
ruff format --check .
git diff --check
git status --short
git log --oneline main..HEAD
```

- [ ] **Step 2: Audit safety properties**

Confirm code cannot generate approval; no full page body is retained; the real run has no packet directories; blocked schemas contain no source text or URL fields; approval checks scope, evidence, manifests, dates, and role; review CLI has no hardcoded gold path; AI remains offline; inquiry says unsent; and production gold hashes match.

- [ ] **Step 3: Review the complete branch diff against the design**

Run `git diff --stat main...HEAD` and `git diff main...HEAD`. Correct any discovered gap by adding a failing regression test first.

- [ ] **Step 4: Repeat Step 1 after every correction**

Do not claim success from a stale verification run. Commit only substantive corrections with `fix(corpus): close source-use review gaps`.
