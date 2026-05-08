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

const SECRET_VARIABLE_KEY_PATTERN = /(cookie|csrf|authorization|password|secret|li_at|jsessionid|(?:auth|access|refresh|bearer)[_-]?token)/i;
const SECRET_VARIABLE_VALUE_PATTERN = /(li_at=|JSESSIONID=|Authorization\s*[:=]|Bearer\s+)/i;
const TIMESTAMP_VARIABLE_KEY_PATTERN = /(createdBefore|createdAfter|deliveredBefore|deliveredAfter|beforeTime|afterTime|timestamp)$/i;

function stripOuterParens(value) {
  const s = String(value || "").trim();
  if (s.startsWith("(") && s.endsWith(")")) return s.slice(1, -1);
  return s;
}

function splitTopLevel(value, delimiter) {
  const out = [];
  let current = "";
  let depth = 0;
  let quote = null;
  const s = String(value || "");
  for (let i = 0; i < s.length; i += 1) {
    const ch = s[i];
    const prev = s[i - 1];
    if (quote) {
      current += ch;
      if (ch === quote && prev !== "\\") quote = null;
      continue;
    }
    if (ch === '"' || ch === "'") {
      quote = ch;
      current += ch;
      continue;
    }
    if (ch === "(" || ch === "[" || ch === "{") depth += 1;
    if (ch === ")" || ch === "]" || ch === "}") depth = Math.max(0, depth - 1);
    if (ch === delimiter && depth === 0) {
      if (current.trim()) out.push(current.trim());
      current = "";
    } else {
      current += ch;
    }
  }
  if (current.trim()) out.push(current.trim());
  return out;
}

function parseGraphQLVariables(variablesRaw) {
  return splitTopLevel(stripOuterParens(variablesRaw), ",")
    .map((part) => {
      const idx = part.indexOf(":");
      if (idx <= 0) return null;
      const key = part.slice(0, idx).trim();
      const rawValue = part.slice(idx + 1).trim();
      return key ? { key, rawValue } : null;
    })
    .filter(Boolean);
}

function normalizeGraphQLScalar(rawValue) {
  const s = String(rawValue ?? "").trim();
  if (/^-?\d+$/.test(s)) return Number(s);
  return s;
}

function isSecretVariable({ key, rawValue }) {
  return SECRET_VARIABLE_KEY_PATTERN.test(key) || SECRET_VARIABLE_VALUE_PATTERN.test(rawValue || "");
}

function buildVariableTemplate(variablesRaw, kind) {
  const pairs = parseGraphQLVariables(variablesRaw).filter((pair) => !isSecretVariable(pair));
  const template = pairs.map(({ key, rawValue }) => {
    if (key === "mailboxUrn") return { key, source: "mailboxUrn" };
    if (key === "conversationUrn") return { key, source: "conversationUrn" };
    if (key === "count") {
      return { key, source: "count", defaultValue: normalizeGraphQLScalar(rawValue) || 20 };
    }
    if (TIMESTAMP_VARIABLE_KEY_PATTERN.test(key)) return { key, source: "now" };
    return { key, value: normalizeGraphQLScalar(rawValue) };
  });
  const requiredDynamicKey = kind === "conversations" ? "mailboxUrn" : "conversationUrn";
  return template.some((entry) => entry.key === requiredDynamicKey) ? template : [];
}

