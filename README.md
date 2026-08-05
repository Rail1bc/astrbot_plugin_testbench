<!-- markdownlint-disable MD041 -->

# 会话测试台 (astrbot_plugin_testbench)

一个为 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 设计的虚拟会话并发测试插件。

> [!NOTE]
> 当前版本 v0.3.0，支持测试组模型、并行会话查看、逐会话独立刷新、群发会话实时统计与历史 JSON 编辑 / 重新生成。

## ✨ 功能特性

- **与真实会话无差别的虚拟会话**：消息通过框架原生插件页面注入 AstrBot 事件总线，走与真实平台消息完全相同的处理管道（唤醒检查 → 白名单 → 会话状态 → 限流 → 内容安全 → 预处理 → 插件 + LLM 处理 → 回复装饰）。
- **测试组模型**：左侧以「测试组」为单位组织会话。一组共享同一套配置（平台来源、配置档案、发送者 id、发送者昵称），组内单个会话可单独覆盖组配置；组内可随时新增 / 删除会话，测试以组为单位开展。
- **并行会话查看**：点击会话「打开」在右侧显示会话面板，可同时打开 2–3 个甚至更多面板并行查看对话历史；面板支持拖拽排序与置顶，支持按轮次对齐阅读。
- **配置档案绑定**：测试组或单个会话可选择配置档案，通过 AstrBot 原生 UCR 路由精确绑定（`platform:type:session` → conf），互不影响、删除时自动清理。
- **并发测试**：一条消息同时发送给所有已打开的会话，每个会话窗口**独立实时刷新**——谁完成谁更新，无需等待整批结束；不设总超时、不额外分批，消息直接进入 AstrBot 原生 pipeline（与真实环境一致）。群发栏实时显示当前打开的会话数量及其所属测试组分布（如「当前会话:8 提示词测试组:5 模型测试组:3」）。每个面板展示状态（成功 / 无回复 / 错误）、回复、耗时与 min / max / avg / p50 / p95 统计；也可在单个面板内单独发送消息。
- **历史 JSON 编辑与重新生成**：面板头部「历史」按钮打开 JSON 编辑器，直接编辑 `{conversations: [...]}` 结构化对话历史——编辑单条消息、新增或删除对话都在 JSON 中完成，保存即整体替换（未列出的对话会被删除），供有能力的用户精细调整；用户发言气泡悬停可「重新生成」——截断该轮及之后的历史并重新走 pipeline 生成新回复。
- **对话历史查看与重置**：每个面板展示该虚拟会话的完整对话历史（含推理内容与工具调用），支持一键重置。
- **会话管理**：测试组与虚拟会话持久化存储，旧版平铺会话数据自动迁移为「默认测试组」。

## 🚀 安装与使用

1. 从本仓库 [Release](https://github.com/Rail1bc/astrbot_plugin_testbench/releases) 下载 `astrbot_plugin_testbench` 的 `.zip` 文件。
2. 在 AstrBot WebUI 的插件页面中选择「从文件安装」。
3. 在插件页面中启用插件，进入「会话测试台」页面。

### 插件页面使用流程

1. **创建测试组**：在左侧创建表单设置组名称、会话数量、平台来源与配置档案（可选发送者信息），点击「创建测试组」，组内自动生成相应数量的虚拟会话（均继承组配置）。
2. **管理组内会话**：展开测试组可看到组内会话列表；点击「添加」随时为组新增会话，或删除组内单个会话、整个测试组。会话右侧「配置」按钮可单独覆盖该会话的平台 / 配置档案 / 发送者信息（留空恢复继承组配置）。
3. **打开 / 切换会话**：点击会话「打开」在右侧并行查看；再次点击「关闭」收起。面板头部可拖拽排序，或点击「置顶」固定在前面。
4. **发送消息**：在单个面板底部输入框发送消息到该会话；或在顶部群发栏输入一条消息，点击「发送到全部」并发发送给所有已打开的会话。
5. **查看结果**：每个面板展示状态（成功 / 无回复 / 错误）与对话历史更新；群发时每个会话窗口**独立刷新**——谁完成谁更新，无需等待整批结束；全部完成后顶部汇总成功 / 无回复 / 错误计数与 min / avg / p95 耗时统计。

> [!TIP]
> 虚拟会话默认以 `webchat` 为平台来源（与 AstrBot WebUI 一致），发送者默认 `testbench` / `测试台`。测试以组为单位，组内会话共享平台 / 配置档案 / 发送者信息，单会话可覆盖；运行测试时也可临时指定配置档案或 Provider / 模型进行覆盖。

## 📂 插件目录与结构

```text
data/plugins/astrbot_plugin_testbench/
├─ main.py               # 插件主入口文件（Star 类 + Web API 后端）
├─ group_store.py        # 测试组数据模型与持久化（纯数据层）
├─ runner.py             # 并发测试运行器（流式汇总结果）
├─ stats.py              # 耗时统计工具（纯函数）
├─ virtual_event.py      # 虚拟消息事件（捕获回复，不真实外发）
├─ metadata.yaml         # 插件元数据信息
├─ CHANGELOG.md          # 更新日志
├─ README.md             # 插件说明文档
├─ pages/virtual-session/ # 插件页面（前端：app.js / api.js / align.js / style.css）
└─ .github/              # GitHub 工作流与协作模板
```

## 🔧 开发与测试

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
