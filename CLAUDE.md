# CLAUDE.md

本文件为 Claude Code 在本插件（`astrbot_plugin_testbench`）目录下工作提供指引。

## 插件概述

会话测试台（astrbot_plugin_testbench）是一个 AstrBot 插件：通过框架原生插件页面创建「虚拟会话」，并把一句话并发投递给多个虚拟会话，用于测试插件、提示词、模型与整体稳定性。

- **版本**：v0.3.0（metadata.yaml 中的版本号，未经用户批准不得擅自 bump）
- **兼容范围**：`astrbot_version: ">=4.16"`
- **独立 git 仓库**：remote `git@github.com:Rail1bc/astrbot_plugin_testbench.git`，**默认分支 `main`**（不是 AstrBot 主仓库的 `dev`）
- **无第三方依赖**：只依赖 AstrBot 公共 API（`astrbot.api.*`），不需要 requirements.txt
- **核心卖点**：虚拟会话与真实会话走**完全相同的处理路径**（不是模拟），只是把消息注入点从平台适配器换成了插件侧直接入队

### 消息处理路径（核心机制）

```
页面 (pages/virtual-session/)  ->  Web API (main.py)  ->  VirtualTestRunner.start()
  -> context.get_event_queue()  ->  EventBus -> PipelineScheduler.execute()
  -> 完整 pipeline（唤醒检查→白名单→会话状态→限流→内容安全→预处理→插件+LLM→装饰→回复）
  -> VirtualMessageEvent.send()/send_streaming() 捕获回复（不外发）
```

与真实平台一致：不设总超时、不分批投递。事件入队后 runner 后台逐个等待完成，前端轮询 `status` 接口实现「每个会话窗口独立刷新」。

## 目录结构

