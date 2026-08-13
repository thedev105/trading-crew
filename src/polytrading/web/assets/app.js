const state = {
  lastSnapshot: null,
  refreshing: false,
  timer: null,
};

const nodes = {
  database: document.querySelector("#database-name"),
  snapshotTime: document.querySelector("#snapshot-time"),
  refreshStatus: document.querySelector("#refresh-status"),
  refresh: document.querySelector("#refresh"),
  overviewCards: document.querySelector("#overview-cards"),
  boundaryStrip: document.querySelector("#boundary-strip"),
  marketRows: document.querySelector("#market-rows"),
  discoverySummary: document.querySelector("#discovery-summary"),
  candidateRows: document.querySelector("#candidate-rows"),
  dossierRows: document.querySelector("#dossier-rows"),
  dossierLeftHeading: document.querySelector("#dossier-left-heading"),
  dossierRightHeading: document.querySelector("#dossier-right-heading"),
  carryRows: document.querySelector("#carry-rows"),
  evidenceCounts: document.querySelector("#evidence-counts"),
  recipeList: document.querySelector("#recipe-list"),
};

const statusTone = (value) => String(value || "missing").toLowerCase();
const display = (value) => value === null || value === undefined ? "Unavailable" : String(value);
const compactTime = (value) => value ? value.replace("T", " ").replace("Z", " UTC") : "Unavailable";
const shortTime = (value) => value ? value.slice(11, 16) : "—";

function element(tag, className, content) {
  const result = document.createElement(tag);
  if (className) result.className = className;
  if (content !== undefined) result.textContent = content;
  return result;
}

function setStatus(message, mode) {
  nodes.refreshStatus.textContent = message;
  nodes.refreshStatus.dataset.state = mode;
}

function statusCard(label, value, detail, tone) {
  const card = element("article", "status-card");
  card.dataset.tone = statusTone(tone);
  card.append(
    element("p", "label", label),
    element("p", "value", value),
    element("p", "detail", detail),
  );
  return card;
}

function renderOverview(snapshot) {
  const health = snapshot.funding_health;
  const fundingCycle = snapshot.latest_funding_cycle;
  const bookCycle = snapshot.latest_book_cycle;
  const coverage = `${health.complete_boundary_count}/${health.requested_hours}`;
  nodes.overviewCards.replaceChildren(
    statusCard("Funding health", health.status, `As of ${compactTime(health.as_of)}`, health.status),
    statusCard("Complete coverage", coverage, `Current streak ${health.current_complete_streak}h`, health.status),
    statusCard(
      "Latest funding cycle",
      fundingCycle ? fundingCycle.status : "Unavailable",
      fundingCycle ? compactTime(fundingCycle.request_completed_at) : "No recorded cycle",
      fundingCycle ? fundingCycle.status : "missing",
    ),
    statusCard(
      "Latest book cycle",
      bookCycle ? bookCycle.status : "Unavailable",
      bookCycle ? `${display(bookCycle.max_effective_skew_ms)} ms max skew` : "No recorded cycle",
      bookCycle ? bookCycle.status : "missing",
    ),
  );

  const cells = health.boundaries.map((boundary) => {
    const cell = element("div", "boundary-cell");
    cell.dataset.tone = statusTone(boundary.status);
    const reasons = boundary.reason_codes.length ? boundary.reason_codes.join(", ") : "No reason codes";
    const label = `${compactTime(boundary.cycle_end)}: ${boundary.status}. ${reasons}`;
    cell.title = label;
    cell.setAttribute("aria-label", label);
    return cell;
  });
  nodes.boundaryStrip.replaceChildren(...cells);
}

function numericClass(value) {
  if (value === null || value === undefined) return "unavailable";
  if (String(value).startsWith("-")) return "negative";
  return "positive";
}

function tableCell(value, className) {
  return element("td", className || (value === null || value === undefined ? "unavailable" : ""), display(value));
}

function renderMarkets(snapshot) {
  const rows = snapshot.markets.map((market) => {
    const row = document.createElement("tr");
    const venue = tableCell(market.venue, "venue-name");
    const instrumentSeen = tableCell(compactTime(market.instrument_observed_at));
    const funding = market.funding_rate === null
      ? tableCell(null)
      : tableCell(`${market.funding_rate} / ${market.funding_interval_hours}h`, numericClass(market.funding_rate));
    if (market.funding_rate !== null) funding.title = `Native rate ${market.funding_rate}`;
    const spread = market.spread_bps === null ? null : `${Number(market.spread_bps).toFixed(3)} bps`;
    row.append(
      venue,
      tableCell(`${market.asset} · ${market.symbol}`),
      instrumentSeen,
      funding,
      tableCell(compactTime(market.funding_effective_at)),
      tableCell(market.best_bid),
      tableCell(market.best_ask),
      tableCell(spread),
      tableCell(compactTime(market.book_observed_at)),
    );
    return row;
  });
  nodes.marketRows.replaceChildren(...rows);
}

