# 问题记录：评审材料中被测 agent 系统提示词取自配置回退，非真实传入 LLM 的版本

- 记录日期：2026-08-07
- 状态：**已记录，暂不实施**（用户拍板：暂时按下不动）
- 结论：评审输入里【以下是被测 Agent 系统提示词】块的内容，在捕获快照为空 / 缺失时由
  `eval/persona.py` 的 `resolve_agent_system_prompt` **从会话配置档案重新解析人格**补入
  （`persona_manager.resolve_selected_persona` → `format_persona_snapshot`）。这是「按配置
  重建」，**不是**真实传给被测 LLM 的那份 `req.system_prompt`——后者可能经过插件或其他
  环节修改。当前评审材料对这类会话展示的是配置快照而非实际输入。**暂不修复**。

## 背景：两条获取路径

1. **捕获路径（真值）**：main.py 的 `on_llm` hook（`OnLLMRequestEvent`，装饰完成后、
   调用前触发）快照 `req.system_prompt`——这是实际喂给被测 LLM 的值（含人格 + 前缀 +
   skills + 系统提醒），若捕获成功即为真值。但 `call_event_hook` 会把同一 `req` 顺序传给
   全部已注册插件的同型 hook：其他 star 的 handler 可能在我们之前 / 之后改写
   `req.system_prompt`，且我们只快照一次。
2. **回退路径（重建）**：捕获快照 system_prompt 为空 / 步骤结果无 `llm_input` 时，
   `eval/persona.py` 从配置档案解析人格拼出提示词文本。它镜像框架装饰的**入参**
   （同一数据源），但不等于装饰**结果**。

## 为什么会有偏差

- 回退值 = 「配置档案里人格的 prompt」重建；真实值 = 「装饰 + 其他插件 / hook 可能改写后」
  的 `req.system_prompt`。若没有任何插件改写、运行时配置与评审时读到的一致，两者一致；
  只要出现插件改写 / 配置漂移，评审材料与被测 LLM 实际收到的就不同。
- 2026-08-07 实际观测：3120 字符 prompt 型人格（非 begin_dialogs）上回退日志
  （`[testbench] 人格回退解析命中`）仍触发，说明捕获快照为空 / 缺失——评审材料实际走的
  是回退重建，不是捕获真值。

## 影响

- 评审 LLM 看到的人格设定可能与被测 LLM 实际收到的有偏差（存在插件改写 / 配置漂移时）。
- 对 begin_dialogs 型人格，捕获与回退同为从配置解析（框架本就不把它写进 system_prompt），
  一致；对 prompt 型人格，无人改写时回退值与实际一致（同一数据源）。

## 暂不修复的原因与可能方向（未实施）

- 用户拍板暂时按下不动。
- 可能方向：① 先定位捕获链路为何在本环境不触发（on_llm hook / entry_id 守卫 /
  快照传递），捕获通了即优先用真值；② 快照扩展为同时记录 `req.contexts` 前段
  （begin_dialogs 注入部分）；③ 给回退日志加来源区分（capture / review），便于核对
  评审材料到底走了哪条路径。
