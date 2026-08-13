# Public Corpus Acquisition Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and operate a public-only Polymarket intake command that creates an immutable, provenance-rich review-candidate corpus without modifying gold data.

**Architecture:** A dedicated `corpus_intake` package parses exact keyset-paginated Gamma API responses into quarantined source candidates. The top-level CLI owns network access; an artifact writer streams raw captures, writes normalized/report artifacts atomically, and writes the completed manifest last.

**Tech Stack:** Python 3.12+, dataclasses, `httpx`, `argparse`, SHA-256, JSON/JSONL, pytest, Ruff

## Global Constraints

- Public unauthenticated HTTPS `GET` only; never call an order, account, wallet, credential, or KYC endpoint.
- Persist explicit `retrieved_at` and `information_cutoff`; reject a cutoff after retrieval.
- Every candidate has `retention_status: review_required`; the implementation has no promotion command.
- Reject `data/gold` and its descendants as output destinations.
- Page limit is `1..100`; page and candidate counts, response bytes, retries, and request duration are bounded.
- Exact raw UTF-8 bodies and SHA-256 lineage are preserved; schema drift and cursor loops fail closed.
- Source heuristics are reviewer-routing tags, never labels or trading signals.
- The acquisition package does not import `polytrading.ai`, and the AI package retains its no-network boundary.
- Quarantined run artifacts live under gitignored `var/` and are not committed.

---

### Task 1: Intake models and strict Polymarket page parser

**Files:**
- Create: `src/polytrading/corpus_intake/__init__.py`
- Create: `src/polytrading/corpus_intake/models.py`
- Create: `src/polytrading/corpus_intake/polymarket.py`
- Create: `tests/corpus_intake/test_polymarket.py`
- Create: `tests/fixtures/polymarket/markets_keyset_page_1.json`
- Create: `tests/fixtures/polymarket/markets_keyset_page_2.json`

**Interfaces:**
- Consumes: exact response `bytes`, request URL/cursor/page ordinal, explicit UTC timestamps, and selected response headers.
- Produces: `parse_page(...) -> ParsedPage`, where `ParsedPage.raw` is `RawPageCapture` and `ParsedPage.candidates` is `tuple[CorpusCandidate, ...]`.

- [ ] **Step 1: Write failing parser tests**

Cover exact-body SHA-256, required identity/question fields, optional-field warnings, event-family extraction, stable candidate IDs, and deterministic routing tags. The central assertion is:

```python
page = parse_page(
    body=fixture.read_bytes(),
    request_url="https://gamma-api.polymarket.com/markets/keyset?limit=2&include_tag=true",
    requested_cursor=None,
    page_ordinal=1,
    retrieved_at=RETRIEVED_AT,
    information_cutoff=CUTOFF,
    headers={"content-type": "application/json"},
)
assert page.raw.body_sha256 == sha256(fixture.read_bytes()).hexdigest()
assert {item.retention_status for item in page.candidates} == {"review_required"}
```

- [ ] **Step 2: Run the focused test and confirm RED**

Run: `python -m pytest tests/corpus_intake/test_polymarket.py -q`  
Expected: FAIL because `polytrading.corpus_intake` does not exist.

- [ ] **Step 3: Implement immutable models and parser**

Define:

```python
@dataclass(frozen=True)
class RawPageCapture:
    source: str
    endpoint: str
    request_url: str
    requested_cursor: str | None
    returned_cursor: str | None
    page_ordinal: int
    retrieved_at: datetime
    information_cutoff: datetime
    status_code: int
    response_headers: tuple[tuple[str, str], ...]
    body_text: str
    body_sha256: str

@dataclass(frozen=True)
class CorpusCandidate:
    candidate_id: str
    source: str
    source_market_id: str
    condition_id: str | None
    event_family_id: str
    slug: str | None
    api_url: str
    public_event_url: str | None
    question: str
    description: str | None
    resolution_source: str | None
    category: str | None
    start_date: str | None
    end_date: str | None
    active: bool | None
    closed: bool | None
    archived: bool | None
    retrieved_at: datetime
    information_cutoff: datetime
    raw_body_sha256: str
    raw_page_ordinal: int
    retention_status: Literal["review_required"]
    warnings: tuple[str, ...]
    routing_tags: tuple[str, ...]
```

`parse_page` must strictly decode UTF-8, require a JSON object with `markets: list`, require a non-empty string `next_cursor` when present, reject booleans as IDs, allowlist normalized fields, and preserve all unknown fields only through `body_text`.

- [ ] **Step 4: Run the focused tests and confirm GREEN**

