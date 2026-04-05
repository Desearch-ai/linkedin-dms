// Desearch LinkedIn DMs — Popup UI Logic

const statusBadge = document.getElementById("statusBadge");
const accountIdEl = document.getElementById("accountId");
const cookieStatusEl = document.getElementById("cookieStatus");
const headersStatusEl = document.getElementById("headersStatus");
const lastUpdatedEl = document.getElementById("lastUpdated");
const backendUrlInput = document.getElementById("backendUrl");
const healthStatusEl = document.getElementById("healthStatus");
const resultEl = document.getElementById("result");
const btnSync = document.getElementById("btnSync");
const btnRefresh = document.getElementById("btnRefresh");
const btnSaveConfig = document.getElementById("btnSaveConfig");
const btnDisconnect = document.getElementById("btnDisconnect");

// ─── Load state ──────────────────────────────────────────────────────────────

async function loadState() {
  const state = await chrome.storage.local.get({
    serviceUrl: "http://localhost:8899",
    accountId: null,
    lastStatus: null,
    lastError: null,
    lastUpdated: null,
    xLiTrack: null,
    csrfToken: null,
    liAtValue: null,
  });

  backendUrlInput.value = state.serviceUrl;
  accountIdEl.textContent = state.accountId ?? "—";

  // Status badge
  if (state.lastStatus === "connected") {
    statusBadge.textContent = "Connected";
    statusBadge.className = "status-badge status-connected";
  } else if (state.lastStatus === "error") {
    statusBadge.textContent = state.lastError || "Error";
    statusBadge.className = "status-badge status-error";
  } else {
    statusBadge.textContent = "Disconnected";
    statusBadge.className = "status-badge status-disconnected";
  }

  // Cookie preview
  if (state.liAtValue) {
    cookieStatusEl.textContent = "..." + state.liAtValue.slice(-8);
  } else {
    cookieStatusEl.textContent = "—";
  }

  // Last updated
  if (state.lastUpdated) {
    const d = new Date(state.lastUpdated);
    lastUpdatedEl.textContent = d.toLocaleString();
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

  // Disable sync if no account
  btnSync.disabled = !state.accountId;
}

// ─── Health check ────────────────────────────────────────────────────────────

async function checkHealth() {
  healthStatusEl.textContent = "Checking...";
  healthStatusEl.className = "health-hint";
  try {
    const { serviceUrl } = await chrome.storage.local.get({ serviceUrl: "http://localhost:8899" });
    const resp = await fetch(`${serviceUrl}/health`, { method: "GET" });
    if (resp.ok) {
      healthStatusEl.textContent = "Service reachable";
      healthStatusEl.className = "health-hint status-connected";
    } else {
      healthStatusEl.textContent = "Service returned " + resp.status;
      healthStatusEl.className = "health-hint status-error";
    }
  } catch {
    healthStatusEl.textContent = "Service unreachable";
    healthStatusEl.className = "health-hint status-error";
  }
}

// ─── Actions ─────────────────────────────────────────────────────────────────

function showResult(text, type = "info") {
  resultEl.textContent = text;
  resultEl.className = type === "error" ? "result-error" : type === "success" ? "result-success" : "";
}

function setButtonsDisabled(disabled) {
  btnSync.disabled = disabled;
  btnRefresh.disabled = disabled;
}

btnSaveConfig.addEventListener("click", async () => {
  const url = backendUrlInput.value.trim().replace(/\/+$/, "");
  if (!url) {
    showResult("Service URL is required.", "error");
    return;
  }
  await chrome.storage.local.set({ serviceUrl: url });
  showResult("Config saved.", "success");
  checkHealth();
});

btnSync.addEventListener("click", async () => {
  setButtonsDisabled(true);
  btnSync.textContent = "Syncing...";
  showResult("");
  try {
    const resp = await chrome.runtime.sendMessage({ type: "MANUAL_SYNC" });
    if (resp.ok) {
      const d = resp.data;
      showResult(
        `Synced ${d.synced_threads} threads, ${d.messages_inserted} new messages.`,
        "success"
      );
    } else {
      showResult(resp.error || "Sync failed.", "error");
    }
  } catch (err) {
    showResult(err.message, "error");
  }
  btnSync.textContent = "Sync Now";
  setButtonsDisabled(false);
  await loadState();
});

btnRefresh.addEventListener("click", async () => {
  setButtonsDisabled(true);
  btnRefresh.textContent = "Refreshing...";
  showResult("");
  try {
    const resp = await chrome.runtime.sendMessage({ type: "MANUAL_REFRESH" });
    if (resp.ok) {
      showResult("Cookies refreshed successfully.", "success");
    } else {
      showResult(resp.error || "Refresh failed.", "error");
    }
  } catch (err) {
    showResult(err.message, "error");
  }
  btnRefresh.textContent = "Refresh Cookies";
  setButtonsDisabled(false);
  await loadState();
});

btnDisconnect.addEventListener("click", async () => {
  if (!confirm("Disconnect? This clears all local extension state.")) return;
  await chrome.storage.local.clear();
  showResult("");
  await loadState();
  checkHealth();
});

// ─── Init ────────────────────────────────────────────────────────────────────

loadState();
checkHealth();
