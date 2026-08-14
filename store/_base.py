"""store 层公共基类：同步写方法的「串行 + 卸载线程」异步执行通道。

各 store 的同步写方法（如 ``create_group``）保持同步签名（单元测试直接调用），
由 API 层经 ``write`` 执行：实例锁保证并发写不交错（读改写 + 全量落盘的组合
在无锁线程下会丢失更新），``asyncio.to_thread`` 把 JSON 序列化与磁盘 I/O 移出
事件循环线程。``flush`` 是「仅落盘当前内存态」的公共通道（stream_store 内部
也用它做全量重写，但须在不持有锁时调用）。

本模块同时提供持久化的两个公共原语（全部 store 共用）：

- ``atomic_write_text``：写临时文件 + ``os.replace`` 原子替换——进程崩溃 /
  断电不会留下半截文件（直接覆盖写的半截 JSON 会在下次启动被当作损坏数据，
  见 ``backup_corrupt_file`` 的告警路径）。
- ``backup_corrupt_file``：解析失败时把文件改名备份并记告警日志，而不是
  静默从空开始（下一次全量写会把「空」写回，用户数据永久丢失）。

继承方须在 ``__init__`` 中创建 ``self._lock = asyncio.Lock()``。
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

# 数据损坏告警必须可靠到达（插件专用 logger 的 INFO 可能被按插件调级过滤），
# 故走根 astrbot logger，消息带 [testbench] 前缀（同 eval/persona.py 约定）。
logger = logging.getLogger("astrbot")


def atomic_write_text(path: Path, content: str) -> None:
    """原子写：写同目录临时文件后 ``os.replace`` 原子替换。

    直接 ``write_text`` 覆盖的窗口内进程崩溃 / 断电会留下半截 JSON，下次
    启动解析失败被当成空数据（见 ``backup_corrupt_file``），下一次保存就把
    「空」写回——用户数据永久丢失。临时文件与目标同目录保证 ``os.replace``
    在同一文件系统内原子改名（Windows 上 ``os.replace`` 可覆盖已存在目标）。
    """
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def backup_corrupt_file(path: Path) -> None:
    """把解析失败的数据文件改名备份（保留现场供人工恢复），并记告警日志。

    调用方应随后从空态继续运行；备份文件以 ``<name>.corrupt-<ts>`` 命名，
    不参与后续任何读写，人工可据其恢复数据。
    """
    backup = path.with_name(f"{path.name}.corrupt-{int(time.time())}")
    try:
        os.replace(path, backup)
    except OSError:
        logger.warning("[testbench] 损坏数据文件 %s 备份失败，原文件保留", path)
        return
    logger.warning(
        "[testbench] 数据文件 %s 解析失败，已备份为 %s，从空数据继续（可人工恢复）",
        path,
        backup,
    )


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
