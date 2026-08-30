// Wiring only. The browser sends stable identifiers, a typed confirmation, and a WebAuthn
// assertion; every price, size, route, and hash is resolved by the server.
import * as api from "/assets/api.js";
import { getState, subscribe, update } from "/assets/store.js";
import {
  renderApproval,
  renderEvidence,
  renderLimits,
  renderLive,
  renderOpportunities,
  renderPosture,
  renderReadiness,
  renderView,
} from "/assets/views.js";

const HEARTBEAT_MILLISECONDS = 2000;

function render(state) {
  renderView(state);
  renderPosture(state);
  renderReadiness(state);
  renderLimits(state);
  renderOpportunities(state);
  renderApproval(state);
  renderLive(state);
  renderEvidence(state);
}

async function refresh() {
  try {
    const [readiness, policy, opportunities, session] = await Promise.all([
      api.readiness(),
      api.policy(),
      api.opportunities(),
      api.liveSession(),
    ]);
    update({
      readiness,
      policy,
      opportunities: opportunities.opportunities ?? [],
      session: session.session,
      error: "",
    });
  } catch (error) {
    update({ error: error.message });
  }
}

async function approve(event) {
  event.preventDefault();
  const state = getState();
  try {
    const options = await api.authenticateOptions(state.selectedOpportunityId, state.mode);
    const assertion = await navigator.credentials.get({ publicKey: options.publicKey });
    await api.authorize(options.challenge_id, {
      id: assertion.id,
      type: assertion.type,
    });
  } catch (error) {
    update({ error: error.message });
  }
  // Never mark success optimistically: the next coherent snapshot decides what happened.
  await refresh();
}

function bind() {
  for (const tab of document.querySelectorAll(".tab")) {
    tab.addEventListener("click", () => update({ view: tab.dataset.target }));
  }
  document.getElementById("opportunities").addEventListener("click", (event) => {
    const id = event.target?.dataset?.opportunityId;
    if (id) {
      update({ selectedOpportunityId: id, view: "approval", error: "" });
    }
  });
  document.getElementById("confirmation").addEventListener("input", (event) => {
    update({ confirmationInput: event.target.value });
  });
  document.getElementById("approval-form").addEventListener("submit", approve);
  document.getElementById("stop").addEventListener("click", async () => {
    try {
      await api.stop();
    } catch (error) {
      update({ error: error.message });
    }
    await refresh();
  });
}

async function start() {
  subscribe(render);
  bind();
  await api.openSession();
  await refresh();
  window.setInterval(async () => {
    try {
      await api.heartbeat();
    } catch (error) {
      update({ error: error.message });
    }
  }, HEARTBEAT_MILLISECONDS);
}

start();
