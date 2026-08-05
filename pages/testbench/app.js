// 会话测试台 - 页面脚本
import { createChatRenderer } from "./chat.js";
import { createAlignController } from "./align.js";
import {
  addGroupSessions,
  createGroup,
  deleteGroups,
  deleteSessions,
  getHistory,
  listConfs,
  listGroups,
  listPlatforms,
  ready,
  regenerateHistory,
  resetSessions,
  runStatus,
  runTest,
  saveHistory,
  updateGroup,
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
let expandedSessions = new Set();

const MAX_SESSIONS = 500;

// 配置档案下拉中代表「显式使用默认配置档案（不绑定）」的哨兵值；
// 与后端约定：保存时映射为 conf_id=""，"" 表示不绑定档案而非继承组配置。
const CONF_DEFAULT = "__default__";

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
    platform_id: session.platform_id || group.platform_id || "webchat",
    conf_id: confId,
    sender_id: session.sender_id || group.sender_id || "testbench",
    sender_name: session.sender_name || group.sender_name || "测试台",
    group_name: group.name,
  };
}

// ---------- 弹窗（iframe 沙箱禁用原生 alert/confirm，用自绘弹窗替代） ----------

let modalCallback = null;

function openModal({ title, content, okText = "确定", cancelText = "取消", danger = false, showCancel, wide = false, onOk, onCancel } = {}) {
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
  $("modal-mask").querySelector(".modal").classList.toggle("modal-wide", wide);
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
  updateRunOverview();
}

function renderGroupList() {
  const list = $("group-list");
  list.innerHTML = "";
  $("group-count").textContent = groups.length ? `${groups.length} 个测试组` : "";

  // 列表内「＋」块：点击创建默认配置的测试组
  const add = document.createElement("button");
  add.className = "add-block";
  add.textContent = "＋ 新建测试组";
  add.addEventListener("click", handleAddGroup);
  list.appendChild(add);

  if (!groups.length) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = "暂无测试组，点上方「＋」创建";
    list.appendChild(empty);
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
      : `<span class="badge">${escapeHtml(platformName("webchat"))}</span>`;
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
      `<button class="icon-btn" data-action="add" title="新增会话">＋</button>` +
      `<button class="icon-btn" data-action="edit" title="编辑测试组">✎</button>` +
      `<button class="icon-btn danger" data-action="delete-group" title="删除组">✕</button>` +
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
    item.querySelector('[data-action="edit"]').addEventListener("click", () => openGroupSettings(g.id));
    item.querySelector('[data-action="delete-group"]').addEventListener("click", () => deleteGroup(g.id));

    // 会话行：头部点击展开配置；行内操作按钮（打开/删除）走各自处理
    item.querySelectorAll(".session-item").forEach((sItem) => {
      const sid = sItem.dataset.id;
      const sHead = sItem.querySelector(".session-head");
      if (sHead) {
        sHead.addEventListener("click", (e) => {
          if (e.target.closest("button")) return;
          toggleSession(sid);
        });
      }
      sItem.querySelectorAll("[data-action]").forEach((btn) => {
        const action = btn.dataset.action;
        if (action === "open") btn.addEventListener("click", () => toggleOpen(sid));
        else if (action === "config") btn.addEventListener("click", () => openSettings(sid));
        else if (action === "delete") btn.addEventListener("click", () => deleteSession(sid));
      });
    });
    list.appendChild(item);
  }
}

// 统计会话中「已单独修改」的配置项（不再继承组配置）
function sessionOverrides(s) {
  const list = [];
  if (s.platform_id != null && s.platform_id !== "") list.push("平台");
  if (s.conf_id != null) list.push("档案");
  if (s.sender_id) list.push("发送者ID");
  if (s.sender_name) list.push("发送者昵称");
  return list;
}

// 会话展开的配置行：显示有效值 + 该项是「已修改」还是「继承组」
function renderSessionConfig(s, v) {
  const rows = [
    ["平台来源", s.platform_id != null && s.platform_id !== "", v.platform_id, platformName(v.platform_id)],
    ["配置档案", s.conf_id != null, v.conf_id, v.conf_id ? confName(v.conf_id) : "默认"],
    ["发送者ID", Boolean(s.sender_id), v.sender_id, v.sender_id || "—"],
    ["发送者昵称", Boolean(s.sender_name), v.sender_name, v.sender_name || "—"],
  ];
  return (
    `<div class="session-config">` +
    rows
      .map(
        ([label, over, , value]) =>
          `<div class="cfg-row">` +
          `<span class="cfg-label">${label}</span>` +
          `<code class="cfg-value">${escapeHtml(value)}</code>` +
          `<span class="chip ${over ? "override" : "inherit"}">${over ? "已修改" : "继承组"}</span>` +
          `</div>`,
      )
      .join("") +
    `<div class="cfg-foot">` +
    `<button class="btn small" data-action="config">编辑配置</button>` +
    `<span class="hint">未修改项跟随组配置</span>` +
    `</div>` +
    `</div>`
  );
}

