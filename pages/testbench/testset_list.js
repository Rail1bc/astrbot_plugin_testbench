// testset_list.js — 左侧测试集列表 + 右侧测试集编辑窗口 + 运行弹窗
// 与 group_list.js 同模式：由 app.js 通过 createTestsetList(env) 创建。
// env 注入视图动作（showRunStatus / runTestset / viewTestsetRun /
// switchToTestsets），本模块不 import app.js，模块间依赖保持单向。
// 列表条目是「有名字的条目」：选中后右侧显示编辑窗口；运行由后端后台任务
// 驱动，本模块只负责启动与「最近运行」列表展示/找回结果。
import {
  createTestset,
  deleteTestsets,
  listTestsetRuns,
  listTestsets,
  updateTestset,
} from "./api.js";
import { state } from "./state.js";
import { openModal, showModal } from "./modal.js";
import { escapeHtml } from "./utils.js";

const $ = (id) => document.getElementById(id);

// 断言类型下拉选项（value 与后端 assertions.py 的规则 type 对应）——行渲染
// 与收集的唯一来源，未来新增测试行为只改这里 + renderMsgRow/collectEditorRows
// + 后端 _normalize_messages。
const RULE_TYPES = [
  ["", "无"],
  ["contains", "包含"],
  ["not_contains", "不包含"],
  ["regex", "正则匹配"],
  ["json", "合法 JSON"],
  ["non_empty", "非空"],
  ["min_len", "最少字数"],
  ["max_len", "最多字数"],
  ["prefix", "前缀"],
  ["suffix", "后缀"],
];

// 需要「断言值」输入的规则类型（json / non_empty 不需要）
const RULE_VALUE_TYPES = new Set([
  "contains",
  "not_contains",
  "regex",
  "min_len",
  "max_len",
  "prefix",
  "suffix",
]);

// 导出 / 导入信封：format/version 为未来「测试集市场」（网络下载）预留兼容面
const EXPORT_FORMAT = "astrbot-testbench-testset";
const EXPORT_VERSION = 1;

const RUN_STATUS_TEXT = {
  running: "运行中",
  done: "完成",
  error: "错误",
  cancelled: "已取消",
};

