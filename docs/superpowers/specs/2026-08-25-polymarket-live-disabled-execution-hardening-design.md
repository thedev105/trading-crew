# Polymarket Live-Disabled Execution Hardening

**Date:** 2026-08-25
**Status:** Direction approved; written specification awaiting user review
**Scope:** Increment 5 for Polymarket only: real protocol-compatible authentication, signing,
immediate-order lifecycle, account-state reconciliation, and kill-switch infrastructure, all
shipped without an issuable live capability
**Authority boundary:** This specification does not authorize credentials, funding, approvals,
transfers, deposits, withdrawals, redemption, activation, or a live order. The delivered system
must remain unable to submit a real order while Polymarket is LIVE_DISABLED.

Companion specification:

- [Multi-Venue Prediction-Market Structural Opportunity System](2026-08-14-multi-venue-prediction-market-structural-opportunity-system-design.md)

## 1. Decision

Increment 5 begins with one separately bounded venue package for Polymarket. It implements the
current authenticated CLOB protocol behind a capability-gated local signer process, but it does
not issue the capability required to use a mutating venue endpoint. Existing manifests remain
LIVE_DISABLED. There is no live-order command, activation command, credential-creation command,
production capability issuer, or dashboard control.

The implementation covers real wire formats rather than a fake-only abstraction so offline
fixtures can prove that signing, authentication, amount encoding, status parsing, recovery, and
reconciliation match the official protocol. The only executable operator surface added by this
increment is offline conformance checking. Read-only dashboard output reports readiness and unmet
gates; it cannot hold secrets or initiate execution.

New orders are restricted to fill-and-kill (FAK) and fill-or-kill (FOK). Resting GTC and GTD
orders are deliberately excluded. Unexpected resting state is an execution fault, not an accepted
outcome.

This is not a live pilot. A later activation proposal would require its own evidence checkpoint,
eligibility review, credential and custody design, capability issuer, maximum-capital approval,
and explicit user authorization. It cannot inherit authority from this specification.

## 2. Objective and Success Definition

This increment answers one engineering question:

> Can the project encode and reconcile a Polymarket immediate-order lifecycle exactly, isolate
> signing secrets behind a narrow process boundary, and prove that no production path can exercise
> that lifecycle while the venue remains LIVE_DISABLED?

Success means an operator can:

1. run official-document-derived conformance fixtures entirely offline;
2. inspect whether the local implementation matches the frozen Polymarket protocol snapshot;
3. exercise full REST and WebSocket state machines against deterministic fake transports;
4. prove stable intent identity, signing, unknown-outcome recovery, cancellation, settlement
   tracking, and ledger reconciliation;
5. inspect read-only readiness, protocol version, kill-switch state, and unmet activation gates;
6. verify that secrets never enter DuckDB, logs, exceptions, reports, or dashboard payloads; and
7. run an authority scan demonstrating that no CLI or production factory can issue a live
   execution capability.

Success does not mean that the strategy is eligible, profitable, funded, or ready to trade.

## 3. Governing Safety Invariants

The following invariants are binding:

1. **No capability, no mutation.** Every order, cancellation, and heartbeat request requires a
   verified, unexpired execution capability at both the coordinator and signer boundaries.
2. **No production issuer.** This increment includes a verifier contract and test-only fixture
   issuer, but no production issuer, configured verification key, activation file generator, or
   activation CLI. Therefore production verification always fails closed.
3. **Manifest agreement is necessary but insufficient.** Both processes independently require a
   current manifest whose jurisdiction status is ELIGIBILITY_REVIEWED and implementation state is
   LIVE_ELIGIBLE. A capability cannot override a manifest rejection.
4. **Secrets are ephemeral.** The wallet private key and CLOB API key, secret, and passphrase enter
   only the signer process through inherited descriptors at startup and remain in memory.
5. **Intent submission is idempotent.** One execution-intent ID maps to one salt, one canonical
   order, and one order fingerprint. A retry cannot silently create a second order.
6. **Unknown is a halt state.** A timeout, lost response, contradictory account read, reconnect, or
   unverifiable acknowledgement produces UNKNOWN, blocks new intents, and starts authoritative
   reconciliation.
