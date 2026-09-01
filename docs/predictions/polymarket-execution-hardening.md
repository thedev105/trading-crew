# Polymarket execution hardening and recovery

## Current boundary

This increment is capability- and evidence-gated. Polymarket execution starts killed on every
launch and cannot become eligible merely because a process has started. The production verifier has
no configured key, there is no production capability issuer or kill-clear callable, and the CLI
exposes no credential, order, activation, or clearance flag. The public Polymarket collector remains
separate from the authenticated execution package.

This is not a live pilot or an activation procedure. It neither authorizes authenticated venue
access nor exposes an operator surface or authorized production path to place, cancel, sign,
activate, fund, or transfer anything.

## Operator preflight boundary

The following sequence records the gates a separate, explicitly approved operator pilot would have
to satisfy; it does not create an authority path in this package. Keep the account killed until all
of the following are current and successful: persisted 45-day qualification, 30-day queue-aware
shadow execution, account eligibility, KYC, jurisdiction/geoblock review, protocol review, manual
funding and allowance, exact reconciliation, passkey activation, and explicit operator action.

The required order is: verify that evidence; unlock Keychain entries locally without ever pasting
or printing keys/credentials in a terminal or UI; launch killed; inspect clean exact
reconciliation; register and verify the passkey; obtain a separately approved external activation
decision only if every gate passes; manually authorize one bounded complete strategy in that
separate pilot; inspect reconciliation; and stop/kill on uncertainty. The 45-day qualification,
30-day shadow evidence, independent legal/KYC/venue-terms review, manual funding/allowance, and
activation decision remain external prerequisites; completing this sequence in this repository does
not authorize activation or trading. Automation is unavailable and must not be enabled, simulated
with a script, or substituted for the manual-first strategy. A stale, missing, ambiguous, or
contradictory item leaves the kill engaged.

## Secret isolation

The signer boundary is designed for a separately supervised local process. A wallet private key
and the CLOB API key, secret, and passphrase may enter that process only through an inherited file
descriptor at startup. They do not enter command-line arguments, environment variables, config
files, DuckDB, logs, reports, dashboard payloads, or IPC request bodies. Startup reads the bounded
descriptors once, closes them, overwrites mutable buffers where the runtime permits, and retains
secret material only in signer memory. There is no credential import or credential-creation
command.

Signer and transport failures cross the boundary as stable codes and evidence hashes. They must
not reflect authentication headers, signed bodies, arbitrary venue text, authenticated
subscription frames, descriptor values, or secret-bearing exceptions. The regression suite uses
distinct runtime-only canaries for every secret class; the values are generated during the test,
never persisted, and never printed on a failed absence check.

## Immediate-order policy

Only fill-and-kill (`FAK`) and fill-or-kill (`FOK`) order semantics are modeled. `GTC`, `GTD`,
passive quoting, and maker behavior are outside this increment. An unexpected resting order or an
acknowledgement that reports a live order is a fault: record the sanitized evidence, engage the
kill switch, stop admitting new intents, and begin authoritative recovery. It is never converted
into an accepted resting-order strategy.

One intent binds one deterministic canonical envelope and order fingerprint. A timeout or lost
acknowledgement never causes a blind order POST retry. Recovery cannot enlarge size, loosen price,
change order type, or invent a hedge.

## Recovery runbook

All recovery begins fail-closed. Preserve the database and sanitized evidence, reject new plans and
intents for the affected account, and use authoritative order, trade, balance, and allowance reads.
User-stream messages reduce latency but are never venue truth after a gap.

### Unexpected acknowledgement or `UNKNOWN`

Treat a timeout, lost or malformed response, contradictory acknowledgement, or unverifiable venue
state as `UNKNOWN`. Engage the kill switch and do not resubmit. Read authoritative order and trade
state using the persisted intent identity, fingerprint, and known venue identifier, then reconcile
account balances and allowances. Only authoritative proof that the venue did not accept the exact
order can make an identical-envelope retry eligible under a separately approved policy; the
default remains no resubmission. Until classification and reconciliation are complete, the account
remains killed and P&L is unavailable.

### Cancellation ambiguity

Cancellation is limited to a known venue order ID already bound to the approved account and intent,
and it still requires independent authority at both boundaries. An HTTP success is not final.
Confirm the terminal order state through an authoritative read. A timeout, lost response,
contradiction, or missed heartbeat creates cancellation ambiguity: assume neither that cancellation
succeeded nor that the order is still resting, block new work, read order/trade/account state, and
leave the kill switch engaged. Production currently has no capability with which to attempt a
cancel.

### Reconnect and event gaps

On a user-stream disconnect, parse gap, missed ping/pong, stale sequence, or reconnect, engage the
kill switch. Before any theoretical resume, fetch authoritative open orders, recent trades,
balances, and allowances and compare them with the immutable event history. Treat REST as authority;
never fill gaps from WebSocket assumptions. Repeated disconnect or protocol failure remains a
recovery condition, not a reason to relax the check.

### Heartbeat failure

A missed, rejected, or ambiguous order heartbeat is a cancellation-uncertainty event. Stop new
intents, engage the kill switch, and perform the same authoritative order/trade/account reads. Do
not infer either cancellation or continued resting state from the heartbeat failure alone.

### Settlement and ledger recovery

