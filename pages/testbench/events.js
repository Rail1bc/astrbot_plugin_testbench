// events.js — 事件驱动反馈层：SSE 订阅、逐会话反馈、在途消息条与断线快照对账
// 由 app.js 通过 createEventController(env) 创建。env 注入本模块依赖的视图动作
// （面板状态 / 历史刷新）与跨模块延迟引用（align 控制器、测试集事件转发目标），
// 使模块间依赖保持单向：本模块不 import app.js / testset_run.js。
// 测试集事件经 setTestsetEvent 转交给 testset_run 模块处理（app.js 装配），
// 避免 events ↔ testset_run 互相 import 的循环依赖。
import {
  getPending,
  listTestsetRuns,
  runStatus,
  runTestsetStatus,
  subscribeEvents,
} from "./api.js";
import { state } from "./state.js";
import { escapeHtml, statusText } from "./utils.js";

// 在途消息的状态文案（与后端 runner 的条目状态一一对应）
const PENDING_STATUS_TEXT = {
  submitted: "已入队",
  waiting_llm: "排队等待 LLM",
  llm: "LLM 生成中",
  done: "完成",
};

export function createEventController(env) {
  // 手动群发 / 单发的消费者注册表：test_id -> {onSession, onAll, seen, finished}。
  // /events 的 session_done / test_done 事件经 handleEvent 分发到这里（替代旧的
  // pollRun 轮询回调）；seen 去重保证每个会话的结果只反馈一次（断线对账重放不重复）。
  const testConsumers = new Map();

  // 测试集事件转发目标：由 app.js 装配（testset_run 模块），延迟取以避开循环依赖
  let getTestsetEvent = () => null;

  function setTestsetEvent(getter) {
    getTestsetEvent = getter;
  }

  function registerTestConsumer(testId, onSession, onAll) {
    testConsumers.set(testId, { onSession, onAll, seen: new Set(), finished: false });
  }

  // 面板异步补发警告行：pipeline 结束后仍有回复到达（检测不是捕获，内容未计入结果）
  function renderPanelWarning(panel, warning) {
    let el = panel.querySelector(".panel-warning");
    if (!warning) {
      if (el) el.remove();
      return;
    }
    if (!el) {
      el = document.createElement("div");
      el.className = "panel-warning";
      const status = panel.querySelector(".panel-status");
      if (status) {
        status.after(el);
      } else {
        panel.querySelector(".panel-body").appendChild(el);
      }
    }
    el.textContent = "⚠ " + warning;
  }

  // 统一的逐会话反馈：手动群发与测试集运行共用（面板显示回复耗时 + 逐会话历史刷新）
  function applySessionFeedback(s) {
    const panel = state.panelEls.get(s.session_id);
    if (panel) {
      if (s.status === "ok") {
        env.panelStatus(panel, "ok", `回复成功（${s.duration}s）`);
      } else {
        env.panelStatus(
          panel,
          s.status === "error" ? "error" : "warn",
          statusText(s.status) + (s.error ? `：${s.error}` : ""),
        );
      }
      renderPanelWarning(panel, s.warning);
    }
    void env.loadHistory(s.session_id);
  }

  // 渲染单个面板的在途消息条：显示正在处理与排队中的消息及其当前阶段；
  // 已完成且已刷入会话历史的消息不再展示（历史气泡即完成指示）。
  // 返回是否发生变化（供调用方决定是否重排对齐高度）
  function renderPendingStrip(panel, entries) {
    const el = panel.querySelector(".panel-pending");
    if (!el) return false;
    const refreshedAt = state.historyRefreshedAt.get(panel.dataset.id) || 0;
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

  // 按会话重建各面板在途条（pending 事件是全量快照，一次替换再逐面板渲染）
  function renderAllPendingStrips() {
    // 先按会话分桶（O(N)），再逐面板取本会话的条目（O(P)），避免逐面板全量过滤
    const bySession = new Map();
    for (const e of state.pendingEntries.values()) {
      const list = bySession.get(e.session_id);
      if (list) list.push(e);
      else bySession.set(e.session_id, [e]);
    }
    let changed = false;
    for (const [id, el] of state.panelEls) {
      if (renderPendingStrip(el, bySession.get(id) || [])) changed = true;
    }
    const align = env.getAlign();
    if (changed && align.isAlignMode()) align.reflowAlign();
  }

  // SSE 事件分发：pending（在途快照）/ session_done / test_done / testset
  function handleEvent(ev) {
    if (ev.type === "pending") {
      state.pendingEntries.clear();
      for (const e of ev.entries || []) state.pendingEntries.set(e.entry_id, e);
      renderAllPendingStrips();
      return;
    }
    if (ev.type === "session_done") {
      const cons = testConsumers.get(ev.test_id);
      if (cons && !cons.seen.has(ev.session_id)) {
        cons.seen.add(ev.session_id);
        try {
          cons.onSession(ev.summary);
        } catch (err) {
          console.error("会话结果刷新失败:", err);
        }
      }
      return;
    }
    if (ev.type === "test_done") {
      const cons = testConsumers.get(ev.test_id);
      if (cons) {
        testConsumers.delete(ev.test_id);
        cons.onAll(ev.record);
      }
      return;
    }
    if (ev.type === "testset") {
      getTestsetEvent()(ev.run_id, ev.run);
    }
  }

  // 订阅 /events 并做一次快照对账：订阅建立后（流已 open）用一次性接口取回
  // 当前权威状态，补上订阅前/断线期间丢失的事件（事件均为全量快照，幂等覆盖）
  async function connectEvents() {
    try {
      await subscribeEvents(handleEvent, () => {
        // 断线：延迟重连；丢的事件由 reconcileEvents 的一次性取回兜底
        setTimeout(() => {
          void connectEvents().catch((err) => console.error("重连事件流失败:", err));
        }, 3000);
      });
      void reconcileEvents();
    } catch (err) {
      // 订阅建立即失败（而非连接中断后 onError 回调触发）：同样延迟重试，
      // 避免事件层本会话报废（subscribeEvents 失败时 eventSub 保持 null，可重新订阅）
      console.error("事件流订阅失败:", err);
      setTimeout(() => {
        void connectEvents().catch((e) => console.error("重连事件流失败:", e));
      }, 3000);
    }
  }

  async function reconcileEvents() {
    // 1. 在途条目快照（单发/群发/重新生成/测试集共用）
    let entries = [];
    try {
      const data = await getPending();
      entries = Array.isArray(data.pending) ? data.pending : [];
    } catch (err) {
      console.warn("拉取在途条目失败:", err);
    }
    state.pendingEntries.clear();
    for (const e of entries) state.pendingEntries.set(e.entry_id, e);
    renderAllPendingStrips();
    // 2. 在途条目带 test_id：逐测试一次性取回结果，喂给已注册消费者（seen 去重）
    const testIds = new Set(entries.map((e) => e.test_id).filter(Boolean));
    for (const tid of testIds) {
      const cons = testConsumers.get(tid);
      if (!cons) continue;
      let rec;
      try {
        rec = await runStatus(tid);
      } catch (err) {
        // 记录已被清理（404）：consumer 永远等不到 test_done，须释放防 Map 泄漏
        testConsumers.delete(tid);
        continue;
      }
      for (const r of rec.results || []) {
        if (cons.seen.has(r.session_id)) continue;
        cons.seen.add(r.session_id);
        try {
          cons.onSession(r);
        } catch (err) {
          console.error("会话结果刷新失败:", err);
        }
      }
      if (rec.done && !cons.finished) {
        cons.finished = true;
        testConsumers.delete(tid);
        cons.onAll(rec);
      }
    }
    // 2.5. 已注册但不在在途条里的消费者（断线期间已完成并剪枝 / 完成即被移除）：
    // 逐个 runStatus 收尾，404（运行记录已被清理）→ 释放防 Map 泄漏。
    for (const tid of testConsumers.keys()) {
      if (testIds.has(tid)) continue;
      const cons = testConsumers.get(tid);
      if (!cons) continue;
      let rec;
      try {
        rec = await runStatus(tid);
      } catch (err) {
        testConsumers.delete(tid);
        continue;
      }
      for (const r of rec.results || []) {
        if (cons.seen.has(r.session_id)) continue;
        cons.seen.add(r.session_id);
        try {
          cons.onSession(r);
        } catch (err) {
          console.error("会话结果刷新失败:", err);
        }
      }
      if (rec.done && !cons.finished) {
        cons.finished = true;
        testConsumers.delete(tid);
        cons.onAll(rec);
      }
    }
    // 3. 测试集运行进度（页面重开找回 / 断线期间推进的步骤）
    let activeRunId = state.activeRunId;
    if (!activeRunId) {
      // 单槽状态为空但后端可能有运行中的测试集（页面重开 / 另一 tab 启动）：
      // 从最近运行里找回并接管，否则后台任务在跑、前端却无任何进度。
      try {
        const runs = await listTestsetRuns();
        // 后端返回 {runs: [...]}（与报告视图一致解包，勿把整个对象当数组）
        const running = ((runs || {}).runs || []).find((r) => r.status === "running");
        if (running) activeRunId = running.run_id;
      } catch (err) {
        console.warn("拉取最近运行失败:", err);
      }
    }
    if (activeRunId) {
      try {
        const run = await runTestsetStatus(activeRunId);
        getTestsetEvent()(run.run_id, run);
      } catch (err) {
        console.warn("拉取测试集运行进度失败:", err);
        // 该运行已不存在（被清理/中止后剪枝）：清掉单槽状态，避免残留
        if (state.activeRunId === activeRunId) state.activeRunId = null;
      }
    }
  }

  return { registerTestConsumer, applySessionFeedback, connectEvents, setTestsetEvent };
}
