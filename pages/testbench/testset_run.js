// testset_run.js — 测试集运行编排视图：进度推进、结果表格、启动 / 取消 / 找回
// 由 app.js 通过 createTestsetRunController(env) 创建。env 注入视图动作
// （showRunStatus / loadHistory）与跨模块延迟引用（refreshTestsets、
// applySessionFeedback），依赖保持单向：本模块不 import app.js / events.js。
// 测试集运行由后端后台任务驱动（离开页面不中断），前端只负责启动；
// 进度经 /events 事件流推送（handleTestsetEvent），记录可经「最近运行」找回，
// 运行中/结束后均可点「查看」看结果表格。终态不自动弹窗，报告暂存
// state.runReports 供「查看报告」按需查看。
import {
  abortTestsetRun as abortTestsetRunApi,
  retryReviews,
  runTestset as runTestsetApi,
  runTestsetStatus,
} from "./api.js";
import { openModal } from "./modal.js";
import { state } from "./state.js";
import { effectiveView, escapeHtml, statusText } from "./utils.js";
import {
  ruleFailCount,
  ruleReviewFailCount,
  segmentLabel,
  segmentSummary,
} from "./pure.js";

const $ = (id) => document.getElementById(id);

// 暂存报告上限：超出丢弃最旧（防 runReports 无界增长——只增不减的 Map 会
// 随长时间使用持续堆积内存）
const MAX_STASHED_REPORTS = 20;

// ---------- 结果表格渲染（纯函数，报告视图也复用同一渲染路径） ----------
// 这些渲染辅助不依赖控制器状态（只读 run dict），提到模块级导出；报告数据是
// 运行终态快照，与 run 记录同构，buildResultsTable / renderFinalVerdicts
// 可直接渲染持久化报告。

// 运行级警告块（cron 任务可能向虚拟会话发送主动消息等）：无警告返回 null，
// 供结果表格 / 报告视图在表格上方呈现
function renderWarningsBlock(warnings) {
  if (!Array.isArray(warnings) || !warnings.length) return null;
  const wrap = document.createElement("div");
  wrap.className = "warn-block";
  const title = document.createElement("div");
  title.className = "warn-block-title";
  title.textContent = `⚠ ${warnings.length} 条主动消息警告：`;
  wrap.appendChild(title);
  for (const w of warnings) {
    const item = document.createElement("div");
    item.textContent = "· " + (w.message || w.kind || "");
    wrap.appendChild(item);
  }
  return wrap;
}

// verdict 的指标摘要（悬停查看：key=value 列表；无指标回退 detail）
function metricsSummary(v) {
  const ms = (v.metrics || [])
    .map((m) => `${m.key}=${m.value}`)
    .join(", ");
  return ms ? `指标: ${ms}` : v.detail || "";
}

// 单个 verdict 的标记：✓ 通过 / ✗ 未通过 / ⚠ 评审失败；点击打开评审详情弹窗。
// vkey 编码 verdict 在 run 数据里的位置（m:步骤:会话:规则序 或 f:规则:会话），
// 点击时经 resolveVerdict 从 run 定位——渲染纯函数不持有可变的 verdict 映射。
function verdictChip(v, vkey) {
  const title = metricsSummary(v);
  const cls = v.pass === true ? "assert-pass" : v.pass === false ? "assert-fail" : "assert-skip";
  const sym = v.pass === true ? "✓" : v.pass === false ? "✗" : "⚠";
  return `<button type="button" class="verdict-chip ${cls}" data-vkey="${vkey}" title="${escapeHtml(title)}">${sym}</button>`;
}

// 结果单元格的断言标记：verdicts 逐条渲染；旧格式回退 assertion ✓/✗
function verdictChips(r, vkeyPrefix) {
  if (Array.isArray(r.verdicts) && r.verdicts.length) {
    return r.verdicts
      .map((v, vi) => verdictChip(v, `${vkeyPrefix}${vi}`))
      .join("");
  }
  if (r.assertion) {
    return `<span class="${r.assertion.pass ? "assert-pass" : "assert-fail"}">${r.assertion.pass ? "✓" : "✗"}</span>`;
  }
  return "";
}

