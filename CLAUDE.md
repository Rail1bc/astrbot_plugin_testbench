# CLAUDE.md

本文件为 Claude Code 在本插件（`astrbot_plugin_testbench`）目录下工作提供指引。

## 插件概述

会话测试台（astrbot_plugin_testbench）是一个 AstrBot 插件：通过框架原生插件页面创建「虚拟会话」，并把一句话并发投递给多个虚拟会话，用于测试插件、提示词、模型与整体稳定性。

- **版本**：v0.4.0（metadata.yaml 中的版本号，未经用户批准不得擅自 bump）
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

**群聊消息的唤醒语义**：消息类型为 `GroupMessage` 时，是否唤醒机器人由发送时的 `auto_at` 选项决定（群发栏 `#run-auto-at` 勾选框默认开启，测试集每条消息可单独配置）——开启则消息链以 `At(机器人自身)` 开头、唤醒检查直接命中；关闭则消息以未唤醒状态进管道，只能被 filter 通过（如 Heartflow）唤醒。唤醒状态在结果摘要中可读（`wake` + `reason`，见「消息类型与自动@」）。

**自动角色（event.role）**：发送时 runner 按解析后的发送者 id 在身份库中查 `is_admin`（`_resolve_role`），构造事件时设置 `event.role`（管理员 → `"admin"`，否则 `"member"`）——虚拟事件不再恒为成员，可触发 `computer_use_require_admin` 门控的计算机工具（见「工具安全警告」）。

## 目录结构

```
astrbot_plugin_testbench/
├─ metadata.yaml        # 插件元数据（name/display_name/version/astrbot_version）
├─ main.py              # Star 入口：装配依赖 + 注册路由 + 两个 LLM 阶段 hook（薄）
├─ api/                 # Web API 路由 handler 层（按资源聚合的 mixin，由 main.py 的 Star 继承）
│  ├─ routes.py         #   _ROUTES 路由表（一处看全，新增端点只加一行）
│  ├─ common.py         #   MAX_SESSIONS_PER_GROUP + ConfRouteMixin（UCR 路由薄包装）
│  ├─ meta.py           #   MetaAPI：Provider / 配置档案 / 平台列表
│  ├─ groups.py         #   GroupsAPI：测试组 CRUD 与组配置更新（含消息类型变更的 umo 清理）
│  ├─ sessions.py       #   SessionsAPI：会话 CRUD / 历史 / 克隆 / 衍生 / 消息流
│  ├─ runs.py           #   RunsAPI：单条群发 / 状态 / 在途查询（支持消息级 sender）
│  ├─ testsets.py       #   TestsetsAPI：测试集 CRUD / 批量段校验 / 运行入口（消息可带身份）
│  ├─ identities.py     #   IdentitiesAPI：测试身份与虚拟群聊 CRUD
│  └─ events.py         #   EventsAPI：/events SSE 事件流
├─ core/                # 运行编排层：事件 / 虚拟事件 / UCR 路由 / 运行器
│  ├─ event_bus.py      #   进程内事件广播（asyncio.Queue 有界队列，满则丢最旧；SSE 事件源）
│  ├─ virtual_event.py  #   VirtualMessageEvent：捕获 send/流式结果，携带完成信号，auto-@ 与消息类型
│  ├─ conf_routes.py    #   UCR 配置档案路由操作收敛（持久路由与临时路由共用一套）
│  ├─ runner.py         #   并发测试运行器（VirtualTestRunner，临时路由经 conf_routes）
│  └─ testset_runner.py #   测试集运行编排器（TestsetRunner：后端按段驱动，单步段逐条、批量段重叠）
├─ store/               # 持久化与数据模型层
│  ├─ group_store.py    #   测试组数据模型与持久化（VirtualGroupManager、umo_of、effective）
│  ├─ identity_store.py #   测试身份与虚拟群聊数据模型与持久化（IdentityStore / ChatGroupStore）
│  ├─ stream_store.py   #   群消息流持久化（StreamStore：与 LLM 历史并行的纯记录）
│  └─ testset_store.py  #   测试集数据模型与持久化（TestsetStore）
├─ eval/                # 断言评估层
│  └─ mechanical.py     #   回复断言规则评估纯函数（evaluate_rule：正则/包含/格式）
├─ history_ops.py       # 会话对话历史操作（HistoryOps：save/regenerate/复制/级联删除）
├─ stats.py             # 耗时统计纯函数（duration_stats：min/max/avg/p50/p95）
├─ pyproject.toml       # 插件仓库自包含的 ruff / pytest 配置（不依赖主仓库）
├─ pages/testbench/
│  ├─ index.html        # 页面骨架（表单/面板的静态 HTML，select 初始为空或仅默认项）
│  ├─ app.js            # 页面入口（面板/发送/会话操作/排序/初始化，组装子模块）
│  ├─ events.js         # 事件驱动反馈层（createEventController(env)：SSE 订阅/逐会话反馈/在途消息条/快照对账）
│  ├─ testset_run.js    # 测试集运行编排视图（createTestsetRunController(env)：进度/结果表格/启动/取消/找回）
│  ├─ state.js          # 全部共享可变状态（state 对象，各模块经它读写）
│  ├─ modal.js          # 自绘弹窗（openModal/showModal/hideModal）
│  ├─ utils.js          # 工具函数与最终配置解析（effectiveView/findSession 等）
│  ├─ group_list.js     # 左侧测试组列表与组/会话配置弹窗（createGroupList(env)）
│  ├─ testset_list.js   # 测试集列表/运行弹窗/最近运行（createTestsetList(env)）
│  ├─ testset_editor.js # 测试集编辑窗口（createTestsetEditor(env)：消息行/断言/批量段/身份/保存/导出导入）
│  ├─ identity_list.js  # 「身份与群聊」视图（createIdentityList(env)：身份/群聊 tab 拆分 CRUD + 右侧群聊编辑视图 + 群发栏身份同步）
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

`umo_of(session)`（store/group_store.py:27）：`f"{platform_id}:{message_type}:{session_id}"`——**消息类型参与 umo**：私聊为 `...:FriendMessage:...`、群聊为 `...:GroupMessage:...`（`session.get("message_type")`，缺省 FriendMessage）。平台 id 默认 `webchat`（与 AstrBot WebUI 一致），发送者默认 `testbench` / `测试台`，会话 id 形如 `vs_<uuid8>`，测试组 id 形如 `g_<uuid8>`。umo 是 AstrBot 会话/配置/历史隔离的键：配置档案路由（UCR）、对话历史（conversation_manager）都按它定位。**message_type 变更 = umo 变更**：`update_group` / `update_session` 的 `platform_changed` 清理逻辑扩展到「platform 或 message_type 变更」→ 按旧 umo 删路由 + 删旧 umo 对话历史（同删除语义）。

### 测试组模型（store/group_store.py）

- 组共享一套配置：`platform_id / conf_id / sender_id / sender_name / message_type / chat_group_id`；组内每个会话默认六字段均为 `None`（表示继承组配置）。
- `effective(group, session)` 解析最终配置（会话覆盖优先）：`message_type`（会话 → 组 → 默认 FriendMessage）、`chat_group_id`（会话 → 组 → None）；`conf_id` 为 `""` 表示显式使用默认档案（不绑定路由），`None` 表示继承组。**auto@ 不属于组/会话配置**——它是发送时选项（群发栏 / 测试集消息级，见 runner 的请求级 `auto_at` 参数）。
- `update_session` 用 `_UNSET` 哨兵区分「未传该字段」与「显式传 null（恢复继承组配置）」；`update_session` 归一化空串字段为 `("platform_id", "message_type", "chat_group_id")`（不含 `conf_id`——conf_id 空串 = 显式默认档案），`update_group` 归一化含 `conf_id`。
- 持久化到 `get_astrbot_plugin_data_path()/virtual_session/groups.json`；旧版平铺 `sessions.json` 自动迁移为「默认测试组」，`_load` 对旧数据 `setdefault` 两新键（message_type / chat_group_id）防缺字段崩溃，并把旧配置里残留的 `auto_at` 键清理掉（防随全量写 JSON 永久残留）。
- `_save()` 是全量写 JSON（`ensure_ascii=False, indent=2`）；删除返回 `(组, 会话)` 对，供上层联动清理 UCR 路由与原生对话历史。

### 消息类型与自动@（群聊虚拟会话）

- **消息类型**：测试组 / 会话可配 `message_type`（"FriendMessage" / "GroupMessage"），决定 umo 与唤醒检查分支。群聊消息可被只监听 GROUP_MESSAGE 的插件（如 Heartflow 主动回复插件）触发。
- **auto-@ 机制**（core/virtual_event.py `create()` + core/runner.py）：`auto_at` 是**发送时选项**（runner.start 的请求级参数，默认开启；来源为群发栏 `#run-auto-at` 勾选框或测试集每条消息的 auto_at 字段），仅 GroupMessage 有意义——runner 计算 `effective_auto_at = auto_at and message_type == GROUP_MESSAGE.value`。开启时构造 `abm.message = [At(qq=abm.self_id, name=abm.self_id), *([Plain(text)] if text else [])]`，`abm.message_str = text` 保持纯文本 → 唤醒检查命中 At → `is_at_or_wake_command=True` → 默认 LLM gate 满足；关闭时消息链只有 Plain，以未唤醒状态进管道，只能被 filter 通过唤醒。群发默认开启（保持既有体验），测试集消息级配置可单独关掉某条。
- **唤醒状态可读**：`event.is_wake` / `event.is_at_or_wake_command` / `event.is_stopped()` 在 pipeline_done 后仍可读；`result_summary()` 返回 `wake` dict（woken / at_or_wake / stopped / llm_requested，后者来自 main.py `on_llm` hook 写入的 `_testbench_llm_requested` extra），no_reply 时 `reason = "not_woken" if not is_wake else "woken_no_reply"`。

