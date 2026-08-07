"""会话测试台插件。

通过插件页面创建"测试组"——一组共享同一套配置（平台来源/配置档案/发送者
id/昵称）的虚拟会话，组内单个会话可覆盖组配置。测试以组为单位：可并发向组内
（或跨组选中的）多个虚拟会话发送同一条消息，用于测试插件、提示词、模型与
整体稳定性。

消息注入路径：`context.get_event_queue()` -> EventBus -> PipelineScheduler，
与真实平台消息完全一致，回复由 `VirtualMessageEvent` 捕获并回传页面。

Web API handler 按资源拆分到 api/ 包（mixin 类，本模块的 Star 继承装配），
运行编排在 core/、持久化在 store/、断言评估在 eval/；本模块只保留 Star
入口：依赖装配、路由注册与两个 LLM 阶段 hook。
"""

from __future__ import annotations

from astrbot.api.event import AstrMessageEvent
from astrbot.api.event.filter import on_llm_request, on_waiting_llm_request
from astrbot.api.star import Context, Star

from .api import (
    _ROUTES,
    ConfRouteMixin,
    EventsAPI,
    GroupsAPI,
    IdentitiesAPI,
    MetaAPI,
    ReportsAPI,
    ReviewersAPI,
    RunsAPI,
    SessionsAPI,
    TestsetsAPI,
)
from .core.event_bus import EventBus
from .core.runner import LATE_SEND_DETECT_WINDOW, VirtualTestRunner
from .core.testset_runner import TestsetRunner
from .core.virtual_event import (
    TESTBENCH_LLM_INPUT_EXTRA_KEY,
    TESTBENCH_LLM_REQUESTED_EXTRA_KEY,
    VirtualMessageEvent,
)
from .history_ops import HistoryOps
from .store.group_store import VirtualGroupManager
from .store.identity_store import ChatGroupStore, IdentityStore
from .store.report_store import ReportStore
from .store.reviewer_store import ReviewerStore
from .store.stream_store import StreamStore
from .store.testset_store import TestsetStore

PLUGIN_NAME = "astrbot_plugin_testbench"


def _snapshot_llm_input(req) -> dict:
    """快照实际喂给被测 LLM 的输入（装饰后）。

    框架 / 其他插件会在调用前改写 `req.prompt`（如 prompt 前缀注入）与
    `req.extra_user_content_parts`（如 `<system_reminder>`、知识库结果），
    评审材料应基于这份实际输入而非测试集原始文本。

    必须渲染成纯字符串（TextPart / ThinkPart 抽取文本），不能存 ContentPart
    引用——快照会随 SSE 事件与报告 JSON 序列化，存对象会破坏序列化。
    """
    parts = []
    for part in getattr(req, "extra_user_content_parts", None) or []:
        text = getattr(part, "text", None)
        if text is None:
            text = getattr(part, "think", None)  # ThinkPart
        if text:
            parts.append(str(text))
    return {
        "prompt": getattr(req, "prompt", None) or "",
        "extra_parts": parts,
        "system_prompt": getattr(req, "system_prompt", None) or "",
    }


def _format_persona_snapshot(persona) -> str:
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


class VirtualSessionPlugin(
    Star,
    MetaAPI,
    GroupsAPI,
    SessionsAPI,
    RunsAPI,
    TestsetsAPI,
    IdentitiesAPI,
    EventsAPI,
    ReportsAPI,
    ReviewersAPI,
    ConfRouteMixin,
):
    def __init__(self, context: Context) -> None:
        super().__init__(context)
        self.group_mgr = VirtualGroupManager()
        self.identity_store = IdentityStore()
        self.chat_group_store = ChatGroupStore()
        self.stream_store = StreamStore()
        self.event_bus = EventBus()
        self.runner = VirtualTestRunner(
            context,
            self.event_bus,
            stream_store=self.stream_store,
            identity_store=self.identity_store,
            chat_group_store=self.chat_group_store,
            late_send_detect_window=LATE_SEND_DETECT_WINDOW,
        )
        self.testset_store = TestsetStore()
        self.reviewer_store = ReviewerStore()
        self.report_store = ReportStore()
        self.testset_runner = TestsetRunner(
            context,
            self.runner,
            self.event_bus,
            reviewer_store=self.reviewer_store,
            report_store=self.report_store,
        )
        self.history_ops = HistoryOps(
            context, lambda: self.group_mgr, self.runner, self.logger
        )
        for path, handler, methods, desc in _ROUTES:
            context.register_web_api(
                f"/{PLUGIN_NAME}{path}", getattr(self, handler), methods, desc
            )

    # ---------- 在途消息状态（LLM 阶段 hook，须挂在 Star 上由 AstrBot 扫描） ----------

    @on_waiting_llm_request()
    async def on_waiting_llm(self, event: AstrMessageEvent) -> None:
        """消息已到达 LLM 阶段、正在等待会话锁（「重复追问」排队等待时触发）。"""
        if isinstance(event, VirtualMessageEvent) and event.entry_id:
            self.runner.mark_waiting_llm(event.entry_id)

    @on_llm_request()
    async def on_llm(self, event: AstrMessageEvent, req) -> None:
        """消息正在调用 LLM（会话锁内、流式/非流式分叉之前触发）。"""
        if isinstance(event, VirtualMessageEvent) and event.entry_id:
            self.runner.mark_llm(event.entry_id)
            event.set_extra(TESTBENCH_LLM_REQUESTED_EXTRA_KEY, True)
            snapshot = _snapshot_llm_input(req)
            if not snapshot["system_prompt"]:
                # begin_dialogs 型人格的身份不写进 req.system_prompt，从配置档案回退解析
                snapshot["system_prompt"] = await self._resolve_persona_system_prompt(
                    event, req
                )
            event.set_extra(TESTBENCH_LLM_INPUT_EXTRA_KEY, snapshot)

    async def _resolve_persona_system_prompt(self, event, req) -> str:
        """req.system_prompt 为空时回退解析被测 agent 的人格设定。

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
        umo = event.unified_msg_origin
        pm = getattr(self.context, "persona_manager", None)
        if pm is None or not hasattr(pm, "resolve_selected_persona"):
            self.logger.info(
                "[testbench] 人格回退解析跳过：context 无 persona_manager",
            )
            return ""
        try:
            provider_settings: dict = {}
            conf_mgr = getattr(self.context, "astrbot_config_mgr", None)
            if conf_mgr is not None and hasattr(conf_mgr, "get_conf"):
                cfg = conf_mgr.get_conf(umo)
                if cfg:
                    provider_settings = cfg.get("provider_settings", {}) or {}
            conversation = getattr(req, "conversation", None)
            conv_persona_id = getattr(conversation, "persona_id", None)
            _, persona, force_id, _ = await pm.resolve_selected_persona(
                umo=umo,
                conversation_persona_id=conv_persona_id,
                platform_name=event.get_platform_name(),
                provider_settings=provider_settings,
            )
            if not persona:
                self.logger.info(
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
            text = _format_persona_snapshot(persona)
            self.logger.info(
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
            self.logger.exception(
                "[testbench] 解析被测 agent 人格设定失败，评审材料回退未捕获占位",
            )
            return ""
