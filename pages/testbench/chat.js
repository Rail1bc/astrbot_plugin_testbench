// chat.js — 会话面板聊天内容渲染（气泡 / 思维链 / 工具调用 / 轮次分组与对齐渲染）
// 由 app.js 通过 createChatRenderer(alignGetter) 创建。alignGetter 返回轮次
// 对齐控制器，仅在渲染时调用——对齐控制器创建时又把 renderChat 注入其 env，
// 若在创建时直接传入 align 对象会形成互相创建的循环依赖。
// 与 align.js 的 createAlignController(env) 同模式：控制器持有视图状态，渲染
// 依赖的数据（会话/历史缓存等）由 app.js 通过 env/闭包注入。
// 历史消息为 OpenAI 格式 dict：助手消息经 msg.tool_calls（{id, function:
// {name, arguments}} 数组）携带工具调用（content 部件只有 text/think/image_url/
// audio_url，工具调用不作为 content 部件），工具返回为 role:"tool" 消息并以
// tool_call_id 关联调用。渲染时以 ctx.toolNames 收集 id → 工具名，使工具返回
// 气泡能标注「哪个工具的返回」。
import { statusText } from "./utils.js";

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
    const ctx = { toolNames: {} };
    for (const conv of conversations) {
      let idx = 0;
      for (const msg of conv.history || []) {
        count++;
        chat.appendChild(bubbleFor(msg, idx, conv.cid, ctx));
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
    const ctx = { toolNames: {} };
    for (const conv of conversations) {
      let idx = 0;
      for (const turn of groupTurns(conv.history)) {
        const wrap = document.createElement("div");
        wrap.className = "turn-wrap";
        for (const msg of turn.messages) {
          wrap.appendChild(bubbleFor(msg, idx, conv.cid, ctx));
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

  // 工具调用参数美化：JSON 字符串解析后按 2 空格缩进输出，非 JSON / 对象原样处理
  function prettyArgs(raw) {
    if (raw == null || raw === "") return "（无参数）";
    if (typeof raw === "string") {
      try {
        return JSON.stringify(JSON.parse(raw), null, 2);
      } catch {
        return raw;
      }
    }
    try {
      return JSON.stringify(raw, null, 2);
    } catch {
      return String(raw);
    }
  }

  // 单个工具调用气泡：summary 即工具名，展开查看参数（默认收起，防长参数撑开面板）
  function toolCallBlock(tool, ctx) {
    const id = (tool && tool.id) || "";
    const name =
      (tool && tool.function && tool.function.name) ||
      (tool && tool.name) ||
      "工具调用";
    if (ctx && id) ctx.toolNames[id] = name;
    const details = document.createElement("details");
    details.className = "tool-call";
    const summary = document.createElement("summary");
    const nameEl = document.createElement("span");
    nameEl.className = "tool-call-name";
    nameEl.textContent = name;
    summary.appendChild(nameEl);
    details.appendChild(summary);
    const body = document.createElement("div");
    body.className = "tool-call-args";
    body.textContent = prettyArgs(
      tool && tool.function ? tool.function.arguments : tool && tool.arguments
    );
    details.appendChild(body);
    details.addEventListener("toggle", () => {
      if (isAlignMode()) requestAnimationFrame(() => reflowAlign());
    });
    return details;
  }

  // 工具返回气泡：头部标注「哪个工具的返回」（经 tool_call_id 关联），正文为返回内容
  function toolResultBlock(msg, ctx) {
    const { text } = extractParts(msg.content);
    const linked =
      ctx && msg.tool_call_id ? ctx.toolNames[msg.tool_call_id] : null;
    const wrap = document.createElement("div");
    wrap.className = "tool-result";
    const head = document.createElement("div");
    head.className = "tool-result-head";
    head.textContent = linked ? `工具返回 · ${linked}` : "工具返回";
    wrap.appendChild(head);
    const body = document.createElement("div");
    body.className = "tool-result-body";
    body.textContent = text || "（无返回内容）";
    wrap.appendChild(body);
    return wrap;
  }

  function bubbleFor(msg, index, convId, ctx) {
    const role = msg.role || "";
    const { reasoning, text } = extractParts(msg.content);
    const tools = Array.isArray(msg.tool_calls) ? msg.tool_calls : null;
    const el = document.createElement("div");
    el.dataset.index = String(index);
    el.dataset.conv = convId || "";
    if (role === "user") {
      el.className = "msg user";
      el.textContent = text || "（空消息）";
    } else if (role === "assistant_reasoning" || role === "reasoning") {
      // 旧格式：独立的推理角色消息，整条内容即思维链
      el.className = "msg bot";
      el.appendChild(reasoningSection(reasoning || text || "（推理过程）"));
    } else if (role === "tool") {
      // 工具返回：结构化气泡（不再是裸文本）
      el.className = "msg tool";
      el.appendChild(toolResultBlock(msg, ctx));
    } else if (role === "system") {
      el.className = "msg meta";
      el.textContent = text || "（系统消息）";
    } else {
      // assistant 等其余角色：正文 + 可折叠思维链 + 工具调用气泡（按出现顺序）
      el.className = "msg bot";
      if (reasoning) el.appendChild(reasoningSection(reasoning));
      if (tools && tools.length) {
        for (const t of tools) el.appendChild(toolCallBlock(t, ctx));
      }
      if (text) {
        el.appendChild(document.createTextNode(text));
      } else if (!reasoning && !(tools && tools.length)) {
        el.appendChild(document.createTextNode("…"));
      }
    }
    // 悬停操作：重新生成（仅 user 发言）；整体历史的编辑走面板头部的「编辑」JSON 编辑器
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

  // 消息流视图：与 LLM 历史并行的纯记录（真实会话中 user 发言 + bot 回复），
  // 轻量渲染——无 LLM 格式的思维链/工具调用。user 气泡标注发送者身份、@ 标记
  // 与回复状态（成功/无回复/错误），bot 气泡即回复内容。
  function streamBubble(m) {
    const role = m.role === "bot" ? "bot" : "user";
    const el = document.createElement("div");
    el.className = "msg " + role;
    const head = document.createElement("div");
    head.className = "stream-msg-head";
    head.textContent =
      (m.sender_name || m.sender_id || (role === "bot" ? "virtual_bot" : "发送者")) +
      (m.at_bot ? " @" : "");
    el.appendChild(head);
    const body = document.createElement("div");
    body.className = "stream-msg-body";
    body.textContent = m.text || "（空消息）";
    el.appendChild(body);
    if (role === "user" && m.reply_status) {
      const st = document.createElement("span");
      st.className = "stream-status " + m.reply_status;
      st.textContent = statusText(m.reply_status);
      el.appendChild(st);
    }
    return el;
  }

  // 消息流轮次分组：每条 user 发言开启新一轮，期间的 bot 回复归属该轮——
  // 与 LLM 历史的轮次语义一致，使消息流视图也能参与轮次对齐
  function groupStreamTurns(messages) {
    const turns = [];
    let cur = null;
    for (const m of messages || []) {
      if (m.role === "user") {
        cur = [];
        turns.push(cur);
      } else if (!cur) {
        cur = [];
        turns.push(cur);
      }
      cur.push(m);
    }
    return turns;
  }

  // 轮次对齐模式下的消息流：与 renderAligned 同结构（每轮一个 .turn-wrap），
  // reflowAlign 统一各面板每轮高度
  function renderStreamAligned(panel, messages) {
    const chatEl = panel.querySelector(".chat");
    chatEl.innerHTML = "";
    chatEl.classList.add("aligned");
    const turns = groupStreamTurns(messages);
    if (!turns.length) {
      const p = document.createElement("div");
      p.className = "empty";
      p.textContent = "暂无消息流记录（在此面板发送消息后可见）";
      chatEl.appendChild(p);
      return;
    }
    for (const msgs of turns) {
      const wrap = document.createElement("div");
      wrap.className = "turn-wrap";
      for (const m of msgs) wrap.appendChild(streamBubble(m));
      chatEl.appendChild(wrap);
    }
  }

  function renderStream(panel, messages) {
    if (isAlignMode()) return renderStreamAligned(panel, messages);
    const chatEl = panel.querySelector(".chat");
    chatEl.innerHTML = "";
    chatEl.classList.remove("aligned");
    if (!messages.length) {
      const p = document.createElement("div");
      p.className = "empty";
      p.textContent = "暂无消息流记录（在此面板发送消息后可见）";
      chatEl.appendChild(p);
      return;
    }
    for (const m of messages) chatEl.appendChild(streamBubble(m));
    chatEl.scrollTop = chatEl.scrollHeight;
  }

  return { renderChat, renderHistory, renderAligned, bubbleFor, renderStream };
}
