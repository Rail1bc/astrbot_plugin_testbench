# 测试集系统重设计（输入行为 / 审查自动化 / 报告）

> **状态**：设计已拍板（2026-08-06 四项决策已定），**v1 与 v2 已全部实施**。
> 输入层（身份 single / pool 与内联快照、导出导入）、评审层（LLM reviewer /
> 类型化指标 / final_rules / 评审时机）与报告层（默认聚合 + 持久化 / 级联删除 /
> 测试集视图报告页 / 评审重试）之后，**v2 收尾三项（2026-08-07 落地）**：
> `any` / `not` 组合算子（§5.1，消息规则）、报告按「统计 / 详情 / LLM 报告」
> 三类组织（§6.2，统计纯聚合 + 详情完整数据）、LLM 生成报告（§6.3，`report_llm`
> 配置 + 生成端点 + markdown 受限子集渲染器 + 转义）。
> 实施历程：三包结构重构（`9ec9a6f`）→ 评审层（LLM reviewer / 类型化指标 /
> final_rules / 评审时机）→ 输入层（身份 single / pool 与内联快照、导出导入）
> → 报告层（report_enabled / 持久化 / 报告视图 / 重试），测试集编辑 UI 优化
> （默认 0 断言 / 消息框拉伸 / slice 范围 / 注入提示词开关）随后补齐，
> v2 收尾（组合算子 / 报告 3 类 / LLM 生成报告）最后落地。

## 1. 目标与范围

把「测试集」从一个「连续 user 消息序列 + 单条二值断言」的工具，升级为一个**可自由组合的测试系统**，三个层面全部配置化：

1. **输入行为可自由组合**（难度不大）：每条消息是「待模拟的行为对象」——输入文本、触发方式（@ 触发 bot / 单纯信息流）、是否命令、发送者身份（id / 昵称 / 是否管理员）。
2. **审查自动化，配置组合覆盖几乎全部审查要求**（本次设计难点）：机械规则 + LLM 评审可组合；测试判定不必然是二值，可以是评分 / 枚举 / 评语等结构化产物。
3. **报告结构与呈现用户可定义**：评审产物统一为类型化 JSON，报告 = JSON 数据合并 + 默认聚合模板兜底 + LLM 生成报告（用户提示词主导）。

价值框架（沿用既有评估）：测试组 = 一句话 × 多会话（宽度）；测试集 = 多轮序列 × 会话（纵深）。

## 2. 现状与差距

### 现状（已实现）

- **消息模型**：`{text, rule?, sender_id?, sender_name?, auto_at?}`（`store/testset_store.py::_normalize_messages`）。
- **断言**：单条 rule，机械判定（`eval/mechanical.py::evaluate_rule`：contains / not_contains / regex / json / non_empty / min_len / max_len / prefix / suffix），产出 `{pass, detail}` 二值。
- **运行**：`core/testset_runner.py` 后端按段驱动（单步段逐条等完成、批量段重叠发再收），`batch_ranges` 控制节奏；消息级 sender / auto_at 已透传。
- **报告**：结果表格弹窗（行 = 步骤、列 = 会话、✓/✗），前端 `state.runReports` 暂存（上限 20 份），无持久化、无导出。
- **导出**：信封 `{format: "astrbot-testbench-testset", version: 1, name, messages, batch_ranges}`。

### 差距

| 层面 | 现状 | 目标 |
|---|---|---|
| 消息形态 | 纯文本 + 单断言 | 行为对象：触发方式 / 命令标记 / 完整身份 / 多断言 |
| 身份 | 消息级 sender_id/sender_name；is_admin 只能经身份库派生 | 身份模式 single/pool + 内联快照；导出身份实体 / 身份池 |
| 断言 | 单规则、二值判定 | 多规则；机械(bool) + LLM(JSON 指标)；跨轮 final_rules + scope |
| 判定产物 | `{pass, detail}` | 类型化指标（number / enum / text / bool）+ 派生 pass |
| 报告 | 固定表格、前端暂存 | `report_enabled` 配置；默认聚合模板 + LLM 报告；后端持久化、关联测试集、级联删除 |

