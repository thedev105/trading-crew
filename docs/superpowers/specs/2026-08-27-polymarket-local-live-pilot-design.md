# Polymarket Local Live Pilot

**Date:** 2026-08-27
**Status:** Design approved in chat on 2026-08-31; implementation planning pending review
**Scope:** A capability-gated, loopback-only live-execution pilot for one individual operator,
one dedicated Polymarket wallet, and deterministic single-Polymarket structural strategies
**Operator context:** The operator is physically located in the Philippines and has stated that
they possess written confirmation of their eligibility. The system stores only a dated reference
and cryptographic hash of that external attestation, never the document itself.
**Authority boundary:** Implementing or testing this specification does not authorize a live
request, credential derivation, wallet funding, token approval, or order. Only the operator may
initiate setup and live activation from the local control UI after every gate passes.

Companion specifications:

- [Multi-Venue Prediction-Market Structural Opportunity System](2026-08-14-multi-venue-prediction-market-structural-opportunity-system-design.md)
- [Polymarket Live-Disabled Execution Hardening](2026-08-25-polymarket-live-disabled-execution-hardening-design.md)

## 1. Decision

Add a separately launched Polymarket execution-control process beside the existing read-only
Market Atlas dashboard. The ordinary dashboard remains a GET/HEAD-only observer. The new process
is a loopback-only control plane with its own origin, routes, content-security policy, session,
passkey challenges, and signer lifecycle. It is not an extension of the dashboard's current
read-only HTTP server.

The pilot reuses the protocol conformance, capability verification, signer, immediate-order state
machines, ledger, kill switch, and reconciliation foundations delivered by the live-disabled
hardening increment. This increment adds the missing production authority components:

- a local capability issuer gated by a platform passkey;
- immutable server ceilings and user-lowerable requested limits;
- OS-keychain-backed credential custody and per-launch unlock;
- signer-only, user-triggered CLOB credential provisioning for the dedicated wallet;
- a dedicated execution cockpit and explicit confirmation ceremonies;
- durable session, nonce, revocation, presence, and activation records;
- production kill clearance gated by exact reconciliation and a new passkey assertion; and
- a staged activation ceremony whose first live strategy is smaller than the normal pilot.

Every single-Polymarket deterministic proof family may qualify. Cross-venue opportunities remain
visible but disabled until every participating venue receives a separate activation design,
implementation, evidence checkpoint, and operator approval.

The implementation contains all three authorization modes:

1. one exact risk-reducing order;
2. one exact complete strategy; and
3. one 15-minute automation session.

The first real action is restricted to a small, manually approved complete deterministic strategy.
Automation-session authority is implemented at the server and signer boundaries but is hard-disabled:
no UI input, stored record, or configuration value can issue or use it. Enabling automation is a
separate, explicit activation decision after the evidence clock, shadow-run requirements, and
manual-pilot reconciliation gates have passed. Every manual capability remains independently
passkey-gated and must also pass every stage in this document before it can construct authenticated
transport.

## 2. Goals and Success Definition

The pilot answers this question:

> Can one locally present operator deliberately authorize tightly bounded Polymarket execution
> without putting wallet secrets, venue credentials, arbitrary order construction, or durable live
> authority in the browser, coordinator, database, logs, or ordinary dashboard?

Success means the operator can:

1. unlock the dedicated wallet and venue credentials from the OS keychain at each application
   launch without exposing secret values to the browser or coordinator;
2. inspect one coherent readiness report covering eligibility, protocol, evidence, wallet,
   allowance, balances, signer health, streams, kill state, and reconciliation;
3. lower requested risk limits in the UI but never exceed compiled ceilings;
4. review deterministic proof, current and stressed economics, exact legs, incomplete-leg risk,
   and the frozen recovery path before authorization;
5. authorize one of the three modes with a typed confirmation and a fresh local passkey assertion;
6. see every lifecycle, balance, risk, loss, presence, and reconciliation transition in a clear
   live UI;
7. stop and kill the session immediately from one prominent control;
8. recover from partial fills, UNKNOWN outcomes, unexpected resting state, stream failures, stale
   account state, and restarts without blind submission retries; and
9. prove through offline automated tests that no unapproved path can construct authenticated
   transport or perform a venue mutation.

Success does not mean the strategy will be profitable, that a venue geoblock result establishes
legal eligibility, or that implementation completion satisfies the evidence and activation gates.

## 3. Approved Pilot Envelope

### 3.1 Immutable ceilings

The following ceilings are compiled into the production policy and included in every capability
and plan hash:

| Control | Immutable ceiling |
| --- | ---: |
| Dedicated wallet trading equity | USD 250 |
| One order notional | USD 10 |
| One complete strategy gross notional | USD 25 |
| Automation-session duration | 15 minutes |
| Automation-session maximum deployed capital | USD 50 |
| Concurrent active strategies | 1 |
| Session realized plus unrealized loss | USD 5 |
| UTC-day realized plus unrealized loss | USD 10 |

The UI may request lower values. The control server, coordinator, and signer independently apply
the minimum of the requested value and the immutable ceiling. A client-supplied increase is
rejected rather than clamped silently.

"Strategy gross notional" includes every normal leg and every recovery order attempted under the
strategy. Recovery receives a separate operation grant but no additional capital allowance. A
recovery action must reduce worst-case incomplete exposure, must not increase deployed capital,
and remains subject to the USD 10 order and USD 25 strategy ceilings.

