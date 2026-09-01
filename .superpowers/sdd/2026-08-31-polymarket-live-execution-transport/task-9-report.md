# Task 9 — recovery, secret, and whole-system verification

## Delivered

- Added the combined ambiguous-submit regression in
  `tests/predictions/test_pilot_execution_port.py`. It proves an `UNKNOWN_OUTCOME` produces one
  submit attempt, engages the parent kill, and sends the signer a kill directive for both the
  primary and recovery grants. The existing runtime-generated secret-canary suite already covers
  REST transport, signer IPC, persistence, dashboard, CLI, and user-stream observable surfaces, so
  no duplicate canary test was added.
- Updated both operator documents with the exact safety sequence: verify persisted 45-day
  qualification and 30-day shadow evidence; confirm current eligibility/KYC/geoblock and protocol
  review; unlock Keychain locally without terminal/UI secret entry; launch killed; inspect exact
  reconciliation; register/verify passkey; activate only with every gate green; manually authorize
  one bounded complete strategy; reconcile; stop/kill on uncertainty.
- Recorded that automation is deliberately unavailable and cannot be substituted with a script,
  API, repeated ceremony, or workaround.

## Verification evidence

| Command | Result |
| --- | --- |
| `rtk .venv/bin/python -m pytest tests/predictions/test_pilot_execution_port.py::test_ambiguous_submit_engages_parent_and_signer_kill_without_retry tests/predictions/test_execution_secret_scan.py tests/predictions/test_polymarket_secret_boundary.py tests/predictions/test_execution_recovery.py tests/predictions/test_pilot_acceptance.py -q` | 205 passed |
| Required focused verification command | 439 passed; 11 blocked signer-sidecar tests (see below) |
| `rtk .venv/bin/python -m pytest -q` | 4,578 passed; 12 failed (see below); 13 pre-existing Pydantic deprecation warnings |
| `rtk .venv/bin/python -m pytest tests/predictions/test_execution_coordinator.py::test_separate_coordinators_on_one_store_share_the_permanent_claim -q` | 1 passed when rerun alone |

## Sandbox limitation, not a test change

All 11 focused-suite failures are spawned-signer tests in
`tests/predictions/test_polymarket_signer_ipc.py`. Python's
`multiprocessing.resource_sharer` attempts to bind an `AF_UNIX` listener below the sandbox temporary
directory and receives `PermissionError: [Errno 1] Operation not permitted`. The same 11 failures
appear in the full suite. The tests and implementation were left unchanged.

The full suite also had one unrelated, non-reproducing concurrent test failure:
`test_separate_coordinators_on_one_store_share_the_permanent_claim` raised `IndexError` from its
one-result `FakeSigner` fixture, then passed in isolation. It is not part of this task and no
production change was made for it.
