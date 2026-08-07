// tests/frontend/pure.test.mjs — pure.js 纯函数单元测试（node:test，零依赖）。
//
// 运行：node --test "tests/frontend/*.test.mjs"（仓库根 package.json 声明
// "type": "module"，pages/testbench/pure.js 即 ES module 可直接加载）。
// 覆盖抽取自 testset_editor.js / testset_run.js 的行收集 / 规则构造 / 导入
// 解析 / verdict 统计 / 段文案逻辑，防止抽取期与后续修改破坏行为。
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  buildRule,
  collectEditorRows,
  collectRules,
  parseScope,
  parseTestsetEnvelope,
  rangesFromFlags,
  ruleFailCount,
  ruleReviewFailCount,
  segmentLabel,
  segmentSummary,
} from "../../pages/testbench/pure.js";

// ---------- buildRule ----------

test("buildRule: 空类型 → null", () => {
  assert.equal(buildRule("", "", "", ""), null);
});

test("buildRule: 值类规则空值 → null", () => {
  assert.equal(buildRule("contains", "   ", "", ""), null);
});

test("buildRule: contains 保留去空白值", () => {
  assert.deepEqual(buildRule("contains", " 你好 ", "", ""), {
    type: "contains",
    value: "你好",
  });
});

test("buildRule: min_len/max_len 非整数 → null", () => {
  assert.equal(buildRule("min_len", "abc", "", ""), null);
  assert.equal(buildRule("max_len", "3.5", "", ""), null);
});

test("buildRule: min_len 整数 → 数值", () => {
  assert.deepEqual(buildRule("min_len", "5", "", ""), { type: "min_len", value: 5 });
});

test("buildRule: json/non_empty 无值（忽略多余值）", () => {
  assert.deepEqual(buildRule("json", "", "", ""), { type: "json" });
  assert.deepEqual(buildRule("non_empty", "ignored", "", ""), { type: "non_empty" });
});

test("buildRule: llm 未选 profile → null", () => {
  assert.equal(buildRule("llm", "", "", "record"), null);
});

test("buildRule: llm 带 profile 无 context → 省略 context", () => {
  assert.deepEqual(buildRule("llm", "", "rp_1", ""), { kind: "llm", profile_id: "rp_1" });
});

test("buildRule: llm 带 profile + context", () => {
  assert.deepEqual(buildRule("llm", "", "rp_1", "record"), {
    kind: "llm",
    profile_id: "rp_1",
    context: "record",
  });
});

// ---------- collectRules ----------

test("collectRules: 空输入 → []", () => {
  assert.deepEqual(collectRules([]), []);
  assert.deepEqual(collectRules(null), []);
});

test("collectRules: 丢弃 buildRule 归 null 的行", () => {
  const out = collectRules([
    { type: "", value: "", profileId: "", context: "" },
    { type: "contains", value: "x", profileId: "", context: "" },
    { type: "llm", value: "", profileId: "rp_1", context: "reply" },
    { type: "regex", value: "  ", profileId: "", context: "" },
  ]);
  assert.deepEqual(out, [
    { type: "contains", value: "x" },
    { kind: "llm", profile_id: "rp_1", context: "reply" },
  ]);
});

// ---------- rangesFromFlags ----------

test("rangesFromFlags: 空 → []", () => {
  assert.deepEqual(rangesFromFlags([]), []);
});

test("rangesFromFlags: 连续段与单条合并", () => {
  assert.deepEqual(rangesFromFlags([true, true, false, true, false]), [
    [0, 1],
    [3, 3],
  ]);
});

test("rangesFromFlags: 全勾选 → 单个整段", () => {
  assert.deepEqual(rangesFromFlags([true, true, true]), [[0, 2]]);
});

// ---------- collectEditorRows ----------

test("collectEditorRows: 空文本行丢弃、批量段基于保留后索引", () => {
  const { messages, batchRanges } = collectEditorRows([
    { text: " 第一条 ", rules: [], sender: {}, isCommand: false, autoAt: true, batch: true },
    { text: "   ", rules: [], sender: {}, isCommand: false, autoAt: true, batch: false },
    {
      text: "第二条",
      rules: [],
      sender: { sender_id: "s1", sender_name: "n1" },
      isCommand: true,
      autoAt: false,
      batch: true,
    },
  ]);
  assert.equal(messages.length, 2);
  assert.deepEqual(messages[0], { text: "第一条", rules: [], auto_at: true });
  assert.deepEqual(messages[1], {
    text: "第二条",
    rules: [],
    is_command: true,
    sender_id: "s1",
    sender_name: "n1",
    auto_at: false,
  });
  assert.deepEqual(batchRanges, [[0, 1]]);
});

test("collectEditorRows: sender 无 sender_id 不合并", () => {
  const { messages } = collectEditorRows([
    { text: "hi", rules: [], sender: {}, isCommand: false, autoAt: true, batch: false },
  ]);
  assert.deepEqual(messages[0], { text: "hi", rules: [], auto_at: true });
});

// ---------- parseScope ----------

test("parseScope: 空 / all → all", () => {
  assert.equal(parseScope(""), "all");
  assert.equal(parseScope(" all "), "all");
});

test("parseScope: 单步与区间", () => {
  assert.deepEqual(parseScope("3"), { from: 2, to: 2 });
  assert.deepEqual(parseScope("2-4"), { from: 1, to: 3 });
});

test("parseScope: 非法输入 → null", () => {
  assert.equal(parseScope("0"), null);
  assert.equal(parseScope("4-2"), null);
  assert.equal(parseScope("abc"), null);
  assert.equal(parseScope("-1"), null);
});

