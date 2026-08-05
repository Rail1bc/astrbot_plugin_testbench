// 会话测试台 - 页面脚本（入口模块）
// 视图层拆分为多个模块：弹窗（modal.js）、左侧测试组列表（group_list.js）、
// 聊天渲染（chat.js）、轮次对齐（align.js）、后端调用封装（api.js）、
// 共享状态（state.js）与工具函数（utils.js）。本模块负责会话面板、发送、
// 会话操作、面板排序与初始化，并组装各子模块。
import { createChatRenderer } from "./chat.js";
import { createAlignController } from "./align.js";
import { createGroupList } from "./group_list.js";
import { createTestsetList } from "./testset_list.js";
import {
  abortTestsetRun as abortTestsetRunApi,
  deleteSessions,
  getHistory,
  getPending,
  listConfs,
  listPlatforms,
  ready,
  regenerateHistory,
  resetSessions,
  runStatus,
  runTest,
  runTestset as runTestsetApi,
  runTestsetStatus,
  saveHistory,
} from "./api.js";
import { openModal, showModal } from "./modal.js";
import { state } from "./state.js";
import {
  confName,
  effectiveView,
  escapeHtml,
  platformName,
  statusText,
} from "./utils.js";

const $ = (id) => document.getElementById(id);

// ---------- 面板 ----------

function toggleOpen(id) {
  if (state.openIds.includes(id)) {
    state.openIds = state.openIds.filter((x) => x !== id);
    state.pinnedIds = state.pinnedIds.filter((x) => x !== id);
  } else {
    state.openIds.push(id);
    openPanel(id);
  }
  renderPanels();
  renderGroupList();
}

function openPanel(id) {
  if (state.panelEls.has(id)) return;
  const panel = document.createElement("div");
  panel.className = "panel";
  panel.dataset.id = id;
  panel.draggable = true;

  const s = effectiveView(id);
  const confBadge =
    s && s.conf_id
      ? `<span class="badge conf-badge conf">${escapeHtml(confName(s.conf_id))}</span>`
      : "";
  const platformBadge = s
    ? `<span class="badge platform-badge">${escapeHtml(platformName(s.platform_id))}</span>`
    : "";
  const groupBadge = s
    ? `<span class="badge group-badge" title="所属测试组">${escapeHtml(s.group_name || "")}</span>`
    : "";

  panel.innerHTML =
    `<div class="panel-head" title="拖拽排序">` +
    `<span class="drag-handle">≡</span>` +
    `<span class="panel-info">` +
    `<span class="panel-title">${escapeHtml(s ? s.name : id)}</span>` +
    groupBadge + platformBadge + confBadge +
    `</span>` +
    `<span class="panel-actions">` +
    `<button class="icon-btn" data-action="history" title="编辑对话历史（JSON）">编辑</button>` +
    `<button class="icon-btn" data-action="reset" title="重置对话历史">重置</button>` +
    `<button class="icon-btn" data-action="pin" title="置顶">置顶</button>` +
    `<button class="icon-btn" data-action="close" title="关闭">✕</button>` +
    `</span>` +
    `</div>` +
    `<div class="panel-body">` +
    `<div class="chat"></div>` +
    `<div class="panel-pending" hidden></div>` +
    `<div class="panel-status" hidden></div>` +
    `</div>` +
    `<div class="panel-input">` +
    `<input class="msg-input" type="text" placeholder="发送消息到本会话（Enter 发送）" />` +
    `<button class="btn primary send-btn">发送</button>` +
    `</div>`;

  panel.querySelector('[data-action="close"]').addEventListener("click", () => toggleOpen(id));
  panel.querySelector('[data-action="reset"]').addEventListener("click", () => resetHistory(id));
  panel.querySelector('[data-action="pin"]').addEventListener("click", () => pin(id));
  panel
    .querySelector('[data-action="history"]')
    .addEventListener("click", () => void openHistoryEditor(id));
  const input = panel.querySelector(".msg-input");
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.isComposing) sendToOne(id, input.value);
  });
  panel.querySelector(".send-btn").addEventListener("click", () => sendToOne(id, input.value));

  state.panelEls.set(id, panel);
  renderPanels();
  void loadHistory(id);
}