### 测试身份与虚拟群聊（store/identity_store.py）

- `IdentityStore` → `virtual_session/identities.json`（`{"items": [{id, name, sender_id, sender_name, is_admin, created_at}]}`）；`ChatGroupStore` → `virtual_session/chat_groups.json`（`{"items": [{id, name, member_ids, created_at}]}`）。两个类复用 `_ListStore`（全量写 JSON），类名带 Test 前缀须 `__test__ = False`（同 testset_store 模式）。
- **身份 `is_admin`**（是否管理员）：新建恒写 `"is_admin": bool(is_admin)`（缺省 False）；旧数据缺键 → 读取一律 `identity.get("is_admin", False)`（后端与前端都如此，不做 `_load` setdefault）。`update_identity` 的 `is_admin` 用 None 哨兵：None=不变，**显式 false 也落盘**（前端取消勾选必须生效）。发送消息时 runner 按解析后的发送者 id 查身份库决定 `event.role`（见「工具安全警告」下的自动角色）。前端对管理员身份有**危险操作警告**：身份列表管理员徽标旁挂「⚠ 危险」徽标，表单勾选管理员时显示内联 `.dialog-warn` 警告条（可调用需管理员权限的工具、可能执行危险操作）。
- 身份与虚拟群聊是**跨测试组共享的持久化资源**：组配置弹窗、群发栏身份选择器、测试集消息身份下拉、绑定群聊默认成员都引用它们。身份缺失字段回退名称；虚拟群聊 `_clean_member_ids` 只保留非空字符串并按出现顺序去重；删除身份后成员 id 保留（悬空引用，选择时按现存身份过滤）。
- 新 API：`GET/POST /identities`、`POST /identities/delete`、`POST /identities/<id>/update`、`GET/POST /chat-groups`、`POST /chat-groups/delete`、`POST /chat-groups/<id>/update`。

### 工具安全警告（core/conf_tools.py + api/meta.py + api/groups.py）