Run: `python -m pytest tests/corpus_intake/test_polymarket.py -q`  
Expected: PASS.

- [ ] **Step 5: Commit the parser slice**

```bash
git add src/polytrading/corpus_intake tests/corpus_intake tests/fixtures/polymarket
git commit -m "feat(corpus): parse public Polymarket candidates"
```

### Task 2: Bounded keyset collector and de-duplication

**Files:**
- Modify: `src/polytrading/corpus_intake/polymarket.py`
- Modify: `tests/corpus_intake/test_polymarket.py`

**Interfaces:**
- Consumes: `httpx.AsyncClient`, `AcquisitionRequest`, and `Callable[[RawPageCapture], None]`.
- Produces: `acquire_polymarket(...) -> AcquisitionResult` with candidates and diagnostics.

- [ ] **Step 1: Write failing mocked-transport tests**

Test two-page traversal using `next_cursor` as `after_cursor`, max-candidate truncation, cursor-loop rejection, conflicting same-ID rejection, exact duplicate diagnostics, response-size rejection, content-type/status rejection, and an empty terminal page.

```python
result = await acquire_polymarket(client, request, captured.append, sleep=no_sleep)
assert len(captured) == 2
assert len(result.candidates) == 3
assert seen_requests[1].url.params["after_cursor"] == "cursor-2"
```

- [ ] **Step 2: Run the collector tests and confirm RED**

Run: `python -m pytest tests/corpus_intake/test_polymarket.py -q`  
Expected: FAIL because `acquire_polymarket` is absent.

- [ ] **Step 3: Implement the bounded collector**

Add `AcquisitionRequest`, `AcquisitionDiagnostics`, `AcquisitionResult`, and:

```python
async def acquire_polymarket(
    client: httpx.AsyncClient,
    request: AcquisitionRequest,
    on_raw_page: Callable[[RawPageCapture], None],
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> AcquisitionResult:
    ...
```

Always request `closed=false`, `include_tag=true`, and a limit no greater than 100. Validate the requested/returned cursor set before the next request. Keep same-event contracts distinct, drop byte-identical duplicate candidates, and fail on same-ID conflicts. Sort the final candidates by `(source, event_family_id, source_market_id)`.

- [ ] **Step 4: Run focused tests and confirm GREEN**

Run: `python -m pytest tests/corpus_intake/test_polymarket.py -q`  
Expected: PASS.

- [ ] **Step 5: Commit the collector slice**

```bash
git add src/polytrading/corpus_intake/polymarket.py tests/corpus_intake/test_polymarket.py
git commit -m "feat(corpus): collect bounded keyset pages"
```

### Task 3: Quarantined artifact writer and coverage report

**Files:**
- Create: `src/polytrading/corpus_intake/artifacts.py`
- Create: `tests/corpus_intake/test_artifacts.py`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: output path, `AcquisitionRequest`, streamed `RawPageCapture` records, and completed `AcquisitionResult`.
- Produces: `raw_pages.jsonl`, `candidates.jsonl`, `coverage.json`, and last-written `manifest.json`.

- [ ] **Step 1: Write failing artifact tests**

Prove canonical JSONL, hash correctness, deterministic candidate order, count/tag/category/event-family coverage, manifest-last behavior, non-empty-directory rejection, and rejection of gold paths after symlink-aware resolution.

```python
writer = CorpusRunWriter(output, project_root=project_root, request=request)
writer.append_raw_page(raw_page)
writer.complete(result)
manifest = json.loads((output / "manifest.json").read_text())
assert manifest["retention_status"] == "review_required"
assert manifest["files"]["candidates.jsonl"]["sha256"] == file_sha256(...)
```

- [ ] **Step 2: Run the artifact tests and confirm RED**

Run: `python -m pytest tests/corpus_intake/test_artifacts.py -q`  
Expected: FAIL because `CorpusRunWriter` is absent.

- [ ] **Step 3: Implement atomic, manifest-last output**

Use `json.dumps(..., sort_keys=True, separators=(",", ":"), ensure_ascii=False)`. Stream raw records to a newly created `raw_pages.jsonl`; flush after each page. Write each remaining file to a sibling `.tmp` path and atomically replace its final path. Include schema version `corpus-intake-v1`, official endpoint/documentation URLs, `retention_status: review_required`, no retention basis, explicit run inputs, diagnostics, counts, and file hashes.

- [ ] **Step 4: Add quarantine outputs to `.gitignore`**

Add exactly:

```gitignore
var/corpus-intake/
```

