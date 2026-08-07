// testset_list.js — 左侧测试集列表 + 运行弹窗 + 评审 Profile 管理
// 与 group_list.js 同模式：由 app.js 通过 createTestsetList(env) 创建。
// env 注入视图动作（showRunStatus / runTestset / viewTestsetRun /
// switchToTestsets），本模块不 import app.js，模块间依赖保持单向。
// 列表条目是「有名字的条目」：选中后右侧显示编辑窗口（渲染/保存/导出/导入
// 在 testset_editor.js，经 setDeps 装配）；运行由后端后台任务驱动，
// 本模块只负责启动。最近运行 / 报告已迁入测试集视图的报告页（testset_editor.js）。
// 评审 Profile 的增删改管理在本模块（左侧卡片「评审 Profile」tab，镜像
// identity_list.js 的列表 + modal 表单模式）；编辑器只保留消息规则 / 最终
// 断言行内 profile 下拉的构建与重建（editor.refreshAllProfileSelects）。
import {
  createReviewer,
  createTestset,
  deleteReviewers,
  deleteTestsets,
  listReviewers,
  listTestsets,
  previewReviewerMetrics,
  updateReviewer,
} from "./api.js";
import { state } from "./state.js";
import { openModal, showModal } from "./modal.js";
import { escapeHtml, field } from "./utils.js";
import { CONTEXT_MODES, createTestsetEditor } from "./testset_editor.js";

// 评审指标类型（与后端 reviewer profile 的 metrics.type 枚举一致）
const METRIC_TYPES = [
  ["number", "数字"],
  ["enum", "枚举"],
  ["text", "文本"],
];

