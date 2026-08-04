// 会话测试台 - 页面脚本
import { createAlignController } from "./align.js";
import {
  addGroupSessions,
  createGroup,
  deleteGroups,
  deleteSessions,
  editHistory,
  getHistory,
  listConfs,
  listGroups,
  listPlatforms,
  ready,
  regenerateHistory,
  resetSessions,
  runStatus,
  runTest,
  updateSession,
} from "./api.js";

const $ = (id) => document.getElementById(id);

let groups = [];
let platforms = [];
let confs = [];
let openIds = [];
let pinnedIds = [];
const panelEls = new Map();
const historyCache = new Map();
let runBusy = false;
let expandedGroups = new Set();

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

function findSession(id) {
  for (const g of groups) {
    const s = (g.sessions || []).find((x) => x.id === id);
    if (s) return { group: g, session: s };
  }
  return null;
}

// 解析会话的最终配置（组配置 + 会话覆盖）
function effectiveView(id) {
  const f = findSession(id);
  if (!f) return null;
  const { group, session } = f;
  const confId =
    session.conf_id === undefined || session.conf_id === null
      ? group.conf_id
      : session.conf_id || null;
  return {
    id: session.id,
    name: session.name || session.id,
    platform_id: session.platform_id || group.platform_id || "virtual_test",
    conf_id: confId,
    group_name: group.name,
  };
}

// ---------- 弹窗（iframe 沙箱禁用原生 alert/confirm，用自绘弹窗替代） ----------

let modalCallback = null;

function openModal({ title, content, okText = "确定", cancelText = "取消", danger = false, showCancel, onOk, onCancel } = {}) {
  const body = $("modal-body");
  body.innerHTML = "";
  if (title) {
    const h = document.createElement("div");
    h.className = "modal-title";
    h.textContent = title;
    body.appendChild(h);
  }
  if (typeof content === "string") {
    const p = document.createElement("p");
    p.textContent = content;
    body.appendChild(p);
  } else if (content) {
    body.appendChild(content);
  }
  // 无 onOk 的纯提示弹窗默认只显示确定按钮
  const cancel = showCancel === undefined ? Boolean(onOk) : showCancel;
  $("modal-ok").textContent = okText;
  $("modal-ok").classList.toggle("danger", danger);
  $("modal-cancel").textContent = cancelText;
  $("modal-cancel").hidden = !cancel;
  modalCallback = { onOk: onOk || null, onCancel: onCancel || null };
  $("modal-mask").hidden = false;
  const first = body.querySelector("input, select, textarea");
  if (first) first.focus();
}

function showModal(text, opts = {}) {
  openModal({ content: text, ...opts });
}

function hideModal() {
  $("modal-mask").hidden = true;
  modalCallback = null;
}

$("modal-ok").addEventListener("click", async () => {
  const cb = modalCallback;
  hideModal();
  if (!cb || !cb.onOk) return;
  try {
    await cb.onOk();
  } catch (err) {
    showRunStatus("error", "操作失败: " + err.message);
  }
});

$("modal-cancel").addEventListener("click", () => {
  const cb = modalCallback;
  hideModal();
  if (cb && cb.onCancel) cb.onCancel();
});

$("modal-mask").addEventListener("click", (e) => {
  if (e.target === $("modal-mask")) hideModal();
});

// ---------- 测试组列表 ----------

async function refreshGroups() {
  const data = await listGroups();
  groups = data.groups || [];
  // 清理已被删除的会话面板
  const valid = new Set();
  for (const g of groups) for (const s of g.sessions || []) valid.add(s.id);
  const removed = openIds.filter((id) => !valid.has(id));
  if (removed.length) {
    openIds = openIds.filter((id) => valid.has(id));
    pinnedIds = pinnedIds.filter((id) => valid.has(id));
    renderPanels();
  }
  renderGroupList();
}

