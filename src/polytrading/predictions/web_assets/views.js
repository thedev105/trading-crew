import { freshnessArcSvg, sparklineSvg } from "./charts.js";

function domFor(context) {
  const documentRef = context.documentRef ?? globalThis.document;
  if (!documentRef || typeof documentRef.createElement !== "function") {
    throw new TypeError("a DOM document is required");
  }
  return documentRef;
}

function element(dom, tagName, className = "", text = null) {
  const node = dom.createElement(tagName);
  node.className = className;
  if (text !== null) {
    node.textContent = String(text);
  }
  return node;
}

function setTone(node, tone) {
  node.setAttribute("data-tone", tone);
  return node;
}

function badge(dom, label, tone = "muted") {
  return setTone(element(dom, "span", "state-badge", label), tone);
}

function panel(dom, title, eyebrow, ...children) {
  const container = element(dom, "article", "panel");
  const heading = element(dom, "header", "panel__heading");
  const headingCopy = element(dom, "div");
  headingCopy.append(
    element(dom, "p", "eyebrow", eyebrow),
    element(dom, "h2", "panel__title", title),
  );
  heading.append(headingCopy);
  container.append(heading, ...children);
  return container;
}

function emptyState(dom, title, detail) {
  const node = element(dom, "div", "empty-state");
  node.append(
    element(dom, "p", "empty-state__title", title),
    element(dom, "p", "muted", detail),
  );
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
  const timestamp = Date.parse(value);
  const difference = Math.max(0, now() - timestamp);
  if (!Number.isFinite(difference)) {
    return "age unavailable";
  }
  const seconds = Math.floor(difference / 1000);
  if (seconds < 60) {
    return `${seconds}s ago`;
  }
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) {
    return `${minutes}m ago`;
  }
  const hours = Math.floor(minutes / 60);
  return `${hours}h ago`;
}

function timestampCopy(value, now) {
  return `${formatUtc(value)} · ${relativeAge(value, now)}`;
}

function metricCard(dom, label, value, detail, tone = "accent") {
  const card = element(dom, "article", "metric-card");
  const valueNode = setTone(element(dom, "p", "metric-card__value", value), tone);
  if (/^[A-Z][A-Z0-9_]+$/.test(String(value))) {
    valueNode.className += " metric-card__value--token";
  }
  card.append(
    element(dom, "p", "metric-card__label", label),
    valueNode,
    element(dom, "p", "metric-card__detail", detail),
  );
  return card;
}

function tableRegion(dom, label, headings, rows, { modifier = "" } = {}) {
  const region = element(dom, "div", "table-region");
  region.setAttribute("role", "region");
  region.setAttribute("aria-label", label);
  region.setAttribute("tabindex", "0");
  const tableClass = modifier ? `data-table data-table--${modifier}` : "data-table";
  const table = element(dom, "table", tableClass);
  const head = element(dom, "thead");
  const headRow = element(dom, "tr");
  for (const heading of headings) {
    const cell = element(dom, "th", "", heading);
    cell.setAttribute("scope", "col");
    headRow.append(cell);
  }
  head.append(headRow);
  const body = element(dom, "tbody");
  for (const values of rows) {
    const row = element(dom, "tr");
    values.forEach((value, index) => {
      const cell = element(dom, index === 0 ? "th" : "td", "", value);
      if (index === 0) {
        cell.setAttribute("scope", "row");
      }
      row.append(cell);
    });
    body.append(row);
  }
  table.append(head, body);
  region.append(table);
  return region;
}

function hashList(dom, hashes, emptyText = "No hashes published for this snapshot.") {
  if (!Array.isArray(hashes) || hashes.length === 0) {
    return element(dom, "p", "muted", emptyText);
  }
  const list = element(dom, "ul", "hash-list");
  for (const hash of hashes) {
    const item = element(dom, "li");
    item.append(element(dom, "code", "hash-value", hash));
    list.append(item);
  }
  return list;
}

