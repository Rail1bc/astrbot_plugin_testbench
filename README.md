<!-- markdownlint-disable MD041 -->

# 会话测试台 (astrbot_plugin_testbench)

一个为 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 设计的虚拟会话并发测试插件。

> [!NOTE]
> 当前版本 v0.4.2，支持测试组模型、并行会话查看、逐会话独立刷新、群发重叠发送（agent 处理中可再次群发）、面板在途消息实时状态、群发会话实时统计与历史 JSON 编辑 / 重新生成；并支持群聊消息类型与自动@、测试身份与虚拟群聊、群消息流视图与唤醒状态反馈。

## ✨ 功能特性

- **与真实会话无差别的虚拟会话**：消息通过框架原生插件页面注入 AstrBot 事件总线，走与真实平台消息完全相同的处理管道（唤醒检查 → 白名单 → 会话状态 → 限流 → 内容安全 → 预处理 → 插件 + LLM 处理 → 回复装饰）。
- **测试组模型**：左侧以「测试组」为单位组织会话。一组共享同一套配置（平台来源、配置档案、发送者 id、发送者昵称），组内单个会话可单独覆盖组配置；组内可随时新增 / 删除会话，测试以组为单位开展。
- **并行会话查看**：点击会话「打开」在右侧显示会话面板，可同时打开 2–3 个甚至更多面板并行查看对话历史；面板支持拖拽排序与置顶，支持按轮次对齐阅读。
- **配置档案绑定**：测试组或单个会话可选择配置档案，通过 AstrBot 原生 UCR 路由精确绑定（`platform:type:session` → conf），互不影响、删除时自动清理。
- **消息类型与自动@（群聊虚拟会话）**：测试组 / 会话可设为群聊消息类型（GroupMessage），使只监听群消息的插件（如 Heartflow 主动回复插件）可被虚拟会话触发；发送时可选「自动@机器人」唤醒——开启则消息链以 @ 开头直接唤醒，关闭则以未唤醒状态进管道、只能被 filter 通过唤醒。唤醒状态与原因（未唤醒 / 已唤醒但无回复）在结果摘要与面板状态中可见。
- **测试身份与虚拟群聊**：左侧「身份与群聊」视图集中管理**跨测试组共享**的测试身份（sender 实体）与虚拟群聊（成员池）；群聊会话可绑定虚拟群聊作为群成员来源，群发栏与测试集消息也可按身份发送。
- **群消息流视图**：每个会话面板可切换「LLM 历史 ↔ 消息流」——消息流是与 LLM 历史并行的纯记录（不注入 LLM 上下文），还原真实消息收发过程与回复状态（成功 / 无回复 / 错误）。
- **并发测试**：一条消息同时发送给所有已打开的会话，每个会话窗口**独立实时刷新**——谁完成谁更新，无需等待整批结束；不设总超时、不额外分批，消息直接进入 AstrBot 原生 pipeline（与真实环境一致）。**群发不阻止重叠发送**：agent 处理上一条消息时可再次群发，覆盖真实「重复追问」场景。每个面板底部有在途消息条，实时显示每条消息「已入队 / 排队等待 LLM / LLM 生成中 / 完成」四个阶段，完成后自动刷入会话历史并从条内移除。群发栏实时显示当前打开的会话数量及其所属测试组分布（如「当前会话:8 提示词测试组:5 模型测试组:3」）。每个面板展示状态（成功 / 无回复 / 错误）、回复、耗时与 min / max / avg / p50 / p95 统计；也可在单个面板内单独发送消息。
- **历史 JSON 编辑与重新生成**：面板头部「编辑」按钮打开 JSON 编辑器，直接编辑 `{conversations: [...]}` 结构化对话历史——编辑单条消息、新增或删除对话都在 JSON 中完成，保存即整体替换（未列出的对话会被删除），供有能力的用户精细调整；用户发言气泡悬停可「重新生成」——截断该轮及之后的历史并重新走 pipeline 生成新回复。
- **对话历史查看与重置**：每个面板展示该虚拟会话的完整对话历史（含推理内容与工具调用）；带推理内容的 LLM 回复默认折叠，点击「展开思维链」即可查看思维链。支持一键重置。
- **会话管理**：测试组与虚拟会话持久化存储，旧版平铺会话数据自动迁移为「默认测试组」。

## 🚀 安装与使用