// 打开组内全部会话（group_list.js 的「打开全部」按钮经 env 调用本函数）
function openAll(gid) {
  const g = state.groups.find((x) => x.id === gid);
  if (!g) return;
  for (const s of g.sessions || []) {
    if (!state.openIds.includes(s.id)) {
      state.openIds.push(s.id);
      openPanel(s.id);
    }
  }
  renderPanels();
  renderGroupList();
}

async function loadHistory(id) {
  const panel = state.panelEls.get(id);
  if (!panel) return;
  const chat = panel.querySelector(".chat");
  try {
    const data = await getHistory(id);
    const conversations = data.conversations || [];
    state.historyCache.set(id, conversations);
    renderChat(panel, conversations);
    // 记录本次成功刷新时刻：完成的消息刷入历史后即从在途条移除
    historyRefreshedAt.set(id, Date.now());
    if (align.isAlignMode()) align.reflowAlign();
  } catch (err) {
    chat.innerHTML = `<div class="empty">加载历史失败: ${escapeHtml(err.message)}</div>`;
  }
}

// 渲染逻辑拆在 chat.js（createChatRenderer），本包装函数在初始化处注入 align 控制器
function renderChat(panel, conversations) {
  return chat.renderChat(panel, conversations);
}

// 面板头部「编辑」：以 JSON 编辑器整体查看 / 替换该会话的对话历史。
// 结构即 /sessions/<id>/history 返回的 { conversations: [...] }；编辑、
// 新增（不带 conversation_id）、删除对话都通过直接修改 JSON 完成。
async function openHistoryEditor(id) {
  // 每次打开都拉取最新历史，避免缓存为空时保存把全部对话误删
  let convs;
  try {
    const data = await getHistory(id);
    convs = data.conversations || [];
  } catch (err) {
    showModal("加载对话历史失败: " + err.message);
    return;
  }
  const s = effectiveView(id);
  const ta = document.createElement("textarea");
  ta.className = "json-editor";
  ta.value = JSON.stringify({ conversations: convs }, null, 2);
  const hint = document.createElement("p");
  hint.className = "hint";
  hint.textContent =
    "保存将用此 JSON 完全替换对话历史：未列出的对话会被删除，不带 conversation_id 的对象会新建对话，conversation_id 在库中已不存在的对象也会新建占位对话。仅建议有能力的用户修改；改坏可用会话的「重置」恢复。";
  const wrap = document.createElement("div");
  wrap.className = "form-col";
  wrap.append(ta, hint);
  openModal({
    title: `编辑对话历史 · ${s ? s.name : id}`,
    content: wrap,
    okText: "保存",
    wide: true,
    onOk: async () => {
      let parsed;
      try {
        parsed = JSON.parse(ta.value);
      } catch (err) {
        throw new Error("JSON 解析失败: " + err.message);
      }
      if (!parsed || !Array.isArray(parsed.conversations)) {
        throw new Error("JSON 必须是 { conversations: [...] } 结构");
      }
      await saveHistory({ id, conversations: parsed.conversations });
      void loadHistory(id);
      showRunStatus("ok", "对话历史已保存");
    },
  });
}

async function regenerateMsg(id, index) {
  const panel = state.panelEls.get(id);
  try {
    const resp = await regenerateHistory({ id, index });
    if (panel) panelStatus(panel, "warn", "重新生成中…");
    pollRun(
      resp.test_id,
      (r) => {
        const p = state.panelEls.get(id);
        if (!p) return;
        if (r.status === "ok") {
          panelStatus(p, "ok", `重新生成完成（${r.duration}s）`);
        } else {
          panelStatus(
            p,
            r.status === "error" ? "error" : "warn",
            statusText(r.status) + (r.error ? `：${r.error}` : ""),
          );
        }
        void loadHistory(id);
      },
      () => {},
    );
  } catch (err) {
    if (panel) panelStatus(panel, "error", "重新生成失败: " + err.message);
    void loadHistory(id); // 后端可能已截断历史，刷新展示
  }
}