7. **WebSocket is not authority.** User-channel events reduce latency but never replace REST
   order/trade/account reads after a gap or reconnect.
8. **Ledger truth follows venue truth.** Profit and loss is not valid until order, trade,
   settlement, balance, allowance, and double-entry ledger state reconcile.
9. **Cancellation does not broaden authority.** Kill and recovery paths may cancel or inspect only
   orders already bound to the approved account and known execution intents.
10. **No value-transfer authority.** The signer cannot call deposit, withdrawal, transfer,
    approval, redemption, conversion, relayer, wallet-creation, or unknown routes.

## 4. Protocol Snapshot and Versioning

Implementation is pinned to official Polymarket documentation retrieved on 2026-08-25:

- https://docs.polymarket.com/getting-started/api
- https://docs.polymarket.com/trading/place-orders
- https://docs.polymarket.com/trading/manage-orders
- https://docs.polymarket.com/trading/realtime-order-updates
- https://docs.polymarket.com/api-reference/trade/send-heartbeat
- https://docs.polymarket.com/api-reference/geoblock

The conformance package stores, for every source, its canonical URL, retrieval time, normalized
content SHA-256, protocol fixture version, and implementation revision. A changed source hash does
not silently update behavior: readiness becomes PROTOCOL_REVIEW_REQUIRED until fixtures and
parsers are reviewed and a new protocol version is committed.

The initial snapshot models:

- Polygon chain ID 137 and Polymarket's current EIP-712 order domain and exchange address;
- the current ClobAuth wallet-signing contract for L1 authentication;
- L2 HMAC-SHA256 authentication over timestamp, uppercase HTTP method, route path, and the exact
  serialized request body;
- the five current L2 headers: POLY_ADDRESS, POLY_SIGNATURE, POLY_TIMESTAMP, POLY_API_KEY, and
  POLY_PASSPHRASE;
- current order fields, wallet signature types, amount rounding, fee-rate binding, and FAK/FOK
  semantics;
- order placement, order reads, trade reads, cancellations, heartbeat, geoblock, and authenticated
  user-channel event shapes;
- current order acknowledgement states including live, matched, and delayed; and
- current trade progression through MATCHED_NOT_BROADCASTED, MATCHED, MINED, CONFIRMED, RETRYING,
  and FAILED.

Exact domain fields, exchange addresses, routes, payload shapes, rounding tables, and status
mappings live in versioned protocol fixtures, not scattered constants. A fixture mutation changes
its hash and invalidates prior conformance results.

## 5. Architecture

The subsystem has four boundaries.

### 5.1 Venue-neutral execution core

The execution core owns immutable plans and intents, authorization decisions, order and trade state
machines, the kill switch, live ledger postings, and reconciliation. It knows that a venue can
sign, submit, cancel, inspect, and stream events, but it does not know Polymarket EIP-712 fields,
HMAC headers, endpoint paths, or wallet signature types.

The core consumes only already-proved and already-priced proposals. It cannot generate candidate
relationships, weaken a proof, change economics, increase risk limits, or promote a manifest.

### 5.2 Polymarket protocol package

The Polymarket package owns:

- canonical order encoding and amount conversion;
- EIP-712 order signing inputs and signature verification;
- ClobAuth L1 signing inputs;
- L2 exact-byte HMAC authentication;
- typed REST requests and sanitized responses for approved routes;
- FAK/FOK acknowledgement and lifecycle mapping;
- authenticated user WebSocket parsing and sequence/gap handling;
- heartbeat request and failure handling;
- geoblock decision parsing; and
- authoritative order, trade, balance, and allowance reconciliation.

This package is independent of the current public-only Polymarket collector. Shared identifiers and
public domain records may be reused, but authenticated transport and secret-bearing types must not
be added to the public adapter.

### 5.3 Local signer sidecar

The signer is a separate local process with the smallest practical dependency graph. It receives
secrets through inherited file descriptors at startup. It returns only signed public envelopes,
order fingerprints, sanitized venue results, and stable error codes.

The coordinator communicates over a local authenticated IPC channel with length-bounded,
schema-versioned messages. Each request includes a request ID, intent ID, capability digest,
manifest digest, account fingerprint, protocol version, operation, and deadline. The signer
rejects duplicate request IDs with different payloads, unknown schema versions, oversized or
malformed messages, expired deadlines, unknown operations, and any capability/manifest mismatch.

