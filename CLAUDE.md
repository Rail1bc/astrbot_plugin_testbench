# CLAUDE.md

本文件为 Claude Code 在本插件（`astrbot_plugin_testbench`）目录下工作提供指引。

## 插件概述

会话测试台（astrbot_plugin_testbench）是一个 AstrBot 插件：通过框架原生插件页面创建「虚拟会话」，并把一句话并发投递给多个虚拟会话，用于测试插件、提示词、模型与整体稳定性。

- **版本**：v0.3.0（metadata.yaml 中的版本号，未经用户批准不得擅自 bump）
- **兼容范围**：`astrbot_version: ">=4.24.1"`（v4.24.1 起提供插件页面 `subscribeSSE`，事件驱动前端依赖它）
- **独立 git 仓库**：remote `git@github.com:Rail1bc/astrbot_plugin_testbench.git`；**开发在 `dev` 分支，`main` 仅用于发布**（release.yml 只在 main 上 metadata.yaml 变更时触发自动发版）
- **无第三方依赖**：只依赖 AstrBot 公共 API（`astrbot.api.*`），不需要 requirements.txt
- **核心卖点**：虚拟会话与真实会话走**完全相同的处理路径**（不是模拟），只是把消息注入点从平台适配器换成了插件侧直接入队

### 消息处理路径（核心机制）

```
页面 (pages/testbench/)  ->  Web API (main.py)  ->  VirtualTestRunner.start()
  -> context.get_event_queue()  ->  EventBus -> PipelineScheduler.execute()
  -> 完整 pipeline（唤醒检查→白名单→会话状态→限流→内容安全→预处理→插件+LLM→装饰→回复）
  -> VirtualMessageEvent.send()/send_streaming() 捕获回复（不外发）
```

与真实平台一致：不设总超时、不分批投递。事件入队后 runner 后台逐个等待完成；runner / testset_runner 在状态变化点向 EventBus 发布事件，后端 `/events` SSE 端点实时推送（在途快照 / 会话完成 / 测试完成 / 测试集进度），前端断线后以轮询接口一次性快照对账。测试集运行与手动群发共用同一条逐会话反馈路径（`applySessionFeedback`），面板行为一致。

## 目录结构

```
astrbot_plugin_testbench/
├─ metadata.yaml        # 插件元数据（name/display_name/version/astrbot_version）
├─ main.py              # Star 类：Web API 路由 + 全部请求处理器
├─ group_store.py       # 测试组数据模型与持久化（VirtualGroupManager、umo_of）
├─ conf_routes.py       # UCR 配置档案路由操作收敛（持久路由与临时路由共用一套）
├─ event_bus.py         # 进程内事件广播（asyncio.Queue 有界队列，满则丢最旧；SSE 事件源）
├─ runner.py            # 并发测试运行器（VirtualTestRunner，临时路由经 conf_routes）
├─ stats.py             # 耗时统计纯函数（duration_stats：min/max/avg/p50/p95）
├─ assertions.py        # 回复断言规则评估纯函数（evaluate_rule：正则/包含/格式）
├─ testset_store.py     # 测试集数据模型与持久化（TestsetStore）
├─ testset_runner.py    # 测试集运行编排器（TestsetRunner：后端按段驱动，单步段逐条、批量段重叠）
├─ virtual_event.py     # VirtualMessageEvent：捕获 send/流式结果，携带完成信号
├─ pyproject.toml       # 插件仓库自包含的 ruff / pytest 配置（不依赖主仓库）
├─ pages/testbench/
│  ├─ index.html        # 页面骨架（表单/面板的静态 HTML，select 初始为空或仅默认项）
│  ├─ app.js            # 页面入口（面板/发送/会话操作/排序/初始化，组装子模块）
│  ├─ state.js          # 全部共享可变状态（state 对象，各模块经它读写）
│  ├─ modal.js          # 自绘弹窗（openModal/showModal/hideModal）
│  ├─ utils.js          # 工具函数与最终配置解析（effectiveView/findSession 等）
│  ├─ group_list.js     # 左侧测试组列表与组/会话配置弹窗（createGroupList(env)）
│  ├─ testset_list.js   # 测试集列表/编辑窗口/运行弹窗/导出导入与最近运行（createTestsetList(env)）
│  ├─ api.js            # bridge 调用的统一封装（listPlatforms/listConfs/...）
│  ├─ align.js          # 轮次对齐控制器（createAlignController，依赖注入）
│  ├─ chat.js           # 聊天内容渲染（createChatRenderer：气泡/思维链/工具调用/轮次分组）
│  └─ style.css         # 亮/暗主题样式
├─ CHANGELOG.md         # 变更记录（[Unreleased] 在上）
├─ README.md            # 面向用户的说明
├─ run_ruff.bat         # Windows 一键 ruff format+check 脚本（调用主仓库 venv）
├─ tests/               # 单元测试（test_backend.py 需 astrbot，test_frontend.py 零依赖）
├─ data/                # 本地运行数据（gitignored，发布时排除）
├─ assets/  .github/    # 仓库资源与 CI 工作流
```

