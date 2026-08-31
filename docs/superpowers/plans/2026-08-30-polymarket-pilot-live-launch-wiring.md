# Polymarket Pilot Live Launch Wiring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire `predictions pilot polymarket` so a real launch composes a `PilotEnvironment` — signer bootstrap, identity via a new signer `DESCRIBE_IDENTITY` operation, persisted-evidence loading — making the operator ceremonies reachable while keeping execution refused (`EXECUTION_UNAVAILABLE`) because this repository still constructs no venue transport.

**Architecture:** The parent launches the signer sidecar from the Keychain (wallet key required, CLOB credentials optional), asks the signer for its identity fingerprints over IPC, loads manifest/qualification/eligibility evidence from the store, and passes the composed environment to `build_pilot_runtime`. Every gate that needs venue transport (execution, credential ceremony, authoritative reconciliation) refuses with the stable codes the services already emit for absent factories.

**Tech Stack:** Python 3.13, pydantic v2, DuckDB, eth-account 0.13.7 (already pinned), pytest.

**Spec:** `docs/superpowers/specs/2026-08-27-polymarket-local-live-pilot-design.md` (sections 4.2–4.4) plus the deliberate boundary recorded in memory and `docs/predictions/polymarket-execution-hardening.md`: this repo constructs no socket-opening transport anywhere.

## Global Constraints

- Secrets never enter argv, env, logs, DuckDB, IPC request bodies, or exceptions; they move as `SecretBuffer`/`SecretMaterial` only.
- `tests/predictions/test_execution_authority_scan.py` seals `execution/` and `polymarket_execution/` source bytes in `_REVIEWED_SOURCE_SHA256`; Tasks 1–2 must update that digest deliberately, after review, in the same commit as the reviewed change.
- The CLI surface stays exactly `--db` and `--port` (`src/polytrading/predictions/cli.py:338-339`). No new flags.
- Every launch starts killed; nothing in this plan may create trading capability or clear a kill.
- No venue transport is constructed. `executor_factory`, `credential_provisioner`, and `activation_inputs` stay `None` in the composed environment.
- Fingerprint convention: `sha256(decoded_address_bytes).hexdigest()`, matching `SignerService._secret_account_matches` (`signer.py:341-351`). EOA pilot: wallet fingerprint equals account fingerprint.
- Full suite must stay green: `.venv/bin/python -m pytest -q` (≈4400 tests).

---

### Task 1: Signer `DESCRIBE_IDENTITY` operation

**Files:**
- Modify: `src/polytrading/predictions/execution/models.py` (add `DESCRIBE_IDENTITY` to `ExecutionOperation`, line 51)
- Modify: `src/polytrading/predictions/polymarket_execution/ipc.py` (payload + result models, unions)
- Modify: `src/polytrading/predictions/polymarket_execution/signer.py` (dispatch before the account-fingerprint gate)
- Modify: `tests/predictions/test_execution_authority_scan.py` (`_REVIEWED_SOURCE_SHA256` update)
- Test: `tests/predictions/test_polymarket_signer_ipc.py` (extend)

**Interfaces:**
- Produces `ExecutionOperation.DESCRIBE_IDENTITY`, `DescribeIdentityPayload(operation=...)`, `IdentityResult(operation, account_fingerprint, wallet_fingerprint)`, and `SignerService` handling that returns the fingerprints derived from the wallet key.
- The request uses `account_fingerprint="0" * 64` (the parent does not know it yet); `DESCRIBE_IDENTITY` is the only operation exempt from `_secret_account_matches`.
- Consumed by Task 3 (offline service factory test) and Task 5 (launch wiring).

- [x] **Step 1: Write the failing tests**

