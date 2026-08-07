"""测试组接口：组 CRUD 与组配置更新。"""

from __future__ import annotations

from typing import Any

from astrbot.api.web import error_response, json_response, request

from ..core.conf_routes import delete_route_if_exists
from ..core.conf_tools import conf_has_callable_tools
from ..store.group_store import umo_of
from .common import MAX_SESSIONS_PER_GROUP, ConfRouteMixin


class GroupsAPI(ConfRouteMixin):
    """测试组 handler 集合（挂在 Star 上，共享 self.group_mgr / self.history_ops）。"""

    async def list_groups(self):
        """列出全部测试组（含组内会话，会话字段为原始覆盖值）。

        每个组附派生键 ``security_warning``：组或任一会话的有效配置档案启用了
        任何可调用工具即为真。标记按当前配置实时计算（配置档案事后可能被修改，
        故不持久化，每次列表重新判定）；逐组浅拷贝，不改写 store 内共享 dict。
        """
        groups = [
            {**g, "security_warning": self._group_has_callable_tools(g)}
            for g in self.group_mgr.list_groups()
        ]
        return json_response({"groups": groups})

    def _group_has_callable_tools(self, group: dict) -> bool:
        """组是否启用了可调用工具：组级 conf 或任一会话级 conf 覆盖命中即真。

        conf_id 为 None/""（未绑定）时按默认配置判定，conf_id 已删除时回退默认
        配置——镜像 ``astrbot_config_mgr.get_conf`` 的运行时回退语义（绑定失效后
        实际生效的正是默认配置）。
        """
        confs = getattr(
            getattr(self.context, "astrbot_config_mgr", None), "confs", None
        )
        if not isinstance(confs, dict):
            return False

        def risky(conf_id: str | None) -> bool:
            conf = confs.get(conf_id) if conf_id else confs.get("default")
            return conf_has_callable_tools(conf)

        if risky(group.get("conf_id")):
            return True
        for session in group.get("sessions") or []:
            if risky(session.get("conf_id") or group.get("conf_id")):
                return True
        return False

    async def create_group(self):
        """创建测试组并生成组内虚拟会话，可选绑定配置档案（UCR 会话级路由）。"""
        payload = await request.json(default={})
        count = payload.get("count", 1)
        if not isinstance(count, int) or isinstance(count, bool):
            return error_response("count 必须是整数", status_code=400)
        if count < 1 or count > MAX_SESSIONS_PER_GROUP:
            return error_response(
                f"count 必须在 1-{MAX_SESSIONS_PER_GROUP} 之间",
                status_code=400,
            )
        conf_id = payload.get("conf_id") or None
        group = await self.group_mgr.write(
            self.group_mgr.create_group,
            name=payload.get("name"),
            count=count,
            platform_id=payload.get("platform_id"),
            conf_id=conf_id,
            sender_id=payload.get("sender_id"),
            sender_name=payload.get("sender_name"),
            name_prefix=payload.get("name_prefix"),
            message_type=payload.get("message_type"),
            chat_group_id=payload.get("chat_group_id"),
        )
        if conf_id:
            sessions = [self.group_mgr.effective(group, s) for s in group["sessions"]]
            await self._apply_conf_routes(sessions, conf_id)
        return json_response(group)

    async def delete_groups(self):
        """删除测试组，并联动清理组内会话的配置档案路由、原生对话历史与消息流。"""
        payload = await request.json(default={})
        ids = payload.get("ids")
        if not isinstance(ids, list) or not ids:
            return error_response("ids 不能为空", status_code=400)
        removed = await self.group_mgr.write(self.group_mgr.delete_groups, ids)
        sessions = [self.group_mgr.effective(group, s) for group, s in removed]
        await self._clear_conf_routes(sessions)
        await self.history_ops.delete_session_conversations(sessions)
        await self.stream_store.delete_sessions([s["id"] for s in sessions])
        return json_response(
            {
                "deleted": len(removed),
                "sessions": [s["id"] for s in sessions],
            }
        )

    async def add_group_sessions(self, group_id: str):
        """向测试组内新增会话（继承组配置，组配置档案同样应用到新会话）。"""
        payload = await request.json(default={})
        count = payload.get("count", 1)
        if not isinstance(count, int) or isinstance(count, bool):
            return error_response("count 必须是整数", status_code=400)
        if count < 1 or count > MAX_SESSIONS_PER_GROUP:
            return error_response(
                f"count 必须在 1-{MAX_SESSIONS_PER_GROUP} 之间",
                status_code=400,
            )
        group = self.group_mgr.get_group(group_id)
        if group is None:
            return error_response("未找到该测试组", status_code=404)
        created = await self.group_mgr.write(
            self.group_mgr.add_sessions,
            group_id,
            count,
            payload.get("name_prefix"),
        )
        conf_id = group.get("conf_id") or None
        if conf_id:
            sessions = [self.group_mgr.effective(group, s) for s in created]
            await self._apply_conf_routes(sessions, conf_id)
        return json_response(created)

    async def update_group(self, group_id: str):
        """更新测试组配置；组平台/消息类型/档案变更会同步应用到仍继承组配置的会话。

        会话已单独覆盖的字段不受组配置变更影响（会话覆盖优先）。平台或消息
        类型变更使 umo 变化：清理旧 umo 的路由与对话历史，再按新的有效配置同步。
        """
        payload = await request.json(default={})
        group = self.group_mgr.get_group(group_id)
        if group is None:
            return error_response("未找到该测试组", status_code=404)

        updates: dict[str, Any] = {}
        for key in (
            "name",
            "platform_id",
            "conf_id",
            "sender_id",
            "sender_name",
            "message_type",
            "chat_group_id",
        ):
            if key not in payload:
                continue
            value = payload[key]
            updates[key] = value if isinstance(value, str) and value else None

        old_sessions = [self.group_mgr.effective(group, s) for s in group["sessions"]]
        await self.group_mgr.write(self.group_mgr.update_group, group_id, **updates)
        updated = self.group_mgr.get_group(group_id)
        if updated is None:
            return error_response("未找到该测试组", status_code=404)

        # 按 id 配对旧/新会话，不依赖两列表顺序一致（update_group 当前不改会话结构，
        # 但按位置 zip 属隐含假设，未来组内新增/重排会话时会错位）
        old_by_id = {s["id"]: s for s in old_sessions}
        for session in updated["sessions"]:
            old = old_by_id.get(session["id"])
            if old is None:
                continue
            new = self.group_mgr.effective(updated, session)
            umo_changed = old["platform_id"] != new["platform_id"] or (
                old["message_type"] != new["message_type"]
            )
            conf_changed = old["conf_id"] != new["conf_id"]
            if umo_changed:
                await delete_route_if_exists(
                    self.context.astrbot_config_mgr.ucr, umo_of(old)
                )
                await self.history_ops.delete_session_conversations([old])
            if umo_changed or conf_changed:
                await self._sync_conf_route(new)
        return json_response(updated)
