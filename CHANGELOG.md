<!-- markdownlint-disable MD024 -->
<!-- markdownlint-disable MD025 -->
<!-- markdownlint-disable MD033 -->
<!-- markdownlint-disable MD034 -->
<!-- markdownlint-disable MD041 -->
# ChangeLog

## [v0.4.3] - 2026-08-07

### ⚡ Performance (性能优化)

- **身份库 sender_id → is_admin 惰性索引（Phase 3）**：`IdentityStore` 新增 `is_admin_of(sender_id)`——按 `sender_id` 惰性构建管理员身份索引（`create_identity` / `update_identity` / `delete_identities` 写操作后失效、下次查询重建），runner 的 `_resolve_role` 改走该 O(1) 查询，不再在每次发送时对身份库全量线性扫描（身份数量增长时避免每次群发都 O(n) 比对）。语义不变：任一身份命中（`sender_id` 精确匹配且 `is_admin` 为真）即按管理员处理，未命中 / 旧数据缺 `is_admin` 键一律普通成员。

---

## [v0.4.2] - 2026-08-07

### 🧹 Chores / Refactoring (重构与工程化)

- **前端纯函数抽取到零依赖模块 `pure.js`，新增 node:test 动态测试（Phase 2）**：把 `testset_editor.js` / `testset_run.js` 的纯逻辑（`collectEditorRows` / `parseScope` / `parseTestsetEnvelope` / `buildRule` / `collectRules` / `rangesFromFlags`、verdict 计数 `ruleFailCount` / `ruleReviewFailCount`、段文案 `segmentLabel` / `segmentSummary` 与配套常量 `RULE_VALUE_TYPES` / `EXPORT_FORMAT` / `EXPORT_VERSION`）抽到 `pages/testbench/pure.js`——不引用 DOM / state / 其它模块，页面模块经 `import` 复用同一份实现（防两处漂移），并新增 `tests/frontend/pure.test.mjs` 用 Node 内置 `node:test`（零依赖）做行为测试；`pytest.yml` 的 js-check job 追加 `node --test` 命令，仓库根新增 `package.json` 声明 `type: module`（node 可直接加载页面 ES module，无 npm 依赖）。

---

## [v0.4.1] - 2026-08-07

### ✨ New Features (新功能)

