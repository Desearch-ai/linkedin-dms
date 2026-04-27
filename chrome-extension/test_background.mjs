/**
 * Acceptance-criteria tests for background.js
 *
 * Mocks chrome.cookies, chrome.storage, chrome.webRequest, chrome.runtime
 * and global fetch to verify:
 *   AC1 – extension loads without error
 *   AC2 – cookie capture registers a new account via POST /accounts
 *   AC3 – cookie change on existing account triggers POST /accounts/refresh
 *   AC4 – header capture stores xLiTrack / csrfToken
 *   AC5 – MANUAL_SYNC message triggers POST /sync
 *   AC6 – MANUAL_REFRESH message triggers refresh or register
 */

import { readFileSync } from "fs";
import { Script, createContext } from "vm";

// ─── Helpers ────────────────────────────────────────────────────────────────

let passed = 0;
let failed = 0;

function assert(cond, label) {
  if (cond) {
    console.log(`  ✓ ${label}`);
    passed++;
  } else {
    console.log(`  ✗ ${label}`);
    failed++;
  }
}

// ─── Build mock chrome + fetch environment ──────────────────────────────────

function buildEnv() {
  const storage = {};
  const listeners = {
    cookieChanged: [],
    onSendHeaders: [],
    onMessage: [],
  };
  const fetchLog = []; // { url, options }

  const chrome = {
    cookies: {
      onChanged: {
        addListener: (fn) => listeners.cookieChanged.push(fn),
      },
      get: (query, cb) => {
        // Return a fake JSESSIONID when asked
        if (query.name === "JSESSIONID") {
          if (cb) cb({ value: '"fake-jsessionid-123"' });
          else return Promise.resolve({ value: '"fake-jsessionid-123"' });
        } else if (query.name === "li_at") {
          if (cb) cb({ value: "fake-li-at-token" });
          else return Promise.resolve({ value: "fake-li-at-token" });
        } else {
          if (cb) cb(null);
          else return Promise.resolve(null);
        }
      },
    },
    storage: {
      local: {
        get: (defaults) => {
          const result = {};
          for (const [k, v] of Object.entries(defaults)) {
            result[k] = storage[k] !== undefined ? storage[k] : v;
          }
          return Promise.resolve(result);
        },
        set: (obj) => {
          Object.assign(storage, obj);
          return Promise.resolve();
        },
      },
    },
    webRequest: {
      onSendHeaders: {
        addListener: (fn, filter, opts) => listeners.onSendHeaders.push({ fn, filter, opts }),
      },
    },
    runtime: {
      onMessage: {
        addListener: (fn) => listeners.onMessage.push(fn),
      },
      sendMessage: (msg) => {
        return new Promise((resolve) => {
          for (const fn of listeners.onMessage) {
            fn(msg, {}, resolve);
          }
        });
      },
    },
  };

  // Mock fetch
  const fakeFetch = (url, options) => {
    fetchLog.push({ url, options });
    // Return different responses based on URL
    if (url.includes("/accounts/refresh")) {
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ ok: true, account_id: 1 }),
        text: () => Promise.resolve("ok"),
      });
    }
    if (url.includes("/accounts")) {
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ account_id: 42 }),
        text: () => Promise.resolve("ok"),
      });
    }
    if (url.includes("/sync")) {
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ ok: true, synced_threads: 3, messages_inserted: 15, messages_skipped_duplicate: 0, pages_fetched: 3 }),
        text: () => Promise.resolve("ok"),
      });
    }
    return Promise.resolve({ ok: false, status: 404, text: () => Promise.resolve("not found") });
  };

  return { chrome, storage, listeners, fetchLog, fakeFetch };
}

function loadBackground(env) {
  const code = readFileSync("chrome-extension/background.js", "utf8");
  const ctx = createContext({
    chrome: env.chrome,
    fetch: env.fakeFetch,
    console,
    Promise,
    Date,
    JSON,
    Error,
    setTimeout,
    URL, // needed for URL parsing in captureMessagingContract
  });
  const script = new Script(code, { filename: "background.js" });
  script.runInContext(ctx);
  return ctx;
}

// ─── Tests ──────────────────────────────────────────────────────────────────

