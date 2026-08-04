"""虚拟会话管理与并发测试运行器。

核心思路：虚拟会话 = 一个专属的 platform_id + session_id。把 `VirtualMessageEvent`
直接投递到 AstrBot 的事件队列（`context.get_event_queue()`），事件总线会像处理
真实平台消息一样，按 umo 解析配置档案并交给 pipeline 调度器执行，因此虚拟会话与
真实会话共享完全相同的处理路径。回复由事件自身的 send()/send_streaming() 捕获。
"""

from __future__ import annotations

import asyncio
import json
import math
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

from .virtual_event import VirtualMessageEvent

if TYPE_CHECKING:
    from astrbot.api.star import Context

DEFAULT_PLATFORM_ID = "virtual_test"
DEFAULT_SENDER_ID = "virtual_user"
DEFAULT_SENDER_NAME = "虚拟用户"
MESSAGE_TYPE = "FriendMessage"


def umo_of(session: dict) -> str:
    """根据虚拟会话数据计算 unified_msg_origin。"""
    platform_id = session.get("platform_id") or DEFAULT_PLATFORM_ID
    return f"{platform_id}:{MESSAGE_TYPE}:{session['id']}"


class VirtualSessionManager:
    """虚拟会话的创建与持久化。

    会话数据保存到 data 目录下的 `virtual_session/sessions.json`，符合
    "插件持久化数据存 data 目录" 的规范，插件更新/重装不会丢失会话。
    """

    def __init__(self, data_dir: Path | None = None) -> None:
        base = Path(get_astrbot_plugin_data_path()) if data_dir is None else data_dir
        self._dir = base / "virtual_session"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._file = self._dir / "sessions.json"
        self._sessions: list[dict] = self._load()

    def _load(self) -> list[dict]:
        if not self._file.exists():
            return []
        try:
            data = json.loads(self._file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return data if isinstance(data, list) else []

    def _save(self) -> None:
        self._file.write_text(
            json.dumps(self._sessions, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def list(self) -> list[dict]:
        """返回全部虚拟会话。"""
        return list(self._sessions)

    def get(self, session_id: str) -> dict | None:
        """按 id 查找会话。"""
        for session in self._sessions:
            if session["id"] == session_id:
                return session
        return None

    def get_many(self, ids: list[str]) -> list[dict]:
        """按 id 批量查找（保持传入顺序，缺失的跳过）。"""
        wanted = set(ids)
        return [s for s in self._sessions if s["id"] in wanted]

    def create_many(
        self,
        count: int,
        platform_id: str | None = None,
        sender_id: str | None = None,
        sender_name: str | None = None,
        name_prefix: str | None = None,
    ) -> list[dict]:
        """批量创建虚拟会话。

        Args:
            count: 创建数量。
            platform_id: 会话所属平台 id（决定 umo 与配置档案路由）。
            sender_id: 消息发送者 id。
            sender_name: 消息发送者昵称。
            name_prefix: 会话展示名前缀。
        """
        created: list[dict] = []
        for _ in range(count):
            sid = f"vs_{uuid.uuid4().hex[:8]}"
            created.append(
                {
                    "id": sid,
                    "name": f"{name_prefix or '虚拟会话'}{len(self._sessions) + len(created) + 1}",
                    "platform_id": platform_id or DEFAULT_PLATFORM_ID,
                    "sender_id": sender_id or DEFAULT_SENDER_ID,
                    "sender_name": sender_name or DEFAULT_SENDER_NAME,
                    "created_at": int(time.time()),
                }
            )
        self._sessions.extend(created)
        self._save()
        return created

    def delete(self, ids: list[str]) -> int:
        """按 id 删除会话，返回删除数量。"""
        id_set = set(ids)
        before = len(self._sessions)
        self._sessions = [s for s in self._sessions if s["id"] not in id_set]
        self._save()
        return before - len(self._sessions)


def _percentile(sorted_values: list[float], p: float) -> float:
    """线性插值分位数（sorted_values 已升序）。"""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = (len(sorted_values) - 1) * p
    low = int(math.floor(pos))
    high = int(math.ceil(pos))
    if low == high:
        return sorted_values[low]
    frac = pos - low
    return sorted_values[low] + (sorted_values[high] - sorted_values[low]) * frac


def duration_stats(durations: list[float]) -> dict:
    """计算耗时统计（min/max/avg/p50/p95）。"""
    if not durations:
        return {"min": 0.0, "max": 0.0, "avg": 0.0, "p50": 0.0, "p95": 0.0}
    sorted_d = sorted(durations)
    return {
        "min": round(min(durations), 3),
        "max": round(max(durations), 3),
        "avg": round(sum(durations) / len(durations), 3),
        "p50": round(_percentile(sorted_d, 0.5), 3),
        "p95": round(_percentile(sorted_d, 0.95), 3),
    }


class VirtualTestRunner:
    """把一条消息并发发送到多个虚拟会话并汇总结果。"""

    def __init__(self, context: Context) -> None:
        self.context = context
        self._lock = asyncio.Lock()
        self._saved_route: tuple[str, str | None] | None = None

    async def _apply_conf_route(self, platform_id: str, conf_id: str) -> None:
        """把 `platform_id::` 路由到指定配置档案，并保存原路由以便恢复。"""
        ucr = self.context.astrbot_config_mgr.ucr
        umop = f"{platform_id}::"
        self._saved_route = (umop, ucr.umop_to_conf_id.get(umop))
        await ucr.update_route(umop, conf_id)

    async def _restore_conf_route(self) -> None:
        if not self._saved_route:
            return
        umop, prev_conf_id = self._saved_route
        ucr = self.context.astrbot_config_mgr.ucr
        if prev_conf_id is None:
            if umop in ucr.umop_to_conf_id:
                await ucr.delete_route(umop)
        else:
            await ucr.update_route(umop, prev_conf_id)
        self._saved_route = None

    async def run(
        self,
        sessions: list[dict],
        text: str,
        provider_id: str | None = None,
        model: str | None = None,
        conf_id: str | None = None,
        timeout: float = 120.0,
        batch_size: int = 10,
        batch_interval: float = 0.0,
    ) -> dict:
        """并发测试。

        Args:
            sessions: 虚拟会话数据列表。
            text: 要发送的消息文本（所有会话相同）。
            provider_id: 可选，覆盖 LLM provider。
            model: 可选，覆盖模型名。
            conf_id: 可选，把会话的配置档案路由到指定档案（测试提示词/系统设定）。
            timeout: 整体等待超时（秒）。
            batch_size: 每批入队的会话数。
            batch_interval: 批次间隔（秒）。

        Returns:
            包含 total/ok/no_reply/timeout/error/stats/results 的字典。
        """
        if not sessions:
            raise ValueError("sessions 不能为空")
        if not text or not text.strip():
            raise ValueError("text 不能为空")

        # 同一时刻只允许一个测试运行（避免并发修改配置档案路由）
        async with self._lock:
            if conf_id:
                platform_ids = {s.get("platform_id") for s in sessions}
                if len(platform_ids) != 1:
                    raise ValueError("指定配置档案时，所有会话的 platform_id 必须一致")
                await self._apply_conf_route(next(iter(platform_ids)), conf_id)
            try:
                return await self._dispatch(
                    sessions,
                    text,
                    provider_id,
                    model,
                    timeout,
                    batch_size,
                    batch_interval,
                )
            finally:
                if conf_id:
                    await self._restore_conf_route()

    async def _dispatch(
        self,
        sessions: list[dict],
        text: str,
        provider_id: str | None,
        model: str | None,
        timeout: float,
        batch_size: int,
        batch_interval: float,
    ) -> dict:
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

        queue = self.context.get_event_queue()
        for i in range(0, len(events), max(1, batch_size)):
            batch = events[i : i + batch_size]
            for event in batch:
                queue.put_nowait(event)
            if batch_interval and i + batch_size < len(events):
                await asyncio.sleep(batch_interval)

        try:
            await asyncio.wait_for(
                asyncio.gather(*(e.pipeline_done_event.wait() for e in events)),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            pass

        results = []
        for event in events:
            if event.pipeline_done_event.is_set():
                results.append(event.result_summary())
            else:
                results.append(
                    event.result_summary(status="timeout", error="等待回复超时")
                )

        return {
            "total": len(results),
            "ok": sum(1 for r in results if r["status"] == "ok"),
            "no_reply": sum(1 for r in results if r["status"] == "no_reply"),
            "timeout": sum(1 for r in results if r["status"] == "timeout"),
            "error": sum(1 for r in results if r["status"] == "error"),
            "stats": duration_stats([r["duration"] for r in results]),
            "results": results,
        }