"Wallet trading equity" is reconciled collateral plus conservatively marked positions controlled
by the dedicated wallet. An unexpected deposit, transfer, payout, or profit that takes the wallet
above USD 250 blocks new entries and engages the kill state until authoritative reconciliation and
an operator-managed reduction outside the application. The application never moves the excess.

Session loss is measured from reconciled session-start equity. UTC-day loss is measured from
00:00 UTC reconciled equity and adjusted only for confirmed external cash flows. Open positions
use conservative executable unwind marks, not midpoints. If a required mark or cash-flow
classification is unavailable, loss is UNKNOWN and execution stops.

### 3.2 Automatic stop conditions

Any of the following revokes normal execution authority and engages the durable account-scoped
kill state:

- an UNKNOWN order, trade, settlement, balance, or allowance outcome;
- an unexpected resting order or any GTC/GTD/post-only instruction;
- a reconciliation gap or ledger divergence;
- stale rules, proofs, books, fees, balances, allowances, eligibility, protocol, or activation
  evidence;
- public market-stream, authenticated user-stream, browser-heartbeat, signer, network, sleep, or
  screen-lock failure;
- a capability mismatch, expiry, nonce replay, revocation, clock error, or origin failure;
- a wallet, order, strategy, session, loss, position, or incomplete-leg limit breach;
- a source-hash or official-protocol change that has not passed conformance review; or
- detection of a secret canary in any observable or persisted surface.

The kill state never clears automatically.

## 4. Architecture and Trust Boundaries

### 4.1 Existing read-only observer

The existing Market Atlas dashboard remains a separate process that:

- opens the prediction database read-only;
- serves GET/HEAD routes and the existing observation-only revision stream;
- holds no wallet key, API key, passphrase, capability signing key, or authenticated transport;
- cannot reach signer IPC or any control endpoint; and
- may display sanitized pilot readiness and lifecycle state but cannot initiate or approve action.

Activating the pilot must not add a mutation route, form action, credential field, command channel,
or signer dependency to this server.

### 4.2 Execution-control process

The new execution-control process binds only to loopback and serves the Pilot UI from a distinct,
fixed origin. It owns:

- browser session and CSRF enforcement;
- platform-passkey registration and assertion verification;
- immutable ceiling evaluation and requested-policy persistence;
- deterministic opportunity selection and server-side plan reconstruction;
- capability issuance, nonce claiming, revocation, and audit projections;
- browser-presence and native sleep/screen-lock health;
- coordinator lifecycle; and
- sanitized live read models for the execution cockpit.

The browser submits only stable opportunity IDs, a requested mode, lower requested caps, and
challenge responses. It cannot submit canonical order bodies, routes, hosts, headers, signatures,
fee assumptions, proof results, economics results, recovery legs, wallet addresses, or arbitrary
token IDs. The server resolves and recomputes all action material from persisted evidence and
fresh authoritative reads.

### 4.3 Local capability authority

A local issuer creates short-lived signed capabilities only after a valid platform-passkey
assertion over an exact action challenge. The challenge binds:

- operator credential ID and browser session;
- wallet and account fingerprints;
- mode and selected opportunity, plan, strategy, or session ID;
- proof, rule, fee, economics, risk, protocol, route-set, manifest, eligibility, and evidence
  hashes;
- requested limits and immutable ceiling hash;
- allowed operations and recovery operation set;
- not-before, expiry, nonce, and challenge ID; and
- exact confirmation text displayed to the operator.

The capability signing key is ephemeral per application launch and held only by the local
capability authority. The coordinator and signer receive its public verification key through an
authenticated inherited channel. Restart invalidates all outstanding capabilities.

Capabilities are single-purpose and single-use where possible. They cannot be extended, widened,
renewed, or converted between modes. Creating another capability always requires a new challenge
and passkey assertion.

### 4.4 Signer sidecar and secret boundary

The signer remains a separate minimum-dependency process. Secret material is retrieved from the OS
keychain only after an explicit password or biometric unlock at every application launch and
passes to the signer through inherited descriptors. The descriptors are closed immediately after
read. Secrets never pass through command-line arguments, environment variables, browser fields,
HTTP, coordinator IPC requests, DuckDB, logs, metrics, traces, crash reports, screenshots, or
support bundles.

The initial implementation uses a secret-store interface with a macOS Keychain adapter for the
current operator environment. Unsupported operating systems fail closed until they receive an
equivalent reviewed adapter. The dedicated wallet is created and backed up outside this
application. Secret enrollment uses the native OS keychain interface, never the Pilot UI or CLI.

If the dedicated wallet has no CLOB API credentials, the Pilot UI may initiate a separate
credential-provisioning ceremony. A fresh passkey challenge binds the wallet fingerprint, current
eligibility and geoblock evidence, protocol/source hashes, and the single exact create-or-derive
credential operation. The signer performs that allowlisted L1 operation and writes the returned
API key, secret, and passphrase directly to the OS keychain. It returns only credential and account
fingerprints plus a sanitized result code. The browser, coordinator, database, and logs never
receive the credential values.

Credential provisioning uses a one-time `CredentialProvisioningGrant`, not an
`ExecutionCapability`. It expires after 60 seconds, cannot invoke an order, cancellation,
heartbeat, transfer, approval, or arbitrary route, and cannot be converted into trading authority.
Automated tests use a fake credential service; only the operator may trigger the real ceremony
from the local UI.

