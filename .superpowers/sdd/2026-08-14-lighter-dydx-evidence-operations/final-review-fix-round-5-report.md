# Final whole-branch review — fix round 5 report

Date: 2026-08-14
Branch: `feature/lighter-dydx-evidence-ops`
Review base: `599850028e9983394534424e86860a255d5d3e23`

## Outcome

The independently confirmed cleanup-`BaseException` finding is fixed systematically across every
binding CLI/web lifecycle in the round-5 brief. A shared sync/async cleanup abstraction now
preserves the exact active acquisition/body exception or cancellation, attempts every registered
cleanup, keeps the deterministic first cleanup cause, and converts cleanup-only `BaseException`
failures into a known ordinary marker. Command and dashboard classifiers unwrap only that marker
hierarchy and continue to expose fixed sanitized outcomes.

Existing successful behavior, ordinary cleanup classifications, `DATABASE_BUSY`, primary identity
and chaining, all-clients-attempted behavior, GET-only/read-only dashboard semantics, and prior
round-2/3/4 cleanup regressions remain intact. No request scope, timestamp/cutoff/no-lookahead rule,
evidence transaction, selector, health/economics rule, UI asset, schema, credential use, or trading
authority changed.

## Root cause and TDD evidence

### Clean baseline

Work began from the exact clean round-4 commit
`599850028e9983394534424e86860a255d5d3e23`.

### Initial RED

The cleanup contract and every binding surface were captured before production edits. The new
helper contract initially failed collection with:

```text
ModuleNotFoundError: No module named 'polytrading.lifecycle'
```

The focused CLI lifecycle run then exposed six assertion failures plus the expected raw
`KeyboardInterrupt` that terminated pytest. Generic public-client, Lighter/dYdX-client,
funding-cycle-store, and trial-health-store cleanup could replace an exact active cancellation or
escape the ordinary command boundary. The focused web run recorded:

```text
4 failed, 2 passed
```

Dashboard request, validation, and server cleanup cancellation could escape or replace the active
primary. These failures also demonstrated that later cleanup could prevent remaining cleanup and
that cleanup-only `CancelledError`/`KeyboardInterrupt` bypassed stable classification.

No production file was changed until those RED results were recorded.

### Green progression and regression retention

After the shared implementation, the complete new listed-surface matrix passed:

```text
18 passed
```

An independent review noticed that five pre-existing OSError regressions had accidentally been
converted instead of retained. They were restored verbatim and five separate BaseException
precedence cases were added. The corrected old/new matrix and affected groups passed:

```text
11 passed in 1.42s
234 passed in 7.37s
```

The independent final reviewer ran a fresh affected matrix:

```text
264 passed in 7.77s
```

and reported no Critical or Important finding.

## Implementation

### Shared primary-preserving cleanup

`src/polytrading/lifecycle.py` supplies `CleanupRegistry`, `AsyncCleanupRegistry`,
`owned_resource_cleanup`, and `async_owned_resource_cleanup` with one contract:

- callbacks are registered immediately when ownership is acquired;
- active acquisition/body `BaseException` is re-raised as the exact same object;
- every registered cleanup callback is attempted even when another raises;
- the first cleanup cause is selected in deterministic registration order;
- cleanup errors are suppressed only while an active primary exists; and
- cleanup-only `CancelledError`, `KeyboardInterrupt`, or another `BaseException` becomes the
  ordinary fixed `OwnedResourceCleanupError("OWNED_RESOURCE_CLEANUP_ERROR")`, chained from the
  first raw cause.

The async registry starts all registered cleanups and awaits them all. Result selection remains in
registration order even when completion order differs. `cleanup_error_cause` unwraps only the
known marker hierarchy; unrelated exceptions with causes are not reclassified.

### CLI lifecycles and classifications

Generic public-adapter sessions register each client immediately after construction, so partial
acquisition is cleaned and all constructed clients are attempted. The Lighter/dYdX session uses
the same async abstraction. Funding-cycle and trial-health stores use the sync abstraction.

