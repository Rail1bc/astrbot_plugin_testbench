// pages/testbench/pure.js — 前端零依赖纯函数层
//
// 从 testset_editor.js / testset_run.js 抽取的纯逻辑：不引用 DOM、state 或
// 其它页面模块，Node 的 node:test（tests/frontend/）可直接加载做动态测试
// （仓库根 package.json 声明 "type": "module"，本文件即 ES module）。
// 页面模块从这里复用同一份实现，保证「行收集 / 导入解析 / 统计」只有一份，
// 且被动态测试覆盖——避免页面逻辑与测试逻辑双份漂移。

// 需要「断言值」输入的规则类型（json / non_empty 不需要）。与 testset_editor.js
// 的 RULE_TYPES（含中文标签）配套：新增值类断言类型须两处同步 + 后端规则。
export const RULE_VALUE_TYPES = new Set([
  "contains",
  "not_contains",
  "regex",
  "min_len",
  "max_len",
  "prefix",
  "suffix",
]);

// 导出 / 导入信封的格式标识与版本（parseTestsetEnvelope 与导出共用一份）
export const EXPORT_FORMAT = "astrbot-testbench-testset";
export const EXPORT_VERSION = 2;

// 连续 true 标志合并为区间（单条 = [i,i]）：勾选行实时预览与保存收集共用
export function rangesFromFlags(flags) {
  const ranges = [];
  let start = -1;
  flags.forEach((c, i) => {
    if (c && start < 0) start = i;
    else if (!c && start >= 0) {
      ranges.push([start, i - 1]);
      start = -1;
    }
  });
  if (start >= 0) ranges.push([start, flags.length - 1]);
  return ranges;
}

// 行内 rule 构造：type 空 → null；需要值的类型值非空才保留（min_len / max_len
// 须整数）；LLM 规则读 profile/context → {kind: "llm", profile_id, context?}。
// sliceRange（消息规则的切片范围输入，仅 context=slice 时生效）：合法输入
// "2-4" / "3" / "3-4,10-12"（多段逗号分隔）经 parseSliceRange 解析为 0 基
// {from, to} 区间列表写入 rule.slice_range，空 / "all" / 非法输入不写入
// （回退该步及之前全部记录）。injectSystemPrompt（LLM 规则级「注入被测
// Agent 系统提示词」开关，缺省开启）：显式 false 才写入
// rule.inject_system_prompt（缺省时后端按开启处理，字段保持干净）
export function buildRule(type, value, profileId, context, sliceRange, injectSystemPrompt) {
  if (!type) return null;
  if (type === "llm") {
    if (!profileId) return null;
    const rule = { kind: "llm", profile_id: profileId };
    if (context) rule.context = context;
    if (context === "slice" && sliceRange) {
      const sc = parseSliceRange(sliceRange);
      if (sc && sc !== "all") rule.slice_range = sc;
    }
    if (injectSystemPrompt === false) rule.inject_system_prompt = false;
    return rule;
  }
  if (RULE_VALUE_TYPES.has(type)) {
    const v = String(value == null ? "" : value).trim();
    if (!v) return null;
    if (type === "min_len" || type === "max_len") {
      const n = Number(v);
      return Number.isInteger(n) ? { type, value: n } : null;
    }
    return { type, value: v };
  }
  return { type };
}

// 行内多断言收集：ruleInputs 为 [{type, value, profileId, context, sliceRange?,
// injectSystemPrompt?}]，buildRule 归为 null 的整行丢弃
export function collectRules(ruleInputs) {
  const rules = [];
  for (const r of ruleInputs || []) {
    const rule = buildRule(
      r.type,
      r.value,
      r.profileId,
      r.context,
      r.sliceRange,
      r.injectSystemPrompt,
    );
    if (rule) rules.push(rule);
  }
  return rules;
}

