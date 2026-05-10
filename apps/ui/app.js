const state = {
  apiBase: localStorage.getItem("linkedinOps.apiBase") || window.location.origin || "http://127.0.0.1:8899",
  accountId: Number(localStorage.getItem("linkedinOps.accountId") || "1"),
  token: sessionStorage.getItem("linkedinOps.token") || "",
  selectedThreadId: null,
  selectedDraft: null,
  latestEvidence: null,
};

const $ = (id) => document.getElementById(id);

function endpoint(path) {
  const base = state.apiBase.replace(/\/$/, "");
  return `${base}${path}`;
}

function headers(extra = {}) {
  const base = { "Content-Type": "application/json", ...extra };
  if (state.token) base.Authorization = `Bearer ${state.token}`;
  return base;
}

function safe(value, fallback = "—") {
  return value === null || value === undefined || value === "" ? fallback : String(value);
}

function setCard(id, title, subtitle = "") {
  const card = $(id);
  card.querySelector("strong").textContent = title;
  card.querySelector("small").textContent = subtitle;
}

function renderError(target, message, nextAction = "Check token/account settings and retry.") {
  target.className = `${target.className.replace(/\bloading\b/g, "")} error`;
  target.innerHTML = `<strong>Unable to load.</strong><br><span>${safe(message)}</span><br><small>${nextAction}</small>`;
}

async function api(path, options = {}) {
  const response = await fetch(endpoint(path), { ...options, headers: headers(options.headers || {}) });
  const data = await response.json().catch(() => ({ ok: false, error: { message: response.statusText } }));
  if (!response.ok || data.ok === false) {
    const err = new Error(data?.error?.message || data?.detail || response.statusText || "Request failed");
    err.payload = data;
    err.status = response.status;
    throw err;
  }
  return data;
}

function threadButton(thread) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = `thread-row ${state.selectedThreadId === thread.thread_id ? "active" : ""}`;
  btn.innerHTML = `
    <span class="thread-title">${safe(thread.title, "Untitled thread")}</span>
    <span class="preview">${safe(thread.last_message_preview, "No local messages yet")}</span>
    <span class="thread-meta">${safe(thread.last_direction)} · ${thread.message_count || 0} messages · ${safe(thread.last_message_at)}</span>
  `;
  btn.addEventListener("click", () => selectThread(thread.thread_id, thread.title));
  return btn;
}

async function loadInbox() {
  const list = $("thread-list");
  list.className = "list loading";
  list.textContent = "Loading thread previews…";
  try {
    const data = await api(`/ops/inbox?account_id=${state.accountId}&limit=30`);
    list.className = "list";
    list.innerHTML = "";
    if (!data.threads.length) {
      list.appendChild(document.importNode($("empty-template").content, true));
      return;
    }
    data.threads.forEach((thread) => list.appendChild(threadButton(thread)));
  } catch (err) {
    renderError(list, err.message, err.payload?.error?.code === "account_not_found" ? "Create an account through POST /accounts first." : undefined);
  }
}

async function runSearch(event) {
  event.preventDefault();
  const query = $("search-query").value.trim();
  if (!query) return loadInbox();
  const direction = $("direction-filter").value;
  const list = $("thread-list");
  list.className = "list loading";
  list.textContent = "Searching local message archive…";
  try {
    const params = new URLSearchParams({ account_id: String(state.accountId), q: query, limit: "30" });
    if (direction) params.set("direction", direction);
    const data = await api(`/ops/search?${params.toString()}`);
    list.className = "list";
    list.innerHTML = "";
    if (!data.results.length) {
      list.innerHTML = `<div class="empty">No local matches for “${query}”. Search is local-only and does not touch LinkedIn.</div>`;
      return;
    }
    data.results.forEach((result) => list.appendChild(threadButton({
      thread_id: result.thread_id,
      title: result.title,
      last_message_preview: result.text_snippet,
      last_direction: result.direction,
      message_count: "match",
      last_message_at: result.sent_at,
    })));
  } catch (err) {
    renderError(list, err.message);
  }
}

async function selectThread(threadId, fallbackTitle = "Thread") {
  state.selectedThreadId = threadId;
  $("draft-form").dataset.threadId = String(threadId);
  $("message-list").className = "timeline loading";
  $("message-list").textContent = "Loading local message history…";
  try {
    const [detail, messages] = await Promise.all([
      api(`/ops/threads/${threadId}?account_id=${state.accountId}`),
      api(`/ops/threads/${threadId}/messages?account_id=${state.accountId}&limit=100`),
    ]);
    $("thread-heading").textContent = detail.thread.title || fallbackTitle || `Thread ${threadId}`;
    $("thread-health").textContent = `${detail.thread.message_count || 0} local messages`;
    renderMessages(messages.messages);
    loadInbox();
  } catch (err) {
    renderError($("message-list"), err.message);
  }
}