The already-protected trial-funding store, trial-books store, and writer lease were migrated to the
same implementation so prior invariants cannot diverge. Their specialized fixed markers now
subclass the shared marker. Writer-lease unlock and file-handle close are both attempted in the
existing order, with the first cause retained.

Funding and trial classifiers inspect a raw cause only through `cleanup_error_cause`. Thus known
cleanup causes retain established DuckDB/HTTP/filesystem/validation/fallback codes, while active
task cancellation still bypasses ordinary `except Exception` command bodies. The generic public
boundary prints only `OWNED_RESOURCE_CLEANUP_ERROR`; no raw exception text enters stderr.

### Dashboard lifecycles and classifications

Dashboard request stores, database-validation stores, and server construction/serve/`server_close`
all use the shared sync abstraction. A request cleanup marker is mapped through the established
classifier, including `DATABASE_BUSY` for DuckDB lock IOExceptions and `INTERNAL_ERROR` for
cleanup cancellation/keyboard interruption. Validation and server boundaries chain their fixed
`DashboardLifecycleError` from the internal raw cleanup cause without exposing that cause.

Successful construction has no post-acquisition validation body: the only owned operation after
construction is cleanup, while constructor failure owns no store. The shared helper still covers
active-acquisition semantics. `HTTPServer.shutdown()` was intentionally not added because this
single-threaded lifecycle calls `serve_forever()` on the same thread; `server_close` remains the
owned cleanup phase and avoids a same-thread shutdown deadlock.

## Cleanup-site audit

Every `close`, `aclose`, unlock, and server cleanup in the changed lifecycle region was inspected.

| Region | Disposition |
| --- | --- |
| `public_adapter_session` client `aclose` | Binding surface; shared async helper, immediate per-client registration, all attempted. |
| `_lighter_dydx_adapter_session` client `aclose` | Binding surface; shared async helper, deterministic first cause, both attempted. |
| `_collect_funding_cycle` store `close` | Binding surface; shared sync helper. |
| `_trial_health` store `close` | Binding surface; shared sync helper. |
| Dashboard request/validation store `close` | Binding surfaces; shared sync helper with sanitized HTTP/lifecycle classification. |
| Dashboard `server_close` | Binding surface; shared sync helper; exact active cancellation preserved. |
| Trial-funding/trial-books store `close` | Prior round invariants; consolidated onto the shared helper with specialized markers retained. |
| Writer-lease unlock and handle `close` | Prior round invariant; consolidated onto the shared helper, both phases attempted. |
| Retry-response and retry-transport `aclose` | Protocol-level retry wrapper, not a listed owned command/session boundary; unchanged. |
| Replay, carry audit/study/economics, fee import, outer collect-public/books, legacy funding-health stores | Audited, pre-existing lifecycles outside the one independently confirmed round-5 finding; intentionally unchanged to avoid expanding the final fix or altering unrelated command semantics. |

## Changed files

Production:

- `src/polytrading/lifecycle.py`
- `src/polytrading/cli.py`
- `src/polytrading/trial/books.py`
- `src/polytrading/trial/writer_lease.py`
- `src/polytrading/web/server.py`

Tests:

- `tests/test_lifecycle.py`
- `tests/test_cli.py`
- `tests/web/test_server.py`

Evidence:

- `.superpowers/sdd/2026-08-14-lighter-dydx-evidence-operations/final-review-fix-round-5-report.md`

## Verification evidence

### Complete lifecycle/CLI/server/writer/books/funding matrix

```text
395 passed in 13.08s
```

The final lifecycle/CLI/server focused recheck after the report was written passed:

```text
196 passed in 7.25s
```

### Final integrated affected matrix

```text
340 passed in 10.73s
```

### Final corrected-tree broad preservation matrix

```text
.venv/bin/python -m pytest -q \
  tests/test_lifecycle.py tests/trial tests/venues/test_synchronized.py \
  tests/carry/test_economics_assembler.py tests/storage/test_store.py \
  tests/test_cli.py tests/web
507 passed in 414.39s (0:06:54)
```

### Definitive full suite and coverage

