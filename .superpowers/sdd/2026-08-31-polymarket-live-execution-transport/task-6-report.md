# Task 6 report — authoritative startup reconciliation

## Fix round 1 — preserve the Task 8 runtime boundary

- Verified the review finding: the production `build_launch_runtime` caller does not yet own a
  `VenueSubmissionPort`, so making the new argument mandatory caused a repeatable startup
  `TypeError` after successful signer bootstrap.
- Made `venue_port` optional. An explicit port still selects the new authoritative reconciliation
  and account-state callback; `None` restores the prior `transport-unavailable`, incomplete
  reconciliation and `EXECUTION_UNAVAILABLE` account-state refusal.
- Left `runtime.py` production wiring unchanged. Concrete port construction remains Task 8, and
  successful bootstrap still creates live services whose in-memory launch state starts killed.
- RED: direct no-port composition and the existing successful-bootstrap runtime test both failed
  with the missing-keyword `TypeError`.
- GREEN: the no-port fallback, supplied-port path, and runtime regression passed (`3 passed`).
- Combined verification:
  `rtk .venv/bin/python -m pytest tests/predictions/test_pilot_reconciliation.py tests/predictions/test_pilot_launch.py tests/predictions/test_pilot_read_models.py tests/predictions/test_pilot_signer_link.py tests/predictions/test_pilot_execution_port.py tests/predictions/test_pilot_runtime.py -q`
  passed (`82 passed`).

## Implemented

- Extended `VenueSubmissionPort` with typed `orders()` and `trades()` reads that return only
  `SanitizedOperationResult` records.
- Added matching `SignerLinkVenuePort` methods. Each constructs exactly one proof-free,
  unfiltered `READ_ORDERS` or `READ_TRADES` payload and rejects a mismatched response operation.
- Added `reconcile_startup`, which always attempts account, position, open-order, and trade reads.
  It marks every read exception, missing/mismatched result, account/position contradiction, open
  order, duplicate trade ID, and nonterminal trade as unknown. It never includes exception text.
- Reconciliation hashes contain only sorted public identifiers, sorted sanitized result/status
  codes, and sorted observation times. Balances, position sizes, raw-body/evidence hashes, and
  venue payload details are excluded.
- When supplied, `compose_pilot_environment` uses a `VenueSubmissionPort` for the environment's
  account-state callback and authoritative startup reconciliation. No grant, kill clearance,
  order, cancellation, or transport behavior was added or changed.

## TDD evidence

- RED: the new reconciliation suite initially failed collection because the module did not exist;
  the link tests then failed because `SignerLinkVenuePort` had no `orders()` or `trades()` methods.
- RED mutation check: removing sorting from the reconciliation hash made the reversal regression
  fail with different hashes for equivalent snapshots.
- GREEN: restored canonical sorting and the full affected suite passed (`58 passed`).

## Verification

- `rtk .venv/bin/python -m pytest tests/predictions/test_pilot_reconciliation.py tests/predictions/test_pilot_launch.py tests/predictions/test_pilot_read_models.py tests/predictions/test_pilot_signer_link.py tests/predictions/test_pilot_execution_port.py -q`
  passed (`58 passed`).
- Targeted Ruff passed for all changed Python files.
- `rtk git diff --check` passed.

## Scope

- No network, real Keychain, real wallet, or subagent was used.
- `build_launch_runtime` construction of the concrete venue port remains deliberately owned by
  Task 8; this task changes the composition contract and fail-closed reconciliation only. Parent
  service state still starts killed.