- **持久化写路径去阻塞**：全部 store（测试组 / 身份 / 群聊 / 测试集 / 评审 profile / 报告）的同步写方法保持同步签名，API 层与异步编排（测试集运行报告生成）经新增 `AsyncWriteMixin.write`（`store/_base.py`）**锁内线程化**执行——`asyncio.to_thread` 把全量写 JSON 移出事件循环线程（不再阻塞 LLM / 消息处理），实例级 `asyncio.Lock` 串行化 read-modify-write + 落盘（防并发写丢更新）。群消息流（`StreamStore`）改为 **JSONL 追加写**（`streams.jsonl`）：磁盘只记 `append` / `reply` 两种 op 记录，`clear` / 删除会话 / 压缩时才全量重写（`reply_status` 烘焙进消息），行数达阈值（10000）自动压缩——热路径不再反复全量重写整个文件。测试集运行终态广播增加一次**报告生成完成后的终态快照**：`_drive_segments` 先广播终态（`report_id` 尚为 None），`_drive` finally 生成报告后重发终态快照，前端 SSE / 轮询者能拿到含 `report_id` 的最终快照。
- **评审 Profile 表单新增 `{{metrics}}` 展开内容实时预览与复制**：编辑输出指标时，表单实时显示 `{{metrics}}` 占位符展开后的完整内容（逐字段取值要求 + 示例输出，经新接口 `POST /reviewers/preview` 由评审运行时的 `metrics_contract_description` 直接计算，保证预览与实际展开字节级一致、前端不镜像格式化逻辑）；预览与占位符**共存**——`{{metrics}}` 照常自动展开，也可点「复制预览」把展开内容复制到提示词手动修改后使用（任选其一或同时存在）。复制按钮带剪贴板回退：`navigator.clipboard` → `document.execCommand("copy")` → 选中预览内容提示手动 Ctrl+C（插件页 iframe 沙箱未授予 `allow-clipboard-write`，自动复制可能被拒）。表单同时新增 **`{{agent_system_prompt}}` 占位符提示**（展开为被测 agent 的装饰后系统提示词，未捕获时为空串）。
- **LLM 评审材料改为结构化「实际输入 + 输出」**：单轮（reply 模式）评审不再只给回复文本——现在包含该轮完整输入与输出，并用中文标签块明确标注身份与分界（`【输入 · user（发送者）】` / `【输出 · agent（virtual_bot）】`，多轮 record / slice 模式每轮带「第 N 步:」前缀、轮间空行分隔）。材料中的输入是**实际喂给被测 LLM 的输入**而非测试集原始文本：插件经 AstrBot 原生 LLM 阶段 hook（`on_llm_request`）在调用前快照装饰后的 `req.prompt` 与 `req.extra_user_content_parts`（框架 / 插件注入的 `<system_reminder>`、知识库结果、附件标记等），随结果摘要（`llm_input`）进入评审上下文；未捕获到快照时回退原始文本。快照在 hook 处渲染为纯字符串，不存组件对象引用（随 SSE 事件与报告 JSON 序列化）。同时评审 profile 的 `system_prompt` 新增 `{{agent_system_prompt}}` 占位符——展开为被测 agent 的装饰后系统提示词（来自同一快照），由用户在评审提示词中自行编排；未捕获时展开为空串（不残留字面量占位符）。verdict 持久化存储 `agent_system_prompt`，报告评审重试（`/reports/<id>/reviews/retry`）重跑时自动透传，评审材料不包含被测 agent 的 system_prompt。
- 新增**主动消息与异步补发检测警告**：虚拟会话无法收到插件主动发送的消息（定时任务到点触发的问候 / 话题等），此前这类回复被静默漏掉、测试结果呈「无回复」且无任何提示。现测试台在运行启动时**探测 cron 定时任务**（AstrBot 公开 API `list_jobs` / `get_next_run_time`）：active_agent 任务的 `payload.session` 投递目标与虚拟会话 umo 精确匹配、basic 任务 payload 做浅层启发式扫描（深度 2，命中虚拟 umo / 会话 id 才警告），命中项作为**运行级警告**（含任务名 / 类型 / 表达式 / 投递目标 / 下次运行时间与说明）随运行记录与事件流呈现——手动群发启动前同步探测、测试集运行启动后后台探测，前端在运行状态条（「⚠ N 条主动消息警告」）、结果表格弹窗顶部警告块与面板状态中提示。检测是增强：cron_manager 未初始化 / 枚举失败一律降级为无警告，不破坏测试本身。同时 runner 新增**异步补发检测窗口**：pipeline 结束后再观察一小段时间（生产装配 1s，构造缺省 0 保持旧行为与测试速度），窗口内 fire-and-forget 补发的回复**不计入结果**（摘要已冻结），仅在面板状态行标记「pipeline 结束后又有 N 条回复到达（插件异步补发）」——如实告知有回复被漏掉；检测不是捕获，长延时 / 外部触发来源无法覆盖，其计划根源由 cron 探测兜住。
- 评审 Profile 放开为**多个实例**并把管理入口迁到左侧测试集列表：左侧「测试集」卡片以 tab 拆分「测试集 / 评审 Profile」（镜像身份实体与身份池的列表 + modal 表单管理模式），profile 的创建 / 编辑 / 删除在列表中完成（每条显示 Provider · 模型 · 评审上下文 · 指标数徽标 + 编辑 / 删除）；不再限制只能有一个 profile——消息规则 / 最终断言按 `profile_id` 引用，创建第二个不再被 API 拒绝；编辑窗口内的 Profile 摘要区随之移除，消息规则 / 最终断言行内 profile 下拉在 profile 增删改后自动重建（防 stale）。
- 测试集消息模型升级：断言规则由单条 `rule` 扩展为 **`rules` 规则列表**（每条消息可配多条断言，按全部通过判定；旧数据与旧信封单条 `rule` 自动迁移），新增**命令标记 `is_command`**（预期触发框架行为而非 LLM 回复的消息，勾选后随消息持久化）；编辑器消息行改为「文本行 + 下方多断言列表」结构（类型下拉 / 断言值 / 删除 + 「＋ 断言」添加，min_len/max_len 值须为整数，空值规则保存时静默丢弃并带行号提示）。
- 测试集新增**身份配置**：可设**单一身份**（single，整集消息恒用该身份发送，消息级身份下拉随之隐藏）或**身份池**（pool，消息行身份下拉列出池成员、按成员选择）；保存时把被引用身份的完整数据**内联快照**进测试集（自包含，身份 / 群聊删除后测试集仍可运行），单身份快照含 `is_admin`（管理员身份发送时 `event.role` 自动为 `"admin"`）、身份池快照为群聊名 + 成员身份列表；测试集运行记录记录每条消息的解析后发送者与管理员标记。
- 测试集导出 / 导入信封升级到 **v2**：信封携带身份配置（single → `identity` 快照；pool → `pool` 身份池），导入复用创建端点、不创建身份 / 群聊记录（内联快照直接可用）；解析兼容 v1（单条 rule → rules 列表）与 v2（rules / is_command / identity / pool），版本号高于当前版本拒绝。
- 新增**身份管理员配置（is_admin）与发送时自动角色**：测试身份实体新增「是否管理员」勾选（新建默认非管理员，旧数据零迁移）；发送虚拟消息时按发送身份是否为管理员自动设置 `event.role`（管理员 → `"admin"`，否则 `"member"`）——此前虚拟事件恒为 `"member"`，开启 `computer_use_require_admin`（默认开启）的配置下虚拟会话永远无法通过计算机工具的管理员权限门控，现可按身份模拟不同权限身份。管理员身份在身份列表以徽标标注，并带「⚠ 危险」警告提示（管理员可调用需管理员权限的工具、可能执行危险操作），身份表单勾选管理员时即时显示内联警告条；身份表单管理员单选框改为 checkbox 在前、与标签同行显示（不再独占一行），群聊编辑视图成员行同时显示昵称与发送者ID，管理员成员挂「管理员」+「⚠ 危险」徽标。
- 新增**工具安全警告**：创建 / 编辑测试组与会话级配置档案覆盖时，若所选配置启用了任何**可调用的工具**（`computer_use_runtime` 为 local/sandbox、`web_search`、`kb_agentic_mode`、`proactive_capability.add_cron_tools`——缺省即开启，故默认配置本身即命中），弹窗内即时显示内联警告条（不阻塞提交）；测试组列表按组与全部会话的**有效配置**实时计算安全标记（⚠ 工具），配置档案事后内容修改会随列表刷新更新（标记派生、不持久化）；`GET /confs` 每个档案新增 `has_callable_tools` 布尔。
- 新增**群聊虚拟会话**支持：测试组 / 会话新增「消息类型」配置（私聊 FriendMessage / 群聊 GroupMessage，默认私聊、旧数据零迁移），群聊消息的 umo 变为 `平台:GroupMessage:会话id`——只监听 `GROUP_MESSAGE` 的插件（如主动回复类插件）现在能被虚拟会话触发；消息类型变更视同 umo 变更，自动清理旧 umo 的路由与对话历史。
- 新增**自动@（auto_at）**（群聊消息，默认开启）：开启时模拟「@机器人 发言」，消息链以 At(机器人自身) 开头、消息文本保持纯文本，唤醒检查直接命中（`is_at_or_wake_command` 置位）；关闭时消息以未唤醒状态进管道，只能被 filter 通过（如 Heartflow）唤醒——用于测试「不 @ 就不回复」的主动回复插件。auto@ 是**发送时选项**而非组/会话配置：群发栏新增「自动@」勾选框（默认开启，群聊消息生效），测试集每条消息也可单独配置是否@（消息行 @ 勾选框，导入导出信封保留 auto_at 字段、旧文件缺省按开启），旧组/会话配置里的 auto_at 键自动清理。
- 新增**测试身份**（`name / sender_id / sender_name` 三元组）与**虚拟群聊**（名称 + 成员池 `member_ids`）两个跨测试组共享的持久化资源，经 UI 窄条第三个按钮「身份与群聊」视图集中管理；群聊测试会话可绑定一个虚拟群聊，投递消息时自动取成员池首个身份作为默认发送者（优先级：消息级身份 > 绑定群聊默认成员 > 会话/组手动 sender > 默认 testbench）。
- 「身份与群聊」视图交互改型：左侧卡片内以 **tab 拆分**「身份」/「群聊」两个列表（身份膨胀后群聊列表不被长列表挤走）；**创建虚拟群聊只填名称**（成员多选移出弹窗——成员多时窗口放不下），成员管理移到新增的右侧「群聊编辑」视图——搜索身份池（按名称 / 发送者ID / 昵称实时过滤、已在群内成员排除）点「＋ 加入」、成员行可「✕」移除、名称可改；删除群聊后编辑视图回到空态。
- 群聊编辑视图界面优化：群成员列表与搜索结果**优先展示昵称**（sender_name，无昵称回退发送者ID，悬停显示完整信息）而非裸 ID——成员多时按昵称识别更直观；左侧身份条目**可拖拽**到右侧群聊编辑视图的成员区快速加入该群（拖入时成员区虚线高亮提示，drop 即加入；未选中群聊 / 身份已在群中给出明确提示而非静默无效）。
- 新增**群消息流持久化**：每个虚拟会话记录与 LLM 对话历史**并行**的纯消息流（user 发言含发送者身份与 @ 标记、bot 回复，按时间序，不注入 LLM 上下文），面板页头新增「LLM 历史 ↔ 消息流」切换按钮；重置会话清流、删除会话删流，单会话上限 500 条超出截断最旧。
- 新增**唤醒状态 / 无回复原因反馈**：no_reply 区分「未唤醒」（`not_woken`）与「已唤醒但无回复」（`woken_no_reply`），结果摘要携带 `wake` 字段（woken / at_or_wake / stopped / llm_requested），面板可据此判断消息是否真正唤醒机器人。
- 新增**动态身份**：群发栏身份选择器可给整批消息指定身份，测试集每条消息也可单独指定身份（消息行新增身份下拉，导入导出信封保留可选 sender 字段，向后兼容旧文件）；新增长后端接口：`GET/POST /identities`、`POST /identities/delete`、`POST /identities/<id>/update`、`GET/POST /chat-groups`、`POST /chat-groups/delete`、`POST /chat-groups/<id>/update`、`GET /sessions/<id>/stream`、`POST /sessions/stream/clear`。
- 会话视图历史中的工具调用与工具返回改为**结构化气泡**：助手消息带 `tool_calls` 时逐个渲染工具调用气泡（summary 即工具名、展开查看美化后的参数 JSON，默认收起防长参数撑开面板），不再以裸「（调用工具…）」占位符挂在思维链下——思维链内出现工具调用时（think 部件与 tool_calls 同消息）工具调用以独立气泡紧随思维链渲染；`role: "tool"` 的返回消息渲染工具返回气泡，头部经 `tool_call_id` 关联标注「哪个工具的返回」，正文即返回内容；非 JSON 参数原样显示、空返回显示占位。
- 新增「测试集」视图（UI 窄条第二个按钮）：测试集是一组连续 user 消息序列，可对单个 / 多个会话做多轮对话的纵深测试（命令 → 子命令 → 参数确认 → 结果）、提示词/插件改动后的回归（同一序列反复跑）与连发/追问压测。支持 CRUD、每条消息可带回复断言规则（正则 / 包含 / 不包含 / 合法 JSON / 非空 / 字数 / 前后缀），后端以 Python 正则评估、结果随运行返回（✓/✗）。
- 测试集支持**批量发送范围**（batch_ranges）：测试集内按消息勾选「批量」、连续勾选自动合并为一个批量段，段内消息立即连续发出（重叠）、段外消息逐条发送（等上一步全部会话完成）；可设多个批量段（每段 ≥1 条），运行发送节奏完全由测试集自身决定、不再在运行时选「逐条 / 批量」模式；侧栏「最近运行」列出历史运行，可点「查看」找回结果（含运行中的继续跟踪）。
- 测试集运行由后端后台任务驱动，与页面生命周期解耦：离开 / 刷新页面不中断后续步骤，返回后经「最近运行」可继续查看进度与结果；运行可随时「取消」（当前步骤完成即止、后续不再发）；单步 10 分钟超时安全阀防止一条悬挂消息拖死整个测试集，运行记录超时自动清理。新增 8 个 Web API：`GET/POST /testsets`、`POST /testsets/delete`、`POST /testsets/<id>/update`、`POST /testsets/run`、`GET /testsets/run/status`、`POST /testsets/run/abort`、`GET /testsets/runs`。
- 群发不再阻止重叠发送：agent 处理上一条消息时可再次群发（真实「重复追问」场景，与真实平台一致由 AstrBot 原生 pipeline 并发处理）。已打开会话面板底部新增在途消息条，实时显示每条消息「已入队 / 排队等待 LLM / LLM 生成中 / 完成」四个阶段——前三个阶段经 AstrBot 原生 LLM 阶段 hook（`on_waiting_llm_request` / `on_llm_request`）观察，只读不干预 pipeline。
- 平台来源完全移除 `virtual_test`，默认改用 `webchat`（与 AstrBot WebUI 一致）；发送者 id / 昵称默认改为 `testbench` / `测试台`。
- 群发栏新增实时统计：显示当前打开的会话数量，并标注各会话来源的测试组分布（如「当前会话:8 提示词测试组:5 模型测试组:3」）。
- 面板头部新增「编辑」按钮：以 JSON 编辑器整体查看 / 替换该会话的 `{conversations: [...]}` 结构化对话历史（编辑单条消息、新增或删除对话都在 JSON 中完成），取代原先逐条消息的编辑；气泡保留「重新生成」。按钮文案后由「历史」更名为「编辑」（功能不变）。
- 会话面板页眉改为多行布局：标题与测试组 / 平台 / 配置徽标随内容长度自动换行并撑开页眉，不再溢出截断；「编辑 / 置顶 / 关闭」操作按钮始终固定在第一行右对齐。
- LLM 回复气泡新增思维链折叠显示：带推理内容（`think` 部件）的回复默认收起，点击「展开思维链」即可查看，再次点击收起；轮次对齐模式下展开 / 收起后自动重排对齐高度。
- 左侧布局重构：最左新增 UI 窄条（方形按钮 ☰「会话列表」/ ✎「测试集」），点击当前视图按钮折叠 / 展开侧栏，点击另一视图按钮切换视图；移除「创建测试组」表单块与独立「测试组列表」块，改为单一列表——列表内含「＋ 新建测试组」块（点击即创建默认配置测试组，随后弹出编辑弹窗）+ 测试组块（点击展开组内会话）。
- 新增测试组编辑弹窗：可修改组名、会话数量（保存时若少于目标值自动新增）、平台来源、配置档案、发送者 id 与昵称；组配置变更同步应用到仍继承组配置的会话（新增后端接口 `POST /groups/<id>/update`，会话已单独覆盖的字段不受影响）。
- 会话可在列表内展开查看配置：逐项显示有效值与「已修改 / 继承组」状态标识，会话头显示「已改 N」汇总徽标；会话配置的修改统一走展开配置中的「编辑配置」弹窗。
- 左侧会话行操作收敛为「打开 / 删除」：移除「配置」按钮，「重置」按钮移至已打开会话的面板页眉（编辑 / 重置 / 置顶 / 关闭）。
- 群发栏移至工作区下方（已打开会话面板在上、群发在右下），轮次对齐滑动条位于面板与群发栏之间。
- 界面重构：右侧工作区视图随左侧列表选择自动切换（不设手动切换按钮）——☰「会话列表」→ 会话视图（已打开会话面板 + 群发栏），✎「测试集」/ 选中测试集条目 → 右侧测试集编辑窗口；测试集列表条目收敛为「有名字的条目」，创建即得到命名条目，消息编辑 / 运行 / 导出 / 删除统一在编辑窗口完成（不再条目内联展开）；会话视图顶部新增常显状态条（测试集运行进度 + 取消按钮，两个视图下都常显），已打开会话面板加一层块包裹。
- 群发栏新增测试集执行：`执行测试` 选择测试集后直接对全部已打开会话运行，进度显示在常显状态条、逐步骤刷新，完成后弹结果表格（批量段步骤带「批量」徽标）。
- 测试集运行目标新增「选择测试组」：可多选测试组，勾选组解析为该组全部会话 id（含未打开的会话）作为目标，运行弹窗只需选目标会话、不再选发送模式。
- 测试集导出 / 导入：编辑窗口可将测试集导出为 JSON 文件（带 `format` / `version` 信封，为未来「测试集市场」从网络下载兼容预留），可导入已有测试集 JSON 文件（复用创建端点，无新后端接口）；导入校验信封格式与版本，非法文件弹窗报错不创建。
- 会话面板页眉新增「⋯」菜单：编辑历史（JSON 编辑器入口，原页眉「编辑」按钮收敛进菜单）、重置历史（清空对话）、**复制历史**（复制当前会话全部对话历史到剪贴板，跨会话可用，刷新页面后清空）、**克隆会话**（同测试组内新建 N 个会话，对话历史与当前会话完全一致，可应用于同组配置下多会话对照）、**粘贴历史**（有复制内容时以剪贴板整体覆盖当前会话历史）、**衍生测试组**（基于当前会话的历史创建一个全新测试组，组内会话历史均与该会话一致，可命名 / 指定会话数，用于把一次成功调试沉淀为可复用的对照组）；新增 2 个后端接口 `POST /sessions/clone` 与 `POST /sessions/derive`（复制与衍生分别按会话 / 按组拷贝原生对话历史）。
- 新增进程内事件总线 `EventBus` 与 `/events` SSE 事件流端点：runner / testset_runner 在状态变化点广播**全量快照**事件（在途 `pending`、会话完成 `session_done`、测试完成 `test_done`、测试集进度 `testset`，快照为发布时刻的拷贝），页面经插件页面 `subscribeSSE` 实时订阅，不轮询即可看到在途 / 逐会话 / 测试集进度实时更新；15s 心跳注释行防代理断连。兼容下限提升至 `astrbot_version: ">=4.24.1"`（subscribeSSE 自该版本提供）。
- 测试组「打开全部」按钮改为**「打开全部 / 关闭全部」二态**：组内会话全部打开时按钮变为「关闭全部」，点击一键关闭本组全部已打开会话（只关属于本组的会话，其他组的会话不受影响）；任一会话在会话视图中被单独关闭后按钮自动回到「打开全部」，点击只补开尚未打开的会话。
- 左侧列表的「＋ 新建测试组」/「＋ 新建测试集」按钮移至**列表末尾**（已有测试组 / 测试集下方），列表为空时紧随空态提示之下——新增入口不再抢占列表首屏，长列表下新建入口始终固定在底部可滚动到。
- 群发栏重构为**从左到右三块**：第 1 块（窄，仅显示状态）首行左对齐当前会话总数、右对齐「轮次对齐」开关，下方按测试组逐行列出分布（每行「组名:N」，条目多时可上下滚动）；第 2 块为群发消息输入框 + 发送按钮；第 3 块为测试集选择 + 执行按钮。原先输入框与测试集选择框横跨整行过长，现收敛为各块内自适应宽度。
- 新增**评审层（LLM 断言）**：测试集从纯机械断言扩展为「机械 + LLM」两层，机械规则全部通过才调 LLM（短路控制成本与不确定性）。新增跨测试集共享的**评审 Profile**（`name / note / provider_id / model / system_prompt / context(reply|record|slice) / metrics` 指标声明——类型必须配置声明、不能运行时推断，报告聚合依赖它；system_prompt 支持 `{{metrics}}` 占位符自动展开为已收集指标；Provider / 模型为测试集级显式配置，避免用被测模型自评）——评审 Profile 的管理入口在左侧「测试集」卡片的「评审 Profile」tab（列表 + 新建 / 编辑 / 删除表单，见本文档首个新功能条目）：未配置时显示引导与新建入口，已配置时列表条目显示徽标（provider · model · 上下文 · N 个指标）+ 编辑 / 删除，删除后引用它的规则按「找不到 profile」评审失败处理；消息断言规则类型新增 **LLM 评审**（选择评审 Profile + 上下文范围「该步回复 / 该步及之前记录 / 范围切片记录」，留空用 Profile 默认），未选 Profile 的 LLM 规则保存时带行号提示（该规则不会生效）；测试集新增**最终断言（跨轮）**编辑区（每条 = 任意类型规则 + 范围「全部步骤 / 从第 N 步到第 M 步」）。运行末对所有已 done 步骤统一触发评审（评审期间运行状态保持 running、会话锁），LLM 按 Profile 提示词产出**结构化 verdict**（`status ok/error/invalid`、`pass` 布尔、类型化 `metrics`、`detail` 说明——**评审失败（LLM 调用失败 / 输出契约不符，pass 为 null）≠ 评审不通过（pass=false）**）；结果表格单元格升级为逐条 verdict 标记 ✓ / ✗ / ⚠（⚠=评审失败，悬停查看指标与详情），总结与表格行尾分别计数「断言未通过」与「评审失败」；最终断言结果在运行报告主表下方追加「最终断言（跨轮）」表（行=规则+范围、列=会话、单元格=verdict 标记）。新增 4 个 Web API：`GET/POST /reviewers`、`POST /reviewers/delete`、`POST /reviewers/<id>/update`。
- 新增**报告层（持久化报告）**：测试集可开启 `report_enabled`（缺省关闭），运行结束后把终态快照（会话 / 步骤 / 最终断言 / 指标聚合）写入 `virtual_session/reports.json` 持久化保存——与内存运行记录解耦、随测试集生命周期（删除测试集级联删除其报告），不受运行记录 10 分钟清理影响。默认模板把全部 verdicts 的 metrics 按声明类型聚合为**指标摘要**（`metrics_summary`：评审失败条数 + 各指标聚合——number 均值 / 最小最大 / 条数、enum 各取值计数、bool 通过率，文本指标不进概览），与原始数据同存一份报告。测试集编辑窗口新增「编辑 / 报告」**视图切换**：报告视图顶部「最近运行」列表（按测试集过滤，原侧栏「最近运行」区移除）、下方「报告」列表（状态 + 创建时间 + 指标概览 + 查看 / 导出 / 删除，查看弹窗复用运行结果表格与最终断言表）。新增 2 个 Web API：`GET /reports/<testset_id>`、`POST /reports/delete`。
- 新增**报告评审重试**：报告持久化了每条 LLM 评审的输入（`context_text`）与输出（`raw`），现在可重新调用评审 LLM 刷新评审结果——报告卡片新增「重试失败（N）/ 重试全部」按钮（仅报告含 LLM 评审时出现，失败数 0 时「重试失败」禁用），一键重跑失败（error/invalid）或全部 LLM 评审；报告详情弹窗的 verdict 标记（✓/✗/⚠）点击打开的评审详情里新增「重试该评审」按钮，单条重跑并即时展示新结果（原弹窗由新结果重建）。重试按 verdict 携带的 `profile_id` 解析当前 profile、用存储的评审输入重新喂给评审 LLM，完成后重建 `metrics_summary` 聚合并整体替换报告数据（同一份报告，verdicts 与聚合被刷新，持久化生效）；机械 verdict（不走 LLM）不参与重试，profile 已删除 / 未存上下文的 verdict 计入「失败」并附原因。新增 1 个 Web API：`POST /reports/<report_id>/reviews/retry`（payload 支持 `scope: "failed"|"all"` 批量或 `targets` 单条定位，返回 `{updated, failed, errors, report}`）。