```text
.venv/bin/python -m pytest --cov=polytrading --cov-report=term-missing \
  --cov-fail-under=90 -q
TOTAL 11474 statements, 813 missed, 93% displayed
Required test coverage of 90% reached. Total coverage: 92.91%
1344 passed in 548.31s (0:09:08)
src/polytrading/lifecycle.py: 68 statements, 0 missed, 100%
```

### Static, formatting, diff, and package/authority gates

```text
.venv/bin/python -m ruff check .
All checks passed!

.venv/bin/python -m ruff format --check .
172 files already formatted

git diff --check
exit 0, no output

.venv/bin/python -m pytest tests/test_package.py -q
10 passed in 2.30s
```

### Fresh installed-wheel smoke

The final tree was copied without `.git`, `.venv`, caches, coverage, build, dist, or egg-info
artifacts and built under:

```text
/private/tmp/polytrading-round5-final-wheel.AAp2kQ
polytrading-0.1.0-py3-none-any.whl
size=262914 bytes
sha256=2b010f928a60f065c2770429e9298d284de1fe993345012e934ce6407435cdae
```

The wheel installed with no dependencies into a fresh venv. The dependency-only `PYTHONPATH`
fallback supplied the exact locked project dependencies while excluding the worktree editable
source. Import-origin proof was:

```text
polytrading /private/tmp/polytrading-round5-final-wheel.AAp2kQ/venv/lib/python3.14/site-packages/polytrading/__init__.py
cli /private/tmp/polytrading-round5-final-wheel.AAp2kQ/venv/lib/python3.14/site-packages/polytrading/cli.py
duckdb 1.5.4
httpx 0.28.1
pydantic 2.13.4
sklearn 1.9.0
worktree_src_on_sys_path False
```

The installed wheel contains `cli.py`, `lifecycle.py`, and `web/server.py`. All help commands
exited zero:

```text
polytrading --help
polytrading collect funding-cycle --help
polytrading trial funding --help
polytrading trial books --help
polytrading trial health --help
polytrading dashboard --help
```

### Real browser and server lifecycle smoke

An installed local dashboard was exercised at `127.0.0.1:18787` against a real DuckDB file.

- Desktop `1728x898`: title, database, snapshot status, main region, and refresh control rendered;
  `overflowX=false`.
- Narrow `390x844`: heading, main region, refresh control, and status remained visible;
  `overflowX=false`.
- With a real read/write DuckDB lock held, refresh retained the prior snapshot and displayed
  `Stale · DATABASE_BUSY`; server logs contained only the fixed `DATABASE_BUSY` code.
- After releasing the lock, refresh returned to `Current · refreshed 20:05 UTC` with no horizontal
  overflow.
- Browser warning/error log was empty. The viewport was reset to `1728x898` and the created tab was
  finalized.
- Ctrl-C stopped the dashboard with exit 0. A subsequent loopback bind printed `PORT_RELEASED`,
  proving `server_close` released port 18787.

## Preservation, performance, authority, and review audits

Store/health selection and mature query logic were untouched. The unchanged 6,480-cycle
query-count regression and documented `3.279302`-second mature-performance evidence therefore
remain applicable; trial/storage preservation tests pass in the matrices above.

No adapter, endpoint, request, socket, websocket, evidence schema, persistence transaction,
timestamp cutoff, selector, UI asset, or economics production path changed. Added production code
contains no credential, account, balance, position, order, fill, transfer, signer, withdrawal,
paper-execution, or live-execution capability. Dashboard access remains loopback-only, GET-only,
and read-only.

The independent final review confirmed exact primary identity, all cleanup attempted, deterministic
first causes, known cleanup-only markers, narrow classifier unwrapping, preserved `DATABASE_BUSY`,
restored prior regressions, sanitized command/HTTP/log output, and the documented scope audit. It
reported no Critical or Important finding.

The generated final coverage file was moved into the retained wheel artifact directory; no build,
egg-info, coverage, or cache artifact remains in the worktree. No blocker or unresolved correctness
concern remains. The required commit author is `thedev105 <tkim3182@gmail.com>`; the exact final
commit hash is reported in the controller handoff.

