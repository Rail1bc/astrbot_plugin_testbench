// 会话测试台 - 页面脚本
// 通过 window.AstrBotPluginPage bridge 与插件后端通信。
const bridge = window.AstrBotPluginPage;

const $ = (id) => document.getElementById(id);

let sessions = [];
let platforms = [];
let confs = [];
let openIds = [];
let pinnedIds = [];
const panelEls = new Map();
const historyCache = new Map();
let runBusy = false;
let alignMode = false;
let alignTurn = 1;
const TURN_H = 150;

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

// ---------- 弹窗（iframe 沙箱禁用原生 alert/confirm，用自绘弹窗替代） ----------

let modalCallback = null;

function showModal(text, { okText = "确定", danger = false, onOk } = {}) {
  $("modal-text").textContent = text;
  $("modal-ok").textContent = okText;
  $("modal-ok").classList.toggle("danger", danger);
  $("modal-cancel").hidden = !onOk;
  modalCallback = onOk || null;
  $("modal-mask").hidden = false;
}

function hideModal() {
  $("modal-mask").hidden = true;
  modalCallback = null;
}

$("modal-ok").addEventListener("click", () => {
  const cb = modalCallback;
  hideModal();
  if (cb) cb();
});

$("modal-cancel").addEventListener("click", hideModal);

$("modal-mask").addEventListener("click", (e) => {
  if (e.target === $("modal-mask")) hideModal();
});

// ---------- 会话列表 ----------

async function refreshSessions() {
  sessions = await bridge.apiGet("sessions");
  // 清理已被删除的会话面板
  const valid = new Set(sessions.map((s) => s.id));
  const removed = openIds.filter((id) => !valid.has(id));
  if (removed.length) {
    openIds = openIds.filter((id) => valid.has(id));
    pinnedIds = pinnedIds.filter((id) => valid.has(id));
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
    pinnedIds = pinnedIds.filter((x) => x !== id);
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
    const conversations = data.conversations || [];
    historyCache.set(id, conversations);
    renderChat(panel, conversations);
    if (alignMode) refreshAlign();
  } catch (err) {
    chat.innerHTML = `<div class="empty">加载历史失败: ${escapeHtml(err.message)}</div>`;
  }
}

function renderChat(panel, conversations) {
  if (alignMode) renderAligned(panel, conversations);
  else renderHistory(panel, conversations);
}