### 🐛 Bug Fixes (缺陷修复)

- 修复报告 / 结果详情弹窗只有「关闭」按钮、无法直观「返回」报告的问题：报告列表「查看」弹窗与「查看报告 / 最近运行」结果弹窗的按钮改为「返回」（关闭即回到弹窗之下的报告 / 列表视图，返回语义更贴合导航直觉）。
- 修复 LLM 评审输出「原始返回 = 契约 schema」的问题：`{{metrics}}` 占位符原先直接转储完整指标契约 JSON（含 `enum_values` / `pass_categories` 等 schema 键名），系统提示词按「输出 {{metrics}}」句式编写时，评审 LLM 会把契约原样回显而不是输出实际评估值（如 `{"身份": "一致", "性格": "一致"}`）。现展开改为逐字段列出取值要求与通过判定并附示例输出的简明描述（`请只输出一个 JSON 对象…`），占位符自足、不再诱导回显；评审详情弹窗里的「评审输出」随之展示真实的评估 JSON。
- 修复刚进入页面时测试组配置档案徽标显示原始档案 id（如 `eadfcf07…`）而非档案名称的问题：初始化时 `loadOptions` 与 `refreshGroups` 并行执行，组列表首帧渲染可能赶在配置档案列表（`state.confs`）就绪之前，`confName` 未命中即回退显示档案 id，须手动刷新才恢复名称。现初始化先 `await loadOptions()` 加载平台与配置档案，再并行刷新各列表（`loadOptions` 内部已各自捕获降级，不破坏 `Promise.allSettled` 的失败隔离）。
- 修复测试会话的配置档案绑定被用户已配的「全部会话」兜底路由遮蔽的问题：AstrBot UCR 按 dict 插入顺序**首个匹配即返回**，而 `update_route` 对新键**追加到末尾**——用户已配「全部会话」类兜底（如 `webchat::`）时，后追加的会话级精确路由会被兜底遮蔽、绑定静默失效。现插件写入的所有精确路由都移到路由表**表头**（`put_route_front`：pop 后重建 dict 并触发持久化），优先于更宽的既有规则命中；会话级精确 umo 只匹配自身，不影响其他会话与规则的解析，未绑定会话仍正常落到兜底。持久路由（创建/删除组与会话、会话配置变更）与 runner 临时路由统一收敛到 `core/conf_routes.py` 的同一套操作。
- 修复群发栏「自动@」勾选框与「发送到全部」按钮在窄窗口下重叠、输入框过小、勾选框被拉成长方形的问题：群发区重排为左右布局——左侧大号多行 textarea（占据群发区大部分，可拖拽调高）、右侧操作列自上而下放身份选择器 / 自动@ 勾选框 / 发送按钮（不再与输入框挤同一行）；输入框由单行 `<input>` 改为 `<textarea>`，Enter 换行、Ctrl+Enter（或 Command+Enter）发送。勾选框被全局 `input{width:100%}` 拉伸成长方形的坑也一并修掉（`align-items: flex-start` 防止标签被拉满列宽、`.run-auto-at-label input` 显式 13×13）。
- 修复测试集运行内部异常时前端进度卡死的问题：后端 `_drive` 的异常路径原先只把 run 置为 error、不广播事件，而前端单槽进度完全靠事件流推进（终态事件驱动取消按钮隐藏 / 「查看报告」按钮出现），收不到终态事件会一直停留在「运行中」且无法恢复。现异常路径同样发布终态 `testset` 事件。
- 重新生成（regenerate）支持按对话定位轮次：`regenerate_history` 接受可选 `conversation_id`，在指定对话内定位该轮（多对话历史下全局索引相对当前对话是错的，必须按对话取值）；`conversation_id` 非字符串时 400。
- 修复并发测试集运行污染前端单槽进度状态的问题：前端进度是单槽状态（`activeRunId` / 取消按钮 / 步骤去重集合只支持一个运行），两个运行同时进行时事件流会互相污染、完成步骤可能被误跳过。现 `run_testset` 入口以 `has_active_run()` 守卫拒绝并发启动（400「已有测试集运行中」）；测试集步骤去重键同时带 runId 前缀，不同运行的同一序号步骤互不干扰（纵深防御）。
- 修复断线对账时测试运行记录已被清理（404）导致消费者泄漏的问题：`reconcileEvents` 对查询失败一律跳过重试，而记录被 prune 后 404 永远不会恢复——`testConsumers` 中的条目永久残留。现失败即释放该消费者。
- 修复暂存报告无界增长的问题：`state.runReports` 只增不减，长时间使用会持续堆积内存。现只保留最近 20 份报告，超出丢弃最旧（`MAX_STASHED_REPORTS`）。
- 修复历史刷新乱序覆盖的问题：群发反馈 / 测试集步骤 / 手动刷新可能并发触发同一会话的 `loadHistory`，后发起的请求可能先返回，旧快照覆盖较新内容（历史回退）或旧错误覆盖新内容。现每个会话维护刷新序号，只采纳最后一次发起的响应，乱序迟到的响应与错误一律丢弃。
- 修复「打开全部」每次只打开组内第 1 个会话、会话「打开 / 关闭」按钮标签不更新的问题：`renderPanels` 调用的 `updateRunOverview` 在拆分重构后失去 app.js 模块级绑定（实现移入 testset_run.js，app.js 仅经 env 对象传给 group_list.js），每次调用都抛 `ReferenceError`——面板在报错前已追加进 DOM、但 `openAll` 循环随即中断、收尾的 `renderGroupList` 不执行，于是每次点击只多开一个会话、按钮标签永不更新。现为 `updateRunOverview` 补模块级绑定并新增防回归静态测试。
- 移除 `api.js` 中从未被调用的 `unsubscribeEvents` 死代码（退订由父窗口在连接断开时自动处理，页面只需重连）。
- 修复「合法 JSON」断言对 LLM 常见输出误判的问题：`evaluate_rule` 原先直接 `json.loads(回复)`，而 LLM 回复常把 JSON 包进 markdown 代码块围栏、或在前后夹带思维链 / 说明文本（AstrBot 开启思维链显示时，回复链头会被装饰阶段注入「🤔 思考: …」前缀）——换行缩进本身不影响解析，但任何额外内容都会让 `json.loads` 失败，导致明明是合法 JSON 的输出被判 ✗。现改为宽松判定：先剥代码块围栏直接解析，失败再取首个开括号到末个闭括号的子串解析（对象 / 数组），仍失败才算不通过。
- 修复测试集运行结果总结不统计断言未通过的问题：断言失败只落在结果表格单元格（✗）、不改变会话 status，而最终总结只数 `status == "error"` 的步骤——「表格 3 个会话 ✗、总结错误 0」的误导由此而来。现总结与表格行尾计数都单独标注「N 条断言未通过」，与步骤/会话错误区分。
- 修复测试集编辑窗口在未选中任何测试集时「添加消息 / 保存 / 运行 / 导出」按钮静默无效的问题：这些按钮在空态下仍可见，点击「＋ 添加消息」虽能加出一行，但保存被「未选中」挡住且无任何反馈——用户会误以为无法添加消息。现所有编辑窗口动作经 `requireSelected` 守卫，未选中时点击弹指引提示（「请先在左侧选择或创建一个测试集」）。
- 修复组内会话为 0 个时无法删除测试组的问题：`delete_groups` 的保存条件原为「`removed` 非空」——`removed` 只收集被删的（组, 会话）对，组内 0 会话时恒为空列表，组对象实际未被移除也未落盘，删组操作静默失败（组内会话可被逐个删光，故 0 会话组是可达状态）。现改为按「是否有组命中」判定，组删除与级联清理行为在 0 会话时保持一致；补数据层与 handler 层回归测试。
- 修复前端模块化拆分引入的整页初始化中止问题：`app.js` 在 `createGroupList` 解构声明之前就引用了 `refreshGroups`（const 解构绑定的暂时性死区），模块求值即抛 `ReferenceError: can't access lexical declaration 'refreshGroups' before initialization`，后续初始化全部不执行——左侧窄条按钮失效、测试组列表只剩静态刷新按钮、无「＋ 新建测试组」入口。现把静态控件绑定移到解构声明之后，并新增防回归静态测试 `test_frontend_no_use_before_declaration`（检查各模块顶格语句不得引用先于其声明的 const/let 绑定；`node --check` 只查语法发现不了此类运行时顺序错误）。
- 修复新创建（从未发过消息）或历史被重置 / 删除后的会话编辑历史保存失败的问题：`save_history` 不再对不存在的 `conversation_id` 报「不存在」错误，而是按整体替换语义新建占位对话（带编辑器里的历史内容，id 由系统重新生成）。
- 修复群发 / 单发 / 重新生成完成后会话窗口不刷新、状态停留在「正在并发发送给 x 个会话…」的问题：`runStatus` 把查询串 `?test_id=...` 拼进了 bridge 端点，而父窗口会拒绝含 `?` 的端点，导致状态轮询恒失败、前端永不触发会话刷新（历史实际已写入，刷新页面重开会话可见）。改为通过 `apiGet` 的第二个参数传递查询参数。
- 优化「轮次对齐」模式的滚动性能：原实现每收到一次 scroll 事件就对其他全部面板强制读高 + 写 `scrollTop`（无差异也写），且写入触发的 scroll 事件级联回写，多面板时造成布局抖动与滚动卡顿。改为按帧（`requestAnimationFrame`）合并同步，且仅对滚动位置有差异的面板写入。
- 修复前端页面脚本因 import 与本地函数重名（`createGroup`）导致模块解析失败、整页 JS 失效的问题：平台来源下拉框为空、配置档案下拉框仅剩默认项（实为 `loadOptions()` 从未执行）。将本地创建处理函数重命名为 `handleCreateGroup`，与 `api.js` 导出的同名 API 函数解冲突。
- 修复平台来源下拉框异常无选项的问题：平台列表接口对单个适配器元数据读取失败做容错（跳过该适配器，不拖垮整个接口），前端对返回数据做类型校验，保证至少保留「virtual_test（默认）」选项。
- 删除测试组 / 会话时联动删除 AstrBot 原生对话历史（按 unified_msg_origin），不再在 WebUI 会话列表中残留虚拟会话的对话数据。