A matched trade remains unsettled until the venue state is `CONFIRMED`; `RETRYING` is nonterminal.
`FAILED`, contradictory history, missing balance evidence, fee disagreement, or settlement
divergence requires exact reconciliation and keeps the account killed. Rebuild postings only from
validated venue order/trade history and authoritative economics. Do not publish paper or realized
P&L until order, trade, settlement, balance, allowance, and double-entry ledger state agree at one
cutoff.

### Process restart

On restart, keep admission closed. Scan the immutable store for submitting or `UNKNOWN` intents,
delayed or unexpected-live acknowledgements, pending cancellation, nonterminal or failed trade
histories, incomplete checkpoints, and unreconciled ledger entries. Perform read-only recovery at
one bounded account-wide cutoff before considering any state resolved. Credentials are not loaded
from serialized state; a future separately authorized signer launch would require fresh inherited
descriptors. A restart does not clear a kill or restore authority.

## Source-hash review

Protocol fixtures bind exact official-source normalized hashes, route-set hashes, package fixture
hashes, and an implementation revision. Any missing, stale, or changed source hash makes
conformance non-current and engages the kill switch. Review the changed official material outside
the execution path, update the frozen snapshot and derived fixtures in a reviewed release, rerun
offline conformance, and independently review the resulting hashes. Never edit a stored hash to
make a mismatch disappear, and never treat a passing fixture from an older snapshot as current.

Run the offline check against a local fixture bundle and local database:

```bash
.venv/bin/polytrading predictions execution conformance polymarket \
  --db var/prediction-markets.duckdb \
  --format json
```

The command has no credential flags and no network transport. Its result includes
`"network_used": false` plus the reviewed source and fixture hashes.

## Kill-switch policy

Production starts killed. Triggers include invalid or stale manifest, capability, eligibility,
geoblock, protocol, or source hashes; stale books, fees, balances, allowances, or clock evidence;
signer failure or account mismatch; unexpected resting state, `UNKNOWN`, collision, REST/stream
contradiction, reconnect gap, heartbeat failure, unsafe rate limiting, settlement or ledger
divergence, risk breach, nonce/signature failure, and any secret-output detection.

While killed, new plans and intents remain unavailable and read-only reconciliation remains
available. There is no production clearance mechanism. Test fixtures can construct a cleared state
only inside tests; no stored event, restart, dashboard interaction, or conformance result clears
production kill state. A future clearance ceremony, if ever approved, belongs to a separate
activation design.

## Market Atlas observer semantics

Market Atlas is a loopback-only, read-only observer. The server exposes `GET`/`HEAD` snapshots and
assets; the SSE route is `GET` only. The browser uses exactly the same-origin snapshot route and SSE
route and has no Polymarket, signer, execution IPC, credential, or command connection.

Every dashboard snapshot is an immutable, validated view captured at one `as_of` cutoff. Revision
SSE data contains only `schema_version`, `revision_id`, `as_of`, `emitted_at`, and
`changed_domains`. Reset SSE data instead contains only `schema_version`, `latest_revision_id`,
`emitted_at`, and the stable `CURSOR_NOT_AVAILABLE` reason. Both frame types carry the event ID in
the SSE `id` line, and neither carries snapshot totals. On a notification, the browser fetches a
complete snapshot. Cursor loss causes a reset; an SSE failure activates bounded snapshot polling;
staleness is explicit; and the last verified snapshot is retained during disconnection. If
validation becomes inconsistent, Market Atlas hides financial totals and P&L rather than combining
cutoffs or rendering partial state. The UI contains navigation buttons only, with no order, cancel,
activation, or kill-clear control.

Start the observer on loopback with the existing command:

```bash
.venv/bin/polytrading predictions dashboard \
  --db var/prediction-markets.duckdb \
  --port 8787
```

## Remaining gates are not satisfied by code completion

Any later proposal must first complete at least **45 continuous calendar days** of synchronized
rules and executable books, with a separate report for each strategy-and-venue combination. Each
report must pass every fixed Class G threshold without post-hoc relaxation:

- at least 25 opportunities survive current fees, executable depth, and one-second latency;
- at least 10 opportunities survive five-second latency;
- median conservative net surplus is at least 0.75%;
- median capacity is at least USD 100;
- projected annual contribution is at least 2% of total equity;
- conservative return on assigned capital exceeds the approved cash benchmark by 5 percentage
  points;
- manual review finds zero false guaranteed-payoff claims;
- simulated 99th-percentile incomplete-leg loss is below 0.25% of equity; and
- simulated drawdown is below 8%.

After those gates, the project still requires **30 additional shadow calendar days** of queue-aware
shadow execution with positive net results excluding rewards, no risk breach, and complete
reconciliation. Replays, dense fixtures, or many observations on one date cannot compress either
calendar gate.

Passing that evidence would still not authorize a live call. A separately requested and approved
design must complete current legal, jurisdiction, KYC, tax, venue-terms, and account eligibility
review; custody, wallet funding, allowance, incident-response, and credential provisioning/rotation
design; a production capability issuer, signature scheme, issuer-key custody, revocation, duration,
and clearance ceremony; production monitoring and ownership; maximum-capital and loss limits; a
pilot review (no more than the separately approved USD 250 ceiling); and explicit user approval.

The system does not attempt geographic circumvention, and geoblock evidence is not legal advice.
This documentation makes no claim of eligibility or profitability: it does not claim that any user
or account is eligible, that a future pilot will be approved, or that the strategy is or will be
profitable.
