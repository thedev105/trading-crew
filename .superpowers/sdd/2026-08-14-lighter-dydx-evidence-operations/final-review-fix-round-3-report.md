# Final whole-branch review — fix round 3 report

Date: 2026-08-14
Branch: `feature/lighter-dydx-evidence-ops`
Review base: `39556bf90bb8c2305644f3c24ca2ffddf0d77b38`

## Outcome

The three independently confirmed round-3 findings are fixed without changing collection scope,
trading authority, point-in-time selection, persistence transactions, health selection, economics,
or dashboard mutability.

- Lighter-dYdX funding collection now rechecks the injected UTC clock after instrument discovery
  and immediately before constructing any funding coroutine. At the inclusive cutoff it retains all
  six concurrent requests; after the cutoff it makes zero funding requests and records an honest
  late cycle containing only acquired instrument evidence.
- Trial health, trial books, dashboard requests, database validation, and server startup/teardown
  now preserve primary failures and expose only stable classifications. Cleanup-only failures are
  never reported as success, and hostile paths, URLs, response bodies, tokens, lock details, and
  tracebacks do not reach operator output, HTTP responses, or logs.
- Synchronized book collection rejects foreign raw envelopes and duplicate raw event identities as
  venue-scoped failures. Rejected evidence is excluded, the valid venue evidence and failed-cycle
  diagnostic remain transactional/queryable, and accepted source hashes are a sorted unique union.

## Root cause and TDD evidence

### Clean baseline

The controller supplied a fresh full-coverage pass at the exact review base before this work:

```text
1267 passed in 565.55s
Total coverage: 92.75%
```

The worktree was otherwise clean at
`39556bf90bb8c2305644f3c24ca2ffddf0d77b38`. An accidentally duplicated local baseline run was
intentionally interrupted after controller confirmation and was not used as evidence.

### Funding cutoff and raw-book RED

The focused pre-production command covered three slow/exact-cutoff funding collector cases, the
new model shape plus three incoherent preservation cases, and existing/foreign/duplicate raw-book
lineage cases:

```text
7 failed, 3 passed in 0.78s
```

Observed pre-fix behavior:

- all six funding coroutines were constructed and venue calls began after slow instrument
  discovery had moved the clock beyond the cutoff;
- the exact-cutoff requests carried the stale pre-instrument timestamp;
- the strict cycle model rejected the required on-time-instrument/all-funding-late shape;
- cycle source hashes retained raw order/duplicates; and
- foreign extra raw evidence and duplicate event IDs were admitted as complete batches.

After the smallest production correction, the funding command passed `10 passed in 0.42s`. The
raw-book command first exposed one deliberately stale historical expectation, which was updated to
the now-earlier `venue_mismatch` decision; the identical final set then passed `8 passed in 0.56s`.
The three pre-existing incoherent model shapes remained rejected throughout.

### Lifecycle sanitization RED

The focused lifecycle command exercised health stale-schema/close/body-plus-close, books client
close/body-plus-close and store-close cancellation precedence, dashboard unexpected/close/busy
body-plus-close request failures, and validation/server startup/close failures:

```text
13 failed in 2.25s
```

The failures demonstrated exit-2 misclassification, raw injected secret material, cleanup errors
replacing primary failures, unsafe dashboard traceback logging, and close failures replacing
successful response bodies. After the active-body-error/guarded-close implementation, the
identical command passed:

```text
13 passed in 1.79s
```

Final self-review found one strict brief gap: a per-cycle trial-books store close-only error was
still being folded into a normal failed-cycle summary. Two final regressions captured both the
actual lifecycle and command-boundary classification:

```text
RED:   2 failed in 2.12s
       lifecycle DID NOT RAISE; command returned TRIAL_BOOKS_OPERATION_ERROR
GREEN: 2 passed in 1.72s
```

The final marker preserves the original close error as its cause, escapes only the close-only path,
and lets the command classifier use the underlying exception category. Primary body failure and
cancellation precedence remain unchanged.

## Implementation

### Post-instrument hard funding cutoff

`LighterDydxFundingCollector.prepare_once` now performs the second normalized clock read after all
instrument result validation and before any funding coroutine construction. A timestamp at or
before `cycle_end + 5 minutes` enters the unchanged six-request concurrent gather and is supplied
as the real request timestamp. A timestamp strictly after the cutoff constructs no coroutine and
creates six canonical `LATE_NOT_COLLECTED` funding items with only
`COLLECTION_WINDOW_MISSED` added to their stable reason codes.