The signer allowlist contains only the protocol operations needed for order signing, immediate
order submission, known-order cancellation, heartbeat, and account/order/trade reads. It does not
accept arbitrary URLs, methods, headers, or bodies from the coordinator.

### 5.4 Transport adapters

REST and WebSocket transports are injected interfaces. Production implementations perform exact
wire I/O only after both gates pass. Deterministic fake transports implement the same interfaces
for all tests and offline conformance. The core never imports a fake-only state model.

The coordinator owns no secret and cannot construct authenticated headers. The signer owns no
strategy logic and cannot invent an order intent.

## 6. Authorization and Capability Model

An ExecutionCapability is a detached-signature bundle with:

- capability version and unique capability ID;
- venue and approved account fingerprint;
- manifest record hash and source hashes;
- eligibility evidence hashes;
- strategy/proof/economics policy hashes;
- protocol fixture hash;
- allowed operations and route-set version;
- maximum capital, per-intent notional, position, and loss limits;
- not-before and expiration timestamps;
- single activation nonce; and
- issuer key ID and detached signature.

The verifier interface returns either a typed VerifiedExecutionCapability or a stable rejection.
Test fixtures use a deterministic in-process verifier. Production construction requires an
explicitly configured public verification key, but this increment provides none and provides no
way to create a signed bundle. A future activation specification must choose and approve the
operator signing ceremony, key custody, bundle duration, revocation channel, and production key.

Before every mutating operation, both coordinator and signer independently verify:

1. the capability signature and canonical bytes;
2. current time within the capability window and acceptable clock skew;
3. venue, account, protocol, route-set, manifest, and policy hashes;
4. current manifest through evaluate_execution_gate;
5. the operation and requested notional against capability limits;
6. activation nonce uniqueness and revocation state;
7. fresh geoblock and account-scope evidence; and
8. the local kill switch is clear.

Read operations used for recovery do not require order-submission authority, but they remain bound
to the configured account and route allowlist. Cancellation and heartbeat require a valid
capability because they are venue mutations. When the capability expires during an incident, the
system stops new orders and reports that automated cancellation is unavailable; it continues
read-only reconciliation and emits an operator-action requirement. This limitation is explicit
rather than hiding an unbounded emergency credential.

## 7. Data Model

All records extend the existing PredictionRecord conventions with schema version, UTC timestamps,
canonical JSON, record hash, and lineage hashes.

### 7.1 LiveExecutionPlan

An immutable plan contains:

- plan ID, proposal ID, candidate ID, proof artifact hash, and economics report hash;
- current book snapshot IDs, fee evidence IDs, and information cutoff;
- selected Polymarket token IDs and FAK/FOK leg order;
- maximum size or spend, per-leg limit prices, and fee caps;
- assigned capital and incomplete-exposure reserve;
- risk-policy, manifest, eligibility, protocol, and capability hashes;
- book, proof, economics, account, and geoblock freshness deadlines;
- kill and unwind conditions; and
- deterministic plan fingerprint.

Plan creation requires fresh authoritative books and current fees. It never converts a stale shadow
proposal directly into an executable plan.

### 7.2 ExecutionIntent

Each immutable intent contains:

- stable intent ID and parent plan ID;
- leg sequence, token ID, side, limit price, base size or maximum spend;
- FAK or FOK order type;
- fee-rate cap and rounding mode;
- approved account and capability fingerprints;
- creation time, deadline, and protocol version; and
- deterministic intent fingerprint.

The intent ID is generated before signing and persisted before submission.

### 7.3 SignedOrderEnvelope

The envelope contains the canonical public order fields, salt, signature type, public signature,
domain and exchange fingerprints, exact body hash, order fingerprint, signer version, and source
intent ID. It contains no wallet private key or CLOB credential.

For one intent fingerprint, the signer derives or retrieves exactly one salt and returns byte-for-
byte equivalent canonical order material on every retry. A different payload under the same intent
ID is rejected as INTENT_COLLISION.

### 7.4 Venue events

VenueOrderEvent and VenueTradeEvent retain the sanitized raw-event hash, source channel, venue
identifier, intent/order/trade identifiers, venue timestamp, local receipt time, normalized state,
sequence metadata where supplied, and protocol version.

