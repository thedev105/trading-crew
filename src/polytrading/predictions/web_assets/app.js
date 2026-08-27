import { startRevisionStream } from "./stream.js";
import { createSnapshotStore } from "./store.js";
import {
  renderEvidence,
  renderExecution,
  renderLedger,
  renderMarkets,
  renderOverview,
} from "./views.js";

const VIEW_ORDER = Object.freeze(["overview", "markets", "execution", "ledger", "evidence"]);
const VIEW_DETAILS = Object.freeze({
  overview: ["Overview", "Operating posture and evidence freshness"],
  markets: ["Markets", "Ranked opportunity intelligence and observed market records"],
  execution: ["Execution", "Read-only lifecycle chronology and recovery evidence"],
  ledger: ["Ledger", "Authoritative reconciliation and financial publication gates"],
  evidence: ["Evidence", "Protocol, sources, safety gates, and observer recipes"],
});
const VIEW_RENDERERS = Object.freeze({
  overview: renderOverview,
  markets: renderMarkets,
  execution: renderExecution,
  ledger: renderLedger,
  evidence: renderEvidence,
});

function requiredNode(documentRef, id) {
  const node = documentRef.getElementById(id);
  if (node === null) {
    throw new Error(`MARKET_ATLAS_NODE_MISSING:${id}`);
  }
  return node;
}

function formatUtc(value) {
  const timestamp = new Date(value);
  if (!Number.isFinite(timestamp.getTime())) {
    return "Unavailable";
  }
  return timestamp
    .toISOString()
    .replace("T", " ")
    .replace(/\.000Z$/, " UTC")
    .replace(/Z$/, " UTC");
}

function relativeAge(value, now) {
  const difference = Math.max(0, now() - Date.parse(value));
  if (!Number.isFinite(difference)) {
    return "age unavailable";
  }
  const seconds = Math.floor(difference / 1000);
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  return `${Math.floor(minutes / 60)}h ago`;
}

function stateTone(state) {
  if (state === "CONNECTED") return "healthy";
  if (state === "DEGRADED" || state === "STALE") return "warning";
  return "fault";
}

function errorPanel(documentRef, title, detail) {
  const panel = documentRef.createElement("div");
  panel.className = "empty-state empty-state--fault";
  const heading = documentRef.createElement("h2");
  heading.className = "empty-state__title";
  heading.textContent = title;
  const copy = documentRef.createElement("p");
  copy.className = "muted";
  copy.textContent = detail;
  panel.append(heading, copy);
  return panel;
}