- **判定**（`conf_tool_info` 纯函数，与 AstrBot 运行时挂载逻辑一致）：配置启用了任何**可调用工具**即命中——`provider_settings.computer_use_runtime` 为 `local`/`sandbox`、`provider_settings.web_search`、顶层 `kb_agentic_mode`、`proactive_capability.add_cron_tools`（**缺省 True**，故默认配置本身即命中，绝大多数组/会话带标记——如实呈现，无排除逻辑）。`conf_has_callable_tools` 是布尔便捷入口。仅暴露布尔，详情留在纯函数内。
- **呈现**：`GET /confs` 每个档案新增 `has_callable_tools` 布尔（档案未加载 → 宽松 False，仅显示用途）；组 / 会话配置弹窗内**内联警告条**（`buildToolWarningBar` 构建 `.dialog-warn` div，默认 hidden，配置档案下拉 change 时按 `confHasTools` 刷新，不阻塞提交）；会话弹窗按**有效配置**判定（`effectiveConfId`：显式默认哨兵 → "default"、真实 id → 该 id、空值继承 → 会话 conf_id → 组 conf_id → "default" 链）。组列表按 `g.security_warning` 显示「⚠ 工具」徽标。
- **组标记实时计算、派生不持久化**：`list_groups` 逐组浅拷贝 `{**g, "security_warning": ...}`（`list_groups()` 返回的是 store 内共享 dict，直接改写会在下次 `_save()` 时把派生键持久化）；组或任一会话的有效配置命中即真——`conf_id` 为 None/"" 按默认配置判定、已删除档案回退默认（镜像 `astrbot_config_mgr.get_conf` 运行时回退语义），会话级 conf_id 优先于组级（镜像 `effective()`）。配置档案事后内容修改，下次列表即更新标记。
- **自动角色**（见「消息处理路径」）：`VirtualMessageEvent.create(is_admin=...)` 设置 `event.role`；runner 的 `_resolve_role(sender_id)` 遍历身份库，`sender_id` 命中且 `is_admin` 为真 → "admin"（identity_store 为 None → 恒 "member"），覆盖全部发送路径（请求级 sender / 绑定群聊默认成员 / 手动 sender / 默认 testbench）。

### 群消息流（store/stream_store.py）

- `StreamStore` → `virtual_session/streams.json`（`{"streams": {sid: {"session_id", "messages": [{id, role, sender_id, sender_name, text, at_bot?, reply_status?, ts}]}}}`）。`append` / `update_reply` / `read_stream` / `clear` / `delete_sessions` 都经**实例级 asyncio.Lock 串行写**（重叠发送竞态）。
- user 消息在入队前写入（runner `_register_pending`），bot 回复在 pipeline_done 后由 `_write_stream_reply` 写入；`reply_status`（ok / no_reply / error）只标在 user 消息上，写入时同步 `result_summary` 的 status。
- `MAX_STREAM_MESSAGES = 500`/会话，超出截断最旧。重置会话 → 清流（`reset_sessions` 联动 `clear`）；删除会话 → 删流（`delete_sessions` / `delete_groups` 联动）；克隆 / 衍生不复制流（流是运行时记录）。消息流与 LLM 历史**并行**、不注入 LLM 上下文，前端经面板页头「LLM 历史 ↔ 消息流」按钮切换。

### UCR 配置档案路由

- 路由操作集中在 `core/conf_routes.py`：持久路由（创建/删除组与会话、会话配置变更时应用与清理）与 runner 临时路由（测试运行时指定 conf_id）共用同一套 umo → conf_id 操作，避免两处实现对 UCR API 的双份维护。
- 绑定用**会话级精确路由** `umo → conf_id`，不用平台级 `platform_id::`，避免影响同平台其他会话；写入一律经 `put_route_front` 置于路由表**表头**——AstrBot UCR 按 dict 插入顺序首个匹配即返回，而 `update_route` 对新键追加到末尾，若用户已配「全部会话」类兜底（如 `webchat::`），后追加的精确路由会被兜底遮蔽、绑定静默失效（曾因此踩坑）。`put_route_front` 先 pop 再重建 dict 把该 umo 放最前、随后 `update_route` 落盘（键已存在只改值、不重排，值已就位仅触发持久化）；重排只移动本插件的 umo，不破坏用户既有规则的相对顺序，未绑定会话仍正常落到兜底。`restore_routes` 恢复原路由时保持普通 `update_route`（不重排）。
- 创建组/添加会话时若带 `conf_id`，调用 `_apply_conf_routes`；删除组/会话/重置时用 `_clear_conf_routes`（仅删已存在的路由）+ `_delete_session_conversations`（级联删原生对话历史，按 umo 调 `conversation_manager.delete_conversations_by_user_id`）。
- runner 的临时路由（测试运行时指定 conf_id）带 `asyncio.Lock` 串行：`save_and_apply_routes` 保存原路由 → 应用 → 全部完成后 `restore_routes` 恢复并释放锁，避免临时路由互相污染。

### VirtualMessageEvent（core/virtual_event.py）

继承 `AstrMessageEvent`，消息类型默认 `FRIEND_MESSAGE`（私聊默认直接唤醒，无需唤醒前缀），可配 `GroupMessage`（群聊，配合 auto_at / 唤醒词 / filter 触发）：

- `create(...)` 构造 `AstrBotMessage`（`self_id="virtual_bot"`），`selected_provider` / `selected_model` 写入 event extra；`message_type` / `auto_at` 参数见「消息类型与自动@」。
- **两个完成信号**：
  - `done_event`：产生过一次回复（send 或流式结束）时置位。
  - `pipeline_done_event`：pipeline 全部执行完（无论是否产生回复）后置位。**实现技巧**：重写 `cleanup_temporary_local_files()`——它是 `PipelineScheduler.execute()` 的 finally 块中唯一调用点（astrbot/core/pipeline/scheduler.py:97），runner 用 `.wait()` 精确等待 pipeline 结束。
- `send()` 捕获 `MessageChain` 到 `self.captured`；`send_streaming()` 按 aiocqhttp/discord 的累积模式合并 chunk → `squash_plain()` → 交给 `send()`，reasoning 单独累积。
- `result_summary()` 返回 `{umo, session_id, status(ok|no_reply), duration, reply, reasoning, error, wake, reason}`；错误文案从 `_llm_error_message` extra 读取，`wake` / `reason` 见「消息类型与自动@」。

### 运行器（core/runner.py）

