"""群消息流持久化：与 LLM 对话历史并行的纯记录。

消息流记录「真实会话中发生了什么」：user 消息（含发送者身份、是否 @机器人）
与 bot 回复按时间序追加，与 LLM 历史（OpenAI 格式上下文）天然并行、不注入
LLM 上下文。面板可在「LLM 历史 ↔ 消息流」间切换查看。

写入经**实例级 asyncio.Lock 串行**（重叠发送竞态）；user 消息在入队前写入、
bot 回复在 pipeline 结束后写入；``reply_status``（ok / no_reply / error）只标
记在 user 消息上。超出 ``MAX_STREAM_MESSAGES`` 截断最旧。重置会话 → 清流；
删除会话 → 删流。

**存储格式（streams.jsonl）**：追加式 JSONL——``append``/``update_reply`` 各
追加一行记录，``clear``/``delete_sessions`` 触发全量重写（操作低频）。日志行
仅两类：

- ``{"op": "append", "session_id", "message": {...}}``
- ``{"op": "reply", "session_id", "message_id", "status"}``

重写（``_save``）把内存态序列化为逐条 append 记录（幂等压缩日志），``_load``
按行回放合并为内存态；单条损坏行跳过。日志行数超过 ``_COMPACT_LINES`` 时
``append`` 改为全量重写，防止高频会话的日志无限膨胀（``update_reply`` 的
tombstone 行由重写消除——重写时 reply_status 已并入消息本体）。
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

# 日志行数上限：超过后下一次 append 触发全量重写（压缩日志，防无限膨胀）
_COMPACT_LINES = 10_000


class StreamStore:
    """群消息流的追加、读取与清理。

    数据保存到 `virtual_session/streams.jsonl`（追加式 JSONL，见模块 docstring），
    内存态为 ``{session_id: {"session_id", "messages": [...]}}``。
    """

    def __init__(self, data_dir: Path | None = None) -> None:
        base = Path(get_astrbot_plugin_data_path()) if data_dir is None else data_dir
        directory = base / "virtual_session"
        directory.mkdir(parents=True, exist_ok=True)
        self._file = directory / "streams.jsonl"
        self._lock = asyncio.Lock()
        self._streams: dict[str, dict] = {}
        self._line_count = 0
        self._load()

    # ---------- 持久化 ----------

    def _load(self) -> None:
        """按行读取日志并回放为内存态；损坏行跳过，文件损坏/缺失从空开始。"""
        streams: dict[str, dict] = {}
        line_count = 0
        if not self._file.exists():
            self._streams = streams
            self._line_count = 0
            return
        try:
            with self._file.open(encoding="utf-8") as f:
                for line in f:
                    line_count += 1
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        op = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(op, dict):
                        self._apply_op(streams, op)
        except OSError:
            streams = {}
            line_count = 0
        self._streams = streams
        self._line_count = line_count

    @staticmethod
    def _apply_op(streams: dict[str, dict], op: dict) -> None:
        """把一条日志记录回放到内存态（append / reply）。"""
        kind = op.get("op")
        if kind == "append":
            session_id = op.get("session_id")
            message = op.get("message")
            if not isinstance(session_id, str) or not isinstance(message, dict):
                return
            stream = streams.get(session_id)
            if stream is None:
                stream = {"session_id": session_id, "messages": []}
                streams[session_id] = stream
            stream["messages"].append(message)
            if len(stream["messages"]) > MAX_STREAM_MESSAGES:
                stream["messages"] = stream["messages"][-MAX_STREAM_MESSAGES:]
            return
        if kind == "reply":
            session_id = op.get("session_id")
            message_id = op.get("message_id")
            if not isinstance(session_id, str) or not isinstance(message_id, str):
                return
            stream = streams.get(session_id)
            if stream is None:
                return
            for message in stream["messages"]:
                if message.get("id") == message_id:
                    message["reply_status"] = op.get("status")
                    return

    def _save(self) -> None:
        """全量重写：把内存态序列化为逐条 append 记录（幂等压缩日志）。"""
        body = "\n".join(
            json.dumps(
                {"op": "append", "session_id": sid, "message": message},
                ensure_ascii=False,
            )
            for sid, stream in self._streams.items()
            for message in stream["messages"]
        )
        self._file.write_text(body + ("\n" if body else ""), encoding="utf-8")
        self._line_count = sum(
            len(stream["messages"]) for stream in self._streams.values()
        )

    def _append_line(self, record: dict) -> None:
        """追加一条日志行（调用方须已持有锁）。"""
        with self._file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._line_count += 1

    async def flush(self) -> None:
        """在实例锁内把当前内存态全量落盘（线程内执行，不阻塞事件循环）。"""
        async with self._lock:
            await asyncio.to_thread(self._save)

    # ---------- 读写 ----------

    async def append(self, session_id: str, entry: dict) -> str:
        """向会话消息流追加一条消息，返回生成的消息 id。

        ``entry`` 含 role / sender_id / sender_name / text 等业务字段（可选
        ``at_bot``、``reply_status``），id 与时间戳由本方法生成。日志行数
        超过阈值时本操作改为全量重写（压缩日志）。
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
            if self._line_count >= _COMPACT_LINES:
                await asyncio.to_thread(self._save)
            else:
                await asyncio.to_thread(
                    self._append_line,
                    {"op": "append", "session_id": session_id, "message": message},
                )
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
                    await asyncio.to_thread(
                        self._append_line,
                        {
                            "op": "reply",
                            "session_id": session_id,
                            "message_id": message_id,
                            "status": status,
                        },
                    )
                    return

    async def clear(self, session_id: str) -> None:
        """清空会话的消息流（重置会话时联动）；低频操作，触发全量重写。"""
        async with self._lock:
            if session_id in self._streams:
                del self._streams[session_id]
                await asyncio.to_thread(self._save)

    async def delete_sessions(self, ids: list[str]) -> None:
        """删除多个会话的消息流（删除会话时联动）；低频操作，触发全量重写。"""
        async with self._lock:
            id_set = set(ids)
            if any(sid in self._streams for sid in id_set):
                self._streams = {
                    sid: stream
                    for sid, stream in self._streams.items()
                    if sid not in id_set
                }
                await asyncio.to_thread(self._save)