function integrityNotice(dom) {
  const notice = element(dom, "section", "integrity-notice");
  notice.setAttribute("role", "alert");
  notice.setAttribute("aria-label", "Snapshot integrity warning");
  const copy = element(dom, "div", "integrity-notice__copy");
  copy.append(
    element(dom, "h2", "integrity-notice__title", "Snapshot validation failed"),
    element(
      dom,
      "p",
      "muted",
      "Showing the last verified snapshot; financial values are suppressed until integrity is restored.",
    ),
  );
  notice.append(badge(dom, "INCONSISTENT", "fault"), copy);
  return notice;
}

function finalizeRoot(root, context, ...children) {
  const visibleChildren = context.connectionState === "INCONSISTENT"
    ? [integrityNotice(domFor(context)), ...children]
    : children;
  root.replaceChildren(...visibleChildren);
  root.setAttribute("aria-busy", "false");
  return root;
}

function stateTone(state) {
  if (["UNKNOWN", "FAILED", "INCOMPLETE", "ENGAGED"].includes(state)) {
    return "fault";
  }
  if (["RETRYING", "ACK_DELAYED", "CANCEL_PENDING", "LIVE_DISABLED"].includes(state)) {
    return "warning";
  }
  if (["COMPLETE", "CONFIRMED", "RECONCILED", "FILLED"].includes(state)) {
    return "healthy";
  }
  return "accent";
}

function sortedTimeline(snapshot) {
  return [...snapshot.execution_timeline].sort((left, right) => {
    const time = Date.parse(left.occurred_at) - Date.parse(right.occurred_at);
    return time || String(left.record_id).localeCompare(String(right.record_id));
  });
}

export function renderOverview(root, snapshot, context = {}) {
  const dom = domFor(context);
  const now = context.now ?? Date.now;
  const unknownCount = snapshot.execution_timeline.filter((entry) => entry.state === "UNKNOWN").length;
  const incomplete = snapshot.live_ledger.incomplete_reconciliation_count;
  const metrics = element(dom, "section", "metric-grid", null);
  metrics.setAttribute("aria-label", "Operating posture metrics");
  metrics.append(
    metricCard(
      dom,
      "Implementation posture",
      snapshot.execution_readiness.implementation_state,
      snapshot.execution_readiness.kill_engaged ? "Kill state engaged" : "Kill state unavailable",
      "warning",
    ),
    metricCard(
      dom,
      "Opportunity intelligence",
      String(snapshot.opportunities.length),
      `${snapshot.opportunities.length} observed opportunities`,
    ),
    metricCard(
      dom,
      "Recovery attention",
      String(unknownCount),
      unknownCount === 0 ? "No UNKNOWN lifecycle state" : "UNKNOWN lifecycle evidence present",
      unknownCount === 0 ? "healthy" : "fault",
    ),
    metricCard(
      dom,
      "Reconciliation",
      incomplete === 0 ? "COMPLETE" : "INCOMPLETE",
      `${snapshot.live_ledger.complete_reconciliation_count} complete · ${incomplete} incomplete`,
      incomplete === 0 ? "healthy" : "fault",
    ),
  );

  const cutoffAge = Math.max(0, (now() - Date.parse(snapshot.as_of)) / 1000);
  const freshnessBody = element(dom, "div", "freshness-layout");
  freshnessBody.append(
    freshnessArcSvg(cutoffAge, 60, {
      documentRef: dom,
      title: "Snapshot cutoff age",
      description: `Snapshot observed ${relativeAge(snapshot.as_of, now)}.`,
    }),
    element(dom, "p", "freshness-copy", timestampCopy(snapshot.as_of, now)),
  );
  const freshness = panel(dom, "Snapshot freshness", "ONE IMMUTABLE CUTOFF", freshnessBody);

  const timeline = sortedTimeline(snapshot);
  const timelineBody = element(dom, "div", "timeline-overview");
  if (timeline.length === 0) {
    timelineBody.append(
      emptyState(dom, "No execution history", "No lifecycle evidence is visible at this cutoff."),
    );
  } else {
    const list = element(dom, "ol", "timeline");
    for (const entry of timeline.slice(-6)) {
      const item = element(dom, "li", "timeline__item");
      item.append(
        badge(dom, entry.state, stateTone(entry.state)),
        element(dom, "span", "timeline__kind", entry.kind),
        element(dom, "time", "timeline__time", timestampCopy(entry.occurred_at, now)),
      );
      list.append(item);
    }
    timelineBody.append(list);
    const times = timeline.map((entry) => Date.parse(entry.occurred_at) / 1000);
    timelineBody.append(
      sparklineSvg(times, {
        documentRef: dom,
        title: "Execution evidence cadence",
        description: `${times.length} lifecycle facts ordered by observed time.`,
      }),
    );
  }
  const activity = panel(dom, "Session chronology", "LATEST OBSERVED EVIDENCE", timelineBody);

  const details = element(dom, "section", "overview-grid");
  details.append(freshness, activity);
  return finalizeRoot(root, context, metrics, details);
}

