# Polymarket Live Execution Transport Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the pilot's deliberately unavailable signer into a capability-gated, signer-owned Polymarket execution path for manually approved, reconciled FAK/FOK strategies while keeping automation-session authority hard-disabled.

**Architecture:** The parent control plane remains loopback-only and secret-free. It sends a public signed capability proof to the signer with each mutation; the signer verifies it with the launch public key, consumes it locally, and alone creates the fixed Polymarket HTTPS transport. Startup reads use a narrowly allowlisted signer read path; every mutation requires the proof plus current independent signer state. Any anomaly or signed stop directive kills locally and leaves only read-only recovery.

**Tech Stack:** Python 3.13, Pydantic v2, `httpx`, Ed25519 (`cryptography`), DuckDB, macOS Keychain, pytest.

**Spec:** `docs/superpowers/specs/2026-08-27-polymarket-local-live-pilot-design.md`

## Global Constraints

- Never place wallet keys, CLOB credentials, request headers, signed venue payloads, raw venue bodies, Keychain values, or exception text in argv, env, DuckDB, browser payloads, logs, diagnostics, IPC errors, or tests.
- The parent control plane never constructs an authenticated Polymarket request; only the signer sidecar does.
- Production HTTP uses only frozen `ROUTE_SPECS` hosts, TLS defaults, `trust_env=False`, no redirects, empty cookie jars, bounded responses, and the existing finite `RestTimeouts`.
- No venue mutation receives a retry. Only the existing explicitly enumerated read routes may make one bounded retry.
- Every launch starts killed; eligibility, protocol, qualification, geoblock, reconciliation, activation, current limits, passkey approval, and signer authority must all pass before a mutation.
- The USD 250 wallet-equity ceiling, USD 10 order cap, USD 25 strategy cap, USD 50 session deployment cap, one concurrent strategy, USD 5 session loss, and USD 10 UTC-day loss remain compiled ceilings enforced at server, coordinator, and signer boundaries.
- Only FAK/FOK orders reach signing or HTTP. Transfers, deposits, withdrawals, approvals, redemptions, arbitrary URLs, and arbitrary headers remain unreachable.
- `AuthorizationMode.AUTOMATION_SESSION` is rejected at both parent and signer boundaries by a compiled constant. UI input and database state cannot enable it.
- A timeout, malformed response, unexpected resting order, auth error, protocol drift, stream gap, stale evidence, replay, transport error, or reconciliation mismatch engages kill; ambiguous orders are never re-submitted.
- Update `tests/predictions/test_execution_authority_scan.py` reviewed source hashes in the same commit as each reviewed `execution/` or `polymarket_execution/` source change.
- Automated verification uses only fake `httpx.MockTransport`, injected clocks, test keys, and temporary stores. It never contacts Polymarket, real Keychain entries, or a real wallet.

---

## File Structure

| File | Responsibility |
| --- | --- |
| `polymarket_execution/ipc.py` | Closed public capability-proof and signed-kill IPC records; request validation and canonical serialization. |
| `pilot/capabilities.py` | Mint a signed, kill-only revocation directive with the launch issuer key. |
| `polymarket_execution/signer.py` | Verify public proofs, consume one-time primary grants, apply signer-local kill, and dispatch only allowlisted work. |
| `pilot/signer_services.py` | Compose the live signer’s fixed REST handlers and its proof/read guards. |
| `polymarket_execution/rest.py` | Construct the production hardened HTTPX client while retaining the fake-only test constructor. |
| `pilot/signer_link.py` | Attach a signed proof to mutation IPC; send signed kill directives; expose only typed venue operations. |
| `pilot/reconciliation.py` | Read account/order/trade/allowance evidence through the signer and produce one sanitized reconciliation snapshot. |
| `pilot/launch.py` | Compose a live `PilotEnvironment` from signer reads and persisted evidence. |
| `pilot/runtime.py` | Build the issuer before the child, pass its public key into signer composition, and wire the execution-port factory. |

### Task 1: Public signer-authority records

**Files:**
- Modify: `src/polytrading/predictions/pilot/capabilities.py`
- Modify: `src/polytrading/predictions/polymarket_execution/ipc.py`
- Modify: `tests/predictions/test_pilot_capabilities.py`
- Modify: `tests/predictions/test_polymarket_signer_ipc.py`

