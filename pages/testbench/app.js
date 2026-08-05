// 会话测试台 - 页面脚本（入口模块）
// 视图层拆分为多个模块：弹窗（modal.js）、左侧测试组列表（group_list.js）、
// 聊天渲染（chat.js）、轮次对齐（align.js）、后端调用封装（api.js）、
// 共享状态（state.js）与工具函数（utils.js）。本模块负责会话面板、发送、
// 会话操作、面板排序与初始化，并组装各子模块。
import { createChatRenderer } from "./chat.js";
import { createAlignController } from "./align.js";
import { createGroupList } from "./group_list.js";
import {
  deleteSessions,
  getHistory,
  getPending,
  listConfs,
  listPlatforms,
  ready,
  regenerateHistory,
  resetSessions,
  runStatus,
  runTest,
  saveHistory,
} from "./api.js";
import { openModal, showModal } from "./modal.js";
import { state } from "./state.js";
import {
  confName,
  effectiveView,
  escapeHtml,
  platformName,
  statusText,
} from "./utils.js";

const $ = (id) => document.getElementById(id);

// ---------- 面板 ----------

function toggleOpen(id) {
  if (state.openIds.includes(id)) {
    state.openIds = state.openIds.filter((x) => x !== id);
    state.pinnedIds = state.pinnedIds.filter((x) => x !== id);
  } else {
    state.openIds.push(id);
    openPanel(id);
  }
  renderPanels();
  renderGroupList();
}

function openPanel(id) {
  if (state.panelEls.has(id)) return;
  const panel = document.createElement("div");
  panel.className = "panel";
  panel.dataset.id = id;
  panel.draggable = true;

  const s = effectiveView(id);
  const confBadge =
    s && s.conf_id
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
    `<div class="panel-pending" hidden></div>` +
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

  state.panelEls.set(id, panel);
  renderPanels();
  void loadHistory(id);
}

// 打开组内全部会话（group_list.js 的「打开全部」按钮经 env 调用本函数）
function openAll(gid) {
  const g = state.groups.find((x) => x.id === gid);
  if (!g) return;
  for (const s of g.sessions || []) {
    if (!state.openIds.includes(s.id)) {
      state.openIds.push(s.id);
      openPanel(s.id);
    }
  }
  renderPanels();
  renderGroupList();
}

