const SNAPSHOT_PATH = "/api/v1/predictions-dashboard";
const DATABASE_BUSY_RETRY_MS = Object.freeze([250, 500, 1000]);

function defaultWait(delay, signal) {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(signal.reason ?? new DOMException("Aborted", "AbortError"));
      return;
    }
    const timer = globalThis.setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }, delay);
    function onAbort() {
      globalThis.clearTimeout(timer);
      reject(signal.reason ?? new DOMException("Aborted", "AbortError"));
    }
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

function responseCode(document) {
  const code = document?.error?.code;
  return typeof code === "string" && code.length > 0 ? code : "REFRESH_FAILED";
}

export async function fetchDashboardSnapshot({
  signal,
  fetchImpl = globalThis.fetch,
  wait = defaultWait,
} = {}) {
  if (typeof fetchImpl !== "function") {
    throw new Error("FETCH_UNAVAILABLE");
  }
  for (let attempt = 0; ; attempt += 1) {
    const response = await fetchImpl(SNAPSHOT_PATH, {
      method: "GET",
      headers: { Accept: "application/json" },
      cache: "no-store",
      signal,
    });
    let document;
    try {
      document = await response.json();
    } catch (_error) {
      throw new Error("INVALID_RESPONSE");
    }
    if (response.ok) {
      return document;
    }
    const code = responseCode(document);
    if (code !== "DATABASE_BUSY" || attempt >= DATABASE_BUSY_RETRY_MS.length) {
      throw new Error(code);
    }
    await wait(DATABASE_BUSY_RETRY_MS[attempt], signal);
  }
}
