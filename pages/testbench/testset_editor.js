// testset_editor.js — 右侧测试集编辑窗口：消息行 / 断言规则 / 批量段 + 保存 / 导出 / 导入
// + 报告视图（页眉「编辑 / 报告」切换：该测试集的最近运行与持久化报告）。
// 报告视图已拆到 testset_reports.js（createReportView 工厂，Phase 4），本模块
// 只保留「编辑 / 报告」切换按钮绑定与选中测试集时按当前视图刷新报告页。
// 由 testset_list.js 创建（createTestsetEditor(env)），env 注入视图动作
// （showRunStatus）。列表侧函数（formatTime / openTestsetRun / deleteTestset /
// doSelect / refreshTestsets / viewTestsetRun）经 setDeps 注入——编辑器与列表
// 互相引用，直接 import 会成环，用 setter 延迟取，依赖保持单向（本模块不
// import testset_list.js）。结果表格渲染由 testset_reports.js 复用 testset_run.js
// 的模块级导出 buildResultsTable / renderFinalVerdicts（testset_run.js 不
// import 本模块）。
// 评审 Profile 的增删改管理已迁到左侧测试集列表（testset_list.js 的「评审
// Profile」tab），本模块只负责消息规则 / 最终断言行内 profile 下拉的构建与
// 重建（refreshAllProfileSelects，供列表侧 profile 变更后刷新）。
import { createTestset, updateTestset } from "./api.js";
import { showModal } from "./modal.js";
import { state } from "./state.js";
import { escapeHtml } from "./utils.js";
import { createReportView } from "./testset_reports.js";
import {
  EXPORT_FORMAT,
  EXPORT_VERSION,
  RULE_VALUE_TYPES,
  buildRule,
  collectEditorRows as collectEditorRowsData,
  collectRules,
  parseScope,
  parseSliceRange,
  parseTestsetEnvelope,
  rangesFromFlags,
  sliceRangeToText,
} from "./pure.js";

const $ = (id) => document.getElementById(id);

// 断言类型下拉选项（value 与后端 assertions.py 的规则 type 对应）——行渲染
// 的唯一来源，未来新增测试行为只改这里 + renderMsgRow/collectEditorRows
// + pure.js 的 RULE_VALUE_TYPES + 后端 _normalize_messages。
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

// LLM 评审规则的上下文模式（与后端 reviewer profile 的 context 枚举一致）：
// reply 仅该步回复；record 为该步及之前全部对话记录；slice 为记录切片——
// 消息规则可选配 slice_range（行内切片范围输入，2-4/3/空，多段逗号分隔如
// 3-4,10-12）限定记录区间，未配范围时与 record 等效；最终断言按 scope 切片
// 后也是记录文本（范围由行内 scope 输入承担，不再重复配 slice_range）。
// 导出给 testset_list.js（评审 Profile 管理已迁到列表侧，表单共用本枚举）。
export const CONTEXT_MODES = [  ["", "（用 Profile 默认）"],
  ["reply", "该步回复"],
  ["record", "该步及之前记录"],
  ["slice", "范围切片记录"],
];

