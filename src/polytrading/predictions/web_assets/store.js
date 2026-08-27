export const CONNECTED = "CONNECTED";
export const DEGRADED = "DEGRADED";
export const STALE = "STALE";
export const DISCONNECTED = "DISCONNECTED";
export const INCONSISTENT = "INCONSISTENT";

const CONNECTION_STATES = new Set([CONNECTED, DEGRADED, STALE, DISCONNECTED, INCONSISTENT]);
const SHA256_PATTERN = /^[0-9a-f]{64}$/;
const UTC_PATTERN = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$/;

function invalidSnapshot(detail) {
  const error = new Error(`INVALID_SNAPSHOT:${detail}`);
  error.code = "INVALID_SNAPSHOT";
  return error;
}

function isRecord(value) {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function requireRecord(value, name) {
  if (!isRecord(value)) {
    throw invalidSnapshot(name);
  }
  return value;
}

function requireArray(value, name) {
  if (!Array.isArray(value)) {
    throw invalidSnapshot(name);
  }
  return value;
}

function requireSchemaOne(value, name) {
  if (value !== 1) {
    throw invalidSnapshot(`${name}.schema_version`);
  }
}

function requireUtc(value, name) {
  if (typeof value !== "string" || !UTC_PATTERN.test(value) || !Number.isFinite(Date.parse(value))) {
    throw invalidSnapshot(name);
  }
  return value;
}

function requireCutoff(record, cutoff, name) {
  requireSchemaOne(record.schema_version, name);
  if (requireUtc(record.as_of, `${name}.as_of`) !== cutoff) {
    throw invalidSnapshot(`${name}.as_of`);
  }
}

function requireBoolean(value, name) {
  if (typeof value !== "boolean") {
    throw invalidSnapshot(name);
  }
}

function requireNonNegativeInteger(value, name) {
  if (!Number.isInteger(value) || value < 0) {
    throw invalidSnapshot(name);
  }
}

function cloneJsonValue(value, path = "snapshot") {
  if (value === null || typeof value === "string" || typeof value === "boolean") {
    return value;
  }
  if (typeof value === "number") {
    if (!Number.isFinite(value)) {
      throw invalidSnapshot(path);
    }
    return value;
  }
  if (Array.isArray(value)) {
    return value.map((item, index) => cloneJsonValue(item, `${path}[${index}]`));
  }
  if (isRecord(value)) {
    const prototype = Object.getPrototypeOf(value);
    if (prototype !== Object.prototype && prototype !== null) {
      throw invalidSnapshot(path);
    }
    const clone = {};
    for (const [key, item] of Object.entries(value)) {
      clone[key] = cloneJsonValue(item, `${path}.${key}`);
    }
    return clone;
  }
  throw invalidSnapshot(path);
}

function deepFreeze(value) {
  if (value && typeof value === "object" && !Object.isFrozen(value)) {
    for (const child of Object.values(value)) {
      deepFreeze(child);
    }
    Object.freeze(value);
  }
  return value;
}

function validateLegacySections(snapshot) {
  const health = requireRecord(snapshot.health, "health");
  requireSchemaOne(health.schema_version, "health");
  requireArray(health.venues, "health.venues");
  requireArray(health.warnings, "health.warnings");
  if (health.as_of !== undefined && requireUtc(health.as_of, "health.as_of") !== snapshot.as_of) {
    throw invalidSnapshot("health.as_of");
  }
  requireArray(snapshot.markets, "markets");
  requireArray(snapshot.books, "books");

  const evidenceCounts = requireRecord(snapshot.evidence_counts, "evidence_counts");
  requireSchemaOne(evidenceCounts.schema_version, "evidence_counts");
  requireRecord(evidenceCounts.counts, "evidence_counts.counts");

  const recipes = requireRecord(snapshot.recipes, "recipes");
  requireSchemaOne(recipes.schema_version, "recipes");
  requireArray(recipes.recipes, "recipes.recipes");

  for (const name of ["candidates", "proofs", "scans"]) {
    const summary = requireRecord(snapshot[name], name);
    requireSchemaOne(summary.schema_version, name);
    requireNonNegativeInteger(summary.total, `${name}.total`);
    requireArray(summary.latest, `${name}.latest`);
  }

  const shadow = requireRecord(snapshot.shadow, "shadow");
  requireSchemaOne(shadow.schema_version, "shadow");
  requireNonNegativeInteger(shadow.proposals_total, "shadow.proposals_total");
  requireNonNegativeInteger(shadow.reconciled_count, "shadow.reconciled_count");
  requireNonNegativeInteger(shadow.unreconciled_count, "shadow.unreconciled_count");
  requireArray(shadow.latest, "shadow.latest");
}

function validateReadiness(value, cutoff) {
  const readiness = requireRecord(value, "execution_readiness");
  requireCutoff(readiness, cutoff, "execution_readiness");
  if (
    readiness.implementation_state !== "LIVE_DISABLED" ||
    readiness.kill_engaged !== true ||
    readiness.production_capability_available !== false ||
    readiness.live_action_available !== false
  ) {
    throw invalidSnapshot("execution_readiness.posture");
  }
  requireArray(readiness.unmet_gates, "execution_readiness.unmet_gates");
}

function validateOpportunities(value, cutoff) {
  for (const [index, item] of requireArray(value, "opportunities").entries()) {
    const opportunity = requireRecord(item, `opportunities[${index}]`);
    requireCutoff(opportunity, cutoff, `opportunities[${index}]`);
    requireBoolean(opportunity.reconciled, `opportunities[${index}].reconciled`);
    requireArray(opportunity.evidence_hashes, `opportunities[${index}].evidence_hashes`);
  }
}

function validateTimeline(value, cutoff) {
  const cutoffMilliseconds = Date.parse(cutoff);
  for (const [index, item] of requireArray(value, "execution_timeline").entries()) {
    const entry = requireRecord(item, `execution_timeline[${index}]`);
    requireCutoff(entry, cutoff, `execution_timeline[${index}]`);
    const occurredAt = requireUtc(entry.occurred_at, `execution_timeline[${index}].occurred_at`);
    if (Date.parse(occurredAt) > cutoffMilliseconds) {
      throw invalidSnapshot(`execution_timeline[${index}].occurred_at`);
    }
    requireBoolean(entry.reconciled, `execution_timeline[${index}].reconciled`);
    requireArray(entry.evidence_hashes, `execution_timeline[${index}].evidence_hashes`);
  }
}

function validateLedger(value, cutoff) {
  const ledger = requireRecord(value, "live_ledger");
  requireCutoff(ledger, cutoff, "live_ledger");
  for (const field of [
    "posting_count",
    "reconciliation_count",
    "complete_reconciliation_count",
    "incomplete_reconciliation_count",
  ]) {
    requireNonNegativeInteger(ledger[field], `live_ledger.${field}`);
  }
  if (
    ledger.complete_reconciliation_count + ledger.incomplete_reconciliation_count !==
    ledger.reconciliation_count
  ) {
    throw invalidSnapshot("live_ledger.reconciliation_count");
  }
  requireBoolean(ledger.pnl_publishable, "live_ledger.pnl_publishable");
  if (ledger.pnl_publishable !== (ledger.realized_pnl_usd !== null)) {
    throw invalidSnapshot("live_ledger.realized_pnl_usd");
  }
}

function validateEvidence(value, cutoff) {
  const evidence = requireRecord(value, "evidence_status");
  requireCutoff(evidence, cutoff, "evidence_status");
  requireNonNegativeInteger(evidence.account_count, "evidence_status.account_count");
  requireArray(evidence.source_hashes, "evidence_status.source_hashes");
  requireArray(evidence.unmet_activation_gates, "evidence_status.unmet_activation_gates");
}

export function validateSnapshotCutoff(snapshot) {
  const root = requireRecord(snapshot, "snapshot");
  requireSchemaOne(root.schema_version, "snapshot");
  if (typeof root.revision_id !== "string" || !SHA256_PATTERN.test(root.revision_id)) {
    throw invalidSnapshot("snapshot.revision_id");
  }
  const cutoff = requireUtc(root.as_of, "snapshot.as_of");
  validateLegacySections(root);
  validateReadiness(root.execution_readiness, cutoff);
  validateOpportunities(root.opportunities, cutoff);
  validateTimeline(root.execution_timeline, cutoff);
  validateLedger(root.live_ledger, cutoff);
  validateEvidence(root.evidence_status, cutoff);
  return cutoff;
}

function redactedSnapshot(snapshot) {
  if (snapshot === null) {
    return null;
  }
  return deepFreeze({
    ...snapshot,
    opportunities: snapshot.opportunities.map((item) => ({
      ...item,
      conservative_surplus_usd: null,
      capacity_usd: null,
    })),
    live_ledger: {
      ...snapshot.live_ledger,
      pnl_publishable: false,
      realized_pnl_usd: null,
    },
    shadow: {
      ...snapshot.shadow,
      reconciled_paper_pnl_usd: null,
    },
  });
}

export function snapshotIsStale(snapshot, { now = Date.now, maxAgeMs = 60_000 } = {}) {
  if (snapshot === null) {
    return false;
  }
  const snapshotAge = now() - Date.parse(snapshot.as_of);
  if (!Number.isFinite(snapshotAge) || snapshotAge > maxAgeMs) {
    return true;
  }
  return snapshot.health.venues.some((venue) => {
    const status = typeof venue.status === "string" ? venue.status.toUpperCase() : "";
    return status.includes("STALE") || status.includes("DEGRADED");
  });
}

export function createSnapshotStore({
  scheduleNotification = queueMicrotask,
  now = Date.now,
} = {}) {
  if (typeof scheduleNotification !== "function" || typeof now !== "function") {
    throw new TypeError("store dependencies must be functions");
  }
  let snapshot = null;
  let connectionState = DISCONNECTED;
  let errorCode = null;
  let lastRefreshAt = null;
  let notificationScheduled = false;
  const subscribers = new Set();

  function getState() {
    const financialsHidden = connectionState === INCONSISTENT;
    return Object.freeze({
      snapshot,
      displaySnapshot: financialsHidden ? redactedSnapshot(snapshot) : snapshot,
      connectionState,
      errorCode,
      lastRefreshAt,
      financialsHidden,
    });
  }

  function notify() {
    if (notificationScheduled) {
      return;
    }
    notificationScheduled = true;
    scheduleNotification(() => {
      notificationScheduled = false;
      const state = getState();
      for (const subscriber of [...subscribers]) {
        subscriber(state);
      }
    });
  }

  function setConnectionState(nextState, nextErrorCode = null) {
    if (!CONNECTION_STATES.has(nextState)) {
      throw new TypeError("unknown connection state");
    }
    if (connectionState === nextState && errorCode === nextErrorCode) {
      return;
    }
    connectionState = nextState;
    errorCode = nextErrorCode;
    notify();
  }

  function replace(candidate) {
    try {
      const detached = cloneJsonValue(candidate);
      validateSnapshotCutoff(detached);
      const frozen = deepFreeze(detached);
      snapshot = frozen;
      errorCode = null;
      lastRefreshAt = new Date(now()).toISOString();
      notify();
      return frozen;
    } catch (error) {
      setConnectionState(INCONSISTENT, "INVALID_SNAPSHOT");
      throw error;
    }
  }

  return Object.freeze({
    getState,
    replaceSnapshot: replace,
    setConnectionState,
    subscribe(subscriber) {
      if (typeof subscriber !== "function") {
        throw new TypeError("subscriber must be a function");
      }
      subscribers.add(subscriber);
      return () => subscribers.delete(subscriber);
    },
  });
}

export function replaceSnapshot(store, candidate) {
  if (!store || typeof store.replaceSnapshot !== "function") {
    throw new TypeError("snapshot store is required");
  }
  return store.replaceSnapshot(candidate);
}