Order state distinguishes at minimum PLANNED, SIGNED, SUBMITTING, ACK_LIVE_UNEXPECTED,
ACK_MATCHED, ACK_DELAYED, PARTIALLY_FILLED, FILLED, CANCEL_PENDING, CANCELLED, REJECTED, UNKNOWN,
and RECONCILED.

Trade state preserves MATCHED_NOT_BROADCASTED, MATCHED, MINED, CONFIRMED, RETRYING, and FAILED.
Only CONFIRMED and FAILED are terminal settlement outcomes. Normalization cannot discard the
original venue state.

### 7.5 Activation, kill, ledger, and reconciliation records

ActivationEvidence records capability and manifest digests, verifier result, timestamps, and
stable rejection codes, never the signed bundle's secret material.

KillSwitchEvent is append-only and records trigger, scope, source intent/order, prior state,
occurred time, and clearance evidence. Clearing a kill is a future authorized operation; this
increment's production posture starts killed and cannot clear itself.

LiveLedgerPosting is double-entry and references the exact order, trade, settlement, fee, and
balance evidence used. Shadow and live namespaces remain separate.

LiveReconciliation records independently fetched venue orders, trades, balances, allowances,
expected ledger postings, exact differences, result, and next required action. P&L consumers may
read only reconciled live postings.

ConformanceResult records fixture/source hashes, implementation revision, executed checks, result,
and sanitized failure fingerprints.

## 8. Storage

Prediction-market migration 008 creates append-only tables:

- live_execution_plans;
- execution_intents;
- signed_order_envelopes;
- venue_order_events;
- venue_trade_events;
- live_ledger_postings;
- live_reconciliations;
- execution_kill_events;
- activation_evidence; and
- protocol_conformance_results.

The migration follows the existing record_json plus record_hash pattern and preserves exact
queryable identities and timestamps needed for uniqueness and ordering. Database writes use one
writer lease and explicit transactions.

Persist-before-submit is mandatory: plan and intent are committed before the signer may build an
envelope, and envelope plus SUBMITTING event are committed before transport I/O. Event and ledger
writes are append-only. Recovery reconstructs state by replaying events and comparing with
authoritative venue reads; it never repairs history by updating a prior row.

Credential values, raw auth headers, secret-bearing WebSocket subscription frames, inherited
descriptor contents, and private keys are forbidden from every table. A storage-safety test scans
the database bytes and exported JSON for seeded canary secrets.

## 9. Execution Flow

### 9.1 Preflight

Before the first intent, the coordinator:

1. loads the immutable proof and economics artifacts;
2. fetches fresh current books and fees and recomputes conservative surplus;
3. re-evaluates the venue manifest and capability;
4. checks activation clock evidence and all fixed Class G thresholds;
5. checks account, balance, allowance, capital, concentration, incomplete exposure, drawdown, and
   loss limits;
6. performs the current official geoblock check without attempting geographic circumvention;
7. verifies protocol and official-source hashes;
8. verifies signer account fingerprint and health;
9. creates and commits LiveExecutionPlan; and
10. creates and commits the first ExecutionIntent.

Any unknown or stale input rejects the plan. A geoblock response is evidence, not a legal
conclusion: the exact decision and restricted source evidence are retained, while ordinary logs
and the dashboard show only decision, country/region where supplied, time, and evidence hash.

### 9.2 Sign and submit

The signer validates the intent, independently re-runs its gates, derives the stable envelope, and
verifies the signature against the intended maker. The coordinator persists the envelope and
SUBMITTING event, then asks the signer to submit the exact body as FAK or FOK.

The response is parsed into a typed acknowledgement. A matched acknowledgement advances to
authoritative order/trade reconciliation. Delayed, unexpected live, malformed, timeout, or
connection-loss outcomes become UNKNOWN and trigger the kill switch. The client never blindly
retries the order POST.

### 9.3 Multi-leg continuation

After any first-leg fill, the coordinator immediately refreshes books, fees, account state,
geoblock evidence, proof inputs, conservative economics, incomplete exposure, and risk. It either:

- creates the next already-bounded intent when every gate still passes;
- follows the plan's frozen immediate unwind policy when completion no longer passes but unwind
  does; or
