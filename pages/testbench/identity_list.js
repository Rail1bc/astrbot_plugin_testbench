// identity_list.js — rail 第三视图「身份与群聊」：测试身份与虚拟群聊的增删改
// 与 group_list.js 同模式：由 app.js 通过 createIdentityList(env) 创建。
// env 注入本模块依赖的视图动作（refreshGroups 回刷组弹窗选项 / showRunStatus），
// 本模块不 import app.js，模块间依赖保持单向。
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
        `</div>`;
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

    const form = document.createElement("div");
    form.className = "form-col";
    form.append(
      field("名称", inpName),
      field("发送者ID（留空使用名称）", inpId),
      field("发送者昵称（留空使用名称）", inpName2),
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
      item.className = "group-item";
      item.innerHTML =
        `<div class="group-head">` +
        `<span class="group-name" title="${escapeHtml(cg.name)}">${escapeHtml(cg.name)}</span>` +
        `<span class="badge">${count} 成员</span>` +
        `<span class="group-actions">` +
        `<button class="icon-btn" data-action="edit" title="编辑群聊">✎</button>` +
        `<button class="icon-btn danger" data-action="delete" title="删除群聊">✕</button>` +
        `</span>` +
        `</div>`;
      item
        .querySelector('[data-action="edit"]')
        .addEventListener("click", () => openChatGroupForm(cg));
      item
        .querySelector('[data-action="delete"]')
        .addEventListener("click", () => deleteChatGroup(cg));
      list.appendChild(item);
    }

    const add = document.createElement("button");
    add.className = "add-block";
    add.textContent = "＋ 新建群聊";
    add.addEventListener("click", () => openChatGroupForm(null));
    list.appendChild(add);
  }

  // 虚拟群聊表单：名称 + 成员多选（checkbox 来自身份池）。
  // 成员引用身份 id；删除身份后成员 id 保留（悬空引用，绑定群聊时按现存身份过滤）。
  function openChatGroupForm(chatGroup) {
    const inpName = document.createElement("input");
    inpName.type = "text";
    inpName.value = chatGroup ? chatGroup.name : "";

    const memberWrap = document.createElement("div");
    memberWrap.className = "form-col";
    memberWrap.style.gap = "4px";
    if (!state.identities.length) {
      const hint = document.createElement("p");
      hint.className = "hint";
      hint.textContent = "暂无身份，请先在上方「身份」区创建";
      memberWrap.appendChild(hint);
    }
    const selected = new Set(chatGroup ? chatGroup.member_ids || [] : []);
    for (const ident of state.identities) {
      const l = document.createElement("label");
      l.className = "settings-field";
      l.style.flexDirection = "row";
      l.style.alignItems = "center";
      l.style.gap = "6px";
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.value = ident.id;
      cb.checked = selected.has(ident.id);
      const span = document.createElement("span");
      span.textContent = `${ident.name}（${ident.sender_id}）`;
      l.append(cb, span);
      memberWrap.appendChild(l);
    }

    const form = document.createElement("div");
    form.className = "form-col";
    form.append(field("群聊名称", inpName), field("成员（勾选加入）", memberWrap));

    openModal({
      title: chatGroup ? `编辑群聊 · ${chatGroup.name}` : "新建群聊",
      content: form,
      okText: chatGroup ? "保存" : "创建",
      onOk: async () => {
        const name = inpName.value.trim();
        if (!name) throw new Error("群聊名称不能为空");
        const memberIds = [];
        memberWrap.querySelectorAll("input[type=checkbox]:checked").forEach((cb) => {
          memberIds.push(cb.value);
        });
        if (chatGroup) await updateChatGroup(chatGroup.id, { name, member_ids: memberIds });
        else await createChatGroup({ name, member_ids: memberIds });
        await refreshChatGroups();
        await refreshGroups();
        showRunStatus("ok", chatGroup ? "虚拟群聊已更新" : `群聊「${name}」已创建`);
      },
    });
  }

  function deleteChatGroup(cg) {
    showModal(`确定删除虚拟群聊「${cg.name || cg.id}」吗？`, {
      danger: true,
      onOk: async () => {
        await deleteChatGroups([cg.id]);
        await refreshChatGroups();
        await refreshGroups();
        showRunStatus("ok", "虚拟群聊已删除");
      },
    });
  }

  return { refreshIdentities, refreshChatGroups, syncBroadcastSenders };
}
