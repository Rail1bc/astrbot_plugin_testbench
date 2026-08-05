"""并发测试运行器。

把一条消息投递到多个虚拟会话，完全交由 AstrBot 原生 pipeline 处理：与真实
平台一致，不设总超时、不分批投递，事件入队后后台逐个等待会话完成并记录，
前端轮询 ``status()`` 即可实现「每个会话窗口独立刷新」。回复由事件自身的
send()/send_streaming() 捕获。
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from .conf_routes import restore_routes, save_and_apply_routes
from .group_store import (
    DEFAULT_PLATFORM_ID,
    DEFAULT_SENDER_ID,
    DEFAULT_SENDER_NAME,
)
from .stats import duration_stats
from .virtual_event import VirtualMessageEvent

if TYPE_CHECKING:
    from astrbot.api.star import Context


class VirtualTestRunner:
    """把一条消息投递到多个虚拟会话，逐个流式汇总结果。

    不带 conf_id 的测试完全并行；带 conf_id 的测试之间通过路由锁串行，避免
    临时 UCR 路由互相污染。
    """

    def __init__(self, context: Context) -> None:
        self.context = context
        self._route_lock = asyncio.Lock()
        self._saved_routes: list[tuple[str, str | None]] = []
        self._runs: dict[str, dict] = {}
        self._run_seq = 0

    async def _apply_conf_route(self, sessions: list[dict], conf_id: str) -> None:
        """为每个会话设置精确的 UCR 路由（umo → conf_id），并保存原路由以便恢复。

        使用会话级精确路由（而非平台级 `platform_id::`），避免影响同平台其他会话，
        且不覆盖会话创建时绑定的持久配置。
        """
        self._saved_routes = await save_and_apply_routes(
            self.context.astrbot_config_mgr.ucr, sessions, conf_id
        )

    async def _restore_conf_route(self) -> None:
        if not self._saved_routes:
            return
        await restore_routes(self.context.astrbot_config_mgr.ucr, self._saved_routes)
        self._saved_routes = []

    async def start(
        self,
        sessions: list[dict],
        text: str,
        provider_id: str | None = None,
        model: str | None = None,
        conf_id: str | None = None,
    ) -> str:
        """投递消息并立即返回 test_id（不等待回复）。

        Args:
            sessions: 已解析最终配置的虚拟会话数据列表。
            text: 要发送的消息文本（所有会话相同）。
            provider_id: 可选，覆盖 LLM provider。
            model: 可选，覆盖模型名。
            conf_id: 可选，临时把会话的配置档案路由到指定档案（测试提示词/系统设定）。

        Returns:
            test_id，用于轮询 status()。
        """
        if not sessions:
            raise ValueError("sessions 不能为空")
        if not text or not text.strip():
            raise ValueError("text 不能为空")

        if conf_id:
            await self._route_lock.acquire()
            try:
                await self._apply_conf_route(sessions, conf_id)
            except Exception:
                self._route_lock.release()
                raise

        events = [
            VirtualMessageEvent.create(
                session_id=s["id"],
                sender_id=s.get("sender_id") or DEFAULT_SENDER_ID,
                sender_name=s.get("sender_name") or DEFAULT_SENDER_NAME,
                text=text,
                platform_id=s.get("platform_id") or DEFAULT_PLATFORM_ID,
                platform_name=s.get("platform_id") or DEFAULT_PLATFORM_ID,
                provider_id=provider_id,
                model=model,
            )
            for s in sessions
        ]

        self._run_seq += 1
        test_id = f"t_{int(time.time() * 1000)}_{self._run_seq}"
        record = {
            "id": test_id,
            "total": len(events),
            "results": {},  # session_id -> 结果摘要
            "created_at": time.time(),
            "finished_at": None,
            "done": False,
            "all_done": asyncio.Event(),
        }
        self._runs[test_id] = record
        self._prune_runs()

        queue = self.context.get_event_queue()
        for event in events:
            queue.put_nowait(event)
        for event in events:
            asyncio.create_task(self._await_event(test_id, event))
        if conf_id:
            asyncio.create_task(self._release_route_after(test_id))
        return test_id

    async def _await_event(self, test_id: str, event: VirtualMessageEvent) -> None:
        await event.pipeline_done_event.wait()
        record = self._runs.get(test_id)
        if record is None:
            return
        record["results"][event.session_id] = event.result_summary()
        if len(record["results"]) >= record["total"] and not record["done"]:
            record["done"] = True
            record["finished_at"] = time.time()
            record["all_done"].set()

    async def _release_route_after(self, test_id: str) -> None:
        record = self._runs.get(test_id)
        if record is None:
            return
        await record["all_done"].wait()
        await self._restore_conf_route()
        self._route_lock.release()

    def status(self, test_id: str) -> dict | None:
        """查询运行状态（含已完成会话的结果与统计）。"""
        record = self._runs.get(test_id)
        if record is None:
            return None
        results = list(record["results"].values())
        return {
            "test_id": test_id,
            "total": record["total"],
            "done": record["done"],
            "results": results,
            "stats": duration_stats([r["duration"] for r in results]),
        }

    def _prune_runs(self) -> None:
        """清理已完成超过 10 分钟的运行记录，避免内存累积。"""
        now = time.time()
        expired = [
            tid
            for tid, r in self._runs.items()
            if r["done"] and (now - (r["finished_at"] or now)) > 600
        ]
        for tid in expired:
            self._runs.pop(tid, None)
