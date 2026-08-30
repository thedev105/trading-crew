// One in-memory snapshot per view. Nothing is written to localStorage or sessionStorage: a
// reload must re-derive its state from the server, never from a browser copy.
const listeners = new Set();

const state = {
  view: "readiness",
  readiness: null,
  policy: null,
  opportunities: [],
  session: null,
  selectedOpportunityId: null,
  confirmationText: "",
  challengeId: null,
  error: "",
};

export function getState() {
  return state;
}

export function subscribe(listener) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function update(patch) {
  Object.assign(state, patch);
  for (const listener of listeners) {
    listener(state);
  }
}