function renderGroupList() {
  const list = $("group-list");
  list.innerHTML = "";
  $("group-count").textContent = groups.length ? `${groups.length} 个测试组` : "";
  if (!groups.length) {
    list.innerHTML = '<div class="empty">暂无测试组，请先创建</div>';
    return;
  }
  for (const g of groups) {
    const item = document.createElement("div");
    item.className = "group-item";
    item.dataset.id = g.id;
    const expanded = expandedGroups.has(g.id);
    const sessions = g.sessions || [];
    const platformBadge = g.platform_id
      ? `<span class="badge">${escapeHtml(platformName(g.platform_id))}</span>`
      : `<span class="badge">${escapeHtml(platformName("virtual_test"))}</span>`;
    const confBadge = g.conf_id
      ? `<span class="badge conf">${escapeHtml(confName(g.conf_id))}</span>`
      : "";
    item.innerHTML =
      `<div class="group-head">` +
      `<span class="group-toggle">${expanded ? "▾" : "▸"}</span>` +
      `<span class="group-name">${escapeHtml(g.name)}</span>` +
      `<span class="badge">${sessions.length} 会话</span>` +
      `<span class="group-actions">` +
      `<button class="btn small" data-action="open-all">打开全部</button>` +
      `<button class="btn small" data-action="add">新增会话</button>` +
      `<button class="btn small danger" data-action="delete-group">删除组</button>` +
      `</span>` +
      `</div>` +
      `<div class="group-meta">${platformBadge}${confBadge}</div>` +
      (expanded
        ? `<div class="group-sessions">${renderGroupSessions(g)}</div>`
        : "");

    const head = item.querySelector(".group-head");
    head.addEventListener("click", (e) => {
      if (e.target.closest("button")) return;
      toggleGroup(g.id);
    });
    item.querySelector('[data-action="open-all"]').addEventListener("click", () => openAll(g.id));
    item.querySelector('[data-action="add"]').addEventListener("click", () => promptAddSessions(g.id));
    item.querySelector('[data-action="delete-group"]').addEventListener("click", () => deleteGroup(g.id));

    // 会话行操作
    item.querySelectorAll(".session-item [data-action]").forEach((btn) => {
      const sid = btn.closest(".session-item").dataset.id;
      const action = btn.dataset.action;
      if (action === "open") btn.addEventListener("click", () => toggleOpen(sid));
      else if (action === "config") btn.addEventListener("click", () => openSettings(sid));
      else if (action === "reset") btn.addEventListener("click", () => resetHistory(sid));
      else if (action === "delete") btn.addEventListener("click", () => deleteSession(sid));
    });
    list.appendChild(item);
  }
}

function renderGroupSessions(g) {
  const sessions = g.sessions || [];
  if (!sessions.length) return '<div class="empty">组内暂无会话，点「新增会话」添加</div>';
  return sessions
    .map((s) => {
      const v = effectiveView(s.id);
      const isOpen = openIds.includes(s.id);
      const overrides = [
        ["平台", s.platform_id],
        ["档案", s.conf_id === "" ? "默认(不绑定)" : s.conf_id],
        ["发送者", s.sender_id || s.sender_name],
      ].filter(([, val]) => val);
      const overBadge = overrides.length
        ? `<span class="badge warn" title="覆盖组配置：${escapeHtml(overrides.map(([k, val]) => `${k}=${val}`).join(", "))}">覆盖${overrides.length}</span>`
        : "";
      return (
        `<div class="session-item" data-id="${escapeHtml(s.id)}">` +
        `<div class="name">${escapeHtml(s.name || s.id)}</div>` +
        `<div class="session-meta">` +
        `<span class="badge">${escapeHtml(platformName(v.platform_id))}</span>` +
        (v.conf_id ? `<span class="badge conf">${escapeHtml(confName(v.conf_id))}</span>` : "") +
        overBadge +
        `</div>` +
        `<div class="session-actions">` +
        `<button class="btn small" data-action="open">${isOpen ? "关闭" : "打开"}</button>` +
        `<button class="btn small" data-action="config">设置</button>` +
        `<button class="btn small" data-action="reset">重置</button>` +
        `<button class="btn small danger" data-action="delete">删除</button>` +
        `</div>` +
        `</div>`
      );
    })
    .join("");
}

function toggleGroup(id) {
  if (expandedGroups.has(id)) expandedGroups.delete(id);
  else expandedGroups.add(id);
  renderGroupList();
}

