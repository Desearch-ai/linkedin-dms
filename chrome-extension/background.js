// Desearch LinkedIn DMs — Chrome Extension Background Service Worker
// Monitors li_at cookie changes and captures x-li-track / csrf-token headers.

const LINKEDIN_DOMAIN = "linkedin.com";
const VOYAGER_API_PATTERN = "https://www.linkedin.com/voyager/api/*";
const MESSAGING_GRAPHQL_PATH = "/voyagerMessagingGraphQL/graphql";

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

// Capture the live messaging GraphQL request contract (queryId + variables shape)
// from real LinkedIn browser traffic. Stores only metadata — never cookies or auth.
async function captureMessagingContract(url) {
  try {
    const parsed = new URL(url);
    const queryId = parsed.searchParams.get("queryId") || "";
    const variablesRaw = parsed.searchParams.get("variables") || "";

    if (!queryId) return;

    // Extract key names only — shape without runtime identifying values.
    const variablesShape = variablesRaw
      .replace(/^\(|\)$/g, "")
      .split(",")
      .filter(Boolean)
      .map((kv) => kv.split(":")[0].trim())
      .filter(Boolean);

    const current = await chrome.storage.local.get({ messagingContract: {} });
    const contract = { ...(current.messagingContract || {}) };

    if (queryId.startsWith("messengerConversations.")) {
      contract.conversationsQueryId = queryId;
      contract.conversationsVariablesShape = variablesShape;
    } else if (queryId.startsWith("messengerMessages.")) {
      contract.messagesQueryId = queryId;
      contract.messagesVariablesShape = variablesShape;
    } else {
      return;
    }

    contract.endpointPath = parsed.pathname;
    contract.capturedAt = new Date().toISOString();

    await chrome.storage.local.set({ messagingContract: contract });
  } catch (_) {
    // best-effort; never propagate
  }
}

async function getCapturedHeaders() {
  // Read latest captured browser headers so each backend call carries the
  // freshest fingerprint (see issue #54). Values are null until the header
  // capture listener observes a Voyager request.
  const { xLiTrack, csrfToken } = await chrome.storage.local.get({
    xLiTrack: null,
    csrfToken: null,
  });
  return { x_li_track: xLiTrack, csrf_token: csrfToken };
}

