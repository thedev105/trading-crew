# Task 5 report — proof-bound parent IPC

## Fix round 1 — preserve plan binding and detach grant authority

- Kept `ExecutionIntent.capability_fingerprint` and `SignerRequest.capability_digest` as the
  frozen plan digest; `CoordinatorExecutionPort` remains unchanged.
- Replaced redundant `plan_digest` with `authority_digest`, which binds a mutation to
  `SignerCapabilityProof.grant.digest` while the signer independently verifies
  `grant.plan_hash == capability_digest`.
- Updated the IPC payload validator, signer authority projection, request fixtures, and sealed
  authority-source hashes so neither the plan nor the signed grant can be substituted.
- Added a coordinator-to-link integration test that builds a real coordinator intent and verifies
  it frames with that intent's plan digest plus the matching proof's authority digest.

### Fix verification

- RED: the coordinator-to-link test failed with `IPC_MODEL_INVALID` because the old parent link
  replaced the intent's plan digest with the grant digest.
- GREEN: the integration test passed after the digest split.
- `rtk .venv/bin/python -m pytest tests/predictions/test_pilot_signer_link.py tests/predictions/test_polymarket_signer_ipc.py tests/predictions/test_pilot_signer_services.py tests/predictions/test_execution_secret_scan.py tests/predictions/test_execution_authority_scan.py tests/predictions/test_pilot_execution_port.py -q`
  passed (`243 passed`) with permission for the local spawned signer socket.
- Targeted Ruff, `rtk git diff --check`, and the authority scan (`68 passed`) passed.

## Implemented

- `SignerLinkVenuePort` now receives `proof_for` and `kill_directive` callbacks.
- Submit and cancel resolve the exact capability ID before IPC, attach that public proof, and derive
  `capability_digest` and `plan_digest` from the returned grant. Missing or mismatched mappings
  fail locally before any frame is written.
- Identity, reads, and the dedicated signer-kill operation remain proof-free.
- `engage_kill` creates only `SignerKillPayload(SIGNER_KILL, signed_directive)` and returns no
  control response data; a signer rejection becomes `SignerLinkError` with its sanitized code.

## Tests

- RED: `rtk .venv/bin/python -m pytest tests/predictions/test_pilot_signer_link.py -q -k "proof or engage_kill"`
  failed because the port did not accept proof callbacks or expose `engage_kill`.
- GREEN: the same selection passed (`3 passed`).
- Required suite: `rtk .venv/bin/python -m pytest tests/predictions/test_pilot_signer_link.py tests/predictions/test_polymarket_signer_ipc.py -q`
  passed (`150 passed`) when run with permission for its local temporary Unix-domain sidecar socket.
- `rtk .venv/bin/ruff check src/polytrading/predictions/pilot/signer_link.py tests/predictions/test_pilot_signer_link.py`
  and `rtk git diff --check` passed.

## Scope

- No network, Keychain, executor, or runtime wiring was added.