> 插件本身在 `data/plugins/astrbot_plugin_testbench`（`data/` 属 AstrBot 本地数据，gitignored），测试随插件仓库维护（`tests/`，详见「测试与验证」）。

## 关键设计

### umo（unified_msg_origin）

`umo_of(session)`（group_store.py:27）：`f"{platform_id}:FriendMessage:{session_id}"`。平台 id 默认 `webchat`（与 AstrBot WebUI 一致），发送者默认 `testbench` / `测试台`，会话 id 形如 `vs_<uuid8>`，测试组 id 形如 `g_<uuid8>`。umo 是 AstrBot 会话/配置/历史隔离的键：配置档案路由（UCR）、对话历史（conversation_manager）都按它定位。

### 测试组模型（group_store.py）

- 组共享一套配置：`platform_id / conf_id / sender_id / sender_name`；组内每个会话默认四个字段均为 `None`（表示继承组配置）。
- `effective(group, session)` 解析最终配置（会话覆盖优先）；`conf_id` 为 `""` 表示显式使用默认档案（不绑定路由），`None` 表示继承组。
- `update_session` 用 `_UNSET` 哨兵区分「未传该字段」与「显式传 null（恢复继承组配置）」。
- 持久化到 `get_astrbot_plugin_data_path()/virtual_session/groups.json`；旧版平铺 `sessions.json` 自动迁移为「默认测试组」。
- `_save()` 是全量写 JSON（`ensure_ascii=False, indent=2`）；删除返回 `(组, 会话)` 对，供上层联动清理 UCR 路由与原生对话历史。

### UCR 配置档案路由

- 路由操作集中在 `conf_routes.py`：持久路由（创建/删除组与会话、会话配置变更时应用与清理）与 runner 临时路由（测试运行时指定 conf_id）共用同一套 umo → conf_id 操作，避免两处实现对 UCR API 的双份维护。
- 绑定用**会话级精确路由** `umo → conf_id`（`ucr.update_route(umop, conf_id)`），不用平台级 `platform_id::`，避免影响同平台其他会话。
- 创建组/添加会话时若带 `conf_id`，调用 `_apply_conf_routes`；删除组/会话/重置时用 `_clear_conf_routes`（仅删已存在的路由）+ `_delete_session_conversations`（级联删原生对话历史，按 umo 调 `conversation_manager.delete_conversations_by_user_id`）。
- runner 的临时路由（测试运行时指定 conf_id）带 `asyncio.Lock` 串行：`save_and_apply_routes` 保存原路由 → 应用 → 全部完成后 `restore_routes` 恢复并释放锁，避免临时路由互相污染。

### VirtualMessageEvent（virtual_event.py）

继承 `AstrMessageEvent`，消息类型 `FRIEND_MESSAGE`（私聊默认直接唤醒，无需唤醒前缀）：

- `create(...)` 构造 `AstrBotMessage`（`self_id="virtual_bot"`），`selected_provider` / `selected_model` 写入 event extra。
- **两个完成信号**：
  - `done_event`：产生过一次回复（send 或流式结束）时置位。
  - `pipeline_done_event`：pipeline 全部执行完（无论是否产生回复）后置位。**实现技巧**：重写 `cleanup_temporary_local_files()`——它是 `PipelineScheduler.execute()` 的 finally 块中唯一调用点（astrbot/core/pipeline/scheduler.py:97），runner 用 `.wait()` 精确等待 pipeline 结束。
- `send()` 捕获 `MessageChain` 到 `self.captured`；`send_streaming()` 按 aiocqhttp/discord 的累积模式合并 chunk → `squash_plain()` → 交给 `send()`，reasoning 单独累积。
- `result_summary()` 返回 `{umo, session_id, status(ok|no_reply), duration, reply, reasoning, error}`；错误文案从 `_llm_error_message` extra 读取。

### 运行器（runner.py）

