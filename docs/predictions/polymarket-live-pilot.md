# Polymarket local live pilot — operator runbook

This runbook describes the loopback-only pilot that lets one locally present operator authorize
tightly bounded Polymarket execution. Nothing in this document starts trading: every live action
is an operator ceremony in the local UI, and the pilot starts killed on every launch.

**Secrets never belong in the UI, the CLI, a `.env` file, logs, screenshots, tickets, chat, email,
or a support message. Never paste a credential or key into a terminal or UI.** The only place a
wallet key or CLOB credential lives is the macOS Keychain and the signer process that reads it
through inherited descriptors.

## 1. Mandatory preflight and first-strategy order

The pilot remains killed until every one of these gates is current and succeeds: persisted
45-day qualification evidence, 30-day queue-aware shadow-execution evidence, account eligibility,
KYC, jurisdiction/geoblock evidence, protocol review, manual funding and venue allowance, exact
reconciliation, passkey activation, and an explicit operator action. No launch, manifest, or
passkey alone creates an execution capability.

Perform this order exactly:

1. Verify the persisted qualification and shadow evidence, then verify current eligibility, KYC,
   geoblock, and protocol-review evidence.
2. Unlock the required Keychain entries locally. Do not reveal, copy, paste, or type their values
   into a terminal or the console.
3. Launch the pilot in its killed posture and inspect the clean, exact startup reconciliation.
4. Register and verify the platform passkey.
5. Activate only if every readiness gate remains green. If any evidence is absent, stale, or
   contradictory, stop and leave the kill engaged.
6. Manually authorize one bounded, complete strategy in the local UI. Inspect its authoritative
   reconciliation before considering any further action.
7. Stop and engage/keep the kill on any uncertainty, including an ambiguous acknowledgement,
   signer/transport problem, missing presence, or reconciliation difference.

Automation is unavailable in this build. Do not attempt an automation-session ceremony, API,
script, or workaround; it is deliberately rejected. The manual-first complete-strategy path is the
only possible operating sequence once all gates have succeeded.

## 2. Local setup

1. Create and back up the dedicated wallet **outside this application**. It holds only pilot funds.
2. Enrol the wallet key in the macOS Keychain with the native Keychain interface, under service
   `polytrading.polymarket.pilot` and account `wallet-private-key`. Store the wallet private key as
   exactly 64 hexadecimal characters (an optional `0x` prefix is accepted); the signer decodes it
   to its 32-byte key form. Never use the Pilot UI or CLI to enter a secret, and never use
   `security -w` or a shell helper that prints a secret.
3. Fund the wallet and set the venue allowance manually, outside this application. The pilot never
   deposits, withdraws, transfers, approves, splits, merges, converts, or redeems.
4. Start the console: `predictions pilot polymarket --db <path> --port <port>`. Those two flags are
   the entire CLI surface; there is no credential, order, activation, capability, or kill-clearance
   flag anywhere.
5. If the wallet key is not enrolled or cannot be unlocked, the command writes
   `pilot: signer unavailable (<CODE>); serving posture only` to stderr and serves only the
   read-only killed posture console. It never accepts a secret from the CLI or browser.
6. If the wallet key is available, the signer identifies the wallet and the operator ceremonies
   become reachable, still killed. CLOB API credentials are optional for this launch, but the
   credential ceremony and every execution path currently refuse `EXECUTION_UNAVAILABLE`: this
   build constructs no venue transport. The browser, coordinator, database, and logs only ever see
   public fingerprints.

## 3. What the console shows

![Readiness view at 1440px: kill state, presence, manifest, protocol, secret store, live authority, and an empty blocker list](assets/polymarket-live-pilot/readiness-1440.png)

**Readiness** — kill state, presence, manifest posture, protocol checkpoint, secret-store health,
qualified proof families, evidence age and hashes, and a stable blocker code for anything missing.

**Limits** — the compiled immutable ceilings beside the operator's requested values. A request may
only lower a ceiling; an attempted increase is rejected, never clamped.

| Control | Immutable ceiling |
| --- | ---: |
| Dedicated wallet trading equity | USD 250 |
| One order notional | USD 10 |
| One complete strategy gross notional | USD 25 |
| Automation session | Unavailable; deliberately disabled |
| Concurrent active strategies | 1 |
| Session realized plus unrealized loss | USD 5 |
| UTC-day realized plus unrealized loss | USD 10 |

![Opportunity approval at 900px: ranked strategy cards with proof, economics, incomplete exposure, recovery branches, tie-break field, and the typed confirmation ceremony](assets/polymarket-live-pilot/approval-900.png)

**Approval** — each eligible strategy with its proof, legs, FAK/FOK types, current and
five-second-stressed surplus, executable capacity, modeled incomplete-leg exposure, recovery
branches, evidence age, rank, and the first ranking field that broke the tie. Cross-venue
opportunities remain visible and disabled.

![Live session at 1440px: mode, authority expiry, budgets, filled legs, and the presence heartbeat](assets/polymarket-live-pilot/live-1440.png)

**Live session** — mode, authority expiry, presence heartbeat, strategies started, deployed
capital, and loss budgets. Financial totals appear only when reconciliation is exact; otherwise the
console says UNKNOWN rather than estimating.

![Recovery at 900px: an UNKNOWN second leg, the frozen recovery unwind, and every financial total reported as UNKNOWN until reconciliation is exact](assets/polymarket-live-pilot/recovery-900.png)