function panelStatus(panel, status, text) {
  const el = panel.querySelector(".panel-status");
  el.hidden = false;
  el.className = "panel-status " + status;
  el.textContent = text;
}

function clearPanelStatus(panel) {
  const el = panel.querySelector(".panel-status");
  el.hidden = true;
  el.textContent = "";
}

// ---------- 发送 ----------

// 轮询测试运行状态：每个会话完成时回调 onSession（单独刷新），全部完成回调 onAll
function pollRun(testId, onSession, onAll) {
  const seen = new Set();
  let stopped = false;
  const timer = setInterval(tick, 1000);
  async function tick() {
    if (stopped) return;
    let record;
    try {
      record = await runStatus(testId);
    } catch (err) {
      return; // 查询失败下轮重试
    }
    for (const r of record.results || []) {
      if (!seen.has(r.session_id)) {
        seen.add(r.session_id);
        try {
          onSession(r);
        } catch (err) {
          console.error("会话结果刷新失败:", err);
        }
      }
    }
    if (record.done) {
      stopped = true;
      clearInterval(timer);
      onAll(record);
    }
  }
  void tick();
}

async function sendToOne(id, text) {
  text = (text || "").trim();
  if (!text) return;
  const panel = state.panelEls.get(id);
  const input = panel.querySelector(".msg-input");
  input.value = "";
  panelStatus(panel, "warn", "发送中…");
  try {
    const resp = await runTest({ sessions: [id], text });
    pollRun(
      resp.test_id,
      (r) => {
        const p = state.panelEls.get(id);
        if (!p) return;
        if (r.status === "ok") {
          panelStatus(p, "ok", `回复成功（${r.duration}s）`);
        } else {
          panelStatus(
            p,
            r.status === "error" ? "error" : "warn",
            statusText(r.status) + (r.error ? `：${r.error}` : ""),
          );
        }
        void loadHistory(id);
      },
      () => {},
    );
  } catch (err) {
    panelStatus(panel, "error", "发送失败: " + err.message);
  }
}

async function sendToAll() {
  const ids = state.openIds.slice();
  const text = $("run-text").value.trim();
  if (!ids.length) {
    showRunStatus("warn", "请先在左侧打开至少一个会话");
    return;
  }
  if (!text) {
    showRunStatus("warn", "请输入群发消息");
    return;
  }
  // 不阻止重叠发送：agent 处理中可再次群发（真实「重复追问」场景），
  // 各会话的在途消息由面板底部的在途条实时展示
  $("run-text").value = "";
  showRunStatus("warn", `正在并发发送给 ${ids.length} 个会话…`);
  try {
    const resp = await runTest({ sessions: ids, text });
    pollRun(
      resp.test_id,
      (r) => {
        const panel = state.panelEls.get(r.session_id);
        if (panel) {
          if (r.status === "ok") {
            panelStatus(panel, "ok", `回复成功（${r.duration}s）`);
          } else {
            panelStatus(
              panel,
              r.status === "error" ? "error" : "warn",
              statusText(r.status) + (r.error ? `：${r.error}` : ""),
            );
          }
        }
        void loadHistory(r.session_id);
      },
      (record) => {
        const s = record.stats || {};
        const ok = record.results.filter((r) => r.status === "ok").length;
        const noReply = record.results.filter((r) => r.status === "no_reply").length;
        const err = record.results.filter((r) => r.status === "error").length;
        showRunStatus(
          err ? "warn" : "ok",
          `完成：成功 ${ok} / 无回复 ${noReply} / 错误 ${err}` +
            `，耗时 avg ${s.avg}s，p95 ${s.p95}s`,
        );
      },
    );
  } catch (err) {
    showRunStatus("error", "发送失败: " + err.message);
  }
}