- `start(sessions, text, provider_id, model, conf_id, assertion)` **立即返回 test_id**（不等待回复）；事件 `put_nowait` 入队，逐事件 `asyncio.create_task(_await_event)` 等待 `pipeline_done_event`。
- `status(test_id)` 返回 `{total, done, results[], stats}`（供断线对账一次性取回；逐会话实时反馈走 SSE 事件流）。
- `wait_done(test_id, timeout_secs=None)`：等待全部完成，超时抛 `asyncio.TimeoutError`（供测试集运行编排器逐步骤等待；参数名 `timeout_secs` 规避 ruff ASYNC109）。
- `start(...)` 带 `assertion` 时，`_await_event` 在 `result_summary()` 后用 `evaluate_rule` 评估并写入 `summary["assertion"]`。
- 运行记录保存在内存 `self._runs`，完成超过 10 分钟自动清理。
- **在途条目（重叠测试）**：每条消息登记一个条目（`self._pending`，经 `event.entry_id` 关联），状态 submitted → waiting_llm → llm → done；中间两个状态由 LLM 阶段 hook（`on_waiting_llm_request` / `on_llm_request`）推进，`pending_entries()` 供断线对账一次性取回；每次状态变更向事件总线广播在途全量快照（`{"type":"pending","entries":[...]}`），前端经 SSE 实时展示；已完成的条目保留 `DONE_KEEP_SECONDS`（30s）后随 `_prune_runs` 清理。

### 回复断言（assertions.py）

`evaluate_rule(rule, reply) -> dict | None` 纯函数：`rule is None` 返回 `None`，否则 `{pass, detail}`。类型：`contains` / `not_contains`（str 或 list value）、`regex`（`re.search`，无效 pattern → pass False）、`json` / `non_empty`（无 value）、`min_len` / `max_len`（int）、`prefix` / `suffix`（str）。**`json` 为宽松判定**（`_parse_json_reply`）：换行缩进合法；先剥 markdown 代码块围栏直接 `json.loads`，失败再取首个开括号到末个闭括号的子串解析（对象 / 数组）——LLM 回复常在 JSON 前后夹带思维链 / 说明文本（AstrBot 开启思维链显示时，回复链头会被装饰阶段注入「🤔 思考: …」前缀），严格解析会误判。缺 value、未知 type → pass False（数据损坏可见，不静默通过）。Python 正则与 JS 不一致，故评估放在后端。

### 测试集（testset_store.py / testset_runner.py）

- `TestsetStore`：持久化到 `data/virtual_session/testsets.json`（与 groups.json 同目录），模型 `{testsets: [{id: "ts_<uuid8>", name, created_at, messages: [{text, rule}], batch_ranges: [[start, end], ...]}]}`；`_normalize_messages` 清洗（text 去首尾空白、空文本丢弃、rule 归一 dict|None）；`_normalize_batch_ranges` 清洗批量段（每项须为非 bool 的两个 int 且 0 ≤ s ≤ e < message_count，不满足的整段丢弃；先按 start 升序再贪心保留不重叠段，结果与输入顺序无关；非 list → `[]`）；`MAX_MESSAGES_PER_TESTSET = 100`；允许空消息创建（先建命名条目、再在窗口里加消息），`_load` 对旧数据 `setdefault("batch_ranges", [])`。两个类名以 Test 开头，须带 `__test__ = False` 防 pytest 误收集。
- `TestsetRunner`：测试集运行是**耗时操作**且用户可能离开页面，因此**由后端后台任务驱动**（`asyncio.create_task(_drive_segments)`），运行记录保存在内存、可轮询 / 找回 / 取消；每处状态变更（启动 / 步骤置 running/done/error / 终态 / abort）广播 `{"type":"testset","run_id",run}` 运行全量快照，前端经 SSE 实时推进；`start_run(testset, sessions)`（**无 mode**，发送节奏由 `testset["batch_ranges"]` 决定）：
  - `_segments(run)` 把消息按 batch_ranges 切分为**单步段**（`[i]`，段外消息）与**批量段**（`[s..e]`，段内消息）；段之间按 start 升序、互不重叠，逐段驱动。
  - **单步段**：发 → 等该步全部会话完成（`runner.wait_done(timeout_secs=TESTSET_STEP_TIMEOUT)`）→ 再发下一步（上下文连续）；超时 / 异常 → 该步 error、run error、**中止后续**（同原 sequential 语义）。
  - **批量段**：先全部 `start`（各步置 running、记 test_id），再逐个 `wait_done`；段内单步超时 / 异常 → 该步 error、继续收其余步；**段内错误不中止后续段**（同原 batch 语义）。
  - abort 只置标记、不 cancel 任务：当前步骤照常完成并收结果，后续步骤不再发；段与段之间、批量段等待循环内都检查 `run["status"]`。
  - 全部段结束后：`run.status = 有 error 步 ? "error" : "done"`（仅当未 abort）。
  - 单步超时安全阀 `TESTSET_STEP_TIMEOUT = 600`；运行记录按 `DONE_RUN_KEEP_SECONDS`（10min）/ `STALE_RUN_TIMEOUT`（1h）清理，`start_run` 与 `list_runs` 时 `_prune_runs()`。
  - `status(run_id)` / `list_runs(limit=10)`（倒序摘要，页面重开找回；摘要无 mode 字段）/ `abort(run_id)`。

### 事件驱动（event_bus.py + /events SSE）