export function startMarketAtlas({
  documentRef = globalThis.document,
  windowRef = globalThis.window,
  startStream = startRevisionStream,
  scheduleNotification = queueMicrotask,
  requestAnimationFrameFn = globalThis.requestAnimationFrame?.bind(globalThis) ??
    ((callback) => globalThis.setTimeout(callback, 0)),
  cancelAnimationFrameFn = globalThis.cancelAnimationFrame?.bind(globalThis) ??
    globalThis.clearTimeout.bind(globalThis),
  now = Date.now,
} = {}) {
  if (!documentRef || !windowRef) {
    throw new TypeError("Market Atlas requires a browser document");
  }
  const nodes = {
    root: requiredNode(documentRef, "view-root"),
    title: requiredNode(documentRef, "view-title"),
    context: requiredNode(documentRef, "view-context"),
    connection: requiredNode(documentRef, "connection-state"),
    implementation: requiredNode(documentRef, "implementation-state"),
    kill: requiredNode(documentRef, "kill-state"),
    cutoff: requiredNode(documentRef, "snapshot-cutoff"),
    refresh: requiredNode(documentRef, "last-refresh"),
    summary: requiredNode(documentRef, "update-summary"),
    overlay: requiredNode(documentRef, "connection-overlay"),
    overlayTitle: requiredNode(documentRef, "connection-overlay-title"),
    overlayDetail: requiredNode(documentRef, "connection-overlay-detail"),
  };
  const tabs = [...documentRef.querySelectorAll("[data-view]")];
  if (tabs.length !== VIEW_ORDER.length) {
    throw new Error("MARKET_ATLAS_NAVIGATION_INVALID");
  }

  const abortController = new AbortController();
  const store = createSnapshotStore({ scheduleNotification, now });
  let currentView = "overview";
  let scheduledFrame = null;
  let stream = null;
  let stopped = false;

  function updateRail(state) {
    const snapshot = state.displaySnapshot;
    nodes.connection.textContent = state.connectionState;
    nodes.connection.setAttribute("data-tone", stateTone(state.connectionState));
    documentRef.body.setAttribute("data-connection", state.connectionState.toLowerCase());

    if (snapshot === null) {
      nodes.implementation.textContent = "LIVE_DISABLED";
      nodes.kill.textContent = "ENGAGED · enforced";
      nodes.cutoff.textContent = "Awaiting snapshot";
      nodes.refresh.textContent = "Not yet";
    } else {
      nodes.implementation.textContent = snapshot.execution_readiness.implementation_state;
      nodes.kill.textContent = snapshot.execution_readiness.kill_engaged
        ? "ENGAGED · enforced"
        : "Unavailable";
      nodes.cutoff.textContent = `${formatUtc(snapshot.as_of)} · ${relativeAge(snapshot.as_of, now)}`;
      nodes.refresh.textContent = state.lastRefreshAt === null
        ? "Not yet"
        : `${formatUtc(state.lastRefreshAt)} · ${relativeAge(state.lastRefreshAt, now)}`;
    }

    const disconnectedWithSnapshot =
      state.connectionState === "DISCONNECTED" && state.snapshot !== null;
    nodes.overlay.hidden = !disconnectedWithSnapshot;
    if (disconnectedWithSnapshot) {
      nodes.overlayTitle.textContent = "Observer channels unavailable";
      nodes.overlayDetail.textContent = "Retaining the last verified snapshot behind this notice.";
    }
    nodes.summary.textContent = snapshot === null
      ? `${state.connectionState}. ${VIEW_DETAILS[currentView][0]} has no verified snapshot.`
      : `${state.connectionState}. ${VIEW_DETAILS[currentView][0]} snapshot cutoff ${formatUtc(snapshot.as_of)}.`;

    const staleNotice = documentRef.getElementById("stale-notice");
    if (staleNotice !== null) {
      staleNotice.hidden = state.connectionState !== "STALE";
      staleNotice.textContent = state.connectionState === "STALE"
        ? "Showing the last verified snapshot; one or more freshness thresholds are exceeded."
        : "";
    }
  }

  function render() {
    scheduledFrame = null;
    if (stopped) {
      return;
    }
    const state = store.getState();
    updateRail(state);
    if (state.displaySnapshot === null) {
      nodes.root.setAttribute("aria-busy", "false");
      const unavailable = state.connectionState === "DISCONNECTED";
      nodes.root.replaceChildren(
        errorPanel(
          documentRef,
          unavailable ? "Snapshot unavailable" : "Establishing verified snapshot",
          unavailable
            ? "Both observation channels are unavailable. No unverified data will be displayed."
            : "Waiting for one complete same-origin snapshot.",
        ),
      );
      return;
    }
    nodes.root.setAttribute("aria-busy", "true");
    VIEW_RENDERERS[currentView](nodes.root, state.displaySnapshot, {
      connectionState: state.connectionState,
      financialsHidden: state.financialsHidden,
      documentRef,
      now,
    });
    renderLegacy(state.displaySnapshot);
  }

  function scheduleRender() {
    if (stopped || scheduledFrame !== null) {
      return;
    }
    scheduledFrame = requestAnimationFrameFn(render);
  }

  function selectView(view, { focus = true } = {}) {
    if (!VIEW_ORDER.includes(view)) {
      throw new TypeError("unknown Market Atlas view");
    }
    currentView = view;
    const [title, context] = VIEW_DETAILS[view];
    nodes.title.textContent = title;
    nodes.context.textContent = context;
    for (const tab of tabs) {
      const selected = tab.dataset.view === view;
      tab.setAttribute("aria-selected", selected ? "true" : "false");
      tab.setAttribute("tabindex", selected ? "0" : "-1");
      if (selected) {
        nodes.root.setAttribute("aria-labelledby", tab.id);
        if (focus) tab.focus({ preventScroll: true });
      }
    }
    scheduleRender();
  }

  function handleTabKeydown(event, index) {
    let nextIndex = null;
    if (event.key === "ArrowRight" || event.key === "ArrowDown") {
      nextIndex = (index + 1) % tabs.length;
    } else if (event.key === "ArrowLeft" || event.key === "ArrowUp") {
      nextIndex = (index - 1 + tabs.length) % tabs.length;
    } else if (event.key === "Home") {
      nextIndex = 0;
    } else if (event.key === "End") {
      nextIndex = tabs.length - 1;
    }
    if (nextIndex === null) {
      return;
    }
    event.preventDefault();
    selectView(tabs[nextIndex].dataset.view, { focus: true });
  }

  tabs.forEach((tab, index) => {
    tab.addEventListener("click", () => selectView(tab.dataset.view, { focus: false }));
    tab.addEventListener("keydown", (event) => handleTabKeydown(event, index));
  });

  const unsubscribe = store.subscribe(scheduleRender);
  stream = startStream({
    store,
    signal: abortController.signal,
    now,
  });

  function abort() {
    if (stopped) return;
    stopped = true;
    abortController.abort();
    stream?.close();
    unsubscribe();
    if (scheduledFrame !== null) {
      cancelAnimationFrameFn(scheduledFrame);
      scheduledFrame = null;
    }
    windowRef.removeEventListener("pagehide", abort);
  }

  windowRef.addEventListener("pagehide", abort, { once: true });
  selectView("overview", { focus: false });
  const ready = Promise.resolve(stream.ready).then(() => {
    scheduleRender();
  });
  return Object.freeze({ store, ready, selectView, abort });
}

