// 会话测试台 - 页面脚本
// 通过 window.AstrBotPluginPage bridge 与插件后端通信。
const bridge = window.AstrBotPluginPage;

const $ = (id) => document.getElementById(id);

let sessions = [];
let platforms = [];
let confs = [];
let openIds = [];
const panelEls = new Map();
let runBusy = false;

function escapeHtml(value) {
  if (value === null || value === undefined) return "";
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function statusText(status) {
  switch (status) {
    case "ok":
      return "成功";
    case "no_reply":
      return "无回复";
    case "timeout":
      return "超时";
    case "error":
      return "错误";
    default:
      return status;
  }
}

function confName(id) {
  if (!id) return "";
  const c = confs.find((x) => x.id === id);
  return c ? c.name : id;
}

function platformName(id) {
  const p = platforms.find((x) => x.id === id);
  return p ? p.display_name || p.name : id;
}

function sessionById(id) {
  return sessions.find((s) => s.id === id);
}

// ---------- 会话列表 ----------

async function refreshSessions() {
  sessions = await bridge.apiGet("sessions");
  // 清理已被删除的会话面板
  const valid = new Set(sessions.map((s) => s.id));
  const removed = openIds.filter((id) => !valid.has(id));
  if (removed.length) {
    openIds = openIds.filter((id) => valid.has(id));
    renderPanels();
  }
  renderSessionList();
}

function renderSessionList() {
  const list = $("session-list");
  list.innerHTML = "";
  $("session-count").textContent = sessions.length ? `${sessions.length} 个会话` : "";
  if (!sessions.length) {
    list.innerHTML = '<div class="empty">暂无虚拟会话，请先创建</div>';
    return;
  }
  for (const s of sessions) {
    const item = document.createElement("div");
    item.className = "session-item";
    item.dataset.id = s.id;
    const confBadge = s.conf_id
      ? `<span class="badge conf">${escapeHtml(confName(s.conf_id))}</span>`
      : "";
    const isOpen = openIds.includes(s.id);
    item.innerHTML =
      `<div class="name">${escapeHtml(s.name)}</div>` +
      `<div class="session-meta">` +
      `<span class="badge">${escapeHtml(platformName(s.platform_id))}</span>` +
      confBadge +
      `</div>` +
      `<div class="session-actions">` +
      `<button class="btn small" data-action="open">${isOpen ? "关闭" : "打开"}</button>` +
      `<button class="btn small" data-action="reset">重置</button>` +
      `<button class="btn small danger" data-action="delete">删除</button>` +
      `</div>`;
    item.querySelector('[data-action="open"]').addEventListener("click", () => toggleOpen(s.id));
    item.querySelector('[data-action="reset"]').addEventListener("click", () => resetHistory(s.id));
    item.querySelector('[data-action="delete"]').addEventListener("click", () => deleteSession(s.id));
    list.appendChild(item);
  }
}

// ---------- 面板 ----------

function toggleOpen(id) {
  if (openIds.includes(id)) {
    openIds = openIds.filter((x) => x !== id);
  } else {
    openIds.push(id);
    openPanel(id);
  }
  renderPanels();
  renderSessionList();
}

function openPanel(id) {
  if (panelEls.has(id)) return;
  const panel = document.createElement("div");
  panel.className = "panel";
  panel.dataset.id = id;
  panel.draggable = true;

  const s = sessionById(id);
  const confBadge = s && s.conf_id
    ? `<span class="badge conf">${escapeHtml(confName(s.conf_id))}</span>`
    : "";
  const platformBadge = s
    ? `<span class="badge">${escapeHtml(platformName(s.platform_id))}</span>`
    : "";

  panel.innerHTML =
    `<div class="panel-head" title="拖拽排序">` +
    `<span class="drag-handle">≡</span>` +
    `<span class="panel-title">${escapeHtml(s ? s.name : id)}</span>` +
    platformBadge + confBadge +
    `<span class="panel-actions">` +
    `<button class="icon-btn" data-action="pin" title="置顶">置顶</button>` +
    `<button class="icon-btn" data-action="close" title="关闭">✕</button>` +
    `</span>` +
    `</div>` +
    `<div class="panel-body">` +
    `<div class="chat"></div>` +
    `<div class="panel-status" hidden></div>` +
    `</div>` +
    `<div class="panel-input">` +
    `<input class="msg-input" type="text" placeholder="发送消息到本会话（Enter 发送）" />` +
    `<button class="btn primary send-btn">发送</button>` +
    `</div>`;

  panel.querySelector('[data-action="close"]').addEventListener("click", () => toggleOpen(id));
  panel.querySelector('[data-action="pin"]').addEventListener("click", () => pin(id));
  const input = panel.querySelector(".msg-input");
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.isComposing) sendToOne(id, input.value);
  });
  panel.querySelector(".send-btn").addEventListener("click", () => sendToOne(id, input.value));

  panelEls.set(id, panel);
  renderPanels();
  void loadHistory(id);
}