- `EventBus`：进程内事件广播（纯 asyncio，无第三方依赖）。订阅者各持独立有界队列（maxlen=1000），满则丢最旧、不阻塞发布者；事件均为 JSON 可序列化的**全量快照**（幂等，丢旧无碍）。
- 事件类型：`{"type":"pending","entries":[...]}`（在途全量快照）、`{"type":"session_done","test_id","session_id","summary"}`、`{"type":"test_done","test_id","record":status(test_id)}`、`{"type":"testset","run_id","run":status(run_id)}`。
- **快照须为发布时刻的拷贝**：pending 条目逐条 `dict(e)`、run 用 `deepcopy`——条目 / run dict 之后被原地更新，不拷贝则已发布事件会漂移成最新状态（曾因共享引用导致首条快照直接显示终态）。
- main.py 的 `/events` 端点以 SSE 推送（15s 心跳注释行防代理断连）；前端 `subscribeSSE("events")` 订阅，断线后延迟重连 + `reconcileEvents()` 以轮询接口一次性快照对账（getPending + 在途各 test_id 逐个 runStatus + 有活动运行则 runTestsetStatus）。
- 测试集运行与手动群发共用同一 `applySessionFeedback` 逐会话反馈路径（面板状态 + 历史刷新），行为一致。

### 前端（pages/testbench/）

