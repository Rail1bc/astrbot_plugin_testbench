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

import json
import time
import uuid
from pathlib import Path
from typing import Any

from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path


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


class _ListStore:
    """基于 ``{"items": [...]}`` 文件的通用列表 store（全量写 JSON）。"""

    def __init__(self, file: Path) -> None:
        self._file = file
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

    def list_identities(self) -> list[dict]:
        return self._store.list()

    def get_identity(self, identity_id: str) -> dict | None:
        return self._store.get(identity_id)

    def create_identity(
        self,
        name: str,
        sender_id: str | None = None,
        sender_name: str | None = None,
    ) -> dict:
        """创建身份；sender_id / sender_name 缺失时回退名称。"""
        resolved_name = str(name or "").strip() or "身份"
        identity = {
            "id": f"id_{uuid.uuid4().hex[:8]}",
            "name": resolved_name,
            "sender_id": str(sender_id or "").strip() or resolved_name,
            "sender_name": str(sender_name or "").strip() or resolved_name,
            "created_at": int(time.time()),
        }
        self._store._items.append(identity)
        self._store._save()
        return identity

    def update_identity(
        self,
        identity_id: str,
        *,
        name: Any = None,
        sender_id: Any = None,
        sender_name: Any = None,
    ) -> dict | None:
        """更新身份；未传字段（None）保持不变，空串重置为名称回退。"""
        identity = self._store.get(identity_id)
        if identity is None:
            return None
        if name is not None:
            identity["name"] = str(name or "").strip() or "身份"
        if sender_id is not None:
            identity["sender_id"] = str(sender_id or "").strip() or identity["name"]
        if sender_name is not None:
            identity["sender_name"] = (
                str(sender_name or "").strip() or identity["name"]
            )
        self._store._save()
        return identity

    def delete_identities(self, ids: list[str]) -> int:
        id_set = set(ids)
        kept = [item for item in self._store._items if item["id"] not in id_set]
        removed = len(self._store._items) - len(kept)
        if removed:
            self._store._items = kept
            self._store._save()
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

    def create_chat_group(
        self, name: str, member_ids: list[str] | None = None
    ) -> dict:
        chat_group = {
            "id": f"cg_{uuid.uuid4().hex[:8]}",
            "name": str(name or "").strip() or "群聊",
            "member_ids": self._clean_member_ids(member_ids),
            "created_at": int(time.time()),
        }
        self._store._items.append(chat_group)
        self._store._save()
        return chat_group

    def update_chat_group(
        self,
        group_id: str,
        *,
        name: Any = None,
        member_ids: Any = None,
    ) -> dict | None:
        chat_group = self._store.get(group_id)
        if chat_group is None:
            return None
        if name is not None:
            chat_group["name"] = str(name or "").strip() or "群聊"
        if member_ids is not None:
            chat_group["member_ids"] = self._clean_member_ids(member_ids)
        self._store._save()
        return chat_group

    def delete_chat_groups(self, ids: list[str]) -> int:
        id_set = set(ids)
        kept = [item for item in self._store._items if item["id"] not in id_set]
        removed = len(self._store._items) - len(kept)
        if removed:
            self._store._items = kept
            self._store._save()
        return removed
