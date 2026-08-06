// identity_list.js — rail 第三视图「身份与群聊」：测试身份与虚拟群聊的增删改
// 与 group_list.js 同模式：由 app.js 通过 createIdentityList(env) 创建。
// env 注入本模块依赖的视图动作（refreshGroups 回刷组弹窗选项 / showRunStatus），
// 本模块不 import app.js，模块间依赖保持单向。
// 左侧「身份与群聊」卡片内以 tab 拆分「身份」与「群聊」两个列表（身份膨胀后
// 群聊列表不被挤走）；创建群聊只填名称（成员多选移出弹窗——成员多时窗口放
// 不下），成员管理在右侧「群聊编辑」视图完成——搜索身份池（名称/发送者ID/
// 昵称）实时过滤后加入、成员行可移除、名称可改。
// 身份与虚拟群聊是跨测试组共享的持久化资源：组配置弹窗、群发栏身份选择器、
// 测试集消息身份下拉都从 state.identities / state.chatGroups 读取，本模块
// 负责这些资源的列表管理（CRUD），并在增删改后刷新引用方。
import {
  createChatGroup,
  createIdentity,
  deleteChatGroups,
  deleteIdentities,
  listChatGroups,
  listIdentities,
  updateChatGroup,
  updateIdentity,
} from "./api.js";
import { state } from "./state.js";
import { openModal, showModal } from "./modal.js";
import { escapeHtml, field } from "./utils.js";

const $ = (id) => document.getElementById(id);