- `start(sessions, text, provider_id, model, conf_id, assertion, sender_id, sender_name, auto_at)` **立即返回 test_id**（不等待回复）；事件 `put_nowait` 入队，逐事件 `asyncio.create_task(_await_event)` 等待 `pipeline_done_event`。`auto_at` 是发送时选项（默认开启，仅 GroupMessage 生效）：`effective_auto_at = auto_at and message_type == GROUP_MESSAGE.value`。
- 发送者解析 `_resolve_sender` 优先级：请求级 `sender_id/sender_name`（仅给 id 时昵称回退 id）> 会话绑定虚拟群聊默认成员（`chat_group_id` 绑定的群取 `member_ids[0]` 对应身份，经 `_default_member_of`）> 会话/组手动 sender > 默认 testbench / 测试台。
- 消息流联动：`_register_pending` 把 user 消息写入 `stream_store`（含 `at_bot` = event.auto_at、请求级 sender），返回的流消息 id 记入条目 `stream_msg_id`；`_await_event` 完成后经 `_write_stream_reply` 回填 user 消息 `reply_status` 并 append bot 回复（无回复不 append）。
- `status(test_id)` 返回 `{total, done, results[], stats}`（供断线对账一次性取回；逐会话实时反馈走 SSE 事件流）。
- `wait_done(test_id, timeout_secs=None)`：等待全部完成，超时抛 `asyncio.TimeoutError`（供测试集运行编排器逐步骤等待；参数名 `timeout_secs` 规避 ruff ASYNC109）。
- `start(...)` 带 `assertion` 时，`_await_event` 在 `result_summary()` 后用 `evaluate_rule` 评估并写入 `summary["assertion"]`。
- 运行记录保存在内存 `self._runs`，完成超过 10 分钟自动清理。
- **在途条目（重叠测试）**：每条消息登记一个条目（`self._pending`，经 `event.entry_id` 关联），状态 submitted → waiting_llm → llm → done；中间两个状态由 LLM 阶段 hook（`on_waiting_llm_request` / `on_llm_request`）推进，`pending_entries()` 供断线对账一次性取回；每次状态变更向事件总线广播在途全量快照（`{"type":"pending","entries":[...]}`），前端经 SSE 实时展示；已完成的条目保留 `DONE_KEEP_SECONDS`（30s）后随 `_prune_runs` 清理。

### 回复断言（eval/mechanical.py）

`evaluate_rule(rule, reply) -> dict | None` 纯函数：`rule is None` 返回 `None`，否则 `{pass, detail}`。类型：`contains` / `not_contains`（str 或 list value）、`regex`（`re.search`，无效 pattern → pass False）、`json` / `non_empty`（无 value）、`min_len` / `max_len`（int）、`prefix` / `suffix`（str）。**`json` 为宽松判定**（`_parse_json_reply`）：换行缩进合法；先剥 markdown 代码块围栏直接 `json.loads`，失败再取首个开括号到末个闭括号的子串解析（对象 / 数组）——LLM 回复常在 JSON 前后夹带思维链 / 说明文本（AstrBot 开启思维链显示时，回复链头会被装饰阶段注入「🤔 思考: …」前缀），严格解析会误判。缺 value、未知 type → pass False（数据损坏可见，不静默通过）。Python 正则与 JS 不一致，故评估放在后端。

### 测试集（store/testset_store.py / core/testset_runner.py）

- `TestsetStore`：持久化到 `data/virtual_session/testsets.json`（与 groups.json 同目录），模型 `{testsets: [{id: "ts_<uuid8>", name, created_at, messages: [{text, rule, sender_id?, sender_name?, auto_at?}], batch_ranges: [[start, end], ...]}]}`；`_normalize_messages` 清洗（text 去首尾空白、空文本丢弃、rule 归一 dict|None、可选 sender_id/sender_name 非空字符串保留、可选 auto_at 为 bool 时保留且缺省不落字段——发送时按 True）；`_normalize_batch_ranges` 清洗批量段（每项须为非 bool 的两个 int 且 0 ≤ s ≤ e < message_count，不满足的整段丢弃；先按 start 升序再贪心保留不重叠段，结果与输入顺序无关；非 list → `[]`）；`MAX_MESSAGES_PER_TESTSET = 100`；允许空消息创建（先建命名条目、再在窗口里加消息），`_load` 对旧数据 `setdefault("batch_ranges", [])`。两个类名以 Test 开头，须带 `__test__ = False` 防 pytest 误收集。
- `TestsetRunner`：测试集运行是**耗时操作**且用户可能离开页面，因此**由后端后台任务驱动**（`asyncio.create_task(_drive_segments)`），运行记录保存在内存、可轮询 / 找回 / 取消；每处状态变更（启动 / 步骤置 running/done/error / 终态 / abort）广播 `{"type":"testset","run_id",run}` 运行全量快照，前端经 SSE 实时推进；`start_run(testset, sessions)`（**无 mode**，发送节奏由 `testset["batch_ranges"]` 决定）：
  - `_segments(run)` 把消息按 batch_ranges 切分为**单步段**（`[i]`，段外消息）与**批量段**（`[s..e]`，段内消息）；段之间按 start 升序、互不重叠，逐段驱动。
  - **单步段**：发 → 等该步全部会话完成（`runner.wait_done(timeout_secs=TESTSET_STEP_TIMEOUT)`）→ 再发下一步（上下文连续）；超时 / 异常 → 该步 error、run error、**中止后续**（同原 sequential 语义）。
  - **批量段**：先全部 `start`（各步置 running、记 test_id），再逐个 `wait_done`；段内单步超时 / 异常 → 该步 error、继续收其余步；**段内错误不中止后续段**（同原 batch 语义）。
  - **消息级参数透传**：steps 记录每条消息的可选 sender_id / sender_name / auto_at（`m.get("auto_at", True)`），单步段与批量段都把这些透传给 `runner.start`——测试集每条消息可单独配置发送身份与是否自动@。
  - abort 只置标记、不 cancel 任务：当前步骤照常完成并收结果，后续步骤不再发；段与段之间、批量段等待循环内都检查 `run["status"]`。
  - 全部段结束后：`run.status = 有 error 步 ? "error" : "done"`（仅当未 abort）。
  - 单步超时安全阀 `TESTSET_STEP_TIMEOUT = 600`；运行记录按 `DONE_RUN_KEEP_SECONDS`（10min）/ `STALE_RUN_TIMEOUT`（1h）清理，`start_run` 与 `list_runs` 时 `_prune_runs()`。
  - `status(run_id)` / `list_runs(limit=10)`（倒序摘要，页面重开找回；摘要无 mode 字段）/ `abort(run_id)`。
  - **单运行守卫**：`has_active_run()` 供 `run_testset` 入口拒绝并发启动（400「已有测试集运行中」）——前端进度是单槽状态（activeRunId / 取消按钮 / 步骤去重集合只支持一个运行），两个运行的事件流会互相污染。