function openAll(gid) {
  const g = groups.find((x) => x.id === gid);
  if (!g) return;
  for (const s of g.sessions || []) {
    if (!openIds.includes(s.id)) {
      openIds.push(s.id);
      openPanel(s.id);
    }
  }
  renderPanels();
  renderGroupList();
}

function promptAddSessions(gid) {
  const input = document.createElement("input");
  input.type = "number";
  input.min = "1";
  input.max = "500";
  input.value = "5";
  openModal({
    title: "新增会话",
    content: field("数量", input),
    okText: "新增",
    onOk: async () => {
      const n = parseInt(input.value, 10);
      if (!Number.isInteger(n) || n < 1) {
        showModal("数量必须是大于 0 的整数");
        return;
      }
      const created = await addGroupSessions(gid, n);
      await refreshGroups();
      showRunStatus("ok", `已新增 ${created.length} 个会话`);
    },
  });
}

function deleteGroup(gid) {
  const g = groups.find((x) => x.id === gid);
  showModal(
    `确定删除测试组「${g ? g.name : gid}」及其全部 ${g ? (g.sessions || []).length : 0} 个会话吗？`,
    {
      danger: true,
      onOk: async () => {
        await deleteGroups([gid]);
        for (const s of g.sessions || []) {
          openIds = openIds.filter((x) => x !== s.id);
          pinnedIds = pinnedIds.filter((x) => x !== s.id);
        }
        renderPanels();
        await refreshGroups();
      },
    },
  );
}

function openSettings(sid) {
  const f = findSession(sid);
  if (!f) return;
  const { group, session } = f;

  const selP = document.createElement("select");
  selP.innerHTML =
    `<option value="">使用组配置（${escapeHtml(platformName(group.platform_id || "virtual_test"))}）</option>` +
    `<option value="virtual_test">virtual_test（默认）</option>` +
    platforms
      .map(
        (p) =>
          `<option value="${escapeHtml(p.id)}">${escapeHtml(p.id)}（${escapeHtml(p.display_name || p.name)}）</option>`,
      )
      .join("");
  selP.value = session.platform_id || "";

  const selC = document.createElement("select");
  selC.innerHTML =
    `<option value="">使用组配置（${group.conf_id ? escapeHtml(confName(group.conf_id)) : "默认"}）</option>` +
    `<option value="__default__">默认配置（不绑定档案）</option>` +
    confs
      .filter((c) => c.id !== "default")
      .map((c) => `<option value="${escapeHtml(c.id)}">${escapeHtml(c.name)}</option>`)
      .join("");
  if (session.conf_id === "") selC.value = "__default__";
  else if (session.conf_id) selC.value = session.conf_id;
  else selC.value = "";

  const inpId = document.createElement("input");
  inpId.type = "text";
  inpId.placeholder = "留空使用组配置";
  inpId.value = session.sender_id || "";

  const inpName = document.createElement("input");
  inpName.type = "text";
  inpName.placeholder = "留空使用组配置";
  inpName.value = session.sender_name || "";

  const form = document.createElement("div");
  form.className = "form-col";
  form.append(
    field("平台来源（覆盖）", selP),
    field("配置档案（覆盖）", selC),
    field("发送者ID", inpId),
    field("发送者昵称", inpName),
  );

  openModal({
    title: `会话配置 · ${s.name || sid}`,
    content: form,
    okText: "保存",
    onOk: async () => {
      await updateSession({
        id: sid,
        platform_id: selP.value || null,
        conf_id: selC.value === "__default__" ? "" : selC.value || null,
        sender_id: inpId.value.trim() || null,
        sender_name: inpName.value.trim() || null,
      });
      await refreshGroups();
      refreshPanelHead(sid); // 已打开面板同步标题与徽标（聊天内容保留）
      showRunStatus("ok", "会话配置已更新");
    },
  });
}

