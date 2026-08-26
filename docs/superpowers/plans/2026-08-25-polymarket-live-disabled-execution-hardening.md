# Polymarket Live-Disabled Execution Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement protocol-compatible Polymarket authentication, immediate-order state
machines, signer isolation, live-ledger reconciliation, offline conformance, and a polished
read-only Market Atlas operator console while keeping every production execution path structurally
LIVE_DISABLED.

**Architecture:** A venue-neutral execution package owns immutable plans, intents, authority,
kill state, lifecycle, ledger, and reconciliation. A separate Polymarket execution package owns
the frozen protocol snapshot, EIP-712/HMAC encoding, typed REST/WebSocket operations, and a
secret-bearing signer sidecar. A loopback-only dashboard publishes immutable one-cutoff snapshots;
an SSE endpoint emits revision notifications only, and the browser atomically replaces its full
snapshot after each notification. Production construction has no capability issuer or verification
key, starts killed, exposes no live CLI, and cannot reach authenticated transport. The browser is
an observer and never connects to Polymarket, the signer, or execution IPC.

**Tech Stack:** Python 3.12–3.14, Pydantic 2.13.4, DuckDB 1.5.4, httpx 0.28.1, websockets
17.0.1, eth-account 0.13.7, pytest 9.1.1, Hypothesis 6.160.0, Ruff 0.15.22.

**Spec:** docs/superpowers/specs/2026-08-25-polymarket-live-disabled-execution-hardening-design.md

## Global Constraints

- Polymarket only. Kalshi, Limitless, and cross-venue live coordination are outside this plan.
- Shipped venue manifests remain LIVE_DISABLED and production construction starts with the
  account-scoped kill switch engaged.
- No production capability issuer, configured verification key, capability generator, activation
  CLI, credential CLI, signer CLI, order CLI, cancel CLI, heartbeat CLI, or kill-clear CLI.
- Every order, cancellation, and heartbeat requires a verified unexpired capability at both
  coordinator and signer boundaries; evaluate_execution_gate must also pass independently at both
  boundaries.
- New orders are FAK or FOK only. GTC and GTD are rejected before signing.
- Secrets enter only the signer through inherited descriptors. Never put private keys, CLOB API
  secret/passphrase, auth headers, or authenticated WebSocket frames in arguments, environment
  variables, IPC requests, DuckDB, logs, exceptions, or dashboard payloads.
- Never blindly retry an order POST. Ambiguous transport outcomes become UNKNOWN, engage the kill
  switch, and require authoritative reads.
- User WebSocket events accelerate observation but never supersede REST order/trade/account reads.
- Live P&L is unavailable until exact ledger and venue reconciliation.
- Official-source and protocol fixture hashes are frozen. A changed hash produces
  PROTOCOL_REVIEW_REQUIRED.
- All money and token quantities use Decimal. All timestamps are timezone-aware UTC. All persisted
  records use canonical JSON plus SHA-256 and are append-only.
- No authenticated or order-submission network smoke test is permitted while LIVE_DISABLED.
- Market Atlas is strictly read-only. It exposes only GET/HEAD observer routes, contains no order,
  cancel, activation, kill-clear, credential, signer, or capability control, and sends no state.
- `/api/v1/predictions-events` carries revision metadata only. Every refresh fetches a complete
  `/api/v1/predictions-dashboard` snapshot built from one database cutoff; the client never patches
  authoritative totals from SSE payloads or combines data from different cutoffs.
- Browser code never opens a venue WebSocket, signer/IPC channel, or external authenticated
  connection. It uses vanilla ES modules, EventSource plus bounded GET polling fallback, and inline
  SVG; add no frontend framework or build pipeline.
- The console has exactly five primary views—Overview, Markets, Execution, Ledger, and Evidence—
  and exposes CONNECTED, DEGRADED, STALE, DISCONNECTED, and INCONSISTENT connection/data states.
- The console must remain usable at desktop, tablet, and mobile widths; support keyboard
  navigation, visible focus, semantic landmarks, sufficient contrast, an accessible live-region,
  and `prefers-reduced-motion`.
- Use .venv/bin/python -m pytest, not a bare pytest executable, so tests.* helper imports resolve.
- The direct cryptography dependency is exactly eth-account==0.13.7, the current stable release
  selected for this plan; do not use the 0.14 beta or implement EIP-712/secp256k1 manually.
- Preserve the user's pre-existing .claude/settings.json modification and stage only task files.

---

## File and Responsibility Map

### Venue-neutral execution package

- src/polytrading/predictions/execution/models.py — immutable plans, intents, signed envelopes,
  venue order/trade events, activation evidence, conformance results, and shared enums.
- src/polytrading/predictions/execution/authority.py — capability bundle/verifier contracts,
  production-unavailable verifier, dual-gate decision, and stable reason codes.
- src/polytrading/predictions/execution/kill_switch.py — append-only kill-state derivation and
  fail-closed mutation guard; no production clearance function.
- src/polytrading/predictions/execution/coordinator.py — persist-before-submit lifecycle,
  post-fill revalidation, UNKNOWN recovery, restart recovery, and injected protocol/risk ports.
- src/polytrading/predictions/execution/ledger.py — double-entry live postings from confirmed venue
  facts.
- src/polytrading/predictions/execution/reconciliation.py — authoritative account snapshot
  comparison and P&L publication gate.

### Polymarket execution package

- src/polytrading/predictions/polymarket_execution/protocol.py — frozen protocol/source manifest,
  route-set version, status mappings, and snapshot validation.
- src/polytrading/predictions/polymarket_execution/order.py — amount rounding, canonical EIP-712
  order typed data, stable salt/fingerprint, signing, and signer recovery.
- src/polytrading/predictions/polymarket_execution/auth.py — ClobAuth L1 typed data and exact-byte
  L2 HMAC headers.
- src/polytrading/predictions/polymarket_execution/routes.py — closed REST operation allowlist and
  typed request/response models.
- src/polytrading/predictions/polymarket_execution/rest.py — injected REST transport, production
  httpx implementation with no order-POST retry, and sanitized response classification.
- src/polytrading/predictions/polymarket_execution/user_stream.py — authenticated user-channel
  parsing, ping/pong health, disconnect/gap signals, and typed event conversion.
- src/polytrading/predictions/polymarket_execution/heartbeat.py — heartbeat state and uncertainty
  classification.
- src/polytrading/predictions/polymarket_execution/ipc.py — bounded length-prefixed local IPC
  schemas and replay/collision validation.
- src/polytrading/predictions/polymarket_execution/secrets.py — inherited-descriptor secret loading,
  zeroization best effort, and redaction.
- src/polytrading/predictions/polymarket_execution/signer.py — capability-gated signer service and
  sidecar process entry function, intentionally not registered as a CLI.
- src/polytrading/predictions/polymarket_execution/conformance.py — offline fixture runner and
  structured report rendering.
- src/polytrading/predictions/polymarket_execution/fixtures/ — bundled protocol_v1.json,
  sources_v1.json, and official-example-derived request/event vectors.

### Existing integration points

- src/polytrading/predictions/storage/schema/008_live_execution.sql — append-only execution tables.
- src/polytrading/predictions/storage/store.py — typed append/query methods and transaction support.
- src/polytrading/predictions/cli.py — offline conformance command only.
- src/polytrading/predictions/dashboard_models.py and dashboard.py — immutable one-cutoff Market
  Atlas snapshot models and read-side aggregation.
- src/polytrading/predictions/dashboard_live.py — deterministic revision calculation, changed-domain
  classification, bounded replay buffer, coalescing, and reset decisions.
- src/polytrading/predictions/dashboard_server.py — loopback GET/HEAD snapshot/static routes and the
  read-only SSE revision stream; no command transport.
- src/polytrading/predictions/web_assets/index.html and app.css — semantic Market Atlas shell,
  visual tokens, responsive layouts, connection states, focus, and reduced-motion behavior.
- src/polytrading/predictions/web_assets/app.js, api.js, stream.js, store.js, charts.js, views.js —
  browser bootstrap, GET-only snapshot client, SSE/poll fallback, atomic state replacement, inline
  SVG charts, and the five read-only views.
- pyproject.toml — exact eth-account pin and bundled fixture package data.
- README.md and docs/predictions/polymarket-execution-hardening.md — operational boundary,
  recovery, and remaining calendar/eligibility gates.

---

### Task 1: Venue-neutral execution domain records

**Files:**
- Create: src/polytrading/predictions/execution/__init__.py
- Create: src/polytrading/predictions/execution/models.py
- Create: tests/predictions/execution_helpers.py
- Test: tests/predictions/test_execution_models.py

**Interfaces:**
- Produces: ImmediateOrderType, ExecutionOperation, VenueOrderState, VenueTradeState,
  LiveExecutionPlan, ExecutionIntent, SignedOrderEnvelope, VenueOrderEvent, VenueTradeEvent,
  ActivationEvidence, KillSwitchEvent, LiveLedgerPosting, LiveReconciliation,
  ProtocolConformanceResult, deterministic_intent_id(), canonical_execution_hash().
- All later tasks import these names; do not rename them in later tasks.

- [ ] **Step 1: Write failing model and invariant tests**

~~~python
# tests/predictions/test_execution_models.py
from dataclasses import replace
from decimal import Decimal

import pytest
from pydantic import ValidationError

from polytrading.predictions.execution.models import (
    ExecutionIntent,
    ImmediateOrderType,
    SignedOrderEnvelope,
    deterministic_intent_id,
)
from tests.predictions.execution_helpers import execution_intent_fields


def test_intent_accepts_only_immediate_order_types() -> None:
    intent = ExecutionIntent(**execution_intent_fields(order_type=ImmediateOrderType.FAK))
    assert intent.order_type is ImmediateOrderType.FAK
    with pytest.raises(ValidationError):
        ExecutionIntent(**execution_intent_fields(order_type="GTC"))


def test_intent_identity_is_stable_and_content_bound() -> None:
    fields = execution_intent_fields()
    first = ExecutionIntent(**fields)
    second = ExecutionIntent(**fields)
    changed = ExecutionIntent(**execution_intent_fields(limit_price=Decimal("0.52")))
    assert first.intent_id == second.intent_id == deterministic_intent_id(first)
    assert changed.intent_id != first.intent_id