Authenticated Polymarket transport is constructed inside the signer only after the signer has
independently verified the current capability, account, protocol, route set, limits, geoblock,
kill, and reconciliation inputs. The transport closes when the action or session completes.

### 4.5 Primary and recovery authority

Each approved plan produces two independently verifiable grants:

- **Primary authority** may initiate or continue only the selected healthy plan or session.
- **Recovery authority** may inspect account state, cancel only a known bound order, or submit only
  frozen risk-reducing recovery intents from the approved plan.

Engaging the kill state destroys primary authority. Recovery authority remains sealed inside the
coordinator and signer and may survive for at most 120 seconds to reduce an already-created
exposure. It cannot start a strategy, change direction, increase worst-case loss, increase deployed
capital, loosen price bounds, or create a new hedge. After expiry, only read-only reconciliation
continues; further mutation requires a new recovery-specific passkey approval.

### 4.6 Network and browser security

The first implementation uses one fixed `http://localhost:<configured-port>` WebAuthn origin and
RP ID `localhost`, relying on browsers' loopback secure-context treatment. The server binds only to
explicit loopback addresses and rejects every Host or Origin other than the configured localhost
origin. It does not listen on LAN interfaces.

The control server requires:

- no CORS responses;
- exact same-origin checks on every state-changing request;
- a SameSite=Strict, HttpOnly browser session cookie;
- a per-session CSRF secret and single-use action challenge;
- strict CSP with no remote script, style, frame, image, font, worker, or connection source;
- no inline executable script and no third-party assets;
- frame denial, MIME sniffing denial, restrictive permissions policy, and no referrer;
- bounded request sizes, schema-exact JSON, fixed route tables, and rate limits; and
- startup refusal when the configured origin, port, binding, passkey RP ID, or secret-store health
  is inconsistent.

Browser presence is a signed-session heartbeat every two seconds. Two missed heartbeats or five
seconds without a valid heartbeat, whichever occurs first, engages the kill state. Native sleep
or screen-lock notification kills immediately. The control page must remain open, the machine
awake, and the operator locally present for automation authority to remain valid.

## 5. Eligibility, Protocol, and Activation Evidence

### 5.1 Operator eligibility record

The operator is an individual physically located in the Philippines. The operator has stated that
they possess written eligibility confirmation. The system represents that confirmation with an
append-only record containing only:

- an operator-chosen opaque reference;
- document SHA-256 computed outside ordinary logs;
- venue, account-holder type, physical jurisdiction, and wallet fingerprint;
- issuer or reviewer category without sensitive identity details;
- review date and explicit expiry or mandatory re-review date;
- scoped assertions selected by the operator; and
- a statement that the record is an operator-supplied gate, not a legal determination by the
  software.

The source document is never imported, copied, displayed, or committed. A missing, expired,
mismatched, or superseded reference blocks capability issuance.

As retrieved on 2026-08-27, Polymarket's public geographic-restrictions page does not list the
Philippines among its blocked countries. That observation is time-sensitive and does not establish
compliance with Philippine law, Polymarket's Terms, KYC/account rules, or any other obligation.
The application therefore also requires the operator's current attestation and performs the
official venue geoblock check immediately before capability issuance and again before each
strategy. It never circumvents a restriction or uses a VPN/proxy to alter the result.

### 5.2 Fresh protocol checkpoint

The existing implementation is pinned to a 2026-08-25 protocol snapshot. Live activation requires
a new source checkpoint immediately before implementation acceptance and again at Stage 4. All
official source hashes, exact request/response fixtures, domains, contracts, routes, authentication
methods, wallet signature types, order types, rounding, minimums, fees, heartbeat behavior, and
user-stream events must be reviewed and pass conformance.

This is especially important for the dedicated new-wallet path: current official quickstart
material retrieved on 2026-08-27 illustrates a deposit-wallet flow and a signature type that may
differ from the current implementation's assumptions. The signer must discover and bind the
actual configured wallet/funder/signature model. An unsupported or ambiguous account form blocks
activation; it is never coerced to a default signature type.

### 5.3 Evidence clock

Activation requires persisted, independently recomputed evidence—not a UI checkbox—for every live
proof family:

- 45 continuous calendar days of synchronized rules and executable books;
- at least 25 opportunities surviving current fees, executable depth, and one-second latency;
- at least 10 opportunities surviving five-second latency;
- median conservative net surplus of at least 0.75%;
- median executable capacity of at least USD 100;
- projected annual contribution of at least 2% of total equity;
- conservative return on assigned capital at least five percentage points above the approved cash
  benchmark;
- zero false guaranteed-payoff claims in manual review;
- simulated 99th-percentile incomplete-leg loss below 0.25% of equity;
- simulated drawdown below 8%; and
- 30 additional calendar days of queue-aware shadow execution with positive results excluding
  rewards, no risk breach, and complete reconciliation.

Strong evidence for one proof family does not authorize another. Missing qualifying evidence keeps
that family disabled without preventing already-qualified single-Polymarket families from being
shown. Cross-venue evidence cannot authorize cross-venue live execution.

### 5.4 Manifest promotion and invalidation

The shipped Polymarket manifest remains `LIVE_DISABLED` throughout implementation and automated
verification. After Stages 0–2 pass, the Stage 3 activation ceremony may append a new versioned
Polymarket `VenueManifest` with `jurisdiction_review_status=ELIGIBILITY_REVIEWED`,
`implementation_state=LIVE_ELIGIBLE`, and `authenticated_live_capability=true`. That manifest
binds the reviewed official source hashes and opaque review identity. The accompanying pilot
activation record binds it further to the one approved wallet/account, operator attestation,
policy, protocol, and evidence digests.