## 3. 总体架构：三层模型

```
输入层（消息形态）→ 评审层（规则 + 类型化指标）→ 报告层（JSON 合并 + 呈现）
```

- **统一通货**：评审产物 = **类型化指标 JSON**。机械规则恒产出 bool；LLM 评审产出 JSON（含 ≥1 个核心指标）。
- **数据与呈现解耦**：报告只消费「收集到的指标数据」，呈现方式（默认模板 / LLM 报告）与数据收集无关。
- 每步 × 每会话 × 每规则的产物都是 JSON 可序列化的指标集，按 run 收集合并即得报告数据。

## 4. 输入层：消息 = 待模拟的行为对象

测试集新增**身份配置**与**报告配置**，消息从 `{text, rule}` 扩展为：

```jsonc
// 测试集配置（身份模式二选一 + 是否产出报告）
{
  "identity_mode": "single" | "pool",  // single：测试集级 1 个身份；pool：绑定身份池
  "identity_id"?: "id_...",            // single：身份实体（空 = 默认身份「测试台」非管理员）
  "chat_group_id"?: "cg_...",          // pool：身份池（虚拟群聊）
  "report_enabled": false,             // 是否产出测试报告（默认不产出）
  "messages": [ Message ],
  "batch_ranges": [[s, e]]
}

// 消息
{
  "text": "输入文本（命令则带命令前缀）",
  "auto_at": true,          // @触发 bot vs 单纯信息流（仅 GroupMessage 生效；现状已有）
  "is_command": false,      // 是否命令：预期触发框架行为而非 LLM 回复（新）
  "sender_id"?: "id_...",   // pool 模式引用身份池成员；空 = 默认身份（single 模式恒用测试集身份）
  "rules": [ ... ]          // 多断言（见评审层）
}
```

**触发方式（auto_at）**：已存在，仅 GroupMessage 有意义——开启则消息链以 `At(机器人)` 开头直接命中唤醒；关闭以未唤醒状态进管道，只能被 filter 唤醒。消息级可配置。

**命令（is_command）**：注入走完整 pipeline（含 PreProcessStage 命令前缀处理），文本带命令前缀本就能触发框架行为。显式标记的价值在**表达测试意图**：断言目标不同（命令通常产出框架行为结果、未必调 LLM / 未必有 LLM 回复），报告与唤醒语义也随之区分。

**发送者身份（复用身份实体，已拍板）**：编辑器从**身份库**选择身份实体（复用）；**允许为空 = 默认身份（测试台，非管理员）**。测试集配置 `identity_mode` 二选一：

- `single`（默认）：测试集级绑定 **1 个身份**（`identity_id`），所有消息恒用它——「始终只有 1 个身份」。
- `pool`：绑定一个**虚拟群聊作身份池**（`chat_group_id`），消息级 `sender_id` 引用池内成员——「所有身份都来源于同一个身份池」。

测试集保存时把被引用身份的完整数据（id / name / sender_id / sender_name / is_admin）**内联快照**进测试集（自包含）——运行、导出、导入都不依赖身份库记录是否存在，也避免身份删除后的悬空引用。

**导出 / 导入（已拍板）**：按身份模式携带——`single` → **1 个身份实体**；`pool` → **身份池**（群聊名 + 成员身份实体列表）。**导入不创建身份 / 群聊记录**（不写 `identity_store` / `chat_groups`），测试集携带的内联快照直接可用。旧文件缺省字段照常导入（沿用 `parseTestsetEnvelope` 的宽松兼容）。

## 5. 评审层：规则、产物与审查自动化

### 5.1 规则模型（极小规则树）

- **叶节点 = 原子判定器**，两种 kind：
  - `mechanical`：现有类型（正则 / 包含 / 格式 / 长度等），恒产出 **bool 指标**。
  - `llm`：引用 reviewer profile，产出 **JSON 指标集**（≥1 个核心指标）。llm 规则可配 `context`（reply / record / slice）、`slice_range`（slice 时限定记录段）与 `inject_system_prompt`（缺省开启，注入被测 agent 系统提示词块到评审输入开头——用前后闭合的「以下是 / 以上是」中文块包裹，未捕获 / 为空显示占位文案；`req.system_prompt` 为空时回退从会话配置档案解析人格，把人格提示词与开场对话补进快照）。
