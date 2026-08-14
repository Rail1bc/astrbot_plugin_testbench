"""身份 / 虚拟群聊 / 群消息流接口。

测试身份（sender 实体）与虚拟群聊（成员池）是跨测试组共享的持久化资源，
在前端 rail 第三视图「身份与群聊」中管理、在组配置与群发栏下拉中引用；群
消息流是会话的运行时记录（与 LLM 历史并行，面板切换查看）。
"""

from __future__ import annotations

from astrbot.api.web import error_response, json_response

from .common import json_dict


def _require_ids(payload: dict) -> list[str] | None:
    """校验并返回非空 id 列表（元素须为非空字符串）；无效返回 None（调用方转 400）。

    元素级校验防 dict/list 漏网：store 内部 ``set(ids)`` 对不可哈希元素会抛
    TypeError → 500。
    """
    ids = payload.get("ids")
    if not isinstance(ids, list) or not ids:
        return None
    if not all(isinstance(x, str) and x for x in ids):
        return None
    return ids


class IdentitiesAPI:
    """测试身份、虚拟群聊与消息流 handler 集合。

    挂在 Star 上，共享 self.identity_store / self.chat_group_store /
    self.stream_store / self.group_mgr。
    """

    # ---------- 身份 ----------

    async def list_identities(self):
        """列出全部测试身份。"""
        return json_response({"identities": self.identity_store.list_identities()})

    async def create_identity(self):
        """创建测试身份（name 必填；sender_id / sender_name 缺失时回退名称）。"""
        payload = await json_dict()
        if payload is None:
            return error_response("请求体必须是 JSON 对象", status_code=400)
        name = payload.get("name")
        if not isinstance(name, str) or not name.strip():
            return error_response("name 不能为空", status_code=400)
        identity = await self.identity_store.write(
            self.identity_store.create_identity,
            name=name,
            sender_id=payload.get("sender_id"),
            sender_name=payload.get("sender_name"),
            is_admin=payload.get("is_admin"),
        )
        return json_response(identity)

    async def update_identity(self, identity_id: str):
        """更新测试身份（只更新传入字段）。"""
        payload = await json_dict()
        if payload is None:
            return error_response("请求体必须是 JSON 对象", status_code=400)
        updated = await self.identity_store.write(
            self.identity_store.update_identity,
            identity_id,
            name=payload.get("name"),
            sender_id=payload.get("sender_id"),
            sender_name=payload.get("sender_name"),
            is_admin=payload.get("is_admin"),
        )
        if updated is None:
            return error_response("未找到该身份", status_code=404)
        return json_response(updated)

    async def delete_identities(self):
        """删除测试身份。"""
        payload = await json_dict()
        if payload is None:
            return error_response("请求体必须是 JSON 对象", status_code=400)
        ids = _require_ids(payload)
        if ids is None:
            return error_response("ids 不能为空", status_code=400)
        deleted = await self.identity_store.write(
            self.identity_store.delete_identities, ids
        )
        return json_response({"deleted": deleted})

    # ---------- 虚拟群聊 ----------

    async def list_chat_groups(self):
        """列出全部虚拟群聊。"""
        return json_response({"chat_groups": self.chat_group_store.list_chat_groups()})

    async def create_chat_group(self):
        """创建虚拟群聊（name 必填；member_ids 引用身份 id）。"""
        payload = await json_dict()
        if payload is None:
            return error_response("请求体必须是 JSON 对象", status_code=400)
        name = payload.get("name")
        if not isinstance(name, str) or not name.strip():
            return error_response("name 不能为空", status_code=400)
        chat_group = await self.chat_group_store.write(
            self.chat_group_store.create_chat_group,
            name=name,
            member_ids=payload.get("member_ids"),
        )
        return json_response(chat_group)

    async def update_chat_group(self, group_id: str):
        """更新虚拟群聊（只更新传入字段）。"""
        payload = await json_dict()
        if payload is None:
            return error_response("请求体必须是 JSON 对象", status_code=400)
        updated = await self.chat_group_store.write(
            self.chat_group_store.update_chat_group,
            group_id,
            name=payload.get("name"),
            member_ids=payload.get("member_ids"),
        )
        if updated is None:
            return error_response("未找到该虚拟群聊", status_code=404)
        return json_response(updated)

    async def delete_chat_groups(self):
        """删除虚拟群聊。"""
        payload = await json_dict()
        if payload is None:
            return error_response("请求体必须是 JSON 对象", status_code=400)
        ids = _require_ids(payload)
        if ids is None:
            return error_response("ids 不能为空", status_code=400)
        deleted = await self.chat_group_store.write(
            self.chat_group_store.delete_chat_groups, ids
        )
        return json_response({"deleted": deleted})

    # ---------- 群消息流 ----------

    async def session_stream(self, session_id: str):
        """查看虚拟会话的消息流（与 LLM 历史并行的运行时记录）。"""
        if self.group_mgr.find_session(session_id) is None:
            return error_response("未找到该虚拟会话", status_code=404)
        messages = await self.stream_store.read_stream(session_id)
        return json_response({"session_id": session_id, "messages": messages})

    async def clear_stream(self):
        """清空指定会话的消息流。"""
        payload = await json_dict()
        if payload is None:
            return error_response("请求体必须是 JSON 对象", status_code=400)
        ids = _require_ids(payload)
        if ids is None:
            return error_response("ids 不能为空", status_code=400)
        for session_id in ids:
            await self.stream_store.clear(session_id)
        return json_response({"cleared": len(ids)})
