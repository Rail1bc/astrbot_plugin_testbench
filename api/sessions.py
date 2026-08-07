"""会话接口：会话 CRUD、对话历史查看、克隆与衍生。"""

from __future__ import annotations

import json
from typing import Any

from astrbot.api.web import error_response, json_response

from ..core.conf_routes import delete_route_if_exists
from ..store.group_store import umo_of
from .common import MAX_SESSIONS_PER_GROUP, ConfRouteMixin, json_dict


class SessionsAPI(ConfRouteMixin):
    """会话 handler 集合（挂在 Star 上，共享 self.group_mgr / self.history_ops）。"""

    async def list_sessions(self):
        """列出全部虚拟会话（已解析最终配置，附组信息）。"""
        return json_response(self.group_mgr.flat_sessions())

    async def update_session(self):
        """设置会话自身的配置覆盖；传 null 恢复继承组配置，conf_id 传空串显式使用默认档案。"""
        payload = await json_dict()
        if payload is None:
            return error_response("请求体必须是 JSON 对象", status_code=400)
        session_id = payload.get("id")
        if not isinstance(session_id, str) or not session_id:
            return error_response("id 不能为空", status_code=400)
        found = self.group_mgr.find_session(session_id)
        if found is None:
            return error_response("未找到该虚拟会话", status_code=404)
        group, session = found
        old_session = self.group_mgr.effective(group, session)

        overrides: dict[str, Any] = {}
        for key in (
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
            if value is None:
                overrides[key] = None  # 恢复继承组配置
            elif key == "conf_id" and value == "":
                overrides[key] = ""  # 显式使用默认配置档案（不绑定）
            elif isinstance(value, str) and value:
                overrides[key] = value
            else:
                overrides[key] = None

        await self.group_mgr.write(
            self.group_mgr.update_session, session_id, **overrides
        )
        new_session = self.group_mgr.effective(group, session)

        # 平台或消息类型变更会使 umo 变化：清理旧 umo 的路由与对话历史，再按新 umo 同步
        umo_changed = old_session["platform_id"] != new_session["platform_id"] or (
            old_session["message_type"] != new_session["message_type"]
        )
        conf_changed = old_session["conf_id"] != new_session["conf_id"]
        if umo_changed:
            await delete_route_if_exists(
                self.context.astrbot_config_mgr.ucr, umo_of(old_session)
            )
            await self.history_ops.delete_session_conversations([old_session])
        if umo_changed or conf_changed:
            await self._sync_conf_route(new_session)
        return json_response(new_session)

    async def delete_sessions(self):
        """删除虚拟会话，并联动清理其配置档案路由、原生对话历史与消息流。"""
        payload = await json_dict()
        if payload is None:
            return error_response("请求体必须是 JSON 对象", status_code=400)
        ids = payload.get("ids")
        if not isinstance(ids, list) or not ids:
            return error_response("ids 不能为空", status_code=400)
        removed = await self.group_mgr.write(self.group_mgr.delete_sessions, ids)
        sessions = [self.group_mgr.effective(group, s) for group, s in removed]
        await self._clear_conf_routes(sessions)
        await self.history_ops.delete_session_conversations(sessions)
        await self.stream_store.delete_sessions([s["id"] for s in sessions])
        return json_response({"deleted": len(sessions)})

    async def clone_sessions(self):
        """克隆会话：在源会话所属测试组内新建 count 个会话，并把源会话的
        对话历史拷贝给每个新会话——同一历史起点，可分别改配置/模型测试。"""
        payload = await json_dict()
        if payload is None:
            return error_response("请求体必须是 JSON 对象", status_code=400)
        session_id = payload.get("session_id")
        count = payload.get("count")
        if not isinstance(session_id, str) or not session_id:
            return error_response("session_id 不能为空", status_code=400)
        if (
            not isinstance(count, int)
            or isinstance(count, bool)
            or count < 1
            or count > MAX_SESSIONS_PER_GROUP
        ):
            return error_response(
                f"count 必须是 1-{MAX_SESSIONS_PER_GROUP} 之间的整数",
                status_code=400,
            )
        found = self.group_mgr.find_session(session_id)
        if found is None:
            return error_response("未找到该虚拟会话", status_code=404)
        group, session = found
        if len(group.get("sessions", [])) + count > MAX_SESSIONS_PER_GROUP:
            return error_response("克隆后会话数超过测试组上限", status_code=400)
        created = await self.group_mgr.write(
            self.group_mgr.add_sessions,
            group["id"],
            count,
            name_prefix=session.get("name"),
        )
        resolved_created = [self.group_mgr.effective(group, s) for s in created]
        conf_id = group.get("conf_id") or None
        if conf_id:
            await self._apply_conf_routes(resolved_created, conf_id)
        copied = await self.history_ops.copy_history(
            self.group_mgr.effective(group, session), resolved_created
        )
        return json_response(
            {
                "group_id": group["id"],
                "session_ids": [s["id"] for s in created],
                "copied": copied,
            }
        )

    async def derive_session(self):
        """衍生会话：基于某会话的对话历史创建全新测试组，组内每个会话的历史
        都与该目标会话一致——同一起点的全新会话集合，便于后续分别改配置测试。
        新组继承源组的平台/档案/发送者配置。"""
        payload = await json_dict()
        if payload is None:
            return error_response("请求体必须是 JSON 对象", status_code=400)
        session_id = payload.get("session_id")
        count = payload.get("count")
        if not isinstance(session_id, str) or not session_id:
            return error_response("session_id 不能为空", status_code=400)
        if (
            not isinstance(count, int)
            or isinstance(count, bool)
            or count < 1
            or count > MAX_SESSIONS_PER_GROUP
        ):
            return error_response(
                f"count 必须是 1-{MAX_SESSIONS_PER_GROUP} 之间的整数",
                status_code=400,
            )
        found = self.group_mgr.find_session(session_id)
        if found is None:
            return error_response("未找到该虚拟会话", status_code=404)
        group, session = found
        name = payload.get("name")
        new_group = await self.group_mgr.write(
            self.group_mgr.create_group,
            name=(
                f"{group.get('name') or '测试组'} 衍生"
                if not (isinstance(name, str) and name.strip())
                else name
            ),
            count=count,
            platform_id=group.get("platform_id"),
            conf_id=group.get("conf_id"),
            sender_id=group.get("sender_id"),
            sender_name=group.get("sender_name"),
        )
        resolved_new = [
            self.group_mgr.effective(new_group, s) for s in new_group["sessions"]
        ]
        conf_id = new_group.get("conf_id") or None
        if conf_id:
            await self._apply_conf_routes(resolved_new, conf_id)
        copied = await self.history_ops.copy_history(
            self.group_mgr.effective(group, session), resolved_new
        )
        return json_response(
            {
                "group_id": new_group["id"],
                "group_name": new_group["name"],
                "session_ids": [s["id"] for s in new_group["sessions"]],
                "copied": copied,
            }
        )

    async def session_history(self, session_id: str):
        """查看虚拟会话的对话历史（LLM 上下文消息列表）。"""
        found = self.group_mgr.find_session(session_id)
        if found is None:
            return error_response("未找到该虚拟会话", status_code=404)
        group, session = found
        resolved = self.group_mgr.effective(group, session)
        convs = await self.context.conversation_manager.get_conversations(
            umo_of(resolved)
        )
        conversations = []
        for conv in convs:
            history: list[dict] = []
            if conv.history:
                try:
                    history = json.loads(conv.history)
                except json.JSONDecodeError:
                    history = []
            conversations.append(
                {
                    "conversation_id": conv.cid,
                    "title": conv.title,
                    "history": history,
                }
            )
        return json_response({"conversations": conversations})

    async def reset_sessions(self):
        """重置虚拟会话的对话历史与消息流（删除该 umo 下的全部对话）。"""
        payload = await json_dict()
        if payload is None:
            return error_response("请求体必须是 JSON 对象", status_code=400)
        ids = payload.get("ids")
        if not isinstance(ids, list) or not ids:
            return error_response("ids 不能为空", status_code=400)
        sessions = self.group_mgr.effective_many(ids)
        reset = await self.history_ops.delete_session_conversations(sessions)
        for session in sessions:
            await self.stream_store.clear(session["id"])
        return json_response({"reset": reset})

    async def save_history(self):
        """整体替换会话的对话历史（完整逻辑在 history_ops.HistoryOps）。"""
        return await self.history_ops.save_history()

    async def regenerate_history(self):
        """重新生成指定轮次（完整逻辑在 history_ops.HistoryOps）。"""
        return await self.history_ops.regenerate_history()
