"use strict";

const $ = (id) => document.getElementById(id);
const selected = new Map();
let chats = [];
let status = null;
let mutating = false;
let refreshingStatus = false;
let searchRevision = 0;

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

async function api(path, body) {
  const options = {headers: {"X-Tokenmaxxer": "1"}};
  if (body !== undefined) {
    options.method = "POST";
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "The dashboard is unavailable. Try again shortly.");
  return data;
}

function date(seconds) {
  return seconds ? new Date(seconds * 1000).toLocaleString([], {month: "short", day: "numeric", hour: "numeric", minute: "2-digit"}) : "Not checked yet";
}

function notice(message, error = false) {
  $("action-message").textContent = message;
  $("action-message").className = "notice" + (error ? " error" : "");
  $("action-message").hidden = !message;
}

function updateSelection() {
  $("selection-count").textContent = selected.size ? `${selected.size} chat${selected.size === 1 ? "" : "s"} selected` : "No chats selected";
  $("enqueue").disabled = !selected.size || mutating || !status;
}

function renderChats() {
  const queued = new Set((status?.entries || []).map((entry) => entry.thread_id));
  const fragment = document.createDocumentFragment();
  for (const chat of chats) {
    if (queued.has(chat.id)) selected.delete(chat.id);
    const row = element("label", "chat-row" + (selected.has(chat.id) ? " selected" : ""));
    const checkbox = element("input");
    checkbox.type = "checkbox";
    checkbox.checked = selected.has(chat.id);
    checkbox.disabled = queued.has(chat.id) || mutating;
    checkbox.addEventListener("change", () => {
      if (checkbox.checked) selected.set(chat.id, chat.title);
      else selected.delete(chat.id);
      row.classList.toggle("selected", checkbox.checked);
      updateSelection();
    });
    const copy = element("span", "chat-copy");
    copy.append(element("span", "chat-title", chat.title || "Untitled chat"));
    const project = (chat.cwd || "").split("/").filter(Boolean).slice(-2).join(" / ");
    const subtitle = element("span", "chat-project", `${project || "Local chat"} · ${chat.id.slice(0, 8)}`);
    subtitle.title = `${chat.cwd || ""}\n${chat.id}`;
    copy.append(subtitle);
    row.append(checkbox, copy);
    if (queued.has(chat.id)) row.append(element("span", "queued-label", "QUEUED"));
    fragment.append(row);
  }
  if (!chats.length) fragment.append(element("p", "empty", "No chats found. Try a different search."));
  $("chats").replaceChildren(fragment);
  $("chat-count").textContent = chats.length;
  updateSelection();
}

function chatLink(entry) {
  const link = element("a", "", entry.title);
  link.href = `codex://threads/${encodeURIComponent(entry.thread_id)}`;
  link.title = "Open in Codex";
  return link;
}

function renderStatus() {
  const stale = !status.last_check || Date.now() / 1000 - status.last_check > 180;
  const monitor = !status.service_installed ? "Worker not installed" : status.paused ? "Queue paused" : stale ? "Check overdue" : "Watching for resets";
  $("monitor-state").textContent = monitor;
  $("last-check").textContent = `Last check: ${date(status.last_check)}`;
  $("pause").textContent = status.paused ? "Resume queue" : "Pause queue";
  $("pause").disabled = mutating;
  $("queue-count").textContent = status.entries.length;
  $("connection-error").textContent = status.last_error || (!status.service_installed ? "The background worker is not installed. Chats cannot resume automatically until it is restored." : stale ? "The worker has not checked recently. Keep this Mac awake; the next successful check will update this status." : "");
  $("connection-error").hidden = !$("connection-error").textContent;
  const cards = status.entries.map((entry) => {
    const card = element("article", "queue-card");
    const top = element("div", "queue-top");
    const remove = element("button", "remove", "Remove");
    remove.disabled = mutating;
    remove.setAttribute("aria-label", `Remove ${entry.title} from queue`);
    remove.addEventListener("click", () => change("/api/remove", {chat: entry.id}));
    top.append(chatLink(entry), remove);
    const states = {waiting: status.paused ? "PAUSED" : "WAITING FOR RESET", sending: "CONFIRMING DELIVERY", needs_attention: "NEEDS ATTENTION"};
    card.append(top, element("span", "badge" + (entry.state === "needs_attention" ? " attention" : ""), states[entry.state] || entry.state));
    card.append(element("p", "", entry.note));
    card.append(element("p", "reset-time", `Reset expected: ${date(Math.max(...Object.values(entry.reset_windows)))}`));
    const details = element("details");
    details.append(element("summary", "", "Continuation instruction"), element("p", "", entry.prompt));
    card.append(details);
    return card;
  });
  if (!cards.length) {
    const empty = element("div", "empty-state");
    empty.append(element("span", "", "↗"), element("h3", "", "A little planning. More progress."), element("p", "", "Select chats to prepare your next session."));
    cards.push(empty);
  }
  // Preserve instruction disclosure state across periodic refreshes.
  const openIds = new Set([...$("queue").querySelectorAll("article")].filter((card) => card.querySelector("details")?.open).map((card) => card.dataset.id));
  cards.forEach((card, index) => {
    const entry = status.entries[index];
    if (entry) { card.dataset.id = entry.id; card.querySelector("details").open = openIds.has(entry.id); }
  });
  $("queue").replaceChildren(...cards);
  $("history-count").textContent = status.history.length ? `(${status.history.length})` : "";
  const outcomes = {resumed: "Continuation accepted", cancelled: "Removed from queue", finished_before_reset: "Finished before reset"};
  const history = status.history.map((entry) => {
    const item = element("div", "history-item");
    item.append(chatLink(entry), element("p", "", `${outcomes[entry.outcome] || entry.outcome} · ${date(entry.finished_at)}`));
    return item;
  });
  $("history").replaceChildren(...(history.length ? history : [element("p", "hint", "Completed enrollments will appear here.")]));
  renderChats();
}