async function testAC1_loads() {
  console.log("\nAC1: Extension loads without error");
  try {
    const env = buildEnv();
    loadBackground(env);
    assert(true, "background.js loaded successfully");
    assert(env.listeners.cookieChanged.length === 1, "cookie listener registered");
    assert(env.listeners.onSendHeaders.length === 1, "header capture listener registered");
    assert(env.listeners.onMessage.length === 1, "message listener registered");
  } catch (e) {
    assert(false, `background.js failed to load: ${e.message}`);
  }
}

async function testAC2_newAccountRegistration() {
  console.log("\nAC2: Cookie capture registers new account (no accountId stored)");
  const env = buildEnv();
  // Seed previously-captured headers so registration forwards them (issue #54).
  env.storage.xLiTrack = '{"clientVersion":"1.13.42912"}';
  env.storage.csrfToken = "ajax:CSRF123";
  // No accountId in storage → should call POST /accounts
  loadBackground(env);

  const cookieListener = env.listeners.cookieChanged[0];
  // Simulate li_at cookie change
  await new Promise((resolve) => {
    cookieListener({
      cookie: { domain: ".linkedin.com", name: "li_at", value: "new-li-at-value" },
      removed: false,
    });
    setTimeout(resolve, 50);
  });

  const accountCall = env.fetchLog.find(f => f.url.includes("/accounts") && !f.url.includes("/refresh"));
  assert(!!accountCall, "POST /accounts was called");
  if (accountCall) {
    const body = JSON.parse(accountCall.options.body);
    assert(body.li_at === "new-li-at-value", "li_at value passed correctly");
    assert(body.jsessionid === "fake-jsessionid-123", "JSESSIONID passed (quotes stripped)");
    assert(body.label === "chrome-extension", "label is 'chrome-extension'");
    assert(body.x_li_track === '{"clientVersion":"1.13.42912"}', "x_li_track forwarded on registration");
    assert(body.csrf_token === "ajax:CSRF123", "csrf_token forwarded on registration");
  }
  assert(env.storage.accountId === 42, "accountId stored after registration");
  assert(env.storage.lastStatus === "connected", "status set to connected");
}

async function testAC3_cookieRefresh() {
  console.log("\nAC3: Cookie change triggers POST /accounts/refresh");
  const env = buildEnv();
  env.storage.accountId = 1; // Existing account
  env.storage.xLiTrack = "TRACK_FOR_REFRESH";
  env.storage.csrfToken = "CSRF_FOR_REFRESH";
  loadBackground(env);

  const cookieListener = env.listeners.cookieChanged[0];
  await new Promise((resolve) => {
    cookieListener({
      cookie: { domain: ".linkedin.com", name: "li_at", value: "refreshed-li-at" },
      removed: false,
    });
    setTimeout(resolve, 50);
  });

  const refreshCall = env.fetchLog.find(f => f.url.includes("/accounts/refresh"));
  assert(!!refreshCall, "POST /accounts/refresh was called");
  if (refreshCall) {
    const body = JSON.parse(refreshCall.options.body);
    assert(body.account_id === 1, "account_id passed correctly");
    assert(body.li_at === "refreshed-li-at", "updated li_at value passed");
    assert(body.jsessionid === "fake-jsessionid-123", "JSESSIONID included");
    assert(body.x_li_track === "TRACK_FOR_REFRESH", "x_li_track forwarded on refresh");
    assert(body.csrf_token === "CSRF_FOR_REFRESH", "csrf_token forwarded on refresh");
  }
  assert(env.storage.lastStatus === "connected", "status set to connected");
}

async function testAC3d_refreshWithoutCapturedHeaders() {
  console.log("\nAC3d: Refresh sends null x_li_track/csrf_token when nothing captured");
  const env = buildEnv();
  env.storage.accountId = 1;
  loadBackground(env);

  env.listeners.cookieChanged[0]({
    cookie: { domain: ".linkedin.com", name: "li_at", value: "x" },
    removed: false,
  });
  await new Promise((r) => setTimeout(r, 50));

  const refreshCall = env.fetchLog.find(f => f.url.includes("/accounts/refresh"));
  assert(!!refreshCall, "POST /accounts/refresh was called");
  if (refreshCall) {
    const body = JSON.parse(refreshCall.options.body);
    assert(body.x_li_track === null, "x_li_track is null when not captured");
    assert(body.csrf_token === null, "csrf_token is null when not captured");
  }
}