- `api.js`：`window.AstrBotPluginPage` bridge 的 `apiGet`/`apiPost` 统一封装（bridge 的响应解析是 `response.data?.data ?? response.data`）+ `subscribeEvents`/`unsubscribeEvents`（`subscribeSSE("events")` 的单一全局订阅封装，断线时自动置空，由页面延迟重连）。
- `state.js`：全部共享可变状态收进一个 `state` 对象（groups/platforms/confs/openIds/pinnedIds/panelEls/historyCache/expandedGroups/expandedSessions/testsets/selectedTestsetId/activeRunId/pendingEntries/runReports/latestReportRunId/testsetReportedSteps）。ES module 顶层绑定无法跨模块共享可变值，故集中到叶子模块，各模块从 `state` 读写，保持依赖单向。
- `utils.js`：纯工具与配置解析（`escapeHtml`/`statusText`/`confName`/`platformName`/`findSession`/`effectiveView`），唯一依赖 `state`。`effectiveView` 是后端 `effective()` 的客户端镜像（曾漏 sender 字段导致显示「—」，有防回归测试）。
- `modal.js`：自绘弹窗（iframe 沙箱禁用原生 alert/confirm），回调状态 `modalCallback` 封装在模块内部，对外只暴露 `openModal`/`showModal`/`hideModal`。
- `group_list.js`：左侧测试组列表与组/会话配置弹窗。`createGroupList(env)` 依赖注入视图动作（toggleOpen/openAll/deleteSession/renderPanels/showRunStatus/updateRunOverview），本模块不 import app.js，模块依赖保持单向。`platformOptions()`/`confOptions()` 是平台/档案下拉选项的共享构建（含「档案已不存在」占位，防静默丢绑定）。
- `testset_list.js`：左侧测试集列表、**右侧编辑窗口**、运行弹窗、导出 / 导入与最近运行。`createTestsetList(env)` 依赖注入视图动作（showRunStatus/runTestset/viewTestsetRun/switchToTestsets），同样不 import app.js。列表条目是**有名字的条目**（点击选中后右侧打开编辑窗口，不再内联展开）；编辑器按行维护「文本 + 断言类型 + 断言值 + 批量勾选」，**`renderMsgRow` 单行构建 + `collectEditorRows` 反向收集是唯二变化点**，`RULE_TYPES` 定义断言类型选项（无 / contains / not_contains / regex / json / non_empty / min_len / max_len / prefix / suffix）为唯一来源，未来新增测试行为只改这两处 + 后端 `_normalize_messages`；连续勾选「批量」的消息合并为批量段（`collectEditorRows` 丢弃空文本行后按索引连续性合并，`batch_ranges` 引用保留后的消息索引），`#ts-segments` 实时显示段摘要；**脏标记** `dirty`（任一行输入 / 勾选变化置位，切换测试集 / 导入 / 导出 / 运行前确认，`refreshTestsets` 在 dirty 时跳过编辑器重渲染以免异步刷新清掉未保存修改；**未选中任何测试集时编辑窗口按钮（添加消息 / 保存 / 运行 / 导出）仍可见，点击须经 `requireSelected` 给指引提示而非静默无效**）。导出走 Blob `<a download>`（iframe 沙箱含 `allow-downloads`），信封 `{format: "astrbot-testbench-testset", version: 1, name, messages, batch_ranges}` 预留「测试集市场」下载兼容；导入复用 `createTestset` 端点（无新后端接口），`parseTestsetEnvelope` 校验 format/version≤1/name/messages/batch_ranges。运行弹窗目标**三选**：已打开的 / 全部 / 选择测试组（`buildGroupCheckboxes` 组多选 → `selectedGroupSessionIds` 把勾选组解析为该组全部会话 id），`env.runTestset(testset, ids)` 只收会话 id 列表。最近运行条目标注状态 chip（运行中/完成/错误/已取消），运行中条目也可点「查看」续轮询。
- `chat.js`：`createChatRenderer(alignGetter)` 集中聊天内容渲染（气泡 `bubbleFor` / 思维链 `reasoningSection` / 工具调用 `toolCallBlock` 与工具返回 `toolResultBlock` / 轮次分组 `groupTurns` 与对齐渲染 `renderAligned`）。align 以 getter 注入（渲染时才取），避免与 `createAlignController` 互相创建的循环依赖；`app.js` 提供 `renderChat` 包装函数注入 align 控制器并传给 align.js 的 env。
- `app.js` 分区：面板（openPanel/loadHistory/历史 JSON 编辑/重新生成）→ **事件驱动反馈**（`connectEvents` 订阅 SSE；`handleEvent` 分发 pending / session_done / test_done / testset；`registerTestConsumer(test_id, onSession, onAll)` 注册逐会话消费者，`applySessionFeedback(summary)` 统一逐会话反馈：面板状态 + 历史刷新——手动群发与测试集共用；`reconcileEvents()` 断线/初始化一次性快照对账）→ 发送（`sendToOne`/`sendToAll`/`regenerateMsg` 经 `registerTestConsumer` 挂消费者，`updateRunOverview`）→ 会话操作（重置/删除）→ 面板排序（renderPanels/拖拽/置顶）→ **rail 视图切换**（`showView`：同时驱动左侧列表 `.groups-card` / `.testsets-card` 互斥 **与右侧视图 `.sessions-view` / `.testsets-view` 互斥**——左侧选择自动切换右侧视图、不再手动切换；rail 按钮 active 互斥，点当前视图 toggle 折叠，切到测试集时 `refreshTestsets()`）→ **测试集运行编排**（`runTestset(testset, ids)` 一次启动后端运行（**无 mode**，启动文案含批量段摘要 `segmentSummary`）→ `handleTestsetEvent(run_id, run)` 消费事件流：running 时进度文案按 `segmentLabel` 标注「第 s+1–e+1 步（批量）」、逐步骤显示，**新完成步骤逐结果 `applySessionFeedback` 实时刷新面板**；终态**不弹窗**、暂存 `state.runReports[runId]` + `latestReportRunId`、显示「查看报告」按钮（`showTestsetResults` 仅按需调用）；**终态总结与表格行尾都单独统计断言未通过（`assertFails`/「断言 ✗ N」）**——断言失败不改会话 status，若总结只数 `status=="error"` 会显示「错误 0」而表格一片 ✗，误导用户；`showTestsetResults` 表格行=步骤、列=会话、单元格含断言 ✓/✗、批量段步骤带「批量」徽标 `batchBadge`；`viewTestsetRun` 从「最近运行」找回，运行中一次性重建进度后靠事件流续推；`abortTestsetRun` 请求取消，当前步骤完成即止；`runTestsetFromBar` 读 `#run-testset` 下拉对已打开会话执行）→ 选项加载 → 初始化（组装 align/chat/group_list/testset_list，绑定全局事件；`#run-testset` change 启用 / 禁用执行按钮；末尾 `void connectEvents()`）。`loadOptions()` 拉取 platforms/confs 写入 `state`；`refreshGroups()` 来自 group_list（刷新左侧列表并清理失效面板）。
- 群发**不阻止重叠发送**（真实「重复追问」场景，与真实平台一致由 pipeline 并发处理）；每个面板底部有在途消息条（`.panel-pending`），订阅 SSE 事件流后以 `pending` 全量快照重建 `state.pendingEntries`、`renderPendingStrip` 按会话渲染「已入队 / 排队等待 LLM / LLM 生成中 / 完成」chip（`PENDING_STATUS_TEXT`），`getPending()` 供断线对账；**完成且已刷入会话历史的消息即从条内移除**（`loadHistory` 成功记录 `historyRefreshedAt`，过滤掉 `status=="done"` 且完成于该时刻之前的条目，条内只留真正在途与完成后的短暂过渡）；strip 在 `.chat` 外不干扰轮次对齐，显隐变化后按需 `reflowAlign()`。
- 左侧布局：最左为 `.ui-rail` UI 窄条（方形按钮，当前 2 个「会话列表」/「测试集」，点击切换视图，点当前视图按钮折叠/展开侧栏，为后续扩展预留）；侧栏有两个互斥视图：`.groups-card`——「＋ 新建测试组」块（点击创建默认配置组并弹编辑弹窗）+ 测试组块（可展开组内会话），组头操作：打开全部 / ＋新增 / ✎编辑 / ✕删除，会话行头点击展开配置（`renderSessionConfig` 每行显示有效值 + 「已修改/继承组」chip，`sessionOverrides` 统计已单独修改的项），会话操作按钮：打开 / 删除（配置修改走展开配置中的「编辑配置」弹窗，「重置」在已打开会话的面板页眉）；`.testsets-card`——「＋ 新建测试集」块 + **纯命名条目**（名字 + N 条消息徽标 + 选中高亮，无内联展开 / 无行内操作按钮，点击 → `selectTestset` 打开右侧编辑窗口）+ 底部「最近运行」区。
- 工作区（`.workspace`）为 flex 列布局：顶部 `#workspace-strip` 常显状态条（运行状态 + 取消按钮，两个视图下都常显）；`.sessions-view`——`.panels-block`（包裹已打开会话面板 + 空态提示，flex:1）、`#align-bar`、`.run-bar` 两行（第 1 行群发输入 + 发送到全部 + 轮次对齐；第 2 行 `#run-testset` 测试集下拉 + `#btn-run-testset` 执行 + `#run-overview`）；`.testsets-view`（初始 hidden）——`.testset-editor` 编辑窗口。`.sessions-view` / `.testsets-view` 必须带 `[hidden]{display:none}` 特例（flex 列元素与 HTML hidden 的已知陷阱，沿用 groups-card）。
- 面板页眉为多行 flex 布局：标题与徽标包在 `.panel-info`（`flex-wrap: wrap`）内随内容换行撑开页眉，`.panel-actions` 始终第一行右对齐。操作按钮收敛为「⋯」下拉菜单（`.panel-menu` / `.panel-menu-dropdown`，`setupPanelMenu` 切换显隐、document 级点击外关闭）+ 常显「置顶 / 关闭」：菜单项经 `data-action` 分发——`history`（JSON 编辑器，即原「编辑」按钮）、`reset`（重置历史）、`copy`（复制历史到 `state.clipboard`，去掉 conversation_id 使粘贴行为可预测）、`clone`（`promptCountDialog` 数字弹窗 → `cloneSessionApi`，同组新建 N 个历史一致的会话）、`paste`（有剪贴板内容才可用，danger 确认后经 `saveHistory` 整体覆盖）、`derive`（带命名与计数弹窗 → `deriveSessionApi`，创建全新测试组）；克隆 / 衍生后 `refreshGroups()` 刷新左侧列表。`refreshPanelHead()` / `openPanel()` 都维护该结构。
- 气泡渲染 `bubbleFor(msg, index, ctx)` 用 `extractParts(msg.content)` 拆分**思维链**（`ThinkPart`，`{type:"think", think:"..."}`）与正文：带推理内容的回复渲染 `.reasoning-wrap`（原生 `<details>`，默认收起，summary 即「展开/收起思维链」按钮）；旧格式的 `assistant_reasoning` / `reasoning` 角色整条按思维链处理。`<details>` 的 `toggle` 事件在轮次对齐模式下 `requestAnimationFrame(() => align.reflowAlign())` 重排高度。
- **工具调用 / 工具返回气泡**（OpenAI 格式历史：助手消息经 `msg.tool_calls`（`{id, function:{name, arguments}}` 数组）携带工具调用，content 部件只有 text/think/image_url/audio_url，工具调用不作为 content 部件；返回为 `role:"tool"` 消息 + `tool_call_id`）：助手消息逐个渲染 `.tool-call` 气泡（`<details>`，summary 即工具名、默认收起，展开显示 `prettyArgs` 美化后的参数 JSON；非 JSON 参数原样）；`role:"tool"` 渲染 `.tool-result` 气泡（头部经 `ctx.toolNames` 用 `tool_call_id` 关联标注「工具返回 · <工具名>」，正文即返回内容）。`ctx = {toolNames: {}}` 是每次 `renderHistory`/`renderAligned` 调用共享的渲染上下文（跨消息收集 id → 工具名），两次渲染循环都创建并传给 `bubbleFor`；`toolCallBlock`/`toolResultBlock` 的 `<details>` toggle 同样在轮次对齐模式下重排高度。思维链内出现工具调用（think 部件与 tool_calls 同消息）时，工具调用以独立气泡紧随思维链渲染，不再以裸文本挂在思维链下。
- 对话历史编辑走**面板头部「历史」按钮的 JSON 编辑器**（`openHistoryEditor`）：直接编辑 `{conversations: [...]}` 全结构，保存调 `saveHistory` 整体替换（编辑/新增/删除对话都在 JSON 里完成）；单条气泡只保留「重新生成」。不做复杂的单轮编辑 UI——没有能力修改结构化历史的用户不建议自己改。
- `align.js`：`createAlignController(env)` 依赖注入访问器（getOpenIds/getPanelEls/getHistoryCache/getPanelsEl/renderChat），实现轮次对齐 + 滚动同步。
- `index.html` 的 `<select>` 是静态骨架：`create-platform` 初始为空（`loadOptions()` 填充后默认项为「默认（webchat）」）、`create-conf` 只有 `<option value="">默认配置</option>`，**必须由 loadOptions() 填充**。
- 平台下拉默认项为「默认（webchat）」（对应后端 `DEFAULT_PLATFORM_ID="webchat"`，空值即使用默认）；真实平台来自后端 `platforms` 接口（`platform_manager.platform_insts`，含 webchat）。

