export const CONNECTED = "CONNECTED";
export const DEGRADED = "DEGRADED";
export const STALE = "STALE";
export const DISCONNECTED = "DISCONNECTED";
export const INCONSISTENT = "INCONSISTENT";

const CONNECTION_STATES = new Set([CONNECTED, DEGRADED, STALE, DISCONNECTED, INCONSISTENT]);
const SHA256_PATTERN = /^[0-9a-f]{64}$/;
const UTC_PATTERN = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$/;
const PROTOTYPE_META_KEYS = new Set(["__proto__", "constructor", "prototype"]);

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

function requireOwn(record, field, name) {
  if (!Object.hasOwn(record, field)) {
    throw invalidSnapshot(name);
  }
  return record[field];
}

function requireSchemaOne(record, name) {
  const value = requireOwn(record, "schema_version", `${name}.schema_version`);
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
  requireSchemaOne(record, name);
  if (requireUtc(requireOwn(record, "as_of", `${name}.as_of`), `${name}.as_of`) !== cutoff) {
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
    const clone = Object.create(null);
    for (const key of Reflect.ownKeys(value)) {
      if (typeof key !== "string" || PROTOTYPE_META_KEYS.has(key)) {
        throw invalidSnapshot(path);
      }
      const descriptor = Object.getOwnPropertyDescriptor(value, key);
      if (!descriptor?.enumerable || !("value" in descriptor)) {
        throw invalidSnapshot(`${path}.${key}`);
      }
      Object.defineProperty(clone, key, {
        value: cloneJsonValue(descriptor.value, `${path}.${key}`),
        enumerable: true,
        writable: true,
        configurable: true,
      });
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
  const health = requireRecord(requireOwn(snapshot, "health", "health"), "health");
  requireSchemaOne(health, "health");
  requireArray(requireOwn(health, "venues", "health.venues"), "health.venues");
  requireArray(requireOwn(health, "warnings", "health.warnings"), "health.warnings");
  if (
    Object.hasOwn(health, "as_of") &&
    requireUtc(health.as_of, "health.as_of") !== snapshot.as_of
  ) {
    throw invalidSnapshot("health.as_of");
  }
  requireArray(requireOwn(snapshot, "markets", "markets"), "markets");
  requireArray(requireOwn(snapshot, "books", "books"), "books");

  const evidenceCounts = requireRecord(
    requireOwn(snapshot, "evidence_counts", "evidence_counts"),
    "evidence_counts",
  );
  requireSchemaOne(evidenceCounts, "evidence_counts");
  requireRecord(
    requireOwn(evidenceCounts, "counts", "evidence_counts.counts"),
    "evidence_counts.counts",
  );

  const recipes = requireRecord(requireOwn(snapshot, "recipes", "recipes"), "recipes");
  requireSchemaOne(recipes, "recipes");
  requireArray(requireOwn(recipes, "recipes", "recipes.recipes"), "recipes.recipes");

  for (const name of ["candidates", "proofs", "scans"]) {
    const summary = requireRecord(requireOwn(snapshot, name, name), name);
    requireSchemaOne(summary, name);
    requireNonNegativeInteger(
      requireOwn(summary, "total", `${name}.total`),
      `${name}.total`,
    );
    requireArray(requireOwn(summary, "latest", `${name}.latest`), `${name}.latest`);
  }

  const shadow = requireRecord(requireOwn(snapshot, "shadow", "shadow"), "shadow");
  requireSchemaOne(shadow, "shadow");
  requireNonNegativeInteger(
    requireOwn(shadow, "proposals_total", "shadow.proposals_total"),
    "shadow.proposals_total",
  );
  requireNonNegativeInteger(
    requireOwn(shadow, "reconciled_count", "shadow.reconciled_count"),
    "shadow.reconciled_count",
  );
  requireNonNegativeInteger(
    requireOwn(shadow, "unreconciled_count", "shadow.unreconciled_count"),
    "shadow.unreconciled_count",
  );
  requireArray(requireOwn(shadow, "latest", "shadow.latest"), "shadow.latest");
}

function validateReadiness(value, cutoff) {
  const readiness = requireRecord(value, "execution_readiness");
  requireCutoff(readiness, cutoff, "execution_readiness");
  if (
    requireOwn(readiness, "implementation_state", "execution_readiness.implementation_state") !==
      "LIVE_DISABLED" ||
    requireOwn(readiness, "kill_engaged", "execution_readiness.kill_engaged") !== true ||
    requireOwn(
      readiness,
      "production_capability_available",
      "execution_readiness.production_capability_available",
    ) !== false ||
    requireOwn(readiness, "live_action_available", "execution_readiness.live_action_available") !==
      false
  ) {
    throw invalidSnapshot("execution_readiness.posture");
  }
  requireArray(
    requireOwn(readiness, "unmet_gates", "execution_readiness.unmet_gates"),
    "execution_readiness.unmet_gates",
  );
}

function validateOpportunities(value, cutoff) {
  for (const [index, item] of requireArray(value, "opportunities").entries()) {
    const opportunity = requireRecord(item, `opportunities[${index}]`);
    requireCutoff(opportunity, cutoff, `opportunities[${index}]`);
    requireBoolean(
      requireOwn(opportunity, "reconciled", `opportunities[${index}].reconciled`),
      `opportunities[${index}].reconciled`,
    );
    requireArray(
      requireOwn(opportunity, "evidence_hashes", `opportunities[${index}].evidence_hashes`),
      `opportunities[${index}].evidence_hashes`,
    );
  }
}

function validateTimeline(value, cutoff) {
  const cutoffMilliseconds = Date.parse(cutoff);
  for (const [index, item] of requireArray(value, "execution_timeline").entries()) {
    const entry = requireRecord(item, `execution_timeline[${index}]`);
    requireCutoff(entry, cutoff, `execution_timeline[${index}]`);
    const occurredAt = requireUtc(
      requireOwn(entry, "occurred_at", `execution_timeline[${index}].occurred_at`),
      `execution_timeline[${index}].occurred_at`,
    );
    if (Date.parse(occurredAt) > cutoffMilliseconds) {
      throw invalidSnapshot(`execution_timeline[${index}].occurred_at`);
    }
    requireBoolean(
      requireOwn(entry, "reconciled", `execution_timeline[${index}].reconciled`),
      `execution_timeline[${index}].reconciled`,
    );
    requireArray(
      requireOwn(entry, "evidence_hashes", `execution_timeline[${index}].evidence_hashes`),
      `execution_timeline[${index}].evidence_hashes`,
    );
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
    requireNonNegativeInteger(
      requireOwn(ledger, field, `live_ledger.${field}`),
      `live_ledger.${field}`,
    );
  }
  if (
    ledger.complete_reconciliation_count + ledger.incomplete_reconciliation_count !==
    ledger.reconciliation_count
  ) {
    throw invalidSnapshot("live_ledger.reconciliation_count");
  }
  const pnlPublishable = requireOwn(
    ledger,
    "pnl_publishable",
    "live_ledger.pnl_publishable",
  );
  requireBoolean(pnlPublishable, "live_ledger.pnl_publishable");
  const realizedPnl = requireOwn(
    ledger,
    "realized_pnl_usd",
    "live_ledger.realized_pnl_usd",
  );
  if (pnlPublishable !== (realizedPnl !== null)) {
    throw invalidSnapshot("live_ledger.realized_pnl_usd");
  }
}

function validateEvidence(value, cutoff) {
  const evidence = requireRecord(value, "evidence_status");
  requireCutoff(evidence, cutoff, "evidence_status");
  requireNonNegativeInteger(
    requireOwn(evidence, "account_count", "evidence_status.account_count"),
    "evidence_status.account_count",
  );
  requireArray(
    requireOwn(evidence, "source_hashes", "evidence_status.source_hashes"),
    "evidence_status.source_hashes",
  );
  requireArray(
    requireOwn(
      evidence,
      "unmet_activation_gates",
      "evidence_status.unmet_activation_gates",
    ),
    "evidence_status.unmet_activation_gates",
  );
}

export function validateSnapshotCutoff(snapshot) {
  const root = requireRecord(snapshot, "snapshot");
  requireSchemaOne(root, "snapshot");
  const revisionId = requireOwn(root, "revision_id", "snapshot.revision_id");
  if (typeof revisionId !== "string" || !SHA256_PATTERN.test(revisionId)) {
    throw invalidSnapshot("snapshot.revision_id");
  }
  const cutoff = requireUtc(requireOwn(root, "as_of", "snapshot.as_of"), "snapshot.as_of");
  validateLegacySections(root);
  validateReadiness(
    requireOwn(root, "execution_readiness", "execution_readiness"),
    cutoff,
  );
  validateOpportunities(requireOwn(root, "opportunities", "opportunities"), cutoff);
  validateTimeline(requireOwn(root, "execution_timeline", "execution_timeline"), cutoff);
  validateLedger(requireOwn(root, "live_ledger", "live_ledger"), cutoff);
  validateEvidence(requireOwn(root, "evidence_status", "evidence_status"), cutoff);
  return cutoff;
}

function redactedSnapshot(snapshot) {
  if (snapshot === null) {
    return null;
  }
  function redact(value) {
    if (Array.isArray(value)) {
      return value.map(redact);
    }
    if (!isRecord(value)) {
      return value;
    }
    const projected = Object.create(null);
    for (const [key, item] of Object.entries(value)) {
      let projectedValue;
      if (key === "pnl_publishable") {
        projectedValue = false;
      } else if (
        key === "conservative_surplus_usd" ||
        key === "capacity_usd" ||
        key.toLowerCase().includes("pnl")
      ) {
        projectedValue = null;
      } else {
        projectedValue = redact(item);
      }
      Object.defineProperty(projected, key, {
        value: projectedValue,
        enumerable: true,
        writable: true,
        configurable: true,
      });
    }
    return projected;
  }
  return deepFreeze(redact(snapshot));
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
  let state = {
    snapshot: null,
    connectionState: DISCONNECTED,
    errorCode: null,
    lastRefreshAt: null,
  };
  let notificationScheduled = false;
  const subscribers = new Set();

  function getState() {
    const financialsHidden = state.connectionState === INCONSISTENT;
    return Object.freeze({
      ...state,
      displaySnapshot: financialsHidden ? redactedSnapshot(state.snapshot) : state.snapshot,
      financialsHidden,
    });
  }

  function dispatchNotification() {
    notificationScheduled = false;
    const current = getState();
    for (const subscriber of [...subscribers]) {
      try {
        subscriber(current);
      } catch (_error) {
        // One observer cannot roll back or block an already committed snapshot.
      }
    }
  }

  function prepareNotification() {
    if (notificationScheduled) {
      return () => undefined;
    }
    let armed = false;
    let fired = false;
    let delivered = false;
    scheduleNotification(() => {
      if (delivered) {
        return;
      }
      if (!armed) {
        fired = true;
        return;
      }
      delivered = true;
      dispatchNotification();
    });
    return () => {
      notificationScheduled = true;
      armed = true;
      if (fired && !delivered) {
        delivered = true;
        dispatchNotification();
      }
    };
  }

  function markInconsistent() {
    state = {
      ...state,
      connectionState: INCONSISTENT,
      errorCode: "INVALID_SNAPSHOT",
    };
    try {
      prepareNotification()();
    } catch (_error) {
      // The state remains fail-closed even when the notification seam is hostile.
    }
  }

  function setConnectionState(nextState, nextErrorCode = null) {
    if (!CONNECTION_STATES.has(nextState)) {
      throw new TypeError("unknown connection state");
    }
    if (state.connectionState === nextState && state.errorCode === nextErrorCode) {
      return;
    }
    const armNotification = prepareNotification();
    state = { ...state, connectionState: nextState, errorCode: nextErrorCode };
    armNotification();
  }

  function replace(candidate) {
    try {
      const detached = cloneJsonValue(candidate);
      validateSnapshotCutoff(detached);
      const frozen = deepFreeze(detached);
      const refreshedAt = new Date(now()).toISOString();
      const armNotification = prepareNotification();
      state = { ...state, snapshot: frozen, errorCode: null, lastRefreshAt: refreshedAt };
      armNotification();
      return frozen;
    } catch (error) {
      markInconsistent();
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