function renderGroupSessions(g) {
  const sessions = g.sessions || [];
  if (!sessions.length) return '<div class="empty">组内暂无会话，点组名右侧「＋」添加</div>';
  return sessions
    .map((s) => {
      const v = effectiveView(s.id);
      const isOpen = openIds.includes(s.id);
      const overrides = sessionOverrides(s);
      const overBadge = overrides.length
        ? `<span class="badge warn" title="已单独修改：${escapeHtml(overrides.join("、"))}">已改${overrides.length}</span>`
        : "";
      const sExpanded = expandedSessions.has(s.id);
      return (
        `<div class="session-item" data-id="${escapeHtml(s.id)}">` +
        `<div class="session-head">` +
        `<span class="group-toggle">${sExpanded ? "▾" : "▸"}</span>` +
        `<span class="name">${escapeHtml(s.name || s.id)}</span>` +
        overBadge +
        `<span class="session-actions">` +
        `<button class="btn small" data-action="open">${isOpen ? "关闭" : "打开"}</button>` +
        `<button class="btn small danger" data-action="delete">删除</button>` +
        `</span>` +
        `</div>` +
        `<div class="session-meta">` +
        `<span class="badge">${escapeHtml(platformName(v.platform_id))}</span>` +
        (v.conf_id ? `<span class="badge conf">${escapeHtml(confName(v.conf_id))}</span>` : "") +
        `</div>` +
        (sExpanded ? renderSessionConfig(s, v) : "") +
        `</div>`
      );
    })
    .join("");
}

