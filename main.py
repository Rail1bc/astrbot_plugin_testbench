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
    MetaAPI,
    RunsAPI,
    SessionsAPI,
    TestsetsAPI,
)
from .core.event_bus import EventBus
from .core.runner import VirtualTestRunner
from .core.testset_runner import TestsetRunner
from .core.virtual_event import VirtualMessageEvent
from .history_ops import HistoryOps
from .store.group_store import VirtualGroupManager
from .store.testset_store import TestsetStore

PLUGIN_NAME = "astrbot_plugin_testbench"


class VirtualSessionPlugin(
    Star,
    MetaAPI,
    GroupsAPI,
    SessionsAPI,
    RunsAPI,
    TestsetsAPI,
    EventsAPI,
    ConfRouteMixin,
):
    def __init__(self, context: Context) -> None:
        super().__init__(context)
        self.group_mgr = VirtualGroupManager()
        self.event_bus = EventBus()
        self.runner = VirtualTestRunner(context, self.event_bus)
        self.testset_store = TestsetStore()
        self.testset_runner = TestsetRunner(context, self.runner, self.event_bus)
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