In `tests/predictions/test_polymarket_signer_ipc.py` (reuse that file's existing service/request builders — it already constructs a `SignerService` with fake handlers and canary secrets):

```python
def test_describe_identity_returns_wallet_derived_fingerprints() -> None:
    service = build_service()  # existing helper with known canary private key
    request = build_request(
        operation=ExecutionOperation.DESCRIBE_IDENTITY,
        account_fingerprint="0" * 64,
        payload={"operation": "DESCRIBE_IDENTITY"},
    )
    response = service.handle(request)
    assert response.ok, response.error_code
    expected = sha256(
        bytes.fromhex(Account.from_key(KNOWN_TEST_KEY).address[2:])
    ).hexdigest()
    assert response.result.account_fingerprint == expected
    assert response.result.wallet_fingerprint == expected


def test_describe_identity_never_reflects_secret_bytes() -> None:
    service = build_service()
    response = service.handle(build_describe_request())
    assert KNOWN_TEST_KEY_HEX not in response.model_dump_json()


def test_describe_identity_is_the_only_gate_exempt_operation() -> None:
    # A READ_ACCOUNT request with a zero fingerprint must still be rejected.
    service = build_service()
    request = build_request(
        operation=ExecutionOperation.READ_ACCOUNT,
        account_fingerprint="0" * 64,
    )
    assert service.handle(request).error_code == "ACCOUNT_FINGERPRINT_MISMATCH"
```

Adapt names to that file's existing helpers; add a `build_describe_request` helper there if none fits.

- [x] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/predictions/test_polymarket_signer_ipc.py -q -k describe`
Expected: FAIL (`DESCRIBE_IDENTITY` not a member of `ExecutionOperation` / validation error).

- [x] **Step 3: Implement**

`execution/models.py`: add `DESCRIBE_IDENTITY = "DESCRIBE_IDENTITY"` to `ExecutionOperation`.

`ipc.py`:

```python
class DescribeIdentityPayload(_SignerRecord):
    operation: Literal[ExecutionOperation.DESCRIBE_IDENTITY]


class IdentityResult(_SignerRecord):
    operation: Literal[ExecutionOperation.DESCRIBE_IDENTITY]
    account_fingerprint: Sha256
    wallet_fingerprint: Sha256
```

Add `DescribeIdentityPayload` to the `SignerPayload` union and `IdentityResult` to the `SignerResult` union (keep discriminators valid). Do NOT widen `SanitizedOperationResult.operation` — identity is its own result type carrying no venue fields.

`signer.py`, in `_handle_uncached` (`signer.py:300`): after the deadline and protocol-version checks but **before** `_secret_account_matches` (line 323), add:

```python
if request.operation is ExecutionOperation.DESCRIBE_IDENTITY:
    return self._describe_identity(request)
```

and implement:

```python
def _describe_identity(self, request: SignerRequest) -> SignerResponse:
    try:
        private_key = bytes(self._secrets.private_key)
        address = Account.from_key(private_key).address
        fingerprint = sha256(bytes.fromhex(address[2:])).hexdigest()
    except Exception:
        return SignerResponse.rejected(request.request_id, "IPC_REQUEST_INVALID")
    finally:
        private_key = None
    return SignerResponse.accepted(
        request.request_id,
        IdentityResult(
            operation=ExecutionOperation.DESCRIBE_IDENTITY,
            account_fingerprint=fingerprint,
            wallet_fingerprint=fingerprint,
        ),
    )
```

Leave `_MUTATING_OPERATIONS`/`_READ_OPERATIONS` untouched so `DESCRIBE_IDENTITY` reaching the operation switch (it cannot, given the early return) would still be `IPC_OPERATION_NOT_ALLOWED`.

- [x] **Step 4: Run the signer tests**

Run: `.venv/bin/python -m pytest tests/predictions/test_polymarket_signer_ipc.py tests/predictions/test_polymarket_secret_boundary.py -q`
Expected: PASS except `test_execution_authority_scan.py` (not in this selection).

- [x] **Step 5: Update the sealed-source digest**

Run the authority scan, read its failure output for the new digests, review the diff once more, then update `_REVIEWED_SOURCE_SHA256` in `tests/predictions/test_execution_authority_scan.py`.

Run: `.venv/bin/python -m pytest tests/predictions/test_execution_authority_scan.py -q`
Expected: PASS.

- [x] **Step 6: Commit**

```bash
git add src/polytrading/predictions/execution/models.py src/polytrading/predictions/polymarket_execution/ipc.py src/polytrading/predictions/polymarket_execution/signer.py tests/predictions/test_polymarket_signer_ipc.py tests/predictions/test_execution_authority_scan.py
git commit -m "feat(predictions): let the signer describe its wallet identity"
```

### Task 2: Wallet-only secret material and bootstrap

**Files:**
- Modify: `src/polytrading/predictions/polymarket_execution/secrets.py` (`SecretMaterial` CLOB trio all-or-none)
- Modify: `src/polytrading/predictions/polymarket_execution/signer.py` (secret-descriptor loader accepts zero-length CLOB frames, if the length check lives there)
- Modify: `src/polytrading/predictions/pilot/signer_bootstrap.py` (optional CLOB secrets, `credentials_present` on `SignerChannel`)
- Modify: `tests/predictions/test_execution_authority_scan.py` (digest update)
- Test: `tests/predictions/test_polymarket_secret_boundary.py`, `tests/predictions/test_pilot_signer_bootstrap.py`

**Interfaces:**
- Consumes nothing new from Task 1.
- Produces: `SecretMaterial` accepting `len == 0` for `api_key`/`api_secret`/`passphrase` **only when all three are empty**; `SignerChannel` gains field `credentials_present: bool`; `launch_signer_sidecar` treats `SECRET_ITEM_MISSING` for the three CLOB accounts as "absent" (empty frame) but still fails on a missing wallet key.
- Task 5 reads `channel.credentials_present`.

- [x] **Step 1: Write the failing tests**

`tests/predictions/test_polymarket_secret_boundary.py`:

```python
def test_secret_material_accepts_an_all_empty_clob_trio() -> None:
    material = SecretMaterial(bytearray(32), bytearray(), bytearray(), bytearray())
    assert len(material.api_key) == 0


def test_secret_material_rejects_a_partial_clob_trio() -> None:
    with pytest.raises(SecretBoundaryError):
        SecretMaterial(bytearray(32), bytearray(b"k"), bytearray(), bytearray())
```

`tests/predictions/test_pilot_signer_bootstrap.py` (reuse its fake store and spawn harness):

```python
def test_bootstrap_launches_wallet_only_and_reports_credentials_absent() -> None:
    store = FakeSecretStore(missing={CLOB_API_KEY_ACCOUNT, CLOB_API_SECRET_ACCOUNT, CLOB_PASSPHRASE_ACCOUNT})
    channel = launch_signer_sidecar(store=store, service_factory=capture_factory, spawn=run_inline)
    assert channel.credentials_present is False
    # the captured SecretMaterial carries the wallet key and empty CLOB buffers


def test_bootstrap_still_fails_without_the_wallet_key() -> None:
    store = FakeSecretStore(missing={WALLET_PRIVATE_KEY_ACCOUNT})
    with pytest.raises(SignerBootstrapError):
        launch_signer_sidecar(store=store, service_factory=capture_factory, spawn=run_inline)


def test_bootstrap_reports_credentials_present_when_all_four_exist() -> None:
    channel = launch_signer_sidecar(store=FakeSecretStore(), service_factory=capture_factory, spawn=run_inline)
    assert channel.credentials_present is True
```

- [x] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/predictions/test_polymarket_secret_boundary.py tests/predictions/test_pilot_signer_bootstrap.py -q -k "wallet_only or empty_clob or partial_clob or credentials"`
Expected: FAIL (`SECRET_MATERIAL_INVALID`, `TypeError` on `credentials_present`).

- [x] **Step 3: Implement**

`secrets.py` `SecretMaterial.__init__` validation (`secrets.py:38-44`): keep `len(private_key) == 32`; replace the per-buffer `0 < len(value)` check for the CLOB trio with:

```python
clob = values[1:]
if any(len(value) > _MAX_SECRET_BYTES for value in clob):
    raise SecretBoundaryError("SECRET_MATERIAL_INVALID") from None
if any(len(value) == 0 for value in clob) and any(len(value) > 0 for value in clob):
    raise SecretBoundaryError("SECRET_MATERIAL_INVALID") from None
```

Add a helper the signer and services can use:

```python
@property
def credentials_present(self) -> bool:
    return len(self._api_key) > 0
```

`signer_bootstrap.py`:
- `_read_secrets`: wrap each CLOB `read_required` so `SecretStoreError("SECRET_ITEM_MISSING")` yields `SecretBuffer.from_bytes(b"")`-equivalent empty buffer (add an `empty()` constructor if `from_bytes` rejects empty input); wallet key errors still raise `SignerBootstrapError`.
- `_write_framed_secret` (`signer_bootstrap.py:147-150`): change the bound to `0 <= length <= MAXIMUM_SECRET_BYTES` and skip the body write when `length == 0` (header only).
- Wherever the sidecar reads the framed secrets (follow `run_signer_sidecar`'s secret loader in `signer.py`), accept a zero-length frame for the three CLOB descriptors, still reject it for the wallet descriptor.
- `SignerChannel`: add `credentials_present: bool`; set it from whether the CLOB reads found items.

- [x] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/predictions/test_polymarket_secret_boundary.py tests/predictions/test_pilot_signer_bootstrap.py tests/predictions/test_polymarket_signer_ipc.py -q`
Expected: PASS.

- [x] **Step 5: Update the sealed-source digest** (same procedure as Task 1 Step 5), run the scan, expect PASS.

- [x] **Step 6: Commit**

```bash
git add src/polytrading/predictions/polymarket_execution/secrets.py src/polytrading/predictions/polymarket_execution/signer.py src/polytrading/predictions/pilot/signer_bootstrap.py tests/predictions/test_polymarket_secret_boundary.py tests/predictions/test_pilot_signer_bootstrap.py tests/predictions/test_execution_authority_scan.py
git commit -m "feat(predictions): allow a wallet-only signer launch"
```

### Task 3: Offline pilot signer service factory

**Files:**
- Create: `src/polytrading/predictions/pilot/signer_services.py`
- Test: `tests/predictions/test_pilot_signer_services.py`

**Interfaces:**
- Consumes `SignerService`, `SignerOperationHandlers`, `AuthorityDecision` from `polymarket_execution`, `SecretMaterial` from Task 2.
- Produces `offline_pilot_signer_service(secrets: SecretMaterial) -> SignerService`: a `SignerServiceFactory` whose authority context factory and read guard deny every operation with reason `"EXECUTION_UNAVAILABLE"`, and whose handlers raise `RuntimeError("EXECUTION_UNAVAILABLE")` (unreachable). `DESCRIBE_IDENTITY` works because Task 1 dispatches it before any gate.
- Task 5 passes this factory to `launch_signer_sidecar`.

- [x] **Step 1: Write the failing tests**

```python
def test_offline_service_answers_describe_identity() -> None:
    service = offline_pilot_signer_service(canary_secret_material())
    response = service.handle(build_describe_request())
    assert response.ok


def test_offline_service_refuses_every_other_operation() -> None:
    service = offline_pilot_signer_service(canary_secret_material())
    for operation in set(ExecutionOperation) - {ExecutionOperation.DESCRIBE_IDENTITY}:
        response = service.handle(build_request_for(operation))
        assert not response.ok
        assert response.error_code in ("EXECUTION_UNAVAILABLE", "ACCOUNT_FINGERPRINT_MISMATCH")
```

Build requests with the canary key's true fingerprint for the second test so the refusal proves the authority gate, not the fingerprint gate, for at least `READ_ACCOUNT`.

- [x] **Step 2: Run to verify failure** — module missing. Expected: FAIL (ImportError).

- [x] **Step 3: Implement** `signer_services.py` (~40 lines): construct `SignerService` with `authority_context_factory`/`read_guard` returning a denied `AuthorityDecision(reason="EXECUTION_UNAVAILABLE")` (match the real `AuthorityDecision` constructor — read it in `execution` before writing), handlers raising, `clock=datetime.now(UTC)`.

- [x] **Step 4: Run** `.venv/bin/python -m pytest tests/predictions/test_pilot_signer_services.py -q` — PASS.

- [x] **Step 5: Commit**

```bash
git add src/polytrading/predictions/pilot/signer_services.py tests/predictions/test_pilot_signer_services.py
git commit -m "feat(predictions): compose an offline pilot signer service"
```

### Task 4: Persisted-evidence environment loader

**Files:**
- Create: `src/polytrading/predictions/pilot/launch.py`
- Modify: `src/polytrading/predictions/pilot/services.py` (`PilotEnvironment.venue_binding: VenueBinding | None = None`; guard issuance)
- Test: `tests/predictions/test_pilot_launch.py`

**Interfaces:**
- Consumes: `PredictionMarketStore` verified reads (`verified_latest_venue_manifest_as_of`, `verified_pilot_eligibility_attestations`, `verified_pilot_credential_provisioning_events`), `evaluate_pilot_qualification` from `pilot/qualification.py` (read its exact signature first — it recomputes from persisted shadow evidence), `PilotEnvironment` from `services.py`.
- Produces:

```python
def compose_pilot_environment(
    store: PredictionMarketStore,
    *,
    account_fingerprint: Sha256,
    wallet_fingerprint: Sha256,
    credentials_present: bool,
    now: Callable[[], datetime],
) -> PilotEnvironment: ...
```

  with: manifest/manifest_state from the store (`"MISSING"` and `manifest=None`, `venue_binding=None` when no manifest exists; a full `VenueBinding` built from the manifest record hash, its source hashes, and the current protocol checkpoint hashes when it does), `protocol_state="CURRENT"` (the runtime already gates on it), qualifications recomputed from persisted evidence (empty tuple when none), `eligibility_expires_at` from the latest verified attestation or `None`, `reconciliation` set to a conservative incomplete state (`reconciliation_complete=False`, zero counts, `observed_at=now()`), and `account_state` a callable that raises `PilotRequestError(HTTPStatus.CONFLICT, "EXECUTION_UNAVAILABLE")` — it is only reachable through paths already guarded by `executor_factory is None`.
- In `services.py`, wherever `self._environment.venue_binding` is read (lines 605 and 615), first:

```python
if self._environment.venue_binding is None:
    raise PilotRequestError(HTTPStatus.CONFLICT, "MANIFEST_NOT_ELIGIBLE")
```

- [x] **Step 1: Write the failing tests**

```python
def test_compose_from_an_empty_store_yields_a_blocked_but_valid_environment(tmp_path) -> None:
    store = PredictionMarketStore(tmp_path / "p.duckdb")
    env = compose_pilot_environment(
        store,
        account_fingerprint="a" * 64,
        wallet_fingerprint="a" * 64,
        credentials_present=False,
        now=lambda: NOW,
    )
    assert env.manifest is None
    assert env.manifest_state == "MISSING"
    assert env.venue_binding is None
    assert env.credentials_present is False
    assert env.executor_factory is None
    assert env.reconciliation.reconciliation_complete is False


def test_compose_reads_a_persisted_manifest_and_attestation(tmp_path) -> None:
    # seed a LIVE_DISABLED manifest + eligibility attestation via existing store appenders,
    # reuse tests/predictions/manifest_helpers.venue_manifest
    ...
    env = compose_pilot_environment(store, ...)
    assert env.manifest_state == "LIVE_DISABLED"
    assert env.venue_binding is not None
    assert env.eligibility_expires_at == attestation.expires_at


def test_registration_is_reachable_and_authorize_refuses_without_executor(tmp_path) -> None:
    # build_pilot_runtime with the composed environment + FakePasskeyService;
    # POST register options succeeds (not 409 PILOT_KILL_ENGAGED);
    # a forced authorize path returns EXECUTION_UNAVAILABLE (reuse Cockpit from
    # tests/predictions/test_pilot_end_to_end.py or a trimmed local copy).
    ...
```

Fill the seeded-store test with the real appender calls after reading `append_venue_manifest` and `append_pilot_eligibility_attestation` signatures — no fixtures invented.

- [x] **Step 2: Run to verify failure** — ImportError. FAIL.

- [x] **Step 3: Implement `launch.py`** per the Produces block. Read `qualification.evaluate_pilot_qualification` and store read methods first; qualifications: recompute per proof family from persisted scan reports and shadow experiments; return `()` when the store holds none.

- [x] **Step 4: Run** `.venv/bin/python -m pytest tests/predictions/test_pilot_launch.py tests/predictions/test_pilot_end_to_end.py -q` — PASS (end-to-end proves the Optional binding change breaks nothing).

- [x] **Step 5: Commit**

```bash
git add src/polytrading/predictions/pilot/launch.py src/polytrading/predictions/pilot/services.py tests/predictions/test_pilot_launch.py
git commit -m "feat(predictions): compose the pilot environment from persisted evidence"
```

### Task 5: Launch wiring in `serve_polymarket_pilot`

**Files:**
- Modify: `src/polytrading/predictions/pilot/runtime.py` (`serve_polymarket_pilot`, `runtime.py:305`)
- Modify: `src/polytrading/predictions/pilot/signer_link.py` (add a `describe_identity(channel) -> tuple[Sha256, Sha256]` helper that frames one `DESCRIBE_IDENTITY` request over the channel streams)
- Test: `tests/predictions/test_pilot_runtime.py` (extend)

**Interfaces:**
- Consumes: `launch_signer_sidecar` + `SignerChannel.credentials_present` (Task 2), `offline_pilot_signer_service` (Task 3), `compose_pilot_environment` (Task 4), `describe_identity` (this task).
- Produces: `serve_polymarket_pilot` behavior — try `MacOSKeychainSecretStore` bootstrap; on success, DESCRIBE over the channel, compose the environment, serve live services (still killed); on `SignerBootstrapError` or `SecretStoreError`, print one stable line to stderr (`pilot: signer unavailable (<CODE>); serving posture only`) and serve today's posture-only runtime. The channel is closed by `runtime.close()` (add it to the cleanup chain).

- [x] **Step 1: Write the failing tests**

Refactor target first: extract the composition into

```python
def build_launch_runtime(
    database_path: Path,
    port: int,
    *,
    platform: str = "darwin",
    bootstrap: Callable[[], tuple[SignerChannel, bool]] | None = None,
    now: Callable[[], datetime] | None = None,
) -> PilotRuntime: ...
```

so tests inject a fake bootstrap; `serve_polymarket_pilot` stays a thin server loop over it.

```python
def test_launch_composes_live_services_when_the_signer_bootstraps(tmp_path) -> None:
    runtime = build_launch_runtime(seeded_db, PORT, bootstrap=fake_bootstrap_with_identity)
    # presence is now reachable: not 409 PILOT_KILL_ENGAGED
    ...


def test_launch_falls_back_to_posture_only_when_secrets_are_missing(tmp_path, capsys) -> None:
    runtime = build_launch_runtime(seeded_db, PORT, bootstrap=raising_bootstrap)
    # presence still returns 409 PILOT_KILL_ENGAGED; stderr carries the stable line
    ...


def test_runtime_close_closes_the_signer_channel(tmp_path) -> None: ...
```

- [x] **Step 2: Run to verify failure** — `build_launch_runtime` missing. FAIL.

- [x] **Step 3: Implement.** `describe_identity` in `signer_link.py` reuses that module's existing frame read/write helpers and builds the `SignerRequest` with zeroed digests, `deadline=now+5s`, `protocol_version=POLYMARKET_PILOT_PROTOCOL_VERSION`. `build_launch_runtime` default `bootstrap` wires `MacOSKeychainSecretStore()` + `offline_pilot_signer_service`; refusal path catches only `SignerBootstrapError`/`SecretStoreError`.

- [x] **Step 4: Run** `.venv/bin/python -m pytest tests/predictions/test_pilot_runtime.py tests/predictions/test_pilot_end_to_end.py -q` — PASS.

- [x] **Step 5: Commit**

```bash
git add src/polytrading/predictions/pilot/runtime.py src/polytrading/predictions/pilot/signer_link.py tests/predictions/test_pilot_runtime.py
git commit -m "feat(predictions): wire the pilot launch to the signer and evidence"
```

### Task 6: Documentation and full verification

**Files:**
- Modify: `docs/predictions/polymarket-live-pilot.md` (section 1: note that a launch without enrolled secrets serves a read-only posture console and prints the stable stderr line; credential ceremony and any execution remain unavailable in this build because no venue transport exists)
- Modify: `docs/superpowers/plans/2026-08-30-polymarket-pilot-live-launch-wiring.md` (check boxes)

- [x] **Step 1: Update the runbook** with the two launch outcomes and the unchanged security posture (no transport, execution refuses `EXECUTION_UNAVAILABLE`).

- [ ] **Step 2: Full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: all green (≈4400+ tests).

- [ ] **Step 3: Graph update**

Run: `graphify update .`

- [x] **Step 4: Commit**

```bash
git add docs/predictions/polymarket-live-pilot.md docs/superpowers/plans/2026-08-30-polymarket-pilot-live-launch-wiring.md
git commit -m "docs(predictions): document the wired pilot launch"
```