- **组合算子只保留三个**：`all`（默认，全部子规则过）/ `any`（至少一条过）/ `not`。比这三个更复杂的表达需求，**一律写进 LLM 提示词**，不进规则树。**已实施（v2 收尾）**：`rules` 列表元素 = 叶 | `{op:"any", rules:[叶...]}`（任意组，任一子规则通过即组通过，组内子行隐式 all、不嵌套）| `{op:"not", rule:叶}`（取反，机械与 LLM 叶都可用）；**顶层隐式 all**。评估递归（`Assessor._eval_entry`）：机械子叶恒评估、LLM 子叶短路（组尚未被决定为通过才评估）、每 entry 一条 verdict（metrics 拼接、pass 派生、`rule_index` = entry 下标）、not 取反 pass（子 pass None 不取反）。**final_rules 不支持组合**（每条保持单叶）。
- **多断言**：每条消息挂规则列表（默认 all）；测试集级 `final_rules` 跨多轮评估（默认评估完整记录，可带 `scope` 限制到测试集一部分）。

### 5.2 类型化指标与核心指标约束

> 这是「默认报告模板零配置可用」的根基。**指标类型契约放 profile 级（已拍板）**——profile 声明自己的输出契约，规则引用 profile；规则级覆盖后置。

- 机械规则 → bool 指标（pass）。
- LLM 评审 JSON **必须有至少 1 个核心指标**，类型 ∈ `number`（整数/小数）/ `enum`（枚举）/ `text`（文本）。
- **指标类型必须配置声明，不能运行时推断**——报告模板要算 avg/min/max，就必须知道哪个字段是数字。reviewer profile 声明**输出契约**：

```jsonc
{
  "id": "rp_politeness",
  "name": "礼貌性审查",
  "note": "判断回复语气是否礼貌 / 中性 / 攻击性",
  "model": "...",                    // 评审模型，测试集级显式配置
  "system_prompt": "...",            // 用户编写，支持占位符展开
  "context": "reply" /* reply | record | slice */,  // 每轮 LLM rule 上下文可配置
  "metrics": [                       // 输出契约（决定报告聚合方式）
    { "key": "score",    "type": "number",
      "pass_threshold": 70 },        // number 派生 pass：≥ 阈值
    { "key": "tone",     "type": "enum",
      "enum_values": ["礼貌", "中性", "攻击性"],
      "pass_categories": ["礼貌", "中性"] },   // enum 派生 pass：∈ 集合
    { "key": "comment",  "type": "text" }      // text 不进聚合，仅详情
  ]
}
```

- 提示词按契约输出；评估时**宽松校验**（沿用 `_parse_json_reply` 的剥围栏 + 子串解析先例 + 对声明形状做轻校验）。Provider 支持 JSON mode 更稳，不支持靠强提示词 + 宽松解析兜底。
- enum 声明候选值则分布列稳定、校验更严；不声明按 distinct 值计数。

### 5.3 判定产物与评审状态

- 每条规则评估产出一条 **verdict 记录**：
  - `status` ∈ `ok` / `error`（评审调用失败：超时 / Provider 错误）/ `invalid`（JSON 不符合声明形状）。
  - `metrics`：类型化指标集；`pass`（派生，只在 ok 上计算）。
- **「评审失败」≠「评审结果为不通过」**：error / invalid 不计入聚合，报告单列「评审失败 N」。混淆会把报告数据污染成「测试失败」的假象。
- **pass 是可选派生而非强制产物**：机械规则本身就是 bool；LLM 规则按 `pass_threshold` / `pass_categories` 派生。run 状态仍保持 `done / error / cancelled`，「测试是否通过」成为报告中的可选列而非系统强制的输出。

### 5.4 审查自动化要点