### 事件驱动（core/event_bus.py + /events SSE）

- `EventBus`：进程内事件广播（纯 asyncio，无第三方依赖）。订阅者各持独立有界队列（maxlen=1000），满则丢最旧、不阻塞发布者；事件均为 JSON 可序列化的**全量快照**（幂等，丢旧无碍）。
- 事件类型：`{"type":"pending","entries":[...]}`（在途全量快照）、`{"type":"session_done","test_id","session_id","summary"}`、`{"type":"test_done","test_id","record":status(test_id)}`、`{"type":"testset","run_id","run":status(run_id)}`。
- **快照须为发布时刻的拷贝**：pending 条目逐条 `dict(e)`、run 用 `deepcopy`——条目 / run dict 之后被原地更新，不拷贝则已发布事件会漂移成最新状态（曾因共享引用导致首条快照直接显示终态）。
- main.py 的 `/events` 端点以 SSE 推送（15s 心跳注释行防代理断连）；前端 `subscribeSSE("events")` 订阅，断线后延迟重连 + `reconcileEvents()` 以轮询接口一次性快照对账（getPending + 在途各 test_id 逐个 runStatus + 有活动运行则 runTestsetStatus）。
- 测试集运行与手动群发共用同一 `applySessionFeedback` 逐会话反馈路径（面板状态 + 历史刷新），行为一致。

### 前端（pages/testbench/）