Manifest promotion creates no capability and cannot submit an order. The coordinator and signer
still require the matching short-lived account-bound capability for every mutation. The ordinary
dashboard is updated only to display the sanitized current manifest and pilot posture; it remains
unable to promote or invalidate a manifest itself.

An expired attestation, failed live geoblock check, source/protocol change, evidence failure,
account mismatch, or explicit operator deactivation appends a new `LIVE_DISABLED` manifest version,
revokes outstanding capabilities, and engages kill. Historical manifest versions remain intact.

## 6. Deterministic Strategy Policy

### 6.1 Enabled proof families

The live selector may consume only deterministic, exhaustive proof artifacts already supported by
the research system:

- binary complement;
- exhaustive multi-outcome coverage, including reviewed negative-risk relationships;
- implication; and
- within-Polymarket equivalence.

The proof must bind current immutable market rules, resolution sources, outcomes, token IDs, and
payoff states. AI may nominate a relationship for deterministic evaluation, but no AI output,
language-model score, wallet copying, sentiment, or discretionary prediction may create, approve,
rank, resize, or execute a trade.

The application does not perform split, merge, conversion, redemption, deposit, withdrawal,
transfer, or approval operations. A proof that requires one of those operations to guarantee its
economics is not live-eligible in this pilot.

### 6.2 Eligibility computation

A strategy is eligible only when all of these are true at one coherent information cutoff:

1. current rule evidence and human rule attestation match the proof;
2. depth, fees, tick sizes, minimum sizes, and account data are fresh;
3. conservative surplus is positive after actual depth, fees, basis, latency, rounding,
   operational cost, capital lockup, and incomplete-leg reserve;
4. conservative surplus remains positive under the approved five-second latency stress;
5. modeled incomplete-leg loss is below 0.25% of total equity and below every tighter pilot cap;
6. the exact plan has a successful shadow replay under the current policy and protocol hashes;
7. wallet, balance, allowance, eligibility, geoblock, signer, stream, protocol, kill, and
   reconciliation gates are complete; and
8. normal and recovery legs fit the requested limits and immutable ceilings.

Favorable rewards, points, rebates, maker assumptions, unproved conversion value, or future
liquidity receive no required-profit credit.

### 6.3 Ranking

Eligible strategies use this deterministic ranking, in order:

1. lowest incomplete-leg loss divided by deployed capital;
2. highest five-second-stressed conservative surplus;
3. highest current executable capacity; and
4. stable proof ID as the final tie-breaker.

No opaque composite score is introduced. The UI displays each ranking field and the first field
that broke a tie.

### 6.4 Authorization modes

**Exact order** authorizes one precomputed FAK/FOK order that reduces an existing known exposure.
It may close, unwind, or recover but cannot initiate naked or directional exposure.

**Complete strategy** authorizes one precomputed multi-leg strategy and its bounded recovery tree.
The plan, leg order, sizes, prices, deadlines, recovery intents, and stop conditions are frozen
before authorization.

**Automation session** authorizes multiple eligible complete strategies for 15 minutes. The
operator approves once at session start; the system does not prompt for every opportunity. Only
one strategy may be active at a time. The session cannot extend itself, increase limits, switch
wallets, enable another proof family, or add another venue.

### 6.5 Leg sequencing and immediate orders

Only FAK and FOK are accepted. Post-only, GTC, GTD, passive quoting, and resting strategies are
rejected structurally at the UI, plan, coordinator, signer, and route boundaries.

The coordinator selects the leg order that minimizes worst-case incomplete exposure. It persists
the intent before signing, submits the exact canonical envelope once, reads and persists the
authoritative result, refreshes all affected evidence, and only then decides whether the next
frozen leg remains eligible. A timeout or ambiguous acknowledgement is UNKNOWN; the order POST is
never blindly retried.

Normal continuation stops on any anomaly. Only the precomputed risk-reducing recovery branch may
remain available.

## 7. Capability and Session Lifecycles

### 7.1 Capability contents

The existing `ExecutionCapability` contract is extended with:

- authorization mode and parent action/session ID;
- exact plan, strategy, proof-family, and recovery-policy hashes;
- requested policy hash and immutable ceiling hash;
- maximum session duration, deployment, strategy count, and concurrent count;
- session and UTC-day loss limits;
- browser-session and passkey-assertion hashes;
- operator-presence deadline;
- primary or recovery grant type; and
- explicit single-use semantics for order and strategy modes.

Raw signed bundles and passkey assertions remain ephemeral. Persistence stores sanitized digests,
verification results, and public challenge metadata only.

### 7.2 Mode lifecycles

An exact-order capability expires when the order reaches a reconciled terminal result or after 60
seconds, whichever comes first. A complete-strategy capability expires when the strategy and any
recovery reach reconciliation or after five minutes, whichever comes first. An automation-session
capability expires after 15 minutes and may not start a new strategy in its final 60 seconds.

All modes expire immediately on stop, kill, restart, wallet lock, passkey credential removal,
source invalidation, or account mismatch. Expiry blocks new normal actions. The independently
bounded recovery grant follows section 4.5.

### 7.3 Nonces, revocation, and audit