// 收集编辑器行：rows 为 [{text, rules, sender, isCommand, autoAt, batch}]。
// 空文本行视为删除，批量段索引基于保留后的消息序列；每条消息带规则列表
// （空规则 → []）、命令标记、可选身份（sender.sender_id 存在才合并）与自动@
export function collectEditorRows(rows) {
  const messages = [];
  const batchFlags = [];
  for (const row of rows || []) {
    const text = String(row.text == null ? "" : row.text).trim();
    if (!text) continue;
    const message = { text, rules: row.rules || [] };
    if (row.isCommand) message.is_command = true;
    if (row.sender && row.sender.sender_id !== undefined) {
      Object.assign(message, row.sender);
    }
    message.auto_at = !!row.autoAt;
    messages.push(message);
    batchFlags.push(!!row.batch);
  }
  return { messages, batchRanges: rangesFromFlags(batchFlags) };
}

// 最终断言 scope 输入解析：空 / "all" → "all"；"2-4" → {from:1, to:3}；
// "3" → {from:2, to:2}；其余 → null（非法，保存前校验报错）
export function parseScope(text) {
  const t = String(text == null ? "" : text).trim();
  if (!t || t === "all") return "all";
  const m = /^(\d+)(?:-(\d+))?$/.exec(t);
  if (!m) return null;
  const start = Number(m[1]);
  const end = m[2] ? Number(m[2]) : start;
  if (start < 1 || end < start) return null;
  return { from: start - 1, to: end - 1 };
}

// 消息规则切片范围输入解析（支持多段，逗号分隔）：空 / "all" → "all"；
// "2-4" → [{from:1, to:3}]；"3" → [{from:2, to:2}]；"3-4,10-12" →
// [{from:2,to:3},{from:9,to:11}]；其余 → null（非法，保存前校验报错）。
// 与 parseScope（最终断言 scope，单段）并存：slice 输入可多段，最终断言
// scope 保持单段 {from,to} 语义（后端 final_rules 的 scope 只吃单段）
export function parseSliceRange(text) {
  const t = String(text == null ? "" : text).trim();
  if (!t || t === "all") return "all";
  const parts = t
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
  if (!parts.length) return null;
  const ranges = [];
  for (const part of parts) {
    const m = /^(\d+)(?:-(\d+))?$/.exec(part);
    if (!m) return null;
    const start = Number(m[1]);
    const end = m[2] ? Number(m[2]) : start;
    if (start < 1 || end < start) return null;
    ranges.push({ from: start - 1, to: end - 1 });
  }
  return ranges;
}

// 切片范围回填文案（编辑框还原）：[{from,to},...] → "2-4,10-12"；
// 旧单段 {from,to} 兼容 → "2-4"；空 / 非法 → ""（= 全部）
export function sliceRangeToText(ranges) {
  if (!ranges) return "";
  const items = Array.isArray(ranges) ? ranges : [ranges];
  return items
    .map((r) => {
      if (r && typeof r === "object" && r.from != null && r.to != null) {
        const s = r.from + 1;
        const e = r.to + 1;
        return s === e ? String(s) : `${s}-${e}`;
      }
      return "";
    })
    .filter(Boolean)
    .join(",");
}