- halts with exposed inventory and an explicit operator-action record when neither action is
  authorized.

The strategy cannot enlarge size, loosen price, change order type, or invent a new hedge during
recovery.

### 9.4 WebSocket and heartbeat

The user WebSocket is an event accelerator. The signer sends the authenticated subscription and
emits sanitized typed events. It sends the protocol-required ping cadence and separately performs
the approved heartbeat operation when a capability authorizes it.

A disconnect, parse gap, missed ping/pong, or sequence concern activates the kill switch. Before
resuming, the system fetches authoritative open orders and recent trades and reconciles them with
persisted events. A missed or failed order heartbeat is treated as cancellation uncertainty:
assume neither that the venue cancelled nor that an order remains; read account state.

### 9.5 Settlement and accounting

Matched trades remain unsettled until the venue reaches CONFIRMED. RETRYING is nonterminal.
FAILED records a terminal settlement failure and requires exact balance/position reconciliation.
Ledger postings preserve fees and position changes at the venue's precision. P&L remains
unavailable until live reconciliation is exact.

## 10. Unknown Outcomes, Cancellation, and Recovery

The order POST retry rule is absolute:

- a rejected request with authoritative proof that no order was accepted may terminate REJECTED;
- a timeout, lost response, malformed response, or contradictory response becomes UNKNOWN;
- UNKNOWN blocks all new intents for the account;
- recovery queries order/trade/account state using the intent's order fingerprint and persisted
  identifiers;
- only authoritative evidence may classify the outcome; and
- the same intent may be resubmitted only when conformance evidence proves the venue did not
  accept it and the recovery policy explicitly permits the identical envelope. The default policy
  is no resubmission.

Cancellation may be retried only for a known venue order ID bound to the current account and
intent. A successful HTTP response is not sufficient; cancellation is complete only after
authoritative order state confirms it. Cancellation ambiguity leaves the account killed.

On process restart, the coordinator first scans for SUBMITTING, UNKNOWN, ACK_DELAYED,
ACK_LIVE_UNEXPECTED, CANCEL_PENDING, nonterminal trades, and unreconciled ledger entries. It
performs read-only recovery before accepting any new work. The signer cannot resume from a
serialized secret store; credentials must be injected again.

## 11. Kill Switch

The kill switch is account-scoped and starts engaged in production. Triggers include:

- manifest, capability, eligibility, geoblock, protocol, or source-hash invalidation;
- stale books, fees, balances, allowances, or activation evidence;
- signer health failure or account-fingerprint mismatch;
- unknown acknowledgement, unexpected resting order, or duplicate/colliding intent;
- REST/WebSocket contradiction, disconnect, gap, or malformed message;
- heartbeat failure;
- rate limiting that prevents safe state reads;
- balance, position, settlement, or ledger divergence;
- risk-limit breach, clock skew, nonce reuse, or signature mismatch; and
- any unsanitized-secret detection.

While killed, the system rejects new plans and intents. Read-only reconciliation remains
available. Cancellation is attempted only while an independently valid capability authorizes it.
The dashboard reports trigger codes and evidence hashes, not sensitive payloads.

No automatic clearance exists in this increment. Offline fixtures may construct an explicitly
cleared test state; production construction cannot.

## 12. Secret Handling and Sanitized Observability

Secrets enter the signer via inherited descriptors, not command-line arguments, environment
variables, config files, DuckDB, or IPC request bodies. Startup closes the inherited descriptors
after reading, overwrites mutable input buffers where the runtime permits, disables core dumps
where supported, and applies restrictive local socket and process permissions.

Logs, metrics, traces, exceptions, IPC errors, dashboard payloads, and conformance output use
stable codes and hashes. They never include:

- private keys, API secrets, passphrases, or seed phrases;
- raw POLY_* authentication headers;
- raw authenticated WebSocket subscription frames;
- full signed request bodies when signature disclosure is unnecessary;
- inherited descriptor numbers or contents;
- full geoblock IP addresses; or
- arbitrary venue response text.

Public signatures and signed order envelopes may be stored only where required for audit, but
ordinary logs use their fingerprints. Redaction happens before data crosses the signer boundary;
the coordinator is not trusted to redact secret-bearing failures.