function toggleSession(id) {
  if (expandedSessions.has(id)) expandedSessions.delete(id);
  else expandedSessions.add(id);
  renderGroupList();
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

// 「＋」块：创建默认配置的测试组，随后弹出编辑弹窗
async function handleAddGroup() {
  try {
    const group = await createGroup({ count: 1 });
    expandedGroups.add(group.id);
    await refreshGroups();
    openGroupSettings(group.id);
  } catch (err) {
    showModal("创建失败: " + err.message);
  }
}

// 平台下拉的选项片段（真实平台列表；「默认」前缀项由各调用方自行决定）
function platformOptions() {
  return platforms
    .map(
      (p) =>
        `<option value="${escapeHtml(p.id)}">${escapeHtml(p.id)}（${escapeHtml(p.display_name || p.name)}）</option>`,
    )
    .join("");
}

// 配置档案下拉的选项数组：排除 default 档案；已绑定但已被删除的档案保留占位选项，
// 避免保存时静默丢失绑定。组编辑与会话配置弹窗共用，占位逻辑只保留此处一份。
function confOptions(current) {
  const options = confs.filter((c) => c.id !== "default");
  if (current && !options.some((c) => c.id === current)) {
    options.unshift({ id: current, name: `${current}（档案已不存在）` });
  }
  return options;
}

function buildPlatformSelect(current) {
  const sel = document.createElement("select");
  sel.innerHTML = `<option value="">默认（webchat）</option>` + platformOptions();
  if (current) sel.value = current;
  return sel;
}

function buildConfSelect(current) {
  const sel = document.createElement("select");
  sel.innerHTML =
    `<option value="">默认配置</option>` +
    confOptions(current)
      .map((c) => `<option value="${escapeHtml(c.id)}">${escapeHtml(c.name)}</option>`)
      .join("");
  if (current) sel.value = current;
  return sel;
}

// 测试组编辑弹窗：组名 / 会话数量 / 平台来源 / 配置档案 / 发送者 id 与昵称
function openGroupSettings(gid) {
  const g = groups.find((x) => x.id === gid);
  if (!g) return;
  const sessions = g.sessions || [];

  const inpName = document.createElement("input");
  inpName.type = "text";
  inpName.value = g.name || "";

  const inpCount = document.createElement("input");
  inpCount.type = "number";
  inpCount.min = "1";
  inpCount.max = String(MAX_SESSIONS);
  inpCount.value = String(Math.max(1, sessions.length));

  const selP = buildPlatformSelect(g.platform_id);
  const selC = buildConfSelect(g.conf_id);

  const inpId = document.createElement("input");
  inpId.type = "text";
  inpId.placeholder = "留空使用默认 testbench";
  inpId.value = g.sender_id || "";

  const inpName2 = document.createElement("input");
  inpName2.type = "text";
  inpName2.placeholder = "留空使用默认 测试台";
  inpName2.value = g.sender_name || "";

  const form = document.createElement("div");
  form.className = "form-col";
  form.append(
    field("组名", inpName),
    field("会话数量（保存时若少于该值将自动新增）", inpCount),
    field("平台来源", selP),
    field("配置档案", selC),
    field("发送者ID", inpId),
    field("发送者昵称", inpName2),
  );

  openModal({
    title: `编辑测试组 · ${g.name}`,
    content: form,
    okText: "保存",
    onOk: async () => {
      const count = parseInt(inpCount.value, 10);
      if (!Number.isInteger(count) || count < 1 || count > MAX_SESSIONS) {
        throw new Error(`会话数量必须是 1-${MAX_SESSIONS} 的整数`);
      }
      await updateGroup({
        id: gid,
        name: inpName.value.trim() || null,
        platform_id: selP.value || null,
        conf_id: selC.value || null,
        sender_id: inpId.value.trim() || null,
        sender_name: inpName2.value.trim() || null,
      });
      const cur = (groups.find((x) => x.id === gid) || {}).sessions || [];
      if (count > cur.length) {
        await addGroupSessions(gid, count - cur.length);
      }
      await refreshGroups();
      showRunStatus("ok", "测试组配置已更新");
    },
  });
}

function openSettings(sid) {
  const f = findSession(sid);
  if (!f) return;
  const { group, session } = f;

  const selP = document.createElement("select");
  selP.innerHTML =
    `<option value="">使用组配置（${escapeHtml(platformName(group.platform_id || "webchat"))}）</option>` +
    platformOptions();
  selP.value = session.platform_id || "";

  const selC = document.createElement("select");
  selC.innerHTML =
    `<option value="">使用组配置（${group.conf_id ? escapeHtml(confName(group.conf_id)) : "默认"}）</option>` +
    `<option value="${CONF_DEFAULT}">默认配置（不绑定档案）</option>` +
    confOptions(session.conf_id || null)
      .map((c) => `<option value="${escapeHtml(c.id)}">${escapeHtml(c.name)}</option>`)
      .join("");
  if (session.conf_id === "") selC.value = CONF_DEFAULT;
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
    title: `会话配置 · ${session.name || sid}`,
    content: form,
    okText: "保存",
    onOk: async () => {
      await updateSession({
        id: sid,
        platform_id: selP.value || null,
        conf_id: selC.value === CONF_DEFAULT ? "" : selC.value || null,
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
  const info = head.querySelector(".panel-info");
  for (const [text, cls, tip, label] of badges) {
    if (!text) continue;
    const span = document.createElement("span");
    span.className = "badge " + cls + (cls === "conf-badge" ? " conf" : "");
    if (tip) span.title = tip;
    span.textContent = label;
    info.appendChild(span);
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
    `<span class="panel-info">` +
    `<span class="panel-title">${escapeHtml(s ? s.name : id)}</span>` +
    groupBadge + platformBadge + confBadge +
    `</span>` +
    `<span class="panel-actions">` +
    `<button class="icon-btn" data-action="history" title="编辑对话历史（JSON）">编辑</button>` +
    `<button class="icon-btn" data-action="reset" title="重置对话历史">重置</button>` +
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
  panel.querySelector('[data-action="reset"]').addEventListener("click", () => resetHistory(id));
  panel.querySelector('[data-action="pin"]').addEventListener("click", () => pin(id));
  panel
    .querySelector('[data-action="history"]')
    .addEventListener("click", () => void openHistoryEditor(id));
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

// 渲染逻辑拆在 chat.js（createChatRenderer），本包装函数在初始化处注入 align 控制器
function renderChat(panel, conversations) {
  return chat.renderChat(panel, conversations);
}

// 面板头部「编辑」：以 JSON 编辑器整体查看 / 替换该会话的对话历史。
// 结构即 /sessions/<id>/history 返回的 { conversations: [...] }；编辑、
// 新增（不带 conversation_id）、删除对话都通过直接修改 JSON 完成。
async function openHistoryEditor(id) {
  // 每次打开都拉取最新历史，避免缓存为空时保存把全部对话误删
  let convs;
  try {
    const data = await getHistory(id);
    convs = data.conversations || [];
  } catch (err) {
    showModal("加载对话历史失败: " + err.message);
    return;
  }
  const s = effectiveView(id);
  const ta = document.createElement("textarea");
  ta.className = "json-editor";
  ta.value = JSON.stringify({ conversations: convs }, null, 2);
  const hint = document.createElement("p");
  hint.className = "hint";
  hint.textContent =
    "保存将用此 JSON 完全替换对话历史：未列出的对话会被删除，不带 conversation_id 的对象会新建对话，conversation_id 在库中已不存在的对象也会新建占位对话。仅建议有能力的用户修改；改坏可用会话的「重置」恢复。";
  const wrap = document.createElement("div");
  wrap.className = "form-col";
  wrap.append(ta, hint);
  openModal({
    title: `编辑对话历史 · ${s ? s.name : id}`,
    content: wrap,
    okText: "保存",
    wide: true,
    onOk: async () => {
      let parsed;
      try {
        parsed = JSON.parse(ta.value);
      } catch (err) {
        throw new Error("JSON 解析失败: " + err.message);
      }
      if (!parsed || !Array.isArray(parsed.conversations)) {
        throw new Error("JSON 必须是 { conversations: [...] } 结构");
      }
      await saveHistory({ id, conversations: parsed.conversations });
      void loadHistory(id);
      showRunStatus("ok", "对话历史已保存");
    },
  });
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

// 群发栏实时显示：当前打开的会话总数 + 按所属测试组的分布
function updateRunOverview() {
  const el = $("run-overview");
  if (!openIds.length) {
    el.hidden = true;
    return;
  }
  const counts = new Map();
  for (const id of openIds) {
    const v = effectiveView(id);
    if (!v) continue;
    const name = v.group_name || "未分组";
    counts.set(name, (counts.get(name) || 0) + 1);
  }
  let html = `<span class="overview-total">当前会话:${openIds.length}</span>`;
  for (const [name, n] of counts) {
    html += `<span class="overview-item">${escapeHtml(name)}:${n}</span>`;
  }
  el.hidden = false;
  el.innerHTML = html;
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
  updateRunOverview();
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

// 气泡悬停操作：重新生成某轮（整体历史的编辑走面板头部「编辑」）
panelsEl.addEventListener("click", (e) => {
  const btn = e.target.closest('[data-action="regenerate"]');
  if (!btn) return;
  const msgEl = btn.closest(".msg");
  if (!msgEl) return;
  const index = parseInt(msgEl.dataset.index, 10);
  if (Number.isNaN(index)) return;
  void regenerateMsg(btn.closest(".panel").dataset.id, index);
});

// ---------- 轮次对齐 ----------
// 对齐逻辑已拆到 align.js（createAlignController），在初始化处创建并绑定事件。

// ---------- 选项加载 ----------

async function loadOptions() {
  try {
    const data = await listPlatforms();
    platforms = Array.isArray(data) ? data : [];
  } catch (err) {
    console.warn("加载平台列表失败:", err);
    platforms = [];
  }
  try {
    const data = await listConfs();
    confs = Array.isArray(data) ? data : [];
  } catch (err) {
    console.warn("加载配置档案失败:", err);
    confs = [];
  }
}

// ---------- 初始化 ----------

$("btn-refresh").addEventListener("click", refreshGroups);
$("btn-run-all").addEventListener("click", sendToAll);
$("run-text").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.isComposing) sendToAll();
});

// UI 窄条：会话列表按钮折叠/展开左侧列表（为后续扩展的其他视图预留）
const railSessionBtn = document.querySelector('.rail-btn[data-view="sessions"]');
railSessionBtn.addEventListener("click", () => {
  const collapsed = document.body.classList.toggle("sidebar-collapsed");
  railSessionBtn.classList.toggle("active", !collapsed);
});

const align = createAlignController({
  getOpenIds: () => openIds,
  getPanelEls: () => panelEls,
  getHistoryCache: () => historyCache,
  getPanelsEl: () => panelsEl,
  renderChat,
});
align.attachEvents();

const chat = createChatRenderer(() => align);

await ready();
await Promise.all([loadOptions(), refreshGroups()]);