async function loadHistory(id) {
  const panel = panelEls.get(id);
  if (!panel) return;
  const chat = panel.querySelector(".chat");
  try {
    const data = await bridge.apiGet(`sessions/${encodeURIComponent(id)}/history`);
    renderHistory(panel, data.conversations || []);
  } catch (err) {
    chat.innerHTML = `<div class="empty">加载历史失败: ${escapeHtml(err.message)}</div>`;
  }
}

function renderHistory(panel, conversations) {
  const chat = panel.querySelector(".chat");
  chat.innerHTML = "";
  let count = 0;
  for (const conv of conversations) {
    for (const msg of conv.history || []) {
      count++;
      chat.appendChild(bubbleFor(msg));
    }
  }
  if (!count) {
    const p = document.createElement("div");
    p.className = "empty";
    p.textContent = "暂无对话历史";
    chat.appendChild(p);
  }
  chat.scrollTop = chat.scrollHeight;
}

function extractText(content) {
  if (content == null) return "";
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    return content
      .map((p) => {
        if (typeof p === "string") return p;
        if (p && typeof p.text === "string") return p.text;
        if (p && typeof p.content === "string") return p.content;
        return "";
      })
      .filter(Boolean)
      .join("\n");
  }
  return "";
}

function bubbleFor(msg) {
  const role = msg.role || "";
  const text = extractText(msg.content);
  const el = document.createElement("div");
  if (role === "user") {
    el.className = "msg user";
    el.textContent = text || "（空消息）";
  } else if (role === "assistant_reasoning" || role === "reasoning") {
    el.className = "msg bot";
    const r = document.createElement("div");
    r.className = "reasoning";
    r.textContent = text || "（推理过程）";
    el.appendChild(r);
  } else if (role === "tool") {
    el.className = "msg tool";
    el.textContent = text || "（工具调用）";
  } else if (role === "system") {
    el.className = "msg meta";
    el.textContent = text || "（系统消息）";
  } else {
    // assistant 等其余角色
    el.className = "msg bot";
    if (!text && msg.tool_calls && msg.tool_calls.length) {
      el.textContent = "（调用工具…）";
      el.classList.add("tool");
    } else {
      el.textContent = text || "…";
    }
  }
  return el;
}

function panelStatus(panel, status, text) {
  const el = panel.querySelector(".panel-status");
  el.hidden = false;
  el.className = "panel-status " + status;
  el.textContent = text;
}

function clearPanelStatus(panel) {
  const el = panel.querySelector(".panel-status");
  el.hidden = true;
  el.textContent = "";
}

// ---------- 发送 ----------

function runParams() {
  return {
    timeout: parseFloat($("run-timeout").value) || 120,
    batch_size: parseInt($("run-batch-size").value, 10) || 10,
  };
}

