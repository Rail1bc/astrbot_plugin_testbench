// testset_list.js — 左侧测试集列表与测试集编辑/运行弹窗
// 与 group_list.js 同模式：由 app.js 通过 createTestsetList(env) 创建。
// env 注入视图动作（showRunStatus / runTestset / viewTestsetRun），本模块
// 不 import app.js，模块间依赖保持单向。运行本身由后端后台任务驱动，本模块
// 只负责启动与「最近运行」列表展示/找回结果。
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

// 断言类型下拉选项（value 与后端 assertions.py 的规则 type 对应）
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

const RUN_STATUS_TEXT = {
  running: "运行中",
  done: "完成",
  error: "错误",
  cancelled: "已取消",
};

export function createTestsetList(env) {
  const { showRunStatus, runTestset, viewTestsetRun } = env;

  async function refreshTestsets() {
    try {
      const data = await listTestsets();
      state.testsets = Array.isArray(data.testsets) ? data.testsets : [];
    } catch (err) {
      state.testsets = [];
      showRunStatus("error", "加载测试集失败: " + err.message);
    }
    renderTestsetList();
    await renderRecentRuns();
  }

  function renderTestsetList() {
    const list = $("testset-list");
    list.innerHTML = "";
    $("testset-count").textContent = state.testsets.length
      ? `${state.testsets.length} 个测试集`
      : "";

    const add = document.createElement("button");
    add.className = "add-block";
    add.textContent = "＋ 新建测试集";
    add.addEventListener("click", () => openTestsetEditor(null));
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
      item.className = "testset-item";
      item.dataset.id = ts.id;
      const expanded = state.expandedTestsets.has(ts.id);
      const count = (ts.messages || []).length;
      item.innerHTML =
        `<div class="group-head">` +
        `<span class="group-toggle">${expanded ? "▾" : "▸"}</span>` +
        `<span class="group-name">${escapeHtml(ts.name)}</span>` +
        `<span class="badge">${count} 条消息</span>` +
        `<span class="group-actions">` +
        `<button class="btn small" data-action="run">▶ 运行</button>` +
        `<button class="icon-btn" data-action="edit" title="编辑测试集">✎</button>` +
        `<button class="icon-btn danger" data-action="delete" title="删除测试集">✕</button>` +
        `</span>` +
        `</div>` +
        (expanded ? `<div class="testset-msgs">${renderTestsetMsgs(ts)}</div>` : "");

      const head = item.querySelector(".group-head");
      head.addEventListener("click", (e) => {
        if (e.target.closest("button")) return;
        toggleTestset(ts.id);
      });
      item.querySelector('[data-action="run"]').addEventListener("click", () =>
        openTestsetRun(ts),
      );
      item.querySelector('[data-action="edit"]').addEventListener("click", () =>
        openTestsetEditor(ts),
      );
      item
        .querySelector('[data-action="delete"]')
        .addEventListener("click", () => deleteTestset(ts.id));
      list.appendChild(item);
    }
  }

  function renderTestsetMsgs(ts) {
    const msgs = ts.messages || [];
    if (!msgs.length) return '<div class="testset-msg-empty">（空测试集）</div>';
    return msgs
      .map(
        (m, i) =>
          `<div class="testset-msg-line">` +
          `<span class="testset-msg-idx">${i + 1}.</span>` +
          `<span class="testset-msg-text" title="${escapeHtml(m.text)}">${escapeHtml(m.text)}</span>` +
          (m.rule
            ? `<span class="testset-msg-rule">${escapeHtml(ruleSummary(m.rule))}</span>`
            : "") +
          `</div>`,
      )
      .join("");
  }

  function ruleSummary(rule) {
    const found = RULE_TYPES.find(([v]) => v === rule.type);
    const label = found ? found[1] : rule.type;
    if (!RULE_VALUE_TYPES.has(rule.type)) return label;
    const v = Array.isArray(rule.value) ? rule.value.join(", ") : rule.value;
    return v != null && v !== "" ? `${label}: ${v}` : label;
  }

  function toggleTestset(id) {
    if (state.expandedTestsets.has(id)) state.expandedTestsets.delete(id);
    else state.expandedTestsets.add(id);
    renderTestsetList();
  }

  function deleteTestset(id) {
    const ts = state.testsets.find((x) => x.id === id);
    showModal(`确定删除测试集「${ts ? ts.name : id}」吗？`, {
      danger: true,
      onOk: async () => {
        await deleteTestsets([id]);
        await refreshTestsets();
        showRunStatus("ok", "测试集已删除");
      },
    });
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

  // ---------- 测试集编辑弹窗 ----------

  function openTestsetEditor(testset) {
    const isNew = !testset;
    const inpName = document.createElement("input");
    inpName.type = "text";
    inpName.value = testset ? testset.name : "";
    inpName.placeholder = "测试集名称";

    const rowsEl = document.createElement("div");
    rowsEl.className = "form-col";
    rowsEl.style.gap = "6px";

    const addRow = (msg) => {
      const row = document.createElement("div");
      row.className = "testset-msg-row";
      const inp = document.createElement("input");
      inp.type = "text";
      inp.className = "msg-text";
      inp.placeholder = "消息文本";
      if (msg) inp.value = msg.text || "";
      const sel = document.createElement("select");
      sel.className = "rule-type";
      sel.innerHTML = RULE_TYPES.map(
        ([v, label]) => `<option value="${v}">${label}</option>`,
      ).join("");
      if (msg && msg.rule) sel.value = msg.rule.type;
      const val = document.createElement("input");
      val.type = "text";
      val.className = "rule-value";
      val.placeholder = "断言值";
      if (msg && msg.rule && RULE_VALUE_TYPES.has(msg.rule.type)) {
        val.value = Array.isArray(msg.rule.value)
          ? msg.rule.value.join(", ")
          : msg.rule.value != null
            ? String(msg.rule.value)
            : "";
      }
      const del = document.createElement("button");
      del.className = "icon-btn danger";
      del.textContent = "✕";
      del.title = "删除该条消息";
      del.addEventListener("click", () => row.remove());
      const refreshValueVisible = () => {
        val.hidden = !RULE_VALUE_TYPES.has(sel.value);
      };
      sel.addEventListener("change", refreshValueVisible);
      refreshValueVisible();
      row.append(inp, sel, val, del);
      rowsEl.appendChild(row);
    };

    if (testset) (testset.messages || []).forEach(addRow);
    addRow(null); // 默认给一行空行

    const btnAdd = document.createElement("button");
    btnAdd.className = "add-block";
    btnAdd.textContent = "＋ 添加消息";
    btnAdd.addEventListener("click", () => addRow(null));

    const form = document.createElement("div");
    form.className = "form-col";
    form.append(field("测试集名称", inpName), rowsEl, btnAdd);

    openModal({
      title: isNew ? "新建测试集" : `编辑测试集 · ${testset.name}`,
      content: form,
      okText: "保存",
      wide: true,
      onOk: async () => {
        const messages = [];
        for (const row of rowsEl.querySelectorAll(".testset-msg-row")) {
          const text = (row.querySelector(".msg-text").value || "").trim();
          if (!text) continue;
          const rule = buildRule(
            row.querySelector(".rule-type").value,
            row.querySelector(".rule-value").value,
          );
          messages.push({ text, rule });
        }
        if (!messages.length) throw new Error("至少需要一条消息");
        const name = inpName.value.trim();
        if (isNew) await createTestset({ name, messages });
        else await updateTestset({ id: testset.id, name, messages });
        await refreshTestsets();
        showRunStatus("ok", isNew ? "测试集已创建" : "测试集已更新");
      },
    });
  }

  // 行内 rule 构造：空类型 → null；需要值的类型值非空才保留
  function buildRule(type, value) {
    if (!type) return null;
    if (RULE_VALUE_TYPES.has(type)) {
      const v = value.trim();
      if (!v) return null;
      if (type === "min_len" || type === "max_len") {
        const n = parseInt(v, 10);
        return Number.isInteger(n) ? { type, value: n } : null;
      }
      return { type, value: v };
    }
    return { type };
  }

  // ---------- 运行弹窗 ----------

  function openTestsetRun(testset) {
    const msgs = testset.messages || [];
    if (!msgs.length) {
      showModal("该测试集没有消息，请先编辑添加");
      return;
    }
    const openCount = state.openIds.length;
    if (!openCount) {
      showModal("请先在「会话列表」中打开至少一个会话");
      return;
    }
    const allIds = allSessionIds();

    const radioMode = buildRadio(
      [
        ["sequential", "逐条发送", "全部会话完成当前步骤后再发下一条（上下文连续）"],
        ["batch", "批量发送", "所有消息立即连续发出（重叠），再逐个收集结果"],
      ],
      "sequential",
    );
    const radioTarget = buildRadio(
      [
        ["open", `已打开的会话（${openCount} 个）`],
        ["all", `全部会话（${allIds.length} 个）`],
      ],
      "open",
    );

    const form = document.createElement("div");
    form.className = "form-col";
    form.append(field("发送模式", radioMode), field("目标会话", radioTarget));

    openModal({
      title: `运行测试集 · ${testset.name}`,
      content: form,
      okText: "开始运行",
      onOk: async () => {
        const mode = currentRadioValue(radioMode);
        const ids =
          currentRadioValue(radioTarget) === "all" ? allIds : state.openIds.slice();
        env.runTestset(testset, mode, ids);
      },
    });
  }

  function allSessionIds() {
    const ids = [];
    for (const g of state.groups) for (const s of g.sessions || []) ids.push(s.id);
    return ids;
  }

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

  return { refreshTestsets, renderTestsetList };
}
