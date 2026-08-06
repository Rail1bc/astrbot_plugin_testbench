"""群消息流持久化：与 LLM 对话历史并行的纯记录。

消息流记录「真实会话中发生了什么」：user 消息（含发送者身份、是否 @机器人）
与 bot 回复按时间序追加，与 LLM 历史（OpenAI 格式上下文）天然并行、不注入
LLM 上下文。面板可在「LLM 历史 ↔ 消息流」间切换查看。

写入经**实例级 asyncio.Lock 串行**（重叠发送竞态）；user 消息在入队前写入、
bot 回复在 pipeline 结束后写入；``reply_status``（ok / no_reply / error）只标
记在 user 消息上。超出 ``MAX_STREAM_MESSAGES`` 截断最旧。重置会话 → 清流；
删除会话 → 删流。
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path

from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

# 单会话消息流条数上限（超出截断最旧，与测试集/组容量同风格的安全阀）
MAX_STREAM_MESSAGES = 500


class StreamStore:
    """群消息流的追加、读取与清理。

    数据保存到 `virtual_session/streams.json`（与 groups.json 同目录），结构为
    ``{"streams": {session_id: {"session_id", "messages": [...]}}}``，全量写 JSON。
    """

    def __init__(self, data_dir: Path | None = None) -> None:
        base = Path(get_astrbot_plugin_data_path()) if data_dir is None else data_dir
        directory = base / "virtual_session"
        directory.mkdir(parents=True, exist_ok=True)
        self._file = directory / "streams.json"
        self._lock = asyncio.Lock()
        self._streams: dict[str, dict] = self._load()

    def _load(self) -> dict[str, dict]:
        if not self._file.exists():
            return {}
        try:
            data = json.loads(self._file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict) or not isinstance(data.get("streams"), dict):
            return {}
        return {
            sid: stream
            for sid, stream in data["streams"].items()
            if isinstance(stream, dict) and isinstance(stream.get("messages"), list)
        }

    def _save(self) -> None:
        self._file.write_text(
            json.dumps({"streams": self._streams}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    async def append(self, session_id: str, entry: dict) -> str:
        """向会话消息流追加一条消息，返回生成的消息 id。

        ``entry`` 含 role / sender_id / sender_name / text 等业务字段（可选
        ``at_bot``、``reply_status``），id 与时间戳由本方法生成。
        """
        async with self._lock:
            stream = self._streams.get(session_id)
            if stream is None:
                stream = {"session_id": session_id, "messages": []}
                self._streams[session_id] = stream
            message = {"id": f"m_{uuid.uuid4().hex[:8]}", "ts": int(time.time())}
            message.update(entry)
            stream["messages"].append(message)
            if len(stream["messages"]) > MAX_STREAM_MESSAGES:
                stream["messages"] = stream["messages"][-MAX_STREAM_MESSAGES:]
            self._save()
            return message["id"]

    async def read_stream(self, session_id: str) -> list[dict]:
        """返回会话的消息流拷贝；无记录时返回空列表。"""
        stream = self._streams.get(session_id)
        return [dict(message) for message in stream["messages"]] if stream else []

    async def update_reply(self, session_id: str, message_id: str, status: str) -> None:
        """更新 user 消息的 reply_status（ok / no_reply / error）。"""
        async with self._lock:
            stream = self._streams.get(session_id)
            if stream is None:
                return
            for message in stream["messages"]:
                if message["id"] == message_id:
                    message["reply_status"] = status
                    self._save()
                    return

    async def clear(self, session_id: str) -> None:
        """清空会话的消息流（重置会话时联动）。"""
        async with self._lock:
            if session_id in self._streams:
                del self._streams[session_id]
                self._save()

    async def delete_sessions(self, ids: list[str]) -> None:
        """删除多个会话的消息流（删除会话时联动）。"""
        async with self._lock:
            id_set = set(ids)
            if any(sid in self._streams for sid in id_set):
                self._streams = {
                    sid: stream
                    for sid, stream in self._streams.items()
                    if sid not in id_set
                }
                self._save()