async function loadHistory(id) {
  const panel = state.panelEls.get(id);
  if (!panel) return;
  const chat = panel.querySelector(".chat");
  try {
    const data = await getHistory(id);
    const conversations = data.conversations || [];
    state.historyCache.set(id, conversations);
    renderChat(panel, conversations);
    // 记录本次成功刷新时刻：完成的消息刷入历史后即从在途条移除
    historyRefreshedAt.set(id, Date.now());
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
  const panel = state.panelEls.get(id);
  try {
    const resp = await regenerateHistory({ id, index });
    if (panel) panelStatus(panel, "warn", "重新生成中…");
    pollRun(
      resp.test_id,
      (r) => {
        const p = state.panelEls.get(id);
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
  const panel = state.panelEls.get(id);
  const input = panel.querySelector(".msg-input");
  input.value = "";
  panelStatus(panel, "warn", "发送中…");
  try {
    const resp = await runTest({ sessions: [id], text });
    pollRun(
      resp.test_id,
      (r) => {
        const p = state.panelEls.get(id);
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
  const ids = state.openIds.slice();
  const text = $("run-text").value.trim();
  if (!ids.length) {
    showRunStatus("warn", "请先在左侧打开至少一个会话");
    return;
  }
  if (!text) {
    showRunStatus("warn", "请输入群发消息");
    return;
  }
  // 不阻止重叠发送：agent 处理中可再次群发（真实「重复追问」场景），
  // 各会话的在途消息由面板底部的在途条实时展示
  $("run-text").value = "";
  showRunStatus("warn", `正在并发发送给 ${ids.length} 个会话…`);
  try {
    const resp = await runTest({ sessions: ids, text });
    pollRun(
      resp.test_id,
      (r) => {
        const panel = state.panelEls.get(r.session_id);
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
      },
    );
  } catch (err) {
    showRunStatus("error", "发送失败: " + err.message);
  }
}

function showRunStatus(status, text) {
  const el = $("run-status");
  el.hidden = false;
  el.className = "run-status " + status;
  el.textContent = text;
}

// ---------- 在途消息（面板实时状态条） ----------

// 在途消息的状态文案（与后端 runner 的条目状态一一对应）
const PENDING_STATUS_TEXT = {
  submitted: "已入队",
  waiting_llm: "排队等待 LLM",
  llm: "LLM 生成中",
  done: "完成",
};

// 各会话最近一次成功刷新历史的时刻（epoch 毫秒）：完成的消息一旦刷入历史
// （气泡可见）即从在途条移除——条内只保留真正在途与完成后的短暂过渡
const historyRefreshedAt = new Map();

// 渲染单个面板的在途消息条：显示正在处理与排队中的消息及其当前阶段；
// 已完成且已刷入会话历史的消息不再展示（历史气泡即完成指示）。
// 返回是否发生变化（供轮询器决定是否重排对齐高度）
function renderPendingStrip(panel, entries) {
  const el = panel.querySelector(".panel-pending");
  if (!el) return false;
  const refreshedAt = historyRefreshedAt.get(panel.dataset.id) || 0;
  // status_at 为后端 epoch 秒，historyRefreshedAt 为 epoch 毫秒；条目
  // 完成于最近一次历史刷新之前 ⇒ 回复已在气泡中，无需再展示
  const visible = entries
    .filter((e) => e.status !== "done" || (e.status_at || 0) * 1000 > refreshedAt)
    .sort((a, b) => (a.created_at || 0) - (b.created_at || 0));
  const key = visible.map((e) => `${e.entry_id}:${e.status}`).join(",");
  if (key === el.dataset.pendingKey) return false;
  el.dataset.pendingKey = key;
  if (!visible.length) {
    el.hidden = true;
    el.innerHTML = "";
    return true;
  }
  el.hidden = false;
  el.innerHTML = visible
    .map((e) => {
      const text =
        e.text && e.text.length > 24 ? e.text.slice(0, 24) + "…" : e.text || "";
      const label = PENDING_STATUS_TEXT[e.status] || e.status;
      return (
        `<span class="pending-item pending-${escapeHtml(e.status)}">` +
        `<span class="pending-text" title="${escapeHtml(e.text)}">${escapeHtml(text)}</span>` +
        `<span class="pending-state">${escapeHtml(label)}</span>` +
        `</span>`
      );
    })
    .join("");
  return true;
}

// 全局轮询在途消息：一次查询，按会话分发到各面板（单发/群发/重新生成共用）
function pollPending() {
  setInterval(async () => {
    let pending = [];
    try {
      const data = await getPending();
      pending = Array.isArray(data.pending) ? data.pending : [];
    } catch (err) {
      return; // 查询失败下轮重试
    }
    let changed = false;
    for (const [id, el] of state.panelEls) {
      if (renderPendingStrip(el, pending.filter((e) => e.session_id === id))) {
        changed = true;
      }
    }
    if (changed && align.isAlignMode()) align.reflowAlign();
  }, 1000);
}

// 群发栏实时显示：当前打开的会话总数 + 按所属测试组的分布
function updateRunOverview() {
  const el = $("run-overview");
  if (!state.openIds.length) {
    el.hidden = true;
    return;
  }
  const counts = new Map();
  for (const id of state.openIds) {
    const v = effectiveView(id);
    if (!v) continue;
    const name = v.group_name || "未分组";
    counts.set(name, (counts.get(name) || 0) + 1);
  }
  let html = `<span class="overview-total">当前会话:${state.openIds.length}</span>`;
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
      const panel = state.panelEls.get(id);
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
      state.openIds = state.openIds.filter((x) => x !== id);
      state.pinnedIds = state.pinnedIds.filter((x) => x !== id);
      renderPanels();
      await refreshGroups();
    },
  });
}

// ---------- 面板排序 ----------

function visibleOrder() {
  return [
    ...state.pinnedIds.filter((id) => state.openIds.includes(id)),
    ...state.openIds.filter((id) => !state.pinnedIds.includes(id)),
  ];
}

// 置顶是开关：置顶的面板固定在最前，再次点击取消置顶
function pin(id) {
  const i = state.pinnedIds.indexOf(id);
  if (i >= 0) state.pinnedIds.splice(i, 1);
  else state.pinnedIds.unshift(id);
  renderPanels();
}

function renderPanels() {
  const panelsEl = $("panels");
  // 置顶面板在最前，其余按打开顺序（appendChild 移动已有节点，保留聊天状态）
  for (const id of visibleOrder()) {
    const el = state.panelEls.get(id);
    if (el) panelsEl.appendChild(el);
  }
  for (const [id, el] of [...state.panelEls]) {
    if (!state.openIds.includes(id)) {
      el.remove();
      state.panelEls.delete(id);
    }
  }
  // 更新置顶按钮的开关视觉状态
  for (const [id, el] of state.panelEls) {
    const btn = el.querySelector('[data-action="pin"]');
    const isPinned = state.pinnedIds.includes(id);
    btn.classList.toggle("active", isPinned);
    btn.title = isPinned ? "取消置顶" : "置顶";
  }
  panelsEl.classList.toggle("single", state.openIds.length === 1);
  $("empty-hint").hidden = state.openIds.length > 0;
  $("align-bar").hidden = !align.isAlignMode() || state.openIds.length === 0;
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
  const pinnedSet = new Set(state.pinnedIds);
  state.pinnedIds = order.filter((id) => pinnedSet.has(id));
  state.openIds = order;
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

// ---------- 选项加载 ----------

async function loadOptions() {
  try {
    const data = await listPlatforms();
    state.platforms = Array.isArray(data) ? data : [];
  } catch (err) {
    console.warn("加载平台列表失败:", err);
    state.platforms = [];
  }
  try {
    const data = await listConfs();
    state.confs = Array.isArray(data) ? data : [];
  } catch (err) {
    console.warn("加载配置档案失败:", err);
    state.confs = [];
  }
}

// ---------- 初始化 ----------

const align = createAlignController({
  getOpenIds: () => state.openIds,
  getPanelEls: () => state.panelEls,
  getHistoryCache: () => state.historyCache,
  getPanelsEl: () => panelsEl,
  renderChat,
});
align.attachEvents();

const chat = createChatRenderer(() => align);

const { refreshGroups, renderGroupList } = createGroupList({
  toggleOpen,
  openAll,
  deleteSession,
  renderPanels,
  showRunStatus,
  updateRunOverview,
});

// 静态控件绑定须放在 createGroupList 解构之后：refreshGroups 是 const 解构
// 绑定，提前引用会触发暂时性死区（ReferenceError），模块求值即中止初始化
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

await ready();
await Promise.all([loadOptions(), refreshGroups()]);
pollPending();