Every challenge, capability, primary operation, and recovery operation has a unique nonce. Nonce
claims are persisted atomically before the corresponding mutation. Reuse or a payload mismatch is
a kill event. Revocation is append-only and takes effect at the issuer, coordinator, and signer.

An audit projection must reconstruct:

- what the operator saw;
- what they typed and approved, represented by hashes and safe public fields;
- which evidence and policy versions were current;
- which capabilities and operations were allowed or rejected;
- what the venue authoritatively reported;
- when presence, stream, signer, or network health changed; and
- why execution continued, recovered, stopped, or remained killed.

It must not reconstruct any secret.

## 8. Data and Persistence

Migration 011 adds append-only pilot records after the existing execution migrations:

- `pilot_eligibility_attestation_refs` — external reference, scope, dates, and hashes only;
- `pilot_policy_profiles` — requested limits, immutable ceiling hash, proof-family enablement, and
  policy hash;
- `pilot_activation_ceremonies` — stages, readiness digest, passkey assertion digest, wallet
  fingerprint, result, and first-strategy reconciliation;
- `pilot_credential_provisioning_events` — wallet fingerprint, protocol/source hashes, grant
  digest, sanitized result, credential fingerprint, and time, never credential values;
- `pilot_authorization_challenges` — safe challenge fields, expiry, used/rejected state, and hash;
- `pilot_capability_events` — issued/verified/rejected/revoked/expired digests without raw bundles;
- `pilot_nonce_claims` — globally unique challenge, capability, and operation claims;
- `pilot_execution_sessions` — mode, bounds, lifecycle, loss state, presence state, and result;
- `pilot_presence_events` — start, loss, sleep, lock, reconnect, and terminal transitions rather
  than every two-second heartbeat; and
- `pilot_kill_clearance_events` — discrepancy evidence, exact reconciliation hash, new passkey
  assertion digest, and clearance result.

Existing plans, intents, signed envelopes, order/trade events, ledger postings, reconciliations,
kill events, operation claims, and authoritative economics remain canonical. The new records refer
to them by stable ID and record hash instead of duplicating them.

All tables retain the repository's schema-versioned canonical JSON and record-hash conventions.
History is append-only. State is derived by replay and authoritative reads, never by rewriting a
prior event. Raw eligibility documents, capability bundles, passkey assertion bytes, wallet keys,
API credentials, auth headers, and authenticated subscription frames are forbidden from storage.

## 9. End-to-End Data Flow

### 9.1 Startup

1. Start the execution-control process on its exact loopback origin.
2. Derive an engaged kill state before loading any secret.
3. Verify database migrations, append-only histories, protocol hashes, policy ceilings, passkey
   registration, and native lock/sleep monitoring.
4. Ask the operator to unlock the required keychain items with password or biometric approval.
5. If CLOB credentials are absent, remain setup-only until the operator completes the separate
   credential-provisioning ceremony; provisioning does not clear kill state.
6. Start the signer through inherited descriptors and verify only its wallet/account and
   credential fingerprints.
7. Construct read-only authenticated account transport long enough to fetch open orders, trades,
   balances, and allowances.
8. Reconcile all nonterminal and prior-session records.
9. Close that transport unless the operator proceeds directly into a new authorization ceremony.
10. Display readiness. Startup never clears kill state automatically.

### 9.2 Approval ceremony

1. The operator opens a currently eligible opportunity or the session configuration.
2. The server refreshes all market, rule, fee, risk, account, protocol, and eligibility inputs and
   reconstructs the exact plan.
3. The UI displays the plan and generates one of these forms using the actual requested cap:
   `ORDER <amount> USD`, `STRATEGY <amount> USD`, or
   `SESSION 15 MIN <amount> USD`.
4. The operator types the displayed phrase exactly.
5. The browser requests a single-use WebAuthn challenge bound to the entire action digest.
6. The operator completes local passkey/biometric verification.
7. The issuer verifies origin, RP ID, challenge, credential, counter/backup state where supplied,
   session, phrase, evidence, freshness, reconciliation, and limits.
8. The issuer creates primary and recovery grants; raw grant bytes do not enter persistence.
9. The coordinator and signer independently reconstruct and verify their authority contexts.
10. Only then may the signer construct authenticated transport.

### 9.3 Execution and reconciliation

For every leg, the coordinator persists intent, signs, persists the public envelope, submits once,
persists the sanitized response, obtains authoritative order/trade/account reads, posts reconciled
ledger entries, and re-evaluates continuation. The user stream accelerates observation but is never
authoritative after a gap or reconnect.

When a strategy completes, no new strategy starts until order, trade, settlement, balance,
allowance, position, and ledger state reconcile exactly. During a session, the remaining time,
deployment, session loss, UTC-day loss, and one-strategy limit are recomputed from authoritative
state before every new plan.

### 9.4 Stop and kill

The prominent Stop-and-kill action is always available while the control UI is connected. It
atomically revokes primary authority, persists the kill event, stops new work, and allows only the
sealed recovery policy. Closing the UI, losing heartbeats, sleeping, locking, or losing required
streams has the same normal-authority effect.

The control server never reports a safe terminal state until authoritative reconciliation is
exact. If reconciliation cannot complete, it remains killed and displays the required manual next
step without inventing a result.

### 9.5 Kill clearance

Clearance requires:

1. no active or unknown submission;
2. authoritative open-order, trade, balance, allowance, position, settlement, and ledger reads;
3. zero unexplained reconciliation difference;
4. current eligibility, protocol, policy, source, signer, and stream evidence;
5. operator review of the triggering discrepancy and recovery outcome;
6. an exact clearance confirmation phrase; and
7. a new platform-passkey assertion.

