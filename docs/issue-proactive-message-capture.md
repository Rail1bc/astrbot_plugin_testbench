# 问题记录：虚拟会话无法捕获主动推送（proactive send）

- 记录日期：2026-08-07
- 状态：**已记录，暂不实施**
- 结论：目标方案为 **B2（AstrBot 核心加"主动消息发送"钩子）**，属于跨仓库改动，需 AstrBot 核心 PR + 发版配合；本插件暂不改动。

## 问题场景

测试"主动回复插件"：一个插件在每天固定时间（cron）触发，激活 bot 向用户问候。虚拟会话
（umo 如 `webchat:FriendMessage:vs_xxx`）**完全无法收到该问候**：

1. 问候最终全部走 `Context.send_message(umo, chain)`（第三方插件、AstrBot 内置
   `send_message_to_user` 工具 `astrbot/core/tools/message_tools.py:338` 同一条路径）。
2. `Context.send_message`（`astrbot/core/star/context.py:508-571`）按 umo 的 platform 名
   匹配 `platform_manager.platform_insts`，命中后调用 `platform.send_by_session(...)`——
   这是**出站路径**，不产生入站事件。
3. 默认 platform 为 webchat：`webchat_adapter.send_by_session`（webchat_adapter.py:86-134）
   发现 `vs_xxx` 无活动前端连接 → `_save_proactive_message`（136-155）把问候以 bot 角色
   **写进真实的 `platform_message_history` 表**（user_id=`vs_xxx`），随后基类仅上报 metric。
4. 虚拟会话唯一的入站通道是 `runner.start()` 注入事件队列；出站主动推送不经过任何
   `VirtualMessageEvent`，`captured` / 运行结果 / 消息流 / 评审 / 面板事件全部无关。

## 影响

- 无法用测试台验证主动回复插件（问候不可见、不可断言、不可评审）。
- 副作用：webchat 情况下问候被写进真实 DB，产生按 `vs_xxx` 会话的"幽灵"bot 记录。
- 若测试组把 platform_id 配成真实平台，`send_by_session` 会尝试向不存在的 `vs_xxx`
  会话真实发送 → 失败 / 报错日志。

## 已评估方案

### B1：插件自注册"虚拟平台"适配器（不选，脆弱）

- 可行性：`Platform` 抽象类只需实现 `run()` / `meta()`（`astrbot/core/platform/platform.py:121-132`），
  `send_by_session` 有默认实现可覆写；插件公开 `Context` 暴露 `platform_manager`
  （`astrbot/core/star/context.py:155`），可往 `platform_insts` 追加适配器实例。
- 设计：组配置 opt-in 新 platform_id（如 `virtual`），适配器在 `send_by_session` 捕获进
  stream_store 并发布事件。所有主动发送都汇聚到 `Context.send_message`，可全覆盖。
- 不选原因：
  1. 往核心 `platform_insts` 塞适配器是摸内部实现的 hack，AstrBot 内部结构变更即失效；
  2. 换 platform id 改变 umo，platform-name 类 filter 会跳过该平台（message-type 类不受影响）；
  3. 默认 webchat 组不受益，必须 opt-in；
  4. 需处理新平台在 UCR 路由 / 历史隔离的联动（已有 platform_changed 清理基建可复用，但仍是额外面）。

### B2：AstrBot 核心加"主动消息发送"钩子（目标方案，跨仓库）

- 设计：在核心 `Context.send_message`（或 `Platform.send_by_session` 统一出口）增加可订阅
  钩子——filter 风格（如 `on_message_sent(umo, message_chain)`）或回调注册表。发送时先通知
  订阅者再落地。
- 本插件侧：订阅该钩子；目标 umo 命中虚拟会话（`vs_` 前缀 / 会话表存在）时，把消息写入
  stream_store（bot 角色）、按会话广播事件，供前端展示与后续评审/断言扩展。
- 优点：干净；与组 platform_id 无关（webchat / 真实平台组全覆盖）；不改变任何平台语义。
- 代价：需要 AstrBot 核心 PR + 发版；插件 bump `astrbot_version` 并跟随发版节奏；
  两个仓库协调交付。
- 后续实施要点（本插件侧）：
  1. 订阅钩子 → 按 umo 查虚拟会话 → 命中则写 `stream_store.append(role="bot")`；
  2. 消息流前端已支持 bot 消息渲染，可零改动展示；
  3. 断言/评审：需新设计"主动消息期望"规则类型（如测试集步骤声明"在 N 秒内应收到主动消息"），
     超出当前范围，另行设计。

### B3：读 webchat 已落库的 proactive 消息（不选，仅 webchat 的过渡 hack）

- 可行性：webchat 的 proactive 路径已把消息写进 `platform_message_history`
  （webchat_adapter.py:136-155，user_id=vs_xxx）；插件公开 `Context` 暴露
  `message_history_manager`（`astrbot/core/star/context.py:159`），可轮询读出并入消息流。
- 不选原因：只对 webchat 生效；轮询有延迟；本质是"读泄漏进 DB 的数据"而非真捕获；
  不能用于断言评审。

## 决策

- **采用 B2**，作为目标方案记录；本插件当前**不实施**。
- 在 AstrBot 核心存在主动发送钩子之前，主动回复插件的测试需求保持"不受支持"的已知限制。
- 与缺口 A（出方向异步回复捕获，见 `docs/eval-async-reply-settle.md`）互相独立，互不阻塞。