async function testAC3_ignoresRemovedCookie() {
  console.log("\nAC3b: Ignores removed cookies");
  const env = buildEnv();
  loadBackground(env);

  env.listeners.cookieChanged[0]({
    cookie: { domain: ".linkedin.com", name: "li_at", value: "x" },
    removed: true,
  });

  await new Promise((r) => setTimeout(r, 50));
  assert(env.fetchLog.length === 0, "no fetch call for removed cookie");
}

async function testAC3_ignoresNonLinkedIn() {
  console.log("\nAC3c: Ignores non-LinkedIn cookies");
  const env = buildEnv();
  loadBackground(env);

  env.listeners.cookieChanged[0]({
    cookie: { domain: ".google.com", name: "li_at", value: "x" },
    removed: false,
  });

  await new Promise((r) => setTimeout(r, 50));
  assert(env.fetchLog.length === 0, "no fetch call for non-LinkedIn cookie");
}

async function testAC4_headerCapture() {
  console.log("\nAC4: Header capture stores xLiTrack and csrfToken");
  const env = buildEnv();
  loadBackground(env);

  const headerListener = env.listeners.onSendHeaders[0];
  assert(headerListener.filter.urls[0] === "https://www.linkedin.com/voyager/api/*", "filter matches voyager API pattern");

  // Simulate a request with both headers
  await headerListener.fn({
    requestHeaders: [
      { name: "x-li-track", value: '{"clientVersion":"1.13.42912"}' },
      { name: "csrf-token", value: "ajax:abc123" },
      { name: "accept", value: "application/json" },
    ],
  });

  assert(env.storage.xLiTrack === '{"clientVersion":"1.13.42912"}', "xLiTrack stored");
  assert(env.storage.csrfToken === "ajax:abc123", "csrfToken stored");
  assert(!!env.storage.headersUpdatedAt, "headersUpdatedAt stored");

  // Simulate partial update: only one header present (case-insensitive name)
  await headerListener.fn({
    requestHeaders: [
      { name: "X-LI-TRACK", value: '{"clientVersion":"1.13.42913"}' },
    ],
  });

  assert(env.storage.xLiTrack === '{"clientVersion":"1.13.42913"}', "xLiTrack updates on partial capture");
  assert(env.storage.csrfToken === "ajax:abc123", "csrfToken preserved when not present in later request");
}

async function testAC5_manualSync() {
  console.log("\nAC5: MANUAL_SYNC triggers POST /sync");
  const env = buildEnv();
  env.storage.accountId = 1;
  env.storage.xLiTrack = "SYNC_TRACK";
  env.storage.csrfToken = "SYNC_CSRF";
  loadBackground(env);

  const resp = await env.chrome.runtime.sendMessage({ type: "MANUAL_SYNC" });
  assert(resp.ok === true, "sync response is ok");
  assert(resp.data.synced_threads === 3, "sync result contains synced_threads");
  assert(resp.data.messages_inserted === 15, "sync result contains messages_inserted");

  const syncCall = env.fetchLog.find(f => f.url.includes("/sync"));
  assert(!!syncCall, "POST /sync was called");
  if (syncCall) {
    const body = JSON.parse(syncCall.options.body);
    assert(body.account_id === 1, "account_id passed to sync");
    assert(body.x_li_track === "SYNC_TRACK", "x_li_track forwarded on manual sync");
    assert(body.csrf_token === "SYNC_CSRF", "csrf_token forwarded on manual sync");
  }
}

async function testAC5c_manualSyncWithoutCapturedHeaders() {
  console.log("\nAC5c: MANUAL_SYNC sends null when nothing captured yet");
  const env = buildEnv();
  env.storage.accountId = 1;
  loadBackground(env);

  await env.chrome.runtime.sendMessage({ type: "MANUAL_SYNC" });
  const syncCall = env.fetchLog.find(f => f.url.includes("/sync"));
  if (syncCall) {
    const body = JSON.parse(syncCall.options.body);
    assert(body.x_li_track === null, "x_li_track is null when not captured");
    assert(body.csrf_token === null, "csrf_token is null when not captured");
  }
}

async function testAC5b_manualSyncIncludesBearerToken() {
  console.log("\nAC5b: MANUAL_SYNC includes Authorization when apiToken is configured");
  const env = buildEnv();
  env.storage.accountId = 1;
  env.storage.apiToken = "local-api-token";
  loadBackground(env);

  const resp = await env.chrome.runtime.sendMessage({ type: "MANUAL_SYNC" });
  assert(resp.ok === true, "sync response is ok");

  const syncCall = env.fetchLog.find(f => f.url.includes("/sync"));
  assert(!!syncCall, "POST /sync was called");
  if (syncCall) {
    assert(syncCall.options.headers.Authorization === "Bearer local-api-token", "Authorization header included");
  }
}