![Killed state at 390px: the mobile layout keeps the stop control and every blocker code visible](assets/polymarket-live-pilot/killed-390.png)

Screenshots come from the fake-data pilot server: no database, signer, keychain, or authenticated
transport is involved in producing them.

## 4. Concepts worth reading once

- **Proof families.** Only deterministic, exhaustive proofs are live-eligible: binary complement,
  exhaustive outcome coverage, implication, and within-Polymarket equivalence. AI may nominate a
  relationship for deterministic evaluation; it can never prove, rank, size, approve, or execute.
- **FAK and FOK only.** Post-only, GTC, GTD, passive quoting, and resting strategies are refused at
  the UI, plan, coordinator, signer, and route boundaries.
- **UNKNOWN.** A timeout or ambiguous acknowledgement is UNKNOWN. The order POST is never retried.
  Normal execution stops and only the frozen risk-reducing recovery branch remains available.
- **Reconciliation.** P&L stays unavailable until venue, settlement, balance, allowance, position,
  and ledger agree exactly.

## 5. Available authorization mode

**Exact order** authorizes one precomputed FAK/FOK order that reduces a known existing position. It
cannot open naked or directional exposure. The capability dies at the reconciled terminal result or
after 60 seconds, whichever comes first.

**Complete strategy** authorizes one precomputed multi-leg strategy and its bounded recovery tree.
The plan, leg order, sizes, prices, deadlines, and stop conditions are frozen before the passkey
ceremony. The capability dies at reconciliation or after five minutes.

**Automation session is unavailable.** It is compiled disabled and its requests are rejected. No
operator may substitute repeated approvals, a script, or another UI path for the manual-first
complete-strategy ceremony.

Every approval is: read the summary, type the exact confirmation text
(`ORDER <amount> USD` or `STRATEGY <amount> USD`), then satisfy the platform passkey. The console
never marks success optimistically; it re-reads a coherent snapshot.

## 6. Presence

The page sends a heartbeat every two seconds. Two missed heartbeats, five seconds of silence, a
screen lock, or a sleep-sized jump in the monotonic clock engages the kill state immediately. The
control page must stay open, the machine awake, and the operator present.

## 7. Checklists

**Before authorizing**
- Verify the persisted 45-day qualification and 30-day shadow-execution evidence before opening
  the Keychain or console.
- Confirm current eligibility, KYC, jurisdiction/geoblock, and protocol-review evidence; readiness
  shows no blockers and evidence age is seconds, not minutes.
- Startup reconciliation is exact. Funding and allowance were performed manually outside the app.
- The passkey has been registered and verified after the clean killed launch.
- The requested limits are the ones you intend, and are at or below every ceiling.
- The summary's legs, prices, sizes, and recovery branches are what you expect.
- You are physically present and the machine will stay awake.
- Choose one bounded complete strategy manually; automation is not an alternative.

**After a run**
- Every leg has an authoritative terminal result; nothing is UNKNOWN.
- Reconciliation is exact and the ledger balances.
- Deployed capital and losses are inside the session and UTC-day budgets.
- If anything is off, stop, leave the kill engaged, and work through section 9.

## 8. Staged activation

- **Stage 0 — offline verification.** Full suite, conformance, and authority scans pass.
- **Stage 1 — shadow qualification.** 45 continuous days of synchronized rules and executable books
  plus 30 additional days of queue-aware shadow execution, recomputed from persisted evidence per
  proof family. Thresholds are never edited to fit results.
- **Stage 2 — operator drills.** Practise stop, kill, recovery, and clearance with no live
  authority.
- **Stage 3 — activation readiness.** Current account eligibility, KYC, geoblock, protocol review,
  manual funding/allowance, exact killed-launch reconciliation, and a verified passkey must all be
  present. A `LIVE_ELIGIBLE` manifest creates no capability and submits nothing.
- **Stage 4 — first live strategy.** Only after every gate remains current, the present operator
  manually authorizes one bounded complete strategy in the UI. Keep the first live size at
  `min(USD 5, the smallest venue-valid complete strategy)`. Automation remains unavailable.

## 9. Recovery playbooks

- **UNKNOWN order outcome.** Do not retry. Let recovery run its frozen branch, then reconcile from
  authoritative order, trade, balance, and allowance reads before anything else.
- **Presence lost mid-strategy.** Authority is already revoked. Recovery has at most 120 seconds;
  after that only read-only reconciliation continues and a new recovery approval is required.
- **Signer or transport failure.** Kill is engaged. Reconcile, then restart the pilot; every launch
  starts killed and every outstanding capability is invalid after a restart.
- **Wallet above USD 250.** New entries are blocked. Reduce the balance outside the application;
  the pilot never moves the excess.
- **Source or protocol change.** Conformance fails first. Review the change, refresh the checkpoint,
  and only then consider a new activation.
- **Clearing the kill.** Requires no in-flight submission, no UNKNOWN outcome, exact reconciliation,
  a reviewed discrepancy record, the exact phrase `CLEAR POLYMARKET PILOT KILL`, and a fresh passkey
  assertion. Clearance creates no trading capability: the next action still needs its own approval.

## 10. What this pilot never does

Funding, withdrawal, transfer, allowance mutation, approval, split, merge, conversion, redemption,
bridging, relaying, and wallet generation are outside the application. So is any cross-venue live
execution, any AI-originated trading decision, any automation-session execution, and any geographic
circumvention.