**Interfaces:**
- Produces `SignerCapabilityProof(grant: CapabilityGrant, signature: bytes)` and `SignerKillDirective(capability_ids: tuple[UUID, ...], issued_at: datetime, signature: bytes)` as strict serializable IPC records.
- Produces `PilotCapabilityIssuer.issue_kill_directive(capability_ids: Iterable[UUID], issued_at: datetime) -> SignerKillDirective` and `verify_kill_directive(directive, public_key) -> bool`.
- `SignerRequest` gains `authority_proof: SignerCapabilityProof | None` and a `SIGNER_KILL` payload; identity and read-only requests require no proof, mutations require exactly one matching proof.

- [ ] **Step 1: Write failing tests**

```python
def test_mutation_request_requires_a_capability_proof() -> None:
    with pytest.raises(SignerProtocolError, match="IPC_MODEL_INVALID"):
        build_submit_request(authority_proof=None)


def test_kill_directive_verifies_only_for_the_launch_issuer() -> None:
    issuer = PilotCapabilityIssuer(key_id="test")
    directive = issuer.issue_kill_directive([uuid4()], issued_at=NOW)
    assert verify_kill_directive(directive, issuer.public_verification_key)
    assert not verify_kill_directive(directive, Ed25519PrivateKey.generate().public_key_bytes())
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `rtk .venv/bin/python -m pytest tests/predictions/test_pilot_capabilities.py tests/predictions/test_polymarket_signer_ipc.py -q -k "proof or directive"`

Expected: FAIL because the proof/directive records and issuer methods do not exist.

- [ ] **Step 3: Implement the closed records and signatures**

```python
class SignerCapabilityProof(_SignerRecord):
    grant: CapabilityGrant
    signature: Base64Bytes


class SignerKillPayload(_SignerRecord):
    operation: Literal[ExecutionOperation.SIGNER_KILL]
    directive: SignerKillDirective
```

Use the issuer’s existing Ed25519 private key to sign `canonical_execution_hash` of a directive with sorted, unique capability IDs. Bind the signature domain to `{"kind": "signer-kill-v1", ...}` so a grant signature cannot be replayed as a kill directive. Require the proof grant digest to equal `SignerRequest.capability_digest`, and require `authority_proof is None` only for `DESCRIBE_IDENTITY`, `SIGNER_KILL`, and the three read operations.

- [ ] **Step 4: Run the focused tests and verify GREEN**

Run: `rtk .venv/bin/python -m pytest tests/predictions/test_pilot_capabilities.py tests/predictions/test_polymarket_signer_ipc.py -q -k "proof or directive"`

Expected: PASS.

- [ ] **Step 5: Update the authority-source digest and commit**

Run: `rtk .venv/bin/python -m pytest tests/predictions/test_execution_authority_scan.py -q`

Commit:

```bash
git add src/polytrading/predictions/pilot/capabilities.py src/polytrading/predictions/polymarket_execution/ipc.py tests/predictions/test_pilot_capabilities.py tests/predictions/test_polymarket_signer_ipc.py tests/predictions/test_execution_authority_scan.py
git commit -m "feat(predictions): carry verified pilot authority to signer"
```

### Task 2: Signer-local proof verification and kill state

**Files:**
- Modify: `src/polytrading/predictions/polymarket_execution/signer.py`
- Modify: `tests/predictions/test_polymarket_signer_ipc.py`
- Modify: `tests/predictions/test_execution_authority_scan.py`

**Interfaces:**
- `SignerService(..., capability_public_key: bytes, ...)` verifies `SignerCapabilityProof` before `AuthorityContextFactory` runs.
- `SignerService` maintains `_consumed_primary_capabilities: set[UUID]` and `_kill_engaged: bool`.
- `SIGNER_KILL` verifies its directive, sets `_kill_engaged=True`, and returns only `SIGNER_KILL_ENGAGED`.

- [ ] **Step 1: Write failing tests**

```python
def test_signer_rejects_a_valid_grant_signed_by_another_issuer() -> None:
    service = build_live_service(issuer_public_key=ISSUER.public_verification_key)
    response = service.handle(build_submit_request(authority_proof=OTHER_ISSUER_PROOF))
    assert response.error_code == "CAPABILITY_SIGNATURE_INVALID"


