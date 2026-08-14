# Final branch review — fix round 2 report

Date: 2026-08-14
Branch: `feature/lighter-dydx-evidence-ops`
Review base: `7956a49f0696c01e9a6157e61b312a20e22ef815`

## Outcome

The two independently confirmed round-2 findings are fixed without changing trading authority,
public-adapter scope, point-in-time selection, mature bulk reads, full-depth economics selection,
or dashboard mutability. Close-time `collect funding-cycle` failures now use the same stable safe
classification as construction and collection failures. Trial economics summaries now enforce the
authoritative uppercase machine-reason syntax while retaining canonical sorted/unique validation.

## Root cause and TDD evidence

### Clean baseline

Before test or production changes, the complete suite passed:

```text
.venv/bin/python -m pytest -q
1257 passed in 566.67s (0:09:26)
```

### RED

Focused regressions were added before production changes. The command covered close-only
`OSError`, close-only `duckdb.Error`, simultaneous body/close failure, hostile summary reason
strings, and preservation of valid current/legacy reason codes.

```text
.venv/bin/python -m pytest \
  tests/test_cli.py::test_funding_cycle_close_failure_uses_stable_sanitized_classification \
  tests/test_cli.py::test_funding_cycle_body_failure_wins_over_sanitized_close_failure \
  tests/trial/test_health_models.py::test_economics_summary_rejects_non_machine_reason_codes \
  tests/trial/test_health_models.py::test_economics_summary_accepts_current_and_legacy_machine_reason_codes -q
9 failed, 1 passed in 2.18s
```

Observed pre-fix behavior:

- close-time `OSError` returned exit 2 and printed an injected path, URL, token, response text, and
  lock detail;
- close-time `duckdb.Error` returned exit 1 but printed the same injected raw detail;
- an HTTP body failure followed by an `OSError` during close returned exit 2, leaked close detail,
  and replaced the primary HTTP classification;
- all six hostile economics-summary reason strings were accepted;
- existing canonical and `LEGACY_ECONOMICS_SCHEMA_UNSUPPORTED` reason codes remained valid.

### GREEN

After the smallest production correction, the identical command passed:

```text
10 passed in 2.03s
```

The command-boundary assertions require exit 1, exact stable classifications, empty stdout, and no
injected raw detail. The simultaneous-failure regression locks primary/body precedence as
`FUNDING_CYCLE_HTTP_ERROR` and excludes both raw body and close messages.

A final focused confirmation also included the unchanged trial-funding HTTP sanitization regression
and constructed both the current and legacy summary examples through normal model validation:

```text
11 passed in 1.91s
```

## Implementation

### Funding-cycle lifecycle classification

`_collect_funding_cycle` now places store construction, collection/late recording, and close inside
the classification boundary. It records an active body exception only to ensure a close failure
cannot replace it. A close failure with no active body failure is re-raised into the existing
classifier. The resulting `FundingCycleCollectionError` remains explicitly chained from the
selected original exception, while user-visible output contains only the stable code.

The separate `trial funding` lifecycle was not modified. Its existing HTTP and writer-lease
sanitization regressions passed in the focused, broader, and full-suite gates.

### Economics-summary reason validation

The authoritative `[A-Z][A-Z0-9_]*` economics reason contract is now implemented once by
`require_machine_reason_codes`. Existing legacy and schema-two economics models reuse that helper,
and `TrialEconomicsEvaluationSummary` adds it after its existing sorted/unique check. Paths, URLs,
free-form/secret-like text, lowercase, leading digits, and hyphenated identifiers fail validation;
canonical current and legacy codes remain accepted.

## Changed files

Production:

- `src/polytrading/cli.py`
- `src/polytrading/carry/economics_models.py`
- `src/polytrading/trial/health_models.py`

Tests:

- `tests/test_cli.py`
- `tests/trial/test_health_models.py`

Evidence:

- `.superpowers/sdd/2026-08-14-lighter-dydx-evidence-operations/final-review-fix-round-2-report.md`

## Verification evidence

### Focused CLI/model/report/web matrix