- 修复 `run_ruff.bat` 找不到虚拟环境激活脚本的问题：脚本查找的目录名 `venv` 实际为 `.venv`（带点前缀），此前运行恒在 STEP 2 失败。
- 修复会话「配置 / 编辑配置」按钮无效的问题：`openSettings` 构建弹窗标题时引用了作用域内不存在的变量 `s`（解构出的名称是 `session`），点击按钮即在计算模板字符串时抛 ReferenceError、弹窗无法打开；该问题自 v0.3.0（测试组模型重构）引入，此前会话配置一直无法在弹窗中修改。
- 修复平台来源变更时未级联清理旧 umo 下 AstrBot 原生对话历史的问题：`update_session` / `update_group` 修改平台来源后，WebUI 会话列表中仍残留旧来源的对话数据，与删除组 / 会话的级联清理语义不一致。
- 修复带配置档案（conf_id）的并发测试在 pipeline 悬挂时永久占用 UCR 临时路由锁的问题：原实现无限等待所有会话完成，此后所有带 conf_id 的测试会永久阻塞；现改为 1 小时安全阀超时强制恢复路由并释放锁（不改变「不设总超时」的测试语义）。
- 修复测试运行器异常路径路由锁泄漏：事件构造 / 入队 / 任务创建任一步抛错时锁不再永久占用；同时清理创建超过 1 小时的悬挂运行记录，避免内存累积。
- 修复 `save_history` 对同一失效 `conversation_id` 重复引用时新建多个重复占位对话的问题：现首次新建后其余引用复用同一占位对话。
- 修复 `list_confs` 对配置档案对象缺键（id/name/path）直接报 500 的问题：改为防御式读取，与 `list_platforms` 的容错风格一致。
- 修复 `update_group` / `update_session` 在配置未实际变化（如仅改组名 / 发送者）时也重写 UCR 配置档案路由的问题：仅在平台或配置档案实际变化时才同步路由。
- 修复会话展开配置中「发送者 ID / 发送者昵称」恒显示「—」的问题：前端 `effectiveView` 解析最终配置时遗漏了这两个字段，现与会话覆盖 → 组配置 → 默认值（testbench / 测试台）的后端解析保持一致。
- 修复会话配置弹窗可能静默丢失档案绑定的问题：会话单独绑定的配置档案被删除后，打开会话配置弹窗时下拉无该选项、回落显示「使用组配置」，保存即把 `conf_id` 静默重置为继承组配置；现与组编辑弹窗一致为已删除档案保留「（档案已不存在）」占位选项，保存保留原绑定。
- 修复在途消息条冗余展示已刷入历史的消息的问题：消息完成后回复已随历史刷新进会话气泡，但「完成」条目仍在条内保留 30s，与气泡内容重复展示。现改为在 `loadHistory` 成功时记录刷新时刻，完成于该时刻之前的条目即从条内移除——条内只保留真正在途（已入队 / 排队等待 LLM / LLM 生成中）与完成后的短暂过渡。
- 修复用户数字输入被 `parseInt` 静默截断的问题：新增会话数量 / 组会话数量输入 `1.5` 会被截断成 `1` 悄悄执行（`Number.isInteger` 检查的是截断后的值，防不住截断本身），min_len / max_len 断言值 `1.5` 同样被截断。三处改 `Number()` 解析（配合 `Number.isInteger` 拒绝小数、解析失败报错不执行）。
- 修复无效断言值被静默丢弃的问题：min_len / max_len 填非整数、或值类断言（包含 / 正则 / 前后缀等）留空时，保存会把整条规则悄悄丢弃（消息仍保存、断言不生效，用户无从察觉）。现保存 / 导出前校验编辑器各行的断言值，带行号提示（「第 N 条消息：规则「最少字数」的断言值必须是整数」），不再无声吞掉。
- 修复批量段收集中途取消导致已发步骤结果丢失的问题：批量段内消息已全部发出后用户点「取消」，收集循环因 `run.status != "running"` 提前 break——已发出的步骤永远卡在 running 状态、结果不落定。现取消后仍把已发出的段内步骤全部收完（abort 只停止后续未发出的消息，语义与单步段一致）。
- 修复 `run_test` 对非字符串 text 静默强制转换的问题：`text=None` 会按 `str(text)` 发成字符串 `"None"`、数字会发成其十进制表示——错误类型的数据被悄悄当成消息投递。现要求 `text` 必须是字符串，否则 400。
- 修复普通测试轮询在运行记录被清理后 interval 永久泄漏的问题：`pollRun` 对查询失败一律 `return` 下轮重试，而运行记录被 prune（完成后 10 分钟）后 404 永远不会恢复——定时器空转永不停止。现遇 404（`未找到`）即停止轮询；瞬时网络错误仍下轮重试。
- 修复组列表刷新失败拖垮页面初始化的问题：`refreshGroups` 原先无 try/catch，`listGroups` 任一瞬时失败会让初始化 `Promise.all` 整体拒绝、`pollPending()` 永不启动（在途消息条全部失效，只能刷新页面）。现失败降级（组列表清空 + 状态条提示，与 `refreshTestsets` 一致），初始化改用 `Promise.allSettled` 隔离各步失败。
- 修复 `list_providers` 非防御式读取的问题：单个 Provider 的 `meta()` / `get_model()` 抛异常会使整个接口 500（前端 Provider 下拉为空）。现与 `list_platforms` 一致逐调用防护——`meta()` 失败跳过该 Provider，`get_model()` 失败降级为 None，均不拖垮接口。
- 修复测试集创建 / 更新的空消息校验文案误导的问题：空消息序列现为合法输入（先建命名条目、再在窗口里加消息），报错文案「messages 必须是非空消息数组」与实际语义矛盾，改为「messages 必须是消息数组」。
- 修复 `send_streaming` 以已耗尽生成器调用基类 `super().send_streaming()` 的脆弱行为：基类实现不消费生成器（只置 `_has_send_oper` 并上报 Metric），空流路径全靠这次调用偶然补上发送标记；现显式置位 `_has_send_oper`（与真实适配器 tg/lark 一致），移除 super() 调用与其多余 Metric 任务。
- 修复测试集运行记录清理后后台任务继续驱动的孤儿问题：`_prune_runs` 对超过 1 小时的悬挂运行只移除记录不取消任务，后台仍在真实投递测试消息且用户无法查询 / 中止；现清理时一并 `task.cancel()`。
- 修复测试集编辑窗口「先保存再导出 / 运行」在保存失败后仍继续的问题：`saveEditor` 吞掉自身错误不返回结果，导出 / 运行在保存失败后仍执行（导出的是编辑器未保存内容、运行的是旧版本）。现 `saveEditor` 返回成功标志，失败即中止后续动作。
- 修复测试集最终断言（final_rules）在真实运行中恒为空的问题：`start_run` 构建运行记录时没有把测试集的 `final_rules` 快照进 run dict，评审阶段从 run 读取时恒为 `[]`——最终断言规则从未被评估（此前测试只覆盖 `Assessor.assess` 直调与空 final_rules 的端到端运行，未暴露该缺口）。现 `start_run` 按启动时快照 `final_rules` 进运行记录，评审阶段从运行快照读取（与 steps 快照消息一致，测试集事后修改不影响本次运行），并补端到端回归测试。
- 修复「新建评审 Profile」按钮点击无效的问题：`openProfileForm` 用 `state.providers` 构建 Provider 下拉，但前端从未加载 LLM Provider 列表（api.js 缺 `listProviders` 封装、state.js 无 `providers` 初始值、`loadOptions` 未拉取 `/providers`）——`state.providers` 恒为 undefined，点击即抛 `TypeError: can't access property "map"`、弹窗打不开且无任何可见反馈。现补齐三段链路：api.js 新增 `listProviders`、state.js 初始化 `providers: []`、`loadOptions` 预载 Provider 列表（失败降级空数组，与 platforms/confs 一致）。
- 修复页面重开 / 断线对账时报「拉取最近运行失败」的问题：`reconcileEvents` 把 `listTestsetRuns()` 返回的 `{runs: [...]}` 整个当数组调 `.find`（对象无 find 方法，`TypeError: (intermediate value).find is not a function`），导致运行中的测试集无法被前端找回接管（后台任务在跑、前端却无进度）。现与报告视图一致解包 `runs` 数组。
- 修复评审 Profile 的 Provider 下拉全部显示 `openai_chat_completion` 的问题：`list_providers` 的名称解析用 `provider_config.get("name") or meta.type`，而新 Provider UI 下来源没有 `name` 键——多个不同参数的 deepseek 模型（同属 `openai_chat_completion` 适配器类型）全部回落显示适配器类型名，无法区分。现名称解析链改为 `name` → `provider_source_id`（WebUI 展示名，即用户给来源起的名字）→ provider `id` → `meta.id` → `meta.type`，与 AstrBot WebUI 侧栏展示一致。
- 修复「新建评审 Profile」弹窗内容超出视口高度、页面放不下（底部按钮被顶出屏幕）的问题：弹窗正文（Provider / 模型 / 提示词 / 输出指标）整体过高，而弹窗无高度上限、正文不可滚动。现 `.modal` 限高（`max-height: min(85vh, 720px)` + flex 列布局）、`.modal-body` 内部滚动（`overflow-y: auto`，操作按钮固定底部）——任意弹窗内容过高都改为弹窗内滚动而非撑爆页面；系统提示词输入框默认高度由 `.json-editor` 的 360px 降为 120px（弹窗紧凑打开），仍可拖拽拉长，拉长后弹窗正文区滚动。