async function sendToOne(id, text) {
  text = (text || "").trim();
  if (!text) return;
  const panel = panelEls.get(id);
  const input = panel.querySelector(".msg-input");
  input.value = "";
  panelStatus(panel, "warn", "发送中…");
  try {
    const result = await bridge.apiPost("test/run", { sessions: [id], text, ...runParams() });
    const r = result.results && result.results[0];
    if (r && r.status === "ok") {
      panelStatus(panel, "ok", `回复成功（${r.duration}s）`);
    } else if (r) {
      panelStatus(
        panel,
        r.status === "error" ? "error" : "warn",
        statusText(r.status) + (r.error ? `：${r.error}` : ""),
      );
    }
  } catch (err) {
    panelStatus(panel, "error", "发送失败: " + err.message);
  }
  void loadHistory(id);
}

async function sendToAll() {
  if (runBusy) return;
  const ids = openIds.slice();
  const text = $("run-text").value.trim();
  if (!ids.length) {
    showRunStatus("warn", "请先在左侧打开至少一个会话");
    return;
  }
  if (!text) {
    showRunStatus("warn", "请输入群发消息");
    return;
  }
  runBusy = true;
  $("btn-run-all").disabled = true;
  $("btn-run-all").textContent = "发送中…";
  $("run-text").value = "";
  showRunStatus("warn", `正在并发发送给 ${ids.length} 个会话…`);
  try {
    const result = await bridge.apiPost("test/run", {
      sessions: ids,
      text,
      ...runParams(),
    });
    const s = result.stats || {};
    showRunStatus(
      result.error || result.timeout ? "warn" : "ok",
      `完成：成功 ${result.ok} / 无回复 ${result.no_reply} / 超时 ${result.timeout} / 错误 ${result.error}` +
        `，耗时 avg ${s.avg}s，p95 ${s.p95}s`,
    );
    for (const r of result.results || []) {
      const panel = panelEls.get(r.session_id);
      if (!panel) continue;
      if (r.status === "ok") {
        panelStatus(panel, "ok", `回复成功（${r.duration}s）`);
      } else {
        panelStatus(
          panel,
          r.status === "error" ? "error" : "warn",
          statusText(r.status) + (r.error ? `：${r.error}` : ""),
        );
      }
    }
    // 重新拉取所有面板历史，与真实 pipeline 持久化的对话同步
    for (const id of ids) void loadHistory(id);
  } catch (err) {
    showRunStatus("error", "发送失败: " + err.message);
  } finally {
    runBusy = false;
    $("btn-run-all").disabled = false;
    $("btn-run-all").textContent = "发送到全部";
  }
}

function showRunStatus(status, text) {
  const el = $("run-status");
  el.hidden = false;
  el.className = "run-status " + status;
  el.textContent = text;
}

// ---------- 会话操作 ----------

async function resetHistory(id) {
  if (!confirm(`确定重置会话 ${id} 的对话历史吗？`)) return;
  const resp = await bridge.apiPost("reset", { ids: [id] });
  const panel = panelEls.get(id);
  if (panel) {
    clearPanelStatus(panel);
    void loadHistory(id);
  }
  showRunStatus("ok", `已重置 ${resp.reset} 个会话的对话历史`);
}

async function deleteSession(id) {
  if (!confirm(`确定删除会话 ${id} 吗？`)) return;
  await bridge.apiPost("sessions/delete", { ids: [id] });
  openIds = openIds.filter((x) => x !== id);
  renderPanels();
  await refreshSessions();
}

// ---------- 创建 ----------