async function getCapturedMessagingContract() {
  const { messagingContract } = await chrome.storage.local.get({ messagingContract: null });
  return messagingContract;
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
  const captured = await getCapturedHeaders();
  const payload = {
    account_id: config.accountId,
    li_at: cookies.li_at,
    jsessionid: cookies.JSESSIONID || null,
    ...captured,
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
  const captured = await getCapturedHeaders();
  const payload = {
    label: "chrome-extension",
    li_at: cookies.li_at,
    jsessionid: cookies.JSESSIONID || null,
    ...captured,
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
    const url = details.url || "";
    const headers = details.requestHeaders || [];
    const track = headers.find((h) => (h.name || "").toLowerCase() === "x-li-track");
    const csrf = headers.find((h) => (h.name || "").toLowerCase() === "csrf-token");

    // Record live messaging GraphQL contract (queryId + variables shape) from real traffic.
    if (url.includes(MESSAGING_GRAPHQL_PATH)) {
      await captureMessagingContract(url);
    }

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

// ─── Extension-first inbox read (task #524) ─────────────────────────────────
// Manual Sync Now drives the LinkedIn read path from the browser, then POSTs
// normalized data to /sync/ingest. The legacy backend /sync remains as a
// fallback path but is no longer used for manual sync.

const VOYAGER_ME_URL = "https://www.linkedin.com/voyager/api/me";
const VOYAGER_BASE = "https://www.linkedin.com";
const CONTRACT_FRESHNESS_MS = 1000 * 60 * 60 * 24 * 7; // 7 days
const INGEST_MESSAGES_PER_THREAD = 20; // First-MVP first-page only.

function isContractFresh(contract) {
  if (!contract) return false;
  if (!contract.conversationsQueryId || !contract.messagesQueryId) return false;
  if (!contract.capturedAt) return true; // present but undated → accept
  const ts = Date.parse(contract.capturedAt);
  if (Number.isNaN(ts)) return true;
  return Date.now() - ts <= CONTRACT_FRESHNESS_MS;
}

function buildLinkedInHeaders(captured) {
  const headers = {
    "Accept": "application/graphql,application/vnd.linkedin.normalized+json+2.1",
    "x-restli-protocol-version": "2.0.0",
    "csrf-token": captured.csrf_token,
  };
  if (captured.x_li_track) headers["x-li-track"] = captured.x_li_track;
  return headers;
}

function extractProfileId(data) {
  if (!data || typeof data !== "object") return null;
  if (data.plainId) return String(data.plainId);
  if (data.entityUrn) return String(data.entityUrn);
  if (data.publicIdentifier) return String(data.publicIdentifier);
  const inner = data.data;
  if (inner && typeof inner === "object") {
    if (inner.plainId) return String(inner.plainId);
    if (inner["*miniProfile"]) return String(inner["*miniProfile"]);
    if (inner.entityUrn) return String(inner.entityUrn);
  }
  if (Array.isArray(data.included)) {
    for (const item of data.included) {
      if (item && typeof item === "object" && item.dashEntityUrn && String(item.dashEntityUrn).includes("fsd_profile")) {
        return String(item.dashEntityUrn);
      }
    }
  }
  return null;
}

function buildMailboxUrn(profileId) {
  const s = String(profileId);
  return s.includes("fsd_profile:") ? s : `urn:li:fsd_profile:${s}`;
}

async function fetchVoyagerMe(captured) {
  const resp = await fetch(VOYAGER_ME_URL, {
    method: "GET",
    headers: buildLinkedInHeaders(captured),
    credentials: "include",
  });
  if (!resp.ok) {
    throw new Error(`LinkedIn /voyager/api/me failed (${resp.status}). Refresh LinkedIn and retry.`);
  }
  const data = await resp.json();
  const pid = extractProfileId(data);
  if (!pid) {
    throw new Error("LinkedIn /voyager/api/me returned no profile id. Open LinkedIn and retry.");
  }
  return pid;
}

async function fetchConversationsPage(mailboxUrn, contract, captured) {
  const variables = `(mailboxUrn:${mailboxUrn})`;
  const path = contract.endpointPath || "/voyager/api/voyagerMessagingGraphQL/graphql";
  const url = `${VOYAGER_BASE}${path}?queryId=${encodeURIComponent(contract.conversationsQueryId)}&variables=${encodeURIComponent(variables)}`;
  const resp = await fetch(url, {
    method: "GET",
    headers: buildLinkedInHeaders(captured),
    credentials: "include",
  });
  if (resp.status === 429 || resp.status === 999) return { rateLimited: true, data: null };
  if (!resp.ok) throw new Error(`LinkedIn conversations request failed (${resp.status}).`);
  return { rateLimited: false, data: await resp.json() };
}

async function fetchMessagesPage(conversationUrn, contract, captured) {
  const variables = `(conversationUrn:${conversationUrn},count:${INGEST_MESSAGES_PER_THREAD})`;
  const path = contract.endpointPath || "/voyager/api/voyagerMessagingGraphQL/graphql";
  const url = `${VOYAGER_BASE}${path}?queryId=${encodeURIComponent(contract.messagesQueryId)}&variables=${encodeURIComponent(variables)}`;
  const resp = await fetch(url, {
    method: "GET",
    headers: buildLinkedInHeaders(captured),
    credentials: "include",
  });
  if (resp.status === 429 || resp.status === 999) return { rateLimited: true, data: null };
  if (!resp.ok) throw new Error(`LinkedIn messages request failed (${resp.status}).`);
  return { rateLimited: false, data: await resp.json() };
}

function parseConversations(data) {
  const out = [];
  if (!data || typeof data !== "object") return out;
  const inner = data.data || {};
  const conv = inner.messengerConversationsBySyncToken || inner.messengerConversations || {};
  const elements = Array.isArray(conv.elements) ? conv.elements : [];
  for (const elem of elements) {
    if (!elem || typeof elem !== "object") continue;
    const urn = elem.entityUrn || elem.conversationUrn || elem.backendConversationUrn;
    if (!urn) continue;
    let title = null;
    if (typeof elem.conversationName === "string" && elem.conversationName.trim()) {
      title = elem.conversationName.trim();
    } else {
      const names = [];
      const parts = Array.isArray(elem.conversationParticipants) ? elem.conversationParticipants : [];
      for (const p of parts) {
        const profile = (p && (p.participantProfile || p.profile)) || {};
        const first = profile.firstName || "";
        const last = profile.lastName || "";
        const full = `${first} ${last}`.trim();
        if (full) names.push(full);
      }
      title = names.length ? names.join(", ") : null;
    }
    out.push({ platform_thread_id: String(urn), title });
  }
  return out;
}

function parseMessages(data, myProfileId) {
  const out = [];
  if (!data || typeof data !== "object") return out;
  const inner = data.data || {};
  const msg = inner.messengerMessagesBySyncToken || inner.messengerMessages || {};
  const elements = Array.isArray(msg.elements) ? msg.elements : [];
  for (const event of elements) {
    if (!event || typeof event !== "object") continue;
    const id = event.entityUrn || event.backendUrn || event.dashEntityUrn;
    if (!id) continue;

    let text = null;
    const body = event.eventContent || event.body;
    if (body && typeof body === "object") {
      if (body.attributedBody && typeof body.attributedBody === "object") {
        text = body.attributedBody.text || null;
      }
      if (!text) text = body.text || body.body || null;
    } else if (typeof body === "string") {
      text = body;
    }

    let senderUrn = null;
    let senderName = null;
    const sender = event.sender || event.from;
    if (sender && typeof sender === "object") {
      const profile = sender.participantProfile || sender.profile || {};
      senderUrn = profile.entityUrn || profile.publicIdentifier || null;
      const first = profile.firstName || "";
      const last = profile.lastName || "";
      const full = `${first} ${last}`.trim();
      senderName = full || senderUrn || null;
    }

    let direction = "in";
    if (myProfileId && senderUrn) {
      const me = String(myProfileId);
      const su = String(senderUrn);
      if (su === me || su.endsWith(`:${me}`) || me.endsWith(`:${su}`)) {
        direction = "out";
      }
    }

    let sentAt = new Date().toISOString();
    const createdAt = event.createdAt ?? event.deliveredAt;
    if (typeof createdAt === "number") {
      sentAt = new Date(createdAt).toISOString();
    }

    out.push({
      platform_message_id: String(id),
      direction,
      sender: senderName,
      text,
      sent_at: sentAt,
    });
  }
  out.sort((a, b) => (a.sent_at < b.sent_at ? -1 : 1));
  return out;
}

async function handleManualSync() {
  const config = await getConfig();
  if (!config.accountId) {
    throw new Error("No account registered. Log in to LinkedIn first.");
  }

  const contract = await getCapturedMessagingContract();
  if (!contract || !contract.conversationsQueryId || !contract.messagesQueryId) {
    throw new Error(
      "Messaging contract not captured. Open https://www.linkedin.com/messaging/ in this browser to record the request shape, then retry sync."
    );
  }
  if (!isContractFresh(contract)) {
    throw new Error(
      "Messaging contract is stale. Open https://www.linkedin.com/messaging/ in this browser to refresh the captured contract, then retry sync."
    );
  }

  const captured = await getCapturedHeaders();
  if (!captured.csrf_token) {
    throw new Error(
      "csrf-token not yet captured from LinkedIn. Open https://www.linkedin.com/ in this browser, then retry sync."
    );
  }

  const profileId = await fetchVoyagerMe(captured);
  const mailboxUrn = buildMailboxUrn(profileId);

  let pagesFetched = 0;
  let rateLimited = false;

  const convResult = await fetchConversationsPage(mailboxUrn, contract, captured);
  if (convResult.rateLimited) {
    rateLimited = true;
  } else {
    pagesFetched += 1;
  }
  const threadsRaw = convResult.data ? parseConversations(convResult.data) : [];

  const ingestThreads = [];
  for (const t of threadsRaw) {
    let messages = [];
    if (!rateLimited) {
      const mr = await fetchMessagesPage(t.platform_thread_id, contract, captured);
      if (mr.rateLimited) {
        rateLimited = true;
      } else {
        pagesFetched += 1;
        messages = parseMessages(mr.data, profileId);
      }
    }
    ingestThreads.push({
      platform_thread_id: t.platform_thread_id,
      title: t.title,
      messages,
    });
  }

  const safeContract = {
    conversationsQueryId: contract.conversationsQueryId,
    messagesQueryId: contract.messagesQueryId,
    endpointPath: contract.endpointPath || null,
    capturedAt: contract.capturedAt || null,
  };

  const resp = await fetch(`${config.serviceUrl}/sync/ingest`, {
    method: "POST",
    headers: buildServiceHeaders(config),
    body: JSON.stringify({
      account_id: config.accountId,
      threads: ingestThreads,
      pages_fetched: pagesFetched,
      rate_limited: rateLimited,
      messaging_contract: safeContract,
    }),
  });

  if (!resp.ok) {
    const detail = await resp.text();
    throw new Error(`Ingest failed (${resp.status}): ${detail}`);
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