```text
.venv/bin/python -m pytest \
  tests/test_cli.py tests/trial/test_health_models.py tests/trial/test_health.py \
  tests/trial/test_health_report.py tests/carry/test_economics_models.py \
  tests/carry/test_economics_report.py tests/web/test_models.py \
  tests/web/test_dashboard.py tests/web/test_assets.py -q
275 passed in 217.43s (0:03:37)
```

### Broad round-1 preservation matrix

The prior final-review matrix was rerun with the new regressions. It covered storage, mature book
evidence, health, CLI, all economics paths, web server/models/dashboard/assets, and packaging.

```text
.venv/bin/python -m pytest \
  tests/storage/test_store.py tests/trial/test_book_evidence.py \
  tests/trial/test_health.py tests/trial/test_health_models.py \
  tests/trial/test_health_report.py tests/test_cli.py \
  tests/carry/test_economics.py tests/carry/test_economics_assembler.py \
  tests/carry/test_economics_execution.py tests/carry/test_economics_funding.py \
  tests/carry/test_economics_models.py tests/carry/test_economics_report.py \
  tests/web/test_models.py tests/web/test_dashboard.py tests/web/test_server.py \
  tests/web/test_assets.py tests/test_package.py -q
434 passed in 413.14s (0:06:53)
```

### Full suite and coverage

```text
.venv/bin/python -m pytest --cov=polytrading --cov-report=term-missing \
  --cov-fail-under=90 -q
TOTAL 11363 statements, 824 missed, 93% displayed
Required test coverage of 90% reached. Total coverage: 92.75%
1267 passed in 575.51s (0:09:35)
```

### Static checks

```text
.venv/bin/python -m ruff check src tests
All checks passed!

.venv/bin/python -m ruff format --check src tests
170 files already formatted

git diff --check
exit 0, no output
```

### Fresh wheel CLI smoke

The wheel was built and installed under
`/private/tmp/polytrading-round2-wheel.XgY65r`:

```text
polytrading-0.1.0-py3-none-any.whl
size=260730 bytes
sha256=56a9ccdc44ee08b6a1820b870868ba87077018e0e1f1dbbd311a4593ce5897f0
```

The host's base Python has no global `duckdb`, so an initial `--system-site-packages` launch failed
with `ModuleNotFoundError: duckdb`. The previously documented dependency-only `PYTHONPATH` fallback
was then used. A `.pth` file in that directory is not processed as a site-initialization file, and
the import-origin proof confirmed that `polytrading` came from the installed wheel:

```text
polytrading /private/tmp/polytrading-round2-wheel.XgY65r/venv/lib/python3.14/site-packages/polytrading/__init__.py
duckdb /Volumes/WORK/poly-trading/.worktrees/lighter-dydx-evidence-ops/.venv/lib/python3.14/site-packages/duckdb/__init__.py
```

These installed-wheel commands all exited zero:

```text
polytrading --help
polytrading collect funding-cycle --help
polytrading trial funding --help
polytrading trial books --help
polytrading trial health --help
```

## Smoke isolation decisions

- The mature 6,480-cycle benchmark was not rerun because neither the store, auditor, bulk header
  reader, book reconstruction, nor query selection changed. The broad and full suites retain the
  query-count and mature-fixture protections from round 1.
- The manual browser smoke was not rerun because no server or asset file changed. Valid economics
  summaries were exercised through health/report and web model/dashboard/asset tests, including
  current and legacy render paths.
- Full-depth economics selection and execution modules were unchanged and passed in the broad
  preservation and full-coverage gates.

## Safety and self-review

- The lifecycle change classifies close-only failures, preserves primary errors when both phases
  fail, retains exception chaining, and never converts a failed close into success.
- The shared reason validator is behavior-preserving for both authoritative economics models and
  narrows only the previously permissive trial summary.
- Canonical sorting and uniqueness remain enforced before syntax validation.
- No credentials, accounts, balances, positions, orders, fills, transfers, signer, wallet, paper
  execution, live execution, adapter, point-in-time, dashboard-write, or allocation capability was
  added or changed.
- Generated `.coverage` and `build/` artifacts were moved to
  `/private/tmp/polytrading-round2-artifacts.3QIc95`; the wheel directory was retained for audit.

No blocker or unresolved correctness concern remains. The required commit author is
`thedev105 <tkim3182@gmail.com>`; the final commit hash is reported in the controller handoff.