// Downstream static consumers still inspect these hidden, read-only summaries.
function cell(text) {
  const node = document.createElement("span");
  node.textContent = text;
  return node;
}

function renderCandidates(snapshot) {
  const target = globalThis.document?.getElementById("candidates");
  if (target === null || target === undefined) return;
  target.replaceChildren();
  for (const candidate of snapshot.candidates.latest) {
    const row = document.createElement("p");
    row.append(cell(candidate.disposition));
    if (candidate.provenance_kind === "ai") {
      const marker = document.createElement("span");
      marker.textContent = "AI-nominated";
      row.append(marker);
    }
    target.append(row);
  }
}

function renderProofs(snapshot) {
  const target = globalThis.document?.getElementById("proofs");
  if (target === null || target === undefined) return;
  target.replaceChildren();
  for (const proof of snapshot.proofs.latest) {
    const row = document.createElement("p");
    row.append(cell(proof.status));
    target.append(row);
  }
}

function renderScans(snapshot) {
  const target = globalThis.document?.getElementById("scans");
  if (target === null || target === undefined) return;
  target.replaceChildren();
  for (const scan of snapshot.scans.latest) {
    const decisionCell = document.createElement("span");
    decisionCell.textContent = scan.decision;
    if (scan.decision === "SHADOW_CANDIDATE") {
      const caption = "research decision — not an instruction to trade";
      decisionCell.append(` ${caption}`);
    }
    target.append(decisionCell);
  }
}

function renderShadow(snapshot) {
  const target = globalThis.document?.getElementById("shadow-proposals");
  if (target === null || target === undefined) return;
  target.replaceChildren();
  for (const shadow of snapshot.shadow.latest) {
    const row = document.createElement("p");
    const isReconciled = shadow.current_state === "reconciled";
    const paperPnl = isReconciled && shadow.paper_pnl !== null
      ? shadow.paper_pnl
      : "not available";
    row.textContent = `${shadow.current_state} ${paperPnl}`;
    if (shadow.current_state === "unknown") {
      row.append(" awaiting reconciliation — paper result invalid");
    }
    target.append(row);
  }
}

function renderLegacy(snapshot) {
  renderCandidates(snapshot);
  renderProofs(snapshot);
  renderScans(snapshot);
  renderShadow(snapshot);
}

if (globalThis.document?.getElementById("view-root")) {
  startMarketAtlas();
}