- **LLM 评审是一等公民**（可行性评估结论）：自然语言本身就是最强的组合器——复合要求写进提示词而非拆规则树；只靠确定性规则穷尽「几乎全部」是不现实的。
- **短路优化**：机械规则先跑、全部过了才调 LLM 评审（成本 / 不确定性控制）。
- **异步**：LLM 评审是异步调用，评估框架最终是 **async 编排器**；现有 `evaluate_rule` 同步纯函数退化为一种叶 kind。
- 评审模型必须**测试集级显式配置**（避免用被测模型自评）。
- **评审时机（已拍板）**：测试集**全部步骤完成后**统一触发评审（不阻塞发送节奏）；**触发评审时锁会话**——单运行守卫（`has_active_run`）在评审期间持续生效，禁止并发运行启动污染被评审的记录，**评审结束或失败即解锁**。run 记录在评审阶段标记「评审中」，终态（done）才携带全部 verdicts。

## 6. 报告层：JSON 合并、默认模板与 LLM 报告

### 6.1 数据收集

评审在测试集全部步骤完成后统一触发（已拍板：触发评审时锁会话，结束或失败
解锁）；verdicts 在评审阶段逐步写入步骤结果，全部就绪后 run 进入终态，报告
从完整记录生成。error / invalid / 短路跳过的规则不入聚合。

所有产物 = 类型化指标 JSON，按 run 收集合并：

```jsonc
{
  "run_id": "...",
  "testset_id": "...",
  "sessions": [ { "session_id": "...", "name": "..." } ],
  "started_at": 0, "finished_at": 0,
  "steps": [
    {
      "message_index": 0, "text": "...", "is_command": false, "sender": {...},
      "results": [                 // 逐会话
        {
          "session_id": "...", "reply": "...", "duration": 0, "wake": {...},
          "verdicts": [            // 逐规则
            { "rule_index": 0, "status": "ok", "pass": true, "metrics": [ {...} ] }
          ]
        }
      ]
    }
  ],
  "metrics_summary": { ... }       // 默认模板的总览聚合（派生）
}
```

### 6.2 默认报告模板（兜底，零配置即聚合，v2 收尾后按 3 类组织）

- **统计（纯聚合，不展开会话）**：按指标类型机械聚合——
  - number → avg / min / max / count；
  - enum → 各分类计数；
  - bool → 通过数 / 总数 / 通过率；
  - text → 不进总览（仅详情，顶多给条数）；
  - 另加「断言：通过 X / 失败 Y（共 Z）」（`build_assertion_stats` 遍历两层
    verdict，仅 pass 非 None 计入）与「耗时：平均 / 最小 / 最大 / p50 / p95
    （N 条）」（`build_duration_stats` 收集全部 done 步骤 × 会话 duration）。
- **详情（完整执行数据）**：逐步骤 × 会话 × verdict 明细（`buildResultsTable` /
  `renderFinalVerdicts`）+ 原始 JSON 导出，不缩水。
- **LLM 报告**：见 §6.3（报告详情弹窗按「统计 / 详情 / LLM 报告」3 tab 组织）。
- error / invalid 记录不计入聚合，单列「评审失败 N」。

### 6.3 LLM 生成报告（已实施，v2 收尾）