function renderMessages(messages) {
  const box = $("message-list");
  box.className = "timeline";
  box.innerHTML = "";
  if (!messages.length) {
    box.className = "timeline empty";
    box.textContent = "Thread has no stored messages. Run sync for this account before drafting.";
    return;
  }
  messages.forEach((message) => {
    const item = document.createElement("article");
    item.className = `message ${message.direction === "out" ? "out" : "in"}`;
    item.innerHTML = `<div class="sender">${safe(message.sender, message.direction)} · ${safe(message.sent_at)}</div><div>${safe(message.text, "[empty]")}</div>`;
    box.appendChild(item);
  });
}

async function createDraft(event) {
  event.preventDefault();
  const output = $("draft-output");
  output.className = "output loading";
  output.textContent = "Creating local approval candidate…";
  try {
    const payload = {
      account_id: state.accountId,
      thread_id: state.selectedThreadId,
      recipient: $("draft-recipient").value.trim(),
      text: $("draft-text").value,
      idempotency_key: $("draft-idempotency").value.trim() || null,
    };
    const data = await api("/ops/drafts", { method: "POST", body: JSON.stringify(payload) });
    state.selectedDraft = { ...data, text: payload.text, recipient: payload.recipient };
    output.className = "output success";
    output.textContent = `Draft ${data.draft_id} created. Approval ${data.approval_id} is ${data.approval_state}; external writes: ${data.external_writes}.`;
    $("approve-draft").disabled = false;
    $("send-approved").disabled = true;
    await Promise.all([loadAudit(), loadSyncStatus()]);
  } catch (err) {
    renderError(output, err.message);
  }
}

async function approveDraft() {
  if (!state.selectedDraft) return;
  const output = $("draft-output");
  output.className = "output loading";
  output.textContent = "Recording local approval…";
  try {
    const data = await api(`/ops/approvals/${state.selectedDraft.approval_id}/approve`, {
      method: "POST",
      body: JSON.stringify({ approved_by: "local-operator" }),
    });
    state.selectedDraft.approval = data.approval;
    output.className = "output success";
    output.textContent = `Approval ${data.approval.approval_id} recorded. Send is now enabled but still requires the exact approved text.`;
    $("send-approved").disabled = false;
    await loadAudit();
  } catch (err) {
    renderError(output, err.message);
  }
}

async function sendApproved() {
  if (!state.selectedDraft) return;
  const confirmed = window.confirm("This will call the approved-send API. Continue only if the operator has reviewed the exact text and recipient.");
  if (!confirmed) return;
  const output = $("draft-output");
  output.className = "output loading";
  output.textContent = "Submitting approved send…";
  try {
    const data = await api("/ops/send-approved", {
      method: "POST",
      body: JSON.stringify({
        approval_id: state.selectedDraft.approval_id,
        account_id: state.accountId,
        recipient: state.selectedDraft.recipient,
        text: state.selectedDraft.text,
        idempotency_key: state.selectedDraft.idempotency_key || null,
      }),
    });
    output.className = "output success";
    output.textContent = `Send ${data.send_id} ${data.status}. Approval ${data.approval_id} marked used.`;
    $("send-approved").disabled = true;
    await Promise.all([loadAudit(), loadSyncStatus()]);
  } catch (err) {
    renderError(output, err.message, "The guard rejected this request before sending unless all approval evidence matched.");
  }
}

async function loadStatus() {
  try {
    const status = await api("/ops/status");
    setCard("service-card", status.service, `DB schema ${status.db.schema_version}; auth ${status.api.auth_required ? "enabled" : "off"}`);
  } catch (err) {
    setCard("service-card", "Needs token", err.message);
  }
}

async function loadHealth() {
  try {
    const data = await api(`/ops/accounts/${state.accountId}/health`);
    setCard("health-card", data.status || "unknown", data.next_action || `messages ${data.counts?.messages || 0}`);
  } catch (err) {
    setCard("health-card", "Not ready", err.message);
  }
}

async function loadSyncStatus() {
  try {
    const data = await api(`/ops/sync/status?account_id=${state.accountId}`);
    setCard("sync-card", `${data.counts.threads || 0} threads`, `${data.counts.messages || 0} messages; writes require approval`);
  } catch (err) {
    setCard("sync-card", "Unavailable", err.message);
  }
}