function opportunitySort(left, right) {
  const leftValue = Number(left.conservative_surplus_usd ?? Number.NEGATIVE_INFINITY);
  const rightValue = Number(right.conservative_surplus_usd ?? Number.NEGATIVE_INFINITY);
  if (leftValue !== rightValue) {
    return rightValue - leftValue;
  }
  return String(left.candidate_id).localeCompare(String(right.candidate_id));
}

function unavailableFact(dom, label, detail) {
  const fact = element(dom, "div", "unavailable-fact");
  fact.append(
    element(dom, "span", "unavailable-fact__label", label),
    element(dom, "strong", "unavailable-fact__value", "Unavailable"),
    element(dom, "span", "muted", detail),
  );
  return fact;
}

export function renderMarkets(root, snapshot, context = {}) {
  const dom = domFor(context);
  const opportunities = [...snapshot.opportunities].sort(opportunitySort);
  const opportunityGrid = element(dom, "div", "opportunity-grid");
  if (opportunities.length === 0) {
    opportunityGrid.append(
      emptyState(
        dom,
        "No opportunity intelligence",
        "No proof-backed opportunity records are visible at this cutoff.",
      ),
    );
  } else {
    for (const opportunity of opportunities) {
      const card = element(dom, "article", "opportunity-card");
      const heading = element(dom, "div", "opportunity-card__heading");
      const title = element(dom, "div");
      title.append(
        element(dom, "p", "eyebrow", opportunity.relationship_type),
        element(dom, "h3", "opportunity-card__title", opportunity.candidate_id),
      );
      heading.append(
        title,
        badge(
          dom,
          opportunity.reconciled ? "RECONCILED" : "UNRECONCILED",
          opportunity.reconciled ? "healthy" : "warning",
        ),
      );
      const economics = element(dom, "dl", "opportunity-economics");
      for (const [label, value] of [
        ["Decision", opportunity.decision ?? "Unavailable"],
        ["Conservative surplus", opportunity.conservative_surplus_usd ?? "Unavailable"],
        ["Observed capacity", opportunity.capacity_usd ?? "Unavailable"],
      ]) {
        const row = element(dom, "div");
        row.append(element(dom, "dt", "", label), element(dom, "dd", "", value));
        economics.append(row);
      }
      const unavailable = element(dom, "div", "unavailable-grid");
      unavailable.append(
        unavailableFact(dom, "Probability", "Not present in the observer snapshot."),
        unavailableFact(dom, "Depth", "No book-depth projection is published here."),
        unavailableFact(dom, "Liquidity", "Capacity is not treated as venue liquidity."),
      );
      const evidence = element(dom, "section", "opportunity-evidence");
      evidence.append(element(dom, "h3", "section-label", "Evidence hashes"));
      evidence.append(hashList(dom, opportunity.evidence_hashes));
      card.append(heading, economics, unavailable, evidence);
      opportunityGrid.append(card);
    }
  }
  const opportunitiesPanel = panel(
    dom,
    "Ranked opportunities",
    `${opportunities.length} OBSERVED AT CUTOFF`,
    opportunityGrid,
  );

  const marketRows = snapshot.markets.map((market) => [
    market.market_id,
    market.venue,
    market.question,
    market.closed ? "CLOSED" : market.active ? "ACTIVE" : "INACTIVE",
    formatUtc(market.retrieved_at),
  ]);
  const marketBody = marketRows.length === 0
    ? emptyState(dom, "No market records", "No markets are visible at this snapshot cutoff.")
    : tableRegion(
        dom,
        "Observed market records",
        ["Market", "Venue", "Question", "State", "Retrieved UTC"],
        marketRows,
      );
  const marketsPanel = panel(dom, "Observed markets", "SOURCE RECORDS", marketBody);
  return finalizeRoot(root, context, opportunitiesPanel, marketsPanel);
}