- 插件侧只提供两件事：把收集的 JSON 报告数据作为上下文喂给**报告 LLM**（可配模型）；以及一个渲染界面。
- 报告内容 / 结构主要是**用户提示词设计**（可产出多种报告结构的组合）。
- **报告 LLM 配置 = 测试集级持久化**：测试集新增 `report_llm` 配置（`{provider_id, system_prompt?, model?}`，模型可选、缺省用 Provider 当前模型，与评审 profile 一致）——报告视图一键生成，未配置不显示生成按钮（避免死按钮）。
- **生成端点**：`POST /reports/<report_id>/llm-report`——报告数据整体作 prompt（`json.dumps(data, ensure_ascii=False, indent=2)`）喂给报告 LLM，成功写入 `data.llm_report = {status:"ok", text, provider_id, model, generated_at}` 持久化；重新生成覆盖旧产物；测试集未配置 / Provider 缺失 / 调用异常 → 400 不落库。
- **渲染（已拍板 → 已实施）**：**markdown 受限子集渲染器**（`pages/testbench/render_markdown.js`，无第三方依赖自写：`#/##/###` 标题、段落、``` 代码块、`-` 无序 / `1.` 有序列表、`| a | b |` 表格、`---` 分隔线；内联粗体 / 斜体 / 行内代码 / 链接）+ **转义**——LLM 输出是数据不是代码，渲染层只经 `textContent` / `createTextNode` 落文本（绝不拼 innerHTML 字符串），链接 `safeHref` 仅放行 http(s)/mailto（`javascript:` 等置 "#"）。HTML iframe srcdoc 渲染为备选（更省事但不可控，未采用）。

### 6.4 报告产出与持久化（已拍板）

- 测试集新增配置 **`report_enabled`（是否产出测试报告），默认不产出**——用户可能只用测试集做自动化、不想看报告。
- 产出的报告**全部持久化**（后端 JSON），**与测试集关联**；**删除测试集 → 级联删除其产出的全部报告**。
- 左侧列表底部**不再罗列「最近运行」**；报告按归属放进**测试集视图**——测试集编辑视图页眉增加「编辑 / 报告」切换，报告视图列出该测试集的全部产出报告（默认模板聚合 / LLM 报告展示与导出）。**运行进度找回**随最近运行一并迁入（已拍板）：报告视图顶部按 testset_id 列出该测试集最近的运行——运行中的可点找回进度、完成的显示摘要；`listTestsetRuns` API **保留**（断线对账 reconcile 依赖它，仅移除前端可见列表）。
- 报告与测试集同生命周期（删除即删），不受内存运行记录清理（`DONE_RUN_KEEP_SECONDS` / `STALE_RUN_TIMEOUT`）约束——报告是持久化产物而非内存暂存。

## 7. 数据模型（草案 schema 汇总）

```jsonc
// 测试集配置（store/testset_store.py 扩展）
{ "name": "...", "identity_mode": "single" | "pool",
  "identity_id"?: "id_...",           // single：测试集级身份（空 = 默认身份「测试台」非管理员）
  "chat_group_id"?: "cg_...",         // pool：身份池（虚拟群聊）
  "report_enabled": false,            // 是否产出测试报告（默认不产出）
  "report_llm"?: { "provider_id": "...", "system_prompt"?: "", "model"?: "..." },  // LLM 报告配置（模型可选，缺省用 Provider 当前模型）
  "messages": [ Message ], "batch_ranges": [[s, e]],
  "final_rules"?: [ { "rule": Rule, "scope": "all" | { "from": i, "to": j } } ] }

// 消息（single 模式不携带 sender；pool 模式按 id 引用池内成员）
{ "text": "...", "auto_at": bool, "is_command": bool,
  "sender_id"?: "id_...",             // pool 引用身份池成员；空 = 默认身份
  "rules": [ Rule ] }

// 规则（叶 | 组合节点；消息规则顶层隐式 all）
Rule = { "kind": "mechanical", "type": "...", "value": ... }      // 现有类型，恒 bool 指标
     | { "kind": "llm", "profile_id": "rp_...",
         "context"?: "reply|record|slice",                        // 缺省回落 profile.context → "reply"
         "slice_range"?: [ { "from": i, "to": j } ],              // context=slice 时限定记录段（0 基闭区间列表）
         "inject_system_prompt"?: bool }                          // 缺省开启：注入被测 agent 系统提示词块到评审输入开头
     | { "op": "any", "rules": [ Rule叶... ] }                    // 任意组：任一子规则通过即组通过（组内不嵌套）
     | { "op": "not", "rule": Rule叶 }                            // 取反（机械与 LLM 叶都可用；final_rules 不支持组合）

// Reviewer profile（测试集级配置；指标契约放此 = 已拍板）
{ "id", "name", "note", "model", "system_prompt",
  "metrics": [ { "key", "type": "number|enum|text", "enum_values"?, "pass_threshold"?, "pass_categories"? } ],
  "context"?: "reply|record|slice" }

// 指标
{ "key", "type": "number|enum|text|bool", "value": number|string|bool }

// 判定产物（verdict）
{ "rule_index", "status": "ok|error|invalid", "pass"?: bool, "metrics": [Metric] }

