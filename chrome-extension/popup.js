// Desearch LinkedIn DMs — Popup UI Logic

const statusDot = document.getElementById("statusDot");
const statusLabel = document.getElementById("statusLabel");
const backendStatusEl = document.getElementById("backendStatus");
const accountIdEl = document.getElementById("accountId");
const lastUpdatedEl = document.getElementById("lastUpdated");
const headersStatusEl = document.getElementById("headersStatus");
const trackStatusEl = document.getElementById("trackStatus");
const csrfStatusEl = document.getElementById("csrfStatus");
const contractStatusEl = document.getElementById("contractStatus");
const contractFreshnessEl = document.getElementById("contractFreshness");
const lastActionStatusEl = document.getElementById("lastActionStatus");
const nextActionEl = document.getElementById("nextAction");
const backendUrlInput = document.getElementById("backendUrl");
const apiTokenInput = document.getElementById("apiToken");
const resultEl = document.getElementById("result");
const btnSync = document.getElementById("btnSync");
const btnRefresh = document.getElementById("btnRefresh");
const btnSaveConfig = document.getElementById("btnSaveConfig");

let readyForSync = false;
let nextAction = "Prepare context before syncing.";
let busy = false;

// ─── Load state ──────────────────────────────────────────────────────────────

function formatTime(value) {
  if (!value) return "—";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleTimeString();
}

function shortQueryId(value) {
  if (!value) return "missing";
  return value.length > 26 ? `${value.slice(0, 23)}…` : value;
}

function updateButtonState() {
  btnSync.disabled = busy || !readyForSync;
  btnRefresh.disabled = busy;
  btnSaveConfig.disabled = busy;
  btnSync.title = readyForSync ? "" : nextAction;
}

async function loadState() {
  const state = await chrome.storage.local.get({
    serviceUrl: "http://localhost:8899",
    apiToken: "",
  });

  backendUrlInput.value = state.serviceUrl;
  apiTokenInput.value = state.apiToken;

  let operatorStatus = null;
  try {
    const resp = await chrome.runtime.sendMessage({ type: "OPERATOR_STATUS" });
    if (resp?.ok) operatorStatus = resp.data;
  } catch (_) {
    // Popup can still render config fields; background will surface real errors.
  }

  const last = operatorStatus?.last || {};
  const backend = operatorStatus?.backend || {};
  const account = operatorStatus?.account || {};
  const headers = operatorStatus?.headers || {};
  const contract = operatorStatus?.messagingContract || {};

  readyForSync = !!operatorStatus?.readyForSync;
  nextAction = operatorStatus?.nextAction || "Prepare context before syncing.";

  // Status indicator
  if (last.status === "connected") {
    statusDot.className = "status-dot dot-connected";
    statusLabel.textContent = "Connected";
  } else if (last.status === "error") {
    statusDot.className = "status-dot dot-error";
    statusLabel.textContent = last.error || "Error";
  } else {
    statusDot.className = "status-dot dot-unknown";
    statusLabel.textContent = "Not connected";
  }

  backendStatusEl.textContent = backend.needsStart ? "Start backend" : (backend.ready ? "Configured" : "Set URL");
  accountIdEl.textContent = account.accountId ?? "—";
  lastUpdatedEl.textContent = formatTime(last.updatedAt);

  const hasTrack = !!headers.xLiTrackCaptured;
  const hasCsrf = !!headers.csrfTokenCaptured;
  if (hasTrack && hasCsrf) {
    headersStatusEl.textContent = "x-li-track, csrf-token";
  } else if (hasTrack || hasCsrf) {
    headersStatusEl.textContent = hasTrack ? "x-li-track only" : "csrf-token only";
  } else {
    headersStatusEl.textContent = "—";
  }

  const headerTime = formatTime(headers.updatedAt);
  trackStatusEl.textContent = hasTrack ? `Captured ${headerTime}` : "Missing — open LinkedIn";
  csrfStatusEl.textContent = hasCsrf ? `Captured ${headerTime}` : "Missing — open LinkedIn";

  if (contract.ready) {
    contractStatusEl.textContent = `${shortQueryId(contract.conversationsQueryId)} / ${shortQueryId(contract.messagesQueryId)}`;
    contractFreshnessEl.textContent = contract.fresh
      ? `Fresh ${formatTime(contract.capturedAt)}`
      : "Stale — open Messaging";
  } else if (contract.conversationsQueryIdCaptured || contract.messagesQueryIdCaptured) {
    contractStatusEl.textContent = contract.conversationsQueryIdCaptured ? "Conversations only" : "Messages only";
    contractFreshnessEl.textContent = "Incomplete — open Messaging";
  } else {
    contractStatusEl.textContent = "Missing — open Messaging";
    contractFreshnessEl.textContent = "—";
  }

  const action = last.action ? `${last.action}: ` : "";
  lastActionStatusEl.textContent = last.status ? `${action}${last.status}` : "—";
  nextActionEl.textContent = nextAction;

  updateButtonState();
}

// ─── Actions ─────────────────────────────────────────────────────────────────

function showResult(text, isError = false) {
  resultEl.textContent = text;
  resultEl.className = isError ? "error-text" : "";
}

function setButtonsDisabled(disabled) {
  busy = disabled;
  updateButtonState();
}

btnSaveConfig.addEventListener("click", async () => {
  const url = backendUrlInput.value.trim().replace(/\/+$/, "");
  const apiToken = apiTokenInput.value.trim();
  if (!url) {
    showResult("Backend URL is required.", true);
    return;
  }
  await chrome.storage.local.set({ serviceUrl: url, apiToken });
  showResult("Config saved. Click Prepare Context to register/refresh browser context.");
  await loadState();
});

btnSync.addEventListener("click", async () => {
  if (!readyForSync) {
    showResult(nextAction, true);
    return;
  }

  setButtonsDisabled(true);
  showResult("Syncing...");
  try {
    const resp = await chrome.runtime.sendMessage({ type: "MANUAL_SYNC" });
    if (resp.ok) {
      const d = resp.data;
      const dupes = d.messages_skipped_duplicate ?? 0;
      const rate = d.rate_limited ? " (rate-limited)" : "";
      showResult(
        `Synced ${d.synced_threads} threads, ${d.messages_inserted} new, ${dupes} duplicates skipped${rate}.`,
      );
    } else {
      showResult(resp.error || "Sync failed. Check readiness above, then retry.", true);
    }
  } catch (err) {
    showResult(err.message, true);
  }
  setButtonsDisabled(false);
  await loadState();
});

btnRefresh.addEventListener("click", async () => {
  setButtonsDisabled(true);
  showResult("Preparing browser context...");
  try {
    const resp = await chrome.runtime.sendMessage({ type: "MANUAL_REFRESH" });
    if (resp.ok) {
      showResult("Context refreshed. If contract is missing/stale, open LinkedIn Messaging until conversations load, then retry Sync.");
    } else {
      showResult(resp.error || "Prepare Context failed. Start the backend or log in to LinkedIn, then retry.", true);
    }
  } catch (err) {
    showResult(err.message, true);
  }
  setButtonsDisabled(false);
  await loadState();
});

// ─── Init ────────────────────────────────────────────────────────────────────

loadState();
