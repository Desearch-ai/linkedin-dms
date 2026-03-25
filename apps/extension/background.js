/* ------------------------------------------------------------------ *
 *  LinkedIn DMs – Chrome Extension Service Worker (Manifest v3)       *
 *  Cookie watcher · Header interceptor · Sync trigger                 *
 * ------------------------------------------------------------------ */

const DEFAULT_SERVICE_URL = "http://localhost:8000";
const LI_DOMAIN = ".linkedin.com";
const VOYAGER_PATTERN = "https://www.linkedin.com/voyager/api/*";

// ── helpers ──────────────────────────────────────────────────────────

async function getState() {
  return chrome.storage.local.get([
    "serviceUrl",
    "accountId",
    "li_at",
    "jsessionid",
    "capturedHeaders",
    "lastSyncAt",
    "status",
  ]);
}

async function setState(patch) {
  return chrome.storage.local.set(patch);
}

function serviceUrl(state) {
  return (state.serviceUrl || DEFAULT_SERVICE_URL).replace(/\/+$/, "");
}

async function apiFetch(path, body) {
  const state = await getState();
  const url = `${serviceUrl(state)}${path}`;
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return res;
}

function setBadge(text, color) {
  chrome.action.setBadgeText({ text });
  chrome.action.setBadgeBackgroundColor({ color });
}

// ── 1. Cookie Watcher ────────────────────────────────────────────────

chrome.cookies.onChanged.addListener(async (changeInfo) => {
  const { cookie, removed, cause } = changeInfo;

  // Only care about li_at on linkedin.com
  if (cookie.name !== "li_at" || !cookie.domain.endsWith("linkedin.com")) {
    return;
  }

  // Cookie was deleted or cleared — mark expired
  if (removed && cause !== "overwrite") {
    await setState({ status: "expired" });
    setBadge("!", "#e74c3c");
    return;
  }

  // Cookie was set or refreshed
  const li_at = cookie.value;
  if (!li_at || li_at.length < 10) return;

  // Also grab JSESSIONID if available
  const jsessionCookie = await chrome.cookies.get({
    url: "https://www.linkedin.com",
    name: "JSESSIONID",
  });
  const jsessionid = jsessionCookie ? jsessionCookie.value.replace(/"/g, "") : null;

  await setState({ li_at, jsessionid });

  const state = await getState();

  if (state.accountId) {
    // Account exists — refresh credentials
    try {
      const res = await apiFetch("/accounts/refresh", {
        account_id: state.accountId,
        li_at,
        jsessionid,
      });
      if (res.ok) {
        await setState({ status: "connected" });
        setBadge("", "#2ecc71");
      } else {
        await setState({ status: "refresh_failed" });
        setBadge("!", "#e67e22");
      }
    } catch {
      await setState({ status: "api_unreachable" });
      setBadge("!", "#e74c3c");
    }
  } else {
    // No account yet — auto-register
    try {
      const res = await apiFetch("/accounts", {
        label: "chrome-extension",
        li_at,
        jsessionid,
      });
      if (res.ok) {
        const data = await res.json();
        await setState({ accountId: data.account_id, status: "connected" });
        setBadge("", "#2ecc71");
      } else {
        await setState({ status: "register_failed" });
        setBadge("!", "#e67e22");
      }
    } catch {
      await setState({ status: "api_unreachable" });
      setBadge("!", "#e74c3c");
    }
  }
});

// ── 2. Header Interceptor ────────────────────────────────────────────

const HEADERS_TO_CAPTURE = ["x-li-track", "csrf-token", "x-restli-protocol-version"];

chrome.webRequest.onSendHeaders.addListener(
  async (details) => {
    if (!details.requestHeaders) return;

    const captured = {};
    for (const h of details.requestHeaders) {
      if (HEADERS_TO_CAPTURE.includes(h.name.toLowerCase())) {
        captured[h.name.toLowerCase()] = h.value;
      }
    }

    if (Object.keys(captured).length > 0) {
      const state = await getState();
      const merged = { ...(state.capturedHeaders || {}), ...captured };
      await setState({ capturedHeaders: merged });
    }
  },
  { urls: [VOYAGER_PATTERN] },
  ["requestHeaders"]
);

// ── 3. Sync Trigger ──────────────────────────────────────────────────

async function triggerSync() {
  const state = await getState();
  if (!state.accountId) {
    return { ok: false, error: "No account registered" };
  }

  try {
    const res = await apiFetch("/sync", { account_id: state.accountId });
    if (res.ok) {
      const data = await res.json();
      await setState({ lastSyncAt: new Date().toISOString(), status: "connected" });
      setBadge("", "#2ecc71");
      return { ok: true, data };
    } else if (res.status === 401) {
      await setState({ status: "expired" });
      setBadge("!", "#e74c3c");
      return { ok: false, error: "Session expired — log into LinkedIn to refresh" };
    } else {
      const text = await res.text();
      return { ok: false, error: text };
    }
  } catch {
    await setState({ status: "api_unreachable" });
    setBadge("!", "#e74c3c");
    return { ok: false, error: "Cannot reach sync service" };
  }
}

// Listen for messages from popup
chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg.action === "sync") {
    triggerSync().then(sendResponse);
    return true; // keep channel open for async response
  }
  if (msg.action === "getState") {
    getState().then(sendResponse);
    return true;
  }
  if (msg.action === "updateServiceUrl") {
    setState({ serviceUrl: msg.url }).then(() => sendResponse({ ok: true }));
    return true;
  }
  if (msg.action === "checkHealth") {
    getState().then(async (state) => {
      try {
        const res = await fetch(`${serviceUrl(state)}/health`);
        const data = await res.json();
        sendResponse({ ok: data.ok === true });
      } catch {
        sendResponse({ ok: false });
      }
    });
    return true;
  }
  if (msg.action === "disconnect") {
    chrome.storage.local.clear().then(() => {
      setBadge("", "#000000");
      sendResponse({ ok: true });
    });
    return true;
  }
});

// ── Init: check existing cookie on install/startup ───────────────────

chrome.runtime.onInstalled.addListener(onStartup);
chrome.runtime.onStartup.addListener(onStartup);

async function onStartup() {
  const cookie = await chrome.cookies.get({
    url: "https://www.linkedin.com",
    name: "li_at",
  });
  if (cookie && cookie.value && cookie.value.length >= 10) {
    // Simulate a cookie change event to trigger registration/refresh
    chrome.cookies.onChanged.dispatch
      ? chrome.cookies.onChanged.dispatch({
          cookie,
          removed: false,
          cause: "explicit",
        })
      : null;
    // Fallback: just save the cookie value
    await setState({ li_at: cookie.value });
  }
}