### 🔄 Changed (行为变更)

- **测试组列表组头简化**：组头首行只保留组名与右侧按钮（打开全部 / 编辑）——会话数徽标移到 `.group-meta` 与平台 / 配置档案 / 安全徽标同行；「＋ 新增会话」与「✕ 删除组」按钮移除——新增会话改走编辑弹窗的「会话数量」（保存时少于目标值自动补建，与原来「＋」功能一致），**删除测试组入口移入编辑弹窗**（danger 按钮 → 原有确认流程，先关编辑弹窗再确认，防弹窗叠加）；组名以 `flex-grow` 占满组头剩余宽度，超长名称悬停 `title` 显示完整值，不再被右侧按钮挤成很短一截。
- **面板视图切换（LLM 历史 / 消息流）改为全局统一控制**：切换按钮从单个会话页眉移除，移到与「轮次对齐」开关**同一行右侧**（`#view-toggle`，`.run-overview-controls` 成组右对齐），点击统一切换**全部已打开的会话**（新打开的面板沿用当前全局视图）——避免部分会话历史视图、部分消息流视图时轮次对齐含义不一致；**消息流视图也参与轮次对齐**（对齐模式下按 user 发言把消息流分组渲染 `.turn-wrap`，与 LLM 历史的轮次语义一致，reflowAlign 统一各面板每轮高度）。
- 前端由 1s 轮询改为**全事件驱动**：移除 `pollRun` / `pollTestsetRun` / `pollPending` 三个轮询器与 `startPolling` 辅助，改订阅 `/events` SSE 事件流（`connectEvents` → `handleEvent` 分发 pending / session_done / test_done / testset）；断线后延迟 3s 重连，并以 `reconcileEvents()` 用轮询接口一次性快照对账（`getPending` + 在途各 test_id 逐个 `runStatus` + 有活动运行则 `runTestsetStatus`），丢失的事件由其兜底——无轮询 fallback。
- 测试集运行与手动群发统一逐会话反馈路径：共用 `applySessionFeedback`（面板状态 + 回复耗时 + 逐会话历史刷新），测试集运行中**新完成的步骤逐结果实时刷新面板**，不再等终态一次性刷新。
- 测试集运行结果**不自动弹窗**：终态暂存 `state.runReports`，顶部常显状态条出现「查看报告」按钮按需查看结果表格；会话窗口仍实时显示各会话回复耗时。

