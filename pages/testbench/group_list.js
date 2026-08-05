// group_list.js — 左侧测试组列表与组/会话配置弹窗
// 由 app.js 通过 createGroupList(env) 创建。env 注入本模块依赖的视图动作
// （面板开关/渲染/会话删除/状态提示/群发概览等），使模块间依赖保持单向
// （本模块不 import app.js；app.js 也只 import 本模块的刷新/渲染函数）。
// 与 align.js 的 createAlignController(env) 同模式：控制器持有视图状态，
// 依赖的数据与动作由 app.js 经 env 注入。
import {
  addGroupSessions,
  createGroup,
  deleteGroups,
  listGroups,
  updateGroup,
  updateSession,
} from "./api.js";
import { state } from "./state.js";
import { openModal, showModal } from "./modal.js";
import {
  confName,
  effectiveView,
  escapeHtml,
  findSession,
  platformName,
} from "./utils.js";

const $ = (id) => document.getElementById(id);

const MAX_SESSIONS = 500;

// 配置档案下拉中代表「显式使用默认配置档案（不绑定）」的哨兵值；
// 与后端约定：保存时映射为 conf_id=""，"" 表示不绑定档案而非继承组配置。
const CONF_DEFAULT = "__default__";

export function createGroupList(env) {
  const {
    toggleOpen,
    openAll,
    deleteSession,
    renderPanels,
    showRunStatus,
    updateRunOverview,
  } = env;

  async function refreshGroups() {
    try {
      const data = await listGroups();
      state.groups = data.groups || [];
      // 清理已被删除的会话面板
      const valid = new Set();
      for (const g of state.groups) for (const s of g.sessions || []) valid.add(s.id);
      const removed = state.openIds.filter((id) => !valid.has(id));
      if (removed.length) {
        state.openIds = state.openIds.filter((id) => valid.has(id));
        state.pinnedIds = state.pinnedIds.filter((id) => valid.has(id));
        renderPanels();
      }
    } catch (err) {
      // 与 refreshTestsets 一致降级：瞬态失败不阻塞初始化（否则 init 的
      // Promise.all 被拒，pollPending 永不启动），列表留待用户点刷新重试
      state.groups = [];
      showRunStatus("error", "加载测试组失败: " + err.message);
    }
    renderGroupList();
    updateRunOverview();
  }

  function renderGroupList() {
    const list = $("group-list");
    list.innerHTML = "";
    $("group-count").textContent = state.groups.length
      ? `${state.groups.length} 个测试组`
      : "";

    // 列表内「＋」块：点击创建默认配置的测试组
    const add = document.createElement("button");
    add.className = "add-block";
    add.textContent = "＋ 新建测试组";
    add.addEventListener("click", handleAddGroup);
    list.appendChild(add);

    if (!state.groups.length) {
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent = "暂无测试组，点上方「＋」创建";
      list.appendChild(empty);
      return;
    }
    for (const g of state.groups) {
      const item = document.createElement("div");
      item.className = "group-item";
      item.dataset.id = g.id;
      const expanded = state.expandedGroups.has(g.id);
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
      item.querySelector('[data-action="open-all"]').addEventListener("click", () =>
        openAll(g.id),
      );
      item.querySelector('[data-action="add"]').addEventListener("click", () =>
        promptAddSessions(g.id),
      );
      item.querySelector('[data-action="edit"]').addEventListener("click", () =>
        openGroupSettings(g.id),
      );
      item
        .querySelector('[data-action="delete-group"]')
        .addEventListener("click", () => deleteGroup(g.id));

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
        const isOpen = state.openIds.includes(s.id);
        const overrides = sessionOverrides(s);
        const overBadge = overrides.length
          ? `<span class="badge warn" title="已单独修改：${escapeHtml(overrides.join("、"))}">已改${overrides.length}</span>`
          : "";
        const sExpanded = state.expandedSessions.has(s.id);
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
    if (state.expandedSessions.has(id)) state.expandedSessions.delete(id);
    else state.expandedSessions.add(id);
    renderGroupList();
  }

  function toggleGroup(id) {
    if (state.expandedGroups.has(id)) state.expandedGroups.delete(id);
    else state.expandedGroups.add(id);
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
        const n = Number(input.value);
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
    const g = state.groups.find((x) => x.id === gid);
    showModal(
      `确定删除测试组「${g ? g.name : gid}」及其全部 ${g ? (g.sessions || []).length : 0} 个会话吗？`,
      {
        danger: true,
        onOk: async () => {
          await deleteGroups([gid]);
          for (const s of g.sessions || []) {
            state.openIds = state.openIds.filter((x) => x !== s.id);
            state.pinnedIds = state.pinnedIds.filter((x) => x !== s.id);
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
      state.expandedGroups.add(group.id);
      await refreshGroups();
      openGroupSettings(group.id);
    } catch (err) {
      showModal("创建失败: " + err.message);
    }
  }

  // 平台下拉的选项片段（真实平台列表；「默认」前缀项由各调用方自行决定）
  function platformOptions() {
    return state.platforms
      .map(
        (p) =>
          `<option value="${escapeHtml(p.id)}">${escapeHtml(p.id)}（${escapeHtml(p.display_name || p.name)}）</option>`,
      )
      .join("");
  }

  // 配置档案下拉的选项数组：排除 default 档案；已绑定但已被删除的档案保留占位选项，
  // 避免保存时静默丢失绑定。组编辑与会话配置弹窗共用，占位逻辑只保留此处一份。
  function confOptions(current) {
    const options = state.confs.filter((c) => c.id !== "default");
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
    const g = state.groups.find((x) => x.id === gid);
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
        const count = Number(inpCount.value);
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
        const cur = (state.groups.find((x) => x.id === gid) || {}).sessions || [];
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
    const panel = state.panelEls.get(id);
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

  return { refreshGroups, renderGroupList };
}