// 按 vkey 从 run 数据定位 verdict（消息级 m:步骤:会话:规则序 / 跨轮级 f:规则:会话）
function resolveVerdict(run, vkey) {
  if (!vkey) return null;
  const [kind, a, b, c] = vkey.split(":");
  if (kind === "m") {
    const step = (run.steps || [])[Number(a)];
    const result = ((step && step.results) || []).find((x) => x.session_id === b);
    return (result && result.verdicts && result.verdicts[Number(c)]) || null;
  }
  if (kind === "f") {
    const fr = (run.final_verdicts || [])[Number(a)];
    const entry = ((fr && fr.results) || []).find((x) => x.session_id === b);
    return (entry && entry.verdict) || null;
  }
  return null;
}

// vkey 转后端定位器：消息级 m:步骤:会话:verdict序 → {kind:"m", step, session_id, verdict}，
// 跨轮级 f:规则:会话 → {kind:"f", rule, session_id}（与后端 _iter_verdict_locators 对齐）
function vkeyToTarget(vkey) {
  const [kind, a, b, c] = vkey.split(":");
  if (kind === "m") {
    return { kind: "m", step: Number(a), session_id: b, verdict: Number(c) };
  }
  return { kind: "f", rule: Number(a), session_id: b };
}

// verdict 详情内容（可重渲染）：状态 / 失败原因 / 指标表 / 评审输入 / 评审输出
// （LLM 原始返回）。retryCtx（{reportId, showStatus, onRetried}）存在且 verdict
// 带 profile_id（LLM 评审）时追加「重试该评审」按钮——重试成功后用返回的新
// verdict 重建弹窗内容，并回调 onRetried 刷新报告视图
function buildVerdictDetailContent(v, vkey, retryCtx) {
  const content = document.createElement("div");
  content.className = "form-col";
  content.style.gap = "8px";
  const statusLine = document.createElement("p");
  statusLine.style.margin = "0";
  statusLine.textContent =
    `规则 ${(v.rule_index ?? 0) + 1}：` +
    (v.pass === true ? "✓ 通过" : v.pass === false ? "✗ 未通过" : "⚠ 评审失败");
  content.appendChild(statusLine);
  if (v.detail) {
    const detail = document.createElement("p");
    detail.className = "cell-err";
    detail.style.margin = "0";
    detail.textContent = v.detail;
    content.appendChild(detail);
  }
  if (v.metrics && v.metrics.length) {
    const table = document.createElement("table");
    table.className = "testset-results";
    table.innerHTML =
      `<thead><tr><th>指标</th><th>类型</th><th>值</th></tr></thead><tbody>` +
      v.metrics
        .map(
          (m) =>
            `<tr><td>${escapeHtml(m.key)}</td><td>${escapeHtml(m.type)}</td><td>${escapeHtml(m.value)}</td></tr>`,
        )
        .join("") +
      `</tbody>`;
    content.appendChild(table);
  }
  if (v.context_text) {
    content.appendChild(verdictDetailSection("评审输入（喂给评审 LLM 的上下文）", v.context_text));
  }
  if (v.raw) {
    content.appendChild(verdictDetailSection("评审输出（LLM 原始返回）", v.raw));
  } else if (v.status === "error") {
    content.appendChild(verdictDetailSection("评审输出（LLM 原始返回）", "（无输出：评审调用失败）"));
  }
  if (retryCtx && retryCtx.reportId && v.profile_id) {
    const retryBtn = document.createElement("button");
    retryBtn.className = "btn small";
    retryBtn.textContent = "重试该评审";
    retryBtn.addEventListener("click", () => {
      retryBtn.disabled = true;
      retryBtn.textContent = "重试中…";
      void retryReviews(retryCtx.reportId, { targets: [vkeyToTarget(vkey)] })
        .then((resp) => {
          // 用返回的新 verdict 重建弹窗内容（resolveVerdict 按 vkey 从更新后的报告数据定位）
          const nv = resolveVerdict(resp.report, vkey);
          openModal({
            title: "评审详情",
            content: buildVerdictDetailContent(nv || v, vkey, retryCtx),
            okText: "关闭",
            wide: true,
          });
          if (retryCtx.onRetried) retryCtx.onRetried(resp);
        })
        .catch((err) => {
          retryBtn.disabled = false;
          retryBtn.textContent = "重试该评审";
          if (retryCtx.showStatus) retryCtx.showStatus("error", "重试失败: " + err.message);
        });
    });
    content.appendChild(retryBtn);
  }
  return content;
}

// verdict 详情弹窗（retryCtx 供报告视图传入单条重试能力；运行结果弹窗无报告不传）
function openVerdictDetail(v, vkey, retryCtx) {
  openModal({
    title: "评审详情",
    content: buildVerdictDetailContent(v, vkey, retryCtx),
    okText: "关闭",
    wide: true,
  });
}