### 🔧 Refactor (代码结构)

- 代码结构拆分，无行为变化：后端 `runner.py` 拆为数据层 `group_store.py`、统计工具 `stats.py` 与运行器 `runner.py`；前端 `app.js` 拆出 `api.js`（bridge 调用统一封装）与 `align.js`（轮次对齐控制器）。
- 插件仓库新增自包含 `pyproject.toml`（ruff / pytest 配置与主仓库口径一致，line-length 88、target py312），任何环境在仓库内直接 `ruff check .` / `pytest` 结果一致；同步修复测试代码的 ASYNC109 告警（`wait_run_done` 的 `timeout` 参数更名），并清理 `list_providers` 对 `prov.meta()` 的重复调用。
- UCR 配置档案路由操作收敛到 `conf_routes.py`：持久路由（创建/删除组与会话、会话配置变更）与 runner 临时路由（测试运行时指定 conf_id）共用同一套 umo → conf_id 操作，消除两处实现对 UCR API 的双份维护；行为不变。
- 前端 `app.js` 拆出 `chat.js`（`createChatRenderer`）：气泡 / 思维链 / 轮次分组与对齐渲染集中到独立模块，align 控制器以 getter 注入（渲染时才取），避免与 `createAlignController` 互相创建的循环依赖；行为不变。
- 页面目录由 `pages/virtual-session/` 更名为 `pages/testbench/`，与插件名一致，避免与其他插件的页面目录冲突（页面 URL / 数据持久化路径不受影响，旧页面访问 404，需重新打开新页面）。
- 移除 `.github/workflows/shit-mountain.yml`（门禁步骤被注释、仅在 main 分支触发且无实际产物，保留无价值）。
- `update_session` 的返回值为死 API 面（调用方已持有内部对象引用），改为返回 `None`；前端 `CONF_DEFAULT` 魔法字符串提取为模块常量，过期注释同步修正。
- 前端 `app.js` 再次拆分：新增 `state.js`（全部共享可变状态收进 `state` 对象）、`modal.js`（自绘弹窗）、`utils.js`（工具函数与 `effectiveView`/`findSession` 等配置解析）、`group_list.js`（左侧测试组列表与组/会话配置弹窗，经 `createGroupList(env)` 注入视图动作保持模块依赖单向）；app.js 缩减为面板 / 发送 / 会话操作 / 排序 / 初始化入口，行为不变。
- 前端平台 / 配置档案下拉的选项构建收敛为 `platformOptions()` / `confOptions()` 共享辅助（组编辑与会话配置两个弹窗复用），消除重复实现，并让「档案已不存在」占位逻辑只保留一份。
- `update_group` 的路由同步改为按会话 id 配对旧 / 新会话，不再依赖两个会话列表的顺序一致（原按位置 `zip` 属隐含假设）。
- 测试集运行不再选「逐条 / 批量」模式：`run_testset` 请求体移除 `mode` 字段，仅 `{testset_id, sessions}`，发送节奏由测试集内 `batch_ranges` 决定（段驱动，语义见「批量发送范围」）。
- 前端三个轮询器（`pollRun` / `pollTestsetRun` / `pollPending`）曾收敛为共享 `startPolling` 辅助（busy 标志跳过重叠 tick、fn 抛错只记日志），后随事件驱动改造整体移除（见「行为变更」）。
- 前端 `app.js` 再次拆分（纯结构，行为不变）：拆出 `events.js`（事件驱动反馈层 `createEventController(env)`——`connectEvents` 订阅 SSE / `handleEvent` 分发 / `registerTestConsumer` 逐会话消费者 / `applySessionFeedback` 统一反馈 / `reconcileEvents` 断线对账，测试集事件经 `setTestsetEvent` 转交 testset_run 模块避免循环 import）与 `testset_run.js`（测试集运行编排视图 `createTestsetRunController(env)`——`runTestset` / `handleTestsetEvent` / `showTestsetResults` / `viewTestsetRun` / `abortTestsetRun` / `runTestsetFromBar`）。
- 前端 `testset_list.js` 拆出 `testset_editor.js`（右侧测试集编辑窗口 `createTestsetEditor(env)`：消息行 `renderMsgRow` / 反向收集 `collectEditorRows` / 断言规则 `RULE_TYPES` / 批量段 / 脏标记 / 保存 / 导出 / 导入）；编辑器与列表互相引用、直接 import 会成环，列表侧函数（`formatTime` / `openTestsetRun` / `deleteTestset` / `doSelect` / `refreshTestsets`）经 `setDeps` 延迟注入。
- 后端 `main.py` 拆出 `history_ops.py`（会话对话历史操作 `HistoryOps`：`save_history` / `regenerate_history` / `copy_history` / `delete_session_conversations`），main.py 保留路由装配与薄委托（`_ROUTES` 的 getattr 要求 handler 名字仍在 Star 上）；`group_mgr` 以 getter 延迟获取——测试重新绑定 `plugin.group_mgr` 后仍指向新管理器。
- 后端按**目录内聚**重排为 api / core / store / eval 四包（纯结构，行为零变更）：`main.py` 瘦身为 Star 入口（依赖装配 + 路由注册 + 两个 LLM 阶段 hook）；Web API handler 按资源聚合为 `api/` 下的 mixin 类（`MetaAPI` / `GroupsAPI` / `SessionsAPI` / `RunsAPI` / `TestsetsAPI` / `EventsAPI`，共享的 UCR 路由薄包装 `ConfRouteMixin` 与 `MAX_SESSIONS_PER_GROUP` 在 `api/common.py`，`_ROUTES` 路由表在 `api/routes.py`），由 `VirtualSessionPlugin` 继承装配——`plugin.<handler>` 仍为 bound method，`_ROUTES` 的 getattr 解析与测试调用方式均不变；运行编排迁至 `core/`（`event_bus` / `virtual_event` / `conf_routes` / `runner` / `testset_runner`）、持久化迁至 `store/`（`group_store` / `testset_store`）、断言评估迁至 `eval/mechanical.py`（原 `assertions.py`）；`history_ops.py` / `stats.py` 保持扁平。150 个测试全部通过，无行为变更。
- 后端 `_ListStore` 补公开写方法（`add` / `remove` / `replace`），`IdentityStore` / `ChatGroupStore` 的增删改改走这些方法，不再直接访问 `self._store._items` 私有属性（行为不变，公开方法签名不变）；删除 `app.js` 一处重复分区注释；README 目录结构与开发流程同步（补齐 `identity_store.py` / `stream_store.py` / `identity_list.js` 与「push 到 dev 由 CI 把关」工作流）。