Clearance creates an append-only event but no standing capability. A separate action ceremony is
still required to trade.

## 10. Pilot UI

The dedicated Pilot view extends the Market Atlas visual language—deep slate surfaces, clear cyan
information, amber attention, coral danger, disciplined typography, and restrained motion—while
remaining visually and operationally distinct from the observer dashboard.

### 10.1 Readiness

The readiness section shows:

- eligibility-attestation reference, jurisdiction scope, review age, and expiry;
- official geoblock result and timestamp;
- protocol version, official source hashes, conformance, and account signature model;
- wallet fingerprint, reconciled trading equity, collateral, positions, balance, and allowances;
- 45-day Class G and additional 30-day shadow qualification by proof family;
- signer/keychain status, public/user stream health, browser presence, and native lock state;
- kill trigger, reconciliation result, and required recovery action; and
- activation stage and whether the reduced first-live ceiling still applies.

Each item has a plain-language status, age, evidence link, and explicit blocker code. Green styling
never substitutes for text.

### 10.2 Limits

The limits section exposes requested values for wallet use, order notional, strategy gross
notional, session deployment, session duration, concurrent strategies, session loss, and UTC-day
loss. Every control displays its immutable ceiling beside it. Increases above the ceiling are
rejected inline and by the server. Limit changes affect only future challenges and never modify an
issued capability.

### 10.3 Opportunity approval

The approval view includes:

- proof family and exhaustive payoff explanation;
- markets, outcomes, rules, resolution sources, and exact token-bound legs;
- FAK/FOK type, size, limit price, fee, tick/minimum checks, and leg order;
- current and five-second-stressed conservative economics;
- current depth, capacity, capital lock, and evidence freshness;
- maximum incomplete-leg exposure and every modeled terminal state;
- frozen recovery tree and the circumstances selecting each branch;
- ranking fields and deterministic tie-break reason;
- all evidence and policy hashes in an expandable audit panel; and
- capability duration and requested maximum exposure.

The final summary remains visible during typed confirmation and passkey approval. No button labeled
only "Confirm" is sufficient; mode and maximum exposure appear in the button and accessible name.

### 10.4 Live session

The live section shows remaining authorization time, available deployment, session and UTC-day
loss budget, current strategy, leg progress, order/trade/settlement timeline, authoritative versus
stream-derived state, browser heartbeat, signer health, reconciliation, and a dominant
Stop-and-kill control.

Financial totals disappear when snapshot consistency or reconciliation fails. Motion pauses for
STALE, DISCONNECTED, UNKNOWN, or INCONSISTENT state. The interface supports keyboard-only use,
visible focus, semantic landmarks and tables, screen-reader announcements, accessible contrast,
reduced motion, and responsive desktop/tablet/mobile layouts. During live authority, mobile layout
is monitoring and stop-only unless the same platform passkey and exact origin are present.

## 11. Operator Documentation

The release includes documentation written for an individual operator rather than an API
integrator:

1. **Ten-minute setup:** dedicated wallet expectations, external backup, OS Keychain enrollment,
   passkey registration, signer-only credential provisioning, signer unlock, manual funding,
   manual allowance setup, and read-only readiness verification.
2. **Concept guide:** binary complement, exhaustive outcomes, implication, equivalence,
   conservative surplus, stressed economics, incomplete-leg loss, capacity, FAK/FOK, UNKNOWN,
   settlement, and reconciliation in plain language.
3. **Mode walkthroughs:** exact risk-reducing order, complete strategy, and 15-minute automation
   session, with annotated screenshots and every configurable field.
4. **Activation runbook:** evidence gates, eligibility reference, protocol refresh, operator drills,
   reduced first strategy, and full-pilot unlock.
5. **Recovery playbooks:** partial fill, FOK rejection, UNKNOWN acknowledgement, unexpected resting
   order, stream loss, stale balance/allowance, settlement retry/failure, session/daily loss,
   sleep/lock, browser closure, and restart.
6. **Preflight and post-session checklists:** exact human steps, expected evidence, stop conditions,
   and when not to proceed.
7. **Secret-safety guide:** never paste wallet keys, API secrets, passphrases, or seed phrases into
   the Pilot UI, CLI arguments, `.env` files, source files, logs, screenshots, issue trackers,
   chat, email, or support messages.

Documentation makes clear that funding, withdrawal, allowance changes, redemption, wallet backup,
tax decisions, and legal review occur outside the application.

## 12. Error Handling and Recovery Rules

Errors cross process boundaries only as stable codes, safe public identifiers, timestamps, and
evidence hashes. Arbitrary venue response text is redacted inside the signer before IPC.

The following rules are absolute:

- no ambiguous order POST is retried blindly;
- cancellation is complete only after authoritative order state confirms it;
- an unexpected live/resting acknowledgement is a fault even if it later fills;
- WebSocket events do not resolve a REST contradiction;
- session expiry does not imply an order was cancelled;
- missing market data does not imply zero risk or zero loss;
- recovery cannot invent a hedge or enlarge the approved plan;
- a signer/coordinator/browser restart never resumes authority;
- a failed recovery remains visibly killed; and
- P&L is unavailable until exact ledger and venue reconciliation.

When automated recovery authority is unavailable or expired, the application retains read-only
reconciliation and gives a specific operator action. It never falls back to an unbounded emergency
credential.