const $ = (id) => document.getElementById(id);

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
    renderReviewerList(); // 评审 Profile 列表随视图切换 / 刷新一并重绘（数据来自 state）
    syncRunTestsetSelect();
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
    // 与切换 / 删除 / 导入一致：有未保存修改时先确认，否则 createTestset 之后
    // clearDirty 会把脏状态掩盖掉，未保存的编辑被静默丢弃
    if (editor.getDirty()) {
      showModal("新建将丢弃当前测试集的未保存修改，确定吗？", {
        danger: true,
        onOk: () => doOpenNewTestset(),
      });
      return;
    }
    doOpenNewTestset();
  }

  function doOpenNewTestset() {
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

  // ---------- 时间格式化（编辑器元信息 / 报告视图共用） ----------

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

  // ---------- 评审 Profile（左侧「评审 Profile」tab 管理） ----------

  // 与「身份与群聊」同款 tab：测试集 / 评审 Profile 一次只渲染一个列表
  function switchTestsetTab(tab) {
    document.querySelectorAll(".testsets-card .tab-btn").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.tab === tab);
    });
    document.querySelectorAll(".testsets-card .tab-pane").forEach((pane) => {
      pane.hidden = pane.dataset.pane !== tab;
    });
  }

  // 上下文模式中文名（profile 摘要展示用；CONTEXT_MODES 是唯一来源）
  function contextModeLabel(mode) {
    const found = CONTEXT_MODES.find(([v]) => v === mode);
    return found ? found[1] : mode;
  }

  // 渲染评审 Profile 列表：无 profile → 提示 + 新建入口；有 → 逐条摘要 + 编辑/删除
  function renderReviewerList() {
    const list = $("reviewer-list");
    list.innerHTML = "";
    $("reviewer-count").textContent = state.reviewers.length
      ? `${state.reviewers.length} 个 Profile`
      : "";

    if (!state.reviewers.length) {
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent = "暂无评审 Profile，点下方「＋」创建";
      list.appendChild(empty);
    }
    for (const profile of state.reviewers) {
      const metricsCount = Array.isArray(profile.metrics) ? profile.metrics.length : 0;
      const item = document.createElement("div");
      item.className = "group-item";
      item.innerHTML =
        `<div class="group-head">` +
        `<span class="group-name" title="${escapeHtml(profile.name)}">${escapeHtml(profile.name)}</span>` +
        `<span class="group-actions">` +
        `<button class="icon-btn" data-action="edit" title="编辑评审 Profile">✎</button>` +
        `<button class="icon-btn danger" data-action="delete" title="删除评审 Profile">✕</button>` +
        `</span>` +
        `</div>` +
        `<div class="group-meta">` +
        `<span class="badge" title="Provider">${escapeHtml(profile.provider_id)}</span>` +
        `<span class="badge" title="模型">${escapeHtml(profile.model || "—")}</span>` +
        `<span class="badge" title="评审上下文">${contextModeLabel(profile.context)}</span>` +
        `<span class="badge" title="输出契约指标数">${metricsCount} 个指标</span>` +
        `</div>`;
      item
        .querySelector('[data-action="edit"]')
        .addEventListener("click", () => openProfileForm(profile));
      item
        .querySelector('[data-action="delete"]')
        .addEventListener("click", () => deleteReviewer(profile));
      list.appendChild(item);
    }

    const add = document.createElement("button");
    add.className = "add-block";
    add.textContent = "＋ 新建评审 Profile";
    add.addEventListener("click", () => openProfileForm(null));
    list.appendChild(add);
  }

  // 删除评审 Profile：确认后删除并刷新（引用它的 LLM 规则运行时得到错误判定）
  function deleteReviewer(profile) {
    showModal(
      `确定删除评审 Profile「${profile.name}」吗？引用它的 LLM 评审规则在运行时将得到「找不到评审 profile」的错误判定。`,
      {
        danger: true,
        onOk: async () => {
          await deleteReviewers([profile.id]);
          await refreshReviewers();
          showRunStatus("ok", "评审 Profile 已删除");
        },
      },
    );
  }

  // 拉取评审 Profile 到 state 并重绘列表；顺带重建编辑器内已渲染的 profile
  // 下拉（消息规则行与最终断言行，经 editor.refreshAllProfileSelects）
  async function refreshReviewers() {
    try {
      const data = await listReviewers();
      state.reviewers = Array.isArray(data.reviewers) ? data.reviewers : [];
    } catch (err) {
      state.reviewers = [];
      showRunStatus("error", "加载评审 Profile 失败: " + err.message);
    }
    renderReviewerList();
    editor.refreshAllProfileSelects();
  }

  // 评审 Profile 表单弹窗（新建 / 编辑共用）：provider / 模型 / 提示词 / 输出契约指标。
  // 校验失败（throw）停留在弹窗；保存成功后刷新 Profile 列表与编辑器下拉
  function openProfileForm(existing) {
    const wrap = document.createElement("div");
    wrap.className = "form-col";

    const inpName = document.createElement("input");
    inpName.type = "text";
    inpName.value = existing ? existing.name || "" : "";
    const inpNote = document.createElement("input");
    inpNote.type = "text";
    inpNote.placeholder = "备注（可选）";
    inpNote.value = existing ? existing.note || "" : "";

    const selProvider = document.createElement("select");
    selProvider.innerHTML =
      `<option value="">选择 Provider…</option>` +
      state.providers
        .map(
          (p) =>
            `<option value="${escapeHtml(p.id)}">${escapeHtml(p.name || p.id)}</option>`,
        )
        .join("");
    if (existing && existing.provider_id) selProvider.value = existing.provider_id;

    const inpModel = document.createElement("input");
    inpModel.type = "text";
    inpModel.placeholder = "模型名（如 gpt-4o），建议与当前 Provider 模型一致";
    inpModel.value = existing ? existing.model || "" : "";
    selProvider.addEventListener("change", () => {
      const p = state.providers.find((x) => x.id === selProvider.value);
      if (p && p.current_model) inpModel.value = p.current_model;
    });

    const selContext = document.createElement("select");
    selContext.innerHTML = CONTEXT_MODES.filter(([v]) => v)
      .map(([v, label]) => `<option value="${v}">${label}</option>`)
      .join("");
    selContext.value = existing && existing.context ? existing.context : "reply";

    const taPrompt = document.createElement("textarea");
    taPrompt.className = "json-editor";
    // 覆盖 .json-editor 的 360px 默认高度：弹窗紧凑，仍可拖拽拉长
    // （弹窗正文区限高内部滚动，拉长不撑爆页面）
    taPrompt.style.minHeight = "120px";
    taPrompt.rows = 5;
    taPrompt.placeholder =
      "评审系统提示词。输出必须是 JSON 对象，键与下方指标 key 一一对应。";
    taPrompt.value = existing ? existing.system_prompt || "" : "";
    const promptHint = document.createElement("p");
    promptHint.className = "hint";
    promptHint.textContent =
      "提示词可用占位符 {{metrics}}（保存/运行时自动展开为下方预览所示内容），占位符照常可用；也可复制预览内容到提示词中手动修改——两种方式共存、任选其一。";
    const agentPromptHint = document.createElement("p");
    agentPromptHint.className = "hint";
    agentPromptHint.textContent =
      "提示词还可用 {{agent_system_prompt}} 占位符：运行时展开为被测 agent 的装饰后系统提示词（未捕获时为空串），由你在提示词中自行编排。";

    const metricsBox = buildMetricsEditor(existing ? existing.metrics : null);

    const previewHint = document.createElement("p");
    previewHint.className = "hint";
    previewHint.textContent =
      "{{metrics}} 占位符展开内容预览（随指标编辑实时更新；占位符仍照常生效）：";
    const previewEl = document.createElement("pre");
    previewEl.className = "metrics-preview";
    previewEl.textContent = "暂无指标，添加指标后此处实时显示 {{metrics}} 展开内容。";
    const btnCopyPreview = document.createElement("button");
    btnCopyPreview.type = "button";
    btnCopyPreview.className = "copy-btn";
    btnCopyPreview.textContent = "复制预览";
    const previewBox = document.createElement("div");
    previewBox.className = "form-col";
    previewBox.append(previewHint, previewEl, btnCopyPreview);

    wrap.append(
      field("名称", inpName),
      field("备注", inpNote),
      field("Provider", selProvider),
      field("模型", inpModel),
      field("上下文", selContext),
      field("系统提示词", taPrompt),
      promptHint,
      agentPromptHint,
      field("输出指标", metricsBox),
      previewBox,
    );

    // ---- {{metrics}} 预览：防抖实时刷新 + 复制（与占位符共存，见上方提示） ----
    let previewSeq = 0;
    let previewTimer = null;
    const renderPreview = async () => {
      // seq 在空行分支之前递增：删除全部指标后，过期响应也不得覆盖空态文案
      const seq = ++previewSeq;
      const rows = collectMetricsTolerant(metricsBox);
      if (!rows.length) {
        previewEl.textContent = "暂无指标，添加指标后此处实时显示 {{metrics}} 展开内容。";
        return;
      }
      try {
        const data = await previewReviewerMetrics(rows);
        if (seq !== previewSeq) return; // 丢弃过期响应（快速连续编辑）
        previewEl.textContent = data && data.description ? data.description : "";
      } catch (err) {
        if (seq !== previewSeq) return;
        previewEl.textContent = "预览加载失败: " + (err.message || err);
      }
    };
    const schedulePreview = () => {
      clearTimeout(previewTimer);
      previewTimer = setTimeout(renderPreview, 300);
    };
    // 委托覆盖新增/删除行（删行 row.remove() 不触发 input/change，须补 click）
    metricsBox.addEventListener("input", schedulePreview);
    metricsBox.addEventListener("change", schedulePreview);
    metricsBox.addEventListener("click", schedulePreview);
    renderPreview();

    let copyTimer = null;
    const flashCopy = (text) => {
      btnCopyPreview.textContent = text;
      clearTimeout(copyTimer);
      copyTimer = setTimeout(() => {
        btnCopyPreview.textContent = "复制预览";
      }, 1500);
    };
    btnCopyPreview.addEventListener("click", async () => {
      const text = previewEl.textContent;
      if (!text || !text.trim() || text.startsWith("暂无指标")) return;
      try {
        await navigator.clipboard.writeText(text);
        flashCopy("已复制");
        return;
      } catch {
        // 沙箱未授予 clipboard-write（插件页 iframe sandbox 无 allow-clipboard-write）→ 回退
      }
      let ok = false;
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.cssText = "position:fixed;opacity:0;";
      document.body.appendChild(ta);
      try {
        ta.select();
        ok = document.execCommand("copy");
      } catch {
        ok = false;
      } finally {
        ta.remove(); // select / execCommand 抛错也要清理，防隐藏 textarea 泄漏
      }
      if (ok) {
        flashCopy("已复制");
        return;
      }
      const sel = window.getSelection();
      const range = document.createRange();
      range.selectNodeContents(previewEl);
      sel.removeAllRanges();
      sel.addRange(range);
      flashCopy("已选中，请按 Ctrl+C 复制");
    });

    openModal({
      title: existing ? "编辑评审 Profile" : "新建评审 Profile",
      content: wrap,
      okText: "保存",
      wide: true,
      onOk: async () => {
        if (!inpName.value.trim()) throw new Error("评审 Profile 名称不能为空");
        if (!selProvider.value) throw new Error("请选择 Provider");
        if (!inpModel.value.trim()) throw new Error("模型名不能为空");
        if (!taPrompt.value.trim()) throw new Error("系统提示词不能为空");
        const metrics = collectMetrics(metricsBox);
        if (!metrics.length) throw new Error("至少需要一个评审指标");
        const payload = {
          name: inpName.value.trim(),
          note: inpNote.value.trim() || undefined,
          provider_id: selProvider.value,
          model: inpModel.value.trim(),
          context: selContext.value,
          system_prompt: taPrompt.value,
          metrics,
        };
        if (existing) await updateReviewer(existing.id, payload);
        else await createReviewer(payload);
        await refreshReviewers();
        showRunStatus("ok", existing ? "评审 Profile 已更新" : `评审 Profile「${inpName.value.trim()}」已创建`);
      },
    });
  }

  // 输出指标编辑器：逐行 key / type / 附加输入（阈值 / 枚举 / 通过分类）+ 添加按钮
  function buildMetricsEditor(metrics) {
    const box = document.createElement("div");
    box.className = "form-col";
    box.style.gap = "4px";
    for (const m of metrics || []) box.appendChild(buildMetricRow(m));
    if (!metrics || !metrics.length) box.appendChild(buildMetricRow(null));
    const add = document.createElement("button");
    add.type = "button";
    add.className = "metric-add";
    add.textContent = "＋ 添加指标";
    add.addEventListener("click", () => box.insertBefore(buildMetricRow(null), add));
    box.appendChild(add);
    return box;
  }

  // 单行指标：key / type 下拉；type 决定附加输入（number → 通过阈值，
  // enum → 枚举值 + 通过分类）
  function buildMetricRow(metric) {
    const row = document.createElement("div");
    row.className = "metric-row";
    const key = document.createElement("input");
    key.type = "text";
    key.className = "metric-key";
    key.placeholder = "指标 key";
    if (metric) key.value = metric.key || "";
    const type = document.createElement("select");
    type.className = "metric-type";
    type.innerHTML = METRIC_TYPES.map(
      ([v, label]) => `<option value="${v}">${label}</option>`,
    ).join("");
    type.value = metric && metric.type ? metric.type : "number";
    const extra = document.createElement("span");
    extra.className = "metric-extra";
    const del = document.createElement("button");
    del.type = "button";
    del.className = "icon-btn";
    del.textContent = "✕";
    del.title = "删除该指标";
    del.addEventListener("click", () => row.remove());
    const refreshExtra = () => {
      extra.innerHTML = "";
      if (type.value === "number") {
        const t = document.createElement("input");
        t.type = "number";
        t.className = "metric-threshold";
        t.placeholder = "通过阈值（数值 ≥ 此值）";
        if (metric && metric.pass_threshold != null) t.value = String(metric.pass_threshold);
        extra.appendChild(t);
      } else if (type.value === "enum") {
        const v = document.createElement("input");
        v.type = "text";
        v.className = "metric-enum-values";
        v.placeholder = "枚举值，逗号分隔";
        if (metric && Array.isArray(metric.enum_values)) v.value = metric.enum_values.join(", ");
        const c = document.createElement("input");
        c.type = "text";
        c.className = "metric-pass-categories";
        c.placeholder = "通过分类，逗号分隔";
        if (metric && Array.isArray(metric.pass_categories)) {
          c.value = metric.pass_categories.join(", ");
        }
        extra.append(v, c);
      }
    };
    type.addEventListener("change", refreshExtra);
    refreshExtra();
    row.append(key, type, extra, del);
    return row;
  }

  // 逗号分隔文本 → 去空白非空字符串数组
  function splitList(text) {
    return String(text || "")
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);
  }

  // 收集输出指标；非法（key 空 / type 空 / 阈值非数字）抛错让弹窗停留在表单
  function collectMetrics(metricsBox) {
    const metrics = [];
    for (const row of metricsBox.querySelectorAll(".metric-row")) {
      const key = row.querySelector(".metric-key").value.trim();
      const type = row.querySelector(".metric-type").value;
      if (!key && !type) continue;
      if (!key) throw new Error("评审指标 key 不能为空");
      if (!type) throw new Error("评审指标 type 不能为空");
      const m = { key, type };
      if (type === "number") {
        const t = row.querySelector(".metric-threshold");
        const tv = t ? t.value.trim() : "";
        if (tv !== "") {
          const n = Number(tv);
          if (!Number.isFinite(n)) throw new Error(`指标「${key}」的通过阈值必须是数字`);
          m.pass_threshold = n;
        }
      } else if (type === "enum") {
        const v = splitList(row.querySelector(".metric-enum-values").value);
        if (v.length) m.enum_values = v;
        const c = splitList(row.querySelector(".metric-pass-categories").value);
        if (c.length) m.pass_categories = c;
      }
      metrics.push(m);
    }
    return metrics;
  }

  // 预览用的宽松收集：key 为空的整行跳过、不抛错（保存仍走严格 collectMetrics）
  function collectMetricsTolerant(metricsBox) {
    const metrics = [];
    for (const row of metricsBox.querySelectorAll(".metric-row")) {
      const key = row.querySelector(".metric-key").value.trim();
      const type = row.querySelector(".metric-type").value;
      if (!key) continue;
      const m = { key, type };
      if (type === "number") {
        const t = row.querySelector(".metric-threshold");
        const tv = t ? t.value.trim() : "";
        if (tv !== "") {
          const n = Number(tv);
          if (Number.isFinite(n)) m.pass_threshold = n;
        }
      } else if (type === "enum") {
        const v = splitList(row.querySelector(".metric-enum-values").value);
        if (v.length) m.enum_values = v;
        const c = splitList(row.querySelector(".metric-pass-categories").value);
        if (c.length) m.pass_categories = c;
      }
      metrics.push(m);
    }
    return metrics;
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
    viewTestsetRun,
  }));

  // 左侧卡片「测试集 / 评审 Profile」tab 切换（与身份与群聊同款）
  document.querySelectorAll(".testsets-card .tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => switchTestsetTab(btn.dataset.tab));
  });

  return { refreshTestsets, renderTestsetNav, refreshReviewers };
}