function showRunStatus(status, text) {
  const el = $("run-status");
  el.hidden = false;
  el.className = "run-status " + status;
  el.textContent = text;
}

// ---------- 测试集运行（后端驱动） ----------

// 测试集运行由后端后台任务驱动（离开页面不中断），前端只负责启动与轮询进度；
// 记录可经「最近运行」找回，运行中/结束后均可点「查看」看结果表格。

async function runTestset(testset, ids) {
  try {
    const resp = await runTestsetApi({ testset_id: testset.id, sessions: ids });
    state.activeRunId = resp.run_id;
    $("btn-abort-run").hidden = false;
    const segText = segmentSummary(testset);
    showRunStatus(
      "warn",
      `测试集「${testset.name}」已启动（${resp.steps} 步${segText}），后台运行中…`,
    );
    pollTestsetRun(resp.run_id);
  } catch (err) {
    showRunStatus("error", "启动测试集运行失败: " + err.message);
  }
}

// 批量发送范围的启动文案：如「，含批量段 1-2、4」；无批量段返回空串
function segmentSummary(testset) {
  const ranges = testset.batch_ranges || [];
  if (!ranges.length) return "";
  const parts = ranges.map(([s, e]) => (s === e ? `${s + 1}` : `${s + 1}-${e + 1}`));
  return `，含批量段 ${parts.join("、")}`;
}

// 进度文案：当前步在某批量段内 → 显示段范围；否则显示第 i/N 步
function segmentLabel(run, idx) {
  for (const [s, e] of run.batch_ranges || []) {
    if (idx >= s && idx <= e) return `第 ${s + 1}–${e + 1} 步（批量）`;
  }
  return `第 ${idx + 1}/${run.steps.length} 步`;
}

// 当前测试集轮询定时器：新的轮询开始时停掉旧的，避免重复轮询叠加
let testsetPollTimer = null;

function pollTestsetRun(runId) {
  if (testsetPollTimer) clearInterval(testsetPollTimer);
  let stopped = false;
  let doneSteps = 0;
  const timer = setInterval(tick, 1000);
  testsetPollTimer = timer;
  async function tick() {
    if (stopped) return;
    let run;
    try {
      run = await runTestsetStatus(runId);
    } catch (err) {
      // 404：运行记录已被清理
      stopPolling(timer, "warn", "该测试集运行记录已过期，无法继续查看进度");
      return;
    }
    const name = run.testset_name || "测试集";
    if (run.status === "running") {
      const idx = Math.max(0, run.current_step);
      const stepText = run.steps[idx] ? run.steps[idx].text : "";
      showRunStatus(
        "warn",
        `测试集「${name}」运行中：${segmentLabel(run, idx)} — ${stepText}`,
      );
      // 步骤推进（完成步数增加）→ 刷新已打开面板的历史（逐条模式每步完成即可见）
      const completed = run.steps.filter((s) => s.status === "done").length;
      if (completed > doneSteps) {
        doneSteps = completed;
        for (const sid of state.openIds) void loadHistory(sid);
      }
      return;
    }
    stopPolling(timer);
    const failSteps = run.steps.filter((s) => s.status === "error").length;
    // 断言未通过（✗）与步骤/会话错误是两回事：断言失败只落在结果单元格，
    // 不改变会话 status——总结必须单独计数，否则出现「表格 3 个 ✗ 但总结错误 0」的误导
    const assertFails = (run.steps || []).reduce(
      (n, s) => n + (s.results || []).filter((r) => r.assertion && !r.assertion.pass).length,
      0,
    );
    const doneCount = run.steps.filter((s) => s.status === "done").length;
    const assertText = assertFails ? `，${assertFails} 条断言未通过` : "";
    const summary =
      run.status === "done"
        ? `测试集「${name}」运行完成（${run.steps.length} 步${assertText}）`
        : run.status === "cancelled"
          ? `测试集「${name}」已取消：当前步骤已完成，共完成 ${doneCount} 步`
          : `测试集「${name}」运行出错${failSteps ? `（${failSteps} 步失败）` : ""}${assertText}`;
    showRunStatus(run.status === "done" ? "ok" : "error", summary);
    for (const sid of state.openIds) void loadHistory(sid);
    showTestsetResults(run);
    void refreshTestsets();
  }
  function stopPolling(timer, status, text) {
    if (stopped) return;
    stopped = true;
    clearInterval(timer);
    if (testsetPollTimer === timer) testsetPollTimer = null;
    state.activeRunId = null;
    $("btn-abort-run").hidden = true;
    if (status) showRunStatus(status, text);
  }
  void tick();
}

