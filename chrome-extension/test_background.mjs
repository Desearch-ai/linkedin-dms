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
  }
  assert(env.storage.accountId === 42, "accountId stored after registration");
  assert(env.storage.lastStatus === "connected", "status set to connected");
}

async function testAC3_cookieRefresh() {
  console.log("\nAC3: Cookie change triggers POST /accounts/refresh");
  const env = buildEnv();
  env.storage.accountId = 1; // Existing account
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
  }
  assert(env.storage.lastStatus === "connected", "status set to connected");
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

// ─── Run ────────────────────────────────────────────────────────────────────

async function main() {
  console.log("=== Chrome Extension Acceptance Criteria Tests ===");

  await testAC1_loads();
  await testAC2_newAccountRegistration();
  await testAC3_cookieRefresh();
  await testAC3_ignoresRemovedCookie();
  await testAC3_ignoresNonLinkedIn();
  await testAC4_headerCapture();
  await testAC5_manualSync();
  await testAC5b_manualSyncIncludesBearerToken();
  await testAC6_manualRefresh();

  console.log(`\n=== Results: ${passed} passed, ${failed} failed ===`);
  process.exit(failed > 0 ? 1 : 0);
}

main();
