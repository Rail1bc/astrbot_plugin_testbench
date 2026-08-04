<!-- markdownlint-disable MD024 -->
<!-- markdownlint-disable MD025 -->
<!-- markdownlint-disable MD033 -->
<!-- markdownlint-disable MD034 -->
<!-- markdownlint-disable MD041 -->
# ChangeLog

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