function renderHistory(panel, conversations) {
  const chat = panel.querySelector(".chat");
  chat.innerHTML = "";
  chat.classList.remove("aligned");
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

// 把消息历史按轮次分组：每个 user 发言开启新的一轮，期间的推理/工具调用/回复都属于该轮
function groupTurns(history) {
  const turns = [];
  let cur = null;
  for (const msg of history || []) {
    if ((msg.role || "") === "user") {
      cur = { messages: [] };
      turns.push(cur);
    } else if (!cur) {
      cur = { messages: [] };
      turns.push(cur);
    }
    cur.messages.push(msg);
  }
  return turns;
}

// 轮次对齐模式：每轮固定行高，行内内容超高时行内滚动，保证所有面板按轮次对齐
function renderAligned(panel, conversations) {
  const chat = panel.querySelector(".chat");
  chat.innerHTML = "";
  chat.classList.add("aligned");
  let idx = 0;
  for (const conv of conversations) {
    for (const turn of groupTurns(conv.history)) {
      const row = document.createElement("div");
      row.className = "turn-row";
      const label = document.createElement("div");
      label.className = "turn-label";
      label.textContent = `轮次 ${idx + 1}`;
      row.appendChild(label);
      const body = document.createElement("div");
      body.className = "turn-body";
      for (const msg of turn.messages) body.appendChild(bubbleFor(msg));
      row.appendChild(body);
      chat.appendChild(row);
      idx++;
    }
  }
  if (!idx) {
    const p = document.createElement("div");
    p.className = "empty";
    p.textContent = "暂无对话历史";
    chat.appendChild(p);
  }
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

function resetHistory(id) {
  showModal(`确定重置会话 ${id} 的对话历史吗？`, {
    danger: true,
    onOk: async () => {
      const resp = await bridge.apiPost("reset", { ids: [id] });
      const panel = panelEls.get(id);
      if (panel) {
        clearPanelStatus(panel);
        void loadHistory(id);
      }
      showRunStatus("ok", `已重置 ${resp.reset} 个会话的对话历史`);
    },
  });
}

function deleteSession(id) {
  showModal(`确定删除会话 ${id} 吗？`, {
    danger: true,
    onOk: async () => {
      await bridge.apiPost("sessions/delete", { ids: [id] });
      openIds = openIds.filter((x) => x !== id);
      pinnedIds = pinnedIds.filter((x) => x !== id);
      renderPanels();
      await refreshSessions();
    },
  });
}

// ---------- 创建 ----------

async function createSessions() {
  const count = parseInt($("create-count").value, 10);
  if (!Number.isInteger(count) || count < 1) {
    showModal("数量必须是大于 0 的整数");
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
    showModal("创建失败: " + err.message);
  } finally {
    btn.disabled = false;
  }
}

// ---------- 面板排序 ----------

function visibleOrder() {
  return [
    ...pinnedIds.filter((id) => openIds.includes(id)),
    ...openIds.filter((id) => !pinnedIds.includes(id)),
  ];
}

// 置顶是开关：置顶的面板固定在最前，再次点击取消置顶
function pin(id) {
  const i = pinnedIds.indexOf(id);
  if (i >= 0) pinnedIds.splice(i, 1);
  else pinnedIds.unshift(id);
  renderPanels();
}

function renderPanels() {
  const panelsEl = $("panels");
  // 置顶面板在最前，其余按打开顺序（appendChild 移动已有节点，保留聊天状态）
  for (const id of visibleOrder()) {
    const el = panelEls.get(id);
    if (el) panelsEl.appendChild(el);
  }
  for (const [id, el] of [...panelEls]) {
    if (!openIds.includes(id)) {
      el.remove();
      panelEls.delete(id);
    }
  }
  // 更新置顶按钮的开关视觉状态
  for (const [id, el] of panelEls) {
    const btn = el.querySelector('[data-action="pin"]');
    const isPinned = pinnedIds.includes(id);
    btn.classList.toggle("active", isPinned);
    btn.title = isPinned ? "取消置顶" : "置顶";
  }
  panelsEl.classList.toggle("single", openIds.length === 1);
  $("empty-hint").hidden = openIds.length > 0;
  $("align-bar").hidden = !alignMode || openIds.length === 0;
  if (alignMode) refreshAlign();
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
  const order = visibleOrder();
  const from = order.indexOf(dragId);
  const to = order.indexOf(target.dataset.id);
  if (from < 0 || to < 0) return;
  order.splice(from, 1);
  order.splice(order.indexOf(target.dataset.id), 0, dragId);
  // 回写顺序，同时保持各面板的置顶状态
  const pinnedSet = new Set(pinnedIds);
  pinnedIds = order.filter((id) => pinnedSet.has(id));
  openIds = order;
  renderPanels();
});

panelsEl.addEventListener("dragend", () => {
  dragId = null;
  document.querySelectorAll(".panel.dragging").forEach((p) => p.classList.remove("dragging"));
});

// ---------- 轮次对齐 ----------

function alignMaxTurns() {
  let m = 1;
  for (const id of openIds) {
    const panel = panelEls.get(id);
    if (!panel) continue;
    m = Math.max(m, panel.querySelectorAll(".turn-row").length);
  }
  return m;
}

// 把统一滑动条定位到指定轮次，同步所有面板的滚动位置
function setTurn(t, { force = false } = {}) {
  if (!alignMode) return;
  const max = alignMaxTurns();
  t = Math.max(1, Math.min(max, Math.round(t)));
  if (!force && t === alignTurn) return;
  alignTurn = t;
  const top = (t - 1) * TURN_H;
  for (const [, panel] of panelEls) {
    const chat = panel.querySelector(".chat");
    if (chat) chat.scrollTop = top;
  }
  $("align-slider").value = String(t);
  $("align-turn-label").textContent = `轮次 ${t}/${max}`;
}

function refreshAlign() {
  if (!alignMode) return;
  $("align-slider").max = String(alignMaxTurns());
  setTurn(alignTurn, { force: true });
}

function applyAlignMode() {
  alignMode = $("align-toggle").checked;
  $("panels").classList.toggle("align", alignMode);
  $("align-bar").hidden = !alignMode || openIds.length === 0;
  for (const id of openIds) {
    const panel = panelEls.get(id);
    if (!panel) continue;
    renderChat(panel, historyCache.get(id) || []);
  }
  if (alignMode) {
    alignTurn = 1;
    refreshAlign();
  }
}

$("align-toggle").addEventListener("change", applyAlignMode);

$("align-slider").addEventListener("input", () => {
  setTurn(parseInt($("align-slider").value, 10), { force: true });
});

// 对齐模式下滚轮同步滚动所有窗口；行内还有可滚动内容时优先行内滚动
let wheelAccum = 0;
panelsEl.addEventListener(
  "wheel",
  (e) => {
    if (!alignMode) return;
    const body = e.target.closest(".turn-body");
    if (body) {
      const canDown = body.scrollTop + body.clientHeight < body.scrollHeight - 1;
      const canUp = body.scrollTop > 0;
      if ((e.deltaY > 0 && canDown) || (e.deltaY < 0 && canUp)) return;
    }
    e.preventDefault();
    wheelAccum += e.deltaY;
    const STEP = 60;
    while (wheelAccum >= STEP) {
      wheelAccum -= STEP;
      setTurn(alignTurn + 1);
    }
    while (wheelAccum <= -STEP) {
      wheelAccum += STEP;
      setTurn(alignTurn - 1);
    }
  },
  { passive: false },
);

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
