// Desearch LinkedIn DMs — Popup UI Logic

const statusDot = document.getElementById("statusDot");
const statusLabel = document.getElementById("statusLabel");
const accountIdEl = document.getElementById("accountId");
const lastUpdatedEl = document.getElementById("lastUpdated");
const headersStatusEl = document.getElementById("headersStatus");
const backendUrlInput = document.getElementById("backendUrl");
const apiTokenInput = document.getElementById("apiToken");
const resultEl = document.getElementById("result");
const btnSync = document.getElementById("btnSync");
const btnRefresh = document.getElementById("btnRefresh");
const btnSaveConfig = document.getElementById("btnSaveConfig");

// ─── Load state ──────────────────────────────────────────────────────────────

async function loadState() {
  const state = await chrome.storage.local.get({
    serviceUrl: "http://localhost:8899",
    apiToken: "",
    accountId: null,
    lastStatus: null,
    lastError: null,
    lastUpdated: null,
    xLiTrack: null,
    csrfToken: null,
    headersUpdatedAt: null,
  });

  backendUrlInput.value = state.serviceUrl;
  apiTokenInput.value = state.apiToken;
  accountIdEl.textContent = state.accountId ?? "—";

  // Status indicator
  if (state.lastStatus === "connected") {
    statusDot.className = "status-dot dot-connected";
    statusLabel.textContent = "Connected";
  } else if (state.lastStatus === "error") {
    statusDot.className = "status-dot dot-error";
    statusLabel.textContent = state.lastError || "Error";
  } else {
    statusDot.className = "status-dot dot-unknown";
    statusLabel.textContent = "Not connected";
  }

  // Last updated
  if (state.lastUpdated) {
    const d = new Date(state.lastUpdated);
    lastUpdatedEl.textContent = d.toLocaleTimeString();
  } else {
    lastUpdatedEl.textContent = "—";
  }

  // Headers
  const hasTrack = !!state.xLiTrack;
  const hasCsrf = !!state.csrfToken;
  if (hasTrack && hasCsrf) {
    headersStatusEl.textContent = "x-li-track, csrf-token";
  } else if (hasTrack || hasCsrf) {
    headersStatusEl.textContent = hasTrack ? "x-li-track only" : "csrf-token only";
  } else {
    headersStatusEl.textContent = "—";
  }
}

// ─── Actions ─────────────────────────────────────────────────────────────────

function showResult(text, isError = false) {
  resultEl.textContent = text;
  resultEl.className = isError ? "error-text" : "";
}

function setButtonsDisabled(disabled) {
  btnSync.disabled = disabled;
  btnRefresh.disabled = disabled;
}

btnSaveConfig.addEventListener("click", async () => {
  const url = backendUrlInput.value.trim().replace(/\/+$/, "");
  const apiToken = apiTokenInput.value.trim();
  if (!url) {
    showResult("Backend URL is required.", true);
    return;
  }
  await chrome.storage.local.set({ serviceUrl: url, apiToken });
  showResult("Config saved.");
});

btnSync.addEventListener("click", async () => {
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
      showResult(resp.error || "Sync failed.", true);
    }
  } catch (err) {
    showResult(err.message, true);
  }
  setButtonsDisabled(false);
  await loadState();
});

btnRefresh.addEventListener("click", async () => {
  setButtonsDisabled(true);
  showResult("Refreshing cookies...");
  try {
    const resp = await chrome.runtime.sendMessage({ type: "MANUAL_REFRESH" });
    if (resp.ok) {
      showResult("Cookies refreshed successfully.");
    } else {
      showResult(resp.error || "Refresh failed.", true);
    }
  } catch (err) {
    showResult(err.message, true);
  }
  setButtonsDisabled(false);
  await loadState();
});

// ─── Init ────────────────────────────────────────────────────────────────────

loadState();