async function dryRunSync() {
  const card = $("campaign-status");
  card.className = "state-card loading";
  card.textContent = "Checking sync plan without external reads/writes…";
  try {
    const data = await api("/ops/sync/dry-run", { method: "POST", body: JSON.stringify({ account_id: state.accountId }) });
    card.className = "state-card success";
    card.innerHTML = `<strong>Sync dry-run ready.</strong><br>limit ${data.planned.limit_per_thread}, pages ${safe(data.planned.max_pages_per_thread)}, external writes ${data.external_writes}.`;
  } catch (err) {
    renderError(card, err.message);
  }
}

async function dryRunCampaign() {
  const card = $("campaign-status");
  card.className = "state-card loading";
  card.textContent = "Running local campaign dry-run…";
  try {
    const data = await api("/ops/campaigns/1/run-dry-run", { method: "POST", body: JSON.stringify({ account_id: state.accountId, limit: 25 }) });
    card.className = "state-card success";
    card.innerHTML = `<strong>Campaign dry-run complete.</strong><br>${data.planned_actions.length} planned actions; external writes ${data.external_writes}; blocked not approved ${data.summary.blocked_not_approved}.`;
  } catch (err) {
    renderError(card, err.message);
  }
}

async function loadCampaignStatus() {
  const card = $("campaign-status");
  try {
    const data = await api(`/ops/campaigns/1/status?account_id=${state.accountId}`);
    card.className = "state-card";
    card.innerHTML = `<strong>Campaign #${data.campaign_id}: ${data.state}</strong><br>Drafted ${data.totals.drafted}, approved ${data.totals.approved}, sent ${data.totals.sent}. Remaining today: ${data.rate_limit.remaining_today}.`;
  } catch (err) {
    card.className = "state-card empty";
    card.textContent = "No campaigns configured. Dry-run remains available for local planning only.";
  }
}

async function loadAudit() {
  const list = $("audit-list");
  list.className = "list loading";
  list.textContent = "Loading audit events and outbound-send history…";
  try {
    const data = await api(`/ops/audit?account_id=${state.accountId}&limit=20`);
    state.latestEvidence = data;
    list.className = "list";
    list.innerHTML = "";
    if (!data.events.length && !data.outbound_sends.length) {
      list.innerHTML = `<div class="empty">No outbound sends recorded and no audit events yet.</div>`;
      return;
    }
    [...data.events.map((event) => ({ kind: "audit", ...event })), ...data.outbound_sends.map((send) => ({ kind: "send", ...send }))]
      .slice(0, 12)
      .forEach((item) => {
        const row = document.createElement("article");
        row.className = "audit-row";
        row.innerHTML = `<strong>${item.kind === "send" ? `send.${item.status}` : item.event_type}</strong><br><span class="thread-meta">${safe(item.created_at || item.updated_at)} · ${safe(item.entity_type || item.recipient)}</span><pre>${JSON.stringify(item.payload || { id: item.id, status: item.status, idempotency_key: item.idempotency_key }, null, 2)}</pre>`;
        list.appendChild(row);
      });
  } catch (err) {
    renderError(list, err.message);
  }
}

async function copyEvidence() {
  if (!state.latestEvidence) await loadAudit();
  const payload = JSON.stringify({ account_id: state.accountId, evidence: state.latestEvidence }, null, 2);
  await navigator.clipboard.writeText(payload);
  $("audit-list").insertAdjacentHTML("afterbegin", `<div class="success output">Copied redacted evidence JSON to clipboard.</div>`);
}

function persistSettings(event) {
  event.preventDefault();
  state.apiBase = $("api-base").value.trim() || window.location.origin;
  state.accountId = Number($("account-id").value || "1");
  state.token = $("api-token").value.trim();
  localStorage.setItem("linkedinOps.apiBase", state.apiBase);
  localStorage.setItem("linkedinOps.accountId", String(state.accountId));
  if (state.token) sessionStorage.setItem("linkedinOps.token", state.token);
  else sessionStorage.removeItem("linkedinOps.token");
  boot();
}

async function boot() {
  $("api-base").value = state.apiBase;
  $("account-id").value = String(state.accountId);
  $("api-token").value = state.token;
  $("approve-draft").disabled = true;
  $("send-approved").disabled = true;
  state.selectedDraft = null;
  await Promise.all([loadStatus(), loadHealth(), loadSyncStatus(), loadInbox(), loadCampaignStatus(), loadAudit()]);
}

$("settings-form").addEventListener("submit", persistSettings);
$("search-form").addEventListener("submit", runSearch);
$("draft-form").addEventListener("submit", createDraft);
$("approve-draft").addEventListener("click", approveDraft);
$("send-approved").addEventListener("click", sendApproved);
$("sync-dry-run").addEventListener("click", dryRunSync);
$("campaign-dry-run").addEventListener("click", dryRunCampaign);
$("export-evidence").addEventListener("click", copyEvidence);

boot();