async function refreshStatus() {
  if (refreshingStatus || mutating) return;
  refreshingStatus = true;
  try {
    status = await api("/api/status");
    renderStatus();
  } catch (error) {
    $("connection-error").textContent = `Could not refresh queue status: ${error.message}`;
    $("connection-error").hidden = false;
    $("monitor-state").textContent = "Connection unavailable";
  } finally { refreshingStatus = false; }
}

async function loadChats() {
  const revision = ++searchRevision;
  $("chat-hint").textContent = "Searching local chats…";
  try {
    const result = await api(`/api/chats?search=${encodeURIComponent($("search").value.trim())}`);
    if (revision !== searchRevision) return;
    chats = result;
    renderChats();
    $("chat-hint").textContent = "Up to 100 matching local chats. Selections stay selected across searches.";
  } catch (error) {
    if (revision !== searchRevision) return;
    $("chat-hint").textContent = `Could not load chats: ${error.message}`;
  }
}

async function loadUsage() {
  try {
    const usage = await api("/api/usage");
    const buckets = Object.entries(usage.rateLimitsByLimitId || {});
    const previous = $("bucket").value;
    $("bucket").replaceChildren(...buckets.map(([id]) => {
      const option = element("option", "", id); option.value = id; return option;
    }));
    if (buckets.some(([id]) => id === previous)) $("bucket").value = previous;
    $("bucket-field").hidden = buckets.length <= 1;
    const windows = [];
    for (const [id, bucket] of buckets) {
      for (const name of ["primary", "secondary"]) {
        const window = bucket[name];
        if (!window || typeof window.usedPercent !== "number") continue;
        const item = element("div", "usage-window");
        const meter = element("progress"); meter.max = 100; meter.value = window.usedPercent;
        meter.setAttribute("aria-label", `${id} ${name} usage`);
        item.append(element("span", "", `${id} · ${Math.round(window.usedPercent)}% used`), meter, element("span", "", `Resets ${date(window.resetsAt)}`));
        windows.push(item);
      }
    }
    $("usage").replaceChildren(...(windows.length ? windows : [element("span", "", "Usage windows unavailable. Refresh to try again.")]));
  } catch (error) { $("usage").textContent = `Usage unavailable: ${error.message}`; }
}

async function change(path, body) {
  if (mutating) return;
  mutating = true;
  renderStatus();
  try { const result = await api(path, body); notice(result.message); }
  catch (error) { notice(error.message, true); }
  finally { mutating = false; await refreshStatus(); }
}

$("enqueue").addEventListener("click", async () => {
  if (mutating || !selected.size) return;
  const batch = [...selected];
  const prompt = $("prompt").value.trim();
  const bucket = $("bucket").value;
  const failures = [];
  let queued = 0;
  mutating = true;
  renderStatus();
  for (const [id, title] of batch) {
    notice(`Queueing ${title}…`);
    try {
      const body = {chat: id};
      if (prompt) body.prompt = prompt;
      if (bucket) body.limit_id = bucket;
      await api("/api/queue", body);
      selected.delete(id);
      queued++;
    } catch (error) { failures.push(`${title}: ${error.message}`); }
  }
  mutating = false;
  await refreshStatus();
  notice(`${queued} chat${queued === 1 ? "" : "s"} queued for a single continuation.${failures.length ? "\n" + failures.join("\n") : ""}`, failures.length > 0);
});
$("pause").addEventListener("click", () => change(status.paused ? "/api/resume" : "/api/pause", {}));
$("search-form").addEventListener("submit", (event) => { event.preventDefault(); loadChats(); });
$("refresh").addEventListener("click", async () => {
  $("refresh").disabled = true;
  try { await Promise.all([refreshStatus(), loadChats(), loadUsage()]); }
  finally { $("refresh").disabled = false; }
});
Promise.all([refreshStatus(), loadChats(), loadUsage()]);
setInterval(refreshStatus, 10000);