async function testAC5c_manualSyncThreadsBrowserContext() {
  console.log("\nAC5c: MANUAL_SYNC threads captured x_li_track and csrf_token into payload");
  const env = buildEnv();
  env.storage.accountId = 1;
  env.storage.xLiTrack = '{"clientVersion":"1.13.42912"}';
  env.storage.csrfToken = "ajax:abc123";
  loadBackground(env);

  const resp = await env.chrome.runtime.sendMessage({ type: "MANUAL_SYNC" });
  assert(resp.ok === true, "sync response is ok");

  const syncCall = env.fetchLog.find(f => f.url.includes("/sync"));
  assert(!!syncCall, "POST /sync was called");
  if (syncCall) {
    const body = JSON.parse(syncCall.options.body);
    assert(body.account_id === 1, "account_id passed to sync");
    assert(body.x_li_track === '{"clientVersion":"1.13.42912"}', "x_li_track threaded into sync payload");
    assert(body.csrf_token === "ajax:abc123", "csrf_token threaded into sync payload");
  }
}

async function testAC6_manualRefresh() {
  console.log("\nAC6: MANUAL_REFRESH triggers cookie refresh");
  const env = buildEnv();
  env.storage.accountId = 1;
  loadBackground(env);

  const resp = await env.chrome.runtime.sendMessage({ type: "MANUAL_REFRESH" });
  assert(resp.ok === true, "refresh response is ok");

  const refreshCall = env.fetchLog.find(f => f.url.includes("/accounts/refresh"));
  assert(!!refreshCall, "POST /accounts/refresh was called");
}

async function testAC7_messagingContractConversations() {
  console.log("\nAC7: Captures conversations queryId from real messaging traffic");
  const env = buildEnv();
  loadBackground(env);

  const headerListener = env.listeners.onSendHeaders[0];
  const url =
    "https://www.linkedin.com/voyager/api/voyagerMessagingGraphQL/graphql" +
    "?queryId=messengerConversations.abc123def456&variables=(mailboxUrn:urn:li:fsd_profile:42,count:20)";

  await headerListener.fn({
    url,
    requestHeaders: [
      { name: "x-li-track", value: '{"clientVersion":"1.13.42912"}' },
      { name: "csrf-token", value: "ajax:abc123" },
    ],
  });

  const contract = env.storage.messagingContract;
  assert(!!contract, "messagingContract stored after conversations request");
  assert(
    contract.conversationsQueryId === "messengerConversations.abc123def456",
    "conversationsQueryId captured correctly"
  );
  assert(
    contract.endpointPath === "/voyager/api/voyagerMessagingGraphQL/graphql",
    "endpointPath captured"
  );
  assert(!!contract.capturedAt, "capturedAt recorded");
  assert(Array.isArray(contract.conversationsVariablesShape), "conversationsVariablesShape is an array");
  assert(contract.conversationsVariablesShape.includes("mailboxUrn"), "mailboxUrn key in shape");
  assert(contract.conversationsVariablesShape.includes("count"), "count key in shape");
}

async function testAC7b_messagingContractMessages() {
  console.log("\nAC7b: Captures messages queryId from real messaging traffic");
  const env = buildEnv();
  loadBackground(env);

  const headerListener = env.listeners.onSendHeaders[0];
  const url =
    "https://www.linkedin.com/voyager/api/voyagerMessagingGraphQL/graphql" +
    "?queryId=messengerMessages.def456abc789&variables=(conversationUrn:2-abc,count:50,createdBefore:1234567890)";

  await headerListener.fn({
    url,
    requestHeaders: [
      { name: "x-li-track", value: '{"clientVersion":"1.13.42912"}' },
    ],
  });

  const contract = env.storage.messagingContract;
  assert(!!contract, "messagingContract stored after messages request");
  assert(
    contract.messagesQueryId === "messengerMessages.def456abc789",
    "messagesQueryId captured correctly"
  );
  assert(Array.isArray(contract.messagesVariablesShape), "messagesVariablesShape is an array");
  assert(contract.messagesVariablesShape.includes("conversationUrn"), "conversationUrn key in shape");
  assert(contract.messagesVariablesShape.includes("count"), "count key in shape");
  assert(contract.messagesVariablesShape.includes("createdBefore"), "createdBefore key in shape");
}