function verdictDetailSection(label, text) {
  const wrap = document.createElement("div");
  const h = document.createElement("div");
  h.className = "verdict-detail-section";
  h.textContent = label;
  const pre = document.createElement("pre");
  pre.className = "verdict-detail-pre";
  pre.textContent = text;
  wrap.append(h, pre);
  return wrap;
}

// 最终断言（跨轮）结果表：行=最终规则（范围），列=会话，单元格=verdict。
// 无最终断言（final_verdicts 为空）返回 null，主表格下方不渲染。
// retryCtx 可选（报告视图传入，供 verdict 详情弹窗提供单条评审重试）
export function renderFinalVerdicts(run, retryCtx) {
  const fv = run.final_verdicts || [];
  if (!fv.length) return null;
  const sessions = run.sessions || [];
  const table = document.createElement("table");
  table.className = "testset-results final-verdicts";
  const head = document.createElement("thead");
  head.innerHTML =
    `<tr><th>最终断言（跨轮）</th>` +
    sessions.map((s) => `<th class="cell-session">${escapeHtml(s.name || s.id)}</th>`).join("") +
    `<th>结果</th></tr>`;
  const body = document.createElement("tbody");
  body.innerHTML = fv
    .map((fr, fi) => {
      const scopeText =
        fr.scope && fr.scope.from != null
          ? `第 ${fr.scope.from + 1}–${fr.scope.to + 1} 步`
          : "全部步骤";
      const cells = sessions
        .map((s) => {
          const res = (fr.results || []).find((x) => x.session_id === s.id);
          if (!res || !res.verdict) return `<td class="cell-session">—</td>`;
          return `<td class="cell-session">${verdictChip(res.verdict, `f:${fi}:${s.id}`)}</td>`;
        })
        .join("");
      const fail = (fr.results || []).filter(
        (r) => r.verdict && r.verdict.pass === false,
      ).length;
      const reviewFail = (fr.results || []).filter(
        (r) =>
          r.verdict &&
          (r.verdict.status === "error" || r.verdict.status === "invalid"),
      ).length;
      return (
        `<tr>` +
        `<td class="cell-step">规则 ${fr.rule_index + 1}（${scopeText}）</td>` +
        cells +
        `<td>${fail ? `断言 ✗ ${fail}` : "通过"}${reviewFail ? ` / 评审失败 ${reviewFail}` : ""}</td>` +
        `</tr>`
      );
    })
    .join("");
  table.append(head, body);
  table.addEventListener("click", (e) => {
    const btn = e.target.closest(".verdict-chip");
    if (btn) {
      const v = resolveVerdict(run, btn.dataset.vkey);
      if (v) openVerdictDetail(v, btn.dataset.vkey, retryCtx);
    }
  });
  return table;
}

// 主结果表格（行=步骤、列=会话，行尾统计成功/无回复/错误 + 断言 ✗ + 评审失败）：
// 运行结果弹窗与报告详情共用。retryCtx 可选（报告视图传入，供 verdict 详情
// 弹窗提供单条评审重试）
export function buildResultsTable(run, retryCtx) {
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
          return (
            `<td class="cell-session">` +
            escapeHtml(statusText(r.status)) + dur + verdictChips(r, `m:${i}:${s.id}:`) +
            `</td>`
          );
        })
        .join("");
      const ok = (step.results || []).filter((r) => r.status === "ok").length;
      const noReply = (step.results || []).filter((r) => r.status === "no_reply").length;
      const err = (step.results || []).filter((r) => r.status === "error").length;
      const assertFail = (step.results || []).reduce(
        (n, r) => n + ruleFailCount(r),
        0,
      );
      const reviewFail = (step.results || []).reduce(
        (n, r) => n + ruleReviewFailCount(r),
        0,
      );
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
        `<td>成功 ${ok} / 无回复 ${noReply} / 错误 ${err}${assertFail ? ` / 断言 ✗ ${assertFail}` : ""}${reviewFail ? ` / 评审失败 ${reviewFail}` : ""}</td>` +
        `</tr>`
      );
    })
    .join("");
  table.append(head, body);
  table.addEventListener("click", (e) => {
    const btn = e.target.closest(".verdict-chip");
    if (btn) {
      const v = resolveVerdict(run, btn.dataset.vkey);
      if (v) openVerdictDetail(v, btn.dataset.vkey, retryCtx);
    }
  });
  return table;
}

