// render_markdown.js — LLM 报告 markdown 受限子集解析与渲染（零依赖纯函数）
//
// LLM 生成报告是「数据不是代码」：渲染层只经 textContent / createTextNode 落
// 文本（转义天然保证，绝不拼 HTML 字符串），链接 href 仅放行 http(s)/
// mailto 协议（javascript: 等协议直接置 "#"）。parseMarkdown 解析为块 + 内联
// token 的中间结构（零 DOM 依赖，node:test 可直接测），renderMarkdownBlocks
// 把块结构渲染进调用方提供的 doc（测试可传 JSDOM 或 mock document）。
//
// 支持块：# / ## / ### 标题、段落、``` 代码块、- 无序列表、1. 有序列表、
// | a | b | 表格（含 --- 分隔行）、--- 分隔线；内联：**粗体**、*斜体*、
// `行内代码`、[文本](链接)。不支持嵌套与更多语法（受限子集，够 LLM 报告用）。

// 内联 token 序列化（解析层与渲染层共用的中间结构）
// token: {type:"text", text} | {type:"bold", children} | {type:"italic", children}
//        | {type:"code", text} | {type:"link", text, url}
export function parseInline(text) {
  const tokens = [];
  let last = 0;
  const re = /\*\*([^*]+)\*\*|`([^`]+)`|\[([^\]]*)\]\(([^)\s]+)\)|\*([^*\s][^*]*)\*/g;
  let m;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) tokens.push({ type: "text", text: text.slice(last, m.index) });
    if (m[1] !== undefined) {
      tokens.push({ type: "bold", children: [{ type: "text", text: m[1] }] });
    } else if (m[2] !== undefined) {
      tokens.push({ type: "code", text: m[2] });
    } else if (m[3] !== undefined) {
      tokens.push({ type: "link", text: m[3], url: m[4] });
    } else if (m[5] !== undefined) {
      tokens.push({ type: "italic", children: [{ type: "text", text: m[5] }] });
    }
    last = m.index + m[0].length;
  }
  if (last < text.length) tokens.push({ type: "text", text: text.slice(last) });
  return tokens;
}

// 表格行拆分为单元格（剥首尾 |，按 | 分割并去空白）
function splitRow(line) {
  let s = line.trim();
  if (s.startsWith("|")) s = s.slice(1);
  if (s.endsWith("|")) s = s.slice(0, -1);
  return s.split("|").map((cell) => cell.trim());
}

// 表格分隔行（| --- | :---: | --- |）判定：只含 | - : 空格
function isTableSep(line) {
  return /^[ \t]*\|?[ \t]*:?-+:?[ \t]*(\|[ \t]*:?-+:?[ \t]*)*\|?[ \t]*$/.test(line);
}

// markdown 受限子集 → 块列表
// 块：{type:"heading", level, inline} | {type:"paragraph", inline}
//    | {type:"code", lang, code} | {type:"list", ordered, items:[inline]}
//    | {type:"table", headers:[inline], rows:[[inline]]} | {type:"hr"}
export function parseMarkdown(text) {
  const lines = String(text == null ? "" : text).split(/\r?\n/);
  const blocks = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];

    // 代码块：``` 围栏（lang 可选），到下一个 ``` 结束（不关闭则到文末）
    const fence = /^```(\S*)\s*$/.exec(line);
    if (fence) {
      const lang = fence[1];
      const code = [];
      i += 1;
      while (i < lines.length && !/^```\s*$/.test(lines[i])) {
        code.push(lines[i]);
        i += 1;
      }
      i += 1; // 跳过关闭围栏（或文末）
      blocks.push({ type: "code", lang, code: code.join("\n") });
      continue;
    }

    // 标题：# / ## / ###
    const heading = /^(#{1,3})\s+(.*)$/.exec(line);
    if (heading) {
      blocks.push({
        type: "heading",
        level: heading[1].length,
        inline: parseInline(heading[2]),
      });
      i += 1;
      continue;
    }

    // 分隔线：--- / *** / ___
    if (/^\s*([-*_])\s*\1\s*\1\s*$/.test(line)) {
      blocks.push({ type: "hr" });
      i += 1;
      continue;
    }

    // 无序列表：- / * / +
    const ul = /^\s*[-*+]\s+(.*)$/.exec(line);
    if (ul) {
      const items = [parseInline(ul[1])];
      i += 1;
      while (i < lines.length) {
        const m = /^\s*[-*+]\s+(.*)$/.exec(lines[i]);
        if (!m) break;
        items.push(parseInline(m[1]));
        i += 1;
      }
      blocks.push({ type: "list", ordered: false, items });
      continue;
    }

    // 有序列表：1. / 1)
    const ol = /^\s*\d+[.)]\s+(.*)$/.exec(line);
    if (ol) {
      const items = [parseInline(ol[1])];
      i += 1;
      while (i < lines.length) {
        const m = /^\s*\d+[.)]\s+(.*)$/.exec(lines[i]);
        if (!m) break;
        items.push(parseInline(m[1]));
        i += 1;
      }
      blocks.push({ type: "list", ordered: true, items });
      continue;
    }

    // 表格：连续含 | 的行（首行表头，可带 --- 分隔行，其余为数据行）
    if (line.includes("|")) {
      const tableLines = [line];
      i += 1;
      while (i < lines.length && lines[i].trim().includes("|")) {
        tableLines.push(lines[i]);
        i += 1;
      }
      if (tableLines.length >= 2) {
        const headers = splitRow(tableLines[0]).map(parseInline);
        const rows = [];
        for (let r = 1; r < tableLines.length; r += 1) {
          if (isTableSep(tableLines[r])) continue;
          rows.push(splitRow(tableLines[r]).map(parseInline));
        }
        blocks.push({ type: "table", headers, rows });
        continue;
      }
      // 单行含 | 降级为段落
      blocks.push({ type: "paragraph", inline: parseInline(line) });
      i += 1;
      continue;
    }

    // 空行跳过
    if (!line.trim()) {
      i += 1;
      continue;
    }

    // 段落
    blocks.push({ type: "paragraph", inline: parseInline(line.trim()) });
    i += 1;
  }
  return blocks;
}

// 链接协议白名单（http/https/mailto 放行；其余置 "#" 防 javascript: 等注入）
function safeHref(url) {
  const u = String(url == null ? "" : url).trim();
  return /^(https?:|mailto:)/i.test(u) ? u : "#";
}

// 把内联 token 序列渲染进 parent（textContent / createTextNode，天然转义）
function appendInline(parent, tokens, doc) {
  for (const t of tokens) {
    if (t.type === "text") {
      parent.appendChild(doc.createTextNode(t.text));
    } else if (t.type === "bold") {
      const el = doc.createElement("strong");
      appendInline(el, t.children || [], doc);
      parent.appendChild(el);
    } else if (t.type === "italic") {
      const el = doc.createElement("em");
      appendInline(el, t.children || [], doc);
      parent.appendChild(el);
    } else if (t.type === "code") {
      const el = doc.createElement("code");
      el.textContent = t.text;
      parent.appendChild(el);
    } else if (t.type === "link") {
      const el = doc.createElement("a");
      el.textContent = t.text;
      el.href = safeHref(t.url);
      el.target = "_blank";
      el.rel = "noopener noreferrer";
      parent.appendChild(el);
    }
  }
}

// 块结构 → DOM 容器（doc 为 document 或测试 mock；调用方负责挂载）
export function renderMarkdownBlocks(blocks, doc) {
  const root = doc.createElement("div");
  root.className = "md-render";
  for (const b of blocks) {
    let el;
    switch (b.type) {
      case "heading": {
        el = doc.createElement(`h${Math.min(Math.max(b.level, 1), 3)}`);
        appendInline(el, b.inline || [], doc);
        break;
      }
      case "paragraph": {
        el = doc.createElement("p");
        appendInline(el, b.inline || [], doc);
        break;
      }
      case "code": {
        el = doc.createElement("pre");
        const code = doc.createElement("code");
        code.textContent = b.code;
        if (b.lang) code.dataset.lang = b.lang;
        el.appendChild(code);
        break;
      }
      case "list": {
        el = doc.createElement(b.ordered ? "ol" : "ul");
        for (const item of b.items || []) {
          const li = doc.createElement("li");
          appendInline(li, item, doc);
          el.appendChild(li);
        }
        break;
      }
      case "table": {
        el = doc.createElement("table");
        if ((b.headers || []).length) {
          const tr = doc.createElement("tr");
          for (const h of b.headers) {
            const th = doc.createElement("th");
            appendInline(th, h, doc);
            tr.appendChild(th);
          }
          const thead = doc.createElement("thead");
          thead.appendChild(tr);
          el.appendChild(thead);
        }
        if ((b.rows || []).length) {
          const tbody = doc.createElement("tbody");
          for (const row of b.rows) {
            const tr = doc.createElement("tr");
            for (const cell of row) {
              const td = doc.createElement("td");
              appendInline(td, cell, doc);
              tr.appendChild(td);
            }
            tbody.appendChild(tr);
          }
          el.appendChild(tbody);
        }
        break;
      }
      case "hr": {
        el = doc.createElement("hr");
        break;
      }
      default:
        continue;
    }
    root.appendChild(el);
  }
  return root;
}