## 13. Testing and Performance

Automated tests must never contact live Polymarket endpoints, derive live credentials, unlock the
operator's keychain entries, sign with the real wallet, or submit/cancel an order. All protocol,
UI, capability, signer, and recovery tests use deterministic fake transports, injected clocks,
test-only passkeys, temporary secret stores, and canary credentials.

### 13.1 Required coverage

- Capability signature, canonical bytes, mode, expiry, nonce, replay, revocation, recovery, and
  immutable ceiling enforcement at issuer, coordinator, and signer boundaries.
- WebAuthn origin, RP ID, challenge, session binding, counter/backup-state policy, replay, CSRF,
  Host validation, cookie policy, CSP, CORS absence, and loopback-only binding.
- Secret enrollment abstraction, per-launch unlock, inherited descriptors, process shutdown,
  transport creation/destruction, and secret-leak scans.
- Every proof family, eligibility gate, ranking tie-break, economics stress, sizing boundary,
  rounding rule, minimum order, and risk calculation.
- Exact order, complete strategy, and automation-session lifecycles.
- Full fill, partial fill, FOK rejection, delayed/live/unknown acknowledgement, resting order,
  stale data, rate limit, network loss, stream gap, browser heartbeat loss, sleep, screen lock,
  stop, loss limit, settlement retry/failure, restart, and kill clearance.
- Proof that recovery is risk-reducing, frozen, time-bounded, and unable to increase capital or
  invoke any non-allowlisted route.
- Append-only migrations, restart replay, authoritative reconciliation, ledger conservation, and
  sanitized audit reconstruction.
- UI state, accessibility, responsive behavior, confirmation copy, protected financial totals,
  screenshots, and operator-runbook field accuracy.
- Package/import scans proving the observer dashboard has no control, signer, credential,
  capability-issuer, or authenticated-transport dependency.

### 13.2 Test-speed budget

The new suite must use in-memory models, shared immutable fixtures, fake transports, injected
clocks, and session-scoped expensive setup. Network waits, real sleeps, repeated key generation,
and per-test database reconstruction are prohibited unless a focused test proves that exact
boundary.

The current post-hardening full-suite baseline is 157.42 seconds on the reference development
machine. The pilot increment targets no more than 15% p50 regression under the same environment
(approximately 181 seconds), with focused changed-area tests completing in under 30 seconds. Any
new test taking more than two seconds must be reported and justified. Performance reporting shows
the slowest tests and separates browser visual verification from fast logic tests so local
iteration remains quick without weakening the full gate.

### 13.3 Verification gate

Acceptance requires focused tests, full tests, lint, format, type/package checks where configured,
clean installation, migration upgrade/reopen, protocol conformance, authority scans, secret
canaries, deterministic responsive screenshots, accessibility checks, and independent review.
No authenticated smoke test or real-order test is part of implementation verification.

## 14. Staged Rollout

### Stage 0: Offline verification

All automated, adversarial, property, migration, packaging, and browser tests pass against fake
transports. Production starts killed, with no capability and no authenticated transport.

### Stage 1: Shadow qualification

The system independently recomputes the 45-day Class G and additional 30-day shadow gates from
persisted raw-first evidence for each proof family. A missing day, invalid lineage edge, changed
threshold, reward-dependent result, risk breach, or unreconciled paper result blocks that family.

### Stage 2: Operator drills

The operator completes documented simulated sessions for all three authorization modes and every
kill/recovery scenario. Drills use fake transport but the production UI, passkey ceremony, limits,
signer boundary, and persistence paths. Results are append-only activation evidence.

### Stage 3: Activation readiness

The operator reviews and confirms:

- current external eligibility-attestation reference and hash;
- current official geoblock and Terms sources;
- fresh protocol fixtures and conformance;
- dedicated wallet/account/signature fingerprints and sanitized credential-provisioning result;
- manual funding and allowances;
- immutable and requested limits;
- exact reconciliation and clear discrepancy review;
- completed evidence and drills; and
- first-live reduced ceiling.

A passkey-protected activation ceremony may clear the startup kill state but creates no standing
trading capability. Implementation work and automated verification stop here.

### Stage 4: Operator-triggered first live strategy

Only the operator may enter Stage 4 from the local UI. The first live authorization permits one
complete deterministic strategy, one at a time, with maximum deployed capital equal to the lower
of USD 5 or the smallest venue-valid complete-strategy size. If the venue minimum or executable
structure requires more than USD 5, activation stops; the system does not raise the limit.

Every leg, settlement, balance, allowance, position, and ledger entry must reconcile exactly. Any
anomaly leaves the pilot killed. After a clean first strategy, a second explicit passkey ceremony
unlocks the normal approved ceilings for manual capabilities only. Automation remains hard-disabled
until its separate activation decision. No automatic promotion exists.

## 15. Delivery Boundaries

### 15.1 Included

- Local Polymarket capability issuance and verification.
- Platform-passkey action approval.
- macOS Keychain-backed signer bootstrap through an abstract secret-store boundary.
- Passkey-gated signer-only CLOB credential provisioning without browser or coordinator exposure.
- Requested-limit UI under immutable ceilings.
- Deterministic selector for qualified single-Polymarket proof families.
- Exact-order, strategy, and timed-session modes.
- Primary/recovery authority, durable stop/kill, reconciliation, and clearance.
- Dedicated live control UI and operator documentation.
- Offline tests and staged activation evidence.

### 15.2 Excluded