// 导出信封（按身份模式携带；导入不建身份/群聊，快照直接可用）
{ "format": "astrbot-testbench-testset", "version": 2, "name": "...",
  "identity"?: Identity,                              // single：1 个身份实体
  "pool"?: { "name": "...", "members": [Identity] },  // pool：身份池
  "messages": [Message], "batch_ranges": [[s, e]], "final_rules"?: [...] }
Identity = { "id", "name", "sender_id", "sender_name", "is_admin" }

// 报告（后端持久化，与测试集关联）
{ "report_id", "testset_id", "run_id", "created_at", "data": RunReportData }
```

## 8. 关键设计决策（含理由）

1. **LLM 评审是一等公民**——「配置组合覆盖几乎全部审查要求」的成立条件。自然语言是最强组合器；确定性规则只覆盖「可机械判定」的检查。
2. **指标类型必须配置声明**（profile 输出契约），不运行时推断——报告模板的聚合方式是机械的，前提是类型已知。
3. **pass 是可选派生**——产物（指标）是主数据，二值判定保留派生路径（前端表格 ✓/✗ 与聚合依赖它），但不强制一切二值化。
4. **组合算子极小化**（all / any / not）——不做通用规则引擎；更复杂的表达写进 LLM 提示词。
5. **JSON 产物作为统一通货，数据与呈现解耦**——收集、合并、导出、喂报告 LLM 全部自然。
6. **评审成败与判定结果分离**（ok / error / invalid）——评审失败 ≠ 不通过，防止污染报告数据。
7. **机械规则先跑、过了才调 LLM**——成本与不确定性控制。
8. **报告三层**：默认模板（兜底，零配置）→ LLM 报告（用户提示词主导，渲染层做 markdown 子集 + 转义）→ 模板引擎（**不做进 v1**，将来有需求再按 JSON 数据补）。
9. **身份复用实体 + 内联快照**——编辑器从身份库选择（空 = 默认身份「测试台」非管理员）；测试集保存时快照身份完整数据（自包含，导入不建库也保证可用）；单身份 / 身份池二选一，导出携带、导入不建库。
10. **报告默认不产出、与测试集同生命周期**——`report_enabled`（默认关）；产出即持久化并关联测试集，删测试集级联删报告；报告进测试集视图而非左侧列表。

## 9. 边界与风险

- **LLM 评审覆盖不了的**：需外部数据对照（回复必须等于某外部值）、需对回复执行代码再判定、需精确 ground truth 比对。这类需求超出测试台范围，写进文档而非假装全覆盖。
- **成本与不确定性**：每条规则 × 每会话 × 每步都可能一次 LLM 调用；评审非确定。缓解：测试集级显式配模型、短路优化、评审可逐条开关。
- **JSON 可靠性**：LLM 输出偶发语法错误 / 截断 / 形状不符。缓解：宽松解析 + 声明形状轻校验 + 失败默认（error/invalid）。
- **复杂度控制**：不造通用规则引擎、不造模板引擎（v1）；规则树按「叶 + 极小算子」留扩展点但不全量实现。
- **XSS**：LLM 输出（评审 / 报告）进 DOM 前必须转义——LLM 输出是数据不是代码。

## 10. 实施台阶

**v1（首个行为里程碑）**：✅ 已实施（评审层 → 输入层 → 报告层 → UI 优化分步落地）。
- 测试集身份配置：`identity_mode`（single / pool）、身份内联快照；消息级 `sender_id` 引用池成员、空 = 默认身份；导出含身份实体 / 身份池，导入不建库。
- 消息级规则列表（默认 all）替代单条 rule；`final_rules` + scope。
- LLM reviewer profile（模型 + 提示词 + 输出契约 + 阈值），支持 `{{metrics}}` 占位符预览；支持多个 profile。
- 判定产物升级为类型化指标（机械 bool + LLM JSON），评审状态 ok/error/invalid；评审在测试集全部步骤完成后统一触发、评审期间锁会话、结束或失败解锁（不阻塞发送节奏）；短路（机械未过则同步 LLM 规则跳过）。
- 报告：`report_enabled` 配置（默认关）；产出即持久化并关联测试集、删测试集级联删报告；左侧移除「最近运行」，测试集视图页眉「编辑 / 报告」切换；默认报告模板（零配置聚合）；报告评审重试。
- 前端：消息行多断言编辑、身份选择、结果表格渲染指标、报告视图；测试集编辑 UI 优化（默认 0 断言 / 消息框拉伸 / slice 范围切片 / 注入提示词开关）。

**v2（2026-08-07 已全部实施）**：
- ✅ `any` / `not` 组合算子（消息规则：嵌套任意组 + 叶行取反开关，见 §5.1 与
  CLAUDE.md「组合算子（any / not）」；final_rules 不支持组合，保持单叶）。
- ✅ LLM 生成报告（§6.3：`report_llm` 测试集级配置 + `POST /reports/<id>/llm-report`
  生成端点 + `render_markdown.js` 受限子集渲染器 + textContent 转义）。
- ✅ 报告 3 类组织（§6.2：统计纯聚合 / 详情完整数据 / LLM 报告，详情弹窗 3 tab）。

**结构先行**：三包重构已落地（`9ec9a6f`）。

## 11. 与现有代码的映射

| 变更点 | 落点 |
|---|---|
| 消息模型扩展（is_command / 多 rules / sender_id 引用） | `store/testset_store.py::_normalize_messages` |
| 身份模式（single / pool）+ 内联快照 | `store/testset_store.py` + 身份解析（`core/runner.py::_resolve_sender/_resolve_role`） |
| 机械规则保留为一种叶 kind | `eval/mechanical.py` |
| LLM 评审 + async 评估编排器 + 类型化指标 | `eval/` 新增（reviewer / 评估器 / 报告聚合纯函数） |
| 评审状态与产物落步 | `core/testset_runner.py`（步骤 results 携带 verdicts） |
| profile CRUD、报告持久化 / 查询 / 级联删除端点 | `api/testsets.py` + `api/routes.py` + 报告存储（`store/` 新增） |
| 消息行多断言 / 命令标记 / 身份选择 | `pages/testbench/testset_editor.js` |
| 结果表格渲染 verdicts、报告视图 | `pages/testbench/testset_run.js`（+ 报告渲染模块） |
| 编辑视图页眉「编辑 / 报告」切换；左侧移除「最近运行」 | `pages/testbench/testset_editor.js` + `testset_list.js` |
| 导出 / 导入信封扩展（身份实体 / 身份池，导入不建库） | `pages/testbench/testset_editor.js::parseTestsetEnvelope` 对应后端 |

## 12. 已拍板决策（2026-08-06）

1. **指标类型契约放 profile 级**——一个评审 profile 声明自己的输出契约（`metrics: [{key, type, enum_values?, pass_threshold?, pass_categories?}]`），规则引用 profile；规则级覆盖后置。
2. **身份复用实体 + 内联快照**——编辑器从身份库选择（**允许为空 = 默认身份「测试台」，非管理员**）；测试集保存时快照被引用身份的完整数据（自包含，导入不写库也保证可用）。测试集配置 `identity_mode`：`single`（测试集级 1 个身份）/ `pool`（绑定虚拟群聊作身份池，消息级 `sender_id` 引用池内成员）。导出按模式携带 **1 个身份实体**或**身份池**；**导入不创建身份 / 群聊记录**，快照直接可用。
3. **LLM 报告渲染 = markdown 受限子集渲染器 + 转义**（无第三方依赖自写；HTML iframe srcdoc 为备选）。
4. **报告默认不产出 + 与测试集同生命周期**——测试集配置 `report_enabled`（默认 false，用户只用自动化时无报告负担）；产出即**后端持久化并与测试集关联**，**删测试集级联删其报告**；左侧列表移除「最近运行」，报告进**测试集视图**（编辑视图页眉「编辑 / 报告」切换）。
5. **评审时机 = 测试完成后统一触发 + 会话锁**——评审不阻塞发送节奏；触发评审时锁会话（单运行守卫持续生效，禁止并发运行污染被评审记录），评审结束或失败解锁。运行进度找回随最近运行迁入测试集视图报告页顶部。