// 会话配置变更后刷新已打开面板的标题与徽标（保留聊天内容与状态）
function refreshPanelHead(id) {
  const panel = panelEls.get(id);
  if (!panel) return;
  const s = effectiveView(id);
  const head = panel.querySelector(".panel-head");
  head.querySelector(".panel-title").textContent = s ? s.name : id;
  head.querySelectorAll(".badge").forEach((b) => b.remove());
  if (!s) return;
  const badges = [
    [s.group_name, "group-badge", "所属测试组", escapeHtml(s.group_name || "")],
    [s.platform_id, "platform-badge", "", escapeHtml(platformName(s.platform_id))],
    [s.conf_id, "conf-badge", "", escapeHtml(confName(s.conf_id))],
  ];
  const actions = head.querySelector(".panel-actions");
  for (const [text, cls, tip, label] of badges) {
    if (!text) continue;
    const span = document.createElement("span");
    span.className = "badge " + cls + (cls === "conf-badge" ? " conf" : "");
    if (tip) span.title = tip;
    span.textContent = label;
    head.insertBefore(span, actions);
  }
}

function field(label, input) {
  const l = document.createElement("label");
  l.className = "settings-field";
  const span = document.createElement("span");
  span.textContent = label;
  l.appendChild(span);
  l.appendChild(input);
  return l;
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
  renderGroupList();
}

function openPanel(id) {
  if (panelEls.has(id)) return;
  const panel = document.createElement("div");
  panel.className = "panel";
  panel.dataset.id = id;
  panel.draggable = true;

  const s = effectiveView(id);
  const confBadge = s && s.conf_id
    ? `<span class="badge conf-badge conf">${escapeHtml(confName(s.conf_id))}</span>`
    : "";
  const platformBadge = s
    ? `<span class="badge platform-badge">${escapeHtml(platformName(s.platform_id))}</span>`
    : "";
  const groupBadge = s
    ? `<span class="badge group-badge" title="所属测试组">${escapeHtml(s.group_name || "")}</span>`
    : "";

  panel.innerHTML =
    `<div class="panel-head" title="拖拽排序">` +
    `<span class="drag-handle">≡</span>` +
    `<span class="panel-title">${escapeHtml(s ? s.name : id)}</span>` +
    groupBadge + platformBadge + confBadge +
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
    const data = await getHistory(id);
    const conversations = data.conversations || [];
    historyCache.set(id, conversations);
    renderChat(panel, conversations);
    if (align.isAlignMode()) align.reflowAlign();
  } catch (err) {
    chat.innerHTML = `<div class="empty">加载历史失败: ${escapeHtml(err.message)}</div>`;
  }
}

function renderChat(panel, conversations) {
  if (align.isAlignMode()) renderAligned(panel, conversations);
  else renderHistory(panel, conversations);
}