- `api.js`：`window.AstrBotPluginPage` bridge 的 `apiGet`/`apiPost` 统一封装（bridge 的响应解析是 `response.data?.data ?? response.data`）+ `subscribeEvents`（`subscribeSSE("events")` 的单一全局订阅封装，断线时自动置空，由页面延迟重连）。
- `state.js`：全部共享可变状态收进一个 `state` 对象（groups/platforms/confs/openIds/pinnedIds/panelEls/historyCache/expandedGroups/expandedSessions/testsets/selectedTestsetId/activeRunId/pendingEntries/runReports/latestReportRunId/testsetReportedSteps/streamCache/globalView）。ES module 顶层绑定无法跨模块共享可变值，故集中到叶子模块，各模块从 `state` 读写，保持依赖单向。
- `utils.js`：纯工具与配置解析（`escapeHtml`/`statusText`/`confName`/`platformName`/`findSession`/`effectiveView`），唯一依赖 `state`。`effectiveView` 是后端 `effective()` 的客户端镜像（曾漏 sender 字段导致显示「—」，有防回归测试）。
- `modal.js`：自绘弹窗（iframe 沙箱禁用原生 alert/confirm），回调状态 `modalCallback` 封装在模块内部，对外只暴露 `openModal`/`showModal`/`hideModal`。
- `group_list.js`：左侧测试组列表与组/会话配置弹窗。`createGroupList(env)` 依赖注入视图动作（toggleOpen/openAll/deleteSession/renderPanels/showRunStatus/updateRunOverview/switchToIdentities），本模块不 import app.js，模块依赖保持单向。`platformOptions()`/`confOptions()` 是平台/档案下拉选项的共享构建（含「档案已不存在」占位，防静默丢绑定）。组编辑弹窗含**消息类型**（私聊/群聊 select）、**绑定虚拟群聊**（select，仅群聊显示，`chatGroupOptions` 含「群聊已不存在」占位）与「管理身份与群聊 →」链接（`hideModal` + `switchToIdentities` 跳转视图，**不嵌嵌套管理弹窗**）；会话配置弹窗同两字段（空 = 继承组）。提交按 `isGroup` 分支：私聊时 chat_group_id 恒 null（无副作用）。**auto@ 是发送时选项（群发栏 / 测试集消息级），不属于组/会话配置，弹窗不再提供**。
- `testset_list.js`：左侧测试集列表、运行弹窗与最近运行。`createTestsetList(env)` 依赖注入视图动作（showRunStatus/runTestset/viewTestsetRun/switchToTestsets），同样不 import app.js。列表条目是**有名字的条目**（点击选中后右侧打开编辑窗口，不再内联展开）；右侧编辑窗口由 `createTestsetEditor` 创建（见下条），互相引用的列表侧函数（formatTime/openTestsetRun/deleteTestset/doSelect/refreshTestsets）在创建后经 `editor.setDeps` 注入。运行弹窗目标**三选**：已打开的 / 全部 / 选择测试组（`buildGroupCheckboxes` 组多选 → `selectedGroupSessionIds` 把勾选组解析为该组全部会话 id），`env.runTestset(testset, ids)` 只收会话 id 列表。最近运行条目标注状态 chip（运行中/完成/错误/已取消），运行中条目也可点「查看」找回进度与结果。
- `testset_editor.js`：右侧测试集编辑窗口。`createTestsetEditor(env)`（env 只注入 showRunStatus）由 testset_list.js 创建——编辑器与列表互相引用、直接 import 会成环，故列表侧函数经 `setDeps` 延迟注入。编辑器按行维护「文本 + 断言类型 + 断言值 + 可选发送身份 + 自动@ + 批量勾选」，**`renderMsgRow` 单行构建 + `collectEditorRows` 反向收集是唯二变化点**，`RULE_TYPES` 定义断言类型选项（无 / contains / not_contains / regex / json / non_empty / min_len / max_len / prefix / suffix）为唯一来源，未来新增测试行为只改这两处 + 后端 `_normalize_messages`；每条消息可选身份（`collectSender` 从行内 `.ts-msg-sender` 下拉收集 sender_id/sender_name，身份被删除后保留原值防静默丢绑定）与**自动@**（行内 `.ts-msg-auto-at` 勾选框，默认开启，旧导入数据无 auto_at 按开启处理）；连续勾选「批量」的消息合并为批量段（`collectEditorRows` 丢弃空文本行后按索引连续性合并，`batch_ranges` 引用保留后的消息索引），`#ts-segments` 实时显示段摘要；**脏标记** `dirty`（任一行输入 / 勾选变化置位，切换测试集 / 导入 / 导出 / 运行前确认，`refreshTestsets` 在 dirty 时跳过编辑器重渲染以免异步刷新清掉未保存修改）；**未选中任何测试集时编辑窗口按钮（添加消息 / 保存 / 运行 / 导出）仍可见，点击须经 `requireSelected` 给指引提示而非静默无效**。导出走 Blob `<a download>`（iframe 沙箱含 `allow-downloads`），信封 `{format: "astrbot-testbench-testset", version: 1, name, messages, batch_ranges}` 预留「测试集市场」下载兼容，信封内消息可选 sender / auto_at（旧文件缺省字段照常导入）；导入复用 `createTestset` 端点（无新后端接口），`parseTestsetEnvelope` 校验 format/version≤1/name/messages/batch_ranges。
- `chat.js`：`createChatRenderer(alignGetter)` 集中聊天内容渲染（气泡 `bubbleFor` / 思维链 `reasoningSection` / 工具调用 `toolCallBlock` 与工具返回 `toolResultBlock` / 轮次分组 `groupTurns` 与对齐渲染 `renderAligned`）。align 以 getter 注入（渲染时才取），避免与 `createAlignController` 互相创建的循环依赖；`app.js` 提供 `renderChat` 包装函数注入 align 控制器并传给 align.js 的 env。另含**消息流轻量渲染** `renderStream(panel, messages)`（无 LLM 格式的 tool_calls/思维链：user 气泡显示发送者身份 + @ 标记 + 回复状态 chip，bot 气泡即回复内容；**对齐模式下按 `groupStreamTurns` 把消息流以 user 发言开启的轮分组渲染 `.turn-wrap`**——与 LLM 历史的轮次语义一致，使消息流视图也参与轮次对齐）。
- `identity_list.js`：「身份与群聊」rail 第三视图。`createIdentityList(env)`（env 注入 refreshGroups/showRunStatus）由 app.js 创建，不 import app.js。左侧卡片内以 **tab 拆分**「身份」/「群聊」两个列表（`switchIdentityTab` 按 dataset.tab 分发、pane 互斥显隐，身份膨胀后群聊列表不被长身份列表挤走）：`#identity-list` 身份列表（名称 + sender_id/sender_name 徽标 + 编辑 / 删除，`openIdentityForm` 走 modal 表单——sender 留空回退名称、更新传空串而非 null 让后端重置；条目**可拖拽**——`item.draggable` + dragstart 把身份 id 写入 dataTransfer，拖到右侧成员区即加入）；`#chat-group-list` 虚拟群聊列表（名称 + N 成员徽标 + 选中高亮 + 编辑 / 删除，条目整体点击 → 右侧打开编辑视图）。**创建群聊只填名称**（成员多选移出弹窗——成员多时窗口放不下，`openCreateChatGroup` 提交 `createChatGroup({ name })`，成功后自动选中新群聊）；成员管理在右侧「群聊编辑」视图完成（`renderChatGroupView` 按 `state.selectedChatGroupId` 渲染）：成员行与搜索结果行的徽标**优先展示昵称**（sender_name，无昵称回退 sender_id，悬停 title 见完整信息）；搜索身份池（`renderSearchResults` 按名称/发送者ID/昵称子串过滤、排除已在群成员、空关键字显示全部可加入成员）点「＋ 加入」`addMember`（无选中群聊 / 已在群中给 warn 提示而非静默）、成员行「✕」`removeMember`、头部「保存名称」`saveChatGroupName`、悬空成员引用（身份已删除）保留占位可移除；`#cg-members` 是**拖拽投放区**（dragover preventDefault + `.drag-over` 高亮，drop 读 id 调 `addMember`）。`syncBroadcastSenders()` 同步群发栏 `#run-sender` 身份选择器选项（「各会话自身身份」为默认）。增删改后 `refreshGroups()` 回刷组弹窗选项。
- `app.js` 分区：面板（openPanel/loadHistory/loadStream/setGlobalView/历史 JSON 编辑/重新生成）→ 发送（`sendToOne`/`sendToAll`/`regenerateMsg` 经 `registerTestConsumer` 挂消费者，`selectedBroadcastOptions()` 读群发栏身份选择器与「自动@」勾选框并入 payload，`updateRunOverview`）→ 会话操作（重置/删除）→ 面板排序（renderPanels/拖拽/置顶）→ **rail 视图切换**（`showView`：**三视图互斥**——同时驱动左侧 `.groups-card` / `.testsets-card` / `.identities-card` 三卡互斥与右侧 `.sessions-view` / `.testsets-view` / `.chat-group-view` 三工作区互斥；切到 identities 时显示右侧群聊编辑视图并调用 `renderChatGroupView()`（按当前选中群聊渲染或空态）；rail 按钮 active 互斥，点当前视图 toggle 折叠，切到测试集时 `refreshTestsets()`）→ 选项加载 → 初始化（组装 align/chat/group_list/testset_list/identity_list 与 events/testset_run 控制器，绑定全局事件；`#run-testset` change 启用 / 禁用执行按钮；末尾 `void connectEvents()`）。`loadOptions()` 拉取 platforms/confs 写入 `state`；`refreshGroups()` 来自 group_list（刷新左侧列表并清理失效面板）。面板显示视图为**全局统一**（`state.globalView`）：`setGlobalView(view)` 由轮次对齐开关旁的 `#view-toggle` 触发，统一切换全部已打开的会话并加载对应数据，`openPanel` 新开面板沿用当前全局视图；`renderChat` 包装按全局视图分发（stream → 消息流渲染，history → LLM 历史），使轮次对齐对消息流视图同样生效。
- `events.js`：事件驱动反馈层。`createEventController(env)` 由 app.js 创建，env 注入视图动作（面板状态 / 历史刷新）与跨模块延迟引用（align 控制器、测试集事件转发目标），不 import app.js / testset_run.js。`connectEvents()` 订阅 `/events` SSE（断线延迟 3s 重连，`subscribeEvents` 置空后用 `reconcileEvents` 兜底）；`handleEvent` 分发 `pending`（全量快照重建在途条）/ `session_done` / `test_done`（经 `testConsumers` 逐会话消费者投递）/ `testset`（转交 testset_run 模块的 `handleTestsetEvent`，经 `setTestsetEvent` 装配避免循环 import）；`registerTestConsumer(test_id, onSession, onAll)` 注册逐会话消费者，`applySessionFeedback(summary)` 统一逐会话反馈：面板状态 + 回复耗时 + 历史刷新——手动群发与测试集共用；`reconcileEvents()` 断线 / 初始化以轮询接口一次性快照对账（`getPending` + 在途各 test_id 逐个 `runStatus` + 有活动运行则 `runTestsetStatus`）。
- `testset_run.js`：测试集运行编排视图。`createTestsetRunController(env)` 由 app.js 创建，env 注入视图动作（showRunStatus/loadHistory）与跨模块延迟引用（refreshTestsets/applySessionFeedback），不 import app.js / events.js。`runTestset(testset, ids)` 一次启动后端运行（**无 mode**，启动文案含批量段摘要 `segmentSummary`）→ `handleTestsetEvent(run_id, run)` 消费事件流：running 时进度文案按 `segmentLabel` 标注「第 s+1–e+1 步（批量）」、逐步骤显示，**新完成步骤逐结果 `applySessionFeedback` 实时刷新面板**；终态**不弹窗**、暂存 `state.runReports[runId]` + `latestReportRunId`、显示「查看报告」按钮（`showTestsetResults` 仅按需调用）；**终态总结与表格行尾都单独统计断言未通过（`assertFails`/「断言 ✗ N」）**——断言失败不改会话 status，若总结只数 `status=="error"` 会显示「错误 0」而表格一片 ✗，误导用户；`showTestsetResults` 表格行=步骤、列=会话、单元格含断言 ✓/✗、批量段步骤带「批量」徽标 `batchBadge`；`viewTestsetRun` 从「最近运行」找回，运行中一次性重建进度后靠事件流续推；`abortTestsetRun` 请求取消，当前步骤完成即止；`runTestsetFromBar` 读 `#run-testset` 下拉对已打开会话执行。
- 群发**不阻止重叠发送**（真实「重复追问」场景，与真实平台一致由 pipeline 并发处理）；每个面板底部有在途消息条（`.panel-pending`），订阅 SSE 事件流后以 `pending` 全量快照重建 `state.pendingEntries`、`renderPendingStrip` 按会话渲染「已入队 / 排队等待 LLM / LLM 生成中 / 完成」chip（`PENDING_STATUS_TEXT`），`getPending()` 供断线对账；**完成且已刷入会话历史的消息即从条内移除**（`loadHistory` 成功记录 `historyRefreshedAt`，过滤掉 `status=="done"` 且完成于该时刻之前的条目，条内只留真正在途与完成后的短暂过渡）；strip 在 `.chat` 外不干扰轮次对齐，显隐变化后按需 `reflowAlign()`。
- 左侧布局：最左为 `.ui-rail` UI 窄条（方形按钮，当前 3 个「会话列表」/「测试集」/「身份与群聊」，点击切换视图，点当前视图按钮折叠/展开侧栏）；侧栏有三个互斥视图：`.groups-card`——测试组块（可展开组内会话），列表末尾是「＋ 新建测试组」块（点击创建默认配置组并弹编辑弹窗；列表为空时紧随空态提示之下），组头操作：打开全部（二态——组内会话全部打开时变「关闭全部」一键关闭本组全部，任一会话被单独关闭后自动回到「打开全部」、点击只补开未打开的，标签与行为都按 `allOpen` 判定，见 app.js `openAll`）/ ＋新增 / ✎编辑 / ✕删除，会话行头点击展开配置（`renderSessionConfig` 每行显示有效值 + 「已修改/继承组」chip，`sessionOverrides` 统计已单独修改的项），会话操作按钮：打开 / 删除（配置修改走展开配置中的「编辑配置」弹窗，「重置」在已打开会话的面板页眉）；`.testsets-card`——**纯命名条目**（名字 + N 条消息徽标 + 选中高亮，无内联展开 / 无行内操作按钮，点击 → `selectTestset` 打开右侧编辑窗口）+ 列表末尾「＋ 新建测试集」块 + 底部「最近运行」区；`.identities-card`——**tab 拆分**（`.tab-bar` 两个 `.tab-btn`「身份」/「群聊」，`switchIdentityTab` 切换，一次只渲染一个 `.tab-pane`）：「身份」pane = `#identity-list`（末尾「＋ 新建身份」块，管理走 modal 表单）；「群聊」pane = `#chat-group-list`（条目点击 → 右侧编辑视图，末尾「＋ 新建群聊」块——创建弹窗只填名称），见 identity_list.js。
- 工作区（`.workspace`）为 flex 列布局：顶部 `#workspace-strip` 常显状态条（运行状态 + 取消按钮，三个视图下都常显）；`.sessions-view`——`.panels-block`（包裹已打开会话面板 + 空态提示，flex:1）、`#align-bar`、`.run-bar`（从左到右三块：`.run-overview-block` 窄状态块——首行 `.run-overview-head` 左对齐 `#run-overview-count` 当前会话总数、右侧 `.run-overview-controls` **同一行右对齐**「轮次对齐」开关 `#align-toggle`（**开关式胶囊按钮**：原生勾选框隐藏，勾选时整个按钮主色高亮，仍为 checkbox 语义 JS 读 `.checked`）与**全局视图切换按钮 `#view-toggle`**（LLM 历史 / 消息流，统一切换全部已打开会话），两个按钮成组靠右、不插在会话统计信息中间，再下 `#run-overview` 按测试组逐行列出分布（每行 `.overview-item`「组名:N」，多时上下滚动，行数写 0 时隐藏）；`.run-broadcast-block` 群发区为左右布局（`.run-broadcast-row`）：左侧大 textarea `#run-text`（`.run-broadcast-input`，占据群发区大部分，Enter 换行 / Ctrl+Enter 发送）+ 右侧操作列 `.run-broadcast-side`（自上而下：身份选择器 `#run-sender`——「各会话自身身份」+ 各身份选项，由 identity_list 的 `syncBroadcastSenders` 填充；**自动@ 开关 `#run-auto-at`**（与轮次对齐同款胶囊按钮：隐藏勾选框、开启时主色高亮）默认开启；`#btn-run-all` 发送按钮）。列内子项 `align-items: flex-start` 不被拉伸，select / 按钮以 `width:100%` 占满列宽；`.run-testset-block` 测试集下拉 `#run-testset` + `#btn-run-testset`；两块共享 `.run-caption` 标题，测试集行走 `.run-field-row` 行（随群发栏高度拉伸时贴底对齐））；`.testsets-view`（初始 hidden）——`.testset-editor` 编辑窗口；`.chat-group-view`（初始 hidden）——`.chat-group-editor` 群聊编辑视图（头部名称输入 + 保存/删除按钮，成员区 `#cg-member-list` 可移除，搜索框 `#cg-search` + `#cg-search-results` 实时过滤身份池加入）。`.sessions-view` / `.testsets-view` / `.chat-group-view` 必须带 `[hidden]{display:none}` 特例（flex 列元素与 HTML hidden 的已知陷阱，沿用 groups-card）。
- 面板页眉为多行 flex 布局：标题与徽标包在 `.panel-info`（`flex-wrap: wrap`）内随内容换行撑开页眉，`.panel-actions` 始终第一行右对齐。操作按钮收敛为「⋯」下拉菜单（`.panel-menu` / `.panel-menu-dropdown`，`setupPanelMenu` 切换显隐、document 级点击外关闭）+ 常显「置顶 / 关闭」（**视图切换已移到全局**：轮次对齐开关旁的 `#view-toggle` 统一控制，面板页眉不再有 per-panel 切换按钮）：菜单项经 `data-action` 分发——`history`（JSON 编辑器，即原「编辑」按钮）、`reset`（重置历史）、`copy`（复制历史到 `state.clipboard`，去掉 conversation_id 使粘贴行为可预测）、`clone`（`promptCountDialog` 数字弹窗 → `cloneSessionApi`，同组新建 N 个历史一致的会话）、`paste`（有剪贴板内容才可用，danger 确认后经 `saveHistory` 整体覆盖）、`derive`（带命名与计数弹窗 → `deriveSessionApi`，创建全新测试组）；克隆 / 衍生后 `refreshGroups()` 刷新左侧列表。`refreshPanelHead()` / `openPanel()` 都维护该结构。
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
| GET | /confs | list_confs | 配置档案列表（`astrbot_config_mgr.get_conf_list()`；每档案含 `has_callable_tools` 工具启用标志，见「工具安全警告」） |
| GET | /platforms | list_platforms | 已启用平台适配器（防御式：单实例失败跳过） |
| GET/POST | /groups | list_groups / create_group | 测试组列表（每组含实时计算的 `security_warning` 安全标记，派生不持久化）/ 创建组（可选绑 conf_id） |
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
| GET | /sessions/\<id\>/stream | session_stream | 会话消息流（与 LLM 历史并行的运行时记录） |
| POST | /sessions/stream/clear | clear_stream | 清空指定会话的消息流 |
| POST | /reset | reset_sessions | 重置会话对话历史（联动清消息流） |
| POST | /test/run | run_test | 投递消息，立即返回 test_id（可选消息级 sender_id / sender_name / auto_at，auto_at 默认开启） |
| GET | /test/run/status | test_run_status | 查询运行状态（含统计） |
| GET/POST | /identities | list_identities / create_identity | 测试身份列表 / 创建（name 必填，sender_id/sender_name 缺失回退名称） |
| POST | /identities/delete | delete_identities | 删除测试身份 |
| POST | /identities/\<id\>/update | update_identity | 更新测试身份 |
| GET/POST | /chat-groups | list_chat_groups / create_chat_group | 虚拟群聊列表 / 创建（member_ids 引用身份 id） |
| POST | /chat-groups/delete | delete_chat_groups | 删除虚拟群聊 |
| POST | /chat-groups/\<id\>/update | update_chat_group | 更新虚拟群聊 |
| GET/POST | /testsets | list_testsets / create_testset | 测试集列表 / 创建测试集（消息序列可带断言规则与可选身份 / auto_at；允许空消息先建条目；可选 batch_ranges 批量段，非法 → 400） |
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
> 由 GitHub Actions 自动把关——push 到 dev 触发 `pytest.yml`（190 个测试 +
> 前端 JS 语法检查 `js-check`：node --check 十三个页面脚本）+ `ruff-format.yml`；
> dev 验证通过后合并到 `main`，metadata.yaml 变更即触发 release.yml 自动发版。
> 本地命令（下面的 pytest/ruff）仅在需要主动排查时使用。

