// testset_editor.js — 右侧测试集编辑窗口：消息行 / 断言规则 / 批量段 + 保存 / 导出 / 导入
// 由 testset_list.js 创建（createTestsetEditor(env)），env 注入视图动作
// （showRunStatus）。列表侧函数（formatTime / openTestsetRun / deleteTestset /
// doSelect / refreshTestsets）经 setDeps 注入——编辑器与列表互相引用，直接
// import 会成环，用 setter 延迟取，依赖保持单向（本模块不 import testset_list.js）。
import { createTestset, updateTestset } from "./api.js";
import { showModal } from "./modal.js";
import { state } from "./state.js";
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

export function createTestsetEditor(env) {
  const { showRunStatus } = env;

  // 编辑窗口是否有未保存的修改（任一行输入/勾选变化即置位）
  let dirty = false;

  // 列表侧依赖（formatTime / openTestsetRun / deleteTestset / doSelect /
  // refreshTestsets）经 setDeps 注入：互相引用须在创建后装配，避免循环 import
  let getDeps = () => null;
  function setDeps(getter) {
    getDeps = getter;
  }

  // 当前选中的测试集（列表条目选中后右侧编辑它）
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
      ? `${(ts.messages || []).length} 条消息 · ${getDeps().formatTime(ts.created_at)}`
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

  // 单行构建：序号 / 文本 / 规则类型 / 规则值（值类规则才显示）/ 发送身份 /
  // 自动@ / 批量勾选 / 删除。未来新增测试行为在此处加控件，并在
  // collectEditorRows() 同步收集。
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

    // 身份（可选）：该条消息以哪个测试身份发送；值即身份 sender_id。
    // 身份被删除后选项保留原 sender 值（sender_name 存 data-raw-name），
    // 保存时不静默丢绑定。
    const senderSel = document.createElement("select");
    senderSel.className = "ts-msg-sender";
    senderSel.title = "发送身份（可选）";
    const rawSender = msg && msg.sender_id ? msg.sender_id : "";
    const matchedIdentity = state.identities.find((i) => i.sender_id === rawSender);
    senderSel.innerHTML =
      `<option value="">默认身份</option>` +
      state.identities
        .map(
          (i) =>
            `<option value="${escapeHtml(i.sender_id)}">${escapeHtml(i.name)}</option>`,
        )
        .join("");
    if (rawSender && matchedIdentity) {
      senderSel.value = rawSender;
    } else if (rawSender) {
      const opt = document.createElement("option");
      opt.value = rawSender;
      opt.textContent = `${msg.sender_name || rawSender}（身份已删除）`;
      senderSel.appendChild(opt);
      senderSel.value = rawSender;
      senderSel.dataset.rawName = msg.sender_name || "";
    }
    senderSel.addEventListener("change", markDirty);

    // 自动@（可选）：该条消息是否模拟「@机器人」发言唤醒（群聊消息有意义）。
    // 缺省开启；旧导入数据没有 auto_at 字段时按开启处理。
    const autoAt = document.createElement("label");
    autoAt.className = "ts-msg-auto-at";
    const atCb = document.createElement("input");
    atCb.type = "checkbox";
    atCb.checked = !msg || msg.auto_at !== false;
    atCb.title = "自动@（模拟「@机器人」发言唤醒）";
    autoAt.append(atCb, document.createTextNode("@"));
    atCb.addEventListener("change", markDirty);

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

    row.append(idxEl, inp, sel, val, senderSel, autoAt, batch, del);
    return row;
  }

  // 行内身份下拉 → sender 字段：未选返回空对象；身份仍存在取其
  // sender_id/sender_name，身份已删除时回退原 sender_name（data-raw-name）
  // 或 sender_id
  function collectSender(sel) {
    if (!sel || !sel.value) return {};
    const ident = state.identities.find((i) => i.sender_id === sel.value);
    if (ident) return { sender_id: ident.sender_id, sender_name: ident.sender_name };
    return { sender_id: sel.value, sender_name: sel.dataset.rawName || sel.value };
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

  // 连续 true 标志合并为区间（单条 = [i,i]）：勾选行实时预览与保存收集共用
  function rangesFromFlags(flags) {
    const ranges = [];
    let start = -1;
    flags.forEach((c, i) => {
      if (c && start < 0) start = i;
      else if (!c && start >= 0) {
        ranges.push([start, i - 1]);
        start = -1;
      }
    });
    if (start >= 0) ranges.push([start, flags.length - 1]);
    return ranges;
  }

  // 连续勾选的行合并为批量段（单条勾选 = [i,i]），实时预览。
  // 空文本行不会保存（collectEditorRows 丢弃），分段索引须基于保留后的行，与保存一致
  function checkedSegments() {
    const flags = [];
    for (const row of $("ts-messages").querySelectorAll(".ts-msg-row")) {
      const text = row.querySelector(".ts-msg-text").value.trim();
      if (!text) continue;
      flags.push(row.querySelector(".ts-msg-batch input").checked);
    }
    return rangesFromFlags(flags);
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

  // 收集编辑器行：空文本行视为删除，批量段索引基于保留后的消息序列；
  // 每条消息可选身份（sender_id/sender_name，见 renderMsgRow 的身份下拉）
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
      const sender = collectSender(row.querySelector(".ts-msg-sender"));
      const atCb = row.querySelector(".ts-msg-auto-at input");
      messages.push({ text, rule, ...sender, auto_at: atCb.checked });
      batchFlags.push(row.querySelector(".ts-msg-batch input").checked);
    }
    return { messages, batchRanges: rangesFromFlags(batchFlags) };
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
    if (!ts) return false;
    const err = validateEditorRows();
    if (err) {
      showModal(err);
      return false;
    }
    const name = $("ts-name").value.trim() || "测试集";
    const { messages, batchRanges } = collectEditorRows();
    try {
      await updateTestset({ id: ts.id, name, messages, batch_ranges: batchRanges });
      // 保存成功即清脏：否则 refreshTestsets 的 `if (!dirty)` 会跳过编辑器重绘，
      // dirty 永不清除——「未保存」提示残留、切换测试集误报丢弃、再次保存前先误弹保存
      dirty = false;
      await getDeps().refreshTestsets();
      showRunStatus("ok", "测试集已保存");
      return true;
    } catch (err) {
      showRunStatus("error", "保存失败: " + err.message);
      return false;
    }
  }

  function runSelected() {
    const ts = requireSelected();
    if (!ts) return;
    if (dirty) {
      showModal("当前测试集有未保存的修改。是否先保存再运行？", {
        onOk: async () => {
          // 保存失败中止：继续运行会跑旧版本内容，与编辑器显示不一致
          if (!(await saveEditor())) return;
          const fresh = currentSelected();
          if (fresh) getDeps().openTestsetRun(fresh);
        },
      });
      return;
    }
    getDeps().openTestsetRun(ts);
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
          // 保存失败中止：导出的会是编辑器里未保存的内容，与「先保存」承诺不符
          if (await saveEditor()) doExport();
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
      const message = { text, rule };
      // 可选 sender / auto_at（向后兼容：缺省字段的旧信封照常导入，auto_at
      // 缺省视为开启——渲染时按 `!== false` 勾选）
      if (m && typeof m.sender_id === "string" && m.sender_id) {
        message.sender_id = m.sender_id;
      }
      if (m && typeof m.sender_name === "string" && m.sender_name) {
        message.sender_name = m.sender_name;
      }
      if (m && typeof m.auto_at === "boolean") {
        message.auto_at = m.auto_at;
      }
      messages.push(message);
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

  // ---------- 编辑器按钮绑定 ----------

  $("btn-ts-save").addEventListener("click", () => void saveEditor());
  $("btn-ts-run").addEventListener("click", () => runSelected());
  $("btn-ts-export").addEventListener("click", () => exportTestset());
  $("btn-ts-import").addEventListener("click", () => importTestset());
  $("btn-ts-delete").addEventListener("click", () => {
    const ts = currentSelected();
    if (ts) getDeps().deleteTestset(ts.id);
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
      await getDeps().refreshTestsets();
      getDeps().doSelect(ts.id);
      showRunStatus("ok", `测试集「${parsed.name}」已导入`);
    } catch (err) {
      showModal("导入失败: " + err.message);
    }
  });

  return {
    renderTestsetEditor,
    getDirty: () => dirty,
    clearDirty: () => {
      dirty = false;
    },
    setDeps,
  };
}
