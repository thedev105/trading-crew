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
- Froze the server-selected plan after a fresh manifest, reconciliation, account, eligibility,
  and challenge-evidence recheck but before capability issuance. Both grants now bind to that
  exact `FrozenPilotPlan.plan_hash`, and `_run` receives the same plan object rather than selecting
  or compiling again. The browser continues to submit only the opportunity identifier.
- Added focused `manifest_provider` and `reconciliation_provider` callables to `PilotEnvironment`.
  Their `None` defaults retain snapshot compatibility; configured providers are consulted for
  every execution evidence read. Provider exceptions, missing/malformed current state, binding
  mismatch, stale/incomplete reconciliation, expired eligibility, and stale/mismatched/killed
  accounts are all represented as killed evidence.
- Carried the one safely fetched `PilotAccountState` inside `ExecutionEvidence`. The coordinator
  no longer performs a second unguarded signer account read at the authority boundary; missing or
  killed evidence engages parent and signer kill, revokes primary authority, and returns a stable
  `AUTHORITY_REFUSED` failure.
- Added an in-memory framed integration through the real `SignerService`, a real capability proof,
  and a real signed order envelope. It proves the signer's plan-hash check accepts the coordinator
  intent while preserving the distinct capability and authority digest fields.
- Production launch/runtime construction remains intentionally unwired for Task 8; no generic
  transport, Keychain, wallet, or state-port surface was added.

## TDD evidence

- RED: coordinator-kill tests observed no verifier revocation and no signer call.
- RED: a registered fake passkey successfully reached automation challenge persistence instead of
  returning `AUTOMATION_NOT_ACTIVATED`.
- RED: the service had no action-local executor input builder, and automation reached the normal
  `_run` dependency path.
- RED mutation check: removing the stale-reconciliation kill predicate made the new five-minute
  freshness regression fail.
- Fix-round RED: the public cockpit regression observed the grant proofs bound to the protocol
  fixture hash while the executed plan and response used the frozen plan hash.
- Fix-round RED: constructing a real `PilotEnvironment` with current-state providers failed because
  the provider interface did not exist, and the integrated account-unavailable regression failed
  because `ExecutionEvidence` did not carry the safe account result.
- GREEN: the exact-plan identity/binding, authorization evidence recheck, provider freshness and
  fallback, latest-provider challenge binding, single account-read kill, and real-signer
  proof-acceptance regressions all pass.

## Verification

- Fix-round regression set:
  `rtk .venv/bin/python -m pytest -q tests/predictions/test_pilot_end_to_end.py::test_an_approval_binds_both_signer_proofs_to_the_one_executed_frozen_plan tests/predictions/test_pilot_end_to_end.py::test_authorization_rechecks_current_provider_evidence_before_issuing tests/predictions/test_pilot_end_to_end.py::test_a_new_challenge_binds_the_latest_provider_reconciliation tests/predictions/test_pilot_execution.py::test_executor_inputs_contain_only_freshly_issued_proofs_and_live_evidence tests/predictions/test_pilot_execution_port.py::test_unavailable_account_evidence_kills_once_and_returns_a_stable_refusal tests/predictions/test_pilot_signer_link.py::test_a_real_signer_accepts_a_coordinator_intent_bound_to_the_frozen_plan`
  passed (`6 passed`).
- Task 7 focused plus launch/runtime compatibility:
  `rtk .venv/bin/python -m pytest tests/predictions/test_pilot_execution_port.py tests/predictions/test_pilot_execution.py tests/predictions/test_pilot_runtime.py tests/predictions/test_pilot_end_to_end.py tests/predictions/test_pilot_signer_link.py tests/predictions/test_pilot_reconciliation.py tests/predictions/test_pilot_launch.py -q`
  passed (`106 passed`).
- The five directly affected files plus the complete real signer IPC module passed outside the
  sandbox's local-socket restriction (`206 passed`).
- Full repository run completed with `4540 passed, 12 failed`. Eleven failures are sandbox
  `PermissionError` results from spawned-sidecar multiprocessing AF_UNIX socket binds. The other is
  the pre-existing pilot acceptance scan rejecting `httpx` in
  `tests/predictions/test_pilot_signer_services.py`; Task 7 does not modify that file.
- Targeted Ruff, Python byte-compilation, and `git diff --check` passed.

## Scope

- No network request, real Keychain access, real wallet, runtime launch wiring, or subagent was
  used.
