"""进程内事件广播（供 `/events` SSE 端点实时推送运行状态）。

测试运行 / 在途条目 / 测试集进度在状态变化点发布事件，订阅者（SSE 连接的
每个消费者）拿到独立的有界队列。队列满时丢弃最旧事件——慢消费者（页面切到
后台）丢事件不影响发布者，且事件均为全量快照（pending 快照 / 完整 run dict），
丢的旧快照会被更新的快照覆盖，最终状态不丢失。
"""

from __future__ import annotations

import asyncio


class EventBus:
    """进程内事件广播：发布者不阻塞，慢消费者丢最旧事件。"""

    def __init__(self, maxlen: int = 1000) -> None:
        self._consumers: list[asyncio.Queue] = []
        self._maxlen = maxlen

    def subscribe(self) -> asyncio.Queue:
        """注册一个消费者，返回其专属事件队列。"""
        queue: asyncio.Queue = asyncio.Queue(maxsize=self._maxlen)
        self._consumers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        if queue in self._consumers:
            self._consumers.remove(queue)

    def publish(self, event: dict) -> None:
        """向全部消费者广播事件（队列满则丢最旧）。"""
        for queue in list(self._consumers):
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(event)
