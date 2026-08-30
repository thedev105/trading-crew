# Polymarket local live pilot — operator runbook

This runbook describes the loopback-only pilot that lets one locally present operator authorize
tightly bounded Polymarket execution. Nothing in this document starts trading: every live action
is an operator ceremony in the local UI, and the pilot starts killed on every launch.

**Secrets never belong in the UI, the CLI, a `.env` file, logs, screenshots, tickets, chat, email,
or a support message.** The only place a wallet key or CLOB credential lives is the macOS Keychain
and the signer process that reads it through inherited descriptors.

## 1. Ten-minute setup

1. Create and back up the dedicated wallet **outside this application**. It holds only pilot funds.
2. Enrol the wallet key in the macOS Keychain with the native Keychain interface, under service
   `polytrading.polymarket.pilot` and account `wallet-private-key`. Never use the Pilot UI or CLI
   to enter a secret, and never use `security -w` or a shell helper that prints a secret.
3. Fund the wallet and set the venue allowance manually, outside this application. The pilot never
   deposits, withdraws, transfers, approves, splits, merges, converts, or redeems.
4. Start the console: `predictions pilot polymarket --db <path> --port <port>`. Those two flags are
   the entire CLI surface; there is no credential, order, activation, capability, or kill-clearance
   flag anywhere.
5. Open `http://localhost:<port>` in a browser that supports platform passkeys, and register the
   operator passkey. Registration requires the unlocked wallet and an empty credential registry.
6. If the wallet has no CLOB API credentials, run the credential ceremony from the UI. The signer
   performs one allowlisted create-or-derive call under a 60-second single-use grant and writes the
   API key, secret, and passphrase straight into the Keychain. The browser, coordinator, database,
   and logs only ever see fingerprints.

## 2. What the console shows

**Readiness** — kill state, presence, manifest posture, protocol checkpoint, secret-store health,
qualified proof families, evidence age and hashes, and a stable blocker code for anything missing.

**Limits** — the compiled immutable ceilings beside the operator's requested values. A request may
only lower a ceiling; an attempted increase is rejected, never clamped.

| Control | Immutable ceiling |
| --- | ---: |
| Dedicated wallet trading equity | USD 250 |
| One order notional | USD 10 |
| One complete strategy gross notional | USD 25 |
| Automation-session duration | 15 minutes |
| Automation-session deployed capital | USD 50 |
| Concurrent active strategies | 1 |
| Session realized plus unrealized loss | USD 5 |
| UTC-day realized plus unrealized loss | USD 10 |

**Approval** — each eligible strategy with its proof, legs, FAK/FOK types, current and
five-second-stressed surplus, executable capacity, modeled incomplete-leg exposure, recovery
branches, evidence age, rank, and the first ranking field that broke the tie. Cross-venue
opportunities remain visible and disabled.

**Live session** — mode, authority expiry, presence heartbeat, strategies started, deployed
capital, and loss budgets. Financial totals appear only when reconciliation is exact; otherwise the
console says UNKNOWN rather than estimating.

## 3. Concepts worth reading once

- **Proof families.** Only deterministic, exhaustive proofs are live-eligible: binary complement,
  exhaustive outcome coverage, implication, and within-Polymarket equivalence. AI may nominate a
  relationship for deterministic evaluation; it can never prove, rank, size, approve, or execute.
- **FAK and FOK only.** Post-only, GTC, GTD, passive quoting, and resting strategies are refused at
  the UI, plan, coordinator, signer, and route boundaries.
- **UNKNOWN.** A timeout or ambiguous acknowledgement is UNKNOWN. The order POST is never retried.
  Normal execution stops and only the frozen risk-reducing recovery branch remains available.
- **Reconciliation.** P&L stays unavailable until venue, settlement, balance, allowance, position,
  and ledger agree exactly.

## 4. The three modes

**Exact order** authorizes one precomputed FAK/FOK order that reduces a known existing position. It
cannot open naked or directional exposure. The capability dies at the reconciled terminal result or
after 60 seconds, whichever comes first.

**Complete strategy** authorizes one precomputed multi-leg strategy and its bounded recovery tree.
The plan, leg order, sizes, prices, deadlines, and stop conditions are frozen before the passkey
ceremony. The capability dies at reconciliation or after five minutes.

**Automation session** authorizes multiple eligible strategies for 15 minutes with one approval at
the start. Only one strategy runs at a time, no new strategy starts in the final minute, and the
session cannot extend itself, raise a limit, switch wallets, enable another family, or add a venue.

Every approval is: read the summary, type the exact confirmation text
(`ORDER <amount> USD`, `STRATEGY <amount> USD`, or `SESSION 15 MIN <amount> USD`), then satisfy the
platform passkey. The console never marks success optimistically; it re-reads a coherent snapshot.

## 5. Presence

The page sends a heartbeat every two seconds. Two missed heartbeats, five seconds of silence, a
screen lock, or a sleep-sized jump in the monotonic clock engages the kill state immediately. The
control page must stay open, the machine awake, and the operator present.

## 6. Checklists

**Before authorizing**
- Readiness shows no blockers, and evidence age is seconds, not minutes.
- The requested limits are the ones you intend, and are at or below every ceiling.
- The summary's legs, prices, sizes, and recovery branches are what you expect.
- You are physically present and the machine will stay awake.

**After a run**
- Every leg has an authoritative terminal result; nothing is UNKNOWN.
- Reconciliation is exact and the ledger balances.
- Deployed capital and losses are inside the session and UTC-day budgets.
- If anything is off, leave the kill engaged and work through section 8.

## 7. Staged activation

- **Stage 0 — offline verification.** Full suite, conformance, and authority scans pass.
- **Stage 1 — shadow qualification.** 45 continuous days of synchronized rules and executable books
  plus 30 additional days of queue-aware shadow execution, recomputed from persisted evidence per
  proof family. Thresholds are never edited to fit results.
- **Stage 2 — operator drills.** Practise stop, kill, recovery, and clearance with no live
  authority.
- **Stage 3 — activation readiness.** A fresh protocol checkpoint, current attestation and geoblock
  evidence, and a passkey ceremony append a `LIVE_ELIGIBLE` manifest. Promotion creates no
  capability and submits nothing.
- **Stage 4 — first live strategy.** Operator-only, in the UI, after acceptance. Keep the first
  live size at `min(USD 5, the smallest venue-valid complete strategy)`.

## 8. Recovery playbooks

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

## 9. What this pilot never does

Funding, withdrawal, transfer, allowance mutation, approval, split, merge, conversion, redemption,
bridging, relaying, and wallet generation are outside the application. So is any cross-venue live
execution, any AI-originated trading decision, and any geographic circumvention.
