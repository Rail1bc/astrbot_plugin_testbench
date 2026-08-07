"""被测 agent 人格快照辅助（评审材料用）。

供两处入口复用同一份实现，避免双份维护：

- ``main.py`` 的 ``on_llm`` hook（捕获时回退）：快照 ``req.system_prompt``
  为空时，从会话配置档案解析被测 agent 的人格设定补进快照。
- ``eval/assessor.py``（评审时回退）：评审阶段结果无快照 / 快照系统提示词
  为空时，同样从配置档案解析补进评审材料——即使捕获 hook 因任何原因未
  触发，评审输入仍能带上被测 agent 的人格设定（不依赖捕获链路）。

日志统一走根 ``astrbot`` logger（``[testbench]`` 前缀）：插件的专用 logger
（``Star.logger``）支持按插件调级，INFO 日志可能被静默过滤，排查「未捕获」
时根 logger 更可靠（与 AstrBot 自身模块如 persona_mgr.py 的记法一致）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from astrbot import logger

if TYPE_CHECKING:
    from astrbot.api.star import Context


def format_persona_snapshot(persona: dict) -> str:
    """把解析出的人格转成评审材料用文本（提示词 + 开场对话）。

    begin_dialogs 型人格的身份文本在 `_begin_dialogs_processed`
    （role/content 列表），与 `prompt`（DB system_prompt 字段）一起组成
    被测 agent 的人格设定；两者都可能是空的。
    """
    blocks: list[str] = []
    prompt = persona.get("prompt")
    if prompt:
        blocks.append(f"# Persona Instructions\n\n{prompt}\n")
    dialogs = persona.get("_begin_dialogs_processed") or []
    if dialogs:
        lines = [f"{d.get('role', 'user')}: {d.get('content', '')}" for d in dialogs]
        blocks.append("# 开场对话（begin_dialogs）\n\n" + "\n".join(lines))
    return "\n".join(blocks)


async def conversation_persona_id(context: Context, umo: str) -> str | None:
    """取会话级人格 id（评审回退解析用，防御式；无会话级人格 → None）。

    捕获 hook（on_llm）直接读 `req.conversation.persona_id`；评审阶段没有
    req，从对话存储回查选中对话的 persona_id，使回退解析尽可能镜像框架
    装饰路径的入参。conversation_manager 缺失 / 存储异常 → None（回退到
    档案 default_personality）。
    """
    cm = getattr(context, "conversation_manager", None)
    if cm is None:
        return None
    try:
        conv_id = await cm.get_curr_conversation_id(umo)
        if not conv_id:
            return None
        conv = await cm.get_conversation(umo, conv_id)
        return getattr(conv, "persona_id", None) if conv else None
    except Exception:
        return None


async def resolve_agent_system_prompt(
    context: Context,
    *,
    umo: str,
    conv_persona_id: str | None,
    platform_name: str,
) -> str:
    """req.system_prompt 为空时回退解析被测 agent 的人格设定（捕获 hook 与评审共用）。

    astrbot 的人格装饰（`_ensure_persona_and_skills`）：人格的 `prompt`
    字段写进 req.system_prompt，而**开场对话（begin_dialogs）型人格**把
    身份文本注入 req.contexts 对话历史、不碰 system_prompt——这类会话的
    快照系统提示词恒为空，评审材料看不到人格设定。这里从会话配置档案
    解析人格，把提示词与开场对话补进快照，使评审 LLM 仍能看到被测 agent
    的人格设定。解析失败 / 无人格 → 空串（评审层显示未捕获占位）。

    关键决策点打 INFO 日志（`[testbench]` 前缀），供排查「未捕获」：
    是否执行回退、配置档案的 default_personality / 会话级 persona、
    命中的人格及其内容规模。
    """
    pm = getattr(context, "persona_manager", None)
    if pm is None or not hasattr(pm, "resolve_selected_persona"):
        logger.info(
            "[testbench] 人格回退解析跳过：context 无 persona_manager",
        )
        return ""
    try:
        provider_settings: dict = {}
        conf_mgr = getattr(context, "astrbot_config_mgr", None)
        if conf_mgr is not None and hasattr(conf_mgr, "get_conf"):
            cfg = conf_mgr.get_conf(umo)
            if cfg:
                provider_settings = cfg.get("provider_settings", {}) or {}
        _, persona, force_id, _ = await pm.resolve_selected_persona(
            umo=umo,
            conversation_persona_id=conv_persona_id,
            platform_name=platform_name,
            provider_settings=provider_settings,
        )
        if not persona:
            logger.info(
                "[testbench] 人格回退解析未命中人格：umo=%s "
                "default_personality=%r 会话级 persona=%r force=%r",
                umo,
                provider_settings.get("default_personality"),
                conv_persona_id,
                force_id,
            )
            return ""
        persona_id = persona.get("name") or "?"
        prompt = persona.get("prompt") or ""
        dialogs = persona.get("_begin_dialogs_processed") or []
        text = format_persona_snapshot(persona)
        logger.info(
            "[testbench] 人格回退解析命中：umo=%s persona=%r "
            "prompt=%d 字符 开场对话=%d 条 快照补入 %d 字符",
            umo,
            persona_id,
            len(prompt),
            len(dialogs),
            len(text),
        )
        return text
    except Exception:
        logger.exception(
            "[testbench] 解析被测 agent 人格设定失败，评审材料回退未捕获占位",
        )
        return ""
