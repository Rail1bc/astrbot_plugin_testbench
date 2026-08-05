// chat.js — 会话面板聊天内容渲染（气泡 / 思维链 / 轮次分组与对齐渲染）
// 由 app.js 通过 createChatRenderer(alignGetter) 创建。alignGetter 返回轮次
// 对齐控制器，仅在渲染时调用——对齐控制器创建时又把 renderChat 注入其 env，
// 若在创建时直接传入 align 对象会形成互相创建的循环依赖。
// 与 align.js 的 createAlignController(env) 同模式：控制器持有视图状态，渲染
// 依赖的数据（会话/历史缓存等）由 app.js 通过 env/闭包注入。

export function createChatRenderer(alignGetter) {
  function isAlignMode() {
    const a = alignGetter();
    return Boolean(a && a.isAlignMode());
  }

  function reflowAlign() {
    const a = alignGetter();
    if (a) a.reflowAlign();
  }

  function renderChat(panel, conversations) {
    if (isAlignMode()) renderAligned(panel, conversations);
    else renderHistory(panel, conversations);
  }

  function renderHistory(panel, conversations) {
    const chat = panel.querySelector(".chat");
    chat.innerHTML = "";
    chat.classList.remove("aligned");
    let count = 0;
    let idx = 0;
    for (const conv of conversations) {
      for (const msg of conv.history || []) {
        count++;
        chat.appendChild(bubbleFor(msg, idx));
        idx++;
      }
    }
    if (!count) {
      const p = document.createElement("div");
      p.className = "empty";
      p.textContent = "暂无对话历史";
      chat.appendChild(p);
    }
    chat.scrollTop = chat.scrollHeight;
  }

  // 把消息历史按轮次分组：每个 user 发言开启新的一轮，期间的推理/工具调用/回复都属于该轮
  function groupTurns(history) {
    const turns = [];
    let cur = null;
    for (const msg of history || []) {
      if ((msg.role || "") === "user") {
        cur = { messages: [] };
        turns.push(cur);
      } else if (!cur) {
        cur = { messages: [] };
        turns.push(cur);
      }
      cur.messages.push(msg);
    }
    return turns;
  }

  // 轮次对齐模式：保留连续气泡流，每轮包一层 turn-wrap，高度由 reflowAlign 统一为各面板该轮的最大值
  function renderAligned(panel, conversations) {
    const chat = panel.querySelector(".chat");
    chat.innerHTML = "";
    chat.classList.add("aligned");
    let count = 0;
    let idx = 0;
    for (const conv of conversations) {
      for (const turn of groupTurns(conv.history)) {
        const wrap = document.createElement("div");
        wrap.className = "turn-wrap";
        for (const msg of turn.messages) {
          wrap.appendChild(bubbleFor(msg, idx));
          idx++;
        }
        chat.appendChild(wrap);
        count++;
      }
    }
    if (!count) {
      const p = document.createElement("div");
      p.className = "empty";
      p.textContent = "暂无对话历史";
      chat.appendChild(p);
    }
  }

  // 拆分消息内容：思维链（think 部件）与正文。AstrBot 当前把推理内容存为
  // assistant 消息内容里的 ThinkPart（{type: "think", think: "..."}）。
  function extractParts(content) {
    if (content == null) return { reasoning: "", text: "" };
    if (typeof content === "string") return { reasoning: "", text: content };
    if (Array.isArray(content)) {
      const reasoning = [];
      const texts = [];
      for (const p of content) {
        if (typeof p === "string") {
          texts.push(p);
        } else if (p && p.type === "think" && typeof p.think === "string") {
          reasoning.push(p.think);
        } else if (p && typeof p.text === "string") {
          texts.push(p.text);
        } else if (p && typeof p.content === "string") {
          texts.push(p.content);
        }
      }
      return { reasoning: reasoning.join("\n"), text: texts.join("\n") };
    }
    return { reasoning: "", text: "" };
  }

  // 思维链折叠块：默认收起，点击「展开思维链」展开；切换后重排轮次对齐高度
  function reasoningSection(text) {
    const details = document.createElement("details");
    details.className = "reasoning-wrap";
    const summary = document.createElement("summary");
    summary.textContent = "展开思维链";
    details.appendChild(summary);
    const body = document.createElement("div");
    body.className = "reasoning";
    body.textContent = text;
    details.appendChild(body);
    details.addEventListener("toggle", () => {
      summary.textContent = details.open ? "收起思维链" : "展开思维链";
      if (isAlignMode()) requestAnimationFrame(() => reflowAlign());
    });
    return details;
  }

  function bubbleFor(msg, index) {
    const role = msg.role || "";
    const { reasoning, text } = extractParts(msg.content);
    const el = document.createElement("div");
    el.dataset.index = String(index);
    if (role === "user") {
      el.className = "msg user";
      el.textContent = text || "（空消息）";
    } else if (role === "assistant_reasoning" || role === "reasoning") {
      // 旧格式：独立的推理角色消息，整条内容即思维链
      el.className = "msg bot";
      el.appendChild(reasoningSection(reasoning || text || "（推理过程）"));
    } else if (role === "tool") {
      el.className = "msg tool";
      el.textContent = text || "（工具调用）";
    } else if (role === "system") {
      el.className = "msg meta";
      el.textContent = text || "（系统消息）";
    } else {
      // assistant 等其余角色：正文 + 可折叠思维链
      el.className = "msg bot";
      if (reasoning) el.appendChild(reasoningSection(reasoning));
      if (!text && msg.tool_calls && msg.tool_calls.length) {
        el.appendChild(document.createTextNode("（调用工具…）"));
        el.classList.add("tool");
      } else if (text) {
        el.appendChild(document.createTextNode(text));
      } else if (!reasoning) {
        el.appendChild(document.createTextNode("…"));
      }
    }
    // 悬停操作：重新生成（仅 user 发言）；整体历史的编辑走面板头部的「历史」JSON 编辑器
    if (role === "user") {
      const actions = document.createElement("div");
      actions.className = "msg-actions";
      const regenBtn = document.createElement("button");
      regenBtn.type = "button";
      regenBtn.className = "icon-btn";
      regenBtn.dataset.action = "regenerate";
      regenBtn.textContent = "重新生成";
      actions.appendChild(regenBtn);
      el.appendChild(actions);
    }
    return el;
  }

  return { renderChat, renderHistory, renderAligned, bubbleFor };
}
