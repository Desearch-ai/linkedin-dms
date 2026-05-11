const state = {
  accounts: [],
  guilds: [],
  channels: [],
};

async function getJson(path) {
  const res = await fetch(path);
  if (!res.ok) throw new Error(`${path} failed: ${res.status}`);
  return res.json();
}

function option(value, label) {
  const el = document.createElement("option");
  el.value = value;
  el.textContent = label;
  return el;
}

function fillSelect(id, rows, labelFor) {
  const el = document.getElementById(id);
  el.replaceChildren(el.firstElementChild);
  rows.forEach((row) => el.appendChild(option(row.id, labelFor(row))));
}

function paramsFromFilters() {
  const params = new URLSearchParams();
  const account_id = document.getElementById("accountFilter").value;
  const guild_id = document.getElementById("guildFilter").value;
  const channel_id = document.getElementById("channelFilter").value;
  if (account_id) params.set("account_id", account_id);
  if (guild_id) params.set("guild_id", guild_id);
  if (channel_id) params.set("channel_id", channel_id);
  params.set("limit", "100");
  return params;
}

function renderMessages(messages) {
  const root = document.getElementById("messages");
  root.replaceChildren();
  if (!messages.length) {
    root.textContent = "No fixture messages match the current filters.";
    return;
  }
  messages.forEach((message) => {
    const card = document.createElement("article");
    card.className = "card";
    card.innerHTML = `
      <div class="meta">${message.account_label} · ${message.guild_name} / #${message.channel_name}</div>
      <strong>${message.author_display_name}</strong>
      <p>${message.content}</p>
      <time>${message.sent_at}</time>
    `;
    root.appendChild(card);
  });
}

function renderSignals(signals) {
  const root = document.getElementById("signals");
  root.replaceChildren();
  signals.forEach((signal) => {
    const card = document.createElement("article");
    card.className = "card";
    card.innerHTML = `
      <div class="meta">${signal.keyword} · ${signal.guild_name} / #${signal.channel_name}</div>
      <strong>${signal.topic}</strong>
      <p>${signal.summary}</p>
    `;
    root.appendChild(card);
  });
}

async function loadMessages() {
  const query = document.getElementById("searchInput").value.trim();
  const params = paramsFromFilters();
  const endpoint = query ? "/discord/search" : "/discord/messages";
  if (query) params.set("query", query);
  const payload = await getJson(`${endpoint}?${params.toString()}`);
  renderMessages(payload.messages || []);
}

async function boot() {
  const [accounts, guilds, channels, commands, signals] = await Promise.all([
    getJson("/discord/accounts"),
    getJson("/discord/guilds"),
    getJson("/discord/channels"),
    getJson("/discord/commands"),
    getJson("/discord/lead-signals"),
  ]);
  state.accounts = accounts.accounts || [];
  state.guilds = guilds.guilds || [];
  state.channels = channels.channels || [];
  fillSelect("accountFilter", state.accounts, (row) => row.label);
  fillSelect("guildFilter", state.guilds, (row) => row.name);
  fillSelect("channelFilter", state.channels, (row) => `${row.guild_name} / #${row.name}`);

  const commandRoot = document.getElementById("commands");
  (commands.commands || []).forEach((command) => {
    const item = document.createElement("li");
    item.textContent = command;
    commandRoot.appendChild(item);
  });
  renderSignals(signals.lead_signals || []);
  await loadMessages();

  ["accountFilter", "guildFilter", "channelFilter", "searchInput"].forEach((id) => {
    document.getElementById(id).addEventListener("input", loadMessages);
  });
}

boot().catch((error) => {
  document.getElementById("messages").textContent = error.message;
});