- Any implementation-time, CI, review, or agent-triggered live authenticated request.
- Wallet creation, seed backup, deposits, withdrawals, transfers, approvals, allowance changes,
  split/merge, conversion, redemption, bridge, relayer, or token management.
- Directional prediction, discretionary trades, AI authorization, copied wallets, market making,
  passive orders, GTC, GTD, post-only, rewards, points, or incentive-based economics.
- Kalshi, Limitless, cross-venue live execution, or shared cross-venue capital authority.
- Remote access, LAN binding, cloud deployment, mobile remote control, multi-user roles, or an API
  for third-party execution.
- Legal, tax, KYC, custody, or profitability determinations by the software.
- Relaxing the existing 45-day Class G thresholds or additional 30-day shadow requirement.

## 16. Delivery Sequence

The implementation plan should decompose this design into independently reviewable tasks:

1. activation policy, evidence, session, nonce, revocation, and migration 011 models;
2. immutable ceiling and loss-accounting engine;
3. passkey registration/assertion and exact loopback HTTP security boundary;
4. OS keychain abstraction, launch unlock, credential provisioning, signer bootstrap, and
   lifecycle shutdown;
5. production local capability issuer plus primary/recovery verification extensions;
6. deterministic live selector and frozen plan/recovery compiler;
7. exact-order and complete-strategy orchestration;
8. timed automation session, browser/native presence, stop, and loss enforcement;
9. activation, restart recovery, kill clearance, and audit reconstruction;
10. dedicated Pilot UI, responsive/accessibility verification, and screenshots;
11. setup, concepts, mode, activation, recovery, and secret-safety documentation;
12. authority, secret, performance, packaging, migration, full-suite, and independent review gates.

No task may add a temporary bypass, real smoke test, command-line secret path, or live call. Stage 4
remains a separate operator action after the implementation has passed review.

## 17. Acceptance Criteria

This increment is implementation-ready only when:

1. the ordinary Market Atlas server remains structurally read-only and isolated from execution;
2. the Pilot server is loopback-only, exact-origin, no-CORS, CSRF-protected, CSP-restricted, and
   unable to accept arbitrary action material;
3. secrets remain exclusively in the OS keychain, inherited bootstrap channel, and signer memory;
4. CLOB credential provisioning is separately passkey-gated, signer-only, restricted to one
   exact route, persisted only as sanitized fingerprints, and unable to create trading authority;
5. every launch begins killed and every authority grant requires current reconciliation and a
   fresh passkey-bound challenge;
6. all immutable ceilings are enforced independently by the control server, coordinator, and
   signer, while UI configuration can only lower them;
7. exact-order mode cannot initiate exposure, strategy mode cannot leave its frozen plan, and
   session mode cannot exceed 15 minutes or one concurrent strategy;
8. only FAK/FOK immediate orders reach canonical encoding or authenticated transport;
9. all qualified single-Polymarket proof families use deterministic proof, economics, risk, and
   ranking, while cross-venue and AI authority remain disabled;
10. every anomaly revokes primary authority, engages durable kill, and permits only the bounded
   recovery policy;
11. kill clearance requires exact authoritative reconciliation, explicit discrepancy review, and
    a new passkey assertion;
12. evidence qualification is recomputed from persisted lineage and cannot be asserted through a
    mutable checkbox;
13. the eligibility record contains only the external reference/hash and the live geoblock check
    never claims to establish legal eligibility;
14. protocol/account signature assumptions are refreshed against current official sources and an
    unsupported wallet form fails closed;
15. funding and allowance state is read-only and every value-transfer route remains unavailable;
16. the Pilot UI provides complete readiness, limits, approval, live-session, stop, recovery, and
    audit explanations without exposing secrets;
17. all automated verification is offline, authority and secret-leak scans pass, and full-suite
    performance stays within the approved budget or receives an explicit reviewed exception;
18. Stage 3 completion creates no standing live capability; and
19. only the operator can trigger Stage 4, whose first strategy is capped at the lower of USD 5 or
    the smallest venue-valid complete-strategy size and must reconcile before normal ceilings
    unlock.

## 18. Current Official References

The following sources were checked on 2026-08-27. Their contents are time-sensitive inputs to the
protocol and eligibility gates, not frozen legal conclusions:

- Polymarket geographic restrictions:
  https://help.polymarket.com/en/articles/13364163-geographic-restrictions
- Polymarket Terms of Use: https://polymarket.com/tos
- Polymarket trading overview: https://docs.polymarket.com/trading/overview
- Polymarket trading quickstart: https://docs.polymarket.com/trading/quickstart
- Polymarket L2 client methods: https://docs.polymarket.com/trading/clients/l2
- Polymarket order lifecycle: https://docs.polymarket.com/concepts/order-lifecycle
- Polymarket order API: https://docs.polymarket.com/api-reference/trade/post-a-new-order
- Polymarket error codes: https://docs.polymarket.com/resources/error-codes
- Polymarket documentation index: https://docs.polymarket.com/llms.txt
- PAGCOR: https://www.pagcor.ph/
- PAGCOR regulatory information: https://www.pagcor.ph/regulatory/cegs.php
- PAGCOR public warning concerning claimed offshore/internet gaming licences:
  https://www.pagcor.ph/offshore-gaming.php

Before Stage 4, source hashes and interpretations must be refreshed. If official sources conflict,
change, disappear, or leave eligibility or protocol behavior uncertain, the pilot remains killed.