def test_signer_consumes_a_primary_capability_before_handler_dispatch() -> None:
    service = build_live_service()
    assert service.handle(build_submit_request()).ok
    assert service.handle(build_submit_request(new_request_id=True)).error_code == "CAPABILITY_REPLAYED"


def test_signed_kill_blocks_a_previously_valid_mutation() -> None:
    service = build_live_service()
    assert service.handle(build_kill_request()).ok
    assert service.handle(build_submit_request()).error_code == "PILOT_KILL_ENGAGED"
```

- [ ] **Step 2: Run and verify RED**

Run: `rtk .venv/bin/python -m pytest tests/predictions/test_polymarket_signer_ipc.py -q -k "signed_kill or consumes_a_primary or another_issuer"`

Expected: FAIL because the signer has no public-key proof verifier or local kill state.

- [ ] **Step 3: Implement the signer gate before dispatch**

Add `_verify_proof(request, now)` which validates the proof signature against the inherited public key, compares the proof’s grant digest/account/manifest/protocol/operations/times with the request, rejects automation mode unconditionally, and converts the verified grant with `verified_capability_from_grant`. Call it before `_verify_mutation`; add the capability ID to the consumed set only after both verification steps allow the request and before `_dispatch`. `SIGNER_KILL` must verify the kill directive before setting local state and must never call a venue handler. Handler results whose existing route flags require a kill set `_kill_engaged=True` before returning their sanitized response.

- [ ] **Step 4: Run signer and scan tests**

Run: `rtk .venv/bin/python -m pytest tests/predictions/test_polymarket_signer_ipc.py tests/predictions/test_execution_authority_scan.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/polytrading/predictions/polymarket_execution/signer.py tests/predictions/test_polymarket_signer_ipc.py tests/predictions/test_execution_authority_scan.py
git commit -m "feat(predictions): enforce capability authority in signer"
```

### Task 3: Enable the closed production REST constructor

**Files:**
- Modify: `src/polytrading/predictions/polymarket_execution/rest.py`
- Modify: `tests/predictions/test_polymarket_execution_rest.py`
- Modify: `tests/predictions/test_execution_authority_scan.py`

**Interfaces:**
- `HttpxPolymarketRestTransport(timestamp, clock, retry_policy, sleeper, timeouts)` creates exactly one hardened `httpx.AsyncClient` with no injected production transport.
- `_for_test(httpx.MockTransport, ...)` remains the only fake network entrypoint used by tests.

- [ ] **Step 1: Write failing tests**

```python
def test_live_constructor_uses_no_proxy_redirect_cookie_or_ambient_environment(monkeypatch) -> None:
    captured = capture_async_client_arguments(monkeypatch)
    HttpxPolymarketRestTransport(timestamp=lambda: "1", clock=lambda: NOW)
    assert captured["follow_redirects"] is False
    assert captured["trust_env"] is False
    assert captured["cookies"] == {}


def test_live_constructor_rejects_non_default_test_transport_injection() -> None:
    with pytest.raises(TypeError):
        HttpxPolymarketRestTransport(transport=httpx.MockTransport(lambda _: RESPONSE))
```

- [ ] **Step 2: Run and verify RED**

Run: `rtk .venv/bin/python -m pytest tests/predictions/test_polymarket_execution_rest.py -q -k "live_constructor"`

Expected: FAIL because production construction always raises `LIVE_TRANSPORT_UNAVAILABLE`.

- [ ] **Step 3: Implement the production constructor**

Have `__init__` validate the existing timeout/retry arguments and build `httpx.AsyncClient(follow_redirects=False, trust_env=False, timeout=..., headers={}, cookies={})`. Keep `_initialize` as the mock-only path and do not add an arbitrary `transport`, URL, header, proxy, or verification parameter to the public constructor.

- [ ] **Step 4: Run REST tests and source scan**

Run: `rtk .venv/bin/python -m pytest tests/predictions/test_polymarket_execution_rest.py tests/predictions/test_execution_authority_scan.py -q`

Expected: PASS without a network attempt.

- [ ] **Step 5: Commit**

```bash
git add src/polytrading/predictions/polymarket_execution/rest.py tests/predictions/test_polymarket_execution_rest.py tests/predictions/test_execution_authority_scan.py
git commit -m "feat(predictions): construct hardened live REST transport"
```

### Task 4: Compose the live signer service

**Files:**
- Modify: `src/polytrading/predictions/pilot/signer_services.py`
- Modify: `src/polytrading/predictions/pilot/signer_bootstrap.py`
- Modify: `tests/predictions/test_pilot_signer_services.py`
- Modify: `tests/predictions/test_pilot_signer_bootstrap.py`

**Interfaces:**
- `live_pilot_signer_service(*, capability_public_key: bytes, clock: Callable[[], datetime]) -> SignerServiceFactory` is a closure safe to fork with the public key only.
- All CLOB credentials must be present to build `ClobCredentials`; missing credentials retain wallet-only identity/startup reads but reject authenticated trading reads and mutations as `CREDENTIALS_UNAVAILABLE`.

- [ ] **Step 1: Write failing tests**

```python
def test_live_factory_constructs_fixed_rest_handlers_only_when_credentials_exist() -> None:
    service = live_pilot_signer_service(capability_public_key=PUBLIC_KEY, clock=lambda: NOW)(FULL_SECRETS)
    assert service.handle(build_read_account_request()).error_code != "EXECUTION_UNAVAILABLE"


