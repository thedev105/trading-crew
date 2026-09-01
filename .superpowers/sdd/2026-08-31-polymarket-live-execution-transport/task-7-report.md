# Task 7 report — capability-gated manual executor

## Implemented

- Extended `VenueSubmissionPort` with only the typed `engage_kill(capability_ids)` operation.
- Made `CoordinatorExecutionPort.engage_kill` revoke every primary grant first, retain the parent
  kill callback, and attempt exactly one signer kill directive containing the fixed primary and
  recovery capability IDs. A signer-link exception is best effort and cannot undo parent
  revocation or kill state.
- Centralized executor stop handling on `ExecutionPort.engage_kill`; the executor no longer relies
  on a separate caller-side revocation step.
- Rejected `AUTOMATION_SESSION` in challenge construction before challenge/nonce persistence and
  again at the start of `_run`, before opportunity lookup or executor construction.
- Expanded the executor-factory contract with two action-local maps: independently verified
  coordinator capabilities and signer proofs derived only from the just-issued primary/recovery
  pair. The intent/request split remains unchanged: `capability_digest` is the plan hash and
  `authority_digest` is the signed grant digest.
- Added a live evidence closure over the current manifest, reconciliation, authoritative account
  read, and browser/native presence. Missing account state, account mismatch/kill, incomplete or
  five-minute-stale reconciliation, or stale account evidence is represented as killed.
- Kept the server-selected opportunity and frozen-plan path unchanged. Production launch/runtime
  construction remains intentionally unwired for Task 8; no generic transport or port was added.

## TDD evidence

- RED: coordinator-kill tests observed no verifier revocation and no signer call.
- RED: a registered fake passkey successfully reached automation challenge persistence instead of
  returning `AUTOMATION_NOT_ACTIVATED`.
- RED: the service had no action-local executor input builder, and automation reached the normal
  `_run` dependency path.
- RED mutation check: removing the stale-reconciliation kill predicate made the new five-minute
  freshness regression fail.
- GREEN: all new kill, IPC-failure, automation, proof-map, live-evidence, and stale-reconciliation
  regressions pass.

## Verification

- Task 7 focused plus launch/runtime compatibility:
  `rtk .venv/bin/python -m pytest tests/predictions/test_pilot_execution_port.py tests/predictions/test_pilot_execution.py tests/predictions/test_pilot_runtime.py tests/predictions/test_pilot_end_to_end.py tests/predictions/test_pilot_signer_link.py tests/predictions/test_pilot_reconciliation.py tests/predictions/test_pilot_launch.py -q`
  passed (`101 passed`).
- Full repository run completed with `4535 passed, 12 failed`. Eleven failures are sandbox
  `PermissionError` results from spawned-sidecar multiprocessing AF_UNIX socket binds. The other is
  the pre-existing pilot acceptance scan rejecting `httpx` in
  `tests/predictions/test_pilot_signer_services.py`; Task 7 does not modify that file.
- Targeted Ruff and `git diff --check` passed.

## Scope

- No network request, real Keychain access, real wallet, runtime launch wiring, or subagent was
  used.
