import { fetchDashboardSnapshot } from "./api.js";
import {
  CONNECTED,
  DEGRADED,
  DISCONNECTED,
  INCONSISTENT,
  STALE,
  snapshotIsStale,
} from "./store.js";

const EVENTS_PATH = "/api/v1/predictions-events";
const SHA256_PATTERN = /^[0-9a-f]{64}$/;

export function startBoundedSnapshotPolling({
  refresh,
  signal,
  setTimeoutFn = globalThis.setTimeout,
  clearTimeoutFn = globalThis.clearTimeout,
  intervalMs = 5000,
} = {}) {
  if (
    typeof refresh !== "function" ||
    typeof setTimeoutFn !== "function" ||
    typeof clearTimeoutFn !== "function" ||
    !Number.isFinite(intervalMs) ||
    intervalMs <= 0
  ) {
    throw new TypeError("polling requires bounded function dependencies");
  }
  let timer = null;
  let active = true;

  function schedule() {
    if (!active || signal?.aborted || timer !== null) {
      return;
    }
    timer = setTimeoutFn(async () => {
      timer = null;
      if (!active || signal?.aborted) {
        return;
      }
      await refresh();
      schedule();
    }, intervalMs);
  }

  function stop() {
    active = false;
    if (timer !== null) {
      clearTimeoutFn(timer);
      timer = null;
    }
  }

  schedule();
  return Object.freeze({ stop });
}

function notificationRevision(eventName, event) {
  let document;
  try {
    document = JSON.parse(event.data);
  } catch (_error) {
    return null;
  }
  if (!document || document.schema_version !== 1) {
    return null;
  }
  const revisionId =
    eventName === "revision" ? document.revision_id : document.latest_revision_id;
  if (typeof revisionId !== "string" || !SHA256_PATTERN.test(revisionId)) {
    return null;
  }
  return Object.freeze({ revisionId, document });
}

function errorCode(error) {
  if (error && typeof error.message === "string" && error.message.length > 0) {
    return error.message.slice(0, 80);
  }
  return "REFRESH_FAILED";
}