- [ ] **Step 5: Run focused tests and confirm GREEN**

Run: `python -m pytest tests/corpus_intake/test_artifacts.py -q`  
Expected: PASS.

- [ ] **Step 6: Commit the artifact slice**

```bash
git add .gitignore src/polytrading/corpus_intake/artifacts.py tests/corpus_intake/test_artifacts.py
git commit -m "feat(corpus): write quarantined intake artifacts"
```

### Task 4: Public corpus CLI

**Files:**
- Modify: `src/polytrading/cli.py`
- Modify: `tests/test_cli.py`

**Interfaces:**
- Consumes: `polytrading collect corpus` arguments and the existing retrying public HTTP client.
- Produces: exit `0` and a non-sensitive count summary, exit `2` for input/policy errors, or exit `1` for acquisition failures.

- [ ] **Step 1: Write failing CLI tests**

Add parser/dispatch tests for required timestamps, UTC normalization, cutoff order, `1..5000` candidates, `1..100` page size, `1..100` pages, source allowlist containing only `polymarket`, output quarantine enforcement, and injected acquisition success/failure.

```python
assert main([
    "collect", "corpus", "--source", "polymarket",
    "--output", str(tmp_path / "run"),
    "--retrieved-at", "2026-08-12T12:00:00Z",
    "--information-cutoff", "2026-08-12T12:00:00Z",
    "--max-candidates", "500",
]) == 0
```

- [ ] **Step 2: Run CLI tests and confirm RED**

Run: `python -m pytest tests/test_cli.py -q`  
Expected: FAIL because `collect corpus` is not registered.

- [ ] **Step 3: Register and dispatch the command**

Add the `corpus` subparser beneath `collect`, validate arguments before opening the network client, construct `AcquisitionRequest`, start `CorpusRunWriter`, invoke `acquire_polymarket`, and call `complete` only after acquisition succeeds. Keep source body text out of stdout/stderr.

- [ ] **Step 4: Run CLI and boundary tests and confirm GREEN**

Run: `python -m pytest tests/test_cli.py tests/ai/test_package.py tests/ai/test_security.py -q`  
Expected: PASS.

- [ ] **Step 5: Commit the CLI slice**

```bash
git add src/polytrading/cli.py tests/test_cli.py
git commit -m "feat(cli): acquire public corpus candidates"
```

### Task 5: Real acquisition, verification, and operator record

**Files:**
- Create locally, do not commit: `var/corpus-intake/2026-08-12-polymarket-active/`
- Modify: `docs/superpowers/plans/2026-08-12-public-corpus-acquisition.md`

**Interfaces:**
- Consumes: official public Gamma API and completed CLI.
- Produces: actual quarantined candidate/run artifacts and checked plan evidence.

- [ ] **Step 1: Run the full local quality gate**

```bash
python -m pytest -q
python -m pytest --cov=polytrading --cov-report=term-missing --cov-fail-under=90 -q
ruff check .
ruff format --check .
git diff --check
```

Expected: all tests pass, coverage is at least 90%, lint/format/diff checks pass.

- [ ] **Step 2: Acquire 500 real candidates**

```bash
polytrading collect corpus \
  --source polymarket \
  --output var/corpus-intake/2026-08-12-polymarket-active \
  --retrieved-at 2026-08-12T16:00:00Z \
  --information-cutoff 2026-08-12T16:00:00Z \
  --max-candidates 500 \
  --page-size 100 \
  --max-pages 10
```

Expected: exit `0`, exactly the observed count up to 500, and no authenticated request.

- [ ] **Step 3: Verify artifact integrity without printing source text**

Run a repository utility/test entry point that checks every raw body hash, output-file manifest hash, candidate lineage hash, candidate count, unique source-market ID, and `review_required` retention status. Expected: no mismatches and no gold files changed.

- [ ] **Step 4: Record factual run evidence in this plan**

Check the completed boxes and append the command exit statuses, candidate count, event-family count, routing-tag/category counts, and manifest SHA-256. Do not paste market questions or descriptions into the plan.

- [ ] **Step 5: Self-review the committed diff**

Inspect `git diff main...HEAD`, run a credential/provider/order vocabulary scan in `src/polytrading/corpus_intake`, confirm `data/gold` is byte-identical to `main`, and fix every concrete issue before the final commit.

- [ ] **Step 6: Commit implementation records only**

```bash
git add docs/superpowers/plans/2026-08-12-public-corpus-acquisition.md
git commit -m "docs(corpus): record public intake verification"
```

Never add `var/corpus-intake` to Git.