// 结果表格弹窗：行=步骤（文本 + 失败原因），列=会话（状态 + 耗时 + 断言 ✓/✗），
// 行尾为该步 ok/no_reply/error 计数
function showTestsetResults(run) {
  const sessions = run.sessions || [];
  const table = document.createElement("table");
  table.className = "testset-results";
  const head = document.createElement("thead");
  head.innerHTML =
    `<tr><th>步骤</th>` +
    sessions.map((s) => `<th class="cell-session">${escapeHtml(s.name || s.id)}</th>`).join("") +
    `<th>结果</th></tr>`;
  const body = document.createElement("tbody");
  body.innerHTML = (run.steps || [])
    .map((step, i) => {
      const cells = sessions
        .map((s) => {
          const r = (step.results || []).find((x) => x.session_id === s.id);
          if (!r) return `<td class="cell-session">—</td>`;
          const dur = r.duration != null ? `（${r.duration}s）` : "";
          const assertion = r.assertion
            ? `<span class="${r.assertion.pass ? "assert-pass" : "assert-fail"}">${r.assertion.pass ? "✓" : "✗"}</span>`
            : "";
          return (
            `<td class="cell-session">` +
            escapeHtml(statusText(r.status)) + dur + assertion +
            `</td>`
          );
        })
        .join("");
      const ok = (step.results || []).filter((r) => r.status === "ok").length;
      const noReply = (step.results || []).filter((r) => r.status === "no_reply").length;
      const err = (step.results || []).filter((r) => r.status === "error").length;
      const assertFail = (step.results || []).filter(
        (r) => r.assertion && !r.assertion.pass,
      ).length;
      const stepErr = step.error
        ? `<div class="cell-err">失败：${escapeHtml(step.error)}</div>`
        : "";
      const batchBadge = (run.batch_ranges || []).some(([s, e]) => i >= s && i <= e)
        ? '<span class="badge conf">批量</span> '
        : "";
      return (
        `<tr>` +
        `<td class="cell-step">${batchBadge}${escapeHtml(step.text)}${stepErr}</td>` +
        cells +
        `<td>成功 ${ok} / 无回复 ${noReply} / 错误 ${err}${assertFail ? ` / 断言 ✗ ${assertFail}` : ""}</td>` +
        `</tr>`
      );
    })
    .join("");
  table.append(head, body);
  openModal({
    title: `测试集结果 · ${run.testset_name || ""}`,
    content: table,
    okText: "关闭",
    wide: true,
  });
}

// 「最近运行」点查看：运行中则继续轮询，否则直接展示结果表格
async function viewTestsetRun(runId) {
  let run;
  try {
    run = await runTestsetStatus(runId);
  } catch (err) {
    showRunStatus("warn", "该测试集运行记录已过期");
    void refreshTestsets();
    return;
  }
  if (run.status === "running") {
    state.activeRunId = runId;
    $("btn-abort-run").hidden = false;
    showRunStatus("warn", `测试集「${run.testset_name || ""}」运行中，继续跟踪…`);
    pollTestsetRun(runId);
  } else {
    showTestsetResults(run);
  }
}

async function abortTestsetRun(runId) {
  if (!runId) return;
  try {
    await abortTestsetRunApi(runId);
    showRunStatus("warn", "已请求取消：当前步骤完成即止，后续步骤不再发送");
  } catch (err) {
    showRunStatus("error", "取消失败: " + err.message);
  }
}