function gateMetric(label, value) {
  const box = document.createElement("div");
  box.append(element("span", "", label), element("strong", "", value));
  return box;
}

function unavailableTableRow(message, columnCount) {
  const row = document.createElement("tr");
  const cell = tableCell(message, "unavailable summary-cell");
  cell.colSpan = columnCount;
  row.append(cell);
  return row;
}

function renderDiscovery(snapshot) {
  const discovery = snapshot.venue_discovery;
  if (!discovery) {
    const unavailable = statusCard(
      "Venue discovery",
      "Unavailable",
      "Unavailable at this snapshot cutoff",
      "missing",
    );
    nodes.discoverySummary.replaceChildren(unavailable);
    nodes.candidateRows.replaceChildren(unavailableTableRow("Unavailable at this snapshot cutoff", 9));
    nodes.dossierLeftHeading.textContent = "Left evidence";
    nodes.dossierRightHeading.textContent = "Right evidence";
    nodes.dossierRows.replaceChildren(unavailableTableRow("No selected candidate", 5));
    return;
  }

  const selectedReport = discovery.selected_dossier_id
    ? discovery.candidates.find((candidate) => candidate.dossier_id === discovery.selected_dossier_id)
    : null;
  const candidateRows = discovery.candidates.map((candidate, index) => {
    const selected = candidate.dossier_id === discovery.selected_dossier_id;
    const reason = selected ? discovery.selection_reason_code : candidate.primary_reason_code;
    const row = document.createElement("tr");
    if (selected) row.classList.add("candidate-selected");
    const status = tableCell(candidate.status);
    status.classList.add("judgment-cell");
    status.dataset.tone = statusTone(candidate.status);
    row.append(
      tableCell(selected ? `#${index + 1} · Selected` : `#${index + 1}`),
      tableCell(`${candidate.left_venue} → ${candidate.right_venue}`, "venue-name"),
      tableCell(candidate.assets.join(", ")),
      status,
      tableCell(candidate.counts.matched),
      tableCell(candidate.counts.model_required),
      tableCell(candidate.counts.blocking),
      tableCell(candidate.counts.missing_evidence),
      tableCell(reason, "summary-cell"),
    );
    return row;
  });
  nodes.candidateRows.replaceChildren(...candidateRows);

  if (!selectedReport) {
    nodes.discoverySummary.replaceChildren(
      statusCard(
        "Selection",
        "No advanceable candidate",
        discovery.selection_reason_code,
        "missing",
      ),
      statusCard("Activation", "Not authorized", "Research display only", "blocking"),
    );
    nodes.dossierLeftHeading.textContent = "Left evidence";
    nodes.dossierRightHeading.textContent = "Right evidence";
    nodes.dossierRows.replaceChildren(unavailableTableRow("No selected candidate", 5));
    return;
  }

  const pair = `${selectedReport.left_venue} → ${selectedReport.right_venue}`;
  nodes.discoverySummary.replaceChildren(
    statusCard(
      "Selected candidate",
      pair,
      `${selectedReport.assets.join(", ")} · ${selectedReport.dossier_id}`,
      selectedReport.status,
    ),
    statusCard(
      "Research status",
      selectedReport.status,
      `Observed ${compactTime(selectedReport.observed_at)}`,
      selectedReport.status,
    ),
    statusCard("Activation", "Not authorized", "Research result only", "blocking"),
    statusCard(
      "Next gate",
      "Public evidence + economic modeling",
      "No paper or live execution authority",
      "model_required",
    ),
  );

  nodes.dossierLeftHeading.textContent = `${selectedReport.left_venue} evidence`;
  nodes.dossierRightHeading.textContent = `${selectedReport.right_venue} evidence`;

  const rows = selectedReport.checks.map((check) => {
    const row = document.createElement("tr");
    const judgment = tableCell(check.judgment);
    judgment.classList.add("judgment-cell");
    judgment.dataset.tone = statusTone(check.judgment);
    row.append(
      tableCell(check.kind, "check-name"),
      judgment,
      tableCell(check.reason_code),
      tableCell(check.left_summary, "summary-cell"),
      tableCell(check.right_summary, "summary-cell"),
    );
    return row;
  });
  nodes.dossierRows.replaceChildren(...rows);
}

function renderCarry(snapshot) {
  const cards = snapshot.carry_rows.map((item) => {
    const card = element("article", "carry-card");
    const header = document.createElement("header");
    const pill = element("span", "status-pill", item.status);
    pill.dataset.tone = statusTone(item.status);
    header.append(element("h3", "", item.asset), pill);
    const metrics = element("div", "gate-metrics");
    metrics.append(
      gateMetric("Funding", item.funding_ready ? "Ready" : "Blocked"),
      gateMetric("Books", item.book_ready ? "Ready" : "Blocked"),
      gateMetric("Hourly spread", display(item.hourly_spread)),
    );
    const reasons = element("ul", "reason-list");
    const values = item.reason_codes.length ? item.reason_codes : ["No fail-closed reasons"];
    reasons.append(...values.map((reason) => element("li", "", reason)));
    card.append(header, metrics, reasons);
    return card;
  });
  nodes.carryRows.replaceChildren(...cards);
}