export function startRevisionStream({
  store,
  signal,
  fetchSnapshot = fetchDashboardSnapshot,
  EventSourceConstructor = globalThis.EventSource,
  setTimeoutFn = globalThis.setTimeout,
  clearTimeoutFn = globalThis.clearTimeout,
  random = Math.random,
  now = Date.now,
  pollIntervalMs = 5000,
  staleAfterMs = 60_000,
  reconnectBaseMs = 1000,
  reconnectCeilingMs = 16_000,
  onRevision = () => undefined,
  onReset = () => undefined,
  onStateChange = () => undefined,
} = {}) {
  if (
    !store ||
    typeof store.getState !== "function" ||
    typeof store.replaceSnapshot !== "function" ||
    typeof store.setConnectionState !== "function" ||
    typeof fetchSnapshot !== "function" ||
    typeof EventSourceConstructor !== "function" ||
    typeof setTimeoutFn !== "function" ||
    typeof clearTimeoutFn !== "function" ||
    typeof random !== "function" ||
    typeof now !== "function"
  ) {
    throw new TypeError("stream requires explicit observer dependencies");
  }
  if (
    !Number.isFinite(reconnectBaseMs) ||
    reconnectBaseMs <= 0 ||
    !Number.isFinite(reconnectCeilingMs) ||
    reconnectCeilingMs < reconnectBaseMs
  ) {
    throw new TypeError("reconnect bounds are invalid");
  }

  const refreshController = new AbortController();
  let eventSource = null;
  let polling = null;
  let reconnectTimer = null;
  let reconnectAttempt = 0;
  let sseHealthy = false;
  let activeRefresh = null;
  let activeAnnouncedRevision = null;
  let pendingAnnouncedRevision = null;
  let stopped = false;

  function setState(nextState, code = null) {
    store.setConnectionState(nextState, code);
    onStateChange(nextState, code);
  }

  function isStale(snapshot) {
    return snapshotIsStale(snapshot, { now, maxAgeMs: staleAfterMs });
  }

  function stateAfterValidSnapshot(snapshot) {
    if (isStale(snapshot)) {
      setState(STALE, "SNAPSHOT_STALE");
    } else if (sseHealthy) {
      setState(CONNECTED);
    } else {
      setState(DEGRADED, "SSE_UNAVAILABLE");
    }
  }

  async function performRefresh(announcedRevisionId) {
    try {
      let candidate = await fetchSnapshot({ signal: refreshController.signal });
      if (announcedRevisionId !== null && candidate?.revision_id !== announcedRevisionId) {
        candidate = await fetchSnapshot({ signal: refreshController.signal });
        if (candidate?.revision_id !== announcedRevisionId) {
          setState(INCONSISTENT, "REVISION_MISMATCH");
          return null;
        }
      }
      const accepted = store.replaceSnapshot(candidate);
      stateAfterValidSnapshot(accepted);
      return accepted;
    } catch (error) {
      if (refreshController.signal.aborted) {
        return null;
      }
      if (store.getState().connectionState !== INCONSISTENT) {
        setState(sseHealthy ? DEGRADED : DISCONNECTED, errorCode(error));
      }
      return null;
    }
  }

  function requestRefresh({ announcedRevisionId = null } = {}) {
    if (stopped || refreshController.signal.aborted) {
      return Promise.resolve(null);
    }
    if (activeRefresh !== null) {
      if (
        announcedRevisionId !== null &&
        announcedRevisionId !== activeAnnouncedRevision
      ) {
        pendingAnnouncedRevision = announcedRevisionId;
      }
      return activeRefresh;
    }

    activeAnnouncedRevision = announcedRevisionId;
    const run = performRefresh(announcedRevisionId);
    activeRefresh = run;
    run.finally(() => {
      if (activeRefresh !== run) {
        return;
      }
      activeRefresh = null;
      activeAnnouncedRevision = null;
      const pending = pendingAnnouncedRevision;
      pendingAnnouncedRevision = null;
      if (
        pending !== null &&
        !stopped &&
        store.getState().snapshot?.revision_id !== pending
      ) {
        requestRefresh({ announcedRevisionId: pending });
      }
    });
    return run;
  }

  async function whenIdle() {
    while (activeRefresh !== null) {
      const current = activeRefresh;
      await current;
      await Promise.resolve();
    }
  }

  function stopPolling() {
    polling?.stop();
    polling = null;
  }

  function beginPolling() {
    if (polling !== null || stopped) {
      return;
    }
    polling = startBoundedSnapshotPolling({
      refresh: () => requestRefresh(),
      signal: refreshController.signal,
      setTimeoutFn,
      clearTimeoutFn,
      intervalMs: pollIntervalMs,
    });
  }

  function closeEventSource() {
    eventSource?.close();
    eventSource = null;
  }

  function scheduleReconnect() {
    if (stopped || refreshController.signal.aborted || reconnectTimer !== null) {
      return;
    }
    const exponential = Math.min(
      reconnectCeilingMs,
      reconnectBaseMs * 2 ** reconnectAttempt,
    );
    reconnectAttempt += 1;
    const jitterFactor = 0.8 + Math.min(1, Math.max(0, random())) * 0.4;
    const delay = Math.min(reconnectCeilingMs, Math.round(exponential * jitterFactor));
    reconnectTimer = setTimeoutFn(() => {
      reconnectTimer = null;
      connectEventSource();
    }, delay);
  }

  function handleOpen() {
    sseHealthy = true;
    reconnectAttempt = 0;
    if (reconnectTimer !== null) {
      clearTimeoutFn(reconnectTimer);
      reconnectTimer = null;
    }
    stopPolling();
    const state = store.getState();
    if (state.connectionState === INCONSISTENT) {
      return;
    }
    if (state.snapshot !== null) {
      stateAfterValidSnapshot(state.snapshot);
    }
  }

  function handleError() {
    sseHealthy = false;
    closeEventSource();
    const snapshot = store.getState().snapshot;
    if (snapshot !== null) {
      setState(isStale(snapshot) ? STALE : DEGRADED, "SSE_UNAVAILABLE");
    }
    beginPolling();
    scheduleReconnect();
  }

  function handleNotification(eventName, event) {
    const notification = notificationRevision(eventName, event);
    if (notification === null) {
      setState(INCONSISTENT, "INVALID_STREAM_METADATA");
      return;
    }
    if (eventName === "revision") {
      onRevision(notification.document);
    } else {
      onReset(notification.document);
    }
    requestRefresh({ announcedRevisionId: notification.revisionId });
  }

  function connectEventSource() {
    if (stopped || refreshController.signal.aborted) {
      return;
    }
    closeEventSource();
    try {
      const source = new EventSourceConstructor(EVENTS_PATH);
      eventSource = source;
      source.addEventListener("open", handleOpen);
      source.addEventListener("error", handleError);
      source.addEventListener("revision", (event) => handleNotification("revision", event));
      source.addEventListener("reset", (event) => handleNotification("reset", event));
    } catch (_error) {
      handleError();
    }
  }

  function stop() {
    if (stopped) {
      return;
    }
    stopped = true;
    refreshController.abort();
    closeEventSource();
    stopPolling();
    if (reconnectTimer !== null) {
      clearTimeoutFn(reconnectTimer);
      reconnectTimer = null;
    }
  }

  if (signal?.aborted) {
    stop();
  } else {
    signal?.addEventListener("abort", stop, { once: true });
  }

  const ready = requestRefresh().then(() => {
    connectEventSource();
  });

  return Object.freeze({ ready, requestRefresh, whenIdle, close: stop });
}