// 群发栏「执行测试」：目标即已打开的会话
function runTestsetFromBar() {
  const tsId = $("run-testset").value;
  if (!tsId) {
    showRunStatus("warn", "请先选择测试集");
    return;
  }
  if (!state.openIds.length) {
    showRunStatus("warn", "请先在左侧打开至少一个会话");
    return;
  }
  const ts = state.testsets.find((t) => t.id === tsId);
  if (!ts) return;
  if (!(ts.messages || []).length) {
    showRunStatus("warn", "该测试集没有消息，请先在测试集窗口中编辑");
    return;
  }
  runTestset(ts, state.openIds.slice());
}

// ---------- 在途消息（面板实时状态条） ----------

// 在途消息的状态文案（与后端 runner 的条目状态一一对应）
const PENDING_STATUS_TEXT = {
  submitted: "已入队",
  waiting_llm: "排队等待 LLM",
  llm: "LLM 生成中",
  done: "完成",
};

// 各会话最近一次成功刷新历史的时刻（epoch 毫秒）：完成的消息一旦刷入历史
// （气泡可见）即从在途条移除——条内只保留真正在途与完成后的短暂过渡
const historyRefreshedAt = new Map();

// 渲染单个面板的在途消息条：显示正在处理与排队中的消息及其当前阶段；
// 已完成且已刷入会话历史的消息不再展示（历史气泡即完成指示）。
// 返回是否发生变化（供轮询器决定是否重排对齐高度）
function renderPendingStrip(panel, entries) {
  const el = panel.querySelector(".panel-pending");
  if (!el) return false;
  const refreshedAt = historyRefreshedAt.get(panel.dataset.id) || 0;
  // status_at 为后端 epoch 秒，historyRefreshedAt 为 epoch 毫秒；条目
  // 完成于最近一次历史刷新之前 ⇒ 回复已在气泡中，无需再展示
  const visible = entries
    .filter((e) => e.status !== "done" || (e.status_at || 0) * 1000 > refreshedAt)
    .sort((a, b) => (a.created_at || 0) - (b.created_at || 0));
  const key = visible.map((e) => `${e.entry_id}:${e.status}`).join(",");
  if (key === el.dataset.pendingKey) return false;
  el.dataset.pendingKey = key;
  if (!visible.length) {
    el.hidden = true;
    el.innerHTML = "";
    return true;
  }
  el.hidden = false;
  el.innerHTML = visible
    .map((e) => {
      const text =
        e.text && e.text.length > 24 ? e.text.slice(0, 24) + "…" : e.text || "";
      const label = PENDING_STATUS_TEXT[e.status] || e.status;
      return (
        `<span class="pending-item pending-${escapeHtml(e.status)}">` +
        `<span class="pending-text" title="${escapeHtml(e.text)}">${escapeHtml(text)}</span>` +
        `<span class="pending-state">${escapeHtml(label)}</span>` +
        `</span>`
      );
    })
    .join("");
  return true;
}

// 全局轮询在途消息：一次查询，按会话分发到各面板（单发/群发/重新生成共用）
function pollPending() {
  setInterval(async () => {
    let pending = [];
    try {
      const data = await getPending();
      pending = Array.isArray(data.pending) ? data.pending : [];
    } catch (err) {
      return; // 查询失败下轮重试
    }
    let changed = false;
    for (const [id, el] of state.panelEls) {
      if (renderPendingStrip(el, pending.filter((e) => e.session_id === id))) {
        changed = true;
      }
    }
    if (changed && align.isAlignMode()) align.reflowAlign();
  }, 1000);
}

// 群发栏实时显示：当前打开的会话总数 + 按所属测试组的分布
function updateRunOverview() {
  const el = $("run-overview");
  if (!state.openIds.length) {
    el.hidden = true;
    return;
  }
  const counts = new Map();
  for (const id of state.openIds) {
    const v = effectiveView(id);
    if (!v) continue;
    const name = v.group_name || "未分组";
    counts.set(name, (counts.get(name) || 0) + 1);
  }
  let html = `<span class="overview-total">当前会话:${state.openIds.length}</span>`;
  for (const [name, n] of counts) {
    html += `<span class="overview-item">${escapeHtml(name)}:${n}</span>`;
  }
  el.hidden = false;
  el.innerHTML = html;
}