def test_signed_envelope_rejects_a_mismatched_intent_fingerprint() -> None:
    intent = ExecutionIntent(**execution_intent_fields())
    with pytest.raises(ValidationError, match="intent fingerprint"):
        SignedOrderEnvelope(
            schema_version=1,
            intent_id=intent.intent_id,
            intent_fingerprint="0" * 64,
            protocol_version="polymarket-clob-2026-08-25-v1",
            salt=1,
            signature_type=0,
            public_signature="0x" + "11" * 65,
            domain_fingerprint="1" * 64,
            exact_body_hash="2" * 64,
            order_fingerprint="3" * 64,
            signer_version="1",
            canonical_order_json="{}",
        )
~~~

- [ ] **Step 2: Run the tests and confirm the missing package failure**

Run: .venv/bin/python -m pytest tests/predictions/test_execution_models.py -v

Expected: FAIL with ModuleNotFoundError for polytrading.predictions.execution.

- [ ] **Step 3: Implement strict immutable records and deterministic identities**

~~~python
# src/polytrading/predictions/execution/models.py
class ImmediateOrderType(StrEnum):
    FAK = "FAK"
    FOK = "FOK"


class ExecutionOperation(StrEnum):
    SIGN_ORDER = "SIGN_ORDER"
    SUBMIT_ORDER = "SUBMIT_ORDER"
    CANCEL_ORDER = "CANCEL_ORDER"
    HEARTBEAT = "HEARTBEAT"
    READ_ORDERS = "READ_ORDERS"
    READ_TRADES = "READ_TRADES"
    READ_ACCOUNT = "READ_ACCOUNT"


class VenueOrderState(StrEnum):
    PLANNED = "PLANNED"
    SIGNED = "SIGNED"
    SUBMITTING = "SUBMITTING"
    ACK_LIVE_UNEXPECTED = "ACK_LIVE_UNEXPECTED"
    ACK_MATCHED = "ACK_MATCHED"
    ACK_DELAYED = "ACK_DELAYED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_PENDING = "CANCEL_PENDING"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"
    RECONCILED = "RECONCILED"


class VenueTradeState(StrEnum):
    MATCHED_NOT_BROADCASTED = "MATCHED_NOT_BROADCASTED"
    MATCHED = "MATCHED"
    MINED = "MINED"
    CONFIRMED = "CONFIRMED"
    RETRYING = "RETRYING"
    FAILED = "FAILED"


def canonical_execution_hash(value: PredictionRecord | Mapping[str, object]) -> Sha256:
    payload = value.model_dump(mode="json") if isinstance(value, PredictionRecord) else value
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return sha256(encoded.encode()).hexdigest()
~~~

Implement every field from design sections 7.1–7.5. Validators must enforce Polymarket-only
plans, FAK/FOK-only intents, sorted unique lineage hashes, UTC deadlines, positive finite Decimal
limits, order/trade terminal rules, one nonzero ledger posting side, and complete reconciliation
only when unexplained differences are empty. deterministic_intent_id() must be UUIDv5 over the
canonical intent content excluding intent_id itself.

- [ ] **Step 4: Add reusable exact record factories**

~~~python
# tests/predictions/execution_helpers.py
def execution_intent_fields(**overrides: object) -> dict[str, object]:
    fields: dict[str, object] = {
        "schema_version": 1,
        "intent_id": UUID("27d29661-47ff-5f4e-8136-92c9f4f9a782"),
        "plan_id": UUID("0d7c250b-0a21-55f3-a897-8bc98c59f904"),
        "leg_sequence": 0,
        "venue": PredictionVenue.POLYMARKET,
        "token_id": "217426",
        "side": "buy",
        "limit_price": Decimal("0.51"),
        "base_size": Decimal("10"),
        "maximum_spend": Decimal("5.10"),
        "order_type": ImmediateOrderType.FAK,
        "fee_rate_bps_cap": 100,
        "rounding_mode": "ROUND_DOWN",
        "account_fingerprint": "a" * 64,
        "capability_fingerprint": "b" * 64,
        "created_at": datetime(2026, 8, 25, 16, tzinfo=UTC),
        "deadline": datetime(2026, 8, 25, 16, 0, 5, tzinfo=UTC),
        "protocol_version": "polymarket-clob-2026-08-25-v1",
        "intent_fingerprint": "c" * 64,
    }
    fields.update(overrides)
    return fields
~~~

Have the helper derive the expected UUID and fingerprint through production functions rather than
hard-coding inconsistent values; use model_construct only to compute the content projection, then
validate through the public constructor.

- [ ] **Step 5: Run focused tests**

Run: .venv/bin/python -m pytest tests/predictions/test_execution_models.py -v

Expected: PASS.

- [ ] **Step 6: Commit**

~~~bash
git add src/polytrading/predictions/execution tests/predictions/execution_helpers.py tests/predictions/test_execution_models.py
git commit -m "feat(predictions): add live execution domain records"
~~~

---

### Task 2: Migration 008 and append-only execution storage

**Files:**
- Create: src/polytrading/predictions/storage/schema/008_live_execution.sql
- Modify: src/polytrading/predictions/storage/store.py
- Test: tests/predictions/test_execution_store.py
- Modify test: tests/predictions/test_store.py

**Interfaces:**
- Consumes: all persisted records from Task 1.
- Produces on PredictionMarketStore: append_live_execution_plan(), append_execution_intent(),
  append_signed_order_envelope(), append_venue_order_event(), append_venue_trade_event(),
  append_live_ledger_posting(), append_live_reconciliation(), append_kill_switch_event(),
  append_activation_evidence(), append_protocol_conformance_result(), plus verified_* query methods
  by plan, intent, account, and as-of cutoff.

- [ ] **Step 1: Write migration and immutable-retry tests**