---

## [v0.3.0] - 2026-08-04

### ✨ New Features (新功能)

- 左侧会话列表重构为「测试组」模型：创建测试组生成一组共享配置（平台来源 / 配置档案 / 发送者 id / 昵称）的虚拟会话，组内单会话可单独覆盖组配置，组内可随时新增 / 删除会话。
- 旧版平铺会话数据（`sessions.json`）自动迁移为「默认测试组」，无感升级。
- 测试组删除、组内会话删除 / 配置变更时自动清理对应的配置档案路由。
- 「轮次对齐」改为保留连续气泡流的纵向对齐：以 user 发言为轮次边界，每轮按各面板该轮内容的最大长度撑开，各面板总高度一致、轮次精确对齐；滚动任一面板自动同步全部窗口，底部滑动条按轮次定位并实时指示当前轮次。
- 群发改为**逐会话独立实时刷新**：每个会话窗口谁完成谁更新，无需等待整批结束；移除总超时与分批投递，消息直接进入 AstrBot 原生 pipeline（与真实环境一致），面板不再显示「超时」状态。
- 面板消息气泡悬停可「编辑」直接改写历史消息；对任意用户发言可「重新生成」——截断该轮及之后的历史并重新走 pipeline 生成新回复。

---

## [v0.2.1] - 2026-08-04