export function createTestsetRunController(env) {
  // 逐会话反馈回调 / 测试集列表刷新：由 app.js 装配，延迟取以避开循环依赖
  let getApplySessionFeedback = () => null;
  let getRefreshTestsets = () => null;

  function setApplySessionFeedback(getter) {
    getApplySessionFeedback = getter;
  }

  function setRefreshTestsets(getter) {
    getRefreshTestsets = getter;
  }

  // 批量段启动文案 / 进度文案（segmentSummary / segmentLabel 在 pure.js）

  // 测试集运行进度：由 /events 的 testset 事件（完整 run 快照）驱动。
  // 新完成的步骤经 applySessionFeedback 逐会话反馈（与手动群发同路径）；
  // 终态不自动弹窗，报告暂存 state.runReports 供「查看报告」按需查看。
  function handleTestsetEvent(runId, run) {
    // 单槽守卫：已在跟踪另一运行（或事件流里残留的旧运行快照）时忽略——
    // 后端 has_active_run 已禁止并发运行，但事件队列里的过期快照仍可能串场
    // （例如页面离线期间 B 运行结束、上线后又跑起了 A），不能互相污染。
    if (state.activeRunId && state.activeRunId !== runId) return;
    const name = run.testset_name || "测试集";
    if (run.status === "running") {
      state.activeRunId = runId;
      $("btn-abort-run").hidden = false;
      const idx = Math.max(0, run.current_step);
      const stepText = run.steps[idx] ? run.steps[idx].text : "";
      env.showRunStatus(
        "warn",
        `测试集「${name}」运行中：${segmentLabel(run, idx)} — ${stepText}`,
      );
      // 新完成的步骤 → 逐会话反馈（面板耗时 + 逐会话历史刷新，与手动群发一致）。
      // 去重键带 runId 前缀：不同运行的同一序号步骤互不污染（同一集合被并发/
      // 历史残留的运行共用时不会误跳过新运行的完成步骤）
      (run.steps || []).forEach((step, i) => {
        const key = `${runId}:${i}`;
        if (step.status === "done" && !state.testsetReportedSteps.has(key)) {
          state.testsetReportedSteps.add(key);
          for (const r of step.results || []) getApplySessionFeedback()(r);
        }
      });
      return;
    }
    // 终态：不自动弹窗，报告暂存供按需查看
    state.activeRunId = null;
    state.testsetReportedSteps.clear();
    $("btn-abort-run").hidden = true;
    state.runReports[runId] = run;
    state.latestReportRunId = runId;
    // 报告无界增长防护：只保留最近 MAX_STASHED_REPORTS 份，超出丢弃最旧
    const keys = Object.keys(state.runReports);
    while (keys.length > MAX_STASHED_REPORTS) delete state.runReports[keys.shift()];
    $("btn-view-report").hidden = false;
    const failSteps = run.steps.filter((s) => s.status === "error").length;
    // 断言未通过（✗）与步骤/会话错误是两回事：断言失败只落在结果单元格，
    // 不改变会话 status——总结必须单独计数，否则出现「表格 3 个 ✗ 但总结错误 0」的误导。
    // LLM 规则评审失败（error/invalid，pass 为 null）单独计数，不等同于不通过
    const assertFails = (run.steps || []).reduce(
      (n, s) => n + (s.results || []).reduce((m, r) => m + ruleFailCount(r), 0),
      0,
    );
    const reviewFails = (run.steps || []).reduce(
      (n, s) => n + (s.results || []).reduce((m, r) => m + ruleReviewFailCount(r), 0),
      0,
    );
    const doneCount = run.steps.filter((s) => s.status === "done").length;
    const assertText = assertFails ? `，${assertFails} 条断言未通过` : "";
    const reviewText = reviewFails ? `，${reviewFails} 条评审失败` : "";
    const warnCount = Array.isArray(run.warnings) ? run.warnings.length : 0;
    const warnText = warnCount
      ? `，⚠ ${warnCount} 条主动消息警告（定时任务可能向虚拟会话发送消息，详见结果表格）`
      : "";
    const summary =
      run.status === "done"
        ? `测试集「${name}」运行完成（${run.steps.length} 步${assertText}${reviewText}${warnText}）`
        : run.status === "cancelled"
          ? `测试集「${name}」已取消：当前步骤已完成，共完成 ${doneCount} 步`
          : `测试集「${name}」运行出错${failSteps ? `（${failSteps} 步失败）` : ""}${assertText}${reviewText}${warnText}`;
    env.showRunStatus(
      run.status === "done" ? (warnCount ? "warn" : "ok") : "error",
      summary,
    );
    for (const sid of state.openIds) void env.loadHistory(sid);
    void getRefreshTestsets()();
  }

  // 结果表格弹窗：行=步骤（文本 + 失败原因），列=会话（状态 + 耗时 + 断言
  // ✓/✗/⚠），行尾为该步 ok/no_reply/error + 断言 ✗ + 评审失败计数；
  // 测试集有最终断言时在主表下方追加跨轮结果表。表格构建复用模块级的
  // buildResultsTable / renderFinalVerdicts（报告详情弹窗同路径）
  function showTestsetResults(run) {
    const content = document.createElement("div");
    content.className = "form-col";
    content.style.gap = "8px";
    const warnBlock = renderWarningsBlock(run.warnings);
    if (warnBlock) content.appendChild(warnBlock);
    content.appendChild(buildResultsTable(run));
    const finalTable = renderFinalVerdicts(run);
    if (finalTable) content.appendChild(finalTable);
    openModal({
      title: `测试集结果 · ${run.testset_name || ""}`,
      content,
      okText: "返回",
      wide: true,
    });
  }

  async function runTestset(testset, ids) {
    try {
      const resp = await runTestsetApi({ testset_id: testset.id, sessions: ids });
      state.activeRunId = resp.run_id;
      state.testsetReportedSteps.clear();
      $("btn-abort-run").hidden = false;
      const segText = segmentSummary(testset);
      env.showRunStatus(
        "warn",
        `测试集「${testset.name}」已启动（${resp.steps} 步${segText}），后台运行中…`,
      );
    } catch (err) {
      env.showRunStatus("error", "启动测试集运行失败: " + err.message);
    }
  }

  // 「最近运行」点查看：运行中则一次性重建进度（后续由事件流推送），否则直接展示结果表格
  async function viewTestsetRun(runId) {
    let run;
    try {
      run = await runTestsetStatus(runId);
    } catch (err) {
      env.showRunStatus("warn", "该测试集运行记录已过期");
      void getRefreshTestsets()();
      return;
    }
    if (run.status === "running") {
      state.activeRunId = runId;
      state.testsetReportedSteps.clear();
      handleTestsetEvent(runId, run);
    } else {
      showTestsetResults(run);
    }
  }

  async function abortTestsetRun(runId) {
    if (!runId) return;
    try {
      await abortTestsetRunApi(runId);
      env.showRunStatus("warn", "已请求取消：当前步骤完成即止，后续步骤不再发送");
    } catch (err) {
      env.showRunStatus("error", "取消失败: " + err.message);
    }
  }

  // 群发栏「执行测试」：目标即已打开的会话
  function runTestsetFromBar() {
    const tsId = $("run-testset").value;
    if (!tsId) {
      env.showRunStatus("warn", "请先选择测试集");
      return;
    }
    if (!state.openIds.length) {
      env.showRunStatus("warn", "请先在左侧打开至少一个会话");
      return;
    }
    const ts = state.testsets.find((t) => t.id === tsId);
    if (!ts) return;
    if (!(ts.messages || []).length) {
      env.showRunStatus("warn", "该测试集没有消息，请先在测试集窗口中编辑");
      return;
    }
    runTestset(ts, state.openIds.slice());
  }

  // 群发栏第 1 块实时显示：当前打开的会话总数 + 按所属测试组的分布（每行一条）
  function updateRunOverview() {
    const countEl = $("run-overview-count");
    const el = $("run-overview");
    countEl.textContent = `当前会话:${state.openIds.length}`;
    const counts = new Map();
    for (const id of state.openIds) {
      const v = effectiveView(id);
      if (!v) continue;
      const name = v.group_name || "未分组";
      counts.set(name, (counts.get(name) || 0) + 1);
    }
    const items = [...counts.entries()].map(
      ([name, n]) => `<div class="overview-item">${escapeHtml(name)}:${n}</div>`,
    );
    el.innerHTML = items.join("");
    el.hidden = items.length === 0;
  }

  return {
    handleTestsetEvent,
    runTestset,
    viewTestsetRun,
    abortTestsetRun,
    runTestsetFromBar,
    updateRunOverview,
    showTestsetResults,
    setApplySessionFeedback,
    setRefreshTestsets,
  };
}