## Web API 一览（main.py）

全部经 `context.register_web_api(f"/astrbot_plugin_testbench/...")` 注册：

| 方法 | 路径 | 处理器 | 说明 |
|---|---|---|---|
| GET | /providers | list_providers | LLM Provider + 模型列表 |
| GET | /confs | list_confs | 配置档案列表（`astrbot_config_mgr.get_conf_list()`） |
| GET | /platforms | list_platforms | 已启用平台适配器（防御式：单实例失败跳过） |
| GET/POST | /groups | list_groups / create_group | 测试组列表 / 创建组（可选绑 conf_id） |
| POST | /groups/delete | delete_groups | 删组 + 联动清路由与原生对话历史 |
| POST | /groups/\<id\>/sessions | add_group_sessions | 组内新增会话 |
| POST | /groups/\<id\>/update | update_group | 更新测试组配置（组配置变更同步应用到仍继承组配置的会话） |
| GET | /sessions | list_sessions | 全部会话（已解析最终配置） |
| GET | /sessions/pending | session_pending | 在途测试消息的实时状态（已入队/排队等待 LLM/LLM 生成中/完成） |
| POST | /sessions/update | update_session | 会话配置覆盖（null 恢复继承组配置） |
| POST | /sessions/delete | delete_sessions | 删会话 + 联动清理 |
| POST | /sessions/clone | clone_sessions | 克隆会话：同测试组内新建 N 个会话并拷贝其对话历史（count 1-500，组容量上限 MAX_SESSIONS_PER_GROUP=500） |
| POST | /sessions/derive | derive_session | 衍生会话：基于某会话历史创建全新测试组（组内会话历史一致，可命名 / 指定会话数，默认名「\<组名\> 衍生」） |
| GET | /sessions/\<id\>/history | session_history | 对话历史（LLM 上下文消息列表） |
| POST | /sessions/history/save | save_history | 整体替换对话历史（带 cid 更新、无 cid 新建、带不存在的 cid 也新建占位对话、未列出删除；JSON 编辑器保存） |
| POST | /sessions/history/regenerate | regenerate_history | 截断该轮之后历史并重发该轮 user 消息 |
| POST | /reset | reset_sessions | 重置会话对话历史 |
| POST | /test/run | run_test | 投递消息，立即返回 test_id |
| GET | /test/run/status | test_run_status | 查询运行状态（含统计） |
| GET/POST | /testsets | list_testsets / create_testset | 测试集列表 / 创建测试集（消息序列可带断言规则；允许空消息先建条目；可选 batch_ranges 批量段，非法 → 400） |
| POST | /testsets/delete | delete_testsets | 删除测试集 |
| POST | /testsets/\<id\>/update | update_testset | 更新测试集（名称、消息序列与批量段整体替换；batch_ranges 缺省为 []） |
| POST | /testsets/run | run_testset | 启动测试集运行（仅 `{testset_id, sessions}`，无 mode；后端后台任务驱动，立即返回 run_id） |
| GET | /testsets/run/status | testset_run_status | 查询测试集运行进度与结果（逐步骤） |
| POST | /testsets/run/abort | abort_testset_run | 请求取消测试集运行（当前步骤完成即止） |
| GET | /testsets/runs | testset_runs | 最近测试集运行摘要列表 |
| GET | /events | events | SSE 事件流（在途/会话完成/测试完成/测试集进度实时推送） |