def test_wallet_only_live_factory_refuses_authenticated_operations_without_creating_transport() -> None:
    service = live_pilot_signer_service(capability_public_key=PUBLIC_KEY, clock=lambda: NOW)(WALLET_ONLY)
    assert service.handle(build_read_account_request()).error_code == "CREDENTIALS_UNAVAILABLE"
```

- [ ] **Step 2: Run and verify RED**

Run: `rtk .venv/bin/python -m pytest tests/predictions/test_pilot_signer_services.py tests/predictions/test_pilot_signer_bootstrap.py -q -k "live_factory or wallet_only"`

Expected: FAIL because only `offline_pilot_signer_service` exists.

- [ ] **Step 3: Implement the closed factory**

Create `ClobCredentials` only inside the child, create `HttpxPolymarketRestTransport`, wrap it in `SignerRestHandlers`, and pass its typed handlers to `SignerService`. The read guard permits only `READ_ACCOUNT`, `READ_ORDERS`, and `READ_TRADES` with the signed account fingerprint and a five-minute launch lifetime; it does not permit a mutation. Do not modify the offline factory: posture-only launches must remain socket-free.

- [ ] **Step 4: Run focused tests**

Run: `rtk .venv/bin/python -m pytest tests/predictions/test_pilot_signer_services.py tests/predictions/test_pilot_signer_bootstrap.py tests/predictions/test_polymarket_execution_rest.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/polytrading/predictions/pilot/signer_services.py src/polytrading/predictions/pilot/signer_bootstrap.py tests/predictions/test_pilot_signer_services.py tests/predictions/test_pilot_signer_bootstrap.py
git commit -m "feat(predictions): compose signer-owned venue transport"
```

### Task 5: Bind parent IPC calls to proof and kill directives

**Files:**
- Modify: `src/polytrading/predictions/pilot/signer_link.py`
- Modify: `tests/predictions/test_pilot_signer_link.py`

**Interfaces:**
- `SignerLinkVenuePort(..., proof_for: Callable[[UUID], SignerCapabilityProof], kill_directive: Callable[[Iterable[UUID]], SignerKillDirective])` supplies a proof only for `submit`/`cancel`.
- `SignerLinkVenuePort.engage_kill(capability_ids: Iterable[UUID]) -> None` sends `SIGNER_KILL`; no caller can attach an arbitrary payload or route.

- [ ] **Step 1: Write failing tests**

```python
def test_submit_serializes_the_matching_public_proof() -> None:
    port = build_port()
    port.submit(INTENT, CAPABILITY_ID)
    assert captured_request.authority_proof.grant.capability_id == CAPABILITY_ID


def test_engage_kill_sends_only_a_signed_kill_payload() -> None:
    build_port().engage_kill([CAPABILITY_ID])
    assert isinstance(captured_request.payload, SignerKillPayload)
