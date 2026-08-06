"""SSE 事件流接口。"""

from __future__ import annotations

import asyncio
import json

from astrbot.api.web import StreamingResponse


class EventsAPI:
    """/events SSE 事件流（挂 self.event_bus，订阅者拿到独立有界队列）。"""

    async def events(self):
        """SSE 事件流：在途条目 / 会话完成 / 测试完成 / 测试集进度实时推送。

        订阅者拿到独立有界队列（EventBus），断开时（页面关闭/重连）generator
        在 finally 退订；15s 无事件发心跳注释行防代理断连。事件均为全量快照，
        前端断线重连后经一次性接口取回当前状态对账，丢失的旧快照无影响。
        """
        queue = self.event_bus.subscribe()

        async def gen():
            try:
                while True:
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=15)
                    except TimeoutError:
                        yield ": ping\n\n"
                        continue
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            finally:
                self.event_bus.unsubscribe(queue)

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