export function createIdentityList(env) {
  const { refreshGroups, showRunStatus } = env;

  // ---------- 刷新（列表 + 引用方下拉） ----------

  async function refreshIdentities() {
    try {
      const data = await listIdentities();
      state.identities = Array.isArray(data.identities) ? data.identities : [];
    } catch (err) {
      state.identities = [];
      showRunStatus("error", "加载身份失败: " + err.message);
    }
    renderIdentityList();
    syncBroadcastSenders();
    // 编辑视图的搜索池随身份变化重渲染（身份可能晚于群聊加载完成）
    renderChatGroupView();
  }

  async function refreshChatGroups() {
    try {
      const data = await listChatGroups();
      state.chatGroups = Array.isArray(data.chat_groups) ? data.chat_groups : [];
    } catch (err) {
      state.chatGroups = [];
      showRunStatus("error", "加载虚拟群聊失败: " + err.message);
    }
    renderChatGroupList();
    renderChatGroupView();
  }

  // 群发栏身份选择器：选项来自身份列表（「各会话自身身份」为默认）。
  // 群发/单发消息都读这个下拉 → payload 带 sender_id/sender_name。
  function syncBroadcastSenders() {
    const sel = $("run-sender");
    if (!sel) return;
    const current = sel.value;
    sel.innerHTML =
      `<option value="">各会话自身身份</option>` +
      state.identities
        .map(
          (i) =>
            `<option value="${escapeHtml(i.id)}">${escapeHtml(i.name)}（${escapeHtml(i.sender_id)}）</option>`,
        )
        .join("");
    if (current && state.identities.some((i) => i.id === current)) sel.value = current;
  }

  // ---------- 左侧卡片：身份 / 群聊 tab 切换 ----------

  // 身份与群聊分开两个 tab，一次只渲染一个列表：身份膨胀后找群聊不被长列表挤走
  function switchIdentityTab(tab) {
    document.querySelectorAll(".identities-card .tab-btn").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.tab === tab);
    });
    document.querySelectorAll(".identities-card .tab-pane").forEach((pane) => {
      pane.hidden = pane.dataset.pane !== tab;
    });
  }

  // ---------- 身份列表 ----------

  function renderIdentityList() {
    const list = $("identity-list");
    list.innerHTML = "";
    $("identity-count").textContent = state.identities.length
      ? `${state.identities.length} 个身份`
      : "";

    if (!state.identities.length) {
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent = "暂无身份，点下方「＋」创建";
      list.appendChild(empty);
    }
    for (const ident of state.identities) {
      const item = document.createElement("div");
      item.className = "group-item";
      const name = ident.name || ident.sender_id || ident.id;
      item.innerHTML =
        `<div class="group-head">` +
        `<span class="group-name" title="${escapeHtml(name)}">${escapeHtml(name)}</span>` +
        `<span class="group-actions">` +
        `<button class="icon-btn" data-action="edit" title="编辑身份">✎</button>` +
        `<button class="icon-btn danger" data-action="delete" title="删除身份">✕</button>` +
        `</span>` +
        `</div>` +
        `<div class="group-meta">` +
        `<span class="badge" title="sender_id">${escapeHtml(ident.sender_id)}</span>` +
        `<span class="badge" title="sender_name">${escapeHtml(ident.sender_name)}</span>` +
        `${ident.is_admin ? '<span class="badge admin" title="管理员身份，发送时自动设置 event.role=admin">管理员</span>' : ""}` +
        // 管理员身份可调用需管理员权限的工具（如本地/沙箱执行、联网搜索、定时任务），
        // 可能执行危险操作——列表旁挂警告徽标提示
        `${ident.is_admin ? '<span class="badge warn" title="管理员身份发送时 event.role=admin，可调用需管理员权限的工具（本地/沙箱执行、联网搜索、定时任务等），可能执行危险操作">⚠ 危险</span>' : ""}` +
        `</div>`;
      // 拖拽到右侧群聊编辑视图可快速加入该群（dataTransfer 只传身份 id）
      item.draggable = true;
      item.addEventListener("dragstart", (e) => {
        e.dataTransfer.setData("text/plain", ident.id);
        e.dataTransfer.effectAllowed = "copy";
        item.classList.add("dragging");
      });
      item.addEventListener("dragend", () => item.classList.remove("dragging"));
      item
        .querySelector('[data-action="edit"]')
        .addEventListener("click", () => openIdentityForm(ident));
      item
        .querySelector('[data-action="delete"]')
        .addEventListener("click", () => deleteIdentity(ident));
      list.appendChild(item);
    }

    const add = document.createElement("button");
    add.className = "add-block";
    add.textContent = "＋ 新建身份";
    add.addEventListener("click", () => openIdentityForm(null));
    list.appendChild(add);
  }

  // 身份表单：名称必填；sender_id / sender_name 留空回退名称。
  // 更新时传空串（而非 null）让后端重置为名称——null 表示「保持不变」。
  function openIdentityForm(identity) {
    const inpName = document.createElement("input");
    inpName.type = "text";
    inpName.value = identity ? identity.name : "";
    const inpId = document.createElement("input");
    inpId.type = "text";
    inpId.placeholder = "留空使用名称";
    inpId.value = identity ? identity.sender_id : "";
    const inpName2 = document.createElement("input");
    inpName2.type = "text";
    inpName2.placeholder = "留空使用名称";
    inpName2.value = identity ? identity.sender_name : "";
    const inpAdmin = document.createElement("input");
    inpAdmin.type = "checkbox";
    inpAdmin.checked = identity ? !!identity.is_admin : false;
    // 勾选管理员即时显示内联警告（与组/会话弹窗的工具安全警告条同一风格）
    const adminWarn = document.createElement("div");
    adminWarn.className = "dialog-warn";
    adminWarn.hidden = !inpAdmin.checked;
    adminWarn.textContent =
      "⚠ 管理员身份发送的消息带 event.role=admin，可调用需管理员权限的工具" +
      "（本地/沙箱执行、联网搜索、定时任务等），可能执行危险操作，请仅用于可信测试。";
    inpAdmin.addEventListener("change", () => {
      adminWarn.hidden = !inpAdmin.checked;
    });

    // 管理员：checkbox 在前、与标签同行（field() 的纵向布局会让方框独占一行）
    const fAdmin = document.createElement("label");
    fAdmin.className = "settings-field";
    const adminText = document.createElement("span");
    adminText.textContent = "管理员（发送时自动按管理员身份设置角色）";
    fAdmin.append(inpAdmin, adminText);

    const form = document.createElement("div");
    form.className = "form-col";
    form.append(
      field("名称", inpName),
      field("发送者ID（留空使用名称）", inpId),
      field("发送者昵称（留空使用名称）", inpName2),
      fAdmin,
      adminWarn,
    );

    openModal({
      title: identity ? `编辑身份 · ${identity.name}` : "新建身份",
      content: form,
      okText: identity ? "保存" : "创建",
      onOk: async () => {
        const name = inpName.value.trim();
        if (!name) throw new Error("名称不能为空");
        const payload = {
          name,
          sender_id: inpId.value.trim(),
          sender_name: inpName2.value.trim(),
          is_admin: inpAdmin.checked,
        };
        if (identity) await updateIdentity(identity.id, payload);
        else await createIdentity(payload);
        await refreshIdentities();
        await refreshGroups();
        showRunStatus("ok", identity ? "身份已更新" : `身份「${name}」已创建`);
      },
    });
  }

  function deleteIdentity(ident) {
    showModal(`确定删除身份「${ident.name || ident.id}」吗？`, {
      danger: true,
      onOk: async () => {
        await deleteIdentities([ident.id]);
        await refreshIdentities();
        await refreshGroups();
        showRunStatus("ok", "身份已删除");
      },
    });
  }

  // ---------- 虚拟群聊列表 ----------

  function renderChatGroupList() {
    const list = $("chat-group-list");
    list.innerHTML = "";
    $("chat-group-count").textContent = state.chatGroups.length
      ? `${state.chatGroups.length} 个群聊`
      : "";

    if (!state.chatGroups.length) {
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent = "暂无虚拟群聊，点下方「＋」创建";
      list.appendChild(empty);
    }
    for (const cg of state.chatGroups) {
      const count = Array.isArray(cg.member_ids) ? cg.member_ids.length : 0;
      const item = document.createElement("div");
      item.className =
        "group-item" + (cg.id === state.selectedChatGroupId ? " selected" : "");
      item.innerHTML =
        `<div class="group-head">` +
        `<span class="group-name" title="${escapeHtml(cg.name)}">${escapeHtml(cg.name)}</span>` +
        `<span class="badge">${count} 成员</span>` +
        `<span class="group-actions">` +
        `<button class="icon-btn" data-action="edit" title="编辑群聊">✎</button>` +
        `<button class="icon-btn danger" data-action="delete" title="删除群聊">✕</button>` +
        `</span>` +
        `</div>`;
      // 条目整体点击 → 右侧打开该群聊的编辑视图
      item.addEventListener("click", () => openChatGroupView(cg));
      item
        .querySelector('[data-action="edit"]')
        .addEventListener("click", (e) => {
          e.stopPropagation();
          openChatGroupView(cg);
        });
      item
        .querySelector('[data-action="delete"]')
        .addEventListener("click", (e) => {
          e.stopPropagation();
          deleteChatGroup(cg);
        });
      list.appendChild(item);
    }

    const add = document.createElement("button");
    add.className = "add-block";
    add.textContent = "＋ 新建群聊";
    add.addEventListener("click", () => openCreateChatGroup());
    list.appendChild(add);
  }

  // 创建群聊：只填名称（成员多选移出弹窗——成员多时窗口放不下）。创建成功后
  // 自动选中新群聊并打开右侧「群聊编辑」视图，在那里搜索成员加入。
  function openCreateChatGroup() {
    const inpName = document.createElement("input");
    inpName.type = "text";
    inpName.value = "";

    const form = document.createElement("div");
    form.className = "form-col";
    form.append(field("群聊名称", inpName));

    openModal({
      title: "新建群聊",
      content: form,
      okText: "创建",
      onOk: async () => {
        const name = inpName.value.trim();
        if (!name) throw new Error("群聊名称不能为空");
        const cg = await createChatGroup({ name });
        await refreshChatGroups();
        await refreshGroups();
        state.selectedChatGroupId = cg.id;
        renderChatGroupView();
        showRunStatus("ok", `群聊「${name}」已创建，可在右侧添加成员`);
      },
    });
  }

  function deleteChatGroup(cg) {
    showModal(`确定删除虚拟群聊「${cg.name || cg.id}」吗？`, {
      danger: true,
      onOk: async () => {
        await deleteChatGroups([cg.id]);
        if (state.selectedChatGroupId === cg.id) state.selectedChatGroupId = null;
        await refreshChatGroups();
        await refreshGroups();
        renderChatGroupView();
        showRunStatus("ok", "虚拟群聊已删除");
      },
    });
  }

  // ---------- 右侧「群聊编辑」视图 ----------

  function openChatGroupView(cg) {
    state.selectedChatGroupId = cg.id;
    renderChatGroupList();
    renderChatGroupView();
  }

  // 按当前选中群聊渲染右侧编辑视图（名称 / 成员 / 搜索）；未选中 → 空态提示
  function renderChatGroupView() {
    const cg = state.chatGroups.find((x) => x.id === state.selectedChatGroupId);
    $("cg-empty").hidden = !!cg;
    $("cg-members").hidden = !cg;
    if (!cg) {
      $("cg-name").value = "";
      $("cg-meta").textContent = "";
      return;
    }
    $("cg-name").value = cg.name || "";
    const memberIds = Array.isArray(cg.member_ids) ? cg.member_ids : [];
    $("cg-meta").textContent = `${memberIds.length} 个成员`;
    renderMemberList(memberIds);
    renderSearchResults($("cg-search").value.trim());
  }

  function renderMemberList(memberIds) {
    const list = $("cg-member-list");
    $("cg-member-count").textContent = memberIds.length;
    list.innerHTML = "";
    if (!memberIds.length) {
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent = "暂无成员，在下方搜索并加入";
      list.appendChild(empty);
      return;
    }
    for (const mid of memberIds) {
      const ident = state.identities.find((i) => i.id === mid);
      const row = document.createElement("div");
      row.className = "cg-member-row";
      if (ident) {
        // 昵称列优先展示 sender_name（无昵称回退 sender_id），悬停显示完整信息
        const nick = ident.sender_name || ident.sender_id || "—";
        row.innerHTML =
          `<span class="cg-member-name" title="${escapeHtml(ident.name)}">${escapeHtml(ident.name)}</span>` +
          `<span class="badge" title="发送者ID">${escapeHtml(ident.sender_id)}</span>` +
          `<span class="badge" title="昵称 ${escapeHtml(nick)}">${escapeHtml(nick)}</span>` +
          // 管理员成员挂「管理员」+「⚠ 危险」徽标（与身份列表一致）
          `${ident.is_admin ? '<span class="badge admin" title="管理员身份，发送时自动设置 event.role=admin">管理员</span>' : ""}` +
          `${ident.is_admin ? '<span class="badge warn" title="管理员身份可调用需管理员权限的工具（本地/沙箱执行、联网搜索、定时任务等），可能执行危险操作">⚠ 危险</span>' : ""}` +
          `<button class="icon-btn danger" data-action="remove" title="移出该群">✕</button>`;
      } else {
        // 身份已删除：成员 id 悬空引用，保留占位并允许移除
        row.innerHTML =
          `<span class="cg-member-name">${escapeHtml(mid)}（身份已删除）</span>` +
          `<button class="icon-btn danger" data-action="remove" title="移除该引用">✕</button>`;
      }
      row
        .querySelector('[data-action="remove"]')
        .addEventListener("click", () => {
          void removeMember(mid);
        });
      list.appendChild(row);
    }
  }

  // 移除成员：member_ids 整体替换（去重保序由后端 _clean_member_ids 保证）
  async function removeMember(mid) {
    const cg = state.chatGroups.find((x) => x.id === state.selectedChatGroupId);
    if (!cg) return;
    const memberIds = (Array.isArray(cg.member_ids) ? cg.member_ids : []).filter(
      (x) => x !== mid,
    );
    await updateChatGroup(cg.id, { member_ids: memberIds });
    await refreshChatGroups();
    showRunStatus("ok", "成员已移出该群");
  }

  // 搜索身份池：按 名称/发送者ID/昵称 子串过滤（不区分大小写），已在群内的
  // 成员排除；空关键字显示全部可加入成员
  function renderSearchResults(q) {
    const list = $("cg-search-results");
    list.innerHTML = "";
    const cg = state.chatGroups.find((x) => x.id === state.selectedChatGroupId);
    if (!cg) return;
    const memberIds = new Set(Array.isArray(cg.member_ids) ? cg.member_ids : []);
    if (!state.identities.length) {
      const hint = document.createElement("div");
      hint.className = "empty";
      hint.textContent = "暂无身份，请先在左侧「身份」标签页创建";
      list.appendChild(hint);
      return;
    }
    const query = q.toLowerCase();
    const matches = state.identities.filter((i) => {
      if (memberIds.has(i.id)) return false;
      if (!query) return true;
      return (
        (i.name || "").toLowerCase().includes(query) ||
        (i.sender_id || "").toLowerCase().includes(query) ||
        (i.sender_name || "").toLowerCase().includes(query)
      );
    });
    if (!matches.length) {
      const hint = document.createElement("div");
      hint.className = "empty";
      hint.textContent = query ? "无匹配成员" : "全部成员已在群内";
      list.appendChild(hint);
      return;
    }
    for (const ident of matches) {
      const nick = ident.sender_name || ident.sender_id || "—";
      const row = document.createElement("div");
      row.className = "cg-search-row";
      row.innerHTML =
        `<span class="cg-member-name" title="${escapeHtml(nick)}">${escapeHtml(ident.name)}</span>` +
        `<span class="badge" title="昵称 ${escapeHtml(nick)}">${escapeHtml(nick)}</span>` +
        `<button class="btn small primary" data-action="join">＋ 加入</button>`;
      row
        .querySelector('[data-action="join"]')
        .addEventListener("click", () => {
          void addMember(ident.id);
        });
      list.appendChild(row);
    }
  }

  // 加入成员：新 id 追加到末尾后整体替换；无选中群聊 / 已在群中给出提示而非静默
  async function addMember(mid) {
    const cg = state.chatGroups.find((x) => x.id === state.selectedChatGroupId);
    if (!cg) {
      showRunStatus("warn", "请先在左侧选择一个虚拟群聊");
      return;
    }
    const memberIds = Array.isArray(cg.member_ids) ? cg.member_ids.slice() : [];
    if (memberIds.includes(mid)) {
      showRunStatus("warn", "该身份已在群中");
      return;
    }
    memberIds.push(mid);
    await updateChatGroup(cg.id, { member_ids: memberIds });
    $("cg-search").value = "";
    await refreshChatGroups();
    showRunStatus("ok", "成员已加入该群");
  }

  // 编辑视图头部「保存名称」：只更新名称字段
  async function saveChatGroupName() {
    const cg = state.chatGroups.find((x) => x.id === state.selectedChatGroupId);
    if (!cg) return;
    const name = $("cg-name").value.trim();
    if (!name) {
      showRunStatus("warn", "群聊名称不能为空");
      return;
    }
    await updateChatGroup(cg.id, { name });
    await refreshChatGroups();
    showRunStatus("ok", "群聊名称已保存");
  }

  // 编辑视图头部「✕ 删除」：删除并清空选中（与左侧列表删除同一语义）
  function deleteChatGroupView() {
    const cg = state.chatGroups.find((x) => x.id === state.selectedChatGroupId);
    if (!cg) return;
    deleteChatGroup(cg);
  }

  // ---------- 静态控件绑定（左侧 tab + 右侧编辑视图） ----------

  document.querySelectorAll(".identities-card .tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => switchIdentityTab(btn.dataset.tab));
  });
  $("btn-cg-save").addEventListener("click", () => {
    void saveChatGroupName();
  });
  $("btn-cg-delete").addEventListener("click", deleteChatGroupView);
  $("cg-search").addEventListener("input", (e) =>
    renderSearchResults(e.target.value.trim()),
  );

  // 拖拽投放区：把左侧「身份」条目拖到成员区即加入当前群聊
  const cgMembers = $("cg-members");
  cgMembers.addEventListener("dragover", (e) => {
    if (!state.selectedChatGroupId) return;
    if ((e.dataTransfer.types || []).includes("text/plain")) {
      e.preventDefault();
      e.dataTransfer.dropEffect = "copy";
      cgMembers.classList.add("drag-over");
    }
  });
  cgMembers.addEventListener("dragleave", (e) => {
    if (!cgMembers.contains(e.relatedTarget)) cgMembers.classList.remove("drag-over");
  });
  cgMembers.addEventListener("drop", (e) => {
    e.preventDefault();
    cgMembers.classList.remove("drag-over");
    const mid = e.dataTransfer.getData("text/plain");
    if (mid) void addMember(mid);
  });

  return {
    refreshIdentities,
    refreshChatGroups,
    syncBroadcastSenders,
    renderChatGroupView,
  };
}