~~~python
def test_migration_008_creates_all_execution_tables(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    names = {
        row[0]
        for row in store.connection.execute(
            "SELECT table_name FROM information_schema.tables"
        ).fetchall()
    }
    assert {
        "live_execution_plans",
        "execution_intents",
        "signed_order_envelopes",
        "venue_order_events",
        "venue_trade_events",
        "live_ledger_postings",
        "live_reconciliations",
        "execution_kill_events",
        "activation_evidence",
        "protocol_conformance_results",
    } <= names


def test_intent_retry_is_idempotent_but_conflicting_content_fails(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    intent = execution_intent()
    assert store.append_execution_intent(intent)
    assert not store.append_execution_intent(intent)
    with pytest.raises(ConflictingRecordError):
        store.append_execution_intent(intent.model_copy(update={"limit_price": Decimal("0.52")}))
~~~

- [ ] **Step 2: Run tests and confirm migration/version failures**

Run: .venv/bin/python -m pytest tests/predictions/test_execution_store.py tests/predictions/test_store.py::test_migrations_are_contiguous -v

Expected: FAIL because migration 008 and storage methods do not exist.

- [ ] **Step 3: Create append-only tables**

~~~sql
CREATE TABLE live_execution_plans (
    plan_id UUID PRIMARY KEY,
    proposal_id UUID NOT NULL,
    account_fingerprint VARCHAR NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    information_cutoff TIMESTAMPTZ NOT NULL,
    record_json VARCHAR NOT NULL,
    record_hash VARCHAR NOT NULL
);

CREATE TABLE execution_intents (
    intent_id UUID PRIMARY KEY,
    plan_id UUID NOT NULL,
    account_fingerprint VARCHAR NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    deadline TIMESTAMPTZ NOT NULL,
    record_json VARCHAR NOT NULL,
    record_hash VARCHAR NOT NULL
);
~~~

Add the remaining eight tables using the same identity/timestamp/record_json/record_hash pattern.
Use event_id, trade_event_id, posting_id, reconciliation_id, kill_event_id, activation_evidence_id,
and conformance_result_id as primary identities. signed_order_envelopes uses intent_id as its
primary identity so one intent cannot acquire two envelopes.

- [ ] **Step 4: Add generic canonical append/query helpers and typed public methods**

~~~python
def append_execution_intent(self, record: ExecutionIntent) -> bool:
    return self._append_hashed_record(
        table="execution_intents",
        identity_column="intent_id",
        identity=record.intent_id,
        columns=("intent_id", "plan_id", "account_fingerprint", "created_at", "deadline"),
        values=(
            record.intent_id,
            record.plan_id,
            record.account_fingerprint,
            record.created_at,
            record.deadline,
        ),
        record=record,
    )


def verified_execution_intents_for_plan(
    self, plan_id: UUID, as_of: datetime
) -> tuple[ExecutionIntent, ...]:
    return self._verified_records(
        table="execution_intents",
        model=ExecutionIntent,
        where="plan_id = ? AND created_at <= ?",
        parameters=(plan_id, as_of),
        order_by="created_at, intent_id",
    )
~~~

The private helpers must re-parse strict Pydantic records and recompute record_hash before return.
All verified queries apply their record's own information cutoff/deadline rules, not SQL time alone.

- [ ] **Step 5: Test transaction rollback, upgrade/reopen, and as-of isolation**

Add tests proving one transaction rolls back plan+intent+event together, an existing migration-007
database upgrades without altered prior hashes, a reopened read-only store can query readiness,
later events are invisible to an earlier as_of, and raw seeded secret canaries never appear in
record_json.

Run: .venv/bin/python -m pytest tests/predictions/test_execution_store.py tests/predictions/test_store.py -v

Expected: PASS.

- [ ] **Step 6: Commit**

~~~bash
git add src/polytrading/predictions/storage/schema/008_live_execution.sql src/polytrading/predictions/storage/store.py tests/predictions/test_execution_store.py tests/predictions/test_store.py
git commit -m "feat(predictions): persist append-only live execution evidence"
~~~

---

### Task 3: Dual authority gate and fail-closed kill switch

**Files:**
- Create: src/polytrading/predictions/execution/authority.py
- Create: src/polytrading/predictions/execution/kill_switch.py
- Test: tests/predictions/test_execution_authority.py
- Test: tests/predictions/test_execution_kill_switch.py

**Interfaces:**
- Produces: ExecutionCapability, VerifiedExecutionCapability, CapabilityVerifier Protocol,
  UnavailableProductionCapabilityVerifier, AuthorityContext, AuthorityDecision,
  verify_mutation_authority(), derive_kill_state(), require_mutation_allowed().
- verify_mutation_authority() is called independently by coordinator and signer with the same
  inputs; no shared cached pass result.

- [ ] **Step 1: Write failing authority tests**

~~~python
def test_production_verifier_always_rejects_without_a_configured_key() -> None:
    decision = UnavailableProductionCapabilityVerifier().verify(
        capability_bundle=b"fixture", now=NOW
    )
    assert decision.allowed is False
    assert decision.reason == "CAPABILITY_VERIFIER_NOT_CONFIGURED"


def test_live_disabled_manifest_cannot_be_overridden_by_valid_fixture_capability() -> None:
    context = authority_context(
        manifest=venue_manifest(implementation_state=AdapterImplementationState.LIVE_DISABLED),
        verified_capability=verified_capability(),
    )
    decision = verify_mutation_authority(context, ExecutionOperation.SUBMIT_ORDER)
    assert decision == AuthorityDecision(
        allowed=False,
        reason="LIVE_NOT_ELIGIBLE",
        evidence_hashes=context.evidence_hashes,
    )
~~~

- [ ] **Step 2: Run tests and confirm missing modules**

Run: .venv/bin/python -m pytest tests/predictions/test_execution_authority.py tests/predictions/test_execution_kill_switch.py -v

Expected: FAIL with missing authority/kill_switch modules.

- [ ] **Step 3: Implement the capability and dual-gate contracts**

~~~python
class CapabilityVerifier(Protocol):
    def verify(
        self, *, capability_bundle: bytes, now: datetime
    ) -> AuthorityDecision | VerifiedExecutionCapability: ...


class UnavailableProductionCapabilityVerifier:
    def verify(
        self, *, capability_bundle: bytes, now: datetime
    ) -> AuthorityDecision:
        return AuthorityDecision(
            allowed=False,
            reason="CAPABILITY_VERIFIER_NOT_CONFIGURED",
            evidence_hashes=(),
        )


def verify_mutation_authority(
    context: AuthorityContext, operation: ExecutionOperation
) -> AuthorityDecision:
    manifest = evaluate_execution_gate(context.manifest, venue=PredictionVenue.POLYMARKET)
    if not manifest.allowed:
        return AuthorityDecision(False, manifest.reason, manifest.manifest_source_hashes)
    return _verify_capability_fields(context, operation)
~~~

ExecutionCapability contains every field from design section 6. Validate canonical bundle bytes,
signature presence, venue/account/manifest/policy/protocol/route hashes, allowed operations,
not-before/expiration, limits, and activation nonce. This task defines the verifier interface but
does not select a production signature scheme and does not add any issuer to src/.

- [ ] **Step 4: Implement append-only kill derivation**

~~~python
@dataclass(frozen=True)
class KillState:
    engaged: bool
    latest_event: KillSwitchEvent | None


def derive_kill_state(events: Sequence[KillSwitchEvent], *, production: bool) -> KillState:
    if production and not events:
        return KillState(engaged=True, latest_event=None)
    _validate_chronological_unique_events(events)
    return KillState(engaged=True, latest_event=events[-1] if events else None)


def require_mutation_allowed(state: KillState) -> None:
    if state.engaged:
        raise ExecutionAuthorityError("EXECUTION_KILL_ENGAGED")
~~~

There is deliberately no clear_kill() production function. Tests may construct KillState(False,
None) directly inside test fixtures to exercise downstream logic.

- [ ] **Step 5: Cover all rejection codes and nonce/time limits**

Parametrize wrong venue/account/hash/operation, expired/not-yet-valid capability, clock skew,
replayed nonce, limit breach, jurisdiction blocked/unreviewed, LIVE_DISABLED, missing manifest, and
engaged kill. Assert stable codes and absence of bundle bytes in exception text.

Run: .venv/bin/python -m pytest tests/predictions/test_execution_authority.py tests/predictions/test_execution_kill_switch.py -v

Expected: PASS.

- [ ] **Step 6: Commit**

~~~bash
git add src/polytrading/predictions/execution/authority.py src/polytrading/predictions/execution/kill_switch.py tests/predictions/test_execution_authority.py tests/predictions/test_execution_kill_switch.py
git commit -m "feat(predictions): enforce dual execution authority gates"
~~~

---

### Task 4: Frozen Polymarket protocol snapshot and offline conformance foundation

**Files:**
- Create: src/polytrading/predictions/polymarket_execution/__init__.py
- Create: src/polytrading/predictions/polymarket_execution/protocol.py
- Create: src/polytrading/predictions/polymarket_execution/fixtures/sources_v1.json
- Create: src/polytrading/predictions/polymarket_execution/fixtures/protocol_v1.json
- Create: src/polytrading/predictions/polymarket_execution/fixtures/order_vectors_v1.json
- Create: src/polytrading/predictions/polymarket_execution/fixtures/event_vectors_v1.json
- Modify: pyproject.toml
- Test: tests/predictions/test_polymarket_protocol_snapshot.py

**Interfaces:**
- Produces: POLYMARKET_PROTOCOL_VERSION, ProtocolReadiness, PolymarketProtocolSnapshot,
  load_protocol_snapshot(), verify_protocol_sources(), bundled_fixture_path().

- [ ] **Step 1: Write failing snapshot integrity tests**

~~~python
def test_bundled_protocol_snapshot_is_self_hashing_and_current() -> None:
    snapshot = load_protocol_snapshot()
    assert snapshot.version == "polymarket-clob-2026-08-25-v1"
    assert snapshot.chain_id == 137
    assert snapshot.allowed_order_types == ("FAK", "FOK")
    assert verify_protocol_sources(snapshot).state == "CURRENT"


def test_one_changed_fixture_byte_requires_review(tmp_path: Path) -> None:
    copied = copy_bundled_fixtures(tmp_path)
    path = copied / "protocol_v1.json"
    path.write_bytes(path.read_bytes() + b" ")
    assert verify_protocol_sources(load_protocol_snapshot(copied)).state == (
        "PROTOCOL_REVIEW_REQUIRED"
    )
~~~

- [ ] **Step 2: Run tests and confirm the missing package**

Run: .venv/bin/python -m pytest tests/predictions/test_polymarket_protocol_snapshot.py -v

Expected: FAIL with ModuleNotFoundError.

- [ ] **Step 3: Freeze sources with a documented normalization rule**

Retrieve the six official URLs named in design section 4 once during this task. Normalize UTF-8
bytes by converting CRLF to LF only; do not strip or reflow content. Record canonical URL,
retrieved_at=2026-08-25T00:00:00Z, normalized_content_sha256, and the local fact/vector files
derived from that source in sources_v1.json. Do not commit full copyrighted documentation pages.
Review the fact files against the retrieved pages before deleting the temporary downloads.

protocol_v1.json must contain exact EIP-712 domain/type fields, chain ID, current exchange
addresses keyed by wallet signature type, ClobAuth fields, five L2 headers, exact signing preimage
components, route keys/methods/paths, amount precision/rounding rules, FAK/FOK encodings, order
acknowledgement states, trade settlement states, ping cadence, heartbeat contract, and geoblock
response fields.

- [ ] **Step 4: Implement strict load and hash verification**

~~~python
POLYMARKET_PROTOCOL_VERSION = "polymarket-clob-2026-08-25-v1"


def load_protocol_snapshot(root: Path | None = None) -> PolymarketProtocolSnapshot:
    fixture_root = root or bundled_fixture_path()
    document = json.loads((fixture_root / "protocol_v1.json").read_text(encoding="utf-8"))
    return PolymarketProtocolSnapshot.model_validate(document, strict=True)


def verify_protocol_sources(snapshot: PolymarketProtocolSnapshot) -> ProtocolReadiness:
    for fixture in snapshot.fixture_hashes:
        if sha256((snapshot.fixture_root / fixture.path).read_bytes()).hexdigest() != fixture.sha256:
            return ProtocolReadiness("PROTOCOL_REVIEW_REQUIRED", (fixture.path,))
    return ProtocolReadiness("CURRENT", ())
~~~

Resolve package resources with importlib.resources rather than assuming a source checkout.
pyproject.toml must bundle polymarket_execution/fixtures/*.json.

- [ ] **Step 5: Run snapshot and packaging tests**

Run: .venv/bin/python -m pytest tests/predictions/test_polymarket_protocol_snapshot.py -v

Run: .venv/bin/python -m build

Expected: tests PASS and wheel/sdist build succeeds with all four fixture JSON files present in the
wheel listing.

- [ ] **Step 6: Commit**

~~~bash
git add pyproject.toml src/polytrading/predictions/polymarket_execution tests/predictions/test_polymarket_protocol_snapshot.py
git commit -m "feat(predictions): freeze Polymarket protocol conformance sources"
~~~

---

### Task 5: Canonical order amounts, EIP-712 signing, and stable envelopes

**Files:**
- Modify: pyproject.toml
- Create: src/polytrading/predictions/polymarket_execution/order.py
- Test: tests/predictions/test_polymarket_order_signing.py
- Test: tests/predictions/test_polymarket_order_properties.py

**Interfaces:**
- Consumes: ExecutionIntent, SignedOrderEnvelope, PolymarketProtocolSnapshot.
- Produces: PolymarketOrder, order_amounts(), stable_order_salt(), order_typed_data(),
  order_fingerprint(), sign_order(), recover_order_signer().

- [ ] **Step 1: Pin eth-account and install**

Add exactly eth-account==0.13.7 to project dependencies. Do not add web3 or py-clob-client to the
runtime dependency graph.

Run: .venv/bin/python -m pip install -e ".[dev]"

Expected: install succeeds on the active Python version and pip check reports no conflict.

- [ ] **Step 2: Write official-vector and rounding-boundary tests**

~~~python
def test_buy_amounts_match_frozen_official_vector() -> None:
    vector = load_order_vector("buy_fak")
    maker, taker = order_amounts(
        side="buy",
        price=Decimal(vector["price"]),
        size=Decimal(vector["size"]),
        rounding=load_protocol_snapshot().rounding,
    )
    assert (maker, taker) == (vector["maker_amount"], vector["taker_amount"])


def test_sign_and_recover_exact_order_vector() -> None:
    vector = load_order_vector("buy_fak")
    envelope = sign_order(vector_intent(vector), fixture_private_key(), load_protocol_snapshot())
    assert envelope.order_fingerprint == vector["order_fingerprint"]
    assert recover_order_signer(envelope) == vector["maker"]
~~~

Property tests vary price/size at every precision boundary and mutate maker, signer, taker,
tokenId, makerAmount, takerAmount, expiration, nonce, feeRateBps, side, signatureType, chain ID,
and exchange address independently.

- [ ] **Step 3: Run tests and confirm missing functions**

Run: .venv/bin/python -m pytest tests/predictions/test_polymarket_order_signing.py tests/predictions/test_polymarket_order_properties.py -v

Expected: FAIL on imports from order.py.

- [ ] **Step 4: Implement canonical typed data using eth-account**

~~~python
def order_typed_data(order: PolymarketOrder, snapshot: PolymarketProtocolSnapshot) -> dict[str, object]:
    return {
        "types": snapshot.order_types,
        "primaryType": "Order",
        "domain": snapshot.order_domain(order.signature_type),
        "message": order.model_dump(mode="json", by_alias=True),
    }


def sign_order(
    intent: ExecutionIntent,
    private_key: bytes,
    snapshot: PolymarketProtocolSnapshot,
) -> SignedOrderEnvelope:
    order = order_from_intent(intent, stable_order_salt(intent), snapshot)
    signed = Account.sign_typed_data(private_key, full_message=order_typed_data(order, snapshot))
    return envelope_from_signed_order(intent, order, signed, snapshot)
~~~

stable_order_salt() is the unsigned 256-bit SHA-256 integer over protocol version, intent UUID, and
intent fingerprint. Canonical JSON uses sorted keys, compact separators, UTF-8, and Decimal values
already converted to protocol integer units. recover_order_signer() uses encode_typed_data() and
Account.recover_message(); it must equal the intended maker before an envelope is returned.

- [ ] **Step 5: Verify red/green plus dependency health**

Run: .venv/bin/python -m pytest tests/predictions/test_polymarket_order_signing.py tests/predictions/test_polymarket_order_properties.py -v

Run: .venv/bin/python -m pip check

Expected: PASS and no dependency conflicts.

- [ ] **Step 6: Commit**

~~~bash
git add pyproject.toml src/polytrading/predictions/polymarket_execution/order.py tests/predictions/test_polymarket_order_signing.py tests/predictions/test_polymarket_order_properties.py
git commit -m "feat(predictions): sign canonical Polymarket immediate orders"
~~~

---

### Task 6: ClobAuth L1 and exact-byte L2 authentication

**Files:**
- Create: src/polytrading/predictions/polymarket_execution/auth.py
- Test: tests/predictions/test_polymarket_auth.py
- Test: tests/predictions/test_polymarket_auth_properties.py

**Interfaces:**
- Produces: ClobCredentials, L2AuthHeaders, clob_auth_typed_data(), sign_clob_auth(),
  l2_preimage(), sign_l2_request().
- ClobCredentials is secret-bearing and must not inherit PredictionRecord or expose repr values.

- [ ] **Step 1: Write exact-byte and secret-repr tests**

~~~python
def test_l2_signature_uses_exact_serialized_body_bytes() -> None:
    credentials = fixture_clob_credentials()
    first = sign_l2_request(
        credentials,
        timestamp="1787688000",
        method="POST",
        route="/order",
        body=b'{"a":1,"b":2}',
    )
    second = sign_l2_request(
        credentials,
        timestamp="1787688000",
        method="POST",
        route="/order",
        body=b'{ "a": 1, "b": 2 }',
    )
    assert first.signature != second.signature


def test_credentials_never_reveal_secret_values() -> None:
    credentials = fixture_clob_credentials()
    rendered = repr(credentials) + str(credentials)
    assert credentials.secret.decode() not in rendered
    assert credentials.passphrase.decode() not in rendered
~~~

- [ ] **Step 2: Run tests and confirm missing auth module**

Run: .venv/bin/python -m pytest tests/predictions/test_polymarket_auth.py tests/predictions/test_polymarket_auth_properties.py -v

Expected: FAIL on imports.

- [ ] **Step 3: Implement L1 typed data and L2 standard-library HMAC**

~~~python
def l2_preimage(timestamp: str, method: str, route: str, body: bytes) -> bytes:
    return timestamp.encode("ascii") + method.upper().encode("ascii") + route.encode("ascii") + body


def sign_l2_request(
    credentials: ClobCredentials,
    *,
    timestamp: str,
    method: str,
    route: str,
    body: bytes,
) -> L2AuthHeaders:
    digest = hmac.new(
        _urlsafe_b64decode(credentials.secret),
        l2_preimage(timestamp, method, route, body),
        hashlib.sha256,
    ).digest()
    return L2AuthHeaders.from_digest(credentials, timestamp, digest)
~~~

Use the snapshot's ClobAuth domain/types/message for sign_clob_auth(). Normalize the URL-safe
base64 signature exactly as the frozen vector requires. Never accept a Python dict as the L2 body;
serialization must happen once before signing and the exact bytes must be sent unchanged.

- [ ] **Step 4: Add field-by-field mutation and sanitization properties**

Mutate timestamp, case-normalized method, route, every body byte, address, API key, secret, and
passphrase. Assert only signed inputs alter the digest as specified, all five POLY_* headers are
present, and no thrown error contains any secret-bearing value.

Run: .venv/bin/python -m pytest tests/predictions/test_polymarket_auth.py tests/predictions/test_polymarket_auth_properties.py -v

Expected: PASS.

- [ ] **Step 5: Commit**

~~~bash
git add src/polytrading/predictions/polymarket_execution/auth.py tests/predictions/test_polymarket_auth.py tests/predictions/test_polymarket_auth_properties.py
git commit -m "feat(predictions): implement exact Polymarket request authentication"
~~~

---

### Task 7: Bounded IPC, inherited-descriptor secrets, and signer sidecar

**Files:**
- Create: src/polytrading/predictions/polymarket_execution/ipc.py
- Create: src/polytrading/predictions/polymarket_execution/secrets.py
- Create: src/polytrading/predictions/polymarket_execution/signer.py
- Test: tests/predictions/test_polymarket_signer_ipc.py
- Test: tests/predictions/test_polymarket_secret_boundary.py

**Interfaces:**
- Consumes: authority verifier/gate, order signer, auth signer, ExecutionIntent.
- Produces: SignerRequest, SignerResponse, read_frame(), write_frame(), SecretMaterial,
  read_secret_descriptors(), redact_sensitive(), SignerService.handle(), run_signer_sidecar().
- run_signer_sidecar() is an internal callable only; do not register it in project.scripts or CLI.

- [ ] **Step 1: Write framing, collision, allowlist, and canary tests**

~~~python
def test_same_request_id_with_changed_payload_is_rejected() -> None:
    service = fixture_signer_service()
    first = signer_request(request_id=REQUEST_ID, operation=ExecutionOperation.SIGN_ORDER)
    assert service.handle(first).ok
    changed = first.model_copy(update={"intent_fingerprint": "f" * 64})
    assert service.handle(changed).error_code == "IPC_REQUEST_COLLISION"


def test_signer_rejects_unknown_or_prohibited_operations() -> None:
    service = fixture_signer_service()
    response = service.handle_raw(
        b'{"schema_version":1,"operation":"WITHDRAW","request_id":"x"}'
    )
    assert response.error_code == "IPC_OPERATION_NOT_ALLOWED"


def test_seeded_secrets_never_cross_the_signer_boundary(caplog: LogCaptureFixture) -> None:
    canary = b"CANARY-private-key-never-emit"
    service = fixture_signer_service(private_key=canary)
    response = service.handle(malformed_sign_request())
    assert canary.decode() not in response.model_dump_json()
    assert canary.decode() not in caplog.text
~~~

- [ ] **Step 2: Run tests and confirm missing modules**

Run: .venv/bin/python -m pytest tests/predictions/test_polymarket_signer_ipc.py tests/predictions/test_polymarket_secret_boundary.py -v

Expected: FAIL on imports.

- [ ] **Step 3: Implement length-prefixed IPC**

~~~python
MAX_FRAME_BYTES = 1_048_576


def read_frame(stream: BinaryIO) -> bytes:
    length_raw = _read_exact(stream, 4)
    length = int.from_bytes(length_raw, "big")
    if length <= 0 or length > MAX_FRAME_BYTES:
        raise SignerProtocolError("IPC_FRAME_SIZE_INVALID")
    return _read_exact(stream, length)


def write_frame(stream: BinaryIO, payload: bytes) -> None:
    if not payload or len(payload) > MAX_FRAME_BYTES:
        raise SignerProtocolError("IPC_FRAME_SIZE_INVALID")
    stream.write(len(payload).to_bytes(4, "big"))
    stream.write(payload)
    stream.flush()
~~~

SignerRequest includes schema_version, request_id, intent_id, intent_fingerprint,
capability_digest, manifest_digest, account_fingerprint, protocol_version, operation, deadline,
and operation-specific typed payload. SignerResponse includes only request_id, ok, result payload,
public fingerprints/signature where needed, and stable error_code.

- [ ] **Step 4: Implement inherited-descriptor secret loading**

~~~python
@dataclass(slots=True, repr=False)
class SecretMaterial:
    private_key: bytearray
    api_key: bytearray
    api_secret: bytearray
    passphrase: bytearray

    def close(self) -> None:
        for value in (self.private_key, self.api_key, self.api_secret, self.passphrase):
            value[:] = b"\x00" * len(value)
~~~

read_secret_descriptors() receives explicit integer descriptors from the trusted parent callable,
reads length-bounded bytes, closes descriptors immediately, validates nonempty maximum lengths,
and returns SecretMaterial. It does not read os.environ or sys.argv. Attempt resource.setrlimit for
core size where supported; treat unsupported platforms as a sanitized capability flag, not a
secret-bearing error.

- [ ] **Step 5: Implement signer service with independent authority verification**

~~~python
class SignerService:
    def handle(self, request: SignerRequest) -> SignerResponse:
        self._guard_replay(request)
        self._require_allowlisted(request.operation)
        decision = verify_mutation_authority(
            self._fresh_authority_context(request), request.operation
        )
        if not decision.allowed:
            return SignerResponse.rejected(request.request_id, decision.reason)
        return self._dispatch(request)
~~~

READ_ORDERS, READ_TRADES, and READ_ACCOUNT use an account-bound read guard rather than mutation
authority. SIGN_ORDER, SUBMIT_ORDER, CANCEL_ORDER, and HEARTBEAT require the full gate.

- [ ] **Step 6: Exercise a real child process without network**

Use multiprocessing.get_context("spawn"), os.pipe inherited descriptors, and a socketpair/pipe for
frames. Prove child crash, parent crash, truncated frame, oversized frame, replay, malformed JSON,
deadline expiry, secret zeroization, and sanitized error behavior. No test transport opens a
network socket.

Run: .venv/bin/python -m pytest tests/predictions/test_polymarket_signer_ipc.py tests/predictions/test_polymarket_secret_boundary.py -v

Expected: PASS.

- [ ] **Step 7: Commit**

~~~bash
git add src/polytrading/predictions/polymarket_execution/ipc.py src/polytrading/predictions/polymarket_execution/secrets.py src/polytrading/predictions/polymarket_execution/signer.py tests/predictions/test_polymarket_signer_ipc.py tests/predictions/test_polymarket_secret_boundary.py
git commit -m "feat(predictions): isolate Polymarket signing in a local sidecar"
~~~

---

### Task 8: Closed REST route set and no-retry authenticated transport

**Files:**
- Create: src/polytrading/predictions/polymarket_execution/routes.py
- Create: src/polytrading/predictions/polymarket_execution/rest.py
- Test: tests/predictions/test_polymarket_execution_rest.py

**Interfaces:**
- Produces: RouteKey, ROUTE_SPECS, PolymarketRestTransport Protocol,
  HttpxPolymarketRestTransport, RestResult, classify_order_ack(), sanitize_venue_error().
- RouteKey values: SUBMIT_ORDER, CANCEL_ORDER, READ_ORDER, READ_OPEN_ORDERS, READ_TRADES,
  READ_BALANCE_ALLOWANCE, HEARTBEAT, GEOBLOCK.

- [ ] **Step 1: Write route allowlist and no-retry tests**

~~~python
async def test_submit_timeout_is_unknown_and_called_once() -> None:
    calls = 0

    async def lose_response(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("lost", request=request)

    transport = HttpxPolymarketRestTransport(httpx.MockTransport(lose_response))
    result = await transport.execute(submit_order_request())
    assert result.code == "ORDER_OUTCOME_UNKNOWN"
    assert calls == 1


def test_route_set_has_no_value_transfer_operations() -> None:
    rendered = " ".join(spec.path.lower() for spec in ROUTE_SPECS.values())
    for forbidden in ("withdraw", "deposit", "transfer", "approve", "redeem", "relayer"):
        assert forbidden not in rendered
~~~

- [ ] **Step 2: Run tests and confirm missing routes/rest modules**

Run: .venv/bin/python -m pytest tests/predictions/test_polymarket_execution_rest.py -v

Expected: FAIL on imports.

- [ ] **Step 3: Implement typed route specs and exact-body request creation**

~~~python
@dataclass(frozen=True)
class RouteSpec:
    key: RouteKey
    method: Literal["GET", "POST", "DELETE"]
    path_template: str
    mutation: bool


def build_request(
    spec: RouteSpec, payload: PredictionRecord | None, credentials: ClobCredentials
) -> httpx.Request:
    body = b"" if payload is None else canonical_protocol_json(payload)
    headers = sign_l2_request(
        credentials,
        timestamp=current_epoch_seconds(),
        method=spec.method,
        route=render_path(spec, payload),
        body=body,
    )
    return httpx.Request(spec.method, absolute_allowlisted_url(spec, payload), headers=headers, content=body)
~~~

The caller supplies RouteKey plus a typed payload, never arbitrary method/path/URL/header/body.
GEOBLOCK uses the official unauthenticated host and returns only decision, country, region, time,
and raw-evidence hash to normal consumers; raw IP evidence stays in the restricted response object
and is never rendered.

- [ ] **Step 4: Implement transport policy and sanitized classification**

Use a dedicated httpx.AsyncClient without RetryingTransport. Reads may retry only through an
explicit bounded read policy. SUBMIT_ORDER never retries. CANCEL_ORDER may be issued again only by
coordinator recovery with a known order ID; the transport itself never loops. Rate-limit,
timeout, malformed JSON, delayed, live, matched, rejected, and contradictory responses map to
stable RestResult codes without raw response text.

- [ ] **Step 5: Test every route and failure class**

Cover exact method/path/body/header bytes from fixtures, non-allowlisted rejection before I/O,
timeout-before/after-acceptance fake cases, HTTP errors, rate limits, malformed bodies, delayed
ack, unexpected live ack, matched ack, cancellation confirmation requirement, and geoblock
redaction.

Run: .venv/bin/python -m pytest tests/predictions/test_polymarket_execution_rest.py -v

Expected: PASS.

- [ ] **Step 6: Commit**

~~~bash
git add src/polytrading/predictions/polymarket_execution/routes.py src/polytrading/predictions/polymarket_execution/rest.py tests/predictions/test_polymarket_execution_rest.py
git commit -m "feat(predictions): add closed Polymarket execution transports"
~~~

---

### Task 9: Authenticated user stream and heartbeat uncertainty

**Files:**
- Create: src/polytrading/predictions/polymarket_execution/user_stream.py
- Create: src/polytrading/predictions/polymarket_execution/heartbeat.py
- Test: tests/predictions/test_polymarket_user_stream.py
- Test: tests/predictions/test_polymarket_heartbeat.py

**Interfaces:**
- Produces: UserStreamParser, UserStreamHealth, parse_user_event(), HeartbeatState,
  classify_heartbeat(), recovery_reads_after_stream_gap().
- Parsed events are Task 1 VenueOrderEvent/VenueTradeEvent values.

- [ ] **Step 1: Write fixture parsing and disconnect tests**

~~~python
def test_trade_retrying_is_nonterminal() -> None:
    event = parse_user_event(load_event_vector("trade_retrying"), receipt_time=NOW)
    assert isinstance(event, VenueTradeEvent)
    assert event.state is VenueTradeState.RETRYING
    assert not event.terminal


def test_disconnect_requires_authoritative_reads_before_resume() -> None:
    health = UserStreamHealth.connected(NOW).on_disconnect(NOW + timedelta(seconds=3))
    assert health.kill_reason == "USER_STREAM_DISCONNECTED"
    assert recovery_reads_after_stream_gap(health) == (
        RouteKey.READ_OPEN_ORDERS,
        RouteKey.READ_TRADES,
        RouteKey.READ_BALANCE_ALLOWANCE,
    )
~~~

- [ ] **Step 2: Run tests and confirm missing modules**

Run: .venv/bin/python -m pytest tests/predictions/test_polymarket_user_stream.py tests/predictions/test_polymarket_heartbeat.py -v

Expected: FAIL on imports.

- [ ] **Step 3: Implement strict event parsing and sanitized subscription**

UserStreamParser accepts bytes, enforces a maximum message size, parses only documented order and
trade event shapes, retains SHA-256 of raw bytes, and rejects unknown/malformed states with
USER_STREAM_PROTOCOL_ERROR. The secret-bearing subscription frame is built and sent inside the
signer and never returned. Store only its hash and protocol version.

Implement ping/pong cadence from protocol_v1.json with an injected monotonic clock. Missing
ping/pong, disconnect, parse gap, or chronology contradiction returns a kill reason and mandatory
REST recovery set.

- [ ] **Step 4: Implement heartbeat classification**

~~~python
def classify_heartbeat(
    previous: HeartbeatState, result: RestResult, observed_at: datetime
) -> HeartbeatState:
    if result.ok:
        return HeartbeatState.confirmed(observed_at, result.evidence_hash)
    return HeartbeatState.uncertain(
        observed_at=observed_at,
        reason="HEARTBEAT_CANCELLATION_UNCERTAIN",
        required_reads=recovery_reads_after_heartbeat_failure(),
    )
~~~

Never infer that orders were cancelled or remained open after heartbeat failure. Require order,
trade, and account reads.

- [ ] **Step 5: Run complete state/event tests**

Cover placement/update/cancellation order events, all six trade states, duplicate events, reordered
timestamps, unknown state, oversized frame, disconnect/reconnect, missed ping, heartbeat success,
timeout, malformed response, and rate limit.

Run: .venv/bin/python -m pytest tests/predictions/test_polymarket_user_stream.py tests/predictions/test_polymarket_heartbeat.py -v

Expected: PASS.

- [ ] **Step 6: Commit**

~~~bash
git add src/polytrading/predictions/polymarket_execution/user_stream.py src/polytrading/predictions/polymarket_execution/heartbeat.py tests/predictions/test_polymarket_user_stream.py tests/predictions/test_polymarket_heartbeat.py
git commit -m "feat(predictions): track Polymarket user stream and heartbeat health"
~~~

---

### Task 10: Coordinator lifecycle, UNKNOWN recovery, and restart recovery

**Files:**
- Create: src/polytrading/predictions/execution/coordinator.py
- Test: tests/predictions/test_execution_coordinator.py
- Test: tests/predictions/test_execution_recovery.py

**Interfaces:**
- Consumes: PredictionMarketStore, authority/kill APIs, SignerService client port, REST results,
  Task 1 models.
- Produces: PreflightPort Protocol, SignerPort Protocol, AccountReadPort Protocol,
  ExecutionCoordinator.prepare(), submit_intent(), apply_order_event(), recover_account(),
  recover_on_startup().

- [ ] **Step 1: Write persist-before-submit and no-blind-retry tests**

~~~python
def test_plan_intent_and_submitting_event_exist_before_transport_call(store: PredictionMarketStore) -> None:
    observed: list[tuple[bool, bool, bool]] = []

    def submit(_envelope: SignedOrderEnvelope) -> RestResult:
        observed.append(
            (
                store.verified_live_execution_plan(PLAN_ID) is not None,
                store.verified_execution_intent(INTENT_ID) is not None,
                store.latest_order_state(INTENT_ID) is VenueOrderState.SUBMITTING,
            )
        )
        return RestResult.unknown("ORDER_OUTCOME_UNKNOWN")

    coordinator = fixture_coordinator(store=store, submit=submit, kill_clear=True)
    result = coordinator.submit_intent(execution_intent())
    assert observed == [(True, True, True)]
    assert result.state is VenueOrderState.UNKNOWN
    assert coordinator.submit_call_count == 1


def test_restart_recovers_unknown_before_accepting_new_work(store: PredictionMarketStore) -> None:
    seed_unknown_intent(store)
    coordinator = fixture_coordinator(store=store, kill_clear=True)
    report = coordinator.recover_on_startup()
    assert report.reads == ("orders", "trades", "account")
    assert coordinator.new_intents_blocked
~~~

- [ ] **Step 2: Run tests and confirm missing coordinator**

Run: .venv/bin/python -m pytest tests/predictions/test_execution_coordinator.py tests/predictions/test_execution_recovery.py -v

Expected: FAIL on import.

- [ ] **Step 3: Implement preflight and atomic persistence boundary**

~~~python
class PreflightPort(Protocol):
    def validate(self, intent: ExecutionIntent, now: datetime) -> PreflightEvidence: ...


class ExecutionCoordinator:
    def submit_intent(self, intent: ExecutionIntent) -> VenueOrderEvent:
        self._require_not_recovering()
        evidence = self._preflight.validate(intent, self._clock())
        self._verify_coordinator_authority(intent, evidence)
        with self._store.transaction() as tx:
            tx.append_execution_intent(intent)
            envelope = self._signer.sign(intent, evidence)
            tx.append_signed_order_envelope(envelope)
            tx.append_venue_order_event(submitting_event(intent, envelope, self._clock()))
        result = self._signer.submit(envelope, evidence)
        return self._classify_and_persist(result, intent)
~~~

PreflightEvidence contains current proof/economics/book/fee/account/balance/allowance/geoblock,
manifest, activation clock, risk, protocol, and capability hashes plus individual freshness
deadlines. Any missing/stale input is a typed refusal and writes no plan/intent.

- [ ] **Step 4: Implement lifecycle and post-fill revalidation**

Map matched, partial, filled, delayed, live-unexpected, rejected, cancelled, and unknown results to
legal append-only events. Delayed/live-unexpected/timeout/disconnect/contradiction engage kill.
After any first fill, call PreflightPort.revalidate_after_fill() and choose only one of
CONTINUE_FROZEN_PLAN, FROZEN_UNWIND, or HALT_EXPOSED. Never change size, price, order type, or hedge
outside the persisted plan.

- [ ] **Step 5: Implement authoritative recovery**

recover_account() fetches open orders, recent trades, and balances/allowances, correlates by known
venue ID, order fingerprint, and account, then appends evidence. Default ambiguous-submission
policy is no resubmission. Cancellation retries require a known order ID and completion requires a
confirming order read. recover_on_startup() scans SUBMITTING, UNKNOWN, ACK_DELAYED,
ACK_LIVE_UNEXPECTED, CANCEL_PENDING, nonterminal trades, and unreconciled entries before allowing
new work.

- [ ] **Step 6: Cover the full fake REST/WebSocket matrix**

Parametrize full fill, partial fill, FOK rejection, delayed ack, unexpected live, response lost
after acceptance, response lost before acceptance, duplicate intent, collision, rate limit,
disconnect, gap, missed heartbeat, cancellation ambiguity, settlement retry/failure, signer crash,
coordinator crash, and restart.

Run: .venv/bin/python -m pytest tests/predictions/test_execution_coordinator.py tests/predictions/test_execution_recovery.py -v

Expected: PASS with each failure asserting state, kill reason, persisted evidence, required reads,
and exactly zero blind order retries.

- [ ] **Step 7: Commit**

~~~bash
git add src/polytrading/predictions/execution/coordinator.py tests/predictions/test_execution_coordinator.py tests/predictions/test_execution_recovery.py
git commit -m "feat(predictions): coordinate fail-closed Polymarket order lifecycles"
~~~

---

### Task 11: Live ledger and authoritative reconciliation

**Files:**
- Create: src/polytrading/predictions/execution/ledger.py
- Create: src/polytrading/predictions/execution/reconciliation.py
- Test: tests/predictions/test_live_execution_ledger.py
- Test: tests/predictions/test_live_execution_reconciliation.py

**Interfaces:**
- Produces: VenueAccountSnapshot, postings_for_confirmed_trades(), verify_live_conservation(),
  reconcile_live_account(), reconciled_live_pnl().
- Consumes Task 1 events/postings and Task 2 store methods.

- [ ] **Step 1: Write conservation and P&L gating properties**

~~~python
@given(confirmed_trade_sequences())
def test_confirmed_trade_postings_conserve_value(sequence: ConfirmedTradeSequence) -> None:
    postings = postings_for_confirmed_trades(sequence.intents, sequence.trades)
    verify_live_conservation(postings)
    assert sum((p.debit_usd for p in postings), Decimal("0")) == sum(
        (p.credit_usd for p in postings), Decimal("0")
    )


def test_unreconciled_account_has_no_publishable_pnl() -> None:
    reconciliation = reconcile_live_account(
        expected_postings=fixture_postings(),
        account_snapshot=fixture_account_snapshot(balance_delta=Decimal("0.01")),
        observed_at=NOW,
    )
    assert not reconciliation.complete
    assert reconciled_live_pnl(fixture_postings(), reconciliation) is None
~~~

- [ ] **Step 2: Run tests and confirm missing modules**

Run: .venv/bin/python -m pytest tests/predictions/test_live_execution_ledger.py tests/predictions/test_live_execution_reconciliation.py -v

Expected: FAIL on imports.

- [ ] **Step 3: Implement exact double-entry postings**

Create balanced venue_cash/venue_position/fees_paid/settlement_receivable/realized_pnl posting pairs
only from authoritative matched/confirmed facts. Deduplicate by trade ID and settlement state.
MATCHED_NOT_BROADCASTED, MATCHED, MINED, and RETRYING do not create final settlement/P&L postings.
FAILED creates a settlement-failure evidence path and requires a fresh account snapshot.

- [ ] **Step 4: Implement independent account reconciliation**

~~~python
def reconcile_live_account(
    *,
    expected_postings: Sequence[LiveLedgerPosting],
    account_snapshot: VenueAccountSnapshot,
    observed_at: datetime,
) -> LiveReconciliation:
    differences = compare_expected_to_account(expected_postings, account_snapshot)
    return LiveReconciliation(
        schema_version=1,
        reconciliation_id=deterministic_reconciliation_id(account_snapshot, expected_postings),
        account_fingerprint=account_snapshot.account_fingerprint,
        observed_at=observed_at,
        complete=not differences,
        differences=tuple(differences),
        evidence_hashes=sorted_evidence_hashes(account_snapshot, expected_postings),
        next_action=None if not differences else "HALT_AND_RECONCILE",
    )
~~~

Account reads must be independent transport calls, not values copied from WebSocket events.
Compare orders, trades, token positions, available balance, allowance, fees, and settlement state.

- [ ] **Step 5: Test duplicates, missing/reordered events, fees, and settlement**

Assert missing, duplicated, reordered, contradictory, or wrong-account evidence fails exact
reconciliation. Cover partial fills, multiple trades per intent, fee precision, CONFIRMED,
RETRYING, FAILED, and restart reconstruction from stored events.

Run: .venv/bin/python -m pytest tests/predictions/test_live_execution_ledger.py tests/predictions/test_live_execution_reconciliation.py -v

Expected: PASS.

- [ ] **Step 6: Commit**

~~~bash
git add src/polytrading/predictions/execution/ledger.py src/polytrading/predictions/execution/reconciliation.py tests/predictions/test_live_execution_ledger.py tests/predictions/test_live_execution_reconciliation.py
git commit -m "feat(predictions): reconcile live Polymarket execution evidence"
~~~

---

### Task 12: Offline conformance runner and CLI

**Files:**
- Create: src/polytrading/predictions/polymarket_execution/conformance.py
- Modify: src/polytrading/predictions/cli.py
- Modify: tests/predictions/test_cli.py
- Create: tests/predictions/test_polymarket_conformance.py

**Interfaces:**
- Consumes: the Task 4 frozen fixture set, Task 5/6 encoders, Task 8 route definitions, Task 9
  event parser, and Task 2 conformance-result store methods.
- Produces CLI: `polytrading predictions execution conformance polymarket --db PATH
  [--fixtures PATH] --format text|json`.
- Produces: run_conformance(fixture_root, implementation_revision) ->
  ProtocolConformanceResult and stable process exit codes 0=conformant, 2=review required,
  64=invalid local invocation.
- Does not produce any other execution subcommand, transport construction, signer entrypoint,
  credential facility, capability issuer, or activation path.

- [ ] **Step 1: Write a failing parser test for the only execution command**

~~~python
def test_conformance_cli_is_the_only_execution_command() -> None:
    parser = build_parser()
    parsed = parser.parse_args([
        "predictions", "execution", "conformance", "polymarket",
        "--db", "var/predictions.duckdb", "--format", "json",
    ])
    assert parsed.predictions_execution_command == "conformance"
    for forbidden in ("order", "cancel", "heartbeat", "activate", "clear-kill"):
        with pytest.raises(SystemExit):
            parser.parse_args(["predictions", "execution", forbidden, "polymarket"])
~~~

Run: .venv/bin/python -m pytest tests/predictions/test_cli.py -k execution -v

Expected: FAIL because the execution parser branch does not exist.

- [ ] **Step 2: Add the offline-only parser branch**

Register exactly execution -> conformance -> polymarket with required --db, optional --fixtures,
and --format choices text/json. Dispatch through a local callable that accepts Paths and a text
stream; do not import rest.py, signer.py, httpx, websockets, or socket in the parser/dispatch path.

Run: .venv/bin/python -m pytest tests/predictions/test_cli.py -k execution -v

Expected: PASS.

- [ ] **Step 3: Write failing conformance and network-canary tests**

~~~python
def test_conformance_command_never_constructs_network_transport(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(socket, "socket", reject_socket_construction)
    result = run_cli_against_empty_store(tmp_path)
    assert result.exit_code == 0
    assert result.json["network_used"] is False


def test_changed_fixture_requires_protocol_review(tmp_path: Path) -> None:
    fixture_root = copied_bundled_fixtures(tmp_path)
    mutate_one_byte(fixture_root / "protocol_v1.json")
    result = run_conformance(fixture_root, implementation_revision="test")
    assert result.status == "PROTOCOL_REVIEW_REQUIRED"
~~~

Run: .venv/bin/python -m pytest tests/predictions/test_polymarket_conformance.py -v

Expected: FAIL because conformance.py does not exist.

- [ ] **Step 4: Implement the fixture-only conformance runner**

run_conformance validates source/fixture hashes, order typed-data/signature vectors, L1 vectors,
L2 exact-byte vectors, user-event vectors, route allowlist, and protocol version. The CLI defaults
to bundled fixtures, accepts only a local directory override, writes ProtocolConformanceResult
under the normal writer lease, emits deterministic JSON/text, and maps a changed hash to
PROTOCOL_REVIEW_REQUIRED and exit 2 without printing raw fixture content.

Run: .venv/bin/python -m pytest tests/predictions/test_polymarket_conformance.py tests/predictions/test_cli.py -v

Expected: PASS, including valid, missing, malformed, hash-changed, signature-changed, event-changed,
read-only-database, stable exit-code, and network-canary cases.

- [ ] **Step 5: Commit**

~~~bash
git add src/polytrading/predictions/polymarket_execution/conformance.py src/polytrading/predictions/cli.py tests/predictions/test_cli.py tests/predictions/test_polymarket_conformance.py
git commit -m "feat(predictions): add offline Polymarket conformance"
~~~

---

### Task 13: Immutable dashboard read models and SSE revision stream

**Files:**
- Create: src/polytrading/predictions/dashboard_live.py
- Modify: src/polytrading/predictions/dashboard_models.py
- Modify: src/polytrading/predictions/dashboard.py
- Modify: src/polytrading/predictions/dashboard_server.py
- Modify: tests/predictions/test_dashboard_models.py
- Modify: tests/predictions/test_dashboard.py
- Modify: tests/predictions/test_dashboard_server.py
- Create: tests/predictions/test_dashboard_live.py

**Interfaces:**
- Consumes: PredictionMarketStore query/transaction APIs from Task 2; Task 1 execution records;
  Task 11 ledger/reconciliation records; the frozen protocol manifest from Task 4.
- Produces in dashboard_models.py: DashboardDomain, ExecutionReadinessSummary,
  MarketAtlasOpportunity, ExecutionTimelineEntry, LiveLedgerSummary, EvidenceStatus, and the
  expanded PredictionDashboardSnapshot with revision_id and as_of.
- Produces in dashboard_live.py: DashboardRevision, DashboardReset,
  deterministic_dashboard_revision(), changed_dashboard_domains(), DashboardRevisionBuffer, and
  DashboardRevisionPublisher.
- Produces read-only routes: GET/HEAD /api/v1/predictions-dashboard and
  GET /api/v1/predictions-events. No other dashboard method or command endpoint is added.

- [ ] **Step 1: Write failing single-cutoff snapshot tests**

~~~python
def test_market_atlas_snapshot_is_one_immutable_cutoff(tmp_path: Path) -> None:
    database = seeded_prediction_database(tmp_path)
    snapshot = build_prediction_dashboard_snapshot(database, now=NOW)
    assert snapshot.revision_id == deterministic_dashboard_revision(snapshot)
    assert all(section.as_of == snapshot.as_of for section in snapshot.cutoff_bound_sections())
    assert snapshot.execution_readiness.implementation_state == "LIVE_DISABLED"
    assert snapshot.execution_readiness.live_action_available is False


def test_snapshot_contains_only_sanitized_read_models(tmp_path: Path) -> None:
    payload = build_prediction_dashboard_snapshot(
        seeded_prediction_database(tmp_path), now=NOW
    ).model_dump_json()
    for forbidden in SECRET_FIELD_NAMES | RAW_EXECUTION_PAYLOAD_FIELD_NAMES:
        assert forbidden not in payload
~~~

Run: .venv/bin/python -m pytest tests/predictions/test_dashboard_models.py tests/predictions/test_dashboard.py -v

Expected: FAIL because the expanded read models, cutoff metadata, and deterministic revision do
not exist.

- [ ] **Step 2: Implement the five-view read model from one transaction**

Add strict immutable models for the Overview, Markets, Execution, Ledger, and Evidence views.
Every section carries the snapshot's as_of; identifiers, hashes, public signatures, reason codes,
aggregates, freshness, and sanitized evidence references are allowed, while credentials, auth
headers, exact signed request bodies, raw WebSocket frames, IPC payloads, and private account data
are excluded.

Build the snapshot under one read transaction. Capture as_of once, finish every query inside that
transaction, canonicalize the result with revision_id omitted, then SHA-256 the canonical bytes to
obtain revision_id. ExecutionReadinessSummary is derived only from the shipped manifest, frozen
protocol status, latest verified conformance result, latest kill event, and production factory
posture—never from test fixture issuers.

Run: .venv/bin/python -m pytest tests/predictions/test_dashboard_models.py tests/predictions/test_dashboard.py -v

Expected: PASS.

- [ ] **Step 3: Write failing deterministic revision and bounded replay tests**

~~~python
def test_revision_changes_only_for_changed_domains() -> None:
    previous = dashboard_snapshot(revision_seed="a")
    current = previous.model_copy(
        update={"ledger": changed_ledger(previous.ledger), "revision_id": "0" * 64}
    )
    revision = DashboardRevision.from_snapshots(previous, current, emitted_at=NOW)
    assert revision.changed_domains == (DashboardDomain.LEDGER,)
    assert "ledger" not in revision.model_dump_json()


def test_slow_subscriber_gets_reset_not_unbounded_backlog() -> None:
    buffer = DashboardRevisionBuffer(capacity=3)
    published = [buffer.publish(dashboard_snapshot(revision_seed=str(i))) for i in range(5)]
    result = buffer.replay_after(published[0].event_id)
    assert isinstance(result, DashboardReset)
    assert result.latest_revision_id == published[-1].revision_id
~~~

Run: .venv/bin/python -m pytest tests/predictions/test_dashboard_live.py -v

Expected: FAIL because dashboard_live.py does not exist.

- [ ] **Step 4: Implement coalesced revision publication**

DashboardRevision contains schema_version, event_id, revision_id, as_of, emitted_at, and sorted
changed_domains only. DashboardReset contains schema_version, event_id, latest_revision_id,
emitted_at, and reason. The buffer has a fixed capacity of 128 in production and never owns full
snapshots. DashboardRevisionPublisher polls the read model at an injected interval, publishes only
when revision_id changes, wakes subscribers through a condition, and coalesces a subscriber behind
the newest buffered revision. A missing/expired Last-Event-ID yields reset rather than guessed
patches.

Run: .venv/bin/python -m pytest tests/predictions/test_dashboard_live.py -v

Expected: PASS, including stable canonical hashes, domain ordering, duplicate suppression,
coalescing, reset, shutdown, and injected-clock cases.

- [ ] **Step 5: Write failing SSE and method-boundary server tests**

~~~python
def test_sse_emits_revision_metadata_not_authoritative_values(running_dashboard: URL) -> None:
    response = read_one_sse_event(running_dashboard / "api/v1/predictions-events")
    assert response.event == "revision"
    assert set(response.data) == {
        "schema_version", "revision_id", "as_of", "emitted_at", "changed_domains"
    }
    assert not (set(response.data) & AUTHORITATIVE_TOTAL_FIELD_NAMES)


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE"])
def test_dashboard_has_no_command_transport(running_dashboard: URL, method: str) -> None:
    assert request(method, running_dashboard / "api/v1/predictions-dashboard").status_code == 405
    assert request(method, running_dashboard / "api/v1/predictions-events").status_code == 405
~~~

Run: .venv/bin/python -m pytest tests/predictions/test_dashboard_server.py -v

Expected: FAIL because the event route and HEAD handling are absent.

- [ ] **Step 6: Add the loopback SSE observer route**

Keep the existing loopback bind check. GET /api/v1/predictions-events accepts Last-Event-ID,
responds as text/event-stream with Cache-Control: no-store and X-Accel-Buffering: no, and emits
`revision`, `reset`, and comment keepalive frames. Serialize each frame as UTF-8 with one id line,
one event line, one compact JSON data line, and a blank terminator. Disconnect cleanly on a broken
pipe. GET/HEAD snapshot responses use no-store and the same immutable JSON; HEAD returns identical
headers with no body. Static assets remain GET/HEAD only.

Run: .venv/bin/python -m pytest tests/predictions/test_dashboard_server.py tests/predictions/test_dashboard_live.py -v

Expected: PASS, including resume, expired cursor reset, keepalive, slow-client coalescing, clean
shutdown, HEAD, cache headers, loopback binding, and non-GET rejection.

- [ ] **Step 7: Commit**

~~~bash
git add src/polytrading/predictions/dashboard_live.py src/polytrading/predictions/dashboard_models.py src/polytrading/predictions/dashboard.py src/polytrading/predictions/dashboard_server.py tests/predictions/test_dashboard_models.py tests/predictions/test_dashboard.py tests/predictions/test_dashboard_server.py tests/predictions/test_dashboard_live.py
git commit -m "feat(predictions): stream immutable dashboard revisions"
~~~

---

### Task 14: Build the read-only Market Atlas live console

**Files:**
- Modify: src/polytrading/predictions/web_assets/index.html
- Modify: src/polytrading/predictions/web_assets/app.css
- Modify: src/polytrading/predictions/web_assets/app.js
- Create: src/polytrading/predictions/web_assets/api.js
- Create: src/polytrading/predictions/web_assets/stream.js
- Create: src/polytrading/predictions/web_assets/store.js
- Create: src/polytrading/predictions/web_assets/charts.js
- Create: src/polytrading/predictions/web_assets/views.js
- Create: tests/predictions/test_market_atlas_assets.py
- Modify: tests/predictions/test_dashboard_server.py

**Interfaces:**
- Consumes: Task 13 PredictionDashboardSnapshot JSON and DashboardRevision/DashboardReset SSE
  events only.
- Produces in api.js: fetchDashboardSnapshot({signal}) using same-origin GET only.
- Produces in stream.js: startRevisionStream({onRevision, onReset, onStateChange, signal}) and
  startBoundedSnapshotPolling(); it opens only same-origin EventSource/GET traffic.
- Produces in store.js: createSnapshotStore(), validateSnapshotCutoff(), replaceSnapshot(), and the
  CONNECTED, DEGRADED, STALE, DISCONNECTED, INCONSISTENT state machine.
- Produces in charts.js: sparklineSvg(), depthBarsSvg(), and freshnessArcSvg() returning sanitized
  inline SVG nodes without external scripts or images.
- Produces in views.js: renderOverview(), renderMarkets(), renderExecution(), renderLedger(), and
  renderEvidence(). app.js owns bootstrap, navigation, render scheduling, and focus restoration.

- [ ] **Step 1: Write failing asset-contract and observer-boundary tests**

~~~python
def test_market_atlas_has_exactly_five_primary_views() -> None:
    html = asset_text("index.html")
    assert primary_view_names(html) == [
        "Overview", "Markets", "Execution", "Ledger", "Evidence"
    ]


def test_browser_assets_are_read_only_same_origin_observers() -> None:
    source = all_javascript_assets()
    assert "EventSource" in source
    assert "/api/v1/predictions-events" in source
    assert "/api/v1/predictions-dashboard" in source
    assert not FORBIDDEN_BROWSER_TRANSPORT_PATTERN.search(source)
    assert not FORBIDDEN_MUTATION_FETCH_PATTERN.search(source)
    for label in (
        "place order", "cancel order", "activate live", "clear kill", "import credentials"
    ):
        assert label not in (asset_text("index.html") + source).lower()
~~~

Run: .venv/bin/python -m pytest tests/predictions/test_market_atlas_assets.py -v

Expected: FAIL because the Market Atlas modules and five-view shell do not exist.

- [ ] **Step 2: Build the semantic shell and persistent safety rail**

Use a skip link, header, primary navigation, main view region, and aria-live status region. The
persistent rail always displays LIVE_DISABLED, READ ONLY, kill state, connection state, snapshot
cutoff, and last refresh. Navigation controls may be type=button tabs, but the document contains no
form, command field, order/cancel/activation/kill-clear control, state-bearing link, or hidden
mutation affordance. Keep the current slate/cyan/amber/coral palette and typography tokens.

Run: .venv/bin/python -m pytest tests/predictions/test_market_atlas_assets.py -k "shell or views or safety" -v

Expected: PASS.

- [ ] **Step 3: Write failing atomic-store and connection-state source contracts**

Assert store.js exports all five states, validates schema_version/revision_id/as_of, rejects a
section whose as_of differs from the root cutoff, keeps the previous complete snapshot on fetch or
validation failure, and schedules one render after a valid replacement. Assert stream.js treats
SSE as a notification only, coalesces concurrent refresh requests, uses exponential reconnect with
a fixed ceiling and jitter, starts bounded GET polling after disconnect, and stops every timer,
EventSource, and fetch on AbortSignal.

Run: .venv/bin/python -m pytest tests/predictions/test_market_atlas_assets.py -k "store or stream" -v

Expected: FAIL because api.js, stream.js, and store.js are absent.

- [ ] **Step 4: Implement GET-only refresh and atomic state replacement**

fetchDashboardSnapshot requests same-origin /api/v1/predictions-dashboard with GET, Accept JSON,
no-store, and AbortSignal. On `revision` or `reset`, stream.js requests a full snapshot; it never
copies totals from event.data. createSnapshotStore validates the whole payload before swapping one
frozen snapshot reference. If the fetched revision differs from the announced revision after one
immediate refetch, enter INCONSISTENT and keep the last valid snapshot. DEGRADED means SSE lost but
bounded polling succeeds; STALE is derived from server freshness metadata; DISCONNECTED means both
channels fail. CONNECTED requires a valid snapshot plus healthy SSE.

Run: .venv/bin/python -m pytest tests/predictions/test_market_atlas_assets.py -k "store or stream" -v

Expected: PASS.

- [ ] **Step 5: Implement the five Market Atlas views and inline charts**

Overview renders operating posture, freshness, opportunity count, reconciliation state, and a
compact session timeline. Markets renders sortable opportunity cards/table rows with market,
side, probability, edge, liquidity, source freshness, and eligibility reason. Execution renders
intent/order/trade chronology and UNKNOWN/recovery evidence without action controls. Ledger renders
balanced postings, fees, positions, and the P&L availability gate. Evidence renders protocol,
source hashes, conformance, reconciliation, calendar gates, and review status. charts.js creates
accessible SVG with title/desc, currentColor strokes/fills, bounded domains, and text fallbacks;
empty, unavailable, and redacted data get explicit prose rather than zero-looking graphics.

Run: .venv/bin/python -m pytest tests/predictions/test_market_atlas_assets.py tests/predictions/test_dashboard_server.py -v

Expected: PASS, including escaping/sanitization markers, no external asset dependency, no raw
HTML insertion, deterministic sort rules, and static-asset MIME/cache behavior.

- [ ] **Step 6: Add responsive, keyboard, and reduced-motion behavior**

At widths above 1100px use the Market Atlas desktop grid with persistent left navigation and
right safety rail. From 700–1099px collapse to a two-column tablet layout. Below 700px use one
column, horizontally scroll only dense data tables inside labelled regions, and keep status chips
wrapping without overlap. Implement roving tab focus with Arrow/Home/End, preserve focus on view
changes, expose a visible :focus-visible ring, maintain semantic heading order, and disable all
nonessential transitions under prefers-reduced-motion.

Run: .venv/bin/python -m pytest tests/predictions/test_market_atlas_assets.py -k "responsive or accessibility" -v

Expected: PASS.

- [ ] **Step 7: Perform deterministic browser visual verification**

Launch the loopback dashboard against the test database seeded with opportunities, order/trade
history, balanced ledger postings, stale source evidence, a passing conformance report, and an
engaged kill switch. Using the in-app browser, capture Overview, Markets, Execution, Ledger, and
Evidence at 1440x1000; capture Overview at 900x1100 and 390x844. Also capture DEGRADED, STALE,
DISCONNECTED, and INCONSISTENT states by using the deterministic server test hooks that are enabled
only by an injected test configuration object, never by a CLI or environment variable.

Inspect every image for clipping, overflow, overlapping rail content, illegible contrast, empty
chart ambiguity, broken focus treatment, and accidental action affordances. Fix each issue in
index.html/app.css/the owning module, rerun the focused asset tests, and repeat screenshots until
all eleven images are visually correct.

- [ ] **Step 8: Commit**

~~~bash
git add src/polytrading/predictions/web_assets/index.html src/polytrading/predictions/web_assets/app.css src/polytrading/predictions/web_assets/app.js src/polytrading/predictions/web_assets/api.js src/polytrading/predictions/web_assets/stream.js src/polytrading/predictions/web_assets/store.js src/polytrading/predictions/web_assets/charts.js src/polytrading/predictions/web_assets/views.js tests/predictions/test_market_atlas_assets.py tests/predictions/test_dashboard_server.py
git commit -m "feat(predictions): add the read-only Market Atlas console"
~~~

---

### Task 15: Authority proof, documentation, and final verification

**Files:**
- Create: tests/predictions/test_execution_authority_scan.py
- Create: tests/predictions/test_execution_secret_scan.py
- Create: docs/predictions/polymarket-execution-hardening.md
- Modify: README.md

**Interfaces:**
- Consumes: all production modules and public surfaces from Tasks 1–14.
- Produces: structural authority and secret-leak regression tests plus the operator runbook.
- Adds no runtime execution interface or dashboard command route.

- [ ] **Step 1: Write and run structural authority scans**

~~~python
def test_production_tree_has_no_execution_issuer_or_live_surface() -> None:
    source = production_python_source()
    for forbidden in (
        "FixtureCapabilityIssuer", "create_execution_capability", "clear_kill_switch",
        "execution order", "execution cancel", "execution activate",
    ):
        assert forbidden not in source
    assert every_shipped_manifest_is_live_disabled()
    assert production_factory_uses_unavailable_verifier_and_engaged_kill()


def test_dashboard_and_browser_are_observers_only() -> None:
    assert dashboard_route_methods() <= {"GET", "HEAD"}
    assert browser_network_destinations() == {
        "/api/v1/predictions-dashboard", "/api/v1/predictions-events"
    }
    assert not browser_contains_command_or_secret_transport()
~~~

Run: .venv/bin/python -m pytest tests/predictions/test_execution_authority_scan.py -v

Expected: PASS. The scan also proves authenticated REST/signer dispatch cannot be reached without
independent coordinator and signer authority decisions, public polymarket.py does not import the
execution package, no production kill-clear callable exists, and SSE data cannot contain snapshot
totals.

- [ ] **Step 2: Write and run cross-boundary secret-canary scans**

Seed distinct canaries for private key, API key, secret, passphrase, auth header, signed body, and
authenticated subscription frame. Exercise every signer/IPC/REST/stream failure plus dashboard
snapshot, SSE frame, database bytes, logs, exceptions, CLI stdout/stderr, and browser assets. Assert
each canary is absent from every observable and that sanitized error codes remain stable.

Run: .venv/bin/python -m pytest tests/predictions/test_execution_secret_scan.py -v

Expected: PASS.

- [ ] **Step 3: Write the operator boundary and recovery documentation**

docs/predictions/polymarket-execution-hardening.md documents the inherited-descriptor secret
boundary; absence of credential import, live action, or browser command surfaces; FAK/FOK policy;
unexpected resting-order, UNKNOWN, cancellation ambiguity, reconnect, heartbeat, settlement, and
restart recovery; source-hash review; kill triggers and absence of production clearance; Market
Atlas snapshot/SSE/freshness semantics; and the exact remaining 45 continuous calendar days, Class
G thresholds, 30 additional shadow calendar days, eligibility/custody/credentials/capability
issuer/pilot review, and explicit user approval. It states that the system does not circumvent
geographic controls or claim eligibility or profitability.

Update README.md with one concise LIVE_DISABLED paragraph, the offline conformance invocation, the
loopback read-only Market Atlas invocation already supported by the existing dashboard command,
and a link to the full runbook. Do not duplicate the runbook or imply an activation path.

- [ ] **Step 4: Run focused integration and safety verification**

Run: .venv/bin/python -m pytest tests/predictions/test_polymarket_conformance.py tests/predictions/test_execution_authority_scan.py tests/predictions/test_execution_secret_scan.py tests/predictions/test_cli.py tests/predictions/test_dashboard.py tests/predictions/test_dashboard_server.py tests/predictions/test_dashboard_live.py tests/predictions/test_market_atlas_assets.py -v

Expected: PASS.

- [ ] **Step 5: Run complete repository verification**

Run: .venv/bin/python -m pytest

Expected: all tests pass.

Run: .venv/bin/ruff check .

Expected: no lint errors.

Run: .venv/bin/ruff format --check .

Expected: no formatting changes required.

Run: .venv/bin/python -m build

Expected: sdist and wheel build successfully with all protocol fixtures and eight web assets.

- [ ] **Step 6: Verify the built wheel in a fresh isolated environment**

~~~bash
INSTALL_ROOT="$(mktemp -d /tmp/polytrading-execution-install.XXXXXX)"
python3 -m venv "$INSTALL_ROOT/venv"
"$INSTALL_ROOT/venv/bin/python" -m pip install dist/polytrading-0.1.0-py3-none-any.whl
"$INSTALL_ROOT/venv/bin/polytrading" predictions execution conformance polymarket --db "$INSTALL_ROOT/conformance.duckdb" --format json
"$INSTALL_ROOT/venv/bin/python" -m pip check
~~~

Expected: installation and offline conformance succeed, network_used is false, pip check reports no
conflicts, and the wheel contains protocol fixtures plus index.html, app.css, app.js, api.js,
stream.js, store.js, charts.js, and views.js. Leave the explicit temporary directory path in the
verification log; do not use a broad cleanup command.

- [ ] **Step 7: Commit**

~~~bash
git add tests/predictions/test_execution_authority_scan.py tests/predictions/test_execution_secret_scan.py docs/predictions/polymarket-execution-hardening.md README.md
git commit -m "feat(predictions): prove live-disabled execution boundaries"
~~~

---

## Final Review Gate

- [ ] Verify every requirement in design sections 1–19 maps to at least one task and test above.
- [ ] Confirm git diff contains no .claude/settings.json change and no credential/capability
  material.
- [ ] Re-run the complete repository verification commands from Task 15 against the final commit.
- [ ] Use superpowers:requesting-code-review for an independent spec-conformance and security
  review.
- [ ] Resolve every review finding through the receiving-code-review workflow and re-run affected
  focused tests plus the complete suite.
- [ ] Record final verification evidence in README.md or a dedicated completion note without
  changing LIVE_DISABLED, calendar gates, or authority posture.
