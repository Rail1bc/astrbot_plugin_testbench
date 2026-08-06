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
  runTestset as runTestsetApi,
  runTestsetStatus,
} from "./api.js";
import { openModal } from "./modal.js";
import { state } from "./state.js";
import { effectiveView, escapeHtml, statusText } from "./utils.js";

const $ = (id) => document.getElementById(id);

// 暂存报告上限：超出丢弃最旧（防 runReports 无界增长——只增不减的 Map 会
// 随长时间使用持续堆积内存）
const MAX_STASHED_REPORTS = 20;

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
    env.showRunStatus(run.status === "done" ? "ok" : "error", summary);
    for (const sid of state.openIds) void env.loadHistory(sid);
    void getRefreshTestsets()();
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