```

- [ ] **Step 2: Run and verify RED**

Run: `rtk .venv/bin/python -m pytest tests/predictions/test_pilot_signer_link.py -q -k "proof or engage_kill"`

Expected: FAIL because the port emits a digest-only request and has no kill method.

- [ ] **Step 3: Implement typed request construction**

Set `authority_proof=None` for identity/read requests. For submit/cancel, look up the exact capability ID and reject a missing mapping before writing IPC. Make `_exchange` construct the control payload only from `engage_kill`; on control rejection raise `SignerLinkError` without exposing response data. Preserve the existing request-ID collision and stable error behavior.

- [ ] **Step 4: Run link and IPC tests**

Run: `rtk .venv/bin/python -m pytest tests/predictions/test_pilot_signer_link.py tests/predictions/test_polymarket_signer_ipc.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/polytrading/predictions/pilot/signer_link.py tests/predictions/test_pilot_signer_link.py
git commit -m "feat(predictions): bind signer calls to pilot grants"
```

### Task 6: Authoritative startup reconciliation

**Files:**
- Create: `src/polytrading/predictions/pilot/reconciliation.py`
- Modify: `src/polytrading/predictions/pilot/launch.py`
- Modify: `tests/predictions/test_pilot_launch.py`
- Create: `tests/predictions/test_pilot_reconciliation.py`

**Interfaces:**
- `reconcile_startup(port: VenueSubmissionPort, *, account_fingerprint: Sha256, now: Callable[[], datetime]) -> PilotReconciliationState` performs the fixed account, position, order, and trade reads and produces a hashed, sanitized state.
- It returns `reconciliation_complete=False` with `unknown_outcomes > 0` on every read failure, contradiction, open/resting order, or nonterminal trade; it never fabricates zero state.
- `compose_pilot_environment(..., venue_port: VenueSubmissionPort)` uses the port for `account_state` and the reconciliation result instead of `unavailable_account_state`.

- [ ] **Step 1: Write failing tests**

```python
def test_startup_reconciliation_marks_a_clean_authoritative_snapshot_complete() -> None:
    state = reconcile_startup(CLEAN_PORT, account_fingerprint=ACCOUNT, now=lambda: NOW)
    assert state.reconciliation_complete is True
    assert state.active_submissions == state.unknown_outcomes == 0


def test_startup_reconciliation_fails_closed_when_a_read_is_ambiguous() -> None:
    state = reconcile_startup(AMBIGUOUS_PORT, account_fingerprint=ACCOUNT, now=lambda: NOW)
    assert state.reconciliation_complete is False
    assert state.unknown_outcomes == 1
```

- [ ] **Step 2: Run and verify RED**

Run: `rtk .venv/bin/python -m pytest tests/predictions/test_pilot_reconciliation.py tests/predictions/test_pilot_launch.py -q -k "reconciliation"`

Expected: FAIL because startup always creates a `transport-unavailable` reconciliation state.

- [ ] **Step 3: Implement the fail-closed reconciler**

Call only `VenueSubmissionPort.account_state()` and `.positions()` plus the existing typed read-order/read-trade link operations. Hash only sorted public IDs, sanitized result codes, and observation times. Count any unexpected live order, nonterminal trade, missing account/position response, or read exception as unknown. Do not clear kill, issue a grant, or place/cancel an order in this task.

- [ ] **Step 4: Run reconciliation and launch tests**

Run: `rtk .venv/bin/python -m pytest tests/predictions/test_pilot_reconciliation.py tests/predictions/test_pilot_launch.py tests/predictions/test_pilot_read_models.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/polytrading/predictions/pilot/reconciliation.py src/polytrading/predictions/pilot/launch.py tests/predictions/test_pilot_reconciliation.py tests/predictions/test_pilot_launch.py
git commit -m "feat(predictions): reconcile signer account at pilot startup"
```

### Task 7: Wire the capability-gated executor

**Files:**
- Modify: `src/polytrading/predictions/pilot/execution_port.py`
- Modify: `src/polytrading/predictions/pilot/services.py`
- Modify: `src/polytrading/predictions/pilot/sessions.py`
- Modify: `tests/predictions/test_pilot_execution_port.py`
- Modify: `tests/predictions/test_pilot_execution.py`

**Interfaces:**
- `CoordinatorExecutionPort.engage_kill(reason)` revokes primary grants and calls `SignerLinkVenuePort.engage_kill` before returning.
- `LivePilotServices._run` supplies an executor factory whose proof mapping contains only the grants just issued, and whose evidence closure reads current reconciled state/presence/manifest.
- `AUTOMATION_SESSION` yields `AUTOMATION_NOT_ACTIVATED` before issuance and again in executor construction.

- [ ] **Step 1: Write failing tests**

```python
def test_coordinator_kill_propagates_to_the_signer_before_returning() -> None:
    port.engage_kill("UNKNOWN_OUTCOME")
    assert fake_signer.kill_calls == [(PRIMARY_ID, RECOVERY_ID)]


