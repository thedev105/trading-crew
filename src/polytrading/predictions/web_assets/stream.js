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
      try {
        await refresh();
      } catch (_error) {
        // The owner records refresh health; polling itself must remain live.
      } finally {
        schedule();
      }
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
  requestTimeoutMs = 4000,
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
  if (
    !Number.isFinite(requestTimeoutMs) ||
    requestTimeoutMs <= 0 ||
    requestTimeoutMs > 30_000
  ) {
    throw new TypeError("snapshot request deadline is invalid");
  }

  const lifecycleController = new AbortController();
  let eventSource = null;
  let sourceGeneration = 0;
  let polling = null;
  let reconnectTimer = null;
  let reconnectAttempt = 0;
  let sseHealthy = false;
  let snapshotChannelHealthy = false;
  let snapshotChannelFailed = false;
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
    } else if (sseHealthy && snapshotChannelHealthy) {
      setState(CONNECTED);
    } else {
      setState(DEGRADED, sseHealthy ? "SNAPSHOT_UNAVAILABLE" : "SSE_UNAVAILABLE");
    }
  }

  function requestSnapshotWithDeadline() {
    const requestController = new AbortController();
    return new Promise((resolve, reject) => {
      let settled = false;
      let deadlineTimer = null;
      const settle = (callback, value) => {
        if (settled) {
          return;
        }
        settled = true;
        if (deadlineTimer !== null) {
          clearTimeoutFn(deadlineTimer);
          deadlineTimer = null;
        }
        lifecycleController.signal.removeEventListener("abort", handleLifecycleAbort);
        callback(value);
      };
      const handleLifecycleAbort = () => {
        requestController.abort();
        settle(reject, new Error("REFRESH_ABORTED"));
      };
      const handleDeadline = () => {
        requestController.abort();
        settle(reject, new Error("SNAPSHOT_TIMEOUT"));
      };

      try {
        deadlineTimer = setTimeoutFn(handleDeadline, requestTimeoutMs);
        lifecycleController.signal.addEventListener("abort", handleLifecycleAbort, {
          once: true,
        });
        if (lifecycleController.signal.aborted) {
          handleLifecycleAbort();
          return;
        }
        Promise.resolve(fetchSnapshot({ signal: requestController.signal })).then(
          (snapshot) => settle(resolve, snapshot),
          (error) => settle(reject, error),
        );
      } catch (error) {
        settle(reject, error);
      }
    });
  }

  function markSnapshotFailure(error) {
    snapshotChannelHealthy = false;
    snapshotChannelFailed = true;
    beginPolling();
    if (store.getState().connectionState === INCONSISTENT) {
      return;
    }
    const snapshot = store.getState().snapshot;
    if (snapshot !== null) {
      setState(isStale(snapshot) ? STALE : DEGRADED, errorCode(error));
    } else if (sseHealthy) {
      setState(DEGRADED, errorCode(error));
    } else {
      setState(DISCONNECTED, errorCode(error));
    }
  }

  async function performRefresh(announcedRevisionId) {
    try {
      let candidate = await requestSnapshotWithDeadline();
      if (announcedRevisionId !== null && candidate?.revision_id !== announcedRevisionId) {
        candidate = await requestSnapshotWithDeadline();
        if (candidate?.revision_id !== announcedRevisionId) {
          snapshotChannelHealthy = false;
          snapshotChannelFailed = true;
          setState(INCONSISTENT, "REVISION_MISMATCH");
          beginPolling();
          return null;
        }
      }
      const accepted = store.replaceSnapshot(candidate);
      snapshotChannelHealthy = true;
      snapshotChannelFailed = false;
      if (sseHealthy) {
        stopPolling();
      } else {
        beginPolling();
      }
      stateAfterValidSnapshot(accepted);
      return accepted;
    } catch (error) {
      if (lifecycleController.signal.aborted) {
        return null;
      }
      markSnapshotFailure(error);
      return null;
    }
  }

  function requestRefresh({ announcedRevisionId = null } = {}) {
    if (stopped || lifecycleController.signal.aborted) {
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
      signal: lifecycleController.signal,
      setTimeoutFn,
      clearTimeoutFn,
      intervalMs: pollIntervalMs,
    });
  }

  function closeEventSource() {
    const source = eventSource;
    eventSource = null;
    sourceGeneration += 1;
    source?.close();
  }

  function scheduleReconnect() {
    if (
      stopped ||
      lifecycleController.signal.aborted ||
      eventSource !== null ||
      reconnectTimer !== null
    ) {
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
    const state = store.getState();
    if (state.connectionState === INCONSISTENT) {
      beginPolling();
      return;
    }
    if (state.snapshot !== null) {
      if (snapshotChannelHealthy) {
        stopPolling();
      } else {
        beginPolling();
      }
      stateAfterValidSnapshot(state.snapshot);
    } else {
      setState(DEGRADED, "SNAPSHOT_UNAVAILABLE");
      beginPolling();
    }
  }

  function handleError() {
    sseHealthy = false;
    const snapshot = store.getState().snapshot;
    if (snapshot !== null) {
      setState(isStale(snapshot) ? STALE : DEGRADED, "SSE_UNAVAILABLE");
    } else if (snapshotChannelFailed) {
      setState(DISCONNECTED, "SSE_UNAVAILABLE");
    } else {
      setState(DEGRADED, "SSE_UNAVAILABLE");
    }
    beginPolling();
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
    if (stopped || lifecycleController.signal.aborted || eventSource !== null) {
      return;
    }
    let source;
    try {
      source = new EventSourceConstructor(EVENTS_PATH);
    } catch (_error) {
      handleError();
      scheduleReconnect();
      return;
    }
    eventSource = source;
    sourceGeneration += 1;
    const generation = sourceGeneration;
    const isCurrentSource = () =>
      !stopped && eventSource === source && sourceGeneration === generation;
    source.addEventListener("open", () => {
      if (isCurrentSource()) handleOpen();
    });
    source.addEventListener("error", () => {
      if (isCurrentSource()) handleError();
    });
    source.addEventListener("revision", (event) => {
      if (isCurrentSource()) handleNotification("revision", event);
    });
    source.addEventListener("reset", (event) => {
      if (isCurrentSource()) handleNotification("reset", event);
    });
  }

  function stop() {
    if (stopped) {
      return;
    }
    stopped = true;
    lifecycleController.abort();
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

  connectEventSource();
  const ready = requestRefresh();

  return Object.freeze({ ready, requestRefresh, whenIdle, close: stop });
}
