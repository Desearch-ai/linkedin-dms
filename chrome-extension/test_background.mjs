/**
 * Acceptance-criteria tests for background.js
 *
 * Mocks chrome.cookies, chrome.storage, chrome.webRequest, chrome.runtime
 * and global fetch to verify:
 *   AC1 – extension loads without error
 *   AC2 – cookie capture registers a new account via POST /accounts
 *   AC3 – cookie change on existing account triggers POST /accounts/refresh
 *   AC4 – header capture stores xLiTrack / csrfToken
 *   AC5 – MANUAL_SYNC reads LinkedIn from the browser and POSTs /sync/ingest
 *   AC6 – MANUAL_REFRESH triggers refresh or register
 *   AC7 – messaging contract capture from real traffic
 *   AC8 – MANUAL_SYNC fails visibly without contract / csrf
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

const FRESH_CONTRACT = {
  conversationsQueryId: "messengerConversations.live123",
  messagesQueryId: "messengerMessages.live456",
  conversationsVariablesShape: ["mailboxUrn", "count"],
  messagesVariablesShape: ["conversationUrn", "count"],
  endpointPath: "/voyager/api/voyagerMessagingGraphQL/graphql",
  capturedAt: new Date().toISOString(),
};

// ─── Build mock chrome + fetch environment ──────────────────────────────────

function buildEnv({ linkedinResponses } = {}) {
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

  const lr = linkedinResponses || {};

  // Mock fetch — handles LinkedIn voyager + service URLs.
  const fakeFetch = (url, options) => {
    fetchLog.push({ url, options });
    const u = String(url);

    if (u.startsWith("https://www.linkedin.com/voyager/api/me")) {
      const me = lr.me === undefined ? { plainId: "42" } : lr.me;
      if (me === "__error__") {
        return Promise.resolve({
          ok: false,
          status: 401,
          json: () => Promise.resolve({}),
          text: () => Promise.resolve("expired"),
        });
      }
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve(me),
        text: () => Promise.resolve(JSON.stringify(me)),
      });
    }

    if (u.includes("/voyagerMessagingGraphQL/graphql")) {
      const isConv = u.includes("queryId=messengerConversations");
      const isMsg = u.includes("queryId=messengerMessages");
      let body;
      if (isConv) {
        body = lr.conversations === undefined
          ? {
              data: {
                messengerConversationsBySyncToken: {
                  elements: [
                    {
                      entityUrn: "urn:li:msg_conversation:1",
                      conversationName: null,
                      conversationParticipants: [
                        { participantProfile: { entityUrn: "urn:li:fsd_profile:99", firstName: "Alice", lastName: "Example" } },
                      ],
                    },
                    {
                      entityUrn: "urn:li:msg_conversation:2",
                      conversationName: "Group Chat",
                      conversationParticipants: [],
                    },
                  ],
                  metadata: {},
                },
              },
            }
          : lr.conversations;
      } else if (isMsg) {
        body = lr.messages === undefined
          ? {
              data: {
                messengerMessagesBySyncToken: {
                  elements: [
                    {
                      entityUrn: "urn:li:event:1",
                      sender: { participantProfile: { entityUrn: "urn:li:fsd_profile:99", firstName: "Alice", lastName: "Example" } },
                      eventContent: { attributedBody: { text: "Hi there" } },
                      createdAt: 1714200000000,
                    },
                    {
                      entityUrn: "urn:li:event:2",
                      sender: { participantProfile: { entityUrn: "urn:li:fsd_profile:42", firstName: "Me", lastName: "" } },
                      eventContent: { attributedBody: { text: "Hello" } },
                      createdAt: 1714200060000,
                    },
                  ],
                },
              },
            }
          : lr.messages;
      } else {
        body = {};
      }
      if (body && body.__error__) {
        return Promise.resolve({
          ok: false,
          status: body.__error__,
          json: () => Promise.resolve({}),
          text: () => Promise.resolve("err"),
        });
      }
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve(body),
        text: () => Promise.resolve(JSON.stringify(body)),
      });
    }

    if (u.includes("/sync/ingest")) {
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve({
          ok: true,
          synced_threads: 2,
          messages_inserted: 4,
          messages_skipped_duplicate: 0,
          pages_fetched: 3,
          rate_limited: false,
        }),
        text: () => Promise.resolve("ok"),
      });
    }

    if (u.includes("/accounts/refresh")) {
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve({ ok: true, account_id: 1 }),
        text: () => Promise.resolve("ok"),
      });
    }
    if (u.includes("/accounts")) {
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve({ account_id: 42 }),
        text: () => Promise.resolve("ok"),
      });
    }
    return Promise.resolve({ ok: false, status: 404, text: () => Promise.resolve("not found") });
  };

  return { chrome, storage, listeners, fetchLog, fakeFetch };
}

function loadBackground(env) {
  const code = readFileSync("chrome-extension/background.js", "utf8");
  const consoleCalls = [];
  const recordingConsole = {
    log: (...args) => { consoleCalls.push(["log", ...args]); console.log(...args); },
    info: (...args) => { consoleCalls.push(["info", ...args]); console.info(...args); },
    warn: (...args) => { consoleCalls.push(["warn", ...args]); console.warn(...args); },
    error: (...args) => { consoleCalls.push(["error", ...args]); console.error(...args); },
    debug: (...args) => { consoleCalls.push(["debug", ...args]); },
  };
  env.consoleCalls = consoleCalls;
  const ctx = createContext({
    chrome: env.chrome,
    fetch: env.fakeFetch,
    console: recordingConsole,
    Promise,
    Date,
    JSON,
    Error,
    setTimeout,
    URL,
    encodeURIComponent,
  });
  const script = new Script(code, { filename: "background.js" });
  script.runInContext(ctx);
  return ctx;
}

function findIngestCall(env) {
  return env.fetchLog.find((f) => f.url.includes("/sync/ingest"));
}

function findLegacySyncCall(env) {
  return env.fetchLog.find(
    (f) =>
      (f.url.endsWith("/sync") || f.url.match(/\/sync(\?|$)/)) &&
      !f.url.includes("/sync/ingest"),
  );
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
  env.storage.xLiTrack = '{"clientVersion":"1.13.42912"}';
  env.storage.csrfToken = "ajax:CSRF123";
  loadBackground(env);

  const cookieListener = env.listeners.cookieChanged[0];
  await new Promise((resolve) => {
    cookieListener({
      cookie: { domain: ".linkedin.com", name: "li_at", value: "new-li-at-value" },
      removed: false,
    });
    setTimeout(resolve, 50);
  });

  const accountCall = env.fetchLog.find(f => f.url.endsWith("/accounts"));
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
  env.storage.accountId = 1;
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

  await headerListener.fn({
    requestHeaders: [
      { name: "X-LI-TRACK", value: '{"clientVersion":"1.13.42913"}' },
    ],
  });

  assert(env.storage.xLiTrack === '{"clientVersion":"1.13.42913"}', "xLiTrack updates on partial capture");
  assert(env.storage.csrfToken === "ajax:abc123", "csrfToken preserved when not present in later request");
}

async function testAC5_manualSyncReadsLinkedInAndIngests() {
  console.log("\nAC5: MANUAL_SYNC reads LinkedIn from the browser and POSTs /sync/ingest");
  const env = buildEnv();
  env.storage.accountId = 1;
  env.storage.xLiTrack = "SYNC_TRACK";
  env.storage.csrfToken = "SYNC_CSRF";
  env.storage.messagingContract = FRESH_CONTRACT;
  loadBackground(env);

  const resp = await env.chrome.runtime.sendMessage({ type: "MANUAL_SYNC" });
  assert(resp.ok === true, `sync response is ok (got: ${JSON.stringify(resp)})`);
  if (resp.ok) {
    assert(resp.data.synced_threads === 2, "ingest result contains synced_threads");
    assert(resp.data.messages_inserted === 4, "ingest result contains messages_inserted");
    assert(resp.data.messages_skipped_duplicate === 0, "ingest result contains messages_skipped_duplicate");
  }

  const meCall = env.fetchLog.find(f => f.url.startsWith("https://www.linkedin.com/voyager/api/me"));
  assert(!!meCall, "extension fetched LinkedIn /voyager/api/me directly");

  const convCall = env.fetchLog.find(f => f.url.includes("queryId=messengerConversations"));
  assert(!!convCall, "extension fetched conversations from LinkedIn GraphQL");
  if (convCall) {
    assert(convCall.url.includes(FRESH_CONTRACT.conversationsQueryId), "conversations queryId from captured contract");
  }

  const msgCalls = env.fetchLog.filter(f => f.url.includes("queryId=messengerMessages"));
  assert(msgCalls.length >= 1, "extension fetched at least one messages page from LinkedIn GraphQL");
  if (msgCalls.length >= 1) {
    assert(msgCalls[0].url.includes(FRESH_CONTRACT.messagesQueryId), "messages queryId from captured contract");
  }

  const ingestCall = findIngestCall(env);
  assert(!!ingestCall, "POST /sync/ingest was called");
  const legacy = findLegacySyncCall(env);
  assert(!legacy, "legacy POST /sync was NOT called for manual Sync Now");

  if (ingestCall) {
    const body = JSON.parse(ingestCall.options.body);
    assert(body.account_id === 1, "account_id in ingest payload");
    assert(Array.isArray(body.threads), "threads array present in ingest payload");
    assert(body.threads.length === 2, "two threads from mocked LinkedIn response");
    const t = body.threads[0];
    assert(typeof t.platform_thread_id === "string" && t.platform_thread_id.length > 0, "thread platform_thread_id present");
    assert(Array.isArray(t.messages), "thread.messages array present");
    if (t.messages.length) {
      const m = t.messages[0];
      assert(typeof m.platform_message_id === "string", "message platform_message_id present");
      assert(m.direction === "in" || m.direction === "out", "message direction is 'in' or 'out'");
      assert(typeof m.sent_at === "string", "message sent_at is an ISO string");
    }
    assert(typeof body.pages_fetched === "number" && body.pages_fetched >= 1, "pages_fetched count present");
    assert(body.rate_limited === false, "rate_limited flag included");
    assert(!!body.messaging_contract, "messaging_contract metadata forwarded");
    assert(body.messaging_contract.conversationsQueryId === FRESH_CONTRACT.conversationsQueryId, "live conversationsQueryId forwarded");
  }
}

async function testAC5b_manualSyncIncludesBearerToken() {
  console.log("\nAC5b: MANUAL_SYNC includes Authorization on /sync/ingest when apiToken configured");
  const env = buildEnv();
  env.storage.accountId = 1;
  env.storage.apiToken = "local-api-token";
  env.storage.csrfToken = "SYNC_CSRF";
  env.storage.messagingContract = FRESH_CONTRACT;
  loadBackground(env);

  const resp = await env.chrome.runtime.sendMessage({ type: "MANUAL_SYNC" });
  assert(resp.ok === true, "sync response is ok");

  const ingestCall = findIngestCall(env);
  assert(!!ingestCall, "POST /sync/ingest was called");
  if (ingestCall) {
    assert(ingestCall.options.headers.Authorization === "Bearer local-api-token", "Authorization header included on ingest");
  }
}

async function testAC5c_extensionDirectionForMyMessages() {
  console.log("\nAC5c: messages from my profileId are normalized direction='out'");
  const env = buildEnv();
  env.storage.accountId = 1;
  env.storage.csrfToken = "SYNC_CSRF";
  env.storage.messagingContract = FRESH_CONTRACT;
  loadBackground(env);

  const resp = await env.chrome.runtime.sendMessage({ type: "MANUAL_SYNC" });
  assert(resp.ok === true, "sync response is ok");

  const ingestCall = findIngestCall(env);
  if (ingestCall) {
    const body = JSON.parse(ingestCall.options.body);
    const allMessages = body.threads.flatMap((t) => t.messages);
    const outMessages = allMessages.filter((m) => m.direction === "out");
    assert(outMessages.length >= 1, "at least one message normalized as direction='out' for my profile id");
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
  assert(!Object.prototype.hasOwnProperty.call(contract, "cookie"), "no cookie field on contract object");
}

async function testAC8_manualSyncFailsWithoutContract() {
  console.log("\nAC8: MANUAL_SYNC fails visibly when messaging contract is missing");
  const env = buildEnv();
  env.storage.accountId = 1;
  env.storage.csrfToken = "SYNC_CSRF";
  loadBackground(env);

  const resp = await env.chrome.runtime.sendMessage({ type: "MANUAL_SYNC" });
  assert(resp.ok === false, "sync response is not ok when contract missing");
  assert(/contract/i.test(resp.error || ""), "error message mentions contract");
  const ingestCall = findIngestCall(env);
  assert(!ingestCall, "POST /sync/ingest was NOT called when contract missing");
}

async function testAC8b_manualSyncFailsWithStaleContract() {
  console.log("\nAC8b: MANUAL_SYNC fails when contract is stale (older than freshness window)");
  const env = buildEnv();
  env.storage.accountId = 1;
  env.storage.csrfToken = "SYNC_CSRF";
  // ~30 days old
  env.storage.messagingContract = {
    ...FRESH_CONTRACT,
    capturedAt: new Date(Date.now() - 1000 * 60 * 60 * 24 * 30).toISOString(),
  };
  loadBackground(env);

  const resp = await env.chrome.runtime.sendMessage({ type: "MANUAL_SYNC" });
  assert(resp.ok === false, "sync response is not ok when contract is stale");
  assert(/(stale|contract|refresh)/i.test(resp.error || ""), "error message hints at staleness");
}

async function testAC8c_manualSyncFailsWithoutCsrf() {
  console.log("\nAC8c: MANUAL_SYNC fails visibly when csrf-token has not been captured");
  const env = buildEnv();
  env.storage.accountId = 1;
  env.storage.messagingContract = FRESH_CONTRACT;
  loadBackground(env);

  const resp = await env.chrome.runtime.sendMessage({ type: "MANUAL_SYNC" });
  assert(resp.ok === false, "sync response is not ok when csrf missing");
  assert(/csrf/i.test(resp.error || ""), "error message mentions csrf");
}

async function testAC8d_extensionNeverLogsCookiesOrCsrf() {
  console.log("\nAC8d: extension never logs cookie / csrf / li_at values during MANUAL_SYNC");
  const env = buildEnv();
  env.storage.accountId = 1;
  env.storage.csrfToken = "ajax:super-secret-csrf-DO-NOT-LEAK";
  env.storage.xLiTrack = '{"clientVersion":"1.13.42912"}';
  env.storage.messagingContract = FRESH_CONTRACT;
  loadBackground(env);

  await env.chrome.runtime.sendMessage({ type: "MANUAL_SYNC" });
  const allLogs = env.consoleCalls.map((c) => c.slice(1).map(String).join(" ")).join("\n");
  assert(!allLogs.includes("ajax:super-secret-csrf-DO-NOT-LEAK"), "csrf-token value not logged");
  assert(!allLogs.includes("fake-li-at-token"), "li_at value not logged");
  assert(!allLogs.toLowerCase().includes("cookie:"), "no 'cookie:' string emitted to console");
}

async function testAC9_csrfHeaderSentToLinkedIn() {
  console.log("\nAC9: extension forwards captured csrf-token on LinkedIn requests");
  const env = buildEnv();
  env.storage.accountId = 1;
  env.storage.csrfToken = "ajax:lnk-csrf";
  env.storage.xLiTrack = '{"clientVersion":"1.13.42912"}';
  env.storage.messagingContract = FRESH_CONTRACT;
  loadBackground(env);

  await env.chrome.runtime.sendMessage({ type: "MANUAL_SYNC" });
  const meCall = env.fetchLog.find(f => f.url.startsWith("https://www.linkedin.com/voyager/api/me"));
  if (meCall) {
    const headers = meCall.options.headers || {};
    // Headers may use any casing; normalize.
    const lower = Object.fromEntries(Object.entries(headers).map(([k, v]) => [k.toLowerCase(), v]));
    assert(lower["csrf-token"] === "ajax:lnk-csrf", "csrf-token header sent to LinkedIn /me");
  } else {
    assert(false, "/me call missing");
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
  await testAC5_manualSyncReadsLinkedInAndIngests();
  await testAC5b_manualSyncIncludesBearerToken();
  await testAC5c_extensionDirectionForMyMessages();
  await testAC6_manualRefresh();
  await testAC7_messagingContractConversations();
  await testAC7b_messagingContractMessages();
  await testAC7c_messagingContractNoSecrets();
  await testAC8_manualSyncFailsWithoutContract();
  await testAC8b_manualSyncFailsWithStaleContract();
  await testAC8c_manualSyncFailsWithoutCsrf();
  await testAC8d_extensionNeverLogsCookiesOrCsrf();
  await testAC9_csrfHeaderSentToLinkedIn();

  console.log(`\n=== Results: ${passed} passed, ${failed} failed ===`);
  process.exit(failed > 0 ? 1 : 0);
}

main();
