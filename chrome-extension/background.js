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
const SECRET_HEADER_NAME_PATTERN = /^(cookie|authorization|proxy-authorization)$/i;
const SAFE_LINKEDIN_REPLAY_HEADER_NAMES = new Set([
  "x-li-lang",
  "x-li-page-instance",
  "x-li-deco-include-micro-schema",
  // Attempt #9 showed LinkedIn's accepted messaging GraphQL request included
  // these non-secret request-shape headers while replay omitted them. Keep the
  // allowlist narrow: no cookie/auth/user/session headers are ever captured.
  "content-type",
  "x-http-method-override",
]);
const GRAPHQL_REQUEST_SHAPE_HEADER_NAMES = new Set([
  "content-type",
  "x-http-method-override",
]);
const SAFE_LINKEDIN_REPLAY_METHODS = new Set(["GET", "POST"]);
const SAFE_METHOD_OVERRIDE_VALUES = new Set(["GET", "POST"]);
const SAFE_GRAPHQL_CONTENT_TYPE_PREFIXES = [
  "application/x-www-form-urlencoded",
];
const DEFAULT_SAFE_LINKEDIN_REPLAY_HEADERS = {
  "x-li-lang": "en_US",
  "x-li-page-instance": "urn:li:page:d_flagship3_messaging",
};

function normalizeHeaderName(name) {
  return String(name || "").trim().toLowerCase();
}

function canonicalLinkedInHeaderName(name) {
  const lower = normalizeHeaderName(name);
  if (lower === "content-type") return "Content-Type";
  if (lower === "x-http-method-override") return "x-http-method-override";
  if (lower === "x-li-lang") return "x-li-lang";
  if (lower === "x-li-page-instance") return "x-li-page-instance";
  if (lower === "x-li-deco-include-micro-schema") return "x-li-deco-include-micro-schema";
  return lower;
}

function normalizeSafeReplayHeaderValue(name, value) {
  const lower = normalizeHeaderName(name);
  const trimmed = String(value || "").trim();
  if (!trimmed || trimmed.length > 500) return null;
  if (SECRET_VARIABLE_VALUE_PATTERN.test(trimmed) || SECRET_VARIABLE_KEY_PATTERN.test(lower)) return null;

  if (lower === "x-http-method-override") {
    const method = trimmed.toUpperCase();
    return SAFE_METHOD_OVERRIDE_VALUES.has(method) ? method : null;
  }

  if (lower === "content-type") {
    const contentType = trimmed.toLowerCase();
    return SAFE_GRAPHQL_CONTENT_TYPE_PREFIXES.some((prefix) => contentType.startsWith(prefix)) ? trimmed : null;
  }

  return trimmed;
}

function normalizeSafeLinkedInReplayMethod(method) {
  const upper = String(method || "GET").trim().toUpperCase();
  return SAFE_LINKEDIN_REPLAY_METHODS.has(upper) ? upper : "GET";
}

function detectGraphQLQueryMode(parsedUrl) {
  return parsedUrl.searchParams.has("queryId") || parsedUrl.searchParams.has("variables") ? "url-query" : "unknown";
}

function pickGraphQLRequestShapeHeaders(requestHeaders) {
  const safeHeaders = buildSafeLinkedInReplayHeaders(requestHeaders);
  const out = {};
  for (const [name, value] of Object.entries(safeHeaders)) {
    const lower = normalizeHeaderName(name);
    if (GRAPHQL_REQUEST_SHAPE_HEADER_NAMES.has(lower)) out[canonicalLinkedInHeaderName(lower)] = value;
  }
  return out;
}

function buildSafeGraphQLReplayRequest(detailsOrUrl) {
  const details = typeof detailsOrUrl === "string" ? null : detailsOrUrl;
  const url = typeof detailsOrUrl === "string" ? detailsOrUrl : detailsOrUrl?.url || "";
  let queryMode = "unknown";
  let endpointPath = null;
  try {
    const parsed = new URL(url);
    queryMode = detectGraphQLQueryMode(parsed);
    endpointPath = parsed.pathname || null;
  } catch (_) {
    // best effort only
  }
  return {
    method: normalizeSafeLinkedInReplayMethod(details ? details.method : "GET"),
    queryMode,
    endpointPath,
    requestShapeHeaders: pickGraphQLRequestShapeHeaders(details?.requestHeaders || []),
  };
}

function redactHeaderNamesForDiagnostics(headers) {
  return [...new Set((headers || [])
    .map((h) => normalizeHeaderName(h && h.name))
    .filter((name) => name && !SECRET_HEADER_NAME_PATTERN.test(name)))]
    .sort();
}

