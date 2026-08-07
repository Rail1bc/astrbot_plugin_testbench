// testset_editor.js — 右侧测试集编辑窗口：消息行 / 断言规则 / 批量段 + 保存 / 导出 / 导入
// + 报告视图（页眉「编辑 / 报告」切换：该测试集的最近运行与持久化报告）。
// 由 testset_list.js 创建（createTestsetEditor(env)），env 注入视图动作
// （showRunStatus）。列表侧函数（formatTime / openTestsetRun / deleteTestset /
// doSelect / refreshTestsets / viewTestsetRun）经 setDeps 注入——编辑器与列表
// 互相引用，直接 import 会成环，用 setter 延迟取，依赖保持单向（本模块不
// import testset_list.js）。结果表格渲染复用 testset_run.js 的模块级导出
// buildResultsTable / renderFinalVerdicts（testset_run.js 不 import 本模块）。
// 评审 Profile 的增删改管理已迁到左侧测试集列表（testset_list.js 的「评审
// Profile」tab），本模块只负责消息规则 / 最终断言行内 profile 下拉的构建与
// 重建（refreshAllProfileSelects，供列表侧 profile 变更后刷新）。
import {
  createTestset,
  deleteReports,
  listReports,
  listTestsetRuns,
  retryReviews,
  updateTestset,
} from "./api.js";
import { openModal, showModal } from "./modal.js";
import { state } from "./state.js";
import { escapeHtml } from "./utils.js";
import { buildResultsTable, renderFinalVerdicts } from "./testset_run.js";

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
  ["llm", "LLM 评审"],
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

// LLM 评审规则的上下文模式（与后端 reviewer profile 的 context 枚举一致）：
// reply 仅该步回复；record 为该步及之前全部对话记录；slice 与 record 同为
// 记录切片（对消息规则等效，最终断言按 scope 切片后也是记录文本）。
// 导出给 testset_list.js（评审 Profile 管理已迁到列表侧，表单共用本枚举）。
export const CONTEXT_MODES = [
  ["", "（用 Profile 默认）"],
  ["reply", "该步回复"],
  ["record", "该步及之前记录"],
  ["slice", "范围切片记录"],
];

// 导出 / 导入信封：format/version 为未来「测试集市场」（网络下载）预留兼容面。
// v2：消息断言改为 rules 列表 + is_command，携带身份配置（single → identity
// 快照；pool → 身份池），导入不创建身份 / 群聊记录（内联快照直接可用）。
const EXPORT_FORMAT = "astrbot-testbench-testset";
const EXPORT_VERSION = 2;

// 测试集运行 / 报告状态文案（报告视图最近运行条目与报告条目共用）
const RUN_STATUS_TEXT = {
  running: "运行中",
  done: "完成",
  error: "错误",
  cancelled: "已取消",
};

// 报告可重试的 LLM 评审统计：{failed, total}——只数带 profile_id 的 verdict
// （机械 verdict 不走 LLM，无重试意义）；failed 为其中 status error/invalid 的。
// 报告卡片据此决定「重试失败 / 重试全部」按钮的显隐与禁用
function retryableReportStats(data) {
  let failed = 0;
  let total = 0;
  const collect = (v) => {
    if (!v || !v.profile_id) return;
    total += 1;
    if (v.status === "error" || v.status === "invalid") failed += 1;
  };
  for (const step of data.steps || []) {
    for (const r of step.results || []) {
      for (const v of r.verdicts || []) collect(v);
    }
  }
  for (const fr of data.final_verdicts || []) {
    for (const entry of fr.results || []) collect(entry.verdict);
  }
  return { failed, total };
}