// ---------- 会话操作 ----------

function resetHistory(id) {
  showModal(`确定重置会话 ${id} 的对话历史吗？`, {
    danger: true,
    onOk: async () => {
      const resp = await resetSessions([id]);
      const panel = state.panelEls.get(id);
      if (panel) {
        clearPanelStatus(panel);
        void loadHistory(id);
      }
      showRunStatus("ok", `已重置 ${resp.reset} 个会话的对话历史`);
    },
  });
}

function deleteSession(id) {
  const v = effectiveView(id);
  showModal(`确定删除会话 ${v ? v.name : id} 吗？`, {
    danger: true,
    onOk: async () => {
      await deleteSessions([id]);
      state.openIds = state.openIds.filter((x) => x !== id);
      state.pinnedIds = state.pinnedIds.filter((x) => x !== id);
      renderPanels();
      await refreshGroups();
    },
  });
}

// ---------- 面板排序 ----------

function visibleOrder() {
  return [
    ...state.pinnedIds.filter((id) => state.openIds.includes(id)),
    ...state.openIds.filter((id) => !state.pinnedIds.includes(id)),
  ];
}

// 置顶是开关：置顶的面板固定在最前，再次点击取消置顶
function pin(id) {
  const i = state.pinnedIds.indexOf(id);
  if (i >= 0) state.pinnedIds.splice(i, 1);
  else state.pinnedIds.unshift(id);
  renderPanels();
}

function renderPanels() {
  const panelsEl = $("panels");
  // 置顶面板在最前，其余按打开顺序（appendChild 移动已有节点，保留聊天状态）
  for (const id of visibleOrder()) {
    const el = state.panelEls.get(id);
    if (el) panelsEl.appendChild(el);
  }
  for (const [id, el] of [...state.panelEls]) {
    if (!state.openIds.includes(id)) {
      el.remove();
      state.panelEls.delete(id);
    }
  }
  // 更新置顶按钮的开关视觉状态
  for (const [id, el] of state.panelEls) {
    const btn = el.querySelector('[data-action="pin"]');
    const isPinned = state.pinnedIds.includes(id);
    btn.classList.toggle("active", isPinned);
    btn.title = isPinned ? "取消置顶" : "置顶";
  }
  panelsEl.classList.toggle("single", state.openIds.length === 1);
  $("empty-hint").hidden = state.openIds.length > 0;
  $("align-bar").hidden = !align.isAlignMode() || state.openIds.length === 0;
  if (align.isAlignMode()) align.reflowAlign();
  updateRunOverview();
}

// 拖拽排序
let dragId = null;
const panelsEl = $("panels");

panelsEl.addEventListener("dragstart", (e) => {
  const panel = e.target.closest(".panel");
  if (!panel) return;
  dragId = panel.dataset.id;
  e.dataTransfer.effectAllowed = "move";
  panel.classList.add("dragging");
});

panelsEl.addEventListener("dragover", (e) => {
  const panel = e.target.closest(".panel");
  if (!panel || panel.dataset.id === dragId) return;
  e.preventDefault();
  e.dataTransfer.dropEffect = "move";
});

panelsEl.addEventListener("drop", (e) => {
  const target = e.target.closest(".panel");
  if (!target || !dragId || target.dataset.id === dragId) return;
  e.preventDefault();
  const order = visibleOrder();
  const from = order.indexOf(dragId);
  const to = order.indexOf(target.dataset.id);
  if (from < 0 || to < 0) return;
  order.splice(from, 1);
  order.splice(order.indexOf(target.dataset.id), 0, dragId);
  // 回写顺序，同时保持各面板的置顶状态
  const pinnedSet = new Set(state.pinnedIds);
  state.pinnedIds = order.filter((id) => pinnedSet.has(id));
  state.openIds = order;
  renderPanels();
});