## Corrective addendum — outer generic collection stores

Date: 2026-08-14
Correction base: `1f56a95443cbdc5abb9a8f88658150d3cc7ce4db`

### Corrected audit conclusion

A targeted follow-up review found that the original audit table was too narrow when it classified
the outer collect-public/books stores as unrelated pre-existing lifecycles. Those stores directly
surrounded the named generic `public_adapter_session()` lifecycle. Their raw `finally:
store.close()` blocks could therefore replace the exact active body/cancellation or the inner
client cleanup marker after the shared async helper had already selected the correct outcome.

This addendum preserves the original audit trail and supersedes only that one scope decision. The
end-to-end ownership correction is implemented in a separate commit from the original round-5
fix.

### Corrective RED

Production remained at the exact clean correction base while twelve parameterized end-to-end
cases were added for both `_collect_public` and `_collect_books`:

- exact body `CancelledError`, client cleanup `CancelledError`, and store cleanup
  `KeyboardInterrupt`;
- successful body, client cleanup `CancelledError`, and store cleanup `KeyboardInterrupt`;
- ordinary body failure plus client/store `BaseException` cleanup;
- store-only `CancelledError` and `KeyboardInterrupt`; and
- the ordinary store-only OSError regression.

The pre-correction run recorded:

```text
.venv/bin/python -m pytest -q tests/test_cli.py \
  -k 'generic_collection_exact_body_cancellation_wins_over_all_cleanup or \
      generic_collection_client_cleanup_marker_wins_over_store_cleanup or \
      generic_collection_body_failure_stays_primary_and_operator_stable or \
      generic_collection_store_cleanup_only_uses_fixed_sanitized_marker'
12 failed, 146 deselected in 2.29s
```

The failures reproduced every reported mode. A later store `KeyboardInterrupt` replaced the exact
body cancellation and the inner client marker; client and store cleanup still ran in order. Store
cleanup `CancelledError`/`KeyboardInterrupt` escaped `main()`. Store-only OSError exposed the
hostile injected detail and returned exit 2 instead of the shared-marker exit 1.

### Corrective implementation

Only the two mandated production functions changed. `_collect_public` and `_collect_books` now
construct their caller-owned `DuckDBStore` inside `owned_resource_cleanup`, register `store.close`
immediately, and keep the complete nested `public_adapter_session()` plus collection body inside
that outer ownership context.

Lexical unwinding is deliberately nested rather than combining stores and clients in one registry:

1. the inner async session attempts every client cleanup and selects its first cause;
2. the outer sync lifecycle then attempts store cleanup;
3. an exact body primary or inner cleanup marker remains the active primary and suppresses the
   later store failure; and
4. store-only failure becomes `OwnedResourceCleanupError("OWNED_RESOURCE_CLEANUP_ERROR")`.

Success output remains after the outer context, so failed cleanup cannot print a false success.
No command body catches `BaseException`; genuine task cancellation still escapes unchanged.

### Complete `public_adapter_session()` call-site audit

The correction audit found three production call sites and three direct pre-existing test call
sites.

- `_collect_public`: caller-owned store now follows the shared outer ownership contract.
- `_collect_funding_cycle`: already constructed and immediately registered its store with
  `owned_resource_cleanup`; no correction was needed.
- `_collect_books`: caller-owned store now follows the shared outer ownership contract.
- The route-selection unit test uses a real test-fixture store and manually closes it after its
  intentionally successful session. It has no command/operator boundary and is not a production
  owner.
- The direct active-cancellation and cleanup-only session tests pass inert `object()` stores; they
  own no surrounding closeable resource.

No other production caller exists. The correction therefore changes no directly or transitively
unrelated lifecycle.

### Corrective GREEN and final-tree evidence

Focused correction matrix:

```text
12 passed, 146 deselected in 1.44s
```

Complete CLI/lifecycle matrix:

```text
.venv/bin/python -m pytest -q tests/test_lifecycle.py tests/test_cli.py
167 passed in 6.18s
```