export function createTestsetEditor(env) {
  const { showRunStatus } = env;

  // 编辑窗口是否有未保存的修改（任一行输入/勾选变化即置位）
  let dirty = false;

  // 编辑窗口当前视图：「edit」编辑消息 /「report」报告页（最近运行 + 持久化报告）
  let viewMode = "edit";

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
    $("ts-report-enabled").checked = !!(ts && ts.report_enabled);
    const rowsEl = $("ts-messages");
    rowsEl.innerHTML = "";
    if (!ts) {
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent =
        "在左侧选择一个测试集，或点「＋ 新建测试集」创建；选中后在此编辑消息、断言与批量发送范围。";
      rowsEl.appendChild(empty);
      $("ts-final-rules").innerHTML = "";
      $("ts-segments").textContent = "";
    } else {
      initIdentityConfig(ts);
      renderFinalRules(ts);
      const batchSet = new Set();
      for (const [s, e] of ts.batch_ranges || []) {
        for (let i = s; i <= e; i++) batchSet.add(i);
      }
      const msgs = ts.messages || [];
      msgs.forEach((m, i) => rowsEl.appendChild(renderMsgRow(m, i, batchSet.has(i))));
      if (!msgs.length) addMsgRow(); // 空测试集给一行待编辑
      refreshRowSenders();
      updateSegments();
    }
    // 报告视图：按当前视图刷新编辑/报告体显隐；报告模式下拉取并渲染
    // （测试集运行完成等异步刷新触发的 renderTestsetEditor 也会同步更新报告页）
    syncViewModeUI();
    if (viewMode === "report") void renderReportView();
  }

  // 单行构建：第一行（序号 / 文本 / 命令标记 / 发送身份 / 自动@ / 批量勾选 /
  // 删除）+ 下方多断言列表。未来新增测试行为在此处加控件，并在
  // collectEditorRows() 同步收集。
  function renderMsgRow(msg, idx, batchChecked) {
    const row = document.createElement("div");
    row.className = "ts-msg-row";

    const line = document.createElement("div");
    line.className = "ts-msg-line";

    const idxEl = document.createElement("span");
    idxEl.className = "ts-msg-idx";
    idxEl.textContent = idx + 1;

    const inp = document.createElement("input");
    inp.type = "text";
    inp.className = "ts-msg-text";
    inp.placeholder = "消息文本";
    if (msg) inp.value = msg.text || "";

    // 命令标记：预期触发框架行为而非 LLM 回复（断言 / 报告语义随之区分）
    const cmd = document.createElement("label");
    cmd.className = "ts-msg-command";
    const cmdCb = document.createElement("input");
    cmdCb.type = "checkbox";
    cmdCb.checked = !!(msg && msg.is_command);
    cmdCb.title = "命令（预期触发框架行为而非 LLM 回复）";
    cmd.append(cmdCb, document.createTextNode("命令"));
    cmdCb.addEventListener("change", markDirty);

    // 发送身份（可选）：值由身份配置决定——pool 模式为池成员身份 id，
    // single 无测试集身份时为身份库 sender_id（旧行为）。身份被删除后选项
    // 保留原值（data-raw-name 提示），保存时不静默丢绑定。
    const senderSel = document.createElement("select");
    senderSel.className = "ts-msg-sender";
    senderSel.title = "发送身份（可选）";
    if (msg && msg.sender_id) {
      senderSel.dataset.rawSender = msg.sender_id;
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

    line.append(idxEl, inp, cmd, senderSel, autoAt, batch, del);

    // 多断言列表：默认 all（全部子规则须通过）
    const rulesBox = document.createElement("div");
    rulesBox.className = "ts-msg-rules";
    const initialRules =
      msg && Array.isArray(msg.rules) && msg.rules.length ? msg.rules : [null];
    for (const rule of initialRules) {
      rulesBox.appendChild(buildRuleRow(rule));
    }
    rulesBox.appendChild(buildAddRuleBtn(rulesBox));

    inp.addEventListener("input", markDirty);
    cb.addEventListener("change", () => {
      markDirty();
      updateSegments();
    });

    row.append(line, rulesBox);
    return row;
  }

  // 共享的断言类型下拉（消息规则与最终断言共用，避免两处维护）
  function buildRuleTypeSelect(rule) {
    const sel = document.createElement("select");
    sel.className = "ts-msg-rule-type";
    sel.innerHTML = RULE_TYPES.map(
      ([v, label]) => `<option value="${v}">${label}</option>`,
    ).join("");
    if (rule && rule.kind === "llm") sel.value = "llm";
    else if (rule && rule.type) sel.value = rule.type;
    return sel;
  }

  // 评审 Profile 下拉：选项来自 state.reviewers（profile 被删后当前值失效 →
  // 回默认空选项，保存校验会提示重新选择）
  function buildProfileSelect(rule) {
    const sel = document.createElement("select");
    sel.className = "ts-msg-rule-profile";
    sel.title = "评审 Profile（LLM 评审规则的配置实体：provider/模型/提示词/输出契约）";
    const emptyLabel = state.reviewers.length ? "选择评审 Profile…" : "未配置评审 Profile";
    sel.innerHTML =
      `<option value="">${emptyLabel}</option>` +
      state.reviewers
        .map(
          (p) => `<option value="${escapeHtml(p.id)}">${escapeHtml(p.name)}</option>`,
        )
        .join("");
    if (rule && rule.profile_id) sel.value = rule.profile_id;
    return sel;
  }

  // 上下文模式下拉（空 = 用 profile 声明的默认 context）
  function buildContextSelect(rule) {
    const sel = document.createElement("select");
    sel.className = "ts-msg-rule-context";
    sel.title = "评审上下文：仅该步回复 / 该步及之前全部对话记录";
    sel.innerHTML = CONTEXT_MODES.map(
      ([v, label]) => `<option value="${v}">${label}</option>`,
    ).join("");
    if (rule && rule.context) sel.value = rule.context;
    return sel;
  }

  // LLM 规则字段区（profile + context 下拉）；规则行与最终断言行共用
  function buildLlmBox(rule) {
    const box = document.createElement("span");
    box.className = "ts-msg-rule-llm";
    box.append(buildProfileSelect(rule), buildContextSelect(rule));
    return box;
  }

  // 单条断言编辑行：类型下拉 / 值输入（值类规则才显示）/ LLM 字段区 /
  // 删除。类型切换「LLM 评审」时值输入替换为 profile + context 下拉
  function buildRuleRow(rule) {
    const wrap = document.createElement("div");
    wrap.className = "ts-msg-rule";

    const sel = buildRuleTypeSelect(rule);

    const val = document.createElement("input");
    val.type = "text";
    val.className = "ts-msg-rule-value";
    val.placeholder = "断言值";
    if (rule && RULE_VALUE_TYPES.has(rule.type)) {
      val.value = Array.isArray(rule.value)
        ? rule.value.join(", ")
        : rule.value != null
          ? String(rule.value)
          : "";
    }

    const llmBox = buildLlmBox(rule);
    for (const inner of llmBox.querySelectorAll("select")) {
      inner.addEventListener("change", markDirty);
    }

    const del = document.createElement("button");
    del.className = "icon-btn ts-msg-rule-del";
    del.textContent = "✕";
    del.title = "删除该断言";
    del.addEventListener("click", () => {
      wrap.remove();
      markDirty();
    });

    const refreshValueVisible = () => {
      const isLlm = sel.value === "llm";
      val.hidden = !RULE_VALUE_TYPES.has(sel.value) || isLlm;
      llmBox.hidden = !isLlm;
    };
    sel.addEventListener("change", () => {
      refreshValueVisible();
      markDirty();
    });
    val.addEventListener("input", markDirty);
    refreshValueVisible();

    wrap.append(sel, val, llmBox, del);
    return wrap;
  }

  function buildAddRuleBtn(rulesBox) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "ts-msg-add-rule";
    btn.textContent = "＋ 断言";
    btn.title = "添加一条断言（多条按全部通过判定）";
    btn.addEventListener("click", () => {
      rulesBox.insertBefore(buildRuleRow(null), btn);
      markDirty();
    });
    return btn;
  }

  // 当前选中群聊（或测试集携带快照）的池成员身份列表（pool 模式行内发送身份
  // 下拉的唯一来源）
  function currentPoolMembers() {
    const cgId = $("ts-pool-ref").value;
    if (cgId) {
      const cg = state.chatGroups.find((g) => g.id === cgId);
      if (cg) {
        return (cg.member_ids || [])
          .map((mid) => state.identities.find((i) => i.id === mid))
          .filter(Boolean);
      }
    }
    // 未选群聊 / 群聊已删除：回退测试集携带的池快照（导入自包含）
    const ts = currentSelected();
    const pool = ts && ts.pool_snapshot;
    if (pool && Array.isArray(pool.members)) return pool.members;
    return [];
  }

  // single 模式且已配置测试集身份 → 消息级发送身份无意义（恒用测试集身份）
  function identityConfigured() {
    if ($("ts-identity-mode").value !== "single") return false;
    return !!$("ts-identity-ref").value;
  }

  // 重建全部消息行的发送身份下拉（选项随身份模式 / 测试集身份变化），
  // 尽可能保留当前选中值
  function refreshRowSenders() {
    const hideAll = identityConfigured();
    for (const row of $("ts-messages").querySelectorAll(".ts-msg-row")) {
      const sel = row.querySelector(".ts-msg-sender");
      if (!sel) continue;
      sel.hidden = hideAll;
      if (hideAll) continue;
      const current = sel.value || sel.dataset.rawSender || "";
      const poolMode = $("ts-identity-mode").value === "pool";
      if (poolMode) {
        // 池成员：值 = 身份 id；成员被删后当前值失效 → 回默认身份
        const members = currentPoolMembers();
        sel.innerHTML =
          `<option value="">默认身份</option>` +
          members
            .map(
              (m) =>
                `<option value="${escapeHtml(m.id)}">${escapeHtml(m.name)}</option>`,
            )
            .join("");
        if (current && members.some((m) => m.id === current)) sel.value = current;
      } else {
        // 身份库：值 = sender_id（旧行为），身份被删保留原值（data-raw-name 提示）
        const rawSender = current;
        const matchedIdentity = state.identities.find(
          (i) => i.sender_id === rawSender,
        );
        sel.innerHTML =
          `<option value="">默认身份</option>` +
          state.identities
            .map(
              (i) =>
                `<option value="${escapeHtml(i.sender_id)}">${escapeHtml(i.name)}</option>`,
            )
            .join("");
        if (rawSender && matchedIdentity) {
          sel.value = rawSender;
        } else if (rawSender) {
          const opt = document.createElement("option");
          opt.value = rawSender;
          opt.textContent = `${sel.dataset.rawName || rawSender}（身份已删除）`;
          sel.appendChild(opt);
          sel.value = rawSender;
          sel.dataset.rawName = sel.dataset.rawName || rawSender;
        }
      }
    }
  }

  // 行内身份下拉 → sender 字段：未选返回空对象。pool 模式持久化身份 id 引用
  // （池快照自包含，成员删除仍可解析）；single 无测试集身份时取 sender_id /
  // sender_name（旧行为），身份已删除回退原 sender_name（data-raw-name）
  function collectSender(sel) {
    if (!sel || !sel.value) return {};
    if ($("ts-identity-mode").value === "pool") {
      return { sender_id: sel.value };
    }
    const ident = state.identities.find((i) => i.sender_id === sel.value);
    if (ident) return { sender_id: ident.sender_id, sender_name: ident.sender_name };
    return { sender_id: sel.value, sender_name: sel.dataset.rawName || sel.value };
  }

  // ---------- 测试集身份配置（single 单一身份 / pool 身份池） ----------

  // 按测试集数据初始化身份配置区：模式 / 单身份 / 群聊下拉。
  // 身份 / 群聊被删除后保留引用并占位展示快照名（防静默丢绑定）。
  function initIdentityConfig(ts) {
    const mode = ts && ts.identity_mode === "pool" ? "pool" : "single";
    $("ts-identity-mode").value = mode;
    const idRef = ts && ts.identity_id ? ts.identity_id : "";
    const snapshot = ts && ts.identity_snapshot;
    $("ts-identity-ref").innerHTML =
      `<option value="">默认身份（测试台）</option>` +
      state.identities
        .map(
          (i) =>
            `<option value="${escapeHtml(i.id)}">${escapeHtml(i.name)}</option>`,
        )
        .join("");
    if (idRef) {
      if (state.identities.some((i) => i.id === idRef)) {
        $("ts-identity-ref").value = idRef;
      } else if (snapshot && snapshot.id === idRef) {
        const opt = document.createElement("option");
        opt.value = idRef;
        opt.textContent = `${snapshot.name || idRef}（身份已删除）`;
        $("ts-identity-ref").appendChild(opt);
        $("ts-identity-ref").value = idRef;
      }
    }
    const cgRef = ts && ts.chat_group_id ? ts.chat_group_id : "";
    $("ts-pool-ref").innerHTML =
      `<option value="">默认身份（测试台）</option>` +
      state.chatGroups
        .map(
          (g) =>
            `<option value="${escapeHtml(g.id)}">${escapeHtml(g.name)}</option>`,
        )
        .join("");
    if (cgRef) {
      if (state.chatGroups.some((g) => g.id === cgRef)) {
        $("ts-pool-ref").value = cgRef;
      } else {
        const opt = document.createElement("option");
        opt.value = cgRef;
        const pool = ts && ts.pool_snapshot;
        opt.textContent = `${(pool && pool.name) || cgRef}（群聊已删除）`;
        $("ts-pool-ref").appendChild(opt);
        $("ts-pool-ref").value = cgRef;
      }
    }
    refreshIdentityFields();
  }

  function refreshIdentityFields() {
    const poolMode = $("ts-identity-mode").value === "pool";
    $("ts-pool-ref").hidden = !poolMode;
    $("ts-identity-ref").hidden = poolMode;
    refreshRowSenders();
  }

  // 收集身份配置 → 保存 payload 的身份字段（快照由编辑器就地解析，导入路径
  // 后端优先采用 payload 携带的快照）
  function collectIdentityConfig() {
    if ($("ts-identity-mode").value === "pool") {
      const cgId = $("ts-pool-ref").value;
      return {
        identity_mode: "pool",
        identity_id: null,
        chat_group_id: cgId || null,
        pool_snapshot: buildPoolSnapshot(cgId),
      };
    }
    const identId = $("ts-identity-ref").value;
    return {
      identity_mode: "single",
      identity_id: identId || null,
      chat_group_id: null,
      identity_snapshot: buildIdentitySnapshot(identId),
    };
  }

  function buildIdentitySnapshot(identId) {
    if (!identId) return null;
    const ident = state.identities.find((i) => i.id === identId);
    if (!ident) return null;
    return {
      id: ident.id,
      name: ident.name,
      sender_id: ident.sender_id,
      sender_name: ident.sender_name,
      is_admin: !!ident.is_admin,
    };
  }

  function buildPoolSnapshot(cgId) {
    if (!cgId) return null;
    const cg = state.chatGroups.find((g) => g.id === cgId);
    if (!cg) {
      // 群聊已删除：保留测试集携带的池快照（自包含）
      const ts = currentSelected();
      const saved = ts && ts.pool_snapshot;
      return saved && typeof saved === "object" ? saved : null;
    }
    return {
      name: cg.name || "",
      members: (cg.member_ids || [])
        .map((mid) => state.identities.find((i) => i.id === mid))
        .filter(Boolean)
        .map((ident) => ({
          id: ident.id,
          name: ident.name,
          sender_id: ident.sender_id,
          sender_name: ident.sender_name,
          is_admin: !!ident.is_admin,
        })),
    };
  }

  // 重建全部 profile 下拉选项（保持当前选中值；选中 profile 已被删除 → 回落空）。
  // 评审 Profile 的增删改管理在 testset_list.js，变更后经本函数刷新消息规则行
  // 与最终断言行内已渲染的下拉（testset_list.js 的 refreshReviewers 调用）
  function refreshAllProfileSelects() {
    const selects = document.querySelectorAll(".ts-msg-rule-profile");
    const emptyLabel = state.reviewers.length ? "选择评审 Profile…" : "未配置评审 Profile";
    for (const sel of selects) {
      const current = sel.value;
      sel.innerHTML =
        `<option value="">${emptyLabel}</option>` +
        state.reviewers
          .map(
            (p) => `<option value="${escapeHtml(p.id)}">${escapeHtml(p.name)}</option>`,
          )
          .join("");
      if (current && state.reviewers.some((p) => p.id === current)) sel.value = current;
    }
  }

  // ---------- 最终断言（跨轮）编辑 ----------

  // 最终断言范围文案（展示 / 还原编辑用）："all" → 全部步骤；{from,to} → "s–e"（1 基）
  function scopeToText(scope) {
    if (scope && typeof scope === "object" && scope.from != null && scope.to != null) {
      const s = scope.from + 1;
      const e = scope.to + 1;
      return s === e ? String(s) : `${s}-${e}`;
    }
    return "all";
  }

  // 渲染最终断言列表（跨轮评审规则，可选 scope 限定步骤范围）
  function renderFinalRules(ts) {
    const el = $("ts-final-rules");
    el.innerHTML = "";
    const rules = ts && Array.isArray(ts.final_rules) ? ts.final_rules : [];
    for (const fr of rules) el.appendChild(buildFinalRuleRow(fr));
  }

  // 单条最终断言行：类型 / 值（或 LLM 字段）/ 范围 / 删除
  function buildFinalRuleRow(fr) {
    const wrap = document.createElement("div");
    wrap.className = "ts-final-rule";
    const rule = fr && fr.rule ? fr.rule : null;
    const sel = buildRuleTypeSelect(rule);
    const val = document.createElement("input");
    val.type = "text";
    val.className = "ts-msg-rule-value";
    val.placeholder = "断言值";
    if (rule && RULE_VALUE_TYPES.has(rule.type)) {
      val.value = Array.isArray(rule.value)
        ? rule.value.join(", ")
        : rule.value != null
          ? String(rule.value)
          : "";
    }
    const llmBox = buildLlmBox(rule);
    for (const inner of llmBox.querySelectorAll("select")) {
      inner.addEventListener("change", markDirty);
    }
    const scope = document.createElement("input");
    scope.type = "text";
    scope.className = "ts-final-rule-scope";
    scope.placeholder = "范围（空=全部）";
    scope.title = "跨轮评估的步骤范围：留空 / all = 全部步骤，2-4 = 第 2 到第 4 步，3 = 仅第 3 步";
    scope.value = fr && fr.scope != null ? scopeToText(fr.scope) : "all";
    const del = document.createElement("button");
    del.type = "button";
    del.className = "icon-btn ts-msg-rule-del";
    del.textContent = "✕";
    del.title = "删除该最终断言";
    del.addEventListener("click", () => {
      wrap.remove();
      markDirty();
    });
    const refreshValueVisible = () => {
      const isLlm = sel.value === "llm";
      val.hidden = !RULE_VALUE_TYPES.has(sel.value) || isLlm;
      llmBox.hidden = !isLlm;
    };
    sel.addEventListener("change", () => {
      refreshValueVisible();
      markDirty();
    });
    val.addEventListener("input", markDirty);
    scope.addEventListener("input", markDirty);
    refreshValueVisible();
    wrap.append(sel, val, llmBox, scope, del);
    return wrap;
  }

  // 收集最终断言列表：空类型 / 无效规则行丢弃；scope 非法 → "all"（保存前校验已拦截）
  function collectFinalRules() {
    const rules = [];
    for (const wrap of $("ts-final-rules").querySelectorAll(".ts-final-rule")) {
      const type = wrap.querySelector(".ts-msg-rule-type").value;
      const rule = buildRule(
        type,
        wrap.querySelector(".ts-msg-rule-value").value,
        wrap.querySelector(".ts-msg-rule-llm"),
      );
      if (!rule) continue;
      const scope = parseScope(wrap.querySelector(".ts-final-rule-scope").value);
      rules.push({ rule, scope: scope === null ? "all" : scope });
    }
    return rules;
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
  // 每条消息收集断言规则列表（空规则 → []）、命令标记、可选身份（见
  // collectSender）与自动@
  function collectEditorRows() {
    const messages = [];
    const batchFlags = [];
    for (const row of $("ts-messages").querySelectorAll(".ts-msg-row")) {
      const text = row.querySelector(".ts-msg-text").value.trim();
      if (!text) continue;
      const rules = collectRules(row.querySelector(".ts-msg-rules"));
      const sender = collectSender(row.querySelector(".ts-msg-sender"));
      const cmdCb = row.querySelector(".ts-msg-command input");
      const atCb = row.querySelector(".ts-msg-auto-at input");
      const message = { text, rules };
      if (cmdCb.checked) message.is_command = true;
      if (sender.sender_id !== undefined) Object.assign(message, sender);
      message.auto_at = atCb.checked;
      messages.push(message);
      batchFlags.push(row.querySelector(".ts-msg-batch input").checked);
    }
    return { messages, batchRanges: rangesFromFlags(batchFlags) };
  }

  // 行内多断言收集：空类型 / 值类规则空值 / 未选 profile 的 LLM 规则经
  // buildRule 归为 null 丢弃
  function collectRules(rulesBox) {
    const rules = [];
    for (const wrap of rulesBox.querySelectorAll(".ts-msg-rule")) {
      const rule = buildRule(
        wrap.querySelector(".ts-msg-rule-type").value,
        wrap.querySelector(".ts-msg-rule-value").value,
        wrap.querySelector(".ts-msg-rule-llm"),
      );
      if (rule) rules.push(rule);
    }
    return rules;
  }

  // 行内 rule 构造：空类型 → null；需要值的类型值非空才保留；
  // LLM 规则读 profile/context 下拉 → {kind: "llm", profile_id, context?}
  function buildRule(type, value, llmBox) {
    if (!type) return null;
    if (type === "llm") {
      const profileId = llmBox.querySelector(".ts-msg-rule-profile").value;
      if (!profileId) return null;
      const rule = { kind: "llm", profile_id: profileId };
      const ctx = llmBox.querySelector(".ts-msg-rule-context").value;
      if (ctx) rule.context = ctx;
      return rule;
    }
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

  // 校验单条规则行（消息断言与最终断言共用）；返回错误文案或 null。
  // 值类规则空值、min_len/max_len 非整数、LLM 规则未选 profile 会被 buildRule
  // 静默丢弃，这里先带定位提示，而不是无声吞掉。
  function validateRuleRow(wrap, label) {
    const type = wrap.querySelector(".ts-msg-rule-type").value;
    const value = wrap.querySelector(".ts-msg-rule-value").value.trim();
    if (type === "llm") {
      const profileId = wrap.querySelector(".ts-msg-rule-profile").value;
      if (!profileId) return `${label}：LLM 评审规则未选择评审 Profile，该规则不会生效`;
      return null;
    }
    if (!RULE_VALUE_TYPES.has(type)) return null;
    if (!value) {
      return `${label}：规则「${ruleTypeLabel(type)}」未填写断言值，该规则不会生效`;
    }
    if (type === "min_len" || type === "max_len") {
      const n = Number(value);
      if (!Number.isInteger(n)) {
        return `${label}：规则「${ruleTypeLabel(type)}」的断言值必须是整数`;
      }
    }
    return null;
  }

  // 最终断言 scope 输入解析：空 / "all" → "all"；"2-4" → {from:1, to:3}；
  // "3" → {from:2, to:2}；其余 → null（非法，保存前校验报错）
  function parseScope(text) {
    const t = text.trim();
    if (!t || t === "all") return "all";
    const m = /^(\d+)(?:-(\d+))?$/.exec(t);
    if (!m) return null;
    const start = Number(m[1]);
    const end = m[2] ? Number(m[2]) : start;
    if (start < 1 || end < start) return null;
    return { from: start - 1, to: end - 1 };
  }

  // 保存 / 导出前的断言值校验：值类规则空值、min_len/max_len 非整数、
  // LLM 规则未选 profile、最终断言 scope 非法都会在保存前被拦截。
  // 返回错误文案；全部合法返回 null。
  function validateEditorRows() {
    const rows = $("ts-messages").querySelectorAll(".ts-msg-row");
    let kept = 0; // 与 collectEditorRows 一致：空文本行不计入
    for (const row of rows) {
      const text = row.querySelector(".ts-msg-text").value.trim();
      if (!text) continue;
      for (const wrap of row.querySelectorAll(".ts-msg-rule")) {
        const err = validateRuleRow(wrap, `第 ${kept + 1} 条消息`);
        if (err) return err;
      }
      kept += 1;
    }
    const finalWraps = $("ts-final-rules").querySelectorAll(".ts-final-rule");
    for (const [i, wrap] of [...finalWraps].entries()) {
      const type = wrap.querySelector(".ts-msg-rule-type").value;
      if (!type) continue; // 空类型行不生效也不拦截
      const err = validateRuleRow(wrap, `最终断言 ${i + 1}`);
      if (err) return err;
      if (parseScope(wrap.querySelector(".ts-final-rule-scope").value) === null) {
        return `最终断言 ${i + 1}：范围格式无效（如 2-4 表示第 2 到第 4 步，或留空 = 全部步骤）`;
      }
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
    const identity = collectIdentityConfig();
    const finalRules = collectFinalRules();
    try {
      await updateTestset({
        id: ts.id,
        name,
        messages,
        batch_ranges: batchRanges,
        final_rules: finalRules,
        report_enabled: $("ts-report-enabled").checked,
        ...identity,
      });
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
        final_rules: collectFinalRules(),
      };
      // 按身份模式携带：single → identity 快照；pool → 身份池（导入不建库）
      const identity = collectIdentityConfig();
      if (identity.identity_mode === "pool") {
        if (identity.pool_snapshot) envelope.pool = identity.pool_snapshot;
      } else if (identity.identity_snapshot) {
        envelope.identity = identity.identity_snapshot;
      }
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

  // 信封解析（校验 format/version，预留「测试集市场」下载路径：传入 JSON 文本
  // 即可）。v1 兼容：消息单条 rule → rules 列表；v2 增加 is_command / rules 与
  // 身份配置（identity 快照 / pool 身份池），导入不创建身份 / 群聊记录。
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
      // v2 直接 rules；v1 单条 rule 归并为单元素列表
      const rules = [];
      if (m && Array.isArray(m.rules)) {
        for (const r of m.rules) {
          if (r && typeof r === "object") rules.push({ ...r });
        }
      } else if (m && m.rule != null) {
        rules.push({ ...m.rule });
      }
      const message = { text, rules };
      if (m && m.is_command === true) message.is_command = true;
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
    // 身份配置：v2 按 identity / pool 字段；缺省（含 v1）→ single 默认身份
    const result = {
      name,
      messages,
      batch_ranges: batchRanges,
      final_rules: [],
      identity_mode: data.pool ? "pool" : "single",
      identity_id: null,
      chat_group_id: null,
    };
    // 最终断言（跨轮）：v2 携带 final_rules；逐项浅拷贝规则与 scope
    if (Array.isArray(data.final_rules)) {
      for (const fr of data.final_rules) {
        if (!fr || typeof fr !== "object" || !fr.rule || typeof fr.rule !== "object") {
          continue;
        }
        const item = { rule: { ...fr.rule } };
        if (fr.scope !== undefined) item.scope = fr.scope;
        result.final_rules.push(item);
      }
    }
    if (result.identity_mode === "pool") {
      if (data.pool && typeof data.pool === "object") {
        result.pool_snapshot = data.pool;
      }
    } else if (data.identity && typeof data.identity === "object") {
      result.identity_snapshot = data.identity;
      if (data.identity.id) result.identity_id = data.identity.id;
    }
    return result;
  }

  // ---------- 报告视图（页眉「编辑 / 报告」切换） ----------

  // 页眉「报告」按钮：在编辑与报告视图之间切换；切到报告时拉取渲染
  function toggleViewMode() {
    viewMode = viewMode === "edit" ? "report" : "edit";
    syncViewModeUI();
    if (viewMode === "report") void renderReportView();
  }

  // 按当前视图刷新页眉按钮文案与编辑 / 报告体的显隐
  function syncViewModeUI() {
    $("btn-ts-mode").textContent = viewMode === "report" ? "编辑" : "报告";
    $("ts-edit-body").hidden = viewMode !== "edit";
    $("ts-report-body").hidden = viewMode !== "report";
  }

  // 运行 / 报告状态文案（最近运行条目与报告条目共用）
  function runStatusText(status) {
    return RUN_STATUS_TEXT[status] || status;
  }

  // 报告视图：顶部该测试集最近的运行（可找回进度），下方持久化报告列表。
  // 无选中测试集 → 空态引导
  async function renderReportView() {
    const ts = currentSelected();
    const body = $("ts-report-body");
    body.innerHTML = "";
    if (!ts) {
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent = "在左侧选择一个测试集，查看其运行记录与产出的报告。";
      body.appendChild(empty);
      return;
    }
    const runsEl = document.createElement("div");
    runsEl.className = "ts-report-section";
    runsEl.appendChild(sectionTitle("最近运行"));
    const runsHolder = document.createElement("div");
    runsEl.appendChild(runsHolder);
    const reportsEl = document.createElement("div");
    reportsEl.className = "ts-report-section";
    reportsEl.appendChild(sectionTitle("报告"));
    const reportsHolder = document.createElement("div");
    reportsEl.appendChild(reportsHolder);
    body.append(runsEl, reportsEl);

    let runs = [];
    let reports = [];
    let runsFailed = false;
    let reportsFailed = false;
    try {
      const data = await listTestsetRuns(ts.id);
      runs = Array.isArray(data.runs) ? data.runs : [];
    } catch (err) {
      runsFailed = true;
    }
    try {
      const data = await listReports(ts.id);
      reports = Array.isArray(data.reports) ? data.reports : [];
    } catch (err) {
      reportsFailed = true;
    }
    if (runsFailed) runsHolder.appendChild(hintEl("加载最近运行失败"));
    else if (!runs.length) {
      runsHolder.appendChild(
        hintEl("暂无运行记录，运行测试集后在此找回进度（运行由后台任务驱动，离开页面不中断）"),
      );
    } else {
      buildRunsSection(runsHolder, runs);
    }
    if (reportsFailed) reportsHolder.appendChild(hintEl("加载报告失败"));
    else if (!reports.length) {
      reportsHolder.appendChild(
        hintEl("该测试集未产出报告。编辑视图勾选「运行结束后产出持久化报告」后运行即可产出"),
      );
    } else {
      buildReportsSection(reportsHolder, reports);
    }
  }

  function sectionTitle(text) {
    const div = document.createElement("div");
    div.className = "ts-report-section-title";
    div.textContent = text;
    return div;
  }

  function hintEl(text) {
    const div = document.createElement("div");
    div.className = "hint";
    div.textContent = text;
    return div;
  }

  // 最近运行条目：状态 chip + 名称 + 时间 + 步骤进度 + 查看（运行中找回进度，
  // 经 getDeps().viewTestsetRun 走测试集运行控制器的重建 / 结果弹窗路径）
  function buildRunsSection(holder, runs) {
    for (const r of runs) {
      const item = document.createElement("div");
      item.className = "report-item";
      const chip = document.createElement("span");
      chip.className = `chip ${escapeHtml(r.status)}`;
      chip.textContent = runStatusText(r.status);
      const name = document.createElement("span");
      name.className = "report-item-name";
      name.textContent = `${r.testset_name || r.testset_id} · ${getDeps().formatTime(r.started_at)}`;
      const progress = document.createElement("span");
      progress.className = "ts-report-meta";
      progress.textContent =
        r.status === "running"
          ? `进行中 ${r.done_steps}/${r.total_steps} 步`
          : `共 ${r.total_steps} 步`;
      const view = document.createElement("button");
      view.className = "btn small";
      view.textContent = "查看";
      view.addEventListener("click", () => getDeps().viewTestsetRun(r.run_id));
      item.append(chip, name, progress, view);
      holder.appendChild(item);
    }
  }

  // 报告列表：逐条 buildReportItem（名称 + 状态 + 指标聚合摘要 + 操作）
  function buildReportsSection(holder, reports) {
    for (const report of reports) holder.appendChild(buildReportItem(report));
  }

  // 单条报告卡片：运行状态 chip + 生成时间 + 指标聚合总览 + 查看 / 导出 /
  // 重试（失败 / 全部，仅报告含 LLM 评审时出现）/ 删除
  function buildReportItem(report) {
    const data = report.data || {};
    const item = document.createElement("div");
    item.className = "report-item report-item-col";
    const head = document.createElement("div");
    head.className = "report-item-head";
    const chip = document.createElement("span");
    chip.className = `chip ${escapeHtml(data.status)}`;
    chip.textContent = runStatusText(data.status);
    const name = document.createElement("span");
    name.className = "report-item-name";
    name.textContent = `${data.testset_name || "测试集"} · ${getDeps().formatTime(report.created_at)}`;
    head.append(chip, name);
    item.appendChild(head);
    item.appendChild(buildReportOverview(data));
    const actions = document.createElement("div");
    actions.className = "report-item-actions";
    const view = document.createElement("button");
    view.className = "btn small";
    view.textContent = "查看";
    view.addEventListener("click", () => openReportModal(report));
    const exportBtn = document.createElement("button");
    exportBtn.className = "btn small";
    exportBtn.textContent = "导出";
    exportBtn.addEventListener("click", () => exportReport(report));
    const stats = retryableReportStats(data);
    if (stats.total) {
      const retryFail = document.createElement("button");
      retryFail.className = "btn small";
      retryFail.textContent = `重试失败（${stats.failed}）`;
      retryFail.disabled = stats.failed === 0;
      retryFail.title = "重跑状态为失败（error/invalid）的 LLM 评审";
      retryFail.addEventListener("click", () => confirmRetryReviews(report, "failed"));
      const retryAll = document.createElement("button");
      retryAll.className = "btn small";
      retryAll.textContent = "重试全部";
      retryAll.title = "重跑报告的全部 LLM 评审";
      retryAll.addEventListener("click", () => confirmRetryReviews(report, "all"));
      actions.append(view, exportBtn, retryFail, retryAll);
    } else {
      actions.append(view, exportBtn);
    }
    const del = document.createElement("button");
    del.className = "btn small danger";
    del.textContent = "删除";
    del.addEventListener("click", () => deleteReport(report.id));
    actions.appendChild(del);
    item.appendChild(actions);
    return item;
  }

  // 批量重试确认（scope=failed|all）：重试后重渲染报告视图（列表总览与卡片按钮
  // 按新数据刷新）并提示结果
  function confirmRetryReviews(report, scope) {
    const label = scope === "failed" ? "失败评审" : "全部评审";
    showModal(`确定重试该报告的全部${label}吗？将按评审时保存的输入重新调用评审 LLM，报告数据会更新。`, {
      onOk: async () => {
        const resp = await retryReviews(report.id, { scope });
        void renderReportView();
        const msg = `重试完成：更新 ${resp.updated} 条`;
        showRunStatus(
          resp.failed ? "warn" : "ok",
          resp.failed ? `${msg}，${resp.failed} 条仍失败` : msg,
        );
      },
    });
  }

  // 报告总览：metrics_summary 逐指标聚合行（number 平均/极值、enum 分类计数、
  // bool 通过率）+ 评审失败数；无聚合指标 → 提示
  function buildReportOverview(data) {
    const sum = data.metrics_summary || {};
    const metrics = sum.metrics || {};
    const wrap = document.createElement("div");
    wrap.className = "ts-report-meta";
    const lines = [];
    for (const key of Object.keys(metrics)) {
      const m = metrics[key];
      if (m.type === "number") {
        lines.push(`${key}: 平均 ${m.avg}（最小 ${m.min} / 最大 ${m.max}，${m.count} 条）`);
      } else if (m.type === "enum") {
        const counts = Object.entries(m.counts || {})
          .map(([k, v]) => `${k}=${v}`)
          .join("，");
        lines.push(`${key}: ${counts}（共 ${m.total} 条）`);
      } else if (m.type === "bool") {
        lines.push(`${key}: 通过 ${m.pass}/${m.total}（${(m.rate * 100).toFixed(1)}%）`);
      }
    }
    if (sum.review_failures) lines.push(`评审失败 ${sum.review_failures} 条`);
    if (!lines.length) {
      const hint = document.createElement("span");
      hint.className = "hint";
      hint.textContent = "无指标聚合（未配置 LLM 评审指标或全部为文本指标）";
      wrap.appendChild(hint);
      return wrap;
    }
    for (const line of lines) {
      const div = document.createElement("div");
      div.className = "metrics-summary";
      div.textContent = line;
      wrap.appendChild(div);
    }
    return wrap;
  }

  // 报告详情弹窗：总览 + 结果表格 + 最终断言表（与运行结果弹窗共用
  // buildResultsTable / renderFinalVerdicts，数据即持久化报告快照）。
  // retryCtx 传给表格：verdict 详情弹窗可单条重试评审，成功后回调刷新报告视图
  function openReportModal(report) {
    const data = report.data || {};
    const retryCtx = {
      reportId: report.id,
      showStatus: showRunStatus,
      onRetried: () => void renderReportView(),
    };
    const content = document.createElement("div");
    content.className = "form-col";
    content.style.gap = "8px";
    content.appendChild(buildReportOverview(data));
    content.appendChild(buildResultsTable(data, retryCtx));
    const finalTable = renderFinalVerdicts(data, retryCtx);
    if (finalTable) content.appendChild(finalTable);
    openModal({
      title: `报告 · ${data.testset_name || ""}`,
      content,
      okText: "返回",
      wide: true,
    });
  }

  // 导出报告完整 JSON（与测试集导出同走 Blob 下载）
  function exportReport(report) {
    const blob = new Blob([JSON.stringify(report, null, 2)], {
      type: "application/json",
    });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `报告-${(report.data && report.data.testset_name) || report.testset_id || report.id}.json`;
    a.click();
    URL.revokeObjectURL(a.href);
    showRunStatus("ok", "报告已导出");
  }

  function deleteReport(id) {
    showModal("确定删除该报告吗？", {
      danger: true,
      onOk: async () => {
        await deleteReports([id]);
        void renderReportView();
        showRunStatus("ok", "报告已删除");
      },
    });
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
  // 添加最终断言（跨轮）：无需先有消息（最终断言作用在全部/部分步骤上）
  $("btn-ts-add-final").addEventListener("click", () => {
    if (!currentSelected()) {
      showModal("请先在左侧选择或创建一个测试集，再添加最终断言");
      return;
    }
    const el = $("ts-final-rules");
    el.appendChild(buildFinalRuleRow(null));
    markDirty();
  });
  $("ts-name").addEventListener("input", markDirty);
  // 「编辑 / 报告」视图切换
  $("btn-ts-mode").addEventListener("click", () => toggleViewMode());
  // 报告产出开关（测试集配置）：变更即脏
  $("ts-report-enabled").addEventListener("change", markDirty);
  // 身份配置变更：切换模式 / 单身份 / 身份池 → 刷新下拉显隐与行内发送身份
  $("ts-identity-mode").addEventListener("change", () => {
    refreshIdentityFields();
    markDirty();
  });
  $("ts-identity-ref").addEventListener("change", () => {
    refreshRowSenders();
    markDirty();
  });
  $("ts-pool-ref").addEventListener("change", () => {
    refreshRowSenders();
    markDirty();
  });
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
        final_rules: parsed.final_rules,
        identity_mode: parsed.identity_mode,
        identity_id: parsed.identity_id,
        chat_group_id: parsed.chat_group_id,
        identity_snapshot: parsed.identity_snapshot,
        pool_snapshot: parsed.pool_snapshot,
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
    // 评审 Profile 增删改后重建消息规则 / 最终断言行内 profile 下拉
    // （由 testset_list.js 的 refreshReviewers 调用）
    refreshAllProfileSelects,
  };
}
