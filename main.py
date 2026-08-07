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
            event.set_extra(TESTBENCH_LLM_INPUT_EXTRA_KEY, _snapshot_llm_input(req))
