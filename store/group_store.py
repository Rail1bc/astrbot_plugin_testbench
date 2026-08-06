"""测试组数据模型与持久化。

虚拟会话以「测试组」为单位管理：一个组是一组共享同一套配置（平台来源、
配置档案、发送者 id/昵称）的虚拟会话，组内单个会话可覆盖组配置。本模块
负责测试组与会话的创建、持久化与配置解析，不依赖 AstrBot 运行时状态。
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

DEFAULT_PLATFORM_ID = "webchat"
DEFAULT_SENDER_ID = "testbench"
DEFAULT_SENDER_NAME = "测试台"
MESSAGE_TYPE = "FriendMessage"

# 用于区分「未传该字段」与「显式传 null（恢复继承组配置）」
_UNSET = object()


def umo_of(session: dict) -> str:
    """根据（已解析的）虚拟会话数据计算 unified_msg_origin。"""
    platform_id = session.get("platform_id") or DEFAULT_PLATFORM_ID
    message_type = session.get("message_type") or MESSAGE_TYPE
    return f"{platform_id}:{message_type}:{session['id']}"


class VirtualGroupManager:
    """测试组的创建、持久化与配置解析。

    数据保存到 data 目录下 `virtual_session/groups.json`，符合「插件持久化
    数据存 data 目录」的规范。旧版平铺会话文件（sessions.json）会自动迁移为
    一个「默认测试组」。
    """

    def __init__(self, data_dir: Path | None = None) -> None:
        base = Path(get_astrbot_plugin_data_path()) if data_dir is None else data_dir
        self._dir = base / "virtual_session"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._file = self._dir / "groups.json"
        self._legacy_file = self._dir / "sessions.json"
        self._groups: list[dict] = self._load()

    # ---------- 持久化 ----------

    def _load(self) -> list[dict]:
        if self._file.exists():
            try:
                data = json.loads(self._file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                data = None
            if isinstance(data, dict) and isinstance(data.get("groups"), list):
                groups: list[dict] = []
                for g in data["groups"]:
                    if not isinstance(g, dict):
                        continue
                    # 旧版数据补齐新增字段，防止缺键崩溃
                    g.setdefault("message_type", None)
                    g.setdefault("auto_at", True)
                    g.setdefault("chat_group_id", None)
                    for s in g.get("sessions", []):
                        if isinstance(s, dict):
                            s.setdefault("message_type", None)
                            s.setdefault("auto_at", None)
                            s.setdefault("chat_group_id", None)
                    groups.append(g)
                return groups
        return self._migrate_legacy()

    def _migrate_legacy(self) -> list[dict]:
        """旧版 sessions.json（平铺会话列表）迁移为单个默认测试组。"""
        if not self._legacy_file.exists():
            return []
        try:
            legacy = json.loads(self._legacy_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(legacy, list) or not legacy:
            return []
        group = {
            "id": f"g_{uuid.uuid4().hex[:8]}",
            "name": "默认测试组",
            "platform_id": None,
            "conf_id": None,
            "sender_id": None,
            "sender_name": None,
            "message_type": None,
            "auto_at": True,
            "chat_group_id": None,
            "created_at": int(time.time()),
            "sessions": legacy,
        }
        self._groups = [group]
        self._save()
        return self._groups

    def _save(self) -> None:
        self._file.write_text(
            json.dumps({"groups": self._groups}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # ---------- 查询 ----------

    def list_groups(self) -> list[dict]:
        """返回全部测试组。"""
        return list(self._groups)

    def get_group(self, group_id: str) -> dict | None:
        for group in self._groups:
            if group["id"] == group_id:
                return group
        return None

    def find_session(self, session_id: str) -> tuple[dict, dict] | None:
        """按 id 查找会话，返回 (所属组, 会话) 或 None。"""
        for group in self._groups:
            for session in group.get("sessions", []):
                if session["id"] == session_id:
                    return group, session
        return None

    def all_sessions(self) -> list[tuple[dict, dict]]:
        """返回全部 (组, 会话) 对。"""
        return [
            (group, session)
            for group in self._groups
            for session in group.get("sessions", [])
        ]

    # ---------- 创建 / 删除 ----------

    def create_group(
        self,
        name: str,
        count: int = 1,
        platform_id: str | None = None,
        conf_id: str | None = None,
        sender_id: str | None = None,
        sender_name: str | None = None,
        name_prefix: str | None = None,
        message_type: str | None = None,
        auto_at: bool = True,
        chat_group_id: str | None = None,
    ) -> dict:
        """创建测试组并生成 count 个继承组配置的虚拟会话。

        ``message_type`` 为 "FriendMessage" / "GroupMessage"（None 走默认私聊）；
        ``auto_at`` 默认开启（模拟「@机器人」发言）；``chat_group_id`` 绑定
        虚拟群聊作为群成员来源（仅 GroupMessage 有意义）。
        """
        group = {
            "id": f"g_{uuid.uuid4().hex[:8]}",
            "name": str(name or "").strip() or "测试组",
            "platform_id": platform_id,
            "conf_id": conf_id,
            "sender_id": sender_id,
            "sender_name": sender_name,
            "message_type": message_type,
            "auto_at": auto_at,
            "chat_group_id": chat_group_id,
            "created_at": int(time.time()),
            "sessions": [],
        }
        group["sessions"] = self._new_sessions(group, count, name_prefix)
        self._groups.append(group)
        self._save()
        return group

    def add_sessions(
        self,
        group_id: str,
        count: int,
        name_prefix: str | None = None,
    ) -> list[dict]:
        """向组内新增会话（默认继承组配置），返回新增的会话。"""
        group = self.get_group(group_id)
        if group is None:
            raise KeyError(group_id)
        created = self._new_sessions(group, count, name_prefix)
        group["sessions"].extend(created)
        self._save()
        return created

    def _new_sessions(
        self, group: dict, count: int, name_prefix: str | None
    ) -> list[dict]:
        prefix = str(name_prefix or "").strip() or group.get("name") or "虚拟会话"
        base = len(group.get("sessions", []))
        return [
            {
                "id": f"vs_{uuid.uuid4().hex[:8]}",
                "name": f"{prefix}{base + i + 1}",
                "created_at": int(time.time()),
                # 七键均为 None 时表示继承组配置
                "platform_id": None,
                "conf_id": None,
                "sender_id": None,
                "sender_name": None,
                "message_type": None,
                "auto_at": None,
                "chat_group_id": None,
            }
            for i in range(count)
        ]

    def delete_groups(self, ids: list[str]) -> list[tuple[dict, dict]]:
        """删除测试组，返回被删除的 (组, 会话) 对（用于清理路由）。"""
        id_set = set(ids)
        removed: list[tuple[dict, dict]] = []
        kept: list[dict] = []
        matched = False
        for group in self._groups:
            if group["id"] in id_set:
                matched = True
                for session in group.get("sessions", []):
                    removed.append((group, session))
            else:
                kept.append(group)
        # 用 matched 而非 removed：组内 0 会话时 removed 恒为空，但组仍须删除
        if matched:
            self._groups = kept
            self._save()
        return removed

    def delete_sessions(self, ids: list[str]) -> list[tuple[dict, dict]]:
        """删除会话（从其所属组中移除），返回被删除的 (组, 会话) 对。"""
        id_set = set(ids)
        removed: list[tuple[dict, dict]] = []
        for group in self._groups:
            keep: list[dict] = []
            for session in group.get("sessions", []):
                if session["id"] in id_set:
                    removed.append((group, session))
                else:
                    keep.append(session)
            if len(keep) != len(group.get("sessions", [])):
                group["sessions"] = keep
        if removed:
            self._save()
        return removed

    # ---------- 会话配置覆盖 ----------

    def update_session(
        self,
        session_id: str,
        *,
        platform_id: Any = _UNSET,
        conf_id: Any = _UNSET,
        sender_id: Any = _UNSET,
        sender_name: Any = _UNSET,
        message_type: Any = _UNSET,
        auto_at: Any = _UNSET,
        chat_group_id: Any = _UNSET,
    ) -> None:
        """更新会话的配置覆盖。

        字段值为 None 表示恢复「继承组配置」；conf_id 为 "" 表示显式使用
        默认配置档案（不绑定）。message_type / chat_group_id 空串同样归一为
        None（继承组）；auto_at 保留原值语义（True / False / None）。会话对象
        原地变更，调用方持有的引用即最新值；会话不存在时不生效（不抛错）。
        """
        found = self.find_session(session_id)
        if found is None:
            return
        _, session = found
        changed = False
        for key, value in {
            "platform_id": platform_id,
            "conf_id": conf_id,
            "sender_id": sender_id,
            "sender_name": sender_name,
            "message_type": message_type,
            "auto_at": auto_at,
            "chat_group_id": chat_group_id,
        }.items():
            if value is not _UNSET:
                # conf_id 的空串语义是「显式使用默认档案」（不绑定），须原样保留
                if (
                    key in ("platform_id", "message_type", "chat_group_id")
                    and value == ""
                ):
                    value = None
                session[key] = value
                changed = True
        if changed:
            self._save()

    def update_group(
        self,
        group_id: str,
        *,
        name: Any = _UNSET,
        platform_id: Any = _UNSET,
        conf_id: Any = _UNSET,
        sender_id: Any = _UNSET,
        sender_name: Any = _UNSET,
        message_type: Any = _UNSET,
        auto_at: Any = _UNSET,
        chat_group_id: Any = _UNSET,
    ) -> dict | None:
        """更新测试组配置。

        字段值为 None 表示恢复默认（平台/档案/message_type/chat_group_id 空串
        归一为 None）；组名空串回退「测试组」。已单独覆盖的会话配置不受影响
        （仍以会话覆盖优先）。返回更新后的组，组不存在时返回 None。
        """
        group = self.get_group(group_id)
        if group is None:
            return None
        for key, value in {
            "name": name,
            "platform_id": platform_id,
            "conf_id": conf_id,
            "sender_id": sender_id,
            "sender_name": sender_name,
            "message_type": message_type,
            "auto_at": auto_at,
            "chat_group_id": chat_group_id,
        }.items():
            if value is not _UNSET:
                if (
                    key in ("platform_id", "conf_id", "message_type", "chat_group_id")
                    and value == ""
                ):
                    value = None
                group[key] = value
        if not (group.get("name") or "").strip():
            group["name"] = "测试组"
        self._save()
        return group

    # ---------- 配置解析 ----------

    @staticmethod
    def effective(group: dict, session: dict) -> dict:
        """把组共享配置与会话覆盖解析为最终配置（会话覆盖优先）。"""
        conf_id = session.get("conf_id")
        if conf_id is None:
            conf_id = group.get("conf_id") or None
        else:
            conf_id = conf_id or None  # "" 表示显式使用默认配置档案

        # 消息类型：会话 → 组 → 默认私聊；auto_at 与会话消息类型共同决定
        # 是否模拟「@机器人」发言（仅 GroupMessage 有意义，见 core/runner.py）。
        message_type = (
            session.get("message_type") or group.get("message_type") or MESSAGE_TYPE
        )
        auto_at = session.get("auto_at")
        if auto_at is None:
            auto_at = group.get("auto_at")
            if auto_at is None:
                auto_at = True
        chat_group_id = session.get("chat_group_id")
        if chat_group_id is None:
            chat_group_id = group.get("chat_group_id") or None

        return {
            "id": session["id"],
            "name": session.get("name", session["id"]),
            "platform_id": (
                session.get("platform_id")
                or group.get("platform_id")
                or DEFAULT_PLATFORM_ID
            ),
            "sender_id": session.get("sender_id")
            or group.get("sender_id")
            or DEFAULT_SENDER_ID,
            "sender_name": (
                session.get("sender_name")
                or group.get("sender_name")
                or DEFAULT_SENDER_NAME
            ),
            "conf_id": conf_id,
            "message_type": message_type,
            "auto_at": auto_at,
            "chat_group_id": chat_group_id,
            "created_at": session.get("created_at", 0),
        }

    def effective_many(self, ids: list[str]) -> list[dict]:
        """按 id 批量解析会话为最终配置（保持传入顺序，缺失的跳过）。"""
        wanted = set(ids)
        found = {
            session["id"]: (group, session)
            for group, session in self.all_sessions()
            if session["id"] in wanted
        }
        return [self.effective(*found[sid]) for sid in ids if sid in found]

    def flat_sessions(self) -> list[dict]:
        """返回全部会话的展平列表（已解析最终配置，附组信息）。"""
        out: list[dict] = []
        for group, session in self.all_sessions():
            resolved = self.effective(group, session)
            resolved["group_id"] = group["id"]
            resolved["group_name"] = group["name"]
            out.append(resolved)
        return out
