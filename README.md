<!-- markdownlint-disable MD041 -->

# 会话测试台 (astrbot_plugin_testbench)

一个为 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 设计的虚拟会话并发测试插件。

> [!NOTE]
> 当前版本 v0.2.0，支持并行会话查看与配置档案绑定。

## ✨ 功能特性

- **与真实会话无差别的虚拟会话**：消息通过框架原生插件页面注入 AstrBot 事件总线，走与真实平台消息完全相同的处理管道（唤醒检查 → 白名单 → 会话状态 → 限流 → 内容安全 → 预处理 → 插件 + LLM 处理 → 回复装饰）。
- **并行会话查看**：左侧会话列表创建、切换会话，右侧可同时打开 2–3 个甚至更多会话面板并行查看对话历史；面板支持拖拽排序与置顶。
- **会话级配置档案**：创建虚拟会话时选择平台与配置档案，通过 AstrBot 原生 UCR 路由精确绑定到该会话（`platform:type:session` → conf），互不影响、删除会话时自动清理。
- **并发测试**：一条消息同时发送给所有已打开的会话（可选超时与分批投递），每个面板实时展示状态（成功 / 无回复 / 超时 / 错误）、回复、耗时与 min / max / avg / p50 / p95 统计；也可在单个面板内单独发送消息。
- **对话历史查看与重置**：每个面板展示该虚拟会话的完整对话历史（含推理内容与工具调用），支持一键重置。
- **会话管理**：虚拟会话持久化存储，支持批量创建、单个删除与对话历史重置。

## 🚀 安装与使用

1. 从本仓库 [Release](https://github.com/Rail1bc/astrbot_plugin_testbench/releases) 下载 `astrbot_plugin_testbench` 的 `.zip` 文件。
2. 在 AstrBot WebUI 的插件页面中选择「从文件安装」。
3. 在插件页面中启用插件，进入「会话测试台」页面。

### 插件页面使用流程

1. **创建虚拟会话**：在左侧创建表单设置数量、平台来源与配置档案（可选发送者信息），点击「创建会话」，新会话会自动打开。
2. **打开 / 切换会话**：点击左侧会话列表的「打开」即可在右侧并行查看该会话；再次点击「关闭」收起。面板头部可拖拽排序，或点击「置顶」固定在前面。
3. **发送消息**：在单个面板底部输入框发送消息到该会话；或在顶部群发栏输入一条消息，点击「发送到全部」并发发送给所有已打开的会话。
4. **查看结果**：每个面板展示状态（成功 / 无回复 / 超时 / 错误）与对话历史更新；群发完成后顶部显示总数与 min / avg / p95 耗时统计。

> [!TIP]
> 虚拟会话使用独立的虚拟平台 id（默认 `virtual_test`），创建时绑定到所选配置档案；测试无需再选择 Provider / 模型，由会话绑定的配置档案决定。

## 📂 插件目录与结构

```text
data/plugins/astrbot_plugin_testbench/
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
.venv/Scripts/python.exe -m pytest tests/unit/test_testbench_plugin.py -v

# 代码质量检查
.venv/Scripts/python.exe -m ruff check data/plugins/astrbot_plugin_testbench tests/unit/test_testbench_plugin.py
.venv/Scripts/python.exe -m ruff format --check data/plugins/astrbot_plugin_testbench tests/unit/test_testbench_plugin.py
```

Windows 开发者可直接运行 `run_ruff.bat` 进行格式化与质量检查。

## 📄 许可证

[GNU Affero General Public License v3.0](LICENSE)
