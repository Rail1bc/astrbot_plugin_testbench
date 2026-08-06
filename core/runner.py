"""并发测试运行器。

把一条消息投递到多个虚拟会话，完全交由 AstrBot 原生 pipeline 处理：与真实
平台一致，不设总超时、不分批投递，事件入队后后台逐个等待会话完成并记录，
逐会话结果经事件总线广播（/events SSE 事件流）实时推送前端，``status()``
供断线对账一次性取回。回复由事件自身的 send()/send_streaming() 捕获。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from ..eval.mechanical import evaluate_rule
from ..stats import duration_stats
from ..store.group_store import (
    DEFAULT_PLATFORM_ID,
    DEFAULT_SENDER_ID,
    DEFAULT_SENDER_NAME,
)
from .conf_routes import restore_routes, save_and_apply_routes
from .event_bus import EventBus
from .virtual_event import VirtualMessageEvent

if TYPE_CHECKING:
    from astrbot.api.star import Context

# 悬挂运行的安全阀（秒）：未完成的运行记录超过此时间即被清理，临时路由锁
# 也在等待超过此时间后强制恢复释放。仅用于防止悬挂 pipeline 永久占用内存与
# 路由锁，不改变「不设总超时」的测试语义——正常测试远小于此窗口。
STALE_RUN_TIMEOUT = 3600

# 已完成条目在「在途」列表中的展示保留时长（秒）：前端把状态从「LLM 生成中」
# 切换到「完成」后仍能看到结果落定，随后条目自然被清理。
DONE_KEEP_SECONDS = 30

logger = logging.getLogger(__name__)


class VirtualTestRunner:
    """把一条消息投递到多个虚拟会话，逐个流式汇总结果。

    不带 conf_id 的测试完全并行；带 conf_id 的测试之间通过路由锁串行，避免
    临时 UCR 路由互相污染。每条消息登记一个在途条目（submitted → waiting_llm
    → llm → done），由 LLM 阶段 hook 推进状态，供前端面板实时展示。
    """

    def __init__(self, context: Context, event_bus: EventBus | None = None) -> None:
        self.context = context
        # 未注入时自建空总线：publish 到无订阅者的总线是 no-op，测试可省去该参数
        self.event_bus = event_bus or EventBus()
        self._route_lock = asyncio.Lock()
        self._saved_routes: list[tuple[str, str | None]] = []
        self._runs: dict[str, dict] = {}
        self._pending: dict[str, dict] = {}  # entry_id -> 在途条目
        self._run_seq = 0

    def _publish_pending(self) -> None:
        """广播在途条目全量快照（幂等：新快照覆盖旧快照，丢旧无碍）。

        条目是原地可变的 dict（status 随阶段推进），快照须拷贝，否则已发布事件
        里的条目会随时间漂移成最新状态，不再是发布时刻的状态。
        """
        self.event_bus.publish(
            {"type": "pending", "entries": [dict(e) for e in self._pending.values()]}
        )

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
        assertion: dict | None = None,
    ) -> str:
        """投递消息并立即返回 test_id（不等待回复）。

        Args:
            sessions: 已解析最终配置的虚拟会话数据列表。
            text: 要发送的消息文本（所有会话相同）。
            provider_id: 可选，覆盖 LLM provider。
            model: 可选，覆盖模型名。
            conf_id: 可选，临时把会话的配置档案路由到指定档案（测试提示词/系统设定）。
            assertion: 可选，回复断言规则（见 assertions.py），随结果评估返回。

        Returns:
            test_id，用于查询 status()（实时结果经事件流推送）。
        """
        if not sessions:
            raise ValueError("sessions 不能为空")
        if not text or not text.strip():
            raise ValueError("text 不能为空")

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
            "assertion": assertion,
            "created_at": time.time(),
            "finished_at": None,
            "done": False,
            "all_done": asyncio.Event(),
        }
        self._runs[test_id] = record
        self._register_pending(test_id, events, text)
        self._prune_runs()

        if conf_id:
            await self._route_lock.acquire()
            try:
                await self._apply_conf_route(sessions, conf_id)
                self._enqueue(test_id, events)
                asyncio.create_task(self._release_route_after(test_id))
            except BaseException:
                # 入队/建任务途中出错：清理在途条目、恢复已应用的临时路由并释放锁
                self._discard_pending(test_id)
                try:
                    await self._restore_conf_route()
                except Exception:
                    logger.exception("恢复 UCR 路由失败")
                finally:
                    self._route_lock.release()
                raise
        else:
            try:
                self._enqueue(test_id, events)
            except BaseException:
                self._discard_pending(test_id)
                raise
        return test_id

    def _register_pending(
        self, test_id: str, events: list[VirtualMessageEvent], text: str
    ) -> None:
        """为每个事件登记在途条目（供前端实时显示已入队/排队等待 LLM/LLM 生成中）。"""
        for i, event in enumerate(events):
            event.entry_id = f"e_{test_id}_{i}"
            self._pending[event.entry_id] = {
                "entry_id": event.entry_id,
                "session_id": event.session_id,
                "test_id": test_id,
                "text": text,
                "status": "submitted",
                "created_at": time.time(),
                "status_at": time.time(),
            }
        self._publish_pending()

    def _discard_pending(self, test_id: str) -> None:
        """入队/建任务失败时清理该测试的在途条目（与路由锁清理一致，防泄漏）。"""
        for eid in [
            eid for eid, entry in self._pending.items() if entry["test_id"] == test_id
        ]:
            self._pending.pop(eid, None)
        self._publish_pending()

    def mark_waiting_llm(self, entry_id: str) -> None:
        """标记消息已到达 LLM 阶段、正在等待会话锁（OnWaitingLLMRequestEvent）。"""
        entry = self._pending.get(entry_id)
        if entry is not None and entry["status"] == "submitted":
            entry["status"] = "waiting_llm"
            entry["status_at"] = time.time()
            self._publish_pending()

    def mark_llm(self, entry_id: str) -> None:
        """标记消息正在调用 LLM（OnLLMRequestEvent，会话锁内）。"""
        entry = self._pending.get(entry_id)
        if entry is not None and entry["status"] in ("submitted", "waiting_llm"):
            entry["status"] = "llm"
            entry["status_at"] = time.time()
            self._publish_pending()

    def pending_entries(self) -> list[dict]:
        """返回全部在途条目（含刚完成、仍处展示保留期的条目），供断线对账取回。"""
        return list(self._pending.values())

    def _enqueue(self, test_id: str, events: list[VirtualMessageEvent]) -> None:
        """把事件投递到事件队列，并为每个事件启动等待任务。"""
        queue = self.context.get_event_queue()
        for event in events:
            queue.put_nowait(event)
        for event in events:
            asyncio.create_task(self._await_event(test_id, event))

    async def _await_event(self, test_id: str, event: VirtualMessageEvent) -> None:
        await event.pipeline_done_event.wait()
        entry = self._pending.get(event.entry_id)
        if entry is not None:
            entry["status"] = "done"
            entry["status_at"] = time.time()
            self._publish_pending()
        record = self._runs.get(test_id)
        if record is None:
            return
        summary = event.result_summary()
        assertion = record.get("assertion")
        if assertion:
            summary["assertion"] = evaluate_rule(assertion, summary.get("reply") or "")
        record["results"][event.session_id] = summary
        self.event_bus.publish(
            {
                "type": "session_done",
                "test_id": test_id,
                "session_id": event.session_id,
                "summary": summary,
            }
        )
        if len(record["results"]) >= record["total"] and not record["done"]:
            record["done"] = True
            record["finished_at"] = time.time()
            record["all_done"].set()
            self.event_bus.publish(
                {
                    "type": "test_done",
                    "test_id": test_id,
                    "record": self.status(test_id),
                }
            )

    async def _release_route_after(self, test_id: str) -> None:
        record = self._runs.get(test_id)
        if record is None:
            return
        try:
            await asyncio.wait_for(record["all_done"].wait(), timeout=STALE_RUN_TIMEOUT)
        except TimeoutError:
            logger.warning(
                f"测试 {test_id} 超过 {STALE_RUN_TIMEOUT}s 仍未完成，强制释放临时路由锁"
            )
        finally:
            try:
                await self._restore_conf_route()
            except Exception:
                logger.exception("恢复 UCR 路由失败")
            finally:
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

    async def wait_done(self, test_id: str, timeout_secs: float | None = None) -> dict:
        """等待运行全部完成（含超时），返回 status() 结果。

        供测试集运行编排器逐步骤等待；超时抛 asyncio.TimeoutError。
        """
        record = self._runs.get(test_id)
        if record is None:
            raise KeyError(f"未找到测试运行: {test_id}")
        if timeout_secs is not None:
            async with asyncio.timeout(timeout_secs):
                await record["all_done"].wait()
        else:
            await record["all_done"].wait()
        return self.status(test_id)

    def _prune_runs(self) -> None:
        """清理过期的运行记录与在途条目。

        运行记录：已完成超过 10 分钟，或未完成超过 1 小时（视为悬挂）。
        在途条目：已完成超过 DONE_KEEP_SECONDS，或未完成超过 1 小时。
        """
        now = time.time()
        expired = [
            tid
            for tid, r in self._runs.items()
            if (r["done"] and (now - (r["finished_at"] or now)) > 600)
            or (not r["done"] and (now - r["created_at"]) > STALE_RUN_TIMEOUT)
        ]
        for tid in expired:
            self._runs.pop(tid, None)
        stale_entries = [
            eid
            for eid, entry in self._pending.items()
            if (
                entry["status"] == "done"
                and (now - entry["status_at"]) > DONE_KEEP_SECONDS
            )
            or (
                entry["status"] != "done"
                and (now - entry["created_at"]) > STALE_RUN_TIMEOUT
            )
        ]
        for eid in stale_entries:
            self._pending.pop(eid, None)
        if stale_entries:
            self._publish_pending()  # 清理后广播快照，防前端残留已过期的「完成」chip
