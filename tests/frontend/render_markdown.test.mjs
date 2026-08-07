// tests/frontend/render_markdown.test.mjs — markdown 受限子集渲染器测试
// （node:test，零依赖）。
//
// parseInline / parseMarkdown 是零 DOM 依赖纯函数（直接断言 token / 块结构）；
// renderMarkdownBlocks 需要 doc，测试用最小 mock document（createElement /
// createTextNode 记录文本与子节点）——断言转义语义：HTML 注入文本经
// textContent / createTextNode 落文本，绝不拼 innerHTML（渲染层是「数据不是
// 代码」）；链接 href 仅放行 http(s)/mailto，其余协议置 "#"。
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  parseInline,
  parseMarkdown,
  renderMarkdownBlocks,
} from "../../pages/testbench/render_markdown.js";

// ---------- parseInline（内联 token） ----------

test("parseInline: 纯文本", () => {
  assert.deepEqual(parseInline("你好 world"), [{ type: "text", text: "你好 world" }]);
  assert.deepEqual(parseInline(""), []);
});

test("parseInline: 粗体 / 斜体 / 行内代码 / 链接混排", () => {
  assert.deepEqual(
    parseInline("**粗** 和 *斜* 与 `代码` 和 [链接](https://x.com/a)"),
    [
      { type: "bold", children: [{ type: "text", text: "粗" }] },
      { type: "text", text: " 和 " },
      { type: "italic", children: [{ type: "text", text: "斜" }] },
      { type: "text", text: " 与 " },
      { type: "code", text: "代码" },
      { type: "text", text: " 和 " },
      { type: "link", text: "链接", url: "https://x.com/a" },
    ],
  );
});

// ---------- parseMarkdown（块） ----------

test("parseMarkdown: 标题（#/##/###；#### 降级段落）", () => {
  assert.deepEqual(parseMarkdown("# 一级\n## 二级\n### 三级\n#### 四级"), [
    { type: "heading", level: 1, inline: [{ type: "text", text: "一级" }] },
    { type: "heading", level: 2, inline: [{ type: "text", text: "二级" }] },
    { type: "heading", level: 3, inline: [{ type: "text", text: "三级" }] },
    { type: "paragraph", inline: [{ type: "text", text: "#### 四级" }] },
  ]);
});

test("parseMarkdown: 代码块（含 lang；未关闭到文末）", () => {
  assert.deepEqual(
    parseMarkdown("```json\n{\"a\": 1}\n```\n\n```\n裸\n```"),
    [
      { type: "code", lang: "json", code: '{"a": 1}' },
      { type: "code", lang: "", code: "裸" },
    ],
  );
});

test("parseMarkdown: 分隔线（--- / *** / ___）", () => {
  assert.deepEqual(parseMarkdown("---\n\n***"), [{ type: "hr" }, { type: "hr" }]);
});

test("parseMarkdown: 无序 / 有序列表（连续行合并）", () => {
  assert.deepEqual(parseMarkdown("- a\n- **b**\n\n1. x\n2. y"), [
    {
      type: "list",
      ordered: false,
      items: [
        [{ type: "text", text: "a" }],
        [{ type: "bold", children: [{ type: "text", text: "b" }] }],
      ],
    },
    {
      type: "list",
      ordered: true,
      items: [
        [{ type: "text", text: "x" }],
        [{ type: "text", text: "y" }],
      ],
    },
  ]);
});

test("parseMarkdown: 表格（表头 + 分隔行 + 数据行）", () => {
  assert.deepEqual(parseMarkdown("| a | b |\n| --- | --- |\n| 1 | 2 |"), [
    {
      type: "table",
      headers: [
        [{ type: "text", text: "a" }],
        [{ type: "text", text: "b" }],
      ],
      rows: [
        [
          [{ type: "text", text: "1" }],
          [{ type: "text", text: "2" }],
        ],
      ],
    },
  ]);
});

test("parseMarkdown: 单行含 | 降级为段落", () => {
  const blocks = parseMarkdown("只有一根竖线 | 的行");
  assert.equal(blocks.length, 1);
  assert.equal(blocks[0].type, "paragraph");
});