export function renderExecution(root, snapshot, context = {}) {
  const dom = domFor(context);
  const now = context.now ?? Date.now;
  const readiness = snapshot.execution_readiness;
  const postureBody = element(dom, "div", "readiness-grid");
  postureBody.append(
    metricCard(dom, "Implementation", readiness.implementation_state, "Shipped posture", "warning"),
    metricCard(dom, "Kill state", readiness.kill_engaged ? "ENGAGED" : "Unavailable", "Persisted safety evidence", "fault"),
    metricCard(dom, "Protocol", readiness.protocol_state, readiness.conformance_result),
    metricCard(dom, "Live action", readiness.live_action_available ? "AVAILABLE" : "UNAVAILABLE", "Observer carries no authority", "healthy"),
  );
  const gates = element(dom, "div", "gate-strip");
  gates.append(element(dom, "p", "section-label", "Unmet gates"));
  for (const gate of readiness.unmet_gates) {
    gates.append(badge(dom, gate, "warning"));
  }
  const posture = panel(dom, "Execution posture", "AUTHORITY BOUNDARY", postureBody, gates);

  const timeline = sortedTimeline(snapshot);
  const rows = timeline.map((entry) => [
    entry.kind,
    entry.state,
    timestampCopy(entry.occurred_at, now),
    entry.reconciled ? "RECONCILED" : "UNRECONCILED",
    entry.reason_code ?? "None",
    entry.evidence_hashes.join(" · ") || "No hashes published",
  ]);
  const chronologyBody = rows.length === 0
    ? emptyState(dom, "No execution chronology", "No plan, intent, order, trade, kill, or reconciliation facts are visible.")
    : tableRegion(
        dom,
        "Complete execution chronology",
        ["Kind", "State", "Occurred UTC", "Reconciliation", "Reason", "Evidence hashes"],
        rows,
        { modifier: "execution" },
      );
  const chronology = panel(dom, "Lifecycle chronology", "COMPLETE OBSERVED HISTORY", chronologyBody);

  const unknown = timeline.filter((entry) => entry.state === "UNKNOWN");
  const recoveryBody = element(dom, "div", "recovery-stack");
  if (unknown.length === 0) {
    recoveryBody.append(
      emptyState(dom, "No UNKNOWN state", "No unresolved delivery outcome is visible at this cutoff."),
    );
  } else {
    for (const entry of unknown) {
      const record = element(dom, "article", "recovery-card");
      record.append(
        badge(dom, "UNKNOWN", "fault"),
        element(dom, "h3", "recovery-card__title", "Recovery evidence requires attention"),
        element(dom, "p", "mono", entry.record_id),
        element(dom, "p", "muted", entry.reason_code ?? "RECONCILIATION_ACTION_REQUIRED"),
        hashList(dom, entry.evidence_hashes),
      );
      recoveryBody.append(record);
    }
  }
  const recovery = panel(dom, "Recovery evidence", "FAIL-CLOSED OUTCOMES", recoveryBody);
  return finalizeRoot(root, context, posture, chronology, recovery);
}