// 信封解析（校验 format/version，预留「测试集市场」下载路径：传入 JSON 文本
// 即可）。v1 兼容：消息单条 rule → rules 列表；v2 增加 is_command / rules 与
// 身份配置（identity 快照 / pool 身份池），导入不创建身份 / 群聊记录。
export function parseTestsetEnvelope(text) {
  let data;
  try {
    data = JSON.parse(text);
  } catch (err) {
    throw new Error("文件不是合法 JSON: " + err.message);
  }
  if (!data || data.format !== EXPORT_FORMAT) {
    throw new Error("不是有效的测试集文件（format 不匹配）");
  }
  if (typeof data.version !== "number" || data.version > EXPORT_VERSION) {
    throw new Error("不支持的测试集格式版本: " + String(data.version));
  }
  const name =
    typeof data.name === "string" && data.name.trim()
      ? data.name.trim()
      : "导入的测试集";
  if (!Array.isArray(data.messages)) {
    throw new Error("测试集文件缺少 messages 数组");
  }
  const messages = [];
  for (const m of data.messages) {
    const text = m && typeof m.text === "string" ? m.text.trim() : "";
    if (!text) continue;
    // v2 直接 rules；v1 单条 rule 归并为单元素列表
    const rules = [];
    if (m && Array.isArray(m.rules)) {
      for (const r of m.rules) {
        if (r && typeof r === "object") rules.push({ ...r });
      }
    } else if (m && m.rule != null) {
      rules.push({ ...m.rule });
    }
    const message = { text, rules };
    if (m && m.is_command === true) message.is_command = true;
    // 可选 sender / auto_at（向后兼容：缺省字段的旧信封照常导入，auto_at
    // 缺省视为开启——渲染时按 `!== false` 勾选）
    if (m && typeof m.sender_id === "string" && m.sender_id) {
      message.sender_id = m.sender_id;
    }
    if (m && typeof m.sender_name === "string" && m.sender_name) {
      message.sender_name = m.sender_name;
    }
    if (m && typeof m.auto_at === "boolean") {
      message.auto_at = m.auto_at;
    }
    messages.push(message);
  }
  let batchRanges = [];
  if (Array.isArray(data.batch_ranges)) {
    batchRanges = data.batch_ranges
      .filter(
        (r) =>
          Array.isArray(r) &&
          r.length === 2 &&
          typeof r[0] === "number" &&
          typeof r[1] === "number" &&
          Number.isInteger(r[0]) &&
          Number.isInteger(r[1]) &&
          !(r[0] > r[1]),
      )
      .map(([s, e]) => [s, e]);
  }
  // 身份配置：v2 按 identity / pool 字段；缺省（含 v1）→ single 默认身份
  const result = {
    name,
    messages,
    batch_ranges: batchRanges,
    final_rules: [],
    identity_mode: data.pool ? "pool" : "single",
    identity_id: null,
    chat_group_id: null,
  };
  // 最终断言（跨轮）：v2 携带 final_rules；逐项浅拷贝规则与 scope
  if (Array.isArray(data.final_rules)) {
    for (const fr of data.final_rules) {
      if (!fr || typeof fr !== "object" || !fr.rule || typeof fr.rule !== "object") {
        continue;
      }
      const item = { rule: { ...fr.rule } };
      if (fr.scope !== undefined) item.scope = fr.scope;
      result.final_rules.push(item);
    }
  }
  if (result.identity_mode === "pool") {
    if (data.pool && typeof data.pool === "object") {
      result.pool_snapshot = data.pool;
    }
  } else if (data.identity && typeof data.identity === "object") {
    result.identity_snapshot = data.identity;
    if (data.identity.id) result.identity_id = data.identity.id;
  }
  return result;
}

// ---------- 测试集运行统计（verdict 计数） ----------

// 单条会话结果的「断言未通过」数：优先 verdicts（评审层产物，含机械 + LLM
// 规则，pass=false 计失败）；旧格式回退 assertion。评审失败（error/invalid，
// pass 为 null）不计入——评审失败 ≠ 评审不通过，单列 review 失败数
export function ruleFailCount(r) {
  if (Array.isArray(r.verdicts) && r.verdicts.length) {
    return r.verdicts.filter((v) => v.pass === false).length;
  }
  return r.assertion && !r.assertion.pass ? 1 : 0;
}

// 单条会话结果的「评审失败」数（LLM 调用失败 / 输出契约不符，pass 为 null）
export function ruleReviewFailCount(r) {
  if (!Array.isArray(r.verdicts)) return 0;
  return r.verdicts.filter(
    (v) => v.status === "error" || v.status === "invalid",
  ).length;
}

// 批量发送范围的启动文案：如「，含批量段 1-2、4」；无批量段返回空串
export function segmentSummary(testset) {
  const ranges = (testset && testset.batch_ranges) || [];
  if (!ranges.length) return "";
  const parts = ranges.map(([s, e]) => (s === e ? `${s + 1}` : `${s + 1}-${e + 1}`));
  return `，含批量段 ${parts.join("、")}`;
}

// 进度文案：当前步在某批量段内 → 显示段范围；否则显示第 i/N 步
export function segmentLabel(run, idx) {
  for (const [s, e] of (run && run.batch_ranges) || []) {
    if (idx >= s && idx <= e) return `第 ${s + 1}–${e + 1} 步（批量）`;
  }
  const total = (run && run.steps && run.steps.length) || 0;
  return total ? `第 ${idx + 1}/${total} 步` : `第 ${idx + 1} 步`;
}