### 🐛 Bug Fixes (缺陷修复)

- 修复发布 zip 误打包本地运行数据 `data/`（含运行期 sqlite 数据库）的问题，发布包不再包含无关目录。

---

## [v0.2.0] - 2026-08-04

### ✨ New Features (新功能)

- 页面改为左侧会话列表 + 右侧并行面板布局：可同时打开多个会话并行查看对话历史，面板支持拖拽排序与置顶。
- 新增会话对话历史查看：每个面板展示该会话的完整对话历史（含推理内容与工具调用）。
- 创建虚拟会话时可选择平台与配置档案，通过 AstrBot 原生 UCR 路由精确绑定到单个会话（删除会话时自动清理）。
- 群发栏直接并发发送给所有已打开的会话；单个面板也可单独发送消息，无需再选择 Provider / 模型（由会话绑定配置决定）。

---

## [v0.1.0] - 2026-08-04

### ✨ New Features (新功能)

- 初始化会话测试台插件（astrbot_plugin_testbench）。
- 通过框架原生插件页面创建与真实会话走完全相同处理路径的虚拟会话。
- 支持并发测试：一条消息同时发送给 N 个虚拟会话。
- 捕获并展示每个会话的回复、推理内容、耗时与状态（成功 / 无回复 / 超时 / 错误）。
- 提供耗时统计：min / max / avg / p50 / p95。
- 支持选择 Provider、模型与配置档案（UCR 路由）进行定向测试。
- 虚拟会话持久化与对话历史重置。

---

<details>
<summary>点击查看历史更新记录 (History)</summary>

</details>