// ---------- parseTestsetEnvelope ----------

test("parseTestsetEnvelope: 非 JSON → 抛错", () => {
  assert.throws(() => parseTestsetEnvelope("not json"), /不是合法 JSON/);
});

test("parseTestsetEnvelope: format 不匹配 → 抛错", () => {
  assert.throws(
    () =>
      parseTestsetEnvelope(
        JSON.stringify({ format: "other", version: 2, messages: [] }),
      ),
    /不是有效的测试集文件/,
  );
});

test("parseTestsetEnvelope: 版本高于当前 → 抛错", () => {
  assert.throws(
    () =>
      parseTestsetEnvelope(
        JSON.stringify({ format: "astrbot-testbench-testset", version: 3, messages: [] }),
      ),
    /不支持的测试集格式版本/,
  );
});

test("parseTestsetEnvelope: v2 完整解析", () => {
  const parsed = parseTestsetEnvelope(
    JSON.stringify({
      format: "astrbot-testbench-testset",
      version: 2,
      name: " 示例 ",
      messages: [
        {
          text: " 你好 ",
          rules: [{ type: "contains", value: "x" }],
          is_command: true,
          sender_id: "s1",
          sender_name: "n1",
          auto_at: false,
        },
        { text: "  ", rules: [], is_command: false },
      ],
      batch_ranges: [[0, 0]],
      final_rules: [{ rule: { type: "contains", value: "y" }, scope: "all" }],
      identity: { id: "i1", name: "身份" },
    }),
  );
  assert.equal(parsed.name, "示例");
  assert.equal(parsed.messages.length, 1);
  assert.deepEqual(parsed.messages[0], {
    text: "你好",
    rules: [{ type: "contains", value: "x" }],
    is_command: true,
    sender_id: "s1",
    sender_name: "n1",
    auto_at: false,
  });
  assert.deepEqual(parsed.batch_ranges, [[0, 0]]);
  assert.deepEqual(parsed.final_rules, [
    { rule: { type: "contains", value: "y" }, scope: "all" },
  ]);
  assert.equal(parsed.identity_mode, "single");
  assert.equal(parsed.identity_id, "i1");
  assert.deepEqual(parsed.identity_snapshot, { id: "i1", name: "身份" });
});

test("parseTestsetEnvelope: v1 兼容（单条 rule → rules，缺省身份）", () => {
  const parsed = parseTestsetEnvelope(
    JSON.stringify({
      format: "astrbot-testbench-testset",
      version: 1,
      name: "旧",
      messages: [{ text: "hi", rule: { type: "regex", value: "^h" } }],
    }),
  );
  assert.deepEqual(parsed.messages[0].rules, [{ type: "regex", value: "^h" }]);
  assert.equal(parsed.identity_mode, "single");
  assert.equal(parsed.identity_id, null);
  assert.equal(parsed.chat_group_id, null);
});

test("parseTestsetEnvelope: pool 身份模式", () => {
  const parsed = parseTestsetEnvelope(
    JSON.stringify({
      format: "astrbot-testbench-testset",
      version: 2,
      name: "群聊",
      messages: [{ text: "hi" }],
      pool: { name: "群", members: [{ id: "i1" }] },
    }),
  );
  assert.equal(parsed.identity_mode, "pool");
  assert.deepEqual(parsed.pool_snapshot, { name: "群", members: [{ id: "i1" }] });
});

test("parseTestsetEnvelope: 非法 batch_ranges 过滤", () => {
  const parsed = parseTestsetEnvelope(
    JSON.stringify({
      format: "astrbot-testbench-testset",
      version: 2,
      name: "x",
      messages: [{ text: "a" }, { text: "b" }],
      batch_ranges: [[0, 1], [2, 1], "bad", [0, "x"]],
    }),
  );
  assert.deepEqual(parsed.batch_ranges, [[0, 1]]);
});

// ---------- ruleFailCount / ruleReviewFailCount ----------

test("ruleFailCount: verdicts 优先，pass=false 计失败", () => {
  assert.equal(
    ruleFailCount({ verdicts: [{ pass: true }, { pass: false }, { pass: null }] }),
    1,
  );
});

test("ruleFailCount: 旧格式回退 assertion", () => {
  assert.equal(ruleFailCount({ assertion: { pass: false } }), 1);
  assert.equal(ruleFailCount({ assertion: { pass: true } }), 0);
  assert.equal(ruleFailCount({}), 0);
});

test("ruleReviewFailCount: 只数 status error/invalid", () => {
  assert.equal(
    ruleReviewFailCount({
      verdicts: [{ status: "ok" }, { status: "error" }, { status: "invalid" }],
    }),
    2,
  );
  assert.equal(ruleReviewFailCount({ verdicts: [{ pass: false }] }), 0);
  assert.equal(ruleReviewFailCount({}), 0);
});

// ---------- segmentSummary / segmentLabel ----------

test("segmentSummary: 无批量段 → 空串", () => {
  assert.equal(segmentSummary({ batch_ranges: [] }), "");
  assert.equal(segmentSummary({}), "");
});

test("segmentSummary: 单条与区间", () => {
  assert.equal(segmentSummary({ batch_ranges: [[0, 1], [3, 3]] }), "，含批量段 1-2、4");
});

test("segmentLabel: 批量段内/外", () => {
  const run = { batch_ranges: [[1, 2]], steps: [{}, {}, {}, {}] };
  assert.equal(segmentLabel(run, 1), "第 2–3 步（批量）");
  assert.equal(segmentLabel(run, 3), "第 4/4 步");
});