def test_live_service_refuses_automation_even_with_a_valid_passkey() -> None:
    with pytest.raises(PilotRequestError, match="AUTOMATION_NOT_ACTIVATED"):
        services.auth_options({"mode": "AUTOMATION_SESSION", "opportunity_id": OPPORTUNITY})
```

- [ ] **Step 2: Run and verify RED**

Run: `rtk .venv/bin/python -m pytest tests/predictions/test_pilot_execution_port.py tests/predictions/test_pilot_execution.py -q -k "propagates_to_the_signer or automation"`

Expected: FAIL because parent kill does not reach the signer and automation is issuable.

- [ ] **Step 3: Implement the wiring**

Add the typed `engage_kill` method to `VenueSubmissionPort`; call it exactly once under a best-effort `finally` after coordinator primary revocation, retaining parent kill even if the IPC operation fails. In `LivePilotServices._build_challenge`, reject automation before challenge persistence. Construct `CoordinatorExecutionPort` with the signed grant-to-proof map and an evidence closure that treats an unavailable account or stale reconciliation as killed. Keep the existing server-selected opportunity/plan behavior unchanged.

- [ ] **Step 4: Run focused execution tests**

Run: `rtk .venv/bin/python -m pytest tests/predictions/test_pilot_execution_port.py tests/predictions/test_pilot_execution.py tests/predictions/test_pilot_runtime.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/polytrading/predictions/pilot/execution_port.py src/polytrading/predictions/pilot/services.py src/polytrading/predictions/pilot/sessions.py tests/predictions/test_pilot_execution_port.py tests/predictions/test_pilot_execution.py
git commit -m "feat(predictions): wire manual pilot execution authority"
```

### Task 8: Live launch composition and recovery gates

**Files:**
- Modify: `src/polytrading/predictions/pilot/runtime.py`
- Modify: `src/polytrading/predictions/pilot/launch.py`
- Modify: `src/polytrading/predictions/cli.py`
- Modify: `tests/predictions/test_pilot_runtime.py`
- Modify: `tests/predictions/test_pilot_end_to_end.py`

**Interfaces:**
- `build_launch_runtime` creates `PilotCapabilityIssuer` before launching the signer and passes its public key to `live_pilot_signer_service`.
- `PilotRuntime` owns the issuer and channel together; shutdown sends signed kill, closes the signer first, then destroys issuer key material.
- `predictions pilot polymarket --db ... --port ...` gains no flags and performs no venue request until the user starts the local server and the signer receives an allowlisted startup read.

- [ ] **Step 1: Write failing tests**

```python
def test_launch_composes_live_session_from_signer_account_state(tmp_path) -> None:
    runtime = build_launch_runtime(tmp_path / "pilot.duckdb", 8081, bootstrap=live_bootstrap)
    assert session_response(runtime.application).status is HTTPStatus.OK


def test_launch_remains_killed_when_reconciliation_is_not_complete(tmp_path) -> None:
    runtime = build_launch_runtime(tmp_path / "pilot.duckdb", 8081, bootstrap=ambiguous_bootstrap)
    assert readiness(runtime.application)["kill_engaged"] is True
