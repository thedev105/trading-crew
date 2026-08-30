// The only network surface this console has: same-origin pilot routes, one CSRF token, no
// third-party host, and no place to construct an order body.
const BASE = "/api/v1/pilot";

let csrfToken = null;

async function request(method, path, body) {
  const headers = { Accept: "application/json" };
  if (method === "POST") {
    headers["Content-Type"] = "application/json";
    if (csrfToken !== null) {
      headers["X-Pilot-CSRF"] = csrfToken;
    }
  }
  const response = await fetch(`${BASE}${path}`, {
    method,
    headers,
    credentials: "same-origin",
    redirect: "error",
    cache: "no-store",
    body: method === "POST" ? JSON.stringify(body ?? {}) : undefined,
  });
  const payload = await response.json().catch(() => ({ error: "PILOT_RESPONSE_INVALID" }));
  if (!response.ok) {
    throw new Error(payload.error ?? "PILOT_REQUEST_FAILED");
  }
  return payload;
}

export async function openSession() {
  const payload = await request("POST", "/session");
  csrfToken = payload.csrf_token;
  return payload;
}

export const readiness = () => request("GET", "/readiness");
export const policy = () => request("GET", "/policy");
export const opportunities = () => request("GET", "/opportunities");
export const liveSession = () => request("GET", "/live-session");
export const audit = () => request("GET", "/audit");
export const heartbeat = () => request("POST", "/presence", { kind: "HEARTBEAT" });
export const stop = () => request("POST", "/stop", { kind: "OPERATOR_STOP" });
export const authenticateOptions = (opportunityId, mode) =>
  request("POST", "/passkeys/authenticate/options", { opportunity_id: opportunityId, mode });
export const authorize = (challengeId, assertion) =>
  request("POST", "/authorizations", { challenge_id: challengeId, assertion });