export function renderLedger(root, snapshot, context = {}) {
  const dom = domFor(context);
  const hidden = context.financialsHidden || context.connectionState === "INCONSISTENT";
  const ledger = snapshot.live_ledger;
  const pnlCard = element(dom, "section", "pnl-card");
  pnlCard.append(element(dom, "p", "eyebrow", "REALIZED P&L GATE"));
  if (hidden) {
    pnlCard.append(
      setTone(element(dom, "p", "pnl-card__value", "Financial totals hidden"), "fault"),
      element(dom, "p", "muted", "Snapshot integrity is inconsistent; last-good financial values remain suppressed."),
    );
  } else if (ledger.pnl_publishable && ledger.realized_pnl_usd !== null) {
    pnlCard.append(
      setTone(element(dom, "p", "pnl-card__value", `$${ledger.realized_pnl_usd}`), "healthy"),
      element(dom, "p", "muted", "Published only because authoritative reconciliation permits it."),
    );
  } else {
    pnlCard.append(
      setTone(element(dom, "p", "pnl-card__value", "Unavailable"), "warning"),
      element(dom, "p", "muted", "Reconciliation evidence does not permit P&L publication."),
    );
  }

  const metrics = element(dom, "section", "metric-grid");
  metrics.setAttribute("aria-label", "Ledger evidence metrics");
  metrics.append(
    metricCard(dom, "Ledger postings", String(ledger.posting_count), "Observed posting records"),
    metricCard(dom, "Reconciliations", String(ledger.reconciliation_count), "Authoritative account cuts"),
    metricCard(dom, "Complete", String(ledger.complete_reconciliation_count), "Exact closures", "healthy"),
    metricCard(dom, "Incomplete", String(ledger.incomplete_reconciliation_count), "Unresolved differences", ledger.incomplete_reconciliation_count ? "fault" : "healthy"),
  );
  const summary = panel(dom, "Authoritative ledger", "RECONCILIATION BEFORE PUBLICATION", pnlCard, metrics);

  const availability = element(dom, "div", "availability-grid");
  availability.append(
    unavailableFact(dom, "Fee detail", "Only aggregate evidence is present in this snapshot."),
    unavailableFact(dom, "Position detail", "Per-asset positions are not published to the browser."),
    unavailableFact(dom, "Posting lines", "Individual debit and credit lines are not exposed here."),
  );
  const detail = panel(dom, "Ledger detail availability", "NO FABRICATED BREAKDOWN", availability);
  return finalizeRoot(root, context, summary, detail);
}

export function renderEvidence(root, snapshot, context = {}) {
  const dom = domFor(context);
  const evidence = snapshot.evidence_status;
  const protocolMetrics = element(dom, "section", "metric-grid");
  protocolMetrics.setAttribute("aria-label", "Protocol evidence status");
  protocolMetrics.append(
    metricCard(dom, "Protocol", evidence.protocol_version, evidence.protocol_state),
    metricCard(dom, "Conformance", evidence.conformance_result, evidence.conformance_observed_at ? formatUtc(evidence.conformance_observed_at) : "No conformance observation", evidence.conformance_result === "CONFORMANT" ? "healthy" : "warning"),
    metricCard(dom, "Manifest", evidence.manifest_state, "Execution posture", "warning"),
    metricCard(dom, "Accounts", String(evidence.account_count), "Verified account evidence"),
  );
  const protocol = panel(dom, "Protocol and review status", "EVIDENCE BOUNDARY", protocolMetrics);

  const gates = element(dom, "div", "gate-list");
  if (evidence.unmet_activation_gates.length === 0) {
    gates.append(emptyState(dom, "No gate list", "No unmet-gate values are published."));
  } else {
    for (const gate of evidence.unmet_activation_gates) {
      const item = element(dom, "div", "gate-list__item");
      item.append(badge(dom, "BLOCKED", "warning"), element(dom, "code", "", gate));
      gates.append(item);
    }
  }
  const gatePanel = panel(dom, "Safety gates", "CLOSED POSTURE", gates);

  const hashes = panel(
    dom,
    "Source hashes",
    `${evidence.source_hashes.length} VERIFIED REFERENCES`,
    hashList(dom, evidence.source_hashes),
  );

  const countRows = Object.entries(snapshot.evidence_counts.counts)
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([name, count]) => [name, String(count)]);
  const countsBody = countRows.length === 0
    ? emptyState(dom, "No evidence counts", "No evidence categories are published at this cutoff.")
    : tableRegion(dom, "Evidence record counts", ["Evidence family", "Count"], countRows);
  const counts = panel(dom, "Evidence inventory", "RECORD COUNTS", countsBody);

  const recipeList = element(dom, "ol", "recipe-list");
  if (snapshot.recipes.recipes.length === 0) {
    recipeList.append(element(dom, "li", "muted", "No observer recipes are published."));
  } else {
    for (const recipe of snapshot.recipes.recipes) {
      const item = element(dom, "li");
      item.append(element(dom, "code", "recipe-value", recipe));
      recipeList.append(item);
    }
  }
  const recipes = panel(dom, "Evidence recipes", "REFERENCE ONLY", recipeList);
  return finalizeRoot(root, context, protocol, gatePanel, hashes, counts, recipes);
}
