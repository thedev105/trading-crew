"use strict";

const state = { lastSnapshot: null, refreshing: false, timer: null };
const databaseBusyRetryMs = [250, 500, 1000, 2000];

function el(id) {
  return document.getElementById(id);
}

function wait(ms) {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

function validateSnapshot(snapshot) {
  if (typeof snapshot !== "object" || snapshot === null) {
    throw new Error("INVALID_SNAPSHOT");
  }
  if (typeof snapshot.as_of !== "string" || !Array.isArray(snapshot.health?.venues)) {
    throw new Error("INVALID_SNAPSHOT");
  }
  if (!Array.isArray(snapshot.markets) || !Array.isArray(snapshot.recipes?.recipes)) {
    throw new Error("INVALID_SNAPSHOT");
  }
  if (
    typeof snapshot.candidates !== "object" ||
    snapshot.candidates === null ||
    !Array.isArray(snapshot.candidates.latest)
  ) {
    throw new Error("INVALID_SNAPSHOT");
  }
  if (
    typeof snapshot.proofs !== "object" ||
    snapshot.proofs === null ||
    !Array.isArray(snapshot.proofs.latest)
  ) {
    throw new Error("INVALID_SNAPSHOT");
  }
  if (
    typeof snapshot.scans !== "object" ||
    snapshot.scans === null ||
    !Array.isArray(snapshot.scans.latest)
  ) {
    throw new Error("INVALID_SNAPSHOT");
  }
  if (
    typeof snapshot.shadow !== "object" ||
    snapshot.shadow === null ||
    !Array.isArray(snapshot.shadow.latest)
  ) {
    throw new Error("INVALID_SNAPSHOT");
  }
  return snapshot;
}

async function fetchSnapshot(signal) {
  for (let attempt = 0; ; attempt += 1) {
    const response = await fetch("/api/v1/predictions-dashboard", {
      headers: { Accept: "application/json" },
      signal,
      cache: "no-store",
    });
    const document = await response.json();
    const code = document?.error?.code;
    if (response.ok) return validateSnapshot(document);
    if (code !== "DATABASE_BUSY" || attempt === databaseBusyRetryMs.length) {
      throw new Error(code || "REFRESH_FAILED");
    }
    await wait(databaseBusyRetryMs[attempt]);
  }
}

function render(snapshot) {
  el("as-of").textContent = `As of ${snapshot.as_of}`;
  el("stale-notice").hidden = true;

  const venuesBody = el("venues").querySelector("tbody");
  venuesBody.replaceChildren();
  for (const venue of snapshot.health.venues) {
    const row = document.createElement("tr");
    row.append(
      cell(venue.venue),
      cell(venue.status),
      cell(String(venue.collection_gate.allowed)),
      cell(String(venue.market_count)),
      cell(venue.latest_book_age_seconds ?? "none"),
      cell(venue.reason_codes.join(",") || "none"),
    );
    venuesBody.append(row);
  }

  const marketsBody = el("markets").querySelector("tbody");
  marketsBody.replaceChildren();
  for (const market of snapshot.markets) {
    const row = document.createElement("tr");
    row.append(
      cell(market.venue),
      cell(market.market_id),
      cell(market.question),
      cell(String(market.active)),
      cell(String(market.closed)),
      cell(market.retrieved_at),
    );
    marketsBody.append(row);
  }

  el("evidence-counts").textContent = JSON.stringify(snapshot.evidence_counts.counts, null, 2);

  const recipesList = el("recipes");
  recipesList.replaceChildren();
  for (const recipe of snapshot.recipes.recipes) {
    const item = document.createElement("li");
    item.textContent = recipe;
    recipesList.append(item);
  }

  renderCandidates(snapshot);
  renderProofs(snapshot);
  renderScans(snapshot);
  renderShadow(snapshot);
}

function renderCandidates(snapshot) {
  const summary = snapshot.candidates;
  el("candidates-summary").textContent =
    `Total: ${summary.total} | by disposition: ${JSON.stringify(summary.by_disposition)} | ` +
    `by relationship type: ${JSON.stringify(summary.by_relationship_type)} | ` +
    `by provenance: ${JSON.stringify(summary.by_provenance_kind)}`;

  const candidatesBody = el("candidates").querySelector("tbody");
  candidatesBody.replaceChildren();

  const hasCandidates = summary.latest.length > 0;
  el("candidates").hidden = !hasCandidates;
  el("candidates-empty").hidden = hasCandidates;

  for (const candidate of summary.latest) {
    const row = document.createElement("tr");
    const provenanceCell = document.createElement("td");
    provenanceCell.textContent = candidate.provenance_kind;
    if (candidate.provenance_kind === "ai") {
      const badge = document.createElement("span");
      badge.className = "candidate-badge";
      badge.textContent = "AI-nominated";
      provenanceCell.append(" ", badge);
    }
    row.append(
      cell(candidate.candidate_id),
      cell(candidate.relationship_type),
      cell(candidate.venues.join(", ")),
      cell(candidate.disposition),
      provenanceCell,
      cell(String(candidate.unresolved_field_count)),
      cell(candidate.observed_at),
    );
    candidatesBody.append(row);
  }
}

function renderProofs(snapshot) {
  const summary = snapshot.proofs;
  el("proofs-summary").textContent =
    `Total: ${summary.total} | by status: ${JSON.stringify(summary.by_status)} | ` +
    `by template: ${JSON.stringify(summary.by_template)}`;

  const proofsBody = el("proofs").querySelector("tbody");
  proofsBody.replaceChildren();

  const hasProofs = summary.latest.length > 0;
  el("proofs").hidden = !hasProofs;
  el("proofs-empty").hidden = hasProofs;

  for (const proof of summary.latest) {
    const row = document.createElement("tr");
    row.append(
      cell(proof.proof_id),
      cell(proof.candidate_id),
      cell(proof.template),
      cell(proof.status),
      cell(proof.rejection_reason ?? "none"),
      cell(proof.minimum_basket_payout ?? "none"),
      cell(proof.observed_at),
    );
    proofsBody.append(row);
  }
}

function renderScans(snapshot) {
  const summary = snapshot.scans;
  el("scans-summary").textContent =
    `Total: ${summary.total} | by decision: ${JSON.stringify(summary.by_decision)}`;

  const scansBody = el("scans").querySelector("tbody");
  scansBody.replaceChildren();

  const hasScans = summary.latest.length > 0;
  el("scans").hidden = !hasScans;
  el("scans-empty").hidden = hasScans;

  for (const scan of summary.latest) {
    const row = document.createElement("tr");
    const decisionCell = document.createElement("td");
    decisionCell.textContent = scan.decision;
    if (scan.decision === "SHADOW_CANDIDATE") {
      const caption = document.createElement("span");
      caption.className = "scan-caption";
      caption.textContent = "research decision — not an instruction to trade";
      decisionCell.append(" ", caption);
    }
    row.append(
      cell(scan.candidate_id),
      decisionCell,
      cell(scan.reason),
      cell(scan.surplus ?? "none"),
      cell(scan.capacity ?? "none"),
      cell(scan.as_of),
    );
    scansBody.append(row);
  }
}

function renderShadow(snapshot) {
  const summary = snapshot.shadow;
  el("shadow-summary").textContent =
    `Total: ${summary.proposals_total} | reconciled: ${summary.reconciled_count} | ` +
    `unreconciled: ${summary.unreconciled_count} | ` +
    `reconciled paper P&L (USD): ${summary.reconciled_paper_pnl_usd} | ` +
    `by current state: ${JSON.stringify(summary.by_terminal_state)} | ` +
    `experiments by family: ${JSON.stringify(summary.experiments_by_family)}`;

  const shadowBody = el("shadow-proposals").querySelector("tbody");
  shadowBody.replaceChildren();

  const hasShadowProposals = summary.latest.length > 0;
  el("shadow-proposals").hidden = !hasShadowProposals;
  el("shadow-empty").hidden = hasShadowProposals;

  for (const shadow of summary.latest) {
    const row = document.createElement("tr");
    const stateCell = cell(shadow.current_state);
    if (shadow.current_state === "unknown") {
      const warning = document.createElement("span");
      warning.className = "shadow-warning";
      warning.textContent = "awaiting reconciliation — paper result invalid";
      stateCell.append(" ", warning);
    }
    const isReconciled = shadow.current_state === "reconciled";
    const paperPnl =
      isReconciled && shadow.paper_pnl !== null
        ? shadow.paper_pnl
        : "not available";
    row.append(
      cell(shadow.proposal_id),
      cell(shadow.candidate_id),
      stateCell,
      cell(shadow.scenario_id ?? "not started"),
      cell(shadow.quantity),
      cell(paperPnl),
      cell(shadow.observed_at),
    );
    shadowBody.append(row);
  }
}

function cell(text) {
  const td = document.createElement("td");
  td.textContent = text;
  return td;
}

async function refreshSnapshot() {
  if (state.refreshing) return;
  state.refreshing = true;
  window.clearTimeout(state.timer);
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), 10_000);
  try {
    const snapshot = await fetchSnapshot(controller.signal);
    state.lastSnapshot = snapshot;
    render(snapshot);
  } catch (error) {
    if (state.lastSnapshot) {
      el("stale-notice").hidden = false;
    } else {
      el("as-of").textContent = `Unavailable: ${error instanceof Error ? error.message : "REFRESH_FAILED"}`;
    }
  } finally {
    window.clearTimeout(timeout);
    state.refreshing = false;
    state.timer = window.setTimeout(refreshSnapshot, 15_000);
  }
}

refreshSnapshot();
