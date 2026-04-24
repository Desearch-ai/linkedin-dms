// Desearch LinkedIn DMs — Chrome Extension Background Service Worker
// Monitors li_at cookie changes and captures x-li-track / csrf-token headers.

const LINKEDIN_DOMAIN = "linkedin.com";
const VOYAGER_API_PATTERN = "https://www.linkedin.com/voyager/api/*";

const SERVICE_URL_DEFAULT = "http://localhost:8899";

// ─── Helpers ─────────────────────────────────────────────────────────────────

async function getConfig() {
  const result = await chrome.storage.local.get({
    serviceUrl: SERVICE_URL_DEFAULT,
    apiToken: "",
    accountId: null,
  });
  return result;
}

function buildServiceHeaders(config) {
  const headers = { "Content-Type": "application/json" };
  const token = (config.apiToken || "").trim();
  if (token) {
    headers.Authorization = `Bearer ${token}`;
  }
  return headers;
}

async function setStatus(status, error = null) {
  await chrome.storage.local.set({
    lastStatus: status,
    lastError: error,
    lastUpdated: new Date().toISOString(),
  });
}

async function getLinkedInCookies() {
  const cookies = {};
  const liAt = await chrome.cookies.get({
    url: "https://www.linkedin.com",
    name: "li_at",
  });
  if (liAt) cookies.li_at = liAt.value;

  const jsessionid = await chrome.cookies.get({
    url: "https://www.linkedin.com",
    name: "JSESSIONID",
  });
  if (jsessionid) cookies.JSESSIONID = jsessionid.value.replace(/"/g, "");

  return cookies;
}

// ─── Cookie Monitoring ──────────────────────────────────────────────────────

chrome.cookies.onChanged.addListener(({ cookie, removed }) => {
  if (cookie.domain.includes("linkedin.com") && cookie.name === "li_at" && !removed) {
    // Get JSESSIONID too
    chrome.cookies.get({ url: "https://www.linkedin.com", name: "JSESSIONID" }, async (jsession) => {
      try {
        const config = await getConfig();
        const cookies = {
          li_at: cookie.value,
          JSESSIONID: jsession?.value?.replace(/"/g, "") || null,
        };

        if (config.accountId) {
          await pushRefresh(config, cookies);
        } else {
          await registerAccount(config, cookies);
        }
      } catch (err) {
        console.error("[desearch] cookie change handler error:", err);
        await setStatus("error", err.message);
      }
    });
  }
});

async function pushRefresh(config, cookies) {
  const payload = {
    account_id: config.accountId,
    li_at: cookies.li_at,
    jsessionid: cookies.JSESSIONID || null,
  };

  const resp = await fetch(`${config.serviceUrl}/accounts/refresh`, {
    method: "POST",
    headers: buildServiceHeaders(config),
    body: JSON.stringify(payload),
  });

  if (!resp.ok) {
    const detail = await resp.text();
    throw new Error(`Refresh failed (${resp.status}): ${detail}`);
  }

  console.log("[desearch] cookie refresh pushed successfully");
  await setStatus("connected");
}

async function registerAccount(config, cookies) {
  const payload = {
    label: "chrome-extension",
    li_at: cookies.li_at,
    jsessionid: cookies.JSESSIONID || null,
  };

  const resp = await fetch(`${config.serviceUrl}/accounts`, {
    method: "POST",
    headers: buildServiceHeaders(config),
    body: JSON.stringify(payload),
  });

  if (!resp.ok) {
    const detail = await resp.text();
    throw new Error(`Account registration failed (${resp.status}): ${detail}`);
  }

  const data = await resp.json();
  await chrome.storage.local.set({ accountId: data.account_id });
  console.log("[desearch] account registered:", data.account_id);
  await setStatus("connected");
}

// ─── Header Capture ─────────────────────────────────────────────────────────
// Intercept outgoing LinkedIn Voyager API requests to capture x-li-track and
// csrf-token header values from the real browser session.

chrome.webRequest.onSendHeaders.addListener(
  async (details) => {
    const headers = details.requestHeaders || [];
    const track = headers.find((h) => (h.name || "").toLowerCase() === "x-li-track");
    const csrf = headers.find((h) => (h.name || "").toLowerCase() === "csrf-token");

    if (!track && !csrf) return;

    // Preserve previously captured value when only one header is present.
    const current = await chrome.storage.local.get({ xLiTrack: null, csrfToken: null });
    const updates = {
      xLiTrack: track?.value ?? current.xLiTrack,
      csrfToken: csrf?.value ?? current.csrfToken,
      headersUpdatedAt: new Date().toISOString(),
    };

    // store for provider use
    chrome.storage.local.set(updates);
  },
  { urls: [VOYAGER_API_PATTERN] },
  ["requestHeaders"]
);

// ─── Message handling (from popup) ──────────────────────────────────────────

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg.type === "MANUAL_SYNC") {
    handleManualSync()
      .then((result) => sendResponse({ ok: true, data: result }))
      .catch((err) => sendResponse({ ok: false, error: err.message }));
    return true; // keep channel open for async response
  }

  if (msg.type === "MANUAL_REFRESH") {
    handleManualRefresh()
      .then(() => sendResponse({ ok: true }))
      .catch((err) => sendResponse({ ok: false, error: err.message }));
    return true;
  }
});

async function handleManualSync() {
  const config = await getConfig();
  if (!config.accountId) {
    throw new Error("No account registered. Log in to LinkedIn first.");
  }

  const captured = await chrome.storage.local.get({ xLiTrack: null, csrfToken: null });
  const payload = { account_id: config.accountId };
  if (captured.xLiTrack) payload.x_li_track = captured.xLiTrack;
  if (captured.csrfToken) payload.csrf_token = captured.csrfToken;

  const resp = await fetch(`${config.serviceUrl}/sync`, {
    method: "POST",
    headers: buildServiceHeaders(config),
    body: JSON.stringify(payload),
  });

  if (!resp.ok) {
    const detail = await resp.text();
    throw new Error(`Sync failed (${resp.status}): ${detail}`);
  }

  const data = await resp.json();
  await setStatus("connected");
  return data;
}

async function handleManualRefresh() {
  const config = await getConfig();
  const cookies = await getLinkedInCookies();

  if (!cookies.li_at) {
    throw new Error("Not logged in to LinkedIn — no li_at cookie found.");
  }

  if (config.accountId) {
    await pushRefresh(config, cookies);
  } else {
    await registerAccount(config, cookies);
  }
}