export function createTestsetList(env) {
  const { showRunStatus, runTestset, viewTestsetRun, switchToTestsets } = env;

  // 编辑窗口是否有未保存的修改（任一行输入/勾选变化即置位）
  let dirty = false;

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
    if (!dirty) renderTestsetEditor();
  }

  function renderTestsetNav() {
    const list = $("testset-list");
    list.innerHTML = "";
    $("testset-count").textContent = state.testsets.length
      ? `${state.testsets.length} 个测试集`
      : "";

    const add = document.createElement("button");
    add.className = "add-block";
    add.textContent = "＋ 新建测试集";
    add.addEventListener("click", () => openNewTestset());
    list.appendChild(add);

    if (!state.testsets.length) {
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent = "暂无测试集，点上方「＋」创建";
      list.appendChild(empty);
      return;
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
  }

  function selectTestset(id) {
    if (dirty) {
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
    renderTestsetEditor();
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
        dirty = false;
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
        dirty = false; // 未保存修改随测试集一起没了，清脏让编辑器重绘为空态
        await refreshTestsets();
        showRunStatus("ok", "测试集已删除");
      },
    });
  }

  // ---------- 右侧编辑窗口 ----------

  function currentSelected() {
    return state.testsets.find((t) => t.id === state.selectedTestsetId) || null;
  }

  // 编辑窗口按钮在未选中任何测试集时也可见；此时点击要给明确指引，
  // 而不是静默无效（曾出现：点「＋ 添加消息」能加行，但保存被挡住无任何反馈）
  function requireSelected() {
    const ts = currentSelected();
    if (!ts) showModal("请先在左侧选择或创建一个测试集");
    return ts;
  }

  function renderTestsetEditor() {
    dirty = false;
    const ts = currentSelected();
    $("ts-name").value = ts ? ts.name : "";
    $("ts-meta").textContent = ts
      ? `${(ts.messages || []).length} 条消息 · ${formatTime(ts.created_at)}`
      : "";
    $("ts-dirty").hidden = true;
    const rowsEl = $("ts-messages");
    rowsEl.innerHTML = "";
    if (!ts) {
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent =
        "在左侧选择一个测试集，或点「＋ 新建测试集」创建；选中后在此编辑消息、断言与批量发送范围。";
      rowsEl.appendChild(empty);
      $("ts-segments").textContent = "";
      return;
    }
    const batchSet = new Set();
    for (const [s, e] of ts.batch_ranges || []) {
      for (let i = s; i <= e; i++) batchSet.add(i);
    }
    const msgs = ts.messages || [];
    msgs.forEach((m, i) => rowsEl.appendChild(renderMsgRow(m, i, batchSet.has(i))));
    if (!msgs.length) addMsgRow(); // 空测试集给一行待编辑
    updateSegments();
  }

  // 单行构建：序号 / 文本 / 规则类型 / 规则值（值类规则才显示）/ 批量勾选 / 删除。
  // 未来新增测试行为在此处加控件，并在 collectEditorRows() 同步收集。
  function renderMsgRow(msg, idx, batchChecked) {
    const row = document.createElement("div");
    row.className = "ts-msg-row";

    const idxEl = document.createElement("span");
    idxEl.className = "ts-msg-idx";
    idxEl.textContent = idx + 1;

    const inp = document.createElement("input");
    inp.type = "text";
    inp.className = "ts-msg-text";
    inp.placeholder = "消息文本";
    if (msg) inp.value = msg.text || "";

    const sel = document.createElement("select");
    sel.className = "ts-msg-rule-type";
    sel.innerHTML = RULE_TYPES.map(
      ([v, label]) => `<option value="${v}">${label}</option>`,
    ).join("");
    if (msg && msg.rule) sel.value = msg.rule.type;

    const val = document.createElement("input");
    val.type = "text";
    val.className = "ts-msg-rule-value";
    val.placeholder = "断言值";
    if (msg && msg.rule && RULE_VALUE_TYPES.has(msg.rule.type)) {
      val.value = Array.isArray(msg.rule.value)
        ? msg.rule.value.join(", ")
        : msg.rule.value != null
          ? String(msg.rule.value)
          : "";
    }

    const batch = document.createElement("label");
    batch.className = "ts-msg-batch";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = !!batchChecked;
    batch.append(cb, document.createTextNode("批量"));

    const del = document.createElement("button");
    del.className = "icon-btn danger";
    del.textContent = "✕";
    del.title = "删除该条消息";
    del.addEventListener("click", () => {
      row.remove();
      markDirty();
      reindexRows();
      updateSegments();
    });

    const refreshValueVisible = () => {
      val.hidden = !RULE_VALUE_TYPES.has(sel.value);
    };
    sel.addEventListener("change", () => {
      refreshValueVisible();
      markDirty();
    });
    inp.addEventListener("input", markDirty);
    val.addEventListener("input", markDirty);
    cb.addEventListener("change", () => {
      markDirty();
      updateSegments();
    });
    refreshValueVisible();

    row.append(idxEl, inp, sel, val, batch, del);
    return row;
  }

  function reindexRows() {
    const rows = $("ts-messages").querySelectorAll(".ts-msg-row");
    rows.forEach((r, i) => {
      r.querySelector(".ts-msg-idx").textContent = i + 1;
    });
  }

  function markDirty() {
    dirty = true;
    $("ts-dirty").hidden = false;
  }

  // 连续勾选的行合并为批量段（单条勾选 = [i,i]），实时预览。
  // 空文本行不会保存（collectEditorRows 丢弃），分段索引须基于保留后的行，与保存一致
  function checkedSegments() {
    const segs = [];
    const rows = $("ts-messages").querySelectorAll(".ts-msg-row");
    let start = -1;
    let kept = 0;
    rows.forEach((row) => {
      const text = row.querySelector(".ts-msg-text").value.trim();
      const checked = row.querySelector(".ts-msg-batch input").checked;
      if (!text) return;
      if (checked && start < 0) start = kept;
      else if (!checked && start >= 0) {
        segs.push([start, kept - 1]);
        start = -1;
      }
      kept += 1;
    });
    if (start >= 0) segs.push([start, kept - 1]);
    return segs;
  }

  function updateSegments() {
    const segs = checkedSegments();
    $("ts-segments").textContent = segs.length
      ? "批量段：" +
        segs
          .map(([s, e]) => (s === e ? `${s + 1}` : `${s + 1}-${e + 1}`))
          .join("、")
      : "无批量段（全部逐条发送）";
  }

  function addMsgRow() {
    const rowsEl = $("ts-messages");
    const empty = rowsEl.querySelector(".empty");
    if (empty) empty.remove();
    rowsEl.appendChild(
      renderMsgRow(null, rowsEl.querySelectorAll(".ts-msg-row").length, false),
    );
    updateSegments();
  }

  // 收集编辑器行：空文本行视为删除，批量段索引基于保留后的消息序列
  function collectEditorRows() {
    const messages = [];
    const batchFlags = [];
    for (const row of $("ts-messages").querySelectorAll(".ts-msg-row")) {
      const text = row.querySelector(".ts-msg-text").value.trim();
      if (!text) continue;
      const rule = buildRule(
        row.querySelector(".ts-msg-rule-type").value,
        row.querySelector(".ts-msg-rule-value").value,
      );
      messages.push({ text, rule });
      batchFlags.push(row.querySelector(".ts-msg-batch input").checked);
    }
    const batchRanges = [];
    let start = -1;
    batchFlags.forEach((c, i) => {
      if (c && start < 0) start = i;
      else if (!c && start >= 0) {
        batchRanges.push([start, i - 1]);
        start = -1;
      }
    });
    if (start >= 0) batchRanges.push([start, batchFlags.length - 1]);
    return { messages, batchRanges };
  }

  // 行内 rule 构造：空类型 → null；需要值的类型值非空才保留
  function buildRule(type, value) {
    if (!type) return null;
    if (RULE_VALUE_TYPES.has(type)) {
      const v = value.trim();
      if (!v) return null;
      if (type === "min_len" || type === "max_len") {
        const n = Number(v);
        return Number.isInteger(n) ? { type, value: n } : null;
      }
      return { type, value: v };
    }
    return { type };
  }

  // 断言类型的中文名（错误提示用；RULE_TYPES 是唯一来源）
  function ruleTypeLabel(type) {
    const found = RULE_TYPES.find(([v]) => v === type);
    return found ? found[1] : type;
  }

  // 保存 / 导出前的断言值校验：值类规则空值、min_len/max_len 非整数会被
  // buildRule 静默丢弃（规则不生效），这里先带行号提示，而不是无声吞掉。
  // 返回错误文案；全部合法返回 null。
  function validateEditorRows() {
    const rows = $("ts-messages").querySelectorAll(".ts-msg-row");
    let kept = 0; // 与 collectEditorRows 一致：空文本行不计入
    for (const row of rows) {
      const text = row.querySelector(".ts-msg-text").value.trim();
      if (!text) continue;
      const type = row.querySelector(".ts-msg-rule-type").value;
      const value = row.querySelector(".ts-msg-rule-value").value.trim();
      if (RULE_VALUE_TYPES.has(type)) {
        if (!value) {
          return `第 ${kept + 1} 条消息：规则「${ruleTypeLabel(type)}」未填写断言值，该规则不会生效`;
        }
        if (type === "min_len" || type === "max_len") {
          const n = Number(value);
          if (!Number.isInteger(n)) {
            return `第 ${kept + 1} 条消息：规则「${ruleTypeLabel(type)}」的断言值必须是整数`;
          }
        }
      }
      kept += 1;
    }
    return null;
  }

  async function saveEditor() {
    const ts = requireSelected();
    if (!ts) return;
    const err = validateEditorRows();
    if (err) {
      showModal(err);
      return;
    }
    const name = $("ts-name").value.trim() || "测试集";
    const { messages, batchRanges } = collectEditorRows();
    try {
      await updateTestset({ id: ts.id, name, messages, batch_ranges: batchRanges });
      // 保存成功即清脏：否则 refreshTestsets 的 `if (!dirty)` 会跳过编辑器重绘，
      // dirty 永不清除——「未保存」提示残留、切换测试集误报丢弃、再次保存前先误弹保存
      dirty = false;
      await refreshTestsets();
      showRunStatus("ok", "测试集已保存");
    } catch (err) {
      showRunStatus("error", "保存失败: " + err.message);
    }
  }

  function runSelected() {
    const ts = requireSelected();
    if (!ts) return;
    if (dirty) {
      showModal("当前测试集有未保存的修改。是否先保存再运行？", {
        onOk: async () => {
          await saveEditor();
          const fresh = currentSelected();
          if (fresh) openTestsetRun(fresh);
        },
      });
      return;
    }
    openTestsetRun(ts);
  }

  // ---------- 导出 / 导入 ----------

  function exportTestset() {
    const ts = requireSelected();
    if (!ts) return;
    const doExport = () => {
      const err = validateEditorRows();
      if (err) {
        showModal(err);
        return;
      }
      const { messages, batchRanges } = collectEditorRows();
      const envelope = {
        format: EXPORT_FORMAT,
        version: EXPORT_VERSION,
        name: $("ts-name").value.trim() || ts.name,
        messages,
        batch_ranges: batchRanges,
      };
      const blob = new Blob([JSON.stringify(envelope, null, 2)], {
        type: "application/json",
      });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = (envelope.name || "测试集") + ".json";
      a.click();
      URL.revokeObjectURL(a.href);
      showRunStatus("ok", "测试集已导出");
    };
    if (dirty) {
      showModal("当前测试集有未保存的修改。是否先保存再导出？", {
        onOk: async () => {
          await saveEditor();
          doExport();
        },
      });
      return;
    }
    doExport();
  }

  function importTestset() {
    if (dirty) {
      showModal("当前测试集有未保存的修改，导入后将丢失这些修改，确定继续吗？", {
        danger: true,
        onOk: () => $("ts-import-file").click(),
      });
      return;
    }
    $("ts-import-file").click();
  }

  // 信封解析（校验 format/version，预留「测试集市场」下载路径：传入 JSON 文本即可）
  function parseTestsetEnvelope(text) {
    let data;
    try {
      data = JSON.parse(text);
    } catch (err) {
      throw new Error("文件不是合法 JSON: " + err.message);
    }
    if (!data || data.format !== EXPORT_FORMAT) {
      throw new Error("不是有效的测试集文件（format 不匹配）");
    }
    if (typeof data.version !== "number" || data.version > EXPORT_VERSION) {
      throw new Error("不支持的测试集格式版本: " + String(data.version));
    }
    const name =
      typeof data.name === "string" && data.name.trim()
        ? data.name.trim()
        : "导入的测试集";
    if (!Array.isArray(data.messages)) {
      throw new Error("测试集文件缺少 messages 数组");
    }
    const messages = [];
    for (const m of data.messages) {
      const text = m && typeof m.text === "string" ? m.text.trim() : "";
      if (!text) continue;
      const rule = m && m.rule != null ? { ...m.rule } : null;
      messages.push({ text, rule });
    }
    let batchRanges = [];
    if (Array.isArray(data.batch_ranges)) {
      batchRanges = data.batch_ranges
        .filter(
          (r) =>
            Array.isArray(r) &&
            r.length === 2 &&
            typeof r[0] === "number" &&
            typeof r[1] === "number" &&
            Number.isInteger(r[0]) &&
            Number.isInteger(r[1]) &&
            !(r[0] > r[1]),
        )
        .map(([s, e]) => [s, e]);
    }
    return { name, messages, batch_ranges: batchRanges };
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

  function field(label, input) {
    const l = document.createElement("label");
    l.className = "settings-field";
    const span = document.createElement("span");
    span.textContent = label;
    l.appendChild(span);
    l.appendChild(input);
    return l;
  }

  // ---------- 编辑器按钮绑定 ----------

  $("btn-ts-save").addEventListener("click", () => void saveEditor());
  $("btn-ts-run").addEventListener("click", () => runSelected());
  $("btn-ts-export").addEventListener("click", () => exportTestset());
  $("btn-ts-import").addEventListener("click", () => importTestset());
  $("btn-ts-delete").addEventListener("click", () => {
    if (state.selectedTestsetId) deleteTestset(state.selectedTestsetId);
  });
  $("btn-ts-add-msg").addEventListener("click", () => {
    if (!currentSelected()) {
      showModal("请先在左侧选择或创建一个测试集，再添加消息");
      return;
    }
    markDirty();
    addMsgRow();
  });
  $("ts-name").addEventListener("input", markDirty);
  $("ts-import-file").addEventListener("change", async (e) => {
    const file = e.target.files[0];
    e.target.value = ""; // 允许再次导入同一文件
    if (!file) return;
    try {
      const parsed = parseTestsetEnvelope(await file.text());
      const ts = await createTestset({
        name: parsed.name,
        messages: parsed.messages,
        batch_ranges: parsed.batch_ranges,
      });
      dirty = false;
      await refreshTestsets();
      doSelect(ts.id);
      showRunStatus("ok", `测试集「${parsed.name}」已导入`);
    } catch (err) {
      showModal("导入失败: " + err.message);
    }
  });

  return { refreshTestsets, renderTestsetNav };
}