1. 从本仓库 [Release](https://github.com/Rail1bc/astrbot_plugin_testbench/releases) 下载 `astrbot_plugin_testbench` 的 `.zip` 文件。
2. 在 AstrBot WebUI 的插件页面中选择「从文件安装」。
3. 在插件页面中启用插件，进入「会话测试台」页面。

### 插件页面使用流程

1. **创建测试组**：在左侧列表点击「＋ 新建测试组」即创建默认配置的测试组，随后在弹出的编辑弹窗中设置组名称、会话数量、平台来源与配置档案（可选发送者信息、消息类型与绑定虚拟群聊），保存后组内自动生成相应数量的虚拟会话（均继承组配置）。
2. **管理组内会话**：展开测试组可看到组内会话列表；点击「添加」随时为组新增会话，或删除组内单个会话、整个测试组。点击会话行头部展开配置，逐项查看有效值与「已修改 / 继承组」状态，点击「编辑配置」可单独覆盖该会话的平台 / 配置档案 / 发送者信息 / 消息类型（留空恢复继承组配置）。
3. **打开 / 切换会话**：点击会话「打开」在右侧并行查看；再次点击「关闭」收起。面板头部可拖拽排序，或点击「置顶」固定在前面。面板页头的视图切换按钮可在「LLM 历史 ↔ 消息流」间切换展示。
4. **发送消息**：在单个面板底部输入框发送消息到该会话；或在工作区下方群发栏输入一条消息，点击「发送到全部」并发发送给所有已打开的会话（群发栏可选发送身份与是否自动@）。
5. **查看结果**：每个面板展示状态（成功 / 无回复 / 错误，无回复时区分「未唤醒」与「已唤醒但无回复」）与对话历史更新；群发时每个会话窗口**独立刷新**——谁完成谁更新，无需等待整批结束；全部完成后顶部汇总成功 / 无回复 / 错误计数与 min / avg / p95 耗时统计。

> [!TIP]
> 虚拟会话默认以 `webchat` 为平台来源（与 AstrBot WebUI 一致），发送者默认 `testbench` / `测试台`。测试以组为单位，组内会话共享平台 / 配置档案 / 发送者信息 / 消息类型，单会话可覆盖；运行测试时也可临时指定配置档案或 Provider / 模型进行覆盖。群聊消息类型的会话需借助「自动@」或 filter（如 Heartflow）唤醒；测试身份与虚拟群聊在左侧「身份与群聊」视图管理。

## 📂 插件目录与结构

```text
data/plugins/astrbot_plugin_testbench/
├─ main.py               # 插件主入口（Star：依赖装配 + 路由注册 + LLM 阶段 hook）
├─ api/                  # Web API 路由层（meta/groups/sessions/runs/testsets/identities/events）
├─ core/                 # 运行编排层（runner/testset_runner/event_bus/virtual_event/conf_routes）
├─ store/                # 持久化与数据模型（group_store/identity_store/stream_store/testset_store）
├─ eval/                 # 断言评估层（mechanical：正则/包含/格式等机械规则）
├─ history_ops.py        # 会话对话历史操作（保存/重新生成/复制/级联删除）
├─ stats.py              # 耗时统计工具（纯函数）
├─ metadata.yaml         # 插件元数据信息
├─ CHANGELOG.md          # 更新日志
├─ README.md             # 插件说明文档
├─ pages/testbench/      # 插件页面（前端：app/api/align/chat/state/utils/modal/group_list/testset_list/testset_editor/identity_list/events/testset_run/pure.js + index.html/style.css）
└─ .github/              # GitHub 工作流与协作模板
```

## 🔧 开发与测试

> **开发流程**：本地不跑测试，修改直接提交推送到 `dev` 分支，由 GitHub Actions 自动把关——push 到 dev 触发 `pytest.yml`（280 个测试函数 + 前端 JS 检查：node --check 语法检查与 node:test 纯函数动态测试）与 `ruff-format.yml`；dev 验证通过后合并到 `main`，metadata.yaml 变更即触发 release.yml 自动发版。以下本地命令仅在主动排查时使用。

```bash
# 单元测试（测试随插件仓库维护，位于 data/plugins/astrbot_plugin_testbench/tests/）
.venv/Scripts/python.exe -m pytest data/plugins/astrbot_plugin_testbench/tests/ -q

# 代码质量检查
.venv/Scripts/python.exe -m ruff check data/plugins/astrbot_plugin_testbench/tests/
.venv/Scripts/python.exe -m ruff format --check data/plugins/astrbot_plugin_testbench/tests/
```

Windows 开发者可直接运行 `run_ruff.bat` 进行格式化与质量检查。

## 📄 许可证

[GNU Affero General Public License v3.0](LICENSE)