panelsEl.addEventListener("dragend", () => {
  dragId = null;
  document.querySelectorAll(".panel.dragging").forEach((p) => p.classList.remove("dragging"));
});

// 气泡悬停操作：重新生成某轮（整体历史的编辑走面板头部「编辑」）
panelsEl.addEventListener("click", (e) => {
  const btn = e.target.closest('[data-action="regenerate"]');
  if (!btn) return;
  const msgEl = btn.closest(".msg");
  if (!msgEl) return;
  const index = parseInt(msgEl.dataset.index, 10);
  if (Number.isNaN(index)) return;
  void regenerateMsg(btn.closest(".panel").dataset.id, index);
});

// ---------- 选项加载 ----------

async function loadOptions() {
  try {
    const data = await listPlatforms();
    state.platforms = Array.isArray(data) ? data : [];
  } catch (err) {
    console.warn("加载平台列表失败:", err);
    state.platforms = [];
  }
  try {
    const data = await listConfs();
    state.confs = Array.isArray(data) ? data : [];
  } catch (err) {
    console.warn("加载配置档案失败:", err);
    state.confs = [];
  }
}

// ---------- 初始化 ----------

const align = createAlignController({
  getOpenIds: () => state.openIds,
  getPanelEls: () => state.panelEls,
  getHistoryCache: () => state.historyCache,
  getPanelsEl: () => panelsEl,
  renderChat,
});
align.attachEvents();

const chat = createChatRenderer(() => align);

const { refreshGroups, renderGroupList } = createGroupList({
  toggleOpen,
  openAll,
  deleteSession,
  renderPanels,
  showRunStatus,
  updateRunOverview,
});

const { refreshTestsets } = createTestsetList({
  showRunStatus,
  runTestset,
  viewTestsetRun,
  // 选中测试集 → 右侧自动切到「测试集」视图（showView 是函数声明，可提升）
  switchToTestsets: () => showView("testsets"),
});

// 静态控件绑定须放在 createGroupList / createTestsetList 解构之后：
// refreshGroups / refreshTestsets 是 const 解构绑定，提前引用会触发暂时性
// 死区（ReferenceError），模块求值即中止初始化
$("btn-refresh").addEventListener("click", refreshGroups);
$("btn-refresh-testsets").addEventListener("click", refreshTestsets);
$("btn-run-all").addEventListener("click", sendToAll);
$("btn-abort-run").addEventListener("click", () => abortTestsetRun(state.activeRunId));
$("btn-run-testset").addEventListener("click", runTestsetFromBar);
$("run-testset").addEventListener("change", () => {
  $("btn-run-testset").disabled = !$("run-testset").value;
});
$("run-text").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.isComposing) sendToAll();
});

// UI 窄条：视图切换（会话列表 / 测试集）。点击当前视图按钮折叠/展开侧栏，
// 点击其他视图按钮切换视图（展开侧栏并刷新对应视图数据）
let activeView = "sessions";

function showView(view) {
  activeView = view;
  document.body.classList.remove("sidebar-collapsed");
  document.querySelector(".groups-card").hidden = view !== "sessions";
  document.querySelector(".testsets-card").hidden = view !== "testsets";
  // 左侧选择驱动右侧视图：会话列表 ↔ 测试集编辑窗口自动切换
  document.querySelector(".sessions-view").hidden = view !== "sessions";
  document.querySelector(".testsets-view").hidden = view !== "testsets";
  document.querySelectorAll(".rail-btn").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.view === view);
  });
  if (view === "testsets") void refreshTestsets();
}

document.querySelectorAll(".rail-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    if (btn.dataset.view === activeView) {
      const collapsed = document.body.classList.toggle("sidebar-collapsed");
      btn.classList.toggle("active", !collapsed);
    } else {
      showView(btn.dataset.view);
    }
  });
});

await ready();
await Promise.all([loadOptions(), refreshGroups(), refreshTestsets()]);
pollPending();