统一用 `astrbot.api.web` 的 `json_response` / `error_response` / `request`；`events` 返回 `StreamingResponse`（media_type `text/event-stream`，15s 心跳注释行防代理断连）。

## 测试与验证

> **开发流程（2026-08-05 起）**：本地**不跑**测试，修改直接提交推送到 `dev` 分支，
> 由 GitHub Actions 自动把关——push 到 dev 触发 `pytest.yml`（143 个测试 +
> 前端 JS 语法检查 `js-check`：node --check 九个页面脚本）+ `ruff-format.yml`；
> dev 验证通过后合并到 `main`，metadata.yaml 变更即触发 release.yml 自动发版。
> 本地命令（下面的 pytest/ruff）仅在需要主动排查时使用。

测试随插件仓库维护（`tests/`，可与主仓库无关地推送、供协作者运行）。

- `tests/test_backend.py`：后端单元测试（124 个），需要 astrbot（PyPI 包，插件运行时依赖）。以 **namespace package** 加载插件：`sys.path.insert(0, str(REPO_ROOT.parent))` 后 `import astrbot_plugin_testbench.*`——插件模块用相对导入（`from .group_store import ...`），必须按包加载，这与 AstrBot 在 data/plugins 下加载插件的方式一致。未安装 astrbot 时整组跳过（`pytest.importorskip`）。
- `tests/test_frontend.py`：前端脚本静态检查（19 个），零依赖，任何环境可运行。

