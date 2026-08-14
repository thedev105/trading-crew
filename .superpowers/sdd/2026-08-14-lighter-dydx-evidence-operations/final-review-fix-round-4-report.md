# Final whole-branch review — fix round 4 report

Date: 2026-08-14
Branch: `feature/lighter-dydx-evidence-ops`
Review base: `c646e634db872f757e4b508eab2ddc9219de8e51`

## Outcome

The three independently confirmed round-4 findings are fixed without changing adapter network
scope, UTC/cutoff/no-lookahead rules, funding or book transaction semantics, health/economics
selection, dashboard mutability, or trading authority.

- Trial-funding stores and the shared writer lease now preserve the exact active body exception or
  cancellation through cleanup. Every owned lease cleanup phase is attempted; cleanup-only
  failures retain the stable first cause and cross funding/books command boundaries as sanitized
  exit-1 codes.
- Synchronized collection rejects every non-exact `AdapterBatch` return as the venue-scoped
  `invalid_adapter_batch` diagnostic before dereferencing it. Valid peer raw evidence and the
  failed cycle remain transactional and queryable; malformed evidence and warnings are excluded.
- Raw event UUIDs are unique across the complete accepted cycle. Canonical venue order assigns
  ownership, later colliding batches are rejected atomically as `duplicate_raw_identity`, and
  later valid batches continue to be processed. Equal hashes with distinct UUIDs remain valid.

## Root cause and TDD evidence

### Clean baseline

Work began from the exact clean round-3 commit
`c646e634db872f757e4b508eab2ddc9219de8e51`. Its definitive evidence was:

```text
1288 passed in 663.71s (0:11:03)
Total coverage: 92.87%
```

### Initial RED

Production remained untouched while the focused lifecycle and synchronized regressions were
added.

The lifecycle matrix produced:

```text
12 failed, 1 passed in 2.28s
```

The sole pre-fix pass was the already-safe direct OSError close-only funding classification. The
failures proved that store/lease cleanup replaced exact body errors or cancellation, a later close
replaced the first unlock error, lease cleanup was folded into a normal books-cycle failure, and
unexpected funding cleanup text could escape the command boundary.

The synchronized matrix produced three failures and one preservation pass. `object()` raised an
`AttributeError`, a structurally valid `SimpleNamespace` was incorrectly duck-accepted, and a
cross-venue duplicate UUID prepared as complete before real persistence conflict. The
same-hash/distinct-ID case passed before the correction, protecting that invariant.

The combined selected run recorded:

```text
14 failed, 1 passed in 2.20s
```

After the smallest initial production correction, the four complete affected files passed:

```text
186 passed in 6.34s
```

### Review-found BaseException cleanup edge

An independent read-only review reproduced one Important edge: owned cleanup guards still caught
only `Exception`. An unlock-raised `CancelledError` could therefore replace a primary and prevent
lock-file close, while cleanup-only cancellation bypassed stable command classification.

The exact follow-up RED/GREEN matrix was:

```text
RED:   6 failed, 10 passed in 1.91s
GREEN: 16 passed in 1.55s
```

Owned cleanup guards now handle `BaseException`, without broadening command bodies to catch active
task cancellation. The follow-up review closed the Important issue and found no remaining Critical
or Important issue.

## Implementation

### Primary-preserving cleanup and classification

`database_writer_lease` records any active acquisition/body `BaseException` and re-raises it with
bare `raise`. Cleanup independently attempts unlock and close, captures the first cleanup
`BaseException`, and never lets a later cleanup replace it. When a primary exists, cleanup failures
are suppressed; when cleanup alone fails, `WriterLeaseCleanupError` carries the fixed message
`DATABASE_WRITER_LEASE_CLEANUP_ERROR` and retains the first raw cause internally.

Trial-funding persistence uses the same active-error pattern. A cleanup-only failure is wrapped in
`TrialFundingStoreCloseError`, allowing even a cleanup-raised `BaseException` to reach the command
as an ordinary stable operational error while retaining its cause. Trial-books store cleanup uses
its established `TrialBookStoreCloseError` marker for the same BaseException edge, and shared lease
cleanup markers escape the per-cycle retry accounting rather than being converted into a false
summary.

The funding/books classifiers unwrap only the known cleanup markers for category selection. Thus
DuckDB, HTTP, filesystem, validation, and fallback operation categories remain stable, while raw
paths, URLs, response bodies, tokens, and lock details never reach stderr. Trial-funding's existing
operational boundary now sanitizes any ordinary `Exception`; timestamp parsing and timing
validation remain outside it, preserving genuine usage exit 2. Active cancellation remains a
`BaseException` and is not consumed by command handlers.

### Nominal adapter-batch admission

After gather cancellation/exception handling and before any attribute access,
`SynchronizedBookCollector.prepare_once` requires `type(result) is AdapterBatch`. A protocol
violation contributes only `<venue>:invalid_adapter_batch` and collection continues. The rejected
value contributes no raw record, normalized book, warning, or hash. The valid peer remains in the
prepared evidence; failed-cycle persistence writes its raw evidence plus the cycle diagnostic and
intentionally writes no partial normalized books.

### Cycle-wide raw identity ownership

The collector maintains an accepted raw-event UUID set in the existing canonical adapter order.
Batch-local shape and evidence-integrity validation run first, so an invalid batch cannot reserve
an identity. A fully valid later batch that intersects the accepted set is rejected as a unit with
`<venue>:duplicate_raw_identity`; subsequent adapters still run. Only accepted batches update the
set and contribute raw/books/warnings/hashes.