```

- [ ] **Step 2: Run and verify RED**

Run: `rtk .venv/bin/python -m pytest tests/predictions/test_pilot_runtime.py tests/predictions/test_pilot_end_to_end.py -q -k "live_session_from_signer or reconciliation_is_not_complete"`

Expected: FAIL because launch still composes an unavailable account-state closure.

- [ ] **Step 3: Implement launch sequencing**

Create the issuer first, launch the child through `live_pilot_signer_service(capability_public_key=issuer.public_verification_key, clock=clock)`, describe identity, create a typed `SignerLinkVenuePort`, reconcile, then compose `PilotEnvironment` and the executor factory. Any bootstrap/read/reconciliation exception closes the channel, closes the issuer, and falls back to killed posture without logging exception text. Do not add CLI credentials or activation bypass flags.

- [ ] **Step 4: Run launch, runtime, and end-to-end tests**

Run: `rtk .venv/bin/python -m pytest tests/predictions/test_pilot_launch.py tests/predictions/test_pilot_runtime.py tests/predictions/test_pilot_end_to_end.py tests/predictions/test_pilot_server.py -q`

Expected: PASS; a test fake transport proves live-session is a 200 for a clean snapshot, while all fresh launches remain killed.

- [ ] **Step 5: Commit**

```bash
git add src/polytrading/predictions/pilot/runtime.py src/polytrading/predictions/pilot/launch.py src/polytrading/predictions/cli.py tests/predictions/test_pilot_runtime.py tests/predictions/test_pilot_end_to_end.py
git commit -m "feat(predictions): launch reconciled live pilot"
```

### Task 9: Recovery, secret, and whole-system verification

**Files:**
- Modify: `docs/predictions/polymarket-live-pilot.md`
- Modify: `docs/predictions/polymarket-execution-hardening.md`
- Modify: affected tests named below

**Interfaces:**
- Documents state that code remains killed until persisted 45-day qualification, 30-day shadow execution, current eligibility/KYC/geoblock, protocol review, manual funding/allowance, exact reconciliation, passkey activation, and explicit operator action all succeed.

- [ ] **Step 1: Write regression tests for safety invariants**

```python
def test_ambiguous_submit_engages_parent_and_signer_kill_without_retry() -> None:
    result = execute_with(ORDER_OUTCOME_UNKNOWN)
    assert result.stop_reason == "UNKNOWN_OUTCOME"
    assert fake_transport.submit_attempts == 1
    assert fake_signer.killed


def test_live_transport_and_ipc_never_expose_secret_canaries() -> None:
    transcript = run_with_canary_credentials()
    assert all(canary not in transcript for canary in CANARIES)
```

- [ ] **Step 2: Run and verify RED if any invariant is missing**

Run: `rtk .venv/bin/python -m pytest tests/predictions/test_execution_secret_scan.py tests/predictions/test_polymarket_secret_boundary.py tests/predictions/test_execution_recovery.py tests/predictions/test_pilot_acceptance.py -q`

Expected: PASS only after the preceding tasks; add the smallest missing regression test before changing behavior.

- [ ] **Step 3: Update runbooks**

Document the preflight order: verify evidence → unlock Keychain locally → launch killed → inspect clean reconciliation → register/verify passkey → activate only if every gate passes → manually authorize one bounded complete strategy → inspect reconciliation → stop/kill on uncertainty. State explicitly that credentials/keys must never be pasted into terminal/UI and that automation is unavailable.

- [ ] **Step 4: Run focused and full verification**

Run:

```bash
rtk .venv/bin/python -m pytest tests/predictions/test_polymarket_execution_rest.py tests/predictions/test_polymarket_signer_ipc.py tests/predictions/test_pilot_signer_services.py tests/predictions/test_pilot_signer_link.py tests/predictions/test_pilot_reconciliation.py tests/predictions/test_pilot_launch.py tests/predictions/test_pilot_execution_port.py tests/predictions/test_pilot_execution.py tests/predictions/test_pilot_runtime.py tests/predictions/test_pilot_end_to_end.py tests/predictions/test_execution_authority_scan.py tests/predictions/test_execution_secret_scan.py -q
rtk .venv/bin/python -m pytest -q
```

Expected: all tests pass; no command makes an authenticated live request.

- [ ] **Step 5: Commit**

```bash
git add docs/predictions/polymarket-live-pilot.md docs/predictions/polymarket-execution-hardening.md tests/predictions
git commit -m "docs(predictions): document gated live-pilot operations"
```

## Plan Self-Review

- Spec coverage: Tasks 1–2 establish independent signer verification and local kill; Tasks 3–5 establish fixed signer-only transport and proof-bound IPC; Tasks 6–8 establish authoritative startup reconciliation, manual execution, launch wiring, and hard-disabled automation; Task 9 records operations and verifies secrecy/recovery. The existing evidence/eligibility/activation gates remain prerequisites, not bypassed work items.
- Scope: no generic exchange layer, WebSocket live activation, funding, allowance mutation, transfer, credential CLI import, remote API, or UI redesign is added.
- Type consistency: all mutation IPC uses `SignerCapabilityProof`; only `PilotCapabilityIssuer` creates `SignerKillDirective`; only `SignerLinkVenuePort.engage_kill` sends it; `VenueSubmissionPort` is the sole parent-side signer interface.
- Placeholder scan: no unresolved implementation placeholders or deferred behavior are present. The plan’s only intentional non-execution state is the compiled automation gate.