const countLabels = {
  raw_envelopes: "Raw envelopes",
  instrument_specs: "Instrument specs",
  funding_observations: "Funding observations",
  market_snapshots: "Market snapshots",
  book_snapshots: "Book snapshots",
  book_collection_cycles: "Book cycles",
  funding_collection_cycles: "Funding cycles",
};

function renderCounts(snapshot) {
  const cards = Object.entries(countLabels).map(([key, label]) => {
    const card = element("div", "count-card");
    card.append(element("dt", "", label), element("dd", "", display(snapshot.evidence_counts[key])));
    return card;
  });
  nodes.evidenceCounts.replaceChildren(...cards);
}

const recipeLabels = {
  collect_public: "Refresh public evidence",
  collect_books_once: "Capture one book cycle",
  collect_current_funding: "Capture current funding boundary",
  inspect_funding_health: "Inspect health in the CLI",
};

function renderRecipes(snapshot) {
  const cards = Object.entries(recipeLabels).map(([key, label]) => {
    const recipe = snapshot.operation_recipes[key];
    const card = element("article", "recipe-card");
    const copy = element("button", "copy-action", "Copy command");
    copy.type = "button";
    copy.setAttribute("aria-label", `Copy ${label.toLowerCase()}`);
    copy.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(recipe);
        copy.textContent = "Copied";
        window.setTimeout(() => { copy.textContent = "Copy command"; }, 1500);
      } catch (_error) {
        setStatus("Copy unavailable in this browser", "stale");
      }
    });
    const content = document.createElement("div");
    content.append(element("h3", "", label), element("code", "", recipe));
    card.append(content, copy);
    return card;
  });
  nodes.recipeList.replaceChildren(...cards);
}

function render(snapshot) {
  nodes.database.textContent = snapshot.database_name;
  nodes.snapshotTime.textContent = compactTime(snapshot.as_of);
  renderOverview(snapshot);
  renderMarkets(snapshot);
  renderDiscovery(snapshot);
  renderCarry(snapshot);
  renderCounts(snapshot);
  renderRecipes(snapshot);
}

function validateSnapshot(snapshot) {
  if (!snapshot || snapshot.schema_version !== 1 || !Array.isArray(snapshot.markets) || snapshot.markets.length !== 12) {
    throw new Error("INVALID_SNAPSHOT");
  }
  if (!snapshot.funding_health || !Array.isArray(snapshot.funding_health.boundaries)) {
    throw new Error("INVALID_SNAPSHOT");
  }
  if (!Object.prototype.hasOwnProperty.call(snapshot, "compatibility_dossier")) {
    throw new Error("INVALID_SNAPSHOT");
  }
  if (!Object.prototype.hasOwnProperty.call(snapshot, "venue_discovery")) {
    throw new Error("INVALID_SNAPSHOT");
  }
  if (snapshot.compatibility_dossier !== null && typeof snapshot.compatibility_dossier !== "object") {
    throw new Error("INVALID_SNAPSHOT");
  }
  if (snapshot.venue_discovery !== null && !Array.isArray(snapshot.venue_discovery.candidates)) {
    throw new Error("INVALID_SNAPSHOT");
  }
  return snapshot;
}

async function refreshSnapshot() {
  if (state.refreshing) return;
  state.refreshing = true;
  window.clearTimeout(state.timer);
  nodes.refresh.disabled = true;
  setStatus(state.lastSnapshot ? "Refreshing evidence…" : "Loading evidence…", "loading");
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), 10_000);
  try {
    const response = await fetch("/api/v1/dashboard", {
      headers: { Accept: "application/json" },
      signal: controller.signal,
      cache: "no-store",
    });
    const document = await response.json();
    if (!response.ok) throw new Error(document?.error?.code || "REFRESH_FAILED");
    const snapshot = validateSnapshot(document);
    state.lastSnapshot = snapshot;
    render(snapshot);
    setStatus(`Current · refreshed ${shortTime(snapshot.as_of)} UTC`, "current");
  } catch (error) {
    const code = error instanceof Error ? error.message : "REFRESH_FAILED";
    setStatus(state.lastSnapshot ? `Stale · ${code}` : `Unavailable · ${code}`, "stale");
  } finally {
    window.clearTimeout(timeout);
    state.refreshing = false;
    nodes.refresh.disabled = false;
    state.timer = window.setTimeout(refreshSnapshot, 15_000);
  }
}

nodes.refresh.addEventListener("click", refreshSnapshot);
refreshSnapshot();