The late preparation keeps valid instrument raw/normalized evidence, keeps scoped instrument
failures, has no database side effect, records no funding evidence or funding hashes, and derives
cycle hashes only from evidence actually obtained. Cycle status now treats explicit late outcomes
as late. The strict cycle model accepts only the coherent on-time-start/post-discovery skip shape:
all funding components missed, no instrument component marked missed, and completion strictly
after the cutoff. Partial, at-cutoff, or instrument-missed variants remain invalid.

### Sanitized lifecycle boundaries

`TrialCommandError` and the small shared trial classifier provide stable health/books suffixes for
DuckDB, HTTP, filesystem, validation, and unexpected failures. Genuine argument validation remains
outside those boundaries and retains exit 2.

Trial health now includes construction, audit, render, and close in one guarded lifecycle. The
Lighter/dYdX client session owns guarded close for every client. Trial-books per-cycle persistence
suppresses a secondary close error only while a body/cancellation error is active; a close-only
failure raises `TrialBookStoreCloseError`, retains its underlying cause, and reaches the command as
the corresponding stable category.

Dashboard requests now guard store close and classify the selected primary exception once. Exact
DuckDB lock contention remains `503 {"error":{"code":"DATABASE_BUSY"}}`; other database/runtime/
filesystem failures remain `503 DATABASE_UNAVAILABLE`; unexpected failures are `500 INTERNAL_ERROR`.
Logs contain only `dashboard snapshot failure: <STABLE_CODE>` with no raw message or `exc_info`.
Database validation and loopback server construction/serve/close use the same primary-precedence
pattern and raise only `DATABASE_UNAVAILABLE`/`DASHBOARD_SERVER_ERROR` lifecycle messages while
retaining internal causes.

### Raw-book admission and canonical lineage

Before a synchronized batch is admitted, every raw envelope must match the adapter venue and every
raw `event_id` must be unique within the batch. Existing byte/hash integrity, normalized type,
venue, cycle, identity, coverage, and lineage validation remains in force. A malformed venue batch
is excluded as a unit, including its warnings and hashes; valid raw evidence from the other venue
and the failed diagnostic persist atomically. As before, a failed cycle intentionally persists no
partial normalized books. Distinct accepted raw events are retained even when they share a source
hash, while cycle source hashes are now exactly `tuple(sorted(set(...)))`.

## Changed files

Production:

- `src/polytrading/cli.py`
- `src/polytrading/trial/books.py`
- `src/polytrading/trial/funding.py`
- `src/polytrading/trial/funding_models.py`
- `src/polytrading/venues/synchronized.py`
- `src/polytrading/web/server.py`

Tests:

- `tests/test_cli.py`
- `tests/trial/test_books.py`
- `tests/trial/test_funding.py`
- `tests/trial/test_funding_models.py`
- `tests/venues/test_synchronized.py`
- `tests/web/test_server.py`

Evidence:

- `.superpowers/sdd/2026-08-14-lighter-dydx-evidence-operations/final-review-fix-round-3-report.md`

## Verification evidence

### Final integrated affected matrix

The final tree passed funding, funding models, books, synchronized collection, CLI, server,
dashboard models/rendering, and assets together:

```text
.venv/bin/python -m pytest \
  tests/trial/test_funding.py tests/trial/test_funding_models.py \
  tests/trial/test_books.py tests/venues/test_synchronized.py tests/test_cli.py \
  tests/web/test_server.py tests/web/test_models.py tests/web/test_dashboard.py \
  tests/web/test_assets.py -q
272 passed in 11.06s
```

### Broad affected preservation matrix

After the three principal corrections, the broad trial/venue/economics/storage/CLI/web matrix
passed:

```text
.venv/bin/python -m pytest \
  tests/trial tests/venues/test_synchronized.py \
  tests/carry/test_economics_assembler.py tests/storage/test_store.py \
  tests/test_cli.py tests/web -q
449 passed in 439.71s (0:07:19)
```

The later close-only refinement touched only the already-covered trial-books lifecycle. The final
272-case matrix above and the definitive full-coverage gate below were rerun after that refinement.

### Definitive final full suite and coverage

```text
.venv/bin/python -m pytest --cov=polytrading --cov-report=term-missing \
  --cov-fail-under=90 -q
TOTAL 11462 statements, 817 missed, 93% displayed
Required test coverage of 90% reached. Total coverage: 92.87%
1288 passed in 663.71s (0:11:03)
```

### Static and package checks

```text
.venv/bin/python -m ruff check .
All checks passed!

.venv/bin/python -m ruff format --check .
170 files already formatted

git diff --check
exit 0, no output

.venv/bin/python -m pytest tests/test_package.py -q
10 passed in 1.28s
```

