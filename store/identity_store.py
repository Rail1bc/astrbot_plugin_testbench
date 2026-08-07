"""测试身份与虚拟群聊数据模型与持久化。

测试身份（sender 实体）与虚拟群聊（成员池）是**跨测试组共享的持久化资源**：
测试组/会话配置、群发栏、测试集消息都可引用。本模块提供两个独立 store：

- ``IdentityStore``：身份列表（name / sender_id / sender_name），消息级发送者来源。
- ``ChatGroupStore``：虚拟群聊列表（name / member_ids），作为群成员来源，
  群聊会话绑定一个虚拟群聊后从其中取默认发送者。

与 groups.json / testsets.json 同目录（`virtual_session/`），全量写 JSON，
不依赖 AstrBot 运行时状态。
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

from ._base import AsyncWriteMixin


def _load_list(file: Path) -> list[dict]:
    """读取 ``{"<key>": [...]}`` 结构的 JSON 文件，损坏时返回空列表。"""
    if not file.exists():
        return []
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        return [item for item in data["items"] if isinstance(item, dict)]
    return []


class _ListStore(AsyncWriteMixin):
    """基于 ``{"items": [...]}`` 文件的通用列表 store（全量写 JSON）。

    同步写方法（add / remove / replace）保持同步签名，由 API 层经 ``write``
    （实例锁内线程化）执行，避免事件循环阻塞与并发写竞态。
    """

    def __init__(self, file: Path) -> None:
        self._file = file
        self._lock = asyncio.Lock()
        self._items: list[dict] = _load_list(file)

    def _save(self) -> None:
        self._file.write_text(
            json.dumps({"items": self._items}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def list(self) -> list[dict]:
        return list(self._items)

    def get(self, item_id: str) -> dict | None:
        for item in self._items:
            if item["id"] == item_id:
                return item
        return None

    def add(self, item: dict) -> None:
        """追加一项并落盘。"""
        self._items.append(item)
        self._save()

    def remove(self, ids: Iterable[str]) -> int:
        """按 id 批量删除；有删除才落盘，返回删除数量。"""
        id_set = set(ids)
        kept = [item for item in self._items if item["id"] not in id_set]
        removed = len(self._items) - len(kept)
        if removed:
            self._items = kept
            self._save()
        return removed

    def replace(self, item_id: str, fields: dict) -> dict | None:
        """原地更新一项（字段合并）并落盘；不存在返回 None。"""
        item = self.get(item_id)
        if item is None:
            return None
        item.update(fields)
        self._save()
        return item


class IdentityStore:
    """测试身份的创建、持久化与查询。

    数据保存到 `virtual_session/identities.json`。身份是 {name, sender_id,
    sender_name} 三元组：虚拟会话投递消息时可指定身份作为发送者，模拟不同
    成员的发言。
    """

    def __init__(self, data_dir: Path | None = None) -> None:
        base = Path(get_astrbot_plugin_data_path()) if data_dir is None else data_dir
        directory = base / "virtual_session"
        directory.mkdir(parents=True, exist_ok=True)
        self._store = _ListStore(directory / "identities.json")
        # sender_id → 管理员 惰性索引：首次查询时构建，写操作（create/update/
        # delete）后失效，下次查询重建。runner 每次发送都要查一次角色，身份库
        # 膨胀后线性扫描是热点（_resolve_role 曾对全部身份逐一比对）。
        self._admin_index: set[str] | None = None

    async def write(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """经 _ListStore 实例锁线程化执行一次同步写操作（create/update/delete）。"""
        return await self._store.write(func, *args, **kwargs)

    def _invalidate_admin_index(self) -> None:
        """写操作后使索引失效（sender_id / is_admin 可能已变）。"""
        self._admin_index = None

    def is_admin_of(self, sender_id: str) -> bool:
        """发送者 id 是否命中任一管理员身份。

        语义与旧 `_resolve_role` 一致：sender_id 精确匹配且 ``is_admin`` 为真
        即视为管理员（同一 sender_id 可对应多个身份，任一命中即真）；不在库
        中、旧数据缺 is_admin 键一律非管理员。索引惰性构建，写操作后失效。
        """
        if self._admin_index is None:
            # 与旧 _resolve_role 逐条扫描谓词逐点等价：仅 is_admin 身份的
            # sender_id 入集；非 str / 缺键排除（旧实现与 str 查询值比较恒不
            # 命中）；空串保留（旧实现 `"" == ""` 会命中）。isinstance 同时防
            # 手改 JSON 里不可哈希值撑爆 set。
            self._admin_index = {
                ident["sender_id"]
                for ident in self._store.list()
                if ident.get("is_admin") and isinstance(ident.get("sender_id"), str)
            }
        return sender_id in self._admin_index

    def list_identities(self) -> list[dict]:
        return self._store.list()

    def get_identity(self, identity_id: str) -> dict | None:
        return self._store.get(identity_id)

    def create_identity(
        self,
        name: str,
        sender_id: str | None = None,
        sender_name: str | None = None,
        is_admin: Any = None,
    ) -> dict:
        """创建身份；sender_id / sender_name 缺失时回退名称，is_admin 缺省 False。"""
        resolved_name = str(name or "").strip() or "身份"
        identity = {
            "id": f"id_{uuid.uuid4().hex[:8]}",
            "name": resolved_name,
            "sender_id": str(sender_id or "").strip() or resolved_name,
            "sender_name": str(sender_name or "").strip() or resolved_name,
            "is_admin": bool(is_admin),
            "created_at": int(time.time()),
        }
        self._store.add(identity)
        self._invalidate_admin_index()
        return identity

    def update_identity(
        self,
        identity_id: str,
        *,
        name: Any = None,
        sender_id: Any = None,
        sender_name: Any = None,
        is_admin: Any = None,
    ) -> dict | None:
        """更新身份；未传字段（None）保持不变，空串重置为名称回退，is_admin 显式 false 生效。"""
        current = self._store.get(identity_id)
        if current is None:
            return None
        fields: dict[str, Any] = {}
        if name is not None:
            fields["name"] = str(name or "").strip() or "身份"
        if sender_id is not None:
            fields["sender_id"] = (
                str(sender_id or "").strip() or fields.get("name") or current["name"]
            )
        if sender_name is not None:
            fields["sender_name"] = (
                str(sender_name or "").strip() or fields.get("name") or current["name"]
            )
        if is_admin is not None:
            fields["is_admin"] = bool(is_admin)
        result = self._store.replace(identity_id, fields)
        if result is not None:
            self._invalidate_admin_index()
        return result

    def delete_identities(self, ids: list[str]) -> int:
        removed = self._store.remove(ids)
        if removed:
            self._invalidate_admin_index()
        return removed


class ChatGroupStore:
    """虚拟群聊的创建、持久化与查询。

    数据保存到 `virtual_session/chat_groups.json`。虚拟群聊是成员池（member_ids
    引用 IdentityStore 的身份 id）；GroupMessage 测试会话可绑定一个虚拟群聊，
    投递消息时取群内首个成员作为默认发送者。
    """

    def __init__(self, data_dir: Path | None = None) -> None:
        base = Path(get_astrbot_plugin_data_path()) if data_dir is None else data_dir
        directory = base / "virtual_session"
        directory.mkdir(parents=True, exist_ok=True)
        self._store = _ListStore(directory / "chat_groups.json")

    async def write(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """经 _ListStore 实例锁线程化执行一次同步写操作（create/update/delete）。"""
        return await self._store.write(func, *args, **kwargs)

    def list_chat_groups(self) -> list[dict]:
        return self._store.list()

    def get_chat_group(self, group_id: str) -> dict | None:
        return self._store.get(group_id)

    @staticmethod
    def _clean_member_ids(member_ids: Any) -> list[str]:
        """清洗成员 id 列表：仅保留非空字符串，按出现顺序去重。"""
        if not isinstance(member_ids, list):
            return []
        out: list[str] = []
        for mid in member_ids:
            mid = str(mid or "").strip()
            if mid and mid not in out:
                out.append(mid)
        return out

    def create_chat_group(self, name: str, member_ids: list[str] | None = None) -> dict:
        chat_group = {
            "id": f"cg_{uuid.uuid4().hex[:8]}",
            "name": str(name or "").strip() or "群聊",
            "member_ids": self._clean_member_ids(member_ids),
            "created_at": int(time.time()),
        }
        self._store.add(chat_group)
        return chat_group

    def update_chat_group(
        self,
        group_id: str,
        *,
        name: Any = None,
        member_ids: Any = None,
    ) -> dict | None:
        fields: dict[str, Any] = {}
        if name is not None:
            fields["name"] = str(name or "").strip() or "群聊"
        if member_ids is not None:
            fields["member_ids"] = self._clean_member_ids(member_ids)
        return self._store.replace(group_id, fields)

    def delete_chat_groups(self, ids: list[str]) -> int:
        return self._store.remove(ids)