## 13. Operator Surfaces

### 13.1 Offline CLI

The only new command family is conformance-oriented, for example:

    polytrading predictions execution conformance polymarket \
      --fixtures tests/fixtures/predictions/polymarket_protocol \
      --format json

It accepts fixture data, never credentials, and never enables a network transport. It reports
fixture/source hashes, protocol version, passing checks, and sanitized failures.

There is no order, cancel, heartbeat, credential, signer-start, activation, or kill-clear CLI in
this increment. Internal transport interfaces are exercised only by tests.

### 13.2 Read-only dashboard

The existing loopback-only prediction dashboard gains a readiness section showing:

- venue implementation state, always LIVE_DISABLED for shipped manifests;
- production capability issuer status, always NOT_CONFIGURED;
- execution kill state, always ENGAGED in production;
- unmet 45-day evidence, Class G threshold, and additional 30-day shadow gates;
- protocol version, source review time, source hashes, and conformance status;
- latest offline conformance result and sanitized failure codes; and
- the statement that no live action is available.

The dashboard opens DuckDB read-only and contains no forms, buttons, links, WebSocket commands, or
API routes that mutate execution state.

## 14. Dependency and Cryptography Policy

The implementation uses a narrowly pinned eth-account dependency for EIP-712 and secp256k1
operations and Python's standard-library HMAC-SHA256 for L2 authentication. It does not implement
elliptic-curve cryptography, signature recovery, or typed-data hashing from scratch.

The dependency update must be locked, installed in a clean environment, reviewed for transitive
packages, and covered by independent official-fixture tests. Application code wraps the library
behind small protocol interfaces so a dependency upgrade cannot silently change canonical bytes.

Capability signature production remains deliberately unselected and unavailable in this
increment. The verifier interface is real, but choosing the production signature scheme and key
ceremony belongs to the later activation specification.

## 15. Testing Strategy

### 15.1 Protocol conformance

Official-document-derived fixtures cover:

- ClobAuth typed data and recovered address;
- EIP-712 order hashes and recovered maker;
- all signed order fields, domain, exchange address, chain ID, wallet signature type, and fee rate;
- exact L2 method, path, timestamp, body bytes, HMAC digest, and headers;
- buy/sell amount conversion and every documented rounding boundary;
- FAK/FOK encoding and acknowledgement mapping;
- order/trade reads, cancellation, heartbeat, and geoblock parsing; and
- user-channel subscription and order/trade events.

Mutation and property tests change each signed field independently and prove that the signature or
fingerprint changes. They cover noncanonical JSON, Unicode, control bytes, decimals, timestamp
boundaries, clock skew, invalid signature types, and wrong exchange addresses.

### 15.2 State-machine and fake-transport end-to-end tests

Deterministic REST/WebSocket fakes cover full fill, partial fill, FOK rejection, delayed
acknowledgement, unexpected live order, response loss after acceptance, response loss before
acceptance, duplicate intent, collision, rate limit, disconnect, event gap, missed heartbeat,
cancellation ambiguity, settlement retry/failure, and restart recovery.

Every failure asserts the exact kill state, persisted events, allowed next operations, absence of a
blind POST retry, and required authoritative reads.

### 15.3 Isolation and attack tests

Process-boundary tests attempt prohibited routes, arbitrary URLs, malformed and oversized IPC,
request replay, altered capability/manifest digests, expired deadlines, secret reflection in every
error field, coordinator crash, signer crash, and inherited-descriptor leakage.

Seeded canary secrets are searched in captured logs, exceptions, database files, JSON exports, and
dashboard responses. No test prints the canary on failure.

### 15.4 Ledger and reconciliation tests

Property tests generate order/trade/fee/settlement sequences and prove double-entry conservation.
Reconciliation compares independently fetched account state with events and ledger postings. Any
missing, duplicate, reordered, or contradictory event prevents exact reconciliation and P&L
publication.

### 15.5 Authority tests

An authority scan imports every production CLI and factory and proves:

- no live-capability issuer or production verification key exists;
- no live order/cancel/heartbeat command is registered;
- all shipped Polymarket manifests remain LIVE_DISABLED;
- production construction starts killed and cannot clear itself;
- authenticated transport cannot be reached with no verified capability;
- public collection still has no secret-bearing dependency; and
- dashboard routes are read-only.