async function createSessions() {
  const count = parseInt($("create-count").value, 10);
  if (!Number.isInteger(count) || count < 1) {
    alert("数量必须是大于 0 的整数");
    return;
  }
  const platformId = $("create-platform").value;
  const confId = $("create-conf").value;
  const btn = $("btn-create");
  btn.disabled = true;
  try {
    const created = await bridge.apiPost("sessions", {
      count,
      platform_id: platformId && platformId !== "virtual_test" ? platformId : undefined,
      conf_id: confId || undefined,
      sender_id: $("create-sender-id").value || undefined,
      sender_name: $("create-sender-name").value || undefined,
      name_prefix: $("create-name-prefix").value || undefined,
    });
    await refreshSessions();
    for (const s of created) {
      if (!openIds.includes(s.id)) openIds.push(s.id);
      openPanel(s.id);
    }
    renderPanels();
    renderSessionList();
    showRunStatus("ok", `已创建并打开 ${created.length} 个会话`);
  } catch (err) {
    alert("创建失败: " + err.message);
  } finally {
    btn.disabled = false;
  }
}

// ---------- 面板排序 ----------

function pin(id) {
  const from = openIds.indexOf(id);
  if (from <= 0) return;
  openIds.splice(from, 1);
  openIds.unshift(id);
  renderPanels();
}

function renderPanels() {
  const panelsEl = $("panels");
  // 按 openIds 顺序调整面板 DOM（appendChild 会移动已有节点，保留聊天状态）
  for (const id of openIds) {
    const el = panelEls.get(id);
    if (el) panelsEl.appendChild(el);
  }
  for (const [id, el] of [...panelEls]) {
    if (!openIds.includes(id)) {
      el.remove();
      panelEls.delete(id);
    }
  }
  panelsEl.classList.toggle("single", openIds.length === 1);
  $("empty-hint").hidden = openIds.length > 0;
}

// 拖拽排序
let dragId = null;
const panelsEl = $("panels");

panelsEl.addEventListener("dragstart", (e) => {
  const panel = e.target.closest(".panel");
  if (!panel) return;
  dragId = panel.dataset.id;
  e.dataTransfer.effectAllowed = "move";
  panel.classList.add("dragging");
});

panelsEl.addEventListener("dragover", (e) => {
  const panel = e.target.closest(".panel");
  if (!panel || panel.dataset.id === dragId) return;
  e.preventDefault();
  e.dataTransfer.dropEffect = "move";
});

panelsEl.addEventListener("drop", (e) => {
  const target = e.target.closest(".panel");
  if (!target || !dragId || target.dataset.id === dragId) return;
  e.preventDefault();
  const from = openIds.indexOf(dragId);
  const to = openIds.indexOf(target.dataset.id);
  if (from < 0 || to < 0) return;
  openIds.splice(from, 1);
  openIds.splice(openIds.indexOf(target.dataset.id), 0, dragId);
  renderPanels();
});

panelsEl.addEventListener("dragend", () => {
  dragId = null;
  document.querySelectorAll(".panel.dragging").forEach((p) => p.classList.remove("dragging"));
});

// ---------- 选项加载 ----------

async function loadOptions() {
  try {
    platforms = await bridge.apiGet("platforms");
  } catch (err) {
    console.warn("加载平台列表失败:", err);
    platforms = [];
  }
  try {
    confs = await bridge.apiGet("confs");
  } catch (err) {
    console.warn("加载配置档案失败:", err);
    confs = [];
  }
  $("create-platform").innerHTML =
    '<option value="virtual_test">virtual_test（默认）</option>' +
    platforms
      .map(
        (p) =>
          `<option value="${escapeHtml(p.id)}">${escapeHtml(p.id)}（${escapeHtml(p.display_name || p.name)}）</option>`,
      )
      .join("");
  $("create-conf").innerHTML =
    '<option value="">默认配置</option>' +
    confs
      .filter((c) => c.id !== "default")
      .map((c) => `<option value="${escapeHtml(c.id)}">${escapeHtml(c.name)}</option>`)
      .join("");
}

// ---------- 初始化 ----------

$("btn-create").addEventListener("click", createSessions);
$("btn-refresh").addEventListener("click", refreshSessions);
$("btn-run-all").addEventListener("click", sendToAll);
$("run-text").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.isComposing) sendToAll();
});

await bridge.ready();
await Promise.all([loadOptions(), refreshSessions()]);
