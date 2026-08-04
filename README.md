<!-- markdownlint-disable MD041 -->

# 虚拟会话测试平台 (astrbot_plugin_virtual_session)

一个为 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 设计的虚拟会话并发测试插件。

> [!NOTE]
> 当前版本 v0.1.0，核心功能已可用。

## ✨ 功能特性

- **与真实会话无差别的虚拟会话**：消息通过框架原生插件页面注入 AstrBot 事件总线，走与真实平台消息完全相同的处理管道（唤醒检查 → 白名单 → 会话状态 → 限流 → 内容安全 → 预处理 → 插件 + LLM 处理 → 回复装饰）。
- **并发测试**：一条消息同时发送给 N 个虚拟会话（可选分批投递与批间隔），用于测试插件、提示词、模型本身以及整体链路的性能与稳定性。
- **结果汇总**：每个会话独立展示状态（成功 / 无回复 / 超时 / 错误）、回复内容、推理内容与耗时；提供 min / max / avg / p50 / p95 统计。
- **定向测试**：可为单次测试指定 Provider、模型，或将虚拟会话路由到指定配置档案（UCR），用于对比提示词 / 系统设定效果。
- **会话管理**：虚拟会话持久化存储，支持批量创建、勾选删除与对话历史重置。

## 🚀 安装与使用

1. 从本仓库 [Release](https://github.com/Rail1bc/astrbot_plugin_virtual_session/releases) 下载 `astrbot_plugin_virtual_session` 的 `.zip` 文件。
2. 在 AstrBot WebUI 的插件页面中选择「从文件安装」。
3. 在插件页面中启用插件，进入「虚拟会话测试平台」页面。

### 插件页面使用流程

1. **创建虚拟会话**：在页面顶部设置数量、平台来源、发送者信息后点击「创建会话」。
2. **勾选目标会话**：在会话列表中勾选要测试的虚拟会话。
3. **配置测试参数**：输入测试消息，按需选择 Provider、模型、配置档案、超时时间、每批数量与批间隔。
4. **运行测试**：点击「运行测试」，等待结果表格与耗时统计刷新。

> [!TIP]
> 虚拟会话使用独立的虚拟平台 id（默认 `virtual_test`），默认路由到「默认」配置档案；如需测试其他配置档案，在测试表单中选择对应档案即可。

## 📂 插件目录与结构

```text
data/plugins/astrbot_plugin_virtual_session/
├─ main.py               # 插件主入口文件（Star 类 + Web API 后端）
├─ runner.py             # 会话管理 + 并发测试运行器
├─ virtual_event.py      # 虚拟消息事件（捕获回复，不真实外发）
├─ metadata.yaml         # 插件元数据信息
├─ CHANGELOG.md          # 更新日志
├─ README.md             # 插件说明文档
├─ pages/virtual-session/ # 插件页面（前端）
└─ .github/              # GitHub 工作流与协作模板
```

## 🔧 开发与测试

```bash
# 单元测试（插件位于 data/plugins 下，测试在插件缺失时会自动跳过）
.venv/Scripts/python.exe -m pytest tests/unit/test_virtual_session_plugin.py -v

# 代码质量检查
.venv/Scripts/python.exe -m ruff check data/plugins/astrbot_plugin_virtual_session tests/unit/test_virtual_session_plugin.py
.venv/Scripts/python.exe -m ruff format --check data/plugins/astrbot_plugin_virtual_session tests/unit/test_virtual_session_plugin.py
```

Windows 开发者可直接运行 `run_ruff.bat` 进行格式化与质量检查。

## 📄 许可证

[GNU Affero General Public License v3.0](LICENSE)
