"""store 层公共基类：同步写方法的「串行 + 卸载线程」异步执行通道。

各 store 的同步写方法（如 ``create_group``）保持同步签名（单元测试直接调用），
由 API 层经 ``write`` 执行：实例锁保证并发写不交错（读改写 + 全量落盘的组合
在无锁线程下会丢失更新），``asyncio.to_thread`` 把 JSON 序列化与磁盘 I/O 移出
事件循环线程。``flush`` 是「仅落盘当前内存态」的公共通道（stream_store 内部
也用它做全量重写，但须在不持有锁时调用）。

继承方须在 ``__init__`` 中创建 ``self._lock = asyncio.Lock()``。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any


class AsyncWriteMixin:
    """为 store 提供「锁内线程化」的同步写执行通道。"""

    _lock: asyncio.Lock

    async def write(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """串行 + 卸载线程执行一次同步写操作（含落盘），返回其返回值。"""
        async with self._lock:
            return await asyncio.to_thread(func, *args, **kwargs)

    async def flush(self) -> None:
        """在实例锁内把当前内存态全量落盘（线程内执行，不阻塞事件循环）。"""
        async with self._lock:
            await asyncio.to_thread(self._save)