测试随插件仓库维护（`tests/`，可与主仓库无关地推送、供协作者运行）。

- `tests/test_backend.py`：后端单元测试（153 个），需要 astrbot（PyPI 包，插件运行时依赖）。以 **namespace package** 加载插件：`sys.path.insert(0, str(REPO_ROOT.parent))` 后 `import astrbot_plugin_testbench.*`——插件模块用相对导入（`from .group_store import ...`），必须按包加载，这与 AstrBot 在 data/plugins 下加载插件的方式一致。未安装 astrbot 时整组跳过（`pytest.importorskip`）。
- `tests/test_frontend.py`：前端脚本静态检查（37 个），零依赖，任何环境可运行。

本地运行（用主仓库 venv，bash cwd 不稳定，命令先 `cd /e/AstrBot` 或 `git -C` 插件目录）：

```bash
cd /e/AstrBot && .venv/Scripts/python.exe -m pytest data/plugins/astrbot_plugin_testbench/tests/ -q
cd /e/AstrBot && .venv/Scripts/python.exe -m ruff check data/plugins/astrbot_plugin_testbench/tests/
```

CI（`.github/workflows/pytest.yml`）：ubuntu + Python 3.12，`pip install astrbot pytest pytest-asyncio` 后跑 `pytest tests/ -q`，随 push/PR 触发。

前端 ES module 语法检查（node 不认 .js 里的 import，须复制为 .mjs；页面全部 13 个模块逐一检查，与 CI js-check 一致）：

```bash
for f in app api align chat state utils modal group_list testset_list testset_editor events testset_run identity_list; do
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