Tests use three venues to prove first-owner semantics, later-collider rejection, continued
processing, reversed-input invariance, warning/hash exclusion, and real queryable persistence.
Separate real persistence preserves two distinct UUIDs with an identical exact payload/hash;
cycle hashes remain the sorted unique union.

## Changed files

Production:

- `src/polytrading/cli.py`
- `src/polytrading/trial/books.py`
- `src/polytrading/trial/writer_lease.py`
- `src/polytrading/venues/synchronized.py`

Tests:

- `tests/test_cli.py`
- `tests/trial/test_books.py`
- `tests/trial/test_writer_lease.py`
- `tests/venues/test_synchronized.py`

Evidence:

- `.superpowers/sdd/2026-08-14-lighter-dydx-evidence-operations/final-review-fix-round-4-report.md`

## Verification evidence

### Complete changed-file matrix

```text
.venv/bin/python -m pytest -q \
  tests/trial/test_writer_lease.py tests/trial/test_books.py \
  tests/venues/test_synchronized.py tests/test_cli.py
191 passed in 7.17s
```

### Final integrated affected matrix

```text
.venv/bin/python -m pytest \
  tests/trial/test_funding.py tests/trial/test_funding_models.py \
  tests/trial/test_books.py tests/trial/test_writer_lease.py \
  tests/venues/test_synchronized.py tests/test_cli.py \
  tests/web/test_server.py tests/web/test_models.py \
  tests/web/test_dashboard.py tests/web/test_assets.py -q
311 passed in 10.16s
```

### Final broad preservation matrix

```text
.venv/bin/python -m pytest \
  tests/trial tests/venues/test_synchronized.py \
  tests/carry/test_economics_assembler.py tests/storage/test_store.py \
  tests/test_cli.py tests/web -q
473 passed in 405.98s (0:06:45)
```

### Definitive full suite and coverage

```text
.venv/bin/python -m pytest --cov=polytrading --cov-report=term-missing \
  --cov-fail-under=90 -q
TOTAL 11505 statements, 813 missed, 93% displayed
Required test coverage of 90% reached. Total coverage: 92.93%
1310 passed in 552.94s (0:09:12)
```

### Static and package gates

```text
.venv/bin/python -m ruff check .
All checks passed!

.venv/bin/python -m ruff format --check .
170 files already formatted

git diff --check
exit 0, no output

.venv/bin/python -m pytest tests/test_package.py -q
10 passed in 1.65s
```

### Fresh installed-wheel smoke

The final working tree was copied without `.git`, `.venv`, caches, coverage, build, or egg-info
artifacts and built under:

```text
/private/tmp/polytrading-round4-final-wheel.afXrnd
polytrading-0.1.0-py3-none-any.whl
size=262138 bytes
sha256=00005266d1dd8bb29e23593902ab95c3b1a377c1198fdde662c1f58486828416
```

The wheel installed with no dependencies into its own fresh venv. The documented dependency-only
`PYTHONPATH` fallback supplied the exact locked project dependencies without processing the
worktree editable `.pth`. Import-origin proof was:

```text
polytrading /private/tmp/polytrading-round4-final-wheel.afXrnd/venv/lib/python3.14/site-packages/polytrading/__init__.py
cli /private/tmp/polytrading-round4-final-wheel.afXrnd/venv/lib/python3.14/site-packages/polytrading/cli.py
duckdb 1.5.4
httpx 0.28.1
pydantic 2.13.4
sklearn 1.9.0
worktree_src_on_sys_path False
```

All installed-wheel help commands exited zero:

```text
polytrading --help
polytrading collect funding-cycle --help
polytrading trial funding --help
polytrading trial books --help
polytrading trial health --help
polytrading dashboard --help
```

## Preservation, authority, and review audits

No storage or health production/test path changed. The mature 6,480-cycle benchmark was therefore
not rerun; its unchanged query-count regression and documented `3.279302`-second evidence remain
applicable and the full trial/storage tests pass above.

No web production or test file changed. Browser model/server/dashboard/assets tests passed in the
final integrated and broad matrices. The round-3 manual desktop/narrow/lock-contention browser
smoke remains applicable under the brief's explicit no-web-change allowance.

The final authority scan found only the two required warning strings stating that no credentials,
accounts, balances, positions, orders, fills, or transfers were accessed. Added production lines
contain no key, wallet, signer, order, withdrawal, transfer, paper-execution, or live-execution
capability. `funding_revisions_between` remains absent from the economics assembler. No adapter,
client, endpoint, request, socket, or websocket production path changed; the only broad network
keyword addition is the existing `httpx.HTTPError` classification branch.

Final self-review and independent review confirmed:

- exact body/cancellation identity survives store, unlock, and lock-file-close failures;
- unlock and close are both attempted, and the first cleanup cause is stable;
- cleanup-only funding/books failures are exit 1 with fixed sanitized codes;
- malformed return values and colliding batches contribute no rejected lineage;
- canonical first-owner and reversed-input behavior are deterministic;
- equal hashes do not collapse distinct raw identities;
- failed cycles remain transactional/queryable with no partial normalized books; and
- no network, authority, cutoff, health/economics, dashboard, or schema invariant changed.

The generated final coverage file was moved into the retained wheel artifact directory; no build,
egg-info, coverage, or cache artifact remains in the worktree. No blocker or unresolved correctness
concern remains. The required commit author is `thedev105 <tkim3182@gmail.com>`; the exact final
commit hash is reported in the controller handoff.
