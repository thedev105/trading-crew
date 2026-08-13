# Public Corpus Acquisition Design

**Date:** 2026-08-12  
**Status:** Approved for implementation under the user's standing instruction to proceed and self-review  
**Scope:** Public, unauthenticated prediction-market candidate acquisition only

## Objective

Create a reproducible intake pipeline that gathers real public prediction-market records for later human corpus construction. The pipeline must preserve source provenance, expose coverage gaps, and prevent unreviewed or legally unverified material from entering `data/gold`.

This work produces **review candidates**, not semantic labels, relationships, adversarial examples, independent reviews, or trading signals.

## Non-goals

- No authenticated endpoints, credentials, KYC identities, orders, balances, custody, or trading.
- No claim that public API access grants a right to redistribute or use content for model training.
- No automated promotion into `data/gold`.
- No generated or inferred human-review records.
- No attempt to satisfy corpus quotas by duplicating, paraphrasing, or fabricating source records.
- No AI model calls. Heuristics may organize the review queue but cannot assign gold labels.

## Source policy

The first enabled source is Polymarket's official, public Gamma API. The adapter uses the documented keyset-paginated markets endpoint with a page limit no greater than 100. It does not use CLOB trading endpoints.

The source interface is deliberately extensible so that a Kalshi adapter can be added later. Kalshi remains disabled until the exact Developer Agreement and the intended retention/use scope are captured and approved. Multi-source diversity is valuable, but it does not justify treating unclear terms as permission.

Every source has a retention state:

- `review_required`: default; capture is quarantined and cannot be promoted.
- `approved`: a separate, explicit approval record identifies the reviewer, review time, terms URL, terms-document hash, and approved use scope.
- `rejected`: collection/promotion is disabled for that scope.

The initial implementation only writes `review_required` artifacts. It intentionally contains no promotion command.

## Architecture

```text
official public API
        |
        v
exact HTTP capture + request metadata + SHA-256
        |
        v
source-specific validation and normalization
        |
        v
deterministic de-duplication + reviewer-routing heuristics
        |
        v
quarantined JSONL candidates + manifest + coverage report

                X no path to data/gold
```

Network I/O lives outside `polytrading.ai`; the offline semantic-scout package retains its no-network boundary. The top-level `collect corpus` command owns public acquisition. Source parsing and artifact writing live in a dedicated `polytrading.corpus_intake` package.

## Command contract

The initial command is:

```text
polytrading collect corpus \
  --source polymarket \
  --output <directory> \
  --retrieved-at <UTC timestamp> \
  --information-cutoff <UTC timestamp> \
  --max-candidates <positive integer> \
  --market-state <open|closed>
```

Both times are required. Runtime wall-clock time is not silently substituted into persisted evidence. `information_cutoff` must not follow `retrieved_at`. A caller may optionally set a page cap; the default is bounded.

The output directory must not be `data/gold` or a descendant of it. Existing non-empty output directories are rejected, so a retry cannot silently mix runs. The intended local destination is `var/corpus-intake/<run-id>`, which is gitignored.

## Raw capture

For every successful page, persist an append-only record containing:

- source name and documented endpoint;
- request URL and canonical request parameters;
- retrieval and information-cutoff timestamps;
- HTTP status and selected non-sensitive response headers;
- exact response body as UTF-8 text;
- SHA-256 of the exact UTF-8 response bytes;
- page ordinal, requested cursor, and returned cursor.

The client accepts only HTTPS, HTTP 200, JSON content, a top-level object, and the expected market-list shape. Redirects, malformed JSON, invalid UTF-8, schema drift, cursor loops, unexpected statuses, oversized pages, and hash mismatches fail closed.

The request contains no source text in shell arguments, SQL, or logs. Raw response content is written only to the quarantined artifact.

## Candidate normalization

Each accepted source market becomes one candidate with these fields:

- stable candidate ID derived from source name and source market ID;
- source platform, market ID, condition ID, event-family ID, slug, API URL, and public event URL;
- exact question, description, and resolution-source text as provided by the API;
- category, source-provided tag labels, active/closed/archived state, and relevant market dates;
- retrieval time and information cutoff;
- raw-capture SHA-256 and raw page ordinal;
- `retention_status: review_required`;
- deterministic warnings and reviewer-routing tags.

Fields are allowlisted. Unknown API fields remain recoverable from the raw capture but do not automatically enter the candidate record.

Missing or mistyped required identity/question fields reject that market record and increment a diagnostic counter. Optional fields that are missing or mistyped produce warnings and normalized null/empty values.

## De-duplication and event families

The intake run de-duplicates in this order:

1. exact `(source, source_market_id)` duplicates;
2. exact canonical-content duplicates;
3. repeated contracts within the same event family remain distinct candidates but share an event-family key.

Conflicting records with the same source market ID fail the run rather than choosing one silently. Cursor reuse is also fatal.

## Reviewer-routing heuristics

Heuristics may attach zero or more review tags such as:

- `numeric_threshold`
- `bounded_range`
- `deadline_or_date`
- `named_source`
- `multi_outcome_event`
- `sports`
- `politics`
- `crypto`
- `ambiguous_resolution_text`

These tags are transparent, deterministic string rules for sampling and coverage only. The report distinguishes observed source categories from heuristic tags. It never calls either one a semantic ground-truth template.

## Artifacts

A completed run contains:

- `raw_pages.jsonl`: exact page captures and hashes;
- `candidates.jsonl`: normalized, de-duplicated review candidates;
- `manifest.json`: schema version, run inputs, source-policy state, counts, hashes of output files, and completion status;
- `coverage.json`: counts by source category, source-provided tag, event-family key, and reviewer-routing tag, plus rejection/warning diagnostics.

JSON is canonicalized with sorted keys and compact separators. JSONL ends with exactly one newline when non-empty. Candidate order is deterministic: source, event-family key, source market ID.

The manifest is written last using an atomic replace. A failed run must not leave a manifest that claims completion.

## Safety and operating limits

- Public unauthenticated GET only.
- A bounded page count, candidate count, response-size limit, timeout, and retry budget.
- Retry only throttling/transient server errors; never retry schema or policy failures.
- Respect the documented endpoint rate limit with a conservative delay even when the published ceiling is higher.
- Redact query parameters from errors unless they are explicitly allowlisted non-secret parameters.
- Do not follow redirects.
- Do not overwrite or append to a completed run.
- Do not commit quarantined artifacts.

## Acceptance criteria

1. Captured fixtures and mocked transports test successful keyset pagination, terminal cursors, cursor-loop rejection, schema failure, duplicate handling, raw-body hashing, deterministic ordering, and partial-run behavior.
2. CLI tests prove explicit timestamps, bounds, quarantine-path enforcement, and public-only source selection.
3. A real run against the official endpoint produces a candidate corpus, manifest, and coverage report under `var/corpus-intake`.
4. The run reports its actual candidate and event-family counts. A target of 500 eligible review candidates is useful, but a shortfall is reported rather than filled artificially.
5. The full existing suite remains green and the offline AI package retains its provider/network boundary.

## Future promotion gate

Promotion is a separate future design. At minimum it must require an approved retention-basis record, frozen source artifact hashes, an explicit label ontology, two independent named human reviews per promoted item, adjudication for disagreements, event-family split controls, and a reproducible manifest. Nothing in this acquisition pipeline bypasses that gate.