test("parseMarkdown: 空行跳过 / 非法输入容错", () => {
  assert.deepEqual(parseMarkdown("第一行\n\n\n第二行"), [
    { type: "paragraph", inline: [{ type: "text", text: "第一行" }] },
    { type: "paragraph", inline: [{ type: "text", text: "第二行" }] },
  ]);
  assert.deepEqual(parseMarkdown(null), []);
  assert.deepEqual(parseMarkdown(undefined), []);
  assert.deepEqual(parseMarkdown(""), []);
  assert.deepEqual(parseMarkdown("   \n\t\n"), []);
});

// ---------- renderMarkdownBlocks（mock document） ----------

// 最小 DOM mock：createElement / createTextNode 记录节点与文本，供断言
// 「渲染经 textContent / 文本节点落文本（转义）、不产生注入元素」。
function mockDoc() {
  const nodes = [];
  function createElement(tag) {
    const el = {
      tagName: String(tag).toUpperCase(),
      children: [],
      _text: "",
      dataset: {},
      _href: "",
      set href(v) {
        this._href = String(v);
      },
      get href() {
        return this._href;
      },
      appendChild(child) {
        this.children.push(child);
      },
    };
    Object.defineProperty(el, "textContent", {
      set(v) {
        this._text = String(v);
      },
      get() {
        return this._text;
      },
    });
    nodes.push(el);
    return el;
  }
  function createTextNode(text) {
    const node = { nodeType: 3, textContent: String(text) };
    nodes.push(node);
    return node;
  }
  return { createElement, createTextNode, nodes };
}

function textOf(node) {
  if (node.nodeType === 3) return node.textContent;
  if (node._text) return node._text;
  return (node.children || []).map(textOf).join("");
}

test("renderMarkdownBlocks: 块结构渲染（标题/段落/列表/代码块）", () => {
  const doc = mockDoc();
  const root = renderMarkdownBlocks(
    parseMarkdown("# 标题\n\n- a\n- b\n\n```js\nlet x = 1;\n```"),
    doc,
  );
  assert.equal(root.className, "md-render");
  const tags = root.children.map((n) => n.tagName);
  assert.deepEqual(tags, ["H1", "UL", "PRE"]);
  assert.equal(textOf(root), "标题ablet x = 1;");
  const codeEl = root.children[2].children[0];
  assert.equal(codeEl.tagName, "CODE");
  assert.equal(codeEl.dataset.lang, "js");
});

test("renderMarkdownBlocks: 表格渲染（thead/tbody）", () => {
  const doc = mockDoc();
  const root = renderMarkdownBlocks(
    parseMarkdown("| a | b |\n| --- | --- |\n| 1 | 2 |"),
    doc,
  );
  const table = root.children[0];
  assert.equal(table.tagName, "TABLE");
  assert.equal(table.children[0].tagName, "THEAD");
  assert.equal(table.children[1].tagName, "TBODY");
  assert.equal(textOf(table), "ab12");
});

test("renderMarkdownBlocks: HTML 注入文本按文本渲染（不产生注入元素）", () => {
  const doc = mockDoc();
  const root = renderMarkdownBlocks(
    parseMarkdown("<script>alert(1)</script>\n\n**<img src=x onerror=alert(2)>**"),
    doc,
  );
  assert.equal(
    doc.nodes.some((n) => n.tagName === "SCRIPT" || n.tagName === "IMG"),
    false,
  );
  assert.equal(textOf(root), "<script>alert(1)</script><img src=x onerror=alert(2)>");
});

test("renderMarkdownBlocks: 链接 href 协议白名单（javascript: 置 #）", () => {
  const doc = mockDoc();
  const root = renderMarkdownBlocks(
    parseMarkdown("[好](https://x.com/a) [坏](javascript:alert(1)) [邮](mailto:a@b.c)"),
    doc,
  );
  const links = doc.nodes.filter((n) => n.tagName === "A");
  assert.deepEqual(
    links.map((l) => l.href),
    ["https://x.com/a", "#", "mailto:a@b.c"],
  );
  assert.ok(links.every((l) => l.target === "_blank" && l.rel === "noopener noreferrer"));
});
