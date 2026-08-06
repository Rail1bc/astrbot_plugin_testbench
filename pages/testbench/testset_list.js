// testset_list.js — 左侧测试集列表 + 运行弹窗 + 最近运行
// 与 group_list.js 同模式：由 app.js 通过 createTestsetList(env) 创建。
// env 注入视图动作（showRunStatus / runTestset / viewTestsetRun /
// switchToTestsets），本模块不 import app.js，模块间依赖保持单向。
// 列表条目是「有名字的条目」：选中后右侧显示编辑窗口（渲染/保存/导出/导入
// 在 testset_editor.js，经 setDeps 装配）；运行由后端后台任务驱动，
// 本模块只负责启动与「最近运行」列表展示/找回结果。
import {
  createTestset,
  deleteTestsets,
  listTestsetRuns,
  listTestsets,
} from "./api.js";
import { state } from "./state.js";
import { openModal, showModal } from "./modal.js";
import { escapeHtml, field } from "./utils.js";
import { createTestsetEditor } from "./testset_editor.js";

const $ = (id) => document.getElementById(id);

const RUN_STATUS_TEXT = {
  running: "运行中",
  done: "完成",
  error: "错误",
  cancelled: "已取消",
};

export function createTestsetList(env) {
  const { showRunStatus, runTestset, viewTestsetRun, switchToTestsets } = env;

  // 右侧编辑窗口（testset_editor.js）：互相引用的列表侧函数创建后经 setDeps
  // 装配（见文件底部），避免循环 import
  const editor = createTestsetEditor({ showRunStatus });

  // ---------- 列表刷新与左侧导航 ----------

  async function refreshTestsets() {
    try {
      const data = await listTestsets();
      state.testsets = Array.isArray(data.testsets) ? data.testsets : [];
    } catch (err) {
      state.testsets = [];
      showRunStatus("error", "加载测试集失败: " + err.message);
    }
    renderTestsetNav();
    syncRunTestsetSelect();
    await renderRecentRuns();
    if (
      state.selectedTestsetId &&
      !state.testsets.some((t) => t.id === state.selectedTestsetId)
    ) {
      state.selectedTestsetId = null; // 选中项已被删除
    }
    // 有未保存修改时不重绘编辑窗口（避免运行完成等异步刷新冲掉用户编辑）
    if (!editor.getDirty()) editor.renderTestsetEditor();
  }

  function renderTestsetNav() {
    const list = $("testset-list");
    list.innerHTML = "";
    $("testset-count").textContent = state.testsets.length
      ? `${state.testsets.length} 个测试集`
      : "";

    if (!state.testsets.length) {
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent = "暂无测试集，点下方「＋」创建";
      list.appendChild(empty);
    }
    for (const ts of state.testsets) {
      const item = document.createElement("div");
      item.className =
        "testset-nav-item" + (ts.id === state.selectedTestsetId ? " active" : "");
      item.dataset.id = ts.id;
      const count = (ts.messages || []).length;
      item.innerHTML =
        `<span class="name" title="${escapeHtml(ts.name)}">${escapeHtml(ts.name)}</span>` +
        `<span class="badge">${count} 条消息</span>`;
      item.addEventListener("click", () => selectTestset(ts.id));
      list.appendChild(item);
    }

    // 列表内「＋」块：点击创建测试集（置于已有测试集下方）
    const add = document.createElement("button");
    add.className = "add-block";
    add.textContent = "＋ 新建测试集";
    add.addEventListener("click", () => openNewTestset());
    list.appendChild(add);
  }

  function selectTestset(id) {
    if (editor.getDirty()) {
      showModal("当前测试集有未保存的修改，切换将丢弃这些修改，确定吗？", {
        danger: true,
        onOk: () => doSelect(id),
      });
      return;
    }
    doSelect(id);
  }

  function doSelect(id) {
    state.selectedTestsetId = id;
    renderTestsetNav();
    editor.renderTestsetEditor();
    switchToTestsets();
  }

  function openNewTestset() {
    const inp = document.createElement("input");
    inp.type = "text";
    inp.value = "测试集";
    inp.placeholder = "测试集名称";
    const form = document.createElement("div");
    form.className = "form-col";
    form.append(field("测试集名称", inp));
    openModal({
      title: "新建测试集",
      content: form,
      okText: "创建",
      onOk: async () => {
        const name = inp.value.trim() || "测试集";
        const ts = await createTestset({ name, messages: [] });
        editor.clearDirty();
        await refreshTestsets();
        doSelect(ts.id);
        showRunStatus("ok", `测试集「${name}」已创建`);
      },
    });
  }

  function deleteTestset(id) {
    const ts = state.testsets.find((x) => x.id === id);
    showModal(`确定删除测试集「${ts ? ts.name : id}」吗？`, {
      danger: true,
      onOk: async () => {
        await deleteTestsets([id]);
        if (state.selectedTestsetId === id) state.selectedTestsetId = null;
        editor.clearDirty(); // 未保存修改随测试集一起没了，清脏让编辑器重绘为空态
        await refreshTestsets();
        showRunStatus("ok", "测试集已删除");
      },
    });
  }

  // ---------- 运行弹窗 ----------

  function openTestsetRun(testset) {
    const msgs = testset.messages || [];
    if (!msgs.length) {
      showModal("该测试集没有消息，请先编辑添加");
      return;
    }
    const openCount = state.openIds.length;
    if (!openCount && !state.groups.length) {
      showModal("请先在「会话列表」中打开至少一个会话");
      return;
    }
    const allIds = allSessionIds();
    const radioTarget = buildRadio(
      [
        ["open", `已打开的会话（${openCount} 个）`],
        ["all", `全部会话（${allIds.length} 个）`],
        ["groups", "选择测试组"],
      ],
      openCount ? "open" : "all",
    );
    const groupsBox = buildGroupCheckboxes();
    groupsBox.hidden = true;
    radioTarget.querySelectorAll("input").forEach((r) => {
      r.addEventListener("change", () => {
        groupsBox.hidden = currentRadioValue(radioTarget) !== "groups";
      });
    });

    const form = document.createElement("div");
    form.className = "form-col";
    form.append(field("目标会话", radioTarget), groupsBox);

    openModal({
      title: `运行测试集 · ${testset.name}`,
      content: form,
      okText: "开始运行",
      onOk: async () => {
        const target = currentRadioValue(radioTarget);
        let ids;
        if (target === "all") {
          ids = allIds;
        } else if (target === "groups") {
          ids = selectedGroupSessionIds();
          if (!ids.length) throw new Error("请至少勾选一个测试组");
        } else {
          ids = state.openIds.slice();
        }
        env.runTestset(testset, ids);
      },
    });
  }

  function allSessionIds() {
    const ids = [];
    for (const g of state.groups) for (const s of g.sessions || []) ids.push(s.id);
    return ids;
  }

  // 组多选：勾选组 → 解析为该组全部会话 id（含未打开的会话）
  function buildGroupCheckboxes() {
    const wrap = document.createElement("div");
    wrap.className = "form-col";
    wrap.style.gap = "4px";
    const groups = state.groups.filter((g) => (g.sessions || []).length);
    if (!groups.length) {
      const hint = document.createElement("p");
      hint.className = "hint";
      hint.textContent = "暂无包含会话的测试组";
      wrap.appendChild(hint);
      return wrap;
    }
    for (const g of groups) {
      const l = document.createElement("label");
      l.className = "settings-field";
      const r = document.createElement("input");
      r.type = "checkbox";
      r.dataset.gid = g.id;
      const span = document.createElement("span");
      span.textContent = `${g.name}（${(g.sessions || []).length} 个会话）`;
      l.append(r, span);
      wrap.appendChild(l);
    }
    return wrap;
  }

  function selectedGroupSessionIds() {
    const ids = new Set();
    document.querySelectorAll("#modal-body input[data-gid]:checked").forEach((box) => {
      const g = state.groups.find((x) => x.id === box.dataset.gid);
      for (const s of g ? g.sessions || [] : []) ids.add(s.id);
    });
    return [...ids];
  }

  // ---------- 最近运行 ----------

  async function renderRecentRuns() {
    const el = $("recent-runs");
    let runs = [];
    try {
      const data = await listTestsetRuns();
      runs = Array.isArray(data.runs) ? data.runs : [];
    } catch (err) {
      el.innerHTML = '<div class="hint">加载最近运行失败</div>';
      return;
    }
    if (!runs.length) {
      el.innerHTML = '<div class="hint">暂无运行记录</div>';
      return;
    }
    el.innerHTML = runs
      .map(
        (r) =>
          `<div class="recent-run">` +
          `<span class="chip ${escapeHtml(r.status)}">${escapeHtml(RUN_STATUS_TEXT[r.status] || r.status)}</span>` +
          `<span class="recent-run-name" title="${escapeHtml(r.testset_name || "")}">${escapeHtml(r.testset_name || r.testset_id)}</span>` +
          `<span class="recent-run-time">${escapeHtml(formatTime(r.started_at))}</span>` +
          `<button class="btn small" data-run-id="${escapeHtml(r.run_id)}">查看</button>` +
          `</div>`,
      )
      .join("");
    el.querySelectorAll("[data-run-id]").forEach((btn) => {
      btn.addEventListener("click", () => viewTestsetRun(btn.dataset.runId));
    });
  }

  function formatTime(epochSec) {
    if (!epochSec) return "";
    const d = new Date(epochSec * 1000);
    const pad = (n) => String(n).padStart(2, "0");
    return `${d.getMonth() + 1}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }

  // ---------- 群发栏测试集下拉同步 ----------

  function syncRunTestsetSelect() {
    const sel = $("run-testset");
    const current = sel.value;
    sel.innerHTML =
      `<option value="">选择测试集…</option>` +
      state.testsets
        .map(
          (t) => `<option value="${escapeHtml(t.id)}">${escapeHtml(t.name)}</option>`,
        )
        .join("");
    if (current && state.testsets.some((t) => t.id === current)) sel.value = current;
  }

  // ---------- 共享小工具 ----------

  function buildRadio(options, initial) {
    const wrap = document.createElement("div");
    wrap.className = "modal-radio-row";
    const name = "tsr_" + Math.random().toString(36).slice(2, 8);
    for (const [value, label] of options) {
      const l = document.createElement("label");
      const r = document.createElement("input");
      r.type = "radio";
      r.name = name;
      r.value = value;
      if (value === initial) r.checked = true;
      l.appendChild(r);
      l.appendChild(document.createTextNode(label));
      wrap.appendChild(l);
    }
    return wrap;
  }

  function currentRadioValue(wrap) {
    const checked = wrap.querySelector("input:checked");
    return checked ? checked.value : "";
  }

  // 编辑器依赖装配：互相引用的函数在创建后注入（函数声明可提升，放哪都行；
  // 统一放底部便于发现全部交叉点）
  editor.setDeps(() => ({
    formatTime,
    openTestsetRun,
    deleteTestset,
    doSelect,
    refreshTestsets,
  }));

  return { refreshTestsets, renderTestsetNav };
}
