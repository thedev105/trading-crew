# Task 3 Report: Reviewed Semantic Corpus Workflow

## Implementation

- Added strict, frozen import, warning, review, progress, policy, and manifest records.
- Added inert hostile-source canonicalization using only `html.parser.HTMLParser`: exact raw text
  and its UTF-8 SHA-256 are retained; script/style/template contents and active attributes are
  removed; CRLF becomes LF; NFC is applied; format controls are removed with raw offsets and code
  points; Cyrillic/Greek confusable candidates are retained and warned rather than translated.
- Added immutable contract/review append semantics, duplicate/revision/event-family/derivative and
  relationship split-leakage validation, two-person review closure, and distinct adjudication.
- Added deterministic pending-only preregistration, atomic local writes, content-addressed frozen
  manifests, stable exact retries, and new versions for changed corpus content.
- Added all requested `polytrading ai corpus` CLI command contracts with no network, provider,
  credential, trading, or execution authority.

## TDD Evidence

Initial diagnostic RED:

```text
.venv/bin/python -m pytest tests/ai/test_corpus.py -q
ERROR tests/ai/test_corpus.py
ModuleNotFoundError: No module named 'polytrading.ai.corpus'
1 error in 0.22s; exit 2
```

Additional RED/GREEN checks caught and fixed JSON strict-mode loading, unfrozen placeholder
activation, CRLF raw-source warning offsets, checked-in empty-progress preregistration, and stable
content-addressed freeze retries. The last of these failed because a repeated freeze recomputed a
different `created_at` for the same dataset ID; it now returns the existing immutable manifest.

Fresh final verification:

```text
.venv/bin/python -m pytest tests/ai/test_corpus.py -q
23 passed in 0.42s

.venv/bin/python -m pytest tests/ai -q
41 passed in 0.49s

.venv/bin/python -m pytest -q
319 passed in 5.41s

.venv/bin/ruff check src tests
All checks passed!

.venv/bin/ruff format --check src tests
55 files already formatted

git diff --check
exit 0 (no output)
```

## Fixture and Production Counts

```text
fixture contracts=6
fixture relationships=3
fixture labels=3
fixture reviews=0
production contracts=0
production relationships=0
production labels=0
production reviews=0
production progress=0
policy templates=20 contracts=500 relationships=250
manifest frozen=False counts={'contracts': 0, 'labels': 0, 'relationships': 0, 'reviews': 0}
```

The six contracts and three relationship labels are explicitly synthetic unit-test fixtures. No
production corpus source, label, review, adjudication, or human-completion claim was fabricated.

## Atomicity and Freeze Coverage

- Tests assert no temporary files remain after a freeze and verify the first unfrozen placeholder
  is atomically replaced.
- Re-freezing identical content returns byte-identical stored state even with a different supplied
  creation time.
- Changing labels produces a different dataset ID and manifest file while the first frozen
  manifest remains byte-identical.
- Freeze rejects unresolved reviews and all validated cross-split leakage before writing a manifest.

## Self-review and Concerns

- Reviewed the authority boundary: implementation is pure local filesystem processing and imports
  no provider SDK, network client, credentials, venue action, risk decision, or trading path.
- Reviewed immutable identities and exact retry behavior for contracts, reviews, progress, and
  manifests; corrections append changed content/version rather than editing a frozen manifest.
- Confusable detection deliberately flags retained Greek/Cyrillic letters as suspicious candidates;
  it is conservative and is not a Unicode spoofing classifier.
- Review/adjudication CLI writes to the exact checked-in `data/gold/reviews.jsonl` workflow path
  because the required command contract does not include a corpus-directory option.

## Code Review Corrections

Four review findings were fixed test-first in a separate follow-up commit:

- Corrected the point-in-time relation to require
  `source_retrieved_at <= information_cutoff`; equality and earlier retrieval are accepted, while
  after-cutoff retrieval is rejected on both import and loaded corpus rows.
- Made `CorpusContract` recompute and compare the raw UTF-8 SHA-256, deterministic canonical text,
  and canonical-text SHA-256. Manually tampered JSONL now fails during ordinary corpus loading,
  before a manifest can be frozen.
- Exported one deterministic `item_input_hash` for immutable contract and relationship records and
  bound every loaded review to that actual item hash before review resolution. Stale contract and
  changed-membership relationship reviews fail closed.
- Changed contract persistence to preserve the exact existing byte prefix and append only validated
  new rows through the atomic temporary-file rename. Exact retries do not write; immutable-identity
  conflicts fail without changing file bytes.

Review RED evidence:

```text
.venv/bin/python -m pytest tests/ai/test_corpus.py -q
8 failed, 23 passed in 0.41s

Failures: accepted after-cutoff retrieval; rejected valid earlier retrieval; accepted tampered raw
hash, canonical text, and canonical hash; accepted stale contract review hash; failed to diagnose
stale relationship review hash; reordered existing JSONL bytes during append.
```

Review GREEN evidence:

```text
.venv/bin/python -m pytest tests/ai/test_corpus.py -q
32 passed

.venv/bin/python -m pytest tests/ai -q
50 passed in 0.65s

.venv/bin/python -m pytest -q
328 passed in 6.45s

.venv/bin/ruff check src tests
All checks passed!

.venv/bin/ruff format --check src tests
55 files already formatted

git diff --check
exit 0 (no output)
```

Deferred minor: active URL-bearing attributes remain outside this correction's requested scope.
The sanitizer continues to remove active element contents and event/style/srcdoc attributes; a
future change should add warnings for active URL attributes without translating or executing them.