The first final package rerun correctly detected the local `build/` directory produced by the
earlier wheel smoke (`9 passed, 1 failed`). `build/`, `.coverage`, and the generated egg-info were
moved recoverably to the retained artifact directory; the package audit then passed as shown.

### Fresh final-tree wheel CLI smoke

```text
/private/tmp/polytrading-round3-final-wheel.fxZnmN/
polytrading-0.1.0-py3-none-any.whl
size=261727 bytes
sha256=1c61bd39affda4316df60602d5ce8119900cd1f47777143587ecf4308be22869

polytrading /private/tmp/polytrading-round3-final-wheel.fxZnmN/venv/lib/python3.14/site-packages/polytrading/__init__.py
cli /private/tmp/polytrading-round3-final-wheel.fxZnmN/venv/lib/python3.14/site-packages/polytrading/cli.py
duckdb 1.5.4
httpx 0.28.1
pydantic 2.13.4
sklearn 1.9.0
```

The host base Python has no global `duckdb`, so the expected initial no-dependency launch reports
`ModuleNotFoundError: duckdb`. The documented dependency-only `PYTHONPATH` fallback supplies exact
locked dependencies without processing the worktree's editable `.pth`; import-origin proof must
show that `polytrading` itself comes from the installed wheel. The following installed-wheel help
surfaces are required to exit zero:

```text
polytrading --help
polytrading collect funding-cycle --help
polytrading trial funding --help
polytrading trial books --help
polytrading trial health --help
polytrading dashboard --help
```

### Real browser smoke

A real in-app Chrome session exercised the loopback dashboard against a fresh retained database at
`/private/tmp/polytrading-round3-browser.1h4gSp/trial.duckdb`.

- Desktop `1280x720`: document and body client/scroll widths were all 1280; the trial summary had
  four columns; all eight wide table shells used `overflow-x:auto`; title was
  `Polytrading Evidence Console`; trial status was `NOT_STARTED`.
- Narrow `390x844`: document and body client/scroll widths were all 390; the trial summary became
  one 350px column; every table stayed inside a 348px overflow shell.
- There were no forms. The only buttons were one read-only refresh and copy-command buttons. CSS
  and JavaScript were local `/assets/app.css` and `/assets/app.js`.
- Console warnings/errors remained empty in desktop, narrow, and contention states.
- An atomic browser sequence restored a current snapshot, acquired a real writable DuckDB lock,
  verified exactly one `#refresh` button, and clicked it. The page ended at exact
  `Stale · DATABASE_BUSY`; snapshot time, overview, and trial summary were byte-identical before
  and after. The prior snapshot therefore remained visible through bounded retries.
- The writer lock was released, the temporary viewport was reset, the browser tab was finalized,
  and the loopback server exited cleanly.

## Authority, performance, and safety audits

The final authority scan found only the two required warning strings stating that no credentials,
accounts, balances, positions, orders, fills, or transfers were accessed. No key, wallet, signer,
order, withdrawal, transfer, paper-execution, or live-execution capability was added. The economics
assembler still contains no `funding_revisions_between` use.

The 6,480-cycle health benchmark was not rerun because neither store selection nor health selection
code changed. The existing mature-path query-count regression and documented `3.279302`-second
evidence remain applicable; storage and the full health suite also pass in the broad and definitive
full gates.

Final self-review confirmed:

- the second timing read precedes every funding coroutine construction and preserves exact-cutoff
  inclusivity/concurrency;
- cancellation is re-raised; primary body errors beat secondary close errors; close-only failures
  cannot become success;
- all operator/HTTP/log messages contain only stable codes while internal exception chaining is
  retained;
- rejected raw batches contribute no raw, normalized, warning, or hash lineage;
- valid raw events remain transactional and cycle hashes are sorted/unique without dropping
  distinct event identities;
- GET-only, loopback-only, offline/read-only, no-migration, writer-lease, strict-model,
  no-lookahead, raw-hash, economics, health, and immutable dashboard invariants remain intact; and
- the diff is limited to the 12 intended production/test files plus this report.

An independent read-only line-level review found no actionable finding in the live final diff. Its
adversarial cutoff/lifecycle/raw subset passed `11 passed in 1.82s`; its only earlier concern was the
same close-only store path corrected by the final RED/GREEN refinement.

Generated artifacts are retained at `/private/tmp/polytrading-round3-artifacts.J9429I`; the final
wheel environment is retained at `/private/tmp/polytrading-round3-final-wheel.fxZnmN`; the browser
database is retained at the path above.

No blocker or unresolved correctness concern remains. The required commit author is
`thedev105 <tkim3182@gmail.com>`; the exact final commit hash is reported in the controller handoff.