Integrated lifecycle/CLI/server/trial/funding/synchronization matrix:

```text
.venv/bin/python -m pytest -q \
  tests/test_lifecycle.py tests/test_cli.py tests/web/test_server.py \
  tests/trial/test_books.py tests/trial/test_writer_lease.py \
  tests/venues/test_funding_cycle.py tests/venues/test_synchronized.py
287 passed in 13.06s
```

Corrected-tree broad preservation matrix:

```text
.venv/bin/python -m pytest -q \
  tests/test_lifecycle.py tests/trial tests/venues/test_synchronized.py \
  tests/carry/test_economics_assembler.py tests/storage/test_store.py \
  tests/test_cli.py tests/web
519 passed in 461.15s (0:07:41)
```

Definitive corrected-tree full suite and coverage:

```text
.venv/bin/python -m pytest --cov=polytrading --cov-report=term-missing \
  --cov-fail-under=90 -q
TOTAL 11474 statements, 813 missed, 93% displayed
Required test coverage of 90% reached. Total coverage: 92.91%
1356 passed in 591.23s (0:09:51)
src/polytrading/lifecycle.py: 68 statements, 0 missed, 100%
```

Browser/server regression suite:

```text
.venv/bin/python -m pytest -q tests/web
72 passed in 4.02s
```

Web production code and assets did not change. The original round-5 real-browser desktop, narrow,
real `DATABASE_BUSY`, recovery, zero-console-error, Ctrl-C, and `PORT_RELEASED` smoke above remains
applicable under the corrective brief's explicit citation allowance.

Final static, formatting, diff, and package/authority gates:

```text
.venv/bin/python -m ruff check .
All checks passed!

.venv/bin/python -m ruff format --check .
172 files already formatted

git diff --check
exit 0, no output

.venv/bin/python -m pytest -q tests/test_package.py
10 passed in 2.44s
```

### Corrected-tree installed-wheel smoke

The source-frozen corrected tree was copied without `.git`, `.venv`, caches, coverage, build,
dist, or egg-info artifacts and built under:

```text
/private/tmp/polytrading-round5-correction-wheel.46zWxj
polytrading-0.1.0-py3-none-any.whl
size=262911 bytes
sha256=4a59d93ba00ee038044fb965c9e97fe76a2fd870095282aeb6515f189808afa6
```

The wheel installed with no dependencies into a fresh venv. Dependency-only `PYTHONPATH`
supplied the locked dependencies without processing the worktree editable `.pth`. Import-origin
proof was:

```text
polytrading /private/tmp/polytrading-round5-correction-wheel.46zWxj/venv/lib/python3.14/site-packages/polytrading/__init__.py
cli /private/tmp/polytrading-round5-correction-wheel.46zWxj/venv/lib/python3.14/site-packages/polytrading/cli.py
duckdb 1.5.4
httpx 0.28.1
pydantic 2.13.4
sklearn 1.9.0
worktree_src_on_sys_path False
```

The wheel contains `cli.py`, `lifecycle.py`, and `web/server.py`. Every required installed command
exited zero:

```text
polytrading --help
polytrading collect funding-cycle --help
polytrading trial funding --help
polytrading trial books --help
polytrading trial health --help
polytrading dashboard --help
```

### Corrective review and preserved boundaries

The independent corrective review ran a fresh focused matrix (`13 passed, 145 deselected in
1.77s`) and complete CLI/lifecycle matrix (`167 passed in 6.09s`), plus Ruff, formatting, and diff
checks. It found no Critical, Important, or Minor issue and declared the correction ready.

No network/adapters/assets/range behavior, client construction, UTC/cutoff/no-lookahead rule,
evidence transaction, selector, health/economics rule, UI asset, schema, credential use, or trading
authority changed. The only production diff is the two outer generic collection ownership blocks.
The generated corrected-tree coverage file was archived with the retained correction wheel; no
coverage, build, dist, egg-info, or cache artifact remains in the worktree. The separate corrective
commit uses `thedev105 <tkim3182@gmail.com>`; its exact hash is reported in the controller handoff.
