// 会话测试台 - 页面脚本（入口模块）
// 视图层拆分为多个模块：弹窗（modal.js）、左侧测试组列表（group_list.js）、
// 聊天渲染（chat.js）、轮次对齐（align.js）、后端调用封装（api.js）、
// 共享状态（state.js）与工具函数（utils.js）。本模块负责会话面板、发送、
// 会话操作、面板排序与初始化，并组装各子模块。
import { createChatRenderer } from "./chat.js";
import { createAlignController } from "./align.js";
import { createEventController } from "./events.js";
import { createGroupList } from "./group_list.js";
import { createTestsetList } from "./testset_list.js";
import { createTestsetRunController } from "./testset_run.js";
import {
  cloneSession as cloneSessionApi,
  deleteSessions,
  deriveSession as deriveSessionApi,
  getHistory,
  listConfs,
  listPlatforms,
  ready,
  regenerateHistory,
  resetSessions,
  runTest,
  saveHistory,
} from "./api.js";
import { openModal, showModal } from "./modal.js";
import { state } from "./state.js";
import {
  confName,
  effectiveView,
  escapeHtml,
  field,
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
    `<div class="panel-menu">` +
    `<button class="icon-btn menu-toggle" data-action="menu" title="更多操作">⋯</button>` +
    `<div class="panel-menu-dropdown" hidden>` +
    `<button class="panel-menu-item" data-action="history">编辑历史</button>` +
    `<button class="panel-menu-item" data-action="reset">重置历史</button>` +
    `<button class="panel-menu-item" data-action="copy">复制历史</button>` +
    `<button class="panel-menu-item" data-action="clone">克隆会话…</button>` +
    `<button class="panel-menu-item" data-action="paste">粘贴历史</button>` +
    `<button class="panel-menu-item" data-action="derive">衍生测试组…</button>` +
    `</div>` +
    `</div>` +
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
  panel.querySelector('[data-action="pin"]').addEventListener("click", () => pin(id));
  setupPanelMenu(panel, id);
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

// 各会话历史刷新序号：并发请求（群发反馈/测试集步骤/手动刷新）可能乱序返回，
// 只采纳最后一次发起的响应，丢弃乱序迟到的旧快照（防历史回退与旧错误覆盖新内容）
const historySeq = new Map();

async function loadHistory(id) {
  const panel = state.panelEls.get(id);
  if (!panel) return;
  const chat = panel.querySelector(".chat");
  const seq = (historySeq.get(id) || 0) + 1;
  historySeq.set(id, seq);
  try {
    const data = await getHistory(id);
    if (historySeq.get(id) !== seq) return; // 已有更新的刷新在途，丢弃本次迟到响应
    const conversations = data.conversations || [];
    state.historyCache.set(id, conversations);
    renderChat(panel, conversations);
    // 记录本次成功刷新时刻：完成的消息刷入历史后即从在途条移除
    state.historyRefreshedAt.set(id, Date.now());
    if (align.isAlignMode()) align.reflowAlign();
  } catch (err) {
    if (historySeq.get(id) !== seq) return; // 迟到失败同样丢弃，不覆盖较新内容
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

async function regenerateMsg(id, index, conversationId) {
  const panel = state.panelEls.get(id);
  try {
    const resp = await regenerateHistory({ id, index, conversation_id: conversationId || undefined });
    if (panel) panelStatus(panel, "warn", "重新生成中…");
    events.registerTestConsumer(
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

// ---------- 发送（事件驱动） ----------
// 逐会话反馈（面板状态 + 历史刷新）与消费者注册收敛在 events.js（applySessionFeedback /
// registerTestConsumer）；sendToOne / sendToAll 只负责启动投递并挂接消费者。

async function sendToOne(id, text) {
  text = (text || "").trim();
  if (!text) return;
  const panel = state.panelEls.get(id);
  const input = panel.querySelector(".msg-input");
  input.value = "";
  panelStatus(panel, "warn", "发送中…");
  try {
    const resp = await runTest({ sessions: [id], text });
    events.registerTestConsumer(resp.test_id, events.applySessionFeedback, () => {});
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
    events.registerTestConsumer(
      resp.test_id,
      events.applySessionFeedback,
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

// ---------- 会话操作 ----------// ---------- 会话操作 ----------

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

// ---------- 会话页眉 ⋯ 菜单：编辑/重置/复制/克隆/粘贴/衍生 ----------

// 绑定面板「⋯」下拉菜单：点击开关切换、点菜单外自动关闭，菜单项分发到各操作
function setupPanelMenu(panel, id) {
  const toggle = panel.querySelector('[data-action="menu"]');
  const dropdown = panel.querySelector(".panel-menu-dropdown");
  if (!toggle || !dropdown) return;
  toggle.addEventListener("click", (e) => {
    e.stopPropagation();
    document.querySelectorAll(".panel-menu-dropdown").forEach((d) => {
      if (d !== dropdown) d.hidden = true;
    });
    dropdown.hidden = !dropdown.hidden;
  });
  dropdown.querySelectorAll(".panel-menu-item").forEach((btn) => {
    btn.addEventListener("click", () => {
      dropdown.hidden = true;
      switch (btn.dataset.action) {
        case "history":
          void openHistoryEditor(id);
          break;
        case "reset":
          resetHistory(id);
          break;
        case "copy":
          void copyHistory(id);
          break;
        case "clone":
          cloneSession(id);
          break;
        case "paste":
          pasteHistory(id);
          break;
        case "derive":
          deriveSession(id);
          break;
      }
    });
  });
}

// 复制历史：把当前会话的全部对话（去掉 conversation_id，粘贴时整体新建）存入剪贴板
async function copyHistory(id) {
  let data;
  try {
    data = await getHistory(id);
  } catch (err) {
    showRunStatus("error", "复制历史失败: " + err.message);
    return;
  }
  const conversations = (data.conversations || []).map((c) => ({
    title: c.title || null,
    history: Array.isArray(c.history) ? c.history : [],
  }));
  if (!conversations.length) {
    showRunStatus("warn", "该会话暂无对话历史，无可复制");
    return;
  }
  state.clipboard = {
    conversations,
    sourceName: (effectiveView(id) || {}).name || id,
    at: Date.now(),
  };
  showRunStatus(
    "ok",
    `已复制 ${conversations.length} 个对话的历史，可在其他会话「⋯」菜单粘贴`,
  );
}

// 粘贴历史：用剪贴板整体覆盖当前会话的历史（复用 save_history 的替换语义）
function pasteHistory(id) {
  const clip = state.clipboard;
  if (!clip) {
    showModal("尚未复制任何历史：请先在某个会话的「⋯」菜单中点击「复制历史」。");
    return;
  }
  const n = clip.conversations.length;
  showModal(
    `将用来自「${clip.sourceName}」的 ${n} 个对话覆盖当前会话的历史，` +
      "当前历史将被删除，此操作不可撤销。确定继续吗？",
    {
      danger: true,
      onOk: async () => {
        await saveHistory({ id, conversations: clip.conversations });
        void loadHistory(id);
        showRunStatus("ok", `已粘贴 ${n} 个对话的历史`);
      },
    },
  );
}

// 克隆会话：在当前测试组内新建 N 个历史一致的会话（同一起点，便于分别改配置测试）
function cloneSession(id) {
  const v = effectiveView(id);
  promptCountDialog(
    "克隆会话",
    `在当前测试组内新建 N 个会话，其对话历史与「${v ? v.name : id}」完全一致，` +
      "克隆后可在各会话上分别修改配置/模型进行对照测试。",
    3,
    async (count) => {
      const resp = await cloneSessionApi(id, count);
      await refreshGroups();
      showRunStatus(
        "ok",
        `已克隆 ${resp.session_ids.length} 个会话（历史与当前会话一致）`,
      );
    },
  );
}

// 衍生测试组：基于当前会话的历史创建全新测试组，组内每个会话的历史都与它一致
function deriveSession(id) {
  const group = state.groups.find((g) =>
    (g.sessions || []).some((s) => s.id === id),
  );
  const defaultName = group && group.name ? `${group.name} 衍生` : "衍生测试组";
  const defaultCount =
    group && Array.isArray(group.sessions) && group.sessions.length
      ? group.sessions.length
      : 3;
  promptFormDialog(
    "衍生测试组",
    "基于当前会话的历史创建全新测试组（继承当前组的平台/档案/发送者配置），组内每个会话的历史都与它一致，可分别改配置测试。",
    [
      { name: "name", label: "测试组名称", value: defaultName },
      {
        name: "count",
        label: "会话数量",
        type: "number",
        min: 1,
        max: 500,
        value: defaultCount,
      },
    ],
    "创建",
    async (inputs) => {
      const name = inputs.name.value.trim();
      if (!name) throw new Error("测试组名称不能为空");
      const n = Number(inputs.count.value);
      if (!Number.isInteger(n) || n < 1 || n > 500) {
        throw new Error("会话数量必须是 1-500 的整数");
      }
      const resp = await deriveSessionApi(id, n, name);
      await refreshGroups();
      showRunStatus(
        "ok",
        `已创建测试组「${name}」：${resp.session_ids.length} 个会话，历史与当前会话一致`,
      );
    },
  );
}

// 表单弹窗：p 说明 + 带标签输入字段（field），onOk 接收 {name: 输入元素}。
// 校验失败（throw）则停留在弹窗。单输入（克隆数量）与多输入（衍生）共用。
function promptFormDialog(title, message, fields, okText, onOk) {
  const wrap = document.createElement("div");
  wrap.className = "form-col";
  const p = document.createElement("p");
  p.textContent = message;
  wrap.append(p);
  const inputs = {};
  for (const f of fields) {
    const input = document.createElement("input");
    input.type = f.type || "text";
    if (f.min) input.min = String(f.min);
    if (f.max) input.max = String(f.max);
    input.value = String(f.value ?? "");
    inputs[f.name] = input;
    wrap.append(field(f.label, input));
  }
  openModal({
    title,
    content: wrap,
    okText,
    onOk: () => onOk(inputs),
  });
}

// 数字输入弹窗：克隆数量等单输入场景的便捷封装
function promptCountDialog(title, message, defaultValue, onOk) {
  promptFormDialog(
    title,
    message,
    [
      { name: "count", label: "数量", type: "number", min: 1, max: 500, value: defaultValue },
    ],
    "确定",
    (inputs) => {
      const n = Number(inputs.count.value);
      if (!Number.isInteger(n) || n < 1 || n > 500) {
        throw new Error("数量必须是 1-500 的整数");
      }
      return onOk(n);
    },
  );
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

// 点击「⋯」菜单外的任意位置关闭所有已打开的面板菜单（开关按钮的 click 已 stopPropagation）
document.addEventListener("click", (e) => {
  if (e.target.closest && e.target.closest(".panel-menu")) return;
  document.querySelectorAll(".panel-menu-dropdown").forEach((d) => {
    d.hidden = true;
  });
});

// 气泡悬停操作：重新生成某轮（整体历史的编辑走面板头部「编辑」）
panelsEl.addEventListener("click", (e) => {
  const btn = e.target.closest('[data-action="regenerate"]');
  if (!btn) return;
  const msgEl = btn.closest(".msg");
  if (!msgEl) return;
  const index = parseInt(msgEl.dataset.index, 10);
  if (Number.isNaN(index)) return;
  void regenerateMsg(
    btn.closest(".panel").dataset.id,
    index,
    msgEl.dataset.conv || null,
  );
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

// 测试集运行编排与事件驱动反馈：两模块交叉依赖（事件流把 testset 事件转发给
// testset_run，testset_run 的逐会话反馈回调给 events）经 wireControllers 装配，
// 用延迟 getter 避开 events ↔ testset_run 互相 import 的循环依赖
const testsetRun = createTestsetRunController({
  showRunStatus,
  loadHistory,
});
const events = createEventController({
  loadHistory,
  panelStatus,
  getAlign: () => align,
});

const { refreshGroups, renderGroupList } = createGroupList({
  toggleOpen,
  openAll,
  deleteSession,
  renderPanels,
  showRunStatus,
  updateRunOverview: testsetRun.updateRunOverview,
});

const { refreshTestsets } = createTestsetList({
  showRunStatus,
  runTestset: testsetRun.runTestset,
  viewTestsetRun: testsetRun.viewTestsetRun,
  // 选中测试集 → 右侧自动切到「测试集」视图（showView 是函数声明，可提升）
  switchToTestsets: () => showView("testsets"),
});

// 装配交叉依赖。须放在全部控制器创建之后（函数体内引用，延迟求值）
function wireControllers() {
  testsetRun.setApplySessionFeedback(() => events.applySessionFeedback);
  testsetRun.setRefreshTestsets(() => refreshTestsets);
  events.setTestsetEvent(() => testsetRun.handleTestsetEvent);
}
wireControllers();

// 静态控件绑定须放在 createGroupList / createTestsetList 解构之后：
// refreshGroups / refreshTestsets 是 const 解构绑定，提前引用会触发暂时性
// 死区（ReferenceError），模块求值即中止初始化
$("btn-refresh").addEventListener("click", refreshGroups);
$("btn-refresh-testsets").addEventListener("click", refreshTestsets);
$("btn-run-all").addEventListener("click", sendToAll);
$("btn-abort-run").addEventListener("click", () => testsetRun.abortTestsetRun(state.activeRunId));
// 「查看报告」：测试集结果不自动弹窗，暂存后由用户按需查看（最近一次完成/取消的运行）
$("btn-view-report").addEventListener("click", () => {
  const run = state.latestReportRunId && state.runReports[state.latestReportRunId];
  if (run) testsetRun.showTestsetResults(run);
});
$("btn-run-testset").addEventListener("click", () => testsetRun.runTestsetFromBar());
$("run-testset").addEventListener("change", () => {
  $("btn-run-testset").disabled = !$("run-testset").value;
});
$("run-text").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.isComposing) sendToAll();
});

// UI 窄条：视图切换（会话列表 / 测试集）。点击当前视图按钮折叠/展开侧栏，
// 点击其他视图按钮切换视图（展开侧栏并刷新对应视图数据）
let activeView = "sessions";

function showView(view) {
  activeView = view;
  document.body.classList.remove("sidebar-collapsed");
  document.querySelector(".groups-card").hidden = view !== "sessions";
  document.querySelector(".testsets-card").hidden = view !== "testsets";
  // 左侧选择驱动右侧视图：会话列表 ↔ 测试集编辑窗口自动切换
  document.querySelector(".sessions-view").hidden = view !== "sessions";
  document.querySelector(".testsets-view").hidden = view !== "testsets";
  document.querySelectorAll(".rail-btn").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.view === view);
  });
  if (view === "testsets") void refreshTestsets();
}

document.querySelectorAll(".rail-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    if (btn.dataset.view === activeView) {
      const collapsed = document.body.classList.toggle("sidebar-collapsed");
      btn.classList.toggle("active", !collapsed);
    } else {
      showView(btn.dataset.view);
    }
  });
});

await ready();
// allSettled：三个初始化步骤相互独立，任一失败不阻塞其余步骤与事件流连接
// （各步骤内部已自行降级，见 refreshGroups / refreshTestsets 的 catch）
await Promise.allSettled([loadOptions(), refreshGroups(), refreshTestsets()]);
void events.connectEvents();