// 导出 / 导入信封：format/version（EXPORT_FORMAT / EXPORT_VERSION，定义在
// pure.js）为未来「测试集市场」（网络下载）预留兼容面。v2：消息断言改为 rules
// 列表 + is_command，携带身份配置（single → identity 快照；pool → 身份池），
// 导入不创建身份 / 群聊记录（内联快照直接可用）。

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

  // 报告视图（最近运行 + 持久化报告）：createReportView 工厂持有 viewMode 与
  // 报告页渲染。getDeps 须包一层闭包——setDeps 会重赋值本闭包的 getDeps 绑定，
  // 工厂拿到的须是调用时的最新值
  const reportView = createReportView({
    currentSelected,
    getDeps: () => getDeps(),
    showRunStatus,
  });

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
    initReportLlmConfig(ts);
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
    reportView.syncViewModeUI();
    if (reportView.isReportMode()) void reportView.renderReportView();
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

    // 消息可能为长文本：textarea 支持纵向拉伸（resize: vertical，见 style.css）
    const inp = document.createElement("textarea");
    inp.rows = 1;
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

    // 多断言列表：默认 all（全部子规则须通过）；新消息默认无断言，
    // 按需点「＋ 断言」添加
    const rulesBox = document.createElement("div");
    rulesBox.className = "ts-msg-rules";
    const initialRules =
      msg && Array.isArray(msg.rules) && msg.rules.length ? msg.rules : [];
    for (const rule of initialRules) {
      rulesBox.appendChild(buildRuleRow(rule));
    }
    rulesBox.appendChild(buildRuleAddBar(rulesBox));

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
    sel.title = "评审上下文：仅该步回复 / 该步及之前全部对话记录 / 范围切片记录";
    sel.innerHTML = CONTEXT_MODES.map(
      ([v, label]) => `<option value="${v}">${label}</option>`,
    ).join("");
    if (rule && rule.context) sel.value = rule.context;
    return sel;
  }

  // 切片范围输入（context = slice 时显示）：支持多段逗号分隔（2-4 / 3 /
  // 3-4,10-12），相对当前步记录钳制（空 = 全部）
  function buildSliceInput(rule) {
    const inp = document.createElement("input");
    inp.type = "text";
    inp.className = "ts-msg-rule-slice";
    inp.placeholder = "切片范围";
    inp.title =
      "切片范围（仅「范围切片记录」上下文）：留空 = 当前步及之前全部，2-4 = 第 2 到第 4 步，3 = 仅第 3 步，多段用逗号分隔如 3-4,10-12";
    if (rule && rule.slice_range) inp.value = sliceRangeToText(rule.slice_range);
    return inp;
  }

  // 「注入被测 Agent 系统提示词」开关（仅 LLM 评审断言，规则级）：缺省开启，
  // 关闭时评审输入开头不注入被测 agent 的（装饰后）系统提示词。占位符展开已
  // 废弃——注入走评审输入（prompt）开头，对所有 Provider 生效
  function buildInjectCb(rule) {
    const label = document.createElement("label");
    label.className = "ts-msg-rule-inject";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = !(rule && rule.inject_system_prompt === false);
    cb.title =
      "LLM 评审时在评审输入开头注入被测 Agent 的系统提示词（未捕获时自动跳过）";
    label.append(cb, document.createTextNode("注入提示词"));
    cb.addEventListener("change", markDirty);
    return label;
  }

  // LLM 规则字段区（profile + context 下拉 + 可选切片范围输入 + 注入开关）；
  // 规则行与最终断言行共用。withSlice 为真时（消息规则）提供切片范围输入——
  // 最终断言的范围由行内 scope 输入承担，不重复配置
  function buildLlmBox(rule, withSlice) {
    const box = document.createElement("span");
    box.className = "ts-msg-rule-llm";
    const ctxSel = buildContextSelect(rule);
    const sliceInp = withSlice ? buildSliceInput(rule) : null;
    const refreshSliceVisible = () => {
      if (sliceInp) sliceInp.hidden = ctxSel.value !== "slice";
    };
    ctxSel.addEventListener("change", refreshSliceVisible);
    refreshSliceVisible();
    box.append(buildProfileSelect(rule), ctxSel);
    if (sliceInp) box.append(sliceInp);
    box.append(buildInjectCb(rule));
    return box;
  }

  // 「取反」开关（消息断言叶行，顶层与任意组内都提供）：表达 json / non_empty
  // 等无否定类型的取反——包裹 {op: "not", rule: <叶>}，后端对 error 等 pass 为
  // null 的 verdict 不取反（评审失败单列，不掩盖组合结果）
  function buildNotCheckbox(checked) {
    const label = document.createElement("label");
    label.className = "ts-msg-rule-not";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = !!checked;
    cb.title = "取反：断言通过 → 不通过、未通过 → 通过（评审失败不取反）";
    label.append(cb, document.createTextNode("取反"));
    cb.addEventListener("change", markDirty);
    return label;
  }

  // 任意组容器：标签 + 删除 + 子规则区（缩进）。组内不嵌套任意组（极小化——
  // 子行只加叶）；加载到嵌套组合数据时递归渲染兜底
  function buildGroupRow(group) {
    const box = document.createElement("div");
    box.className = "ts-msg-rule-group";

    const head = document.createElement("div");
    head.className = "ts-msg-rule-group-head";
    const tag = document.createElement("span");
    tag.textContent = "任意组（至少一条通过）";
    const del = document.createElement("button");
    del.type = "button";
    del.className = "icon-btn ts-msg-rule-group-del";
    del.textContent = "✕";
    del.title = "删除该任意组";
    del.addEventListener("click", () => {
      box.remove();
      markDirty();
    });
    head.append(tag, del);

    const body = document.createElement("div");
    body.className = "ts-msg-rule-group-body";
    for (const child of group && Array.isArray(group.rules) ? group.rules : []) {
      const row = buildRuleRow(child);
      if (row) body.appendChild(row);
    }
    const add = document.createElement("button");
    add.type = "button";
    add.className = "ts-msg-add-rule";
    add.textContent = "＋ 断言";
    add.title = "在任意组内添加一条断言（组内任一断言通过即通过）";
    add.addEventListener("click", () => {
      body.insertBefore(buildRuleRow(null), add);
      markDirty();
    });
    body.appendChild(add);

    box.append(head, body);
    return box;
  }

  // 单条断言编辑行：取反开关 / 类型下拉 / 值输入（值类规则才显示）/ LLM
  // 字段区 / 删除。类型切换「LLM 评审」时值输入替换为 profile + context 下拉。
  // 组合节点：op === "any" → 任意组容器（buildGroupRow 递归渲染子行）；
  // op === "not" → 叶行 + 取反勾选（拆包展示，收集时重新包裹）
  function buildRuleRow(rule) {
    if (rule && rule.op === "any") return buildGroupRow(rule);
    const not = !!(rule && rule.op === "not");
    const inner = not && rule && rule.rule ? rule.rule : rule;

    const wrap = document.createElement("div");
    wrap.className = "ts-msg-rule";

    const notCb = buildNotCheckbox(not);
    const sel = buildRuleTypeSelect(inner);

    const val = document.createElement("input");
    val.type = "text";
    val.className = "ts-msg-rule-value";
    val.placeholder = "断言值";
    if (inner && RULE_VALUE_TYPES.has(inner.type)) {
      val.value = Array.isArray(inner.value)
        ? inner.value.join(", ")
        : inner.value != null
          ? String(inner.value)
          : "";
    }

    const llmBox = buildLlmBox(inner, true);
    for (const innerSel of llmBox.querySelectorAll("select")) {
      innerSel.addEventListener("change", markDirty);
    }
    for (const innerInp of llmBox.querySelectorAll(".ts-msg-rule-slice")) {
      innerInp.addEventListener("input", markDirty);
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

    wrap.append(notCb, sel, val, llmBox, del);
    return wrap;
  }

  // 规则区底部按钮栏：「＋ 断言」与「＋ 任意组」并排（组内任一条通过即通过）
  function buildRuleAddBar(rulesBox) {
    const bar = document.createElement("div");
    bar.className = "ts-msg-rule-addbar";
    const add = document.createElement("button");
    add.type = "button";
    add.className = "ts-msg-add-rule";
    add.textContent = "＋ 断言";
    add.title = "添加一条断言（多条按全部通过判定）";
    add.addEventListener("click", () => {
      rulesBox.insertBefore(buildRuleRow(null), bar);
      markDirty();
    });
    const addGroup = document.createElement("button");
    addGroup.type = "button";
    addGroup.className = "ts-msg-add-rule ts-msg-add-group";
    addGroup.textContent = "＋ 任意组";
    addGroup.title = "添加一个任意组：组内断言为「或」关系，任一通过即通过";
    addGroup.addEventListener("click", () => {
      rulesBox.insertBefore(buildRuleRow({ op: "any", rules: [] }), bar);
      markDirty();
    });
    bar.append(add, addGroup);
    return bar;
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

  // 报告 LLM 配置（测试集级持久化）：Provider 下拉复用评审 profile 同款
  // 「供应商（当前模型）」构建；Provider 已删除 → 保留占位选项防静默丢绑定。
  // 缺省不单独配模型（与评审 profile 一致用 Provider 当前模型）。
  function initReportLlmConfig(ts) {
    const cfg =
      ts && ts.report_llm && typeof ts.report_llm === "object" ? ts.report_llm : null;
    const sel = $("ts-report-llm-provider");
    sel.innerHTML =
      `<option value="">选择 Provider…</option>` +
      state.providers
        .map(
          (p) =>
            `<option value="${escapeHtml(p.id)}">${escapeHtml(p.name || p.id)}${
              p.current_model ? `（${escapeHtml(p.current_model)}）` : ""
            }</option>`,
        )
        .join("");
    if (cfg && cfg.provider_id) {
      if (!state.providers.some((p) => p.id === cfg.provider_id)) {
        const opt = document.createElement("option");
        opt.value = cfg.provider_id;
        opt.textContent = `${cfg.provider_id}（Provider 已删除）`;
        sel.appendChild(opt);
      }
      sel.value = cfg.provider_id;
    }
    $("ts-report-llm-prompt").value = (cfg && cfg.system_prompt) || "";
  }

  // 收集报告 LLM 配置 → 保存 payload 的 report_llm 字段；未选 Provider → null
  // （后端清洗时置 None，与「未配置」一致）
  function collectReportLlmConfig() {
    const providerId = $("ts-report-llm-provider").value;
    if (!providerId) return null;
    return {
      provider_id: providerId,
      system_prompt: $("ts-report-llm-prompt").value,
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
    for (const inner of llmBox.querySelectorAll(".ts-msg-rule-slice")) {
      inner.addEventListener("input", markDirty);
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

  // 收集最终断言列表：空类型 / 无效规则行丢弃；scope 非法 → "all"（保存前校验已拦截）。
  // 规则构造在 pure.js 的 buildRule（纯函数，LLM 行读 profile/context 下拉）
  function collectFinalRules() {
    const rules = [];
    for (const wrap of $("ts-final-rules").querySelectorAll(".ts-final-rule")) {
      const type = wrap.querySelector(".ts-msg-rule-type").value;
      const llmBox = wrap.querySelector(".ts-msg-rule-llm");
      const sliceEl = llmBox.querySelector(".ts-msg-rule-slice");
      const rule = buildRule(
        type,
        wrap.querySelector(".ts-msg-rule-value").value,
        llmBox.querySelector(".ts-msg-rule-profile").value,
        llmBox.querySelector(".ts-msg-rule-context").value,
        sliceEl ? sliceEl.value : "",
        llmBox.querySelector(".ts-msg-rule-inject input").checked,
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

  // 连续勾选的行合并为批量段（单条勾选 = [i,i]），实时预览。
  // 空文本行不会保存（collectEditorRows 丢弃），分段索引须基于保留后的行，与保存一致。
  // 区间合并逻辑在 pure.js 的 rangesFromFlags。
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

  // 单条叶行收集：读类型/值/profile/context/切片/注入开关与「取反」勾选，
  // 交给 pure.js 的 buildRule 构造（not 包裹 / 无效行丢弃在纯函数侧）
  function collectLeafInput(wrap) {
    const llmBox = wrap.querySelector(".ts-msg-rule-llm");
    const sliceEl = llmBox.querySelector(".ts-msg-rule-slice");
    const notEl = wrap.querySelector(".ts-msg-rule-not input");
    return {
      type: wrap.querySelector(".ts-msg-rule-type").value,
      value: wrap.querySelector(".ts-msg-rule-value").value,
      profileId: llmBox.querySelector(".ts-msg-rule-profile").value,
      context: llmBox.querySelector(".ts-msg-rule-context").value,
      sliceRange: sliceEl ? sliceEl.value : "",
      injectSystemPrompt: llmBox.querySelector(".ts-msg-rule-inject input").checked,
      not: notEl ? notEl.checked : false,
    };
  }

  // 规则区收集：按 DOM 顺序遍历叶行与任意组容器（组内递归收集子叶），
  // 按钮栏等非规则元素跳过；任意组输入经 collectRules 的 group 分支构造
  function collectRuleInputsFromBox(box) {
    const inputs = [];
    for (const child of box.children) {
      if (child.classList.contains("ts-msg-rule")) {
        inputs.push(collectLeafInput(child));
      } else if (child.classList.contains("ts-msg-rule-group")) {
        const body = child.querySelector(".ts-msg-rule-group-body");
        inputs.push({
          kind: "group",
          children: body ? collectRuleInputsFromBox(body) : [],
        });
      }
    }
    return inputs;
  }

  // 收集编辑器行：DOM 读取薄包装——逐行把输入读成纯数据后交给 pure.js 的
  // collectEditorRows 构造消息列表与批量段（纯逻辑集中一处、可被 node:test
  // 动态测试）。空文本行丢弃、批量段索引基于保留后的行；每条消息带规则列表
  // （collectRules 纯函数收集，组合节点经 collectRuleInputsFromBox 递归）、
  // 命令标记、可选身份（collectSender）与自动@
  function collectEditorRows() {
    const rows = [];
    for (const row of $("ts-messages").querySelectorAll(".ts-msg-row")) {
      const ruleInputs = collectRuleInputsFromBox(row.querySelector(".ts-msg-rules"));
      const text = row.querySelector(".ts-msg-text").value;
      const rules = collectRules(ruleInputs);
      const sender = collectSender(row.querySelector(".ts-msg-sender"));
      rows.push({
        text,
        rules,
        sender,
        isCommand: row.querySelector(".ts-msg-command input").checked,
        autoAt: row.querySelector(".ts-msg-auto-at input").checked,
        batch: row.querySelector(".ts-msg-batch input").checked,
      });
    }
    return collectEditorRowsData(rows);
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
      const sliceEl = wrap.querySelector(".ts-msg-rule-slice");
      if (
        wrap.querySelector(".ts-msg-rule-context").value === "slice" &&
        sliceEl &&
        sliceEl.value.trim() &&
        parseSliceRange(sliceEl.value) === null
      ) {
        return `${label}：切片范围格式无效（如 2-4 表示第 2 到第 4 步，多段用逗号分隔如 3-4,10-12，或留空 = 当前步及之前全部）`;
      }
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

  // 最终断言 scope 解析（pure.js parseScope：空 / "all" → "all"；"2-4" →
  // {from:1, to:3}；"3" → {from:2, to:2}；非法 → null）。消息规则的切片范围
  // 走 parseSliceRange（支持多段逗号分隔，见 validateRuleRow）。

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
        report_llm: collectReportLlmConfig(),
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

  // 信封解析（parseTestsetEnvelope 在 pure.js：校验 format/version，v1 兼容
  // 单条 rule → rules，v2 处理 rules / is_command / 身份配置与最终断言）。

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
  $("btn-ts-mode").addEventListener("click", () => reportView.toggleViewMode());
  // 报告产出开关（测试集配置）：变更即脏
  $("ts-report-enabled").addEventListener("change", markDirty);
  // 报告 LLM 配置（测试集配置）：变更即脏
  $("ts-report-llm-provider").addEventListener("change", markDirty);
  $("ts-report-llm-prompt").addEventListener("input", markDirty);
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
