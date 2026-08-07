// testset_reports.js — 测试集编辑窗口的报告视图（最近运行 + 持久化报告）
// 从 testset_editor.js 拆出（Phase 4）：createReportView(deps) 工厂接收
// createTestsetEditor 闭包依赖（currentSelected / getDeps / showRunStatus），
// 返回视图切换（edit/report）与报告页渲染；报告卡片 / 指标总览 / 详情弹窗 /
// 导出 / 删除 / 批量重试全部在本模块。结果表格复用 testset_run.js 的
// buildResultsTable / renderFinalVerdicts（testset_run.js 不 import 本模块，
// 无环）。getDeps 经闭包包装读取编辑器最新绑定（setDeps 可重赋值），避免
// 持有陈旧引用。
import {
  deleteReports,
  listReports,
  listTestsetRuns,
  retryReviews,
} from "./api.js";
import { openModal, showModal } from "./modal.js";
import { escapeHtml } from "./utils.js";
import { buildResultsTable, renderFinalVerdicts } from "./testset_run.js";

const $ = (id) => document.getElementById(id);

// 测试集运行 / 报告状态文案（最近运行条目与报告条目共用）
const RUN_STATUS_TEXT = {
  running: "运行中",
  done: "完成",
  error: "错误",
  cancelled: "已取消",
};

// 报告可重试的 LLM 评审统计：{failed, total}——只数带 profile_id 的 verdict
// （机械 verdict 不走 LLM，无重试意义）；failed 为其中 status error/invalid 的。
// 报告卡片据此决定「重试失败 / 重试全部」按钮的显隐与禁用
export function retryableReportStats(data) {
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

// 报告视图工厂：deps = { currentSelected, getDeps, showRunStatus }。viewMode
// 状态（edit / report）本模块持有，编辑器经返回的 isReportMode / toggleViewMode
// 访问；报告体刷新（renderReportView）在切到报告模式时调用。
export function createReportView(deps) {
  const { currentSelected, getDeps, showRunStatus } = deps;

  // 编辑窗口当前视图：「edit」编辑消息 /「report」报告页（最近运行 + 持久化报告）
  let viewMode = "edit";
  // 报告视图渲染序号：两个 await 间切换测试集 / 视图会渲染错对象，乱序响应丢弃
  let reportSeq = 0;

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
    const seq = ++reportSeq;
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
      if (seq !== reportSeq) return; // 期间已切换渲染目标，丢弃本次迟到响应
      runs = Array.isArray(data.runs) ? data.runs : [];
    } catch (err) {
      runsFailed = true;
    }
    try {
      const data = await listReports(ts.id);
      if (seq !== reportSeq) return;
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

  return {
    toggleViewMode,
    syncViewModeUI,
    renderReportView,
    isReportMode: () => viewMode === "report",
  };
}
