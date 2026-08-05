// align.js — 轮次对齐控制器
// 由 app.js 通过 createAlignController(env) 创建。env 提供视图依赖的访问器：
//   getOpenIds()       返回当前打开会话 id 数组（实时读取）
//   getPanelEls()      返回 会话id -> 面板元素 的 Map
//   getHistoryCache()  返回 会话id -> 对话历史 的 Map
//   getPanelsEl()      返回面板容器元素（用于监听滚动）
//   renderChat()       重新渲染面板聊天内容

export function createAlignController(env) {
  let alignMode = false;
  let alignTurn = 1;
  let alignCumulative = [0]; // alignCumulative[i] = 第 i+1 轮顶部的滚动偏移
  let alignScrollGuard = false; // 程序同步滚动时避免 scroll 事件回环
  let alignResizeTimer = 0;
  let alignSyncFrame = 0; // 合并同帧滚动同步的 rAF id
  let alignPendingTop = null; // { top, sourceId } 待同步的滚动位置（sourceId=null 同步全部面板）

  const $ = (id) => document.getElementById(id);

  // 重新测量所有打开面板的每轮内容高度，把每轮统一撑到各面板该轮的最大高度，
  // 使各面板总高度一致、轮次纵向对齐，再刷新滑动条
  function reflowAlign() {
    if (!alignMode) return;
    const turnList = [];
    for (const id of env.getOpenIds()) {
      const panel = env.getPanelEls().get(id);
      if (!panel) continue;
      const ws = [...panel.querySelectorAll(".turn-wrap")];
      for (const w of ws) w.style.height = ""; // 先还原自然高度再测量
      turnList.push(ws);
    }
    const maxTurns = Math.max(1, ...turnList.map((ws) => ws.length));
    const maxH = [];
    for (let i = 0; i < maxTurns; i++) {
      maxH[i] = Math.max(0, ...turnList.map((ws) => (ws[i] ? ws[i].scrollHeight : 0)));
    }
    for (const ws of turnList) {
      ws.forEach((w, i) => {
        w.style.height = maxH[i] + "px";
      });
    }
    alignCumulative = [0];
    for (let i = 0; i < maxTurns; i++) {
      alignCumulative.push(alignCumulative[i] + maxH[i]);
    }
    refreshAlign();
  }

  // 把同一帧内的多次滚动同步合并为一次 rAF 写操作：只对滚动位置与目标值
  // 有差异的面板写 scrollTop，避免每个 scroll 事件都强制 N-1 次布局计算
  function scheduleScrollSync(top, sourceId) {
    alignPendingTop = { top, sourceId };
    if (alignSyncFrame) return;
    alignSyncFrame = requestAnimationFrame(() => {
      alignSyncFrame = 0;
      const pending = alignPendingTop;
      alignPendingTop = null;
      if (!pending) return;
      alignScrollGuard = true;
      try {
        for (const [id, el] of env.getPanelEls()) {
          if (pending.sourceId !== null && id === pending.sourceId) continue;
          const chat = el.querySelector(".chat");
          if (!chat) continue;
          const max = chat.scrollHeight - chat.clientHeight;
          const target = Math.max(0, Math.min(max, pending.top));
          if (Math.abs(chat.scrollTop - target) > 0.5) {
            chat.scrollTop = target;
          }
        }
      } finally {
        alignScrollGuard = false;
      }
    });
  }

  // 把统一滑动条定位到指定轮次，同步所有面板的滚动位置
  function setTurn(t, { force = false } = {}) {
    if (!alignMode) return;
    const max = alignCumulative.length - 1;
    t = Math.max(1, Math.min(max, Math.round(t)));
    if (!force && t === alignTurn) return;
    alignTurn = t;
    const top = alignCumulative[t - 1] || 0;
    scheduleScrollSync(top, null);
    $("align-slider").value = String(t);
    $("align-turn-label").textContent = `轮次 ${t}/${max}`;
  }

  function refreshAlign() {
    if (!alignMode) return;
    $("align-slider").max = String(alignCumulative.length - 1);
    setTurn(alignTurn, { force: true });
  }

  function isAlignMode() {
    return alignMode;
  }

  function applyAlignMode() {
    alignMode = $("align-toggle").checked;
    $("panels").classList.toggle("align", alignMode);
    const openIds = env.getOpenIds();
    $("align-bar").hidden = !alignMode || openIds.length === 0;
    for (const id of openIds) {
      const panel = env.getPanelEls().get(id);
      if (!panel) continue;
      env.renderChat(panel, env.getHistoryCache().get(id) || []);
    }
    if (alignMode) {
      alignTurn = 1;
      reflowAlign();
    }
  }

  // 手动滚动任一面板时同步其余面板，并更新滑动条指示的当前轮次
  function onPanelsScroll(e) {
    if (!alignMode || alignScrollGuard) return;
    const chat = e.target;
    if (!(chat instanceof HTMLElement) || !chat.classList.contains("chat")) return;
    const panel = chat.closest(".panel");
    if (!panel) return;
    const top = chat.scrollTop;
    scheduleScrollSync(top, panel.dataset.id);
    let turn = 1;
    for (let i = alignCumulative.length - 2; i >= 0; i--) {
      if (alignCumulative[i] <= top + 1) {
        turn = i + 1;
        break;
      }
    }
    if (turn === alignTurn) return;
    alignTurn = turn;
    $("align-slider").value = String(turn);
    $("align-turn-label").textContent = `轮次 ${turn}/${alignCumulative.length - 1}`;
  }

  function attachEvents() {
    $("align-toggle").addEventListener("change", applyAlignMode);
    $("align-slider").addEventListener("input", () => {
      setTurn(parseInt($("align-slider").value, 10), { force: true });
    });
    // capture 捕获不冒泡的 scroll
    env.getPanelsEl().addEventListener("scroll", onPanelsScroll, true);
    // 窗口尺寸变化会改变换行高度，防抖后重新对齐
    window.addEventListener("resize", () => {
      if (!alignMode) return;
      clearTimeout(alignResizeTimer);
      alignResizeTimer = setTimeout(reflowAlign, 120);
    });
  }

  return {
    isAlignMode,
    reflowAlign,
    setTurn,
    attachEvents,
  };
}