function buildSafeLinkedInReplayHeaders(requestHeaders) {
  const out = {};
  for (const h of requestHeaders || []) {
    const name = normalizeHeaderName(h && h.name);
    if (!SAFE_LINKEDIN_REPLAY_HEADER_NAMES.has(name)) continue;
    const value = normalizeSafeReplayHeaderValue(name, h && h.value);
    if (!value) continue;
    out[canonicalLinkedInHeaderName(name)] = value;
  }
  return out;
}

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

function safeTemplateWrapper(value) {
  const s = String(value || "");
  if (!s || s.length > 40) return "";
  if (SECRET_VARIABLE_VALUE_PATTERN.test(s) || SECRET_VARIABLE_KEY_PATTERN.test(s)) return "";
  return s;
}

function findDynamicGraphQLValue(rawValue, source) {
  const s = String(rawValue ?? "").trim();
  const urnPattern =
    source === "mailboxUrn"
      ? /urn:li:fsd_profile:[^,)'"\\]+/
      : /urn:li:msg_conversation:[^,)'"\\]+/;
  const urnMatch = s.match(urnPattern);
  if (urnMatch && urnMatch.index !== undefined) {
    return { start: urnMatch.index, end: urnMatch.index + urnMatch[0].length };
  }

  const quoted = s.match(/^(['"])(.*)\1$/);
  if (quoted) return { start: 1, end: s.length - 1 };

  return null;
}

function buildDynamicVariableEntry(key, source, rawValue) {
  const entry = { key, source };
  const s = String(rawValue ?? "").trim();
  const dynamicValue = findDynamicGraphQLValue(s, source);
  if (!dynamicValue) return entry;

  const rawPrefix = safeTemplateWrapper(s.slice(0, dynamicValue.start));
  const rawSuffix = safeTemplateWrapper(s.slice(dynamicValue.end));
  if (rawPrefix) entry.rawPrefix = rawPrefix;
  if (rawSuffix) entry.rawSuffix = rawSuffix;
  return entry;
}

function buildVariableTemplate(variablesRaw, kind) {
  const pairs = parseGraphQLVariables(variablesRaw).filter((pair) => !isSecretVariable(pair));
  const template = pairs.map(({ key, rawValue }) => {
    if (key === "mailboxUrn") return buildDynamicVariableEntry(key, "mailboxUrn", rawValue);
    if (key === "conversationUrn") return buildDynamicVariableEntry(key, "conversationUrn", rawValue);
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
async function captureMessagingContract(detailsOrUrl) {
  try {
    const url = typeof detailsOrUrl === "string" ? detailsOrUrl : detailsOrUrl?.url || "";
    const parsed = new URL(url);
    const queryId = parsed.searchParams.get("queryId") || "";
    const variablesRaw = parsed.searchParams.get("variables") || "";

    if (!queryId) return;

    const current = await chrome.storage.local.get({ messagingContract: {} });
    const contract = { ...(current.messagingContract || {}) };

    const replayRequest = buildSafeGraphQLReplayRequest(detailsOrUrl);

    if (queryId.startsWith("messengerConversations.")) {
      const variablesTemplate = buildVariableTemplate(variablesRaw, "conversations");
      contract.conversationsQueryId = queryId;
      contract.conversationsVariablesShape = variablesTemplate.map((v) => v.key);
      contract.conversationsVariablesTemplate = variablesTemplate;
      contract.conversationsReplayRequest = replayRequest;
    } else if (queryId.startsWith("messengerMessages.")) {
      const variablesTemplate = buildVariableTemplate(variablesRaw, "messages");
      contract.messagesQueryId = queryId;
      contract.messagesVariablesShape = variablesTemplate.map((v) => v.key);
      contract.messagesVariablesTemplate = variablesTemplate;
      contract.messagesReplayRequest = replayRequest;
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
  const { xLiTrack, csrfToken, safeLinkedInReplayHeaders, browserLinkedInRequestHeaderNames } = await chrome.storage.local.get({
    xLiTrack: null,
    csrfToken: null,
    safeLinkedInReplayHeaders: {},
    browserLinkedInRequestHeaderNames: [],
  });

  return {
    x_li_track: xLiTrack,
    csrf_token: csrfToken,
    safe_replay_headers: safeLinkedInReplayHeaders || {},
    browser_header_names: Array.isArray(browserLinkedInRequestHeaderNames) ? browserLinkedInRequestHeaderNames : [],
  };
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
    x_li_track: captured.x_li_track,
    csrf_token: captured.csrf_token,
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
    x_li_track: captured.x_li_track,
    csrf_token: captured.csrf_token,
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
      await captureMessagingContract(details);
    }

    const safeReplayHeaders = buildSafeLinkedInReplayHeaders(headers);
    const browserHeaderNames = redactHeaderNamesForDiagnostics(headers);
    if (!track && !csrf && Object.keys(safeReplayHeaders).length === 0) return;

    // Preserve previously captured value when only one header is present.
    const current = await chrome.storage.local.get({
      xLiTrack: null,
      csrfToken: null,
      safeLinkedInReplayHeaders: {},
      browserLinkedInRequestHeaderNames: [],
    });
    const updates = {
      xLiTrack: track?.value ?? current.xLiTrack,
      csrfToken: csrf?.value ?? current.csrfToken,
      safeLinkedInReplayHeaders: {
        ...(current.safeLinkedInReplayHeaders || {}),
        ...safeReplayHeaders,
      },
      browserLinkedInRequestHeaderNames: browserHeaderNames.length
        ? browserHeaderNames
        : (current.browserLinkedInRequestHeaderNames || []),
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

function wrapDynamicGraphQLValue(entry, value) {
  return `${entry.rawPrefix || ""}${value}${entry.rawSuffix || ""}`;
}

function renderGraphQLVariableValue(entry, replacements) {
  if (entry.source === "mailboxUrn") return wrapDynamicGraphQLValue(entry, replacements.mailboxUrn);
  if (entry.source === "conversationUrn") return wrapDynamicGraphQLValue(entry, replacements.conversationUrn);
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

function truncateDiagnostic(value, maxLen = 220) {
  const s = String(value || "").replace(/\s+/g, " ").trim();
  return s.length > maxLen ? `${s.slice(0, maxLen)}…` : s;
}

function describeTemplateEntry(entry) {
  if (!entry || !entry.key) return null;
  const attrs = [];
  if (entry.source) attrs.push(`source=${entry.source}`);
  if (entry.rawPrefix || entry.rawSuffix) attrs.push("wrapped");
  if (Object.prototype.hasOwnProperty.call(entry, "value")) attrs.push("static");
  return attrs.length ? `${entry.key}{${attrs.join(",")}}` : entry.key;
}

function redactGraphQLVariablesForDiagnostics(variables) {
  const pairs = parseGraphQLVariables(variables);
  if (!pairs.length) return "(unparseable variables)";
  const parts = pairs.map(({ key, rawValue }) => {
    if (SECRET_VARIABLE_KEY_PATTERN.test(key) || SECRET_VARIABLE_VALUE_PATTERN.test(rawValue || "")) {
      return `${key}:[redacted]`;
    }
    if (key === "mailboxUrn") return `${key}:[runtime-mailboxUrn]`;
    if (key === "conversationUrn") return `${key}:[runtime-conversationUrn]`;
    if (TIMESTAMP_VARIABLE_KEY_PATTERN.test(key)) return `${key}:[runtime-timestamp]`;
    const value = truncateDiagnostic(redactOperatorText(rawValue), 80);
    return `${key}:${value || "[empty]"}`;
  });
  return `(${parts.join(",")})`;
}

function buildLinkedInGraphQLError(label, status, contract, variables, responseText, replayDiagnostics = "") {
  const queryId = label === "conversations" ? contract?.conversationsQueryId : contract?.messagesQueryId;
  const template = label === "conversations" ? contract?.conversationsVariablesTemplate : contract?.messagesVariablesTemplate;
  const variableKeys = Array.isArray(template) ? template.map((entry) => entry?.key).filter(Boolean).join(",") : "unknown";
  const templateShape = Array.isArray(template)
    ? template.map(describeTemplateEntry).filter(Boolean).join(",")
    : "unknown";
  const safeResponse = truncateDiagnostic(redactOperatorText(responseText || ""));
  const responsePart = safeResponse ? ` response="${safeResponse}";` : "";
  const replayPart = replayDiagnostics ? ` ${replayDiagnostics};` : "";
  return `LinkedIn ${label} request failed (${status}): queryId=${queryId || "unknown"}; variable keys/order=${variableKeys || "none"}; template=${templateShape || "none"}; rendered variables=${redactGraphQLVariablesForDiagnostics(variables)};${responsePart}${replayPart} refresh LinkedIn Messaging to recapture the live contract if this shape is no longer accepted.`;
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
      conversationsVariablesShape: Array.isArray(contract?.conversationsVariablesShape) ? contract.conversationsVariablesShape : [],
      messagesVariablesShape: Array.isArray(contract?.messagesVariablesShape) ? contract.messagesVariablesShape : [],
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

function buildLinkedInHeaders(captured = {}) {
  const headers = {
    "Accept": "application/graphql,application/vnd.linkedin.normalized+json+2.1",
    "x-restli-protocol-version": "2.0.0",
    ...DEFAULT_SAFE_LINKEDIN_REPLAY_HEADERS,
  };
  for (const [name, value] of Object.entries(captured.safe_replay_headers || {})) {
    const lower = normalizeHeaderName(name);
    if (!SAFE_LINKEDIN_REPLAY_HEADER_NAMES.has(lower) || !value) continue;
    if (GRAPHQL_REQUEST_SHAPE_HEADER_NAMES.has(lower)) continue;
    const safeValue = normalizeSafeReplayHeaderValue(lower, value);
    if (!safeValue) continue;
    headers[canonicalLinkedInHeaderName(lower)] = safeValue;
  }
  if (captured.csrf_token) headers["csrf-token"] = captured.csrf_token;
  if (captured.x_li_track) headers["x-li-track"] = captured.x_li_track;
  return headers;
}

function buildLinkedInReplayRequest(captured = {}, replayRequest = null) {
  const headers = buildLinkedInHeaders(captured);
  for (const [name, value] of Object.entries(replayRequest?.requestShapeHeaders || {})) {
    const lower = normalizeHeaderName(name);
    if (!GRAPHQL_REQUEST_SHAPE_HEADER_NAMES.has(lower)) continue;
    const safeValue = normalizeSafeReplayHeaderValue(lower, value);
    if (!safeValue) continue;
    headers[canonicalLinkedInHeaderName(lower)] = safeValue;
  }
  const method = replayRequest?.method
    ? normalizeSafeLinkedInReplayMethod(replayRequest.method)
    : "GET";
  return { method, headers };
}

function promiseChromeTabsQuery(query) {
  return new Promise((resolve) => {
    if (!chrome.tabs || !chrome.tabs.query) return resolve([]);
    chrome.tabs.query(query, (tabs) => resolve(Array.isArray(tabs) ? tabs : []));
  });
}

async function findLinkedInPageTab() {
  const tabs = await promiseChromeTabsQuery({ url: ["https://www.linkedin.com/*"] });
  return tabs.find((tab) => /https:\/\/www\.linkedin\.com\/messaging\//.test(tab.url || ""))
    || tabs.find((tab) => /https:\/\/www\.linkedin\.com\//.test(tab.url || ""))
    || null;
}

function linkedInReplayDiagnostics(captured, fetchContext, request = {}) {
  const browserNames = Array.isArray(captured.browser_header_names) ? captured.browser_header_names : [];
  const replayNames = Object.keys(request.headers || buildLinkedInHeaders(captured))
    .map(normalizeHeaderName)
    .sort();
  const method = normalizeSafeLinkedInReplayMethod(request.method || "GET");
  let urlPath = "unknown";
  let replayQueryMode = "unknown";
  try {
    const parsed = new URL(request.url || "");
    urlPath = parsed.pathname || "unknown";
    replayQueryMode = detectGraphQLQueryMode(parsed);
  } catch (_) {
    // best effort only
  }
  const capturedQueryMode = request.capturedQueryMode || "unknown";
  const queryMode = capturedQueryMode === "unknown" || capturedQueryMode === replayQueryMode
    ? replayQueryMode
    : `${capturedQueryMode}->${replayQueryMode}`;
  return `fetch context=${fetchContext}; credentials=include; replay method=${method}; url path=${urlPath}; query/body mode=${queryMode}; browser header names=${browserNames.join(",") || "unknown"}; replay header names=${replayNames.join(",") || "none"}`;
}

async function executeLinkedInPageFetch(tabId, url, request) {
  if (!chrome.scripting || !chrome.scripting.executeScript) {
    throw new Error("chrome.scripting is unavailable for LinkedIn page-context fetch");
  }
  const results = await chrome.scripting.executeScript({
    target: { tabId },
    world: "MAIN",
    args: [{ url, method: request.method, headers: request.headers }],
    func: async ({ url, method, headers }) => {
      try {
        const resp = await fetch(url, {
          method,
          headers,
          credentials: "include",
        });
        return {
          ok: resp.ok,
          status: resp.status,
          text: await resp.text(),
        };
      } catch (err) {
        return { ok: false, status: 0, text: "", error: err && err.message ? err.message : String(err) };
      }
    },
  });
  const result = results && results[0] && results[0].result;
  if (!result) throw new Error("LinkedIn page-context fetch returned no result");
  if (result.error) throw new Error(`LinkedIn page-context fetch failed: ${result.error}`);
  return { ...result, fetchContext: "linkedin-page-main-world" };
}

async function fetchLinkedInJson(url, captured, label, replayRequest = null) {
  const request = buildLinkedInReplayRequest(captured, replayRequest);
  request.url = url;
  request.capturedQueryMode = replayRequest?.queryMode || "unknown";
  const tab = await findLinkedInPageTab();
  let result;
  if (tab && tab.id !== undefined && tab.id !== null) {
    result = await executeLinkedInPageFetch(tab.id, url, request);
  } else {
    const resp = await fetch(url, {
      method: request.method,
      headers: request.headers,
      credentials: "include",
    });
    result = { ok: resp.ok, status: resp.status, text: await resp.text(), fetchContext: "extension-service-worker" };
  }

  let data = null;
  if (result.ok) {
    try {
      data = JSON.parse(result.text || "null");
    } catch (_) {
      throw new Error(`LinkedIn ${label} returned non-JSON response (${result.status}). Open LinkedIn Messaging and retry.`);
    }
  }
  return {
    ok: result.ok,
    status: result.status,
    text: result.text || "",
    data,
    diagnostics: linkedInReplayDiagnostics(captured, result.fetchContext, request),
  };
}

function extractProfileId(data) {
  if (!data || typeof data !== "object") return null;

  const candidates = [];
  const addCandidate = (value) => {
    if (value !== undefined && value !== null && String(value).trim()) candidates.push(String(value));
  };

  // For conversations, mailboxUrn must be the fsd_profile URN captured by LinkedIn
  // traffic. /voyager/api/me may also include a numeric plainId; prefer any
  // fsd_profile URN so no-count contracts replay with the exact mailbox family,
  // but preserve the old plainId-first fallback when only non-fsd identifiers exist.
  addCandidate(data.plainId);
  addCandidate(data.entityUrn);
  addCandidate(data.publicIdentifier);
  const inner = data.data;
  if (inner && typeof inner === "object") {
    addCandidate(inner.plainId);
    addCandidate(inner["*miniProfile"]);
    addCandidate(inner.entityUrn);
  }
  if (Array.isArray(data.included)) {
    for (const item of data.included) {
      if (item && typeof item === "object") addCandidate(item.dashEntityUrn);
    }
  }

  return candidates.find((candidate) => candidate.includes("fsd_profile:")) || candidates[0] || null;
}

function buildMailboxUrn(profileId) {
  const s = String(profileId);
  return s.includes("fsd_profile:") ? s : `urn:li:fsd_profile:${s}`;
}

async function fetchVoyagerMe(captured) {
  const resp = await fetchLinkedInJson(VOYAGER_ME_URL, captured, "/voyager/api/me");
  if (!resp.ok) {
    throw new Error(`LinkedIn /voyager/api/me failed (${resp.status}); ${resp.diagnostics}. Refresh LinkedIn and retry.`);
  }
  const pid = extractProfileId(resp.data);
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
  const resp = await fetchLinkedInJson(url, captured, "conversations", contract.conversationsReplayRequest || null);
  if (resp.status === 429 || resp.status === 999) return { rateLimited: true, data: null };
  if (!resp.ok) {
    throw new Error(buildLinkedInGraphQLError("conversations", resp.status, contract, variables, resp.text, resp.diagnostics));
  }
  return { rateLimited: false, data: resp.data };
}

async function fetchMessagesPage(conversationUrn, contract, captured) {
  const variables = buildGraphQLVariables(
    contract.messagesVariablesTemplate,
    { conversationUrn, count: INGEST_MESSAGES_PER_THREAD },
    "messages"
  );
  const path = contract.endpointPath || "/voyager/api/voyagerMessagingGraphQL/graphql";
  const url = `${VOYAGER_BASE}${path}?queryId=${encodeURIComponent(contract.messagesQueryId)}&variables=${encodeURIComponent(variables)}`;
  const resp = await fetchLinkedInJson(url, captured, "messages", contract.messagesReplayRequest || null);
  if (resp.status === 429 || resp.status === 999) return { rateLimited: true, data: null };
  if (!resp.ok) {
    throw new Error(buildLinkedInGraphQLError("messages", resp.status, contract, variables, resp.text, resp.diagnostics));
  }
  return { rateLimited: false, data: resp.data };
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