No authenticated smoke test, live network order test, or secret-bearing CI job is permitted while
the venue is LIVE_DISABLED.

### 15.6 Repository verification

Acceptance requires focused unit/integration tests, the complete test suite, Ruff lint and format
checks, clean package build/install, dependency review, migration upgrade/reopen tests, dashboard
asset tests, protocol conformance, authority scan, and independent code review.

## 16. Delivery Sequence

The implementation plan should decompose this design into reviewable tasks in this order:

1. venue-neutral models, authorization contracts, kill switch, and migration 008;
2. protocol source fixtures and conformance harness;
3. canonical Polymarket order/auth encoding with mutation tests;
4. signer IPC protocol and ephemeral secret boundary;
5. typed REST/WebSocket transports and sanitized fake transports;
6. order/trade state machines, unknown-outcome recovery, heartbeat, and cancellation;
7. live ledger, authoritative reconciliation, and restart recovery;
8. offline CLI and read-only dashboard readiness;
9. authority, secret-leak, packaging, and full-suite verification; and
10. independent review and documentation.

Each task keeps production construction LIVE_DISABLED. No intermediate task may add a temporary
live bypass for testing.

## 17. Explicit Non-Goals

- Issuing, importing, generating, validating for production, or activating live credentials or
  capabilities.
- Funding a wallet or account.
- Deposits, withdrawals, transfers, approvals, conversions, merges, splits, redemption, or
  relayers.
- GTC, GTD, passive quoting, market making, maker rewards, incentive assumptions, or builder fees.
- Kalshi, Limitless, a second venue, or cross-venue live coordination.
- Dynamic order resizing, discretionary hedging, or relaxing frozen proof/economics/risk limits.
- Geographic circumvention or interpreting a geoblock response as legal advice.
- Shortening the 45 continuous calendar days of synchronized evidence, fixed Class G thresholds,
  or 30 additional calendar days of queue-aware shadow execution.
- A USD 250 pilot or any other live capital authorization.

## 18. Acceptance Criteria

This increment is complete when:

1. migration 008 upgrades and reopens existing prediction databases without altering prior rows;
2. official-source hashes and protocol fixtures are versioned and stale-source changes fail closed;
3. all current Polymarket L1, L2, EIP-712, FAK/FOK, REST, WebSocket, heartbeat, geoblock, and
   lifecycle conformance fixtures pass offline;
4. one intent deterministically maps to one order fingerprint, and no ambiguous submission causes
   a blind retry;
5. fake-transport end-to-end tests cover partial fills, unknown acknowledgements, cancellation,
   reconnect, heartbeat failure, settlement failure, and restart recovery;
6. every failure mode activates the specified halt/reconcile behavior;
7. live ledger postings conserve value and P&L remains unavailable until exact reconciliation;
8. seeded canary secrets are absent from storage and every observable output;
9. the signer rejects every non-allowlisted route and every invalid capability or manifest;
10. production factories cannot obtain or clear a live execution capability, shipped manifests
    remain LIVE_DISABLED, and the authority scan passes;
11. the CLI exposes offline conformance only, and the dashboard is read-only with no live control;
12. the full repository verification gates pass; and
13. documentation states the remaining evidence, eligibility, custody, credential, activation,
    and user-approval requirements without implying that code completion satisfies them.

## 19. Deferred Activation Work

A later activation specification, requested and approved separately, must resolve at least:

- completed 45-day and additional 30-day calendar evidence with fixed Class G results;
- current legal, jurisdiction, KYC, tax, terms, custody, and account eligibility review;
- the production capability signature scheme, issuer key custody, revocation, duration, and
  clearance ceremony;
- credential provisioning and rotation without adding a secret-bearing CLI;
- wallet funding, allowance, custody, incident response, and recovery procedures;
- the maximum-capital pilot and explicit loss/kill limits;
- narrowly authorized cancellation behavior after capability expiry;
- production monitoring and on-call ownership; and
- a new explicit user decision to enable any live call.

Until that work is approved and implemented, LIVE_DISABLED is not a warning label around a usable
order path. It is a hard, independently enforced absence of authority.