function renderHistory(panel, conversations) {
  const chat = panel.querySelector(".chat");
  chat.innerHTML = "";
  chat.classList.remove("aligned");
  let count = 0;
  let idx = 0;
  for (const conv of conversations) {
    for (const msg of conv.history || []) {
      count++;
      chat.appendChild(bubbleFor(msg, idx));
      idx++;
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

// 轮次对齐模式：保留连续气泡流，每轮包一层 turn-wrap，高度由 reflowAlign 统一为各面板该轮的最大值
function renderAligned(panel, conversations) {
  const chat = panel.querySelector(".chat");
  chat.innerHTML = "";
  chat.classList.add("aligned");
  let count = 0;
  let idx = 0;
  for (const conv of conversations) {
    for (const turn of groupTurns(conv.history)) {
      const wrap = document.createElement("div");
      wrap.className = "turn-wrap";
      for (const msg of turn.messages) {
        wrap.appendChild(bubbleFor(msg, idx));
        idx++;
      }
      chat.appendChild(wrap);
      count++;
    }
  }
  if (!count) {
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

function bubbleFor(msg, index) {
  const role = msg.role || "";
  const text = extractText(msg.content);
  const el = document.createElement("div");
  el.dataset.index = String(index);
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
  // 悬停操作：编辑（全部消息）+ 重新生成（仅 user 发言）
  const actions = document.createElement("div");
  actions.className = "msg-actions";
  const editBtn = document.createElement("button");
  editBtn.type = "button";
  editBtn.className = "icon-btn";
  editBtn.dataset.action = "edit";
  editBtn.textContent = "编辑";
  actions.appendChild(editBtn);
  if (role === "user") {
    const regenBtn = document.createElement("button");
    regenBtn.type = "button";
    regenBtn.className = "icon-btn";
    regenBtn.dataset.action = "regenerate";
    regenBtn.textContent = "重新生成";
    actions.appendChild(regenBtn);
  }
  el.appendChild(actions);
  return el;
}

// 在会话历史（conversations）中按全局索引取消息
function historyMsgAt(conversations, index) {
  let i = 0;
  for (const conv of conversations || []) {
    for (const m of conv.history || []) {
      if (i === index) return m;
      i++;
    }
  }
  return null;
}

function startEditMsg(panel, index) {
  const id = panel.dataset.id;
  const convs = historyCache.get(id) || [];
  const msg = historyMsgAt(convs, index);
  const msgEl = panel.querySelector(`.msg[data-index="${index}"]`);
  if (!msg || !msgEl || msgEl.querySelector(".msg-edit")) return;
  const wrap = document.createElement("div");
  wrap.className = "msg-edit";
  const ta = document.createElement("textarea");
  ta.className = "msg-edit-input";
  ta.value = extractText(msg.content);
  const row = document.createElement("div");
  row.className = "msg-edit-actions";
  const save = document.createElement("button");
  save.type = "button";
  save.className = "btn primary small";
  save.textContent = "保存";
  const cancel = document.createElement("button");
  cancel.type = "button";
  cancel.className = "btn small";
  cancel.textContent = "取消";
  row.append(save, cancel);
  wrap.append(ta, row);
  msgEl.innerHTML = "";
  msgEl.appendChild(wrap);
  save.addEventListener("click", () => void saveEditMsg(id, index, ta.value));
  cancel.addEventListener("click", () => renderChat(panel, convs));
  ta.focus();
}

async function saveEditMsg(id, index, content) {
  const panel = panelEls.get(id);
  try {
    await editHistory({ id, index, content });
  } catch (err) {
    if (panel) panelStatus(panel, "error", "保存失败: " + err.message);
    return;
  }
  void loadHistory(id);
}

async function regenerateMsg(id, index) {
  const panel = panelEls.get(id);
  try {
    const resp = await regenerateHistory({ id, index });
    if (panel) panelStatus(panel, "warn", "重新生成中…");
    pollRun(
      resp.test_id,
      (r) => {
        const p = panelEls.get(id);
        if (!p) return;
        if (r.status === "ok") {
          panelStatus(p, "ok", `重新生成完成（${r.duration}s）`);
        } else {
          panelStatus(
            p,
            r.status === "error" ? "error" : "warn",
            statusText(r.status) + (r.error ? `：${r.error}` : ""),
          );
        }
        void loadHistory(id);
      },
      () => {},
    );
  } catch (err) {
    if (panel) panelStatus(panel, "error", "重新生成失败: " + err.message);
    void loadHistory(id); // 后端可能已截断历史，刷新展示
  }
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

// 轮询测试运行状态：每个会话完成时回调 onSession（单独刷新），全部完成回调 onAll
function pollRun(testId, onSession, onAll) {
  const seen = new Set();
  let stopped = false;
  const timer = setInterval(tick, 1000);
  async function tick() {
    if (stopped) return;
    let record;
    try {
      record = await runStatus(testId);
    } catch (err) {
      return; // 查询失败下轮重试
    }
    for (const r of record.results || []) {
      if (!seen.has(r.session_id)) {
        seen.add(r.session_id);
        try {
          onSession(r);
        } catch (err) {
          console.error("会话结果刷新失败:", err);
        }
      }
    }
    if (record.done) {
      stopped = true;
      clearInterval(timer);
      onAll(record);
    }
  }
  void tick();
}

async function sendToOne(id, text) {
  text = (text || "").trim();
  if (!text) return;
  const panel = panelEls.get(id);
  const input = panel.querySelector(".msg-input");
  input.value = "";
  panelStatus(panel, "warn", "发送中…");
  try {
    const resp = await runTest({ sessions: [id], text });
    pollRun(
      resp.test_id,
      (r) => {
        const p = panelEls.get(id);
        if (!p) return;
        if (r.status === "ok") {
          panelStatus(p, "ok", `回复成功（${r.duration}s）`);
        } else {
          panelStatus(
            p,
            r.status === "error" ? "error" : "warn",
            statusText(r.status) + (r.error ? `：${r.error}` : ""),
          );
        }
        void loadHistory(id);
      },
      () => {},
    );
  } catch (err) {
    panelStatus(panel, "error", "发送失败: " + err.message);
  }
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
    const resp = await runTest({ sessions: ids, text });
    pollRun(
      resp.test_id,
      (r) => {
        const panel = panelEls.get(r.session_id);
        if (panel) {
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
        void loadHistory(r.session_id);
      },
      (record) => {
        const s = record.stats || {};
        const ok = record.results.filter((r) => r.status === "ok").length;
        const noReply = record.results.filter((r) => r.status === "no_reply").length;
        const err = record.results.filter((r) => r.status === "error").length;
        showRunStatus(
          err ? "warn" : "ok",
          `完成：成功 ${ok} / 无回复 ${noReply} / 错误 ${err}` +
            `，耗时 avg ${s.avg}s，p95 ${s.p95}s`,
        );
        runBusy = false;
        $("btn-run-all").disabled = false;
        $("btn-run-all").textContent = "发送到全部";
      },
    );
  } catch (err) {
    showRunStatus("error", "发送失败: " + err.message);
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
      const resp = await resetSessions([id]);
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
  const v = effectiveView(id);
  showModal(`确定删除会话 ${v ? v.name : id} 吗？`, {
    danger: true,
    onOk: async () => {
      await deleteSessions([id]);
      openIds = openIds.filter((x) => x !== id);
      pinnedIds = pinnedIds.filter((x) => x !== id);
      renderPanels();
      await refreshGroups();
    },
  });
}

// ---------- 创建 ----------

async function createGroup() {
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
    const group = await createGroup({
      name: $("create-group-name").value,
      count,
      platform_id: platformId && platformId !== "virtual_test" ? platformId : undefined,
      conf_id: confId || undefined,
      sender_id: $("create-sender-id").value || undefined,
      sender_name: $("create-sender-name").value || undefined,
      name_prefix: $("create-name-prefix").value || undefined,
    });
    await refreshGroups();
    expandedGroups.add(group.id);
    renderGroupList();
    showRunStatus("ok", `已创建测试组「${group.name}」，含 ${(group.sessions || []).length} 个会话`);
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
  $("align-bar").hidden = !align.isAlignMode() || openIds.length === 0;
  if (align.isAlignMode()) align.reflowAlign();
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

// 气泡悬停操作：编辑历史消息 / 重新生成某轮
panelsEl.addEventListener("click", (e) => {
  const btn = e.target.closest('[data-action="edit"], [data-action="regenerate"]');
  if (!btn) return;
  const panel = btn.closest(".panel");
  const msgEl = btn.closest(".msg");
  if (!panel || !msgEl) return;
  const index = parseInt(msgEl.dataset.index, 10);
  if (Number.isNaN(index)) return;
  if (btn.dataset.action === "edit") startEditMsg(panel, index);
  else if (btn.dataset.action === "regenerate") void regenerateMsg(panel.dataset.id, index);
});

// ---------- 轮次对齐 ----------
// 对齐逻辑已拆到 align.js（createAlignController），在初始化处创建并绑定事件。

// ---------- 选项加载 ----------

async function loadOptions() {
  try {
    platforms = await listPlatforms();
  } catch (err) {
    console.warn("加载平台列表失败:", err);
    platforms = [];
  }
  try {
    confs = await listConfs();
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

$("btn-create").addEventListener("click", createGroup);
$("btn-refresh").addEventListener("click", refreshGroups);
$("btn-run-all").addEventListener("click", sendToAll);
$("run-text").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.isComposing) sendToAll();
});

const align = createAlignController({
  getOpenIds: () => openIds,
  getPanelEls: () => panelEls,
  getHistoryCache: () => historyCache,
  getPanelsEl: () => panelsEl,
  renderChat,
});
align.attachEvents();

await ready();
await Promise.all([loadOptions(), refreshGroups()]);