本地运行（用主仓库 venv，bash cwd 不稳定，命令先 `cd /e/AstrBot` 或 `git -C` 插件目录）：

```bash
cd /e/AstrBot && .venv/Scripts/python.exe -m pytest data/plugins/astrbot_plugin_testbench/tests/ -q
cd /e/AstrBot && .venv/Scripts/python.exe -m ruff check data/plugins/astrbot_plugin_testbench/tests/
```

CI（`.github/workflows/pytest.yml`）：ubuntu + Python 3.12，`pip install astrbot pytest pytest-asyncio` 后跑 `pytest tests/ -q`，随 push/PR 触发。

前端 ES module 语法检查（node 不认 .js 里的 import，须复制为 .mjs；页面全部 9 个模块逐一检查，与 CI js-check 一致）：

```bash
for f in app api align chat state utils modal group_list testset_list; do
  cp "$f.js" "$TEMP/$f.mjs" && node --check "$TEMP/$f.mjs"
done
```

测试设施（tests/test_backend.py 内定义）：`FakeContext`（可注入 queue/ucr/conv_mgr/platform_mgr）、`FakeUCR`、`FakeConvManager`、`FakePlatformManager`/`FakePlatformInst`、`call_handler`/`make_plugin_request`（绑定 PluginRequest 调 handler）、`_add_history`（造对话历史）。

## 常见陷阱（本插件踩过的坑）

1. **ES module import 与顶层声明重名 → 整页 JS 解析期失败**：`import { createGroup }` 与 `function createGroup()` 重名会抛 `SyntaxError: redeclaration of import`，模块完全不执行（下拉框看起来"数据没加载"）。重构 api.js 时曾引入此 bug（commit 9c55c41 修复）。任何新增 import 都要检查与 app.js/align.js 顶层声明是否冲突；`test_frontend_no_import_redeclaration` 是防回归测试。
2. **git 命令必须用 `git -C /e/AstrBot/data/plugins/astrbot_plugin_testbench`**：插件目录是独立仓库，而 bash 的工作目录在工具调用间不稳定。
3. **平台/配置下拉框空 ≠ 后端故障**：先看浏览器控制台是否有 JS 报错（整页脚本失效时 `loadOptions()` 从未执行）。
4. **`all(await x for ...)` 不可迭代**：async generator 不能直接喂给 `all()`，需列表推导收集后再判空。
5. **防御式读取后端资源**：`list_platforms` 对每个适配器 `meta()` 单独 try/except（单个异常不拖垮整个接口），前端对返回做 `Array.isArray` 校验。
6. **Windows 路径**：venv 在 `E:\AstrBot\.venv\Scripts\python.exe`（不是 `bin/`）；git bash 里可用 `/e/AstrBot` 风格路径。
7. **metadata.yaml 版本**：未经用户明确批准不得 bump；发布包由 `.github/workflows/release.yml` 构建，须排除 `data/`（v0.2.1 曾误打包本地运行数据）。
8. **按钮点击无反应先查 JS 运行时错误**：点击处理函数内抛 ReferenceError（如引用未定义变量）会静默失效——弹窗根本没打开、无任何可见反馈，浏览器控制台有报错；node --check / pytest 都发现不了。曾因此导致会话「配置 / 编辑配置」按钮长期无效（`openSettings` 的弹窗标题用了未定义的 `s`，作用域内实为 `session`，v0.3.0 引入）。
9. **顶层语句引用先于 const/let 声明 → 整页初始化中止（暂时性死区）**：`node --check` 只查语法，发现不了「引用先于声明」的顺序错误。模块求值时抛 `ReferenceError: can't access lexical declaration 'x' before initialization`，此后的初始化代码全部不执行（页面只剩静态骨架、按钮绑定全部失效）。拆分 app.js 后曾把 `$("btn-refresh").addEventListener("click", refreshGroups)` 放在 `const { refreshGroups, renderGroupList } = createGroupList(...)` 之前（拆分前 refreshGroups 是可提升的 function 声明，拆分后变成 const 解构绑定）。教训：**模块级 `const`/`let` 的引用必须位于声明之后**；函数体内的引用不受影响（调用时才求值）。`test_frontend_no_use_before_declaration` 是防回归测试——它只查顶格（列 0）语句，避免对函数体内引用的误报。
