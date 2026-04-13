// Desearch LinkedIn DMs — Popup UI Logic

const statusBadge = document.getElementById("statusBadge");
const statusText = document.getElementById("statusText");
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
const syncLabel = btnSync.textContent;

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
  });

  backendUrlInput.value = state.serviceUrl;
  accountIdEl.textContent = state.accountId ?? "—";

  // Status badge
  if (state.lastStatus === "connected") {
    statusText.textContent = "Connected";
    statusBadge.className = "status-badge status-connected";
  } else if (state.lastStatus === "error") {
    statusText.textContent = state.lastError || "Error";
    statusBadge.className = "status-badge status-error";
  } else {
    statusText.textContent = "Disconnected";
    statusBadge.className = "status-badge status-disconnected";
  }

  cookieStatusEl.textContent = await getCookieStatus();

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

async function getCookieStatus() {
  try {
    const cookie = await chrome.cookies.get({
      url: "https://www.linkedin.com",
      name: "li_at",
    });
    return cookie ? "Detected" : "Not detected";
  } catch {
    return "Unavailable";
  }
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
  const originalHTML = btnSync.innerHTML;
  setButtonsDisabled(true);
  btnSync.classList.add("syncing");
  btnSync.innerHTML = originalHTML.replace("Sync Now", "Syncing\u2026");
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
  btnSync.innerHTML = originalHTML;
  btnSync.classList.remove("syncing");
  setButtonsDisabled(false);
  await loadState();
});

btnRefresh.addEventListener("click", async () => {
  const originalHTML = btnRefresh.innerHTML;
  setButtonsDisabled(true);
  btnRefresh.innerHTML = originalHTML.replace("Refresh", "Refreshing\u2026");
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
  btnRefresh.innerHTML = originalHTML;
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

// ─── Theme ──────────────────────────────────────────────────────────────────

const btnTheme = document.getElementById("btnTheme");

function applyTheme(theme) {
  document.documentElement.classList.toggle("light", theme === "light");
}

async function loadTheme() {
  const { theme } = await chrome.storage.local.get({ theme: "dark" });
  applyTheme(theme);
}

btnTheme.addEventListener("click", async () => {
  const isLight = document.documentElement.classList.contains("light");
  const next = isLight ? "dark" : "light";
  applyTheme(next);
  await chrome.storage.local.set({ theme: next });
});

// ─── Init ────────────────────────────────────────────────────────────────────

loadTheme();
loadState();
checkHealth();
