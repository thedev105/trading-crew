// Rendering only: every node is created with the DOM API, never with innerHTML, so no server
// string can become markup in this page.
function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = String(text);
  return node;
}

function replaceChildren(node, children) {
  node.replaceChildren(...children);
}

function facts(entries) {
  const nodes = [];
  for (const [term, value] of entries) {
    nodes.push(element("dt", null, term));
    nodes.push(element("dd", null, value));
  }
  return nodes;
}

export function renderPosture(state) {
  const posture = document.getElementById("posture");
  if (!state.readiness) {
    posture.textContent = "Loading posture…";
    return;
  }
  const killed = state.readiness.kill_engaged ? "KILLED" : "ARMED";
  posture.textContent = `${killed} · ${state.readiness.protocol_version}`;
}

export function renderReadiness(state) {
  const target = document.getElementById("readiness-facts");
  const blockers = document.getElementById("blockers");
  if (!state.readiness) {
    replaceChildren(target, facts([["Status", "loading"]]));
    replaceChildren(blockers, []);
    return;
  }
  replaceChildren(
    target,
    facts([
      ["Kill state", state.readiness.kill_engaged ? "ENGAGED" : "CLEAR"],
      ["Presence", state.readiness.presence_state ?? "UNKNOWN"],
      ["Manifest", state.readiness.manifest_state ?? "MISSING"],
      ["Protocol", state.readiness.protocol_state ?? "UNKNOWN"],
      ["Secret store", state.readiness.secret_store_available ? "AVAILABLE" : "UNAVAILABLE"],
      ["Live authority", state.readiness.live_authority ? "GRANTED" : "NONE"],
    ]),
  );
  const codes = state.readiness.blockers ?? [];
  replaceChildren(
    blockers,
    codes.length === 0
      ? [element("li", null, "NONE")]
      : codes.map((code) => element("li", null, code)),
  );
}

export function renderLimits(state) {
  const rows = document.getElementById("limits-rows");
  const ceilings = state.policy?.ceilings ?? {};
  const requested = state.policy?.requested_limits ?? null;
  replaceChildren(
    rows,
    Object.keys(ceilings)
      .sort()
      .map((control) => {
        const row = element("tr");
        row.append(
          element("td", null, control),
          element("td", null, ceilings[control]),
          element("td", null, requested ? requested[control] : "—"),
        );
        return row;
      }),
  );
}

export function renderOpportunities(state) {
  const list = document.getElementById("opportunities");
  const items = state.opportunities ?? [];
  replaceChildren(
    list,
    items.map((item) => {
      const card = element("li");
      card.dataset.rank = String(item.rank ?? 0);
      card.dataset.disabled = String(Boolean(item.cross_venue));
      const heading = element("h3", null, `${item.proof_family} · rank ${item.rank}`);
      const detail = element("dl", "facts");
      replaceChildren(
        detail,
        facts([
          ["Proof", item.proof_id],
          ["Current surplus", item.current_surplus_usd],
          ["5s stressed surplus", item.stressed_surplus_usd],
          ["Capacity", item.capacity_usd],
          ["Incomplete exposure", item.incomplete_exposure_usd],
          ["Deployed capital", item.deployed_capital_usd],
          ["Recovery branches", item.recovery_branch_count],
          ["Tie-break", item.tie_break_field],
          ["Evidence age (s)", item.evidence_age_seconds],
        ]),
      );
      const select = element("button", null, item.cross_venue ? "Cross-venue (disabled)" : "Select");
      select.type = "button";
      select.disabled = Boolean(item.cross_venue);
      select.dataset.opportunityId = item.proof_id;
      card.append(heading, detail, select);
      return card;
    }),
  );
}

export function renderApproval(state) {
  const summary = document.getElementById("approval-summary");
  const text = document.getElementById("confirmation-text");
  const approve = document.getElementById("approve");
  const error = document.getElementById("confirmation-error");
  const selected = (state.opportunities ?? []).find(
    (item) => item.proof_id === state.selectedOpportunityId,
  );
  if (!selected) {
    replaceChildren(summary, facts([["Selection", "none"]]));
    text.textContent = "";
    approve.disabled = true;
    error.textContent = state.error ?? "";
    return;
  }
  replaceChildren(
    summary,
    facts([
      ["Proof", selected.proof_id],
      ["Family", selected.proof_family],
      ["Legs", selected.leg_summary ?? "FAK/FOK only"],
      ["Deployed capital", selected.deployed_capital_usd],
      ["Incomplete exposure", selected.incomplete_exposure_usd],
      ["Capability expiry", selected.authority_expires_at ?? "on approval"],
    ]),
  );
  text.textContent = state.confirmationText ?? "";
  approve.disabled = state.confirmationInput !== state.confirmationText;
  error.textContent = state.error ?? "";
}

export function renderLive(state) {
  const target = document.getElementById("live-facts");
  const legs = document.getElementById("legs");
  const heartbeat = document.getElementById("heartbeat");
  const session = state.session ?? null;
  replaceChildren(
    target,
    facts([
      ["Session", session?.active ? "ACTIVE" : "NONE"],
      ["Mode", session?.mode ?? "—"],
      ["Authority expires", session?.authority_expires_at ?? "—"],
      ["Strategies started", session?.strategies_started ?? 0],
      ["Deployed capital", session?.deployed_capital_usd ?? "0"],
      ["Session loss", session?.session_loss_usd ?? "UNKNOWN"],
      ["UTC-day loss", session?.utc_day_loss_usd ?? "UNKNOWN"],
      ["Loss status", session?.loss_status ?? "UNKNOWN"],
    ]),
  );
  const entries = session?.legs ?? [];
  replaceChildren(
    legs,
    entries.map((leg) => {
      const item = element(
        "li",
        null,
        `${leg.kind} leg ${leg.leg_index} ${leg.side} ${leg.size} @ ${leg.limit_price} ${leg.order_type} → ${leg.state}`,
      );
      item.dataset.kind = leg.kind;
      return item;
    }),
  );
  heartbeat.textContent = `presence ${session?.presence_state ?? "UNKNOWN"}`;
  heartbeat.dataset.state = session?.presence_state ?? "UNKNOWN";
}

export function renderEvidence(state) {
  const evidence = document.getElementById("evidence");
  const hashes = state.readiness?.evidence_hashes ?? [];
  evidence.textContent = hashes.length ? `evidence ${hashes.join(" ")}` : "evidence none";
}

export function renderView(state) {
  for (const section of document.querySelectorAll(".view")) {
    section.hidden = section.id !== state.view;
  }
  for (const tab of document.querySelectorAll(".tab")) {
    tab.setAttribute("aria-current", String(tab.dataset.target === state.view));
  }
  document.documentElement.dataset.view = state.view;
}