```
astrbot_plugin_testbench/
├─ metadata.yaml        # 插件元数据（name/display_name/version/astrbot_version）
├─ main.py              # Star 类：Web API 路由 + 全部请求处理器（563 行）
├─ group_store.py       # 测试组数据模型与持久化（VirtualGroupManager、umo_of）
├─ runner.py            # 并发测试运行器（VirtualTestRunner）
├─ stats.py             # 耗时统计纯函数（duration_stats：min/max/avg/p50/p95）
├─ virtual_event.py     # VirtualMessageEvent：捕获 send/流式结果，携带完成信号
├─ pages/virtual-session/
│  ├─ index.html        # 页面骨架（表单/面板的静态 HTML，select 初始为空或仅默认项）
│  ├─ app.js            # 页面主逻辑（1089 行，ES module）
│  ├─ api.js            # bridge 调用的统一封装（listPlatforms/listConfs/...）
│  ├─ align.js          # 轮次对齐控制器（createAlignController，依赖注入）
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

- 绑定用**会话级精确路由** `umo → conf_id`（`ucr.update_route(umop, conf_id)`），不用平台级 `platform_id::`，避免影响同平台其他会话。
- 创建组/添加会话时若带 `conf_id`，调用 `_apply_conf_routes`；删除组/会话/重置时用 `_clear_conf_routes`（仅删已存在的路由）+ `_delete_session_conversations`（级联删原生对话历史，按 umo 调 `conversation_manager.delete_conversations_by_user_id`）。
- runner 的临时路由（测试运行时指定 conf_id）带 `asyncio.Lock` 串行：保存原路由 → 应用 → 全部完成后恢复并释放锁，避免临时路由互相污染。

### VirtualMessageEvent（virtual_event.py）

继承 `AstrMessageEvent`，消息类型 `FRIEND_MESSAGE`（私聊默认直接唤醒，无需唤醒前缀）：

- `create(...)` 构造 `AstrBotMessage`（`self_id="virtual_bot"`），`selected_provider` / `selected_model` 写入 event extra。
- **两个完成信号**：
  - `done_event`：产生过一次回复（send 或流式结束）时置位。
  - `pipeline_done_event`：pipeline 全部执行完（无论是否产生回复）后置位。**实现技巧**：重写 `cleanup_temporary_local_files()`——它是 `PipelineScheduler.execute()` 的 finally 块中唯一调用点（astrbot/core/pipeline/scheduler.py:97），runner 用 `.wait()` 精确等待 pipeline 结束。
- `send()` 捕获 `MessageChain` 到 `self.captured`；`send_streaming()` 按 aiocqhttp/discord 的累积模式合并 chunk → `squash_plain()` → 交给 `send()`，reasoning 单独累积。
- `result_summary()` 返回 `{umo, session_id, status(ok|no_reply), duration, reply, reasoning, error}`；错误文案从 `_llm_error_message` extra 读取。

### 运行器（runner.py）

- `start(sessions, text, provider_id, model, conf_id)` **立即返回 test_id**（不等待回复）；事件 `put_nowait` 入队，逐事件 `asyncio.create_task(_await_event)` 等待 `pipeline_done_event`。
- `status(test_id)` 返回 `{total, done, results[], stats}`，前端轮询实现逐会话独立刷新。
- 运行记录保存在内存 `self._runs`，完成超过 10 分钟自动清理。

### 前端（pages/virtual-session/）

- `api.js`：`window.AstrBotPluginPage` bridge 的 `apiGet`/`apiPost` 统一封装。bridge 的响应解析是 `response.data?.data ?? response.data`（json_response 的 body 直接作为 data）。
- `app.js` 分区：弹窗（iframe 沙箱禁用原生 alert/confirm，用自绘弹窗）→ 测试组列表 → 面板 → 发送 → 会话操作 → 创建 → 面板排序 → 轮次对齐 → 选项加载 → 初始化。`loadOptions()` 拉取 platforms/confs 填充下拉框；`refreshGroups()` 渲染左侧组列表；`pollRun()` 轮询测试状态；`updateRunOverview()` 在群发栏实时显示当前打开的会话数与按测试组的分布。
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
| GET | /sessions | list_sessions | 全部会话（已解析最终配置） |
| POST | /sessions/update | update_session | 会话配置覆盖（null 恢复继承组配置） |
| POST | /sessions/delete | delete_sessions | 删会话 + 联动清理 |
| GET | /sessions/\<id\>/history | session_history | 对话历史（LLM 上下文消息列表） |
| POST | /sessions/history/save | save_history | 整体替换对话历史（带 cid 更新、无 cid 新建、带不存在的 cid 也新建占位对话、未列出删除；JSON 编辑器保存） |
| POST | /sessions/history/regenerate | regenerate_history | 截断该轮之后历史并重发该轮 user 消息 |
| POST | /reset | reset_sessions | 重置会话对话历史 |
| POST | /test/run | run_test | 投递消息，立即返回 test_id |
| GET | /test/run/status | test_run_status | 查询运行状态（含统计） |

统一用 `astrbot.api.web` 的 `json_response` / `error_response` / `request`。

## 测试与验证

测试随插件仓库维护（`tests/`，可与主仓库无关地推送、供协作者运行）。

- `tests/test_backend.py`：后端单元测试（51 个），需要 astrbot（PyPI 包，插件运行时依赖）。以 **namespace package** 加载插件：`sys.path.insert(0, str(REPO_ROOT.parent))` 后 `import astrbot_plugin_testbench.*`——插件模块用相对导入（`from .group_store import ...`），必须按包加载，这与 AstrBot 在 data/plugins 下加载插件的方式一致。未安装 astrbot 时整组跳过（`pytest.importorskip`）。
- `tests/test_frontend.py`：前端脚本静态检查（2 个），零依赖，任何环境可运行。

本地运行（用主仓库 venv，bash cwd 不稳定，命令先 `cd /e/AstrBot` 或 `git -C` 插件目录）：

```bash
cd /e/AstrBot && .venv/Scripts/python.exe -m pytest data/plugins/astrbot_plugin_testbench/tests/ -q
cd /e/AstrBot && .venv/Scripts/python.exe -m ruff check data/plugins/astrbot_plugin_testbench/tests/
```

CI（`.github/workflows/pytest.yml`）：ubuntu + Python 3.12，`pip install astrbot pytest pytest-asyncio` 后跑 `pytest tests/ -q`，随 push/PR 触发。

前端 ES module 语法检查（node 不认 .js 里的 import，须复制为 .mjs）：

```bash
cp app.js "$TEMP/app.mjs" && node --check "$TEMP/app.mjs"   # 同理 api.js / align.js
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
