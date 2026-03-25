/* ------------------------------------------------------------------ *
 *  LinkedIn DMs Bridge – Popup UI                                     *
 * ------------------------------------------------------------------ */

const $ = (sel) => document.querySelector(sel);

const STATUS_MAP = {
  connected:       { label: "Connected",    cls: "status-connected" },
  expired:         { label: "Session Expired", cls: "status-expired" },
  refresh_failed:  { label: "Refresh Failed",  cls: "status-error" },
  register_failed: { label: "Register Failed", cls: "status-error" },
  api_unreachable: { label: "Unreachable",     cls: "status-error" },
};

function send(msg) {
  return new Promise((resolve) => chrome.runtime.sendMessage(msg, resolve));
}

// ── Render state ─────────────────────────────────────────────────────

function render(state) {
  // Status
  const info = STATUS_MAP[state.status] || { label: "Disconnected", cls: "status-disconnected" };
  const badge = $("#status-badge");
  badge.textContent = info.label;
  badge.className = info.cls;

  // Service URL
  $("#service-url").value = state.serviceUrl || "http://localhost:8000";

  // Account
  $("#account-id").textContent = state.accountId || "—";
  $("#cookie-status").textContent = state.li_at ? `...${state.li_at.slice(-8)}` : "—";

  const hdrCount = state.capturedHeaders ? Object.keys(state.capturedHeaders).length : 0;
  $("#headers-status").textContent = hdrCount > 0 ? `${hdrCount} captured` : "—";

  // Sync
  $("#btn-sync").disabled = !state.accountId;

  if (state.lastSyncAt) {
    const d = new Date(state.lastSyncAt);
    $("#last-sync").textContent = `Last sync: ${d.toLocaleString()}`;
  } else {
    $("#last-sync").textContent = "Never synced";
  }
}

// ── Health check ─────────────────────────────────────────────────────

async function checkHealth() {
  const el = $("#health-status");
  el.textContent = "Checking...";
  el.className = "";
  const res = await send({ action: "checkHealth" });
  if (res && res.ok) {
    el.textContent = "Service reachable";
    el.className = "status-connected";
  } else {
    el.textContent = "Service unreachable";
    el.className = "status-expired";
  }
}

// ── Event listeners ──────────────────────────────────────────────────

document.addEventListener("DOMContentLoaded", async () => {
  const state = await send({ action: "getState" });
  render(state || {});
  checkHealth();
});

$("#form-url").addEventListener("submit", async (e) => {
  e.preventDefault();
  const url = $("#service-url").value.trim();
  if (!url) return;
  await send({ action: "updateServiceUrl", url });
  checkHealth();
});

$("#btn-sync").addEventListener("click", async () => {
  const btn = $("#btn-sync");
  const resultEl = $("#sync-result");

  btn.disabled = true;
  btn.ariaBusy = "true";
  btn.textContent = "Syncing...";
  resultEl.textContent = "";

  const res = await send({ action: "sync" });

  btn.ariaBusy = "false";
  btn.textContent = "Sync Now";
  btn.disabled = false;

  if (res && res.ok) {
    const d = res.data;
    resultEl.textContent = `Synced ${d.synced_threads} threads, ${d.messages_inserted} new messages`;
    resultEl.className = "status-connected";
  } else {
    resultEl.textContent = res ? res.error : "Unknown error";
    resultEl.className = "status-expired";
  }

  // Refresh full state
  const state = await send({ action: "getState" });
  render(state || {});
});

$("#btn-disconnect").addEventListener("click", async () => {
  if (!confirm("Disconnect this extension? This clears all local state.")) return;
  await send({ action: "disconnect" });
  const state = await send({ action: "getState" });
  render(state || {});
  $("#sync-result").textContent = "";
  checkHealth();
});
