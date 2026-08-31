# Task 2 report: signer-local proof verification and kill state

## Implemented

- Added `SignerRequest.plan_digest` as a separate SHA-256 binding from the grant digest and added
  the closed `SIGNER_KILL_ENGAGED` success result plus replay/kill rejection codes.
- Added signer-local Ed25519 proof verification against the inherited launch public key before the
  authority context factory runs. The signer compares grant digest, account, manifest, frozen
  protocol fixture, plan, allowed operation, validity window, request deadline, and presence
  deadline, rejects automation grants, and projects only the verified grant into the authority
  layer.
- Added irreversible signer-local kill state. A signed `SIGNER_KILL` never reaches a venue handler;
  invalid directives cannot engage or clear kill, and route results with `kill_required=True`
  latch kill before the response returns.
- Added intent-aware primary consumption before dispatch. Complete-strategy authority permits a
  sign-then-submit sequence and distinct frozen-plan intents, but rejects a duplicate submission
  for `(capability_id, intent_id)`. Exact-order authority permits only one intent.
- Kept the no-key offline signer fail closed as `EXECUTION_UNAVAILABLE`; no venue transport,
  Keychain access, or network behavior was added.

## Files

- `src/polytrading/predictions/polymarket_execution/ipc.py`
- `src/polytrading/predictions/polymarket_execution/signer.py`
- `tests/predictions/test_polymarket_signer_ipc.py`
- `tests/predictions/test_execution_authority_scan.py`
- `.superpowers/sdd/2026-08-31-polymarket-live-execution-transport/task-2-report.md`

## Tests

- `rtk .venv/bin/python -m pytest tests/predictions/test_polymarket_signer_ipc.py tests/predictions/test_execution_authority_scan.py -q`
  - 203 passed in 12.02s, including the spawned sidecar tests run with permission for the local
    multiprocessing Unix-domain helper socket.
- `rtk .venv/bin/python -m pytest tests/predictions/test_pilot_capabilities.py tests/predictions/test_pilot_signer_services.py -q`
  - 22 passed in 0.54s.
- `rtk .venv/bin/ruff check src/polytrading/predictions/polymarket_execution/ipc.py src/polytrading/predictions/polymarket_execution/signer.py tests/predictions/test_polymarket_signer_ipc.py tests/predictions/test_execution_authority_scan.py`
  - All checks passed.

## TDD red-green evidence

- Required signer gate RED:
  - `rtk .venv/bin/python -m pytest tests/predictions/test_polymarket_signer_ipc.py -q -k "signed_kill or consumes_a_primary or another_issuer"`
  - 3 failed, 125 deselected. Each failed because `SignerService.__init__` did not accept
    `capability_public_key`.
- Required signer gate GREEN:
  - The same command passed: 3 passed, 125 deselected.
- Route-result kill RED:
  - `-k "handler_result_requiring_kill"` failed because the next mutation remained allowed.
- Route-result kill GREEN:
  - The same selection passed after latching `_kill_engaged` from a validated result.
- Offline compatibility RED/GREEN:
  - The adjacent suite first had 21 passed / 1 failed because the empty-key offline signer returned
    `CAPABILITY_SIGNATURE_INVALID`; after the fail-closed sentinel fix it passed 22/22 with
    `EXECUTION_UNAVAILABLE` restored.

## Self-review

- Re-read the controller ruling and verified consumption is per approved frozen action rather than
  per IPC frame; duplicate submission is checked before handler dispatch and remains consumed even
  if the handler fails.
- Verified proof signature validation precedes every grant-field comparison and precedes the
  authority context factory, so a valid grant from another issuer never reaches injected authority
  code.
- Verified signed kill has a dedicated response type, has no handler path, cannot be cleared by any
  IPC request, and blocks only mutations while preserving identity/read posture.
- Verified the authority-source manifest permits capability projection only in the reviewed
  verifier and signer boundaries and includes the final IPC/signer source hashes.
- No subagent review was used because the task explicitly prohibited subagents; the final diff was
  reviewed locally for plan alignment, security ordering, error sanitization, type/field closure,
  tests, and unintended files.

## Concerns

- `plan_digest` defaults to the all-zero fail-closed transition value so pending parent-link call
  sites remain constructible. Tasks 5 and 7 must explicitly set `capability_digest` and
  `ExecutionIntent.capability_fingerprint` to the grant digest and `plan_digest` to
  `grant.plan_hash`, as ruled.
- `SignerService` accepts an empty public key only for the existing offline posture and then refuses
  mutations as `EXECUTION_UNAVAILABLE`. The live composition task must always pass the issuer's
  32-byte public verification key.