// Capture the live messaging GraphQL request contract (queryId + variables template)
// from real LinkedIn browser traffic. Stores only safe metadata — never cookies or auth.
async function captureMessagingContract(url) {
  try {
    const parsed = new URL(url);
    const queryId = parsed.searchParams.get("queryId") || "";
    const variablesRaw = parsed.searchParams.get("variables") || "";

    if (!queryId) return;

    const current = await chrome.storage.local.get({ messagingContract: {} });
    const contract = { ...(current.messagingContract || {}) };

    if (queryId.startsWith("messengerConversations.")) {
      const variablesTemplate = buildVariableTemplate(variablesRaw, "conversations");
      contract.conversationsQueryId = queryId;
      contract.conversationsVariablesShape = variablesTemplate.map((v) => v.key);
      contract.conversationsVariablesTemplate = variablesTemplate;
    } else if (queryId.startsWith("messengerMessages.")) {
      const variablesTemplate = buildVariableTemplate(variablesRaw, "messages");
      contract.messagesQueryId = queryId;
      contract.messagesVariablesShape = variablesTemplate.map((v) => v.key);
      contract.messagesVariablesTemplate = variablesTemplate;
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

function redactOperatorText(value) {
  if (!value) return value;
  return String(value)
    .replace(/(li_at=)[^;\s]+/gi, "$1[redacted]")
    .replace(/(JSESSIONID=)[^;\s]+/gi, "$1[redacted]")
    .replace(/(csrf-token|csrf_token|csrf)[:=]\s*[^;\s,}]+/gi, "$1=[redacted]")
    .replace(/(Authorization[:=]\s*Bearer\s+)[^\s]+/gi, "$1[redacted]")
    .replace(/Bearer\s+[A-Za-z0-9._-]+/gi, "Bearer [redacted]");
}

async function setStatus(status, error = null, action = null) {
  await chrome.storage.local.set({
    lastStatus: status,
    lastError: error ? redactOperatorText(error) : null,
    lastAction: action,
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
        const error = redactOperatorText(err.message);
        console.error("[desearch] cookie change handler error:", error);
        await setStatus("error", error, "cookie");
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
  await setStatus("connected", null, "refresh");
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
  await setStatus("connected", null, "refresh");
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
  if (msg.type === "OPERATOR_STATUS") {
    buildOperatorStatus()
      .then((status) => sendResponse({ ok: true, data: status }))
      .catch((err) => sendResponse({ ok: false, error: redactOperatorText(err.message) }));
    return true;
  }

  if (msg.type === "MANUAL_SYNC") {
    handleManualSync()
      .then(async (result) => {
        await setStatus("connected", null, "sync");
        sendResponse({ ok: true, data: result });
      })
      .catch(async (err) => {
        const error = redactOperatorText(err.message);
        await setStatus("error", error, "sync");
        sendResponse({ ok: false, error });
      });
    return true; // keep channel open for async response
  }

  if (msg.type === "MANUAL_REFRESH") {
    handleManualRefresh()
      .then(() => sendResponse({ ok: true }))
      .catch(async (err) => {
        const error = redactOperatorText(err.message);
        await setStatus("error", error, "refresh");
        sendResponse({ ok: false, error });
      });
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
const INGEST_CONVERSATIONS_PER_PAGE = 20; // First-MVP first page only.
const INGEST_MESSAGES_PER_THREAD = 20; // First-MVP first-page only.

function isContractFresh(contract) {
  if (!contract) return false;
  if (!contract.conversationsQueryId || !contract.messagesQueryId) return false;
  if (!contract.capturedAt) return true; // present but undated → accept
  const ts = Date.parse(contract.capturedAt);
  if (Number.isNaN(ts)) return true;
  return Date.now() - ts <= CONTRACT_FRESHNESS_MS;
}

function hasVariableTemplateRequiredKeys(template, requiredKeys) {
  if (!Array.isArray(template) || template.length === 0) return false;
  return requiredKeys.every((key) => template.some((entry) => entry && entry.key === key));
}

function hasRequiredContract(contract) {
  return !!(
    contract &&
    contract.conversationsQueryId &&
    contract.messagesQueryId &&
    hasVariableTemplateRequiredKeys(contract.conversationsVariablesTemplate, ["mailboxUrn"]) &&
    hasVariableTemplateRequiredKeys(contract.messagesVariablesTemplate, ["conversationUrn"])
  );
}

function renderGraphQLVariableValue(entry, replacements) {
  if (entry.source === "mailboxUrn") return replacements.mailboxUrn;
  if (entry.source === "conversationUrn") return replacements.conversationUrn;
  if (entry.source === "count") return replacements.count ?? entry.defaultValue ?? entry.value;
  if (entry.source === "now") return Date.now();
  return entry.value;
}

function buildGraphQLVariables(template, replacements, label) {
  if (!Array.isArray(template) || template.length === 0) {
    throw new Error(
      `Captured ${label} messaging contract is missing reusable variables. Open LinkedIn Messaging to refresh the request contract, then retry Sync.`
    );
  }
  const parts = [];
  for (const entry of template) {
    if (!entry || !entry.key) continue;
    const value = renderGraphQLVariableValue(entry, replacements);
    if (value === undefined || value === null || value === "") {
      throw new Error(
        `Captured ${label} messaging contract is missing value for ${entry.key}. Open LinkedIn Messaging to refresh the request contract, then retry Sync.`
      );
    }
    parts.push(`${entry.key}:${String(value)}`);
  }
  return `(${parts.join(",")})`;
}

function buildOperatorNextAction({ backendReady, accountReady, hasTrack, hasCsrf, hasContract, contractFresh, lastError, serviceUrl }) {
  if (!backendReady) return "Set the backend Service URL, save config, then click Prepare Context.";
  if (!accountReady) return "Log in to LinkedIn in this browser, then click Prepare Context.";
  if (!hasCsrf && !hasTrack) return "Open LinkedIn in this browser to capture csrf-token and x-li-track, then click Prepare Context.";
  if (!hasCsrf) return "Open LinkedIn in this browser to capture csrf-token, then click Prepare Context.";
  if (!hasTrack) return "Open LinkedIn in this browser to capture x-li-track, then click Prepare Context.";
  if (!hasContract) return "Open LinkedIn Messaging until conversations load to capture the messaging contract, then click Prepare Context and retry Sync.";
  if (!contractFresh) return "Open LinkedIn Messaging to refresh the stale messaging contract, then click Prepare Context and retry Sync.";
  if (/(fetch failed|failed to fetch|networkerror|econnrefused|connection refused)/i.test(lastError || "")) {
    return `Start the backend at ${serviceUrl}, then click Prepare Context and retry Sync.`;
  }
  return "Ready for Sync Now.";
}

async function buildOperatorStatus() {
  const config = await getConfig();
  const captured = await getCapturedHeaders();
  const contract = await getCapturedMessagingContract();
  const meta = await chrome.storage.local.get({
    headersUpdatedAt: null,
    lastStatus: null,
    lastError: null,
    lastAction: null,
    lastUpdated: null,
  });

  const serviceUrl = (config.serviceUrl || "").trim();
  const backendReady = !!serviceUrl;
  const accountReady = config.accountId !== null && config.accountId !== undefined;
  const hasTrack = !!captured.x_li_track;
  const hasCsrf = !!captured.csrf_token;
  const hasContract = hasRequiredContract(contract);
  const contractFresh = hasContract && isContractFresh(contract);

  const missing = [];
  if (!backendReady) missing.push("backend config");
  if (!accountReady) missing.push("account id");
  if (!hasTrack) missing.push("x-li-track");
  if (!hasCsrf) missing.push("csrf-token");
  if (!hasContract) missing.push("messaging contract");
  else if (!contractFresh) missing.push("fresh messaging contract");

  const lastError = redactOperatorText(meta.lastError || null);
  const backendNeedsStart =
    meta.lastStatus === "error" &&
    backendReady &&
    /(fetch failed|failed to fetch|networkerror|econnrefused|connection refused)/i.test(lastError || "");
  if (backendNeedsStart) missing.push("backend reachable");

  const readyForSync = missing.length === 0;
  const nextAction = buildOperatorNextAction({
    backendReady,
    accountReady,
    hasTrack,
    hasCsrf,
    hasContract,
    contractFresh,
    lastError,
    serviceUrl,
  });

  return {
    readyForSync,
    missing,
    nextAction,
    backend: {
      ready: backendReady,
      serviceUrl,
      needsStart: backendNeedsStart,
    },
    account: {
      ready: accountReady,
      accountId: config.accountId ?? null,
    },
    headers: {
      xLiTrackCaptured: hasTrack,
      csrfTokenCaptured: hasCsrf,
      updatedAt: meta.headersUpdatedAt || null,
    },
    messagingContract: {
      ready: hasContract,
      fresh: contractFresh,
      conversationsQueryIdCaptured: !!contract?.conversationsQueryId,
      messagesQueryIdCaptured: !!contract?.messagesQueryId,
      conversationsQueryId: contract?.conversationsQueryId || null,
      messagesQueryId: contract?.messagesQueryId || null,
      endpointPath: contract?.endpointPath || null,
      capturedAt: contract?.capturedAt || null,
    },
    last: {
      action: meta.lastAction || null,
      status: meta.lastStatus || null,
      error: lastError,
      updatedAt: meta.lastUpdated || null,
    },
  };
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

  const candidates = [];
  const addCandidate = (value) => {
    if (value !== undefined && value !== null && String(value).trim()) candidates.push(String(value));
  };

  // For conversations, mailboxUrn must be the fsd_profile URN captured by LinkedIn
  // traffic. /voyager/api/me may also include a numeric plainId; prefer any
  // fsd_profile URN so no-count contracts replay with the exact mailbox family.
  addCandidate(data.entityUrn);
  const inner = data.data;
  if (inner && typeof inner === "object") {
    addCandidate(inner.entityUrn);
    addCandidate(inner["*miniProfile"]);
  }
  if (Array.isArray(data.included)) {
    for (const item of data.included) {
      if (item && typeof item === "object") addCandidate(item.dashEntityUrn);
    }
  }
  addCandidate(data.plainId);
  addCandidate(data.publicIdentifier);
  if (inner && typeof inner === "object") addCandidate(inner.plainId);

  return candidates.find((candidate) => candidate.includes("fsd_profile:")) || candidates[0] || null;
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
  const variables = buildGraphQLVariables(
    contract.conversationsVariablesTemplate,
    { mailboxUrn, count: INGEST_CONVERSATIONS_PER_PAGE },
    "conversations"
  );
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
  const variables = buildGraphQLVariables(
    contract.messagesVariablesTemplate,
    { conversationUrn, count: INGEST_MESSAGES_PER_THREAD },
    "messages"
  );
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
  if (!hasRequiredContract(contract)) {
    throw new Error(
      "Messaging contract variables not captured. Open https://www.linkedin.com/messaging/ in this browser to record the current request variables, then retry sync."
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