async function testAC7c_messagingContractNoSecrets() {
  console.log("\nAC7c: Messaging contract does not store cookies or raw auth values");
  const env = buildEnv();
  loadBackground(env);

  const headerListener = env.listeners.onSendHeaders[0];
  const url =
    "https://www.linkedin.com/voyager/api/voyagerMessagingGraphQL/graphql" +
    "?queryId=messengerConversations.abc123&variables=(mailboxUrn:urn:li:fsd_profile:42)";

  await headerListener.fn({
    url,
    requestHeaders: [
      { name: "cookie", value: "li_at=super-secret-li-at-token; JSESSIONID=js123" },
      { name: "x-li-track", value: '{"clientVersion":"1.13.42912"}' },
      { name: "csrf-token", value: "ajax:csrf999" },
    ],
  });

  const contract = env.storage.messagingContract;
  assert(!!contract, "messagingContract stored");
  const contractStr = JSON.stringify(contract);
  assert(!contractStr.includes("super-secret-li-at-token"), "li_at cookie value not in stored contract");
  assert(!contractStr.includes("js123"), "JSESSIONID value not in stored contract");
  // The contract must not include any field named 'cookie'
  assert(!Object.prototype.hasOwnProperty.call(contract, "cookie"), "no cookie field on contract object");
}

async function testAC5d_manualSyncIncludesMessagingContract() {
  console.log("\nAC5d: MANUAL_SYNC includes messaging_contract when previously captured");
  const env = buildEnv();
  env.storage.accountId = 1;
  env.storage.messagingContract = {
    conversationsQueryId: "messengerConversations.live123",
    messagesQueryId: "messengerMessages.live456",
    endpointPath: "/voyager/api/voyagerMessagingGraphQL/graphql",
    capturedAt: "2026-04-27T10:00:00.000Z",
  };
  loadBackground(env);

  const resp = await env.chrome.runtime.sendMessage({ type: "MANUAL_SYNC" });
  assert(resp.ok === true, "sync response is ok");

  const syncCall = env.fetchLog.find((f) => f.url.includes("/sync"));
  assert(!!syncCall, "POST /sync was called");
  if (syncCall) {
    const body = JSON.parse(syncCall.options.body);
    assert(!!body.messaging_contract, "messaging_contract field present in sync payload");
    assert(
      body.messaging_contract.conversationsQueryId === "messengerConversations.live123",
      "live conversationsQueryId forwarded to sync"
    );
    assert(
      body.messaging_contract.messagesQueryId === "messengerMessages.live456",
      "live messagesQueryId forwarded to sync"
    );
  }
}

async function testAC5e_manualSyncNullContractWhenNotCaptured() {
  console.log("\nAC5e: MANUAL_SYNC sends messaging_contract: null when nothing captured");
  const env = buildEnv();
  env.storage.accountId = 1;
  loadBackground(env);

  await env.chrome.runtime.sendMessage({ type: "MANUAL_SYNC" });
  const syncCall = env.fetchLog.find((f) => f.url.includes("/sync"));
  if (syncCall) {
    const body = JSON.parse(syncCall.options.body);
    assert(body.messaging_contract === null, "messaging_contract is null when not captured");
  }
}

// ─── Run ────────────────────────────────────────────────────────────────────

async function main() {
  console.log("=== Chrome Extension Acceptance Criteria Tests ===");

  await testAC1_loads();
  await testAC2_newAccountRegistration();
  await testAC3_cookieRefresh();
  await testAC3d_refreshWithoutCapturedHeaders();
  await testAC3_ignoresRemovedCookie();
  await testAC3_ignoresNonLinkedIn();
  await testAC4_headerCapture();
  await testAC5_manualSync();
  await testAC5c_manualSyncWithoutCapturedHeaders();
  await testAC5b_manualSyncIncludesBearerToken();
  await testAC5c_manualSyncThreadsBrowserContext();
  await testAC6_manualRefresh();
  await testAC7_messagingContractConversations();
  await testAC7b_messagingContractMessages();
  await testAC7c_messagingContractNoSecrets();
  await testAC5d_manualSyncIncludesMessagingContract();
  await testAC5e_manualSyncNullContractWhenNotCaptured();

  console.log(`\n=== Results: ${passed} passed, ${failed} failed ===`);
  process.exit(failed > 0 ? 1 : 0);
}

main();
