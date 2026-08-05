"""会话对话历史操作：整体替换保存 / 按轮重新生成 / 复制与级联删除。

从 main.py 拆分：save_history / regenerate_history 两个 Web API handler 及
其辅助（_get_session_history / _msg_text / _copy_history /
_delete_session_conversations）原先都堆在 VirtualSessionPlugin 上，全部围绕
conversation_manager 的读写。HistoryOps 收敛这些操作；main.py 只保留路由装配
与薄委托（_ROUTES 的 getattr 要求 handler 名字仍在 Star 上）。
"""

from __future__ import annotations

import copy
import json
from typing import Any

from astrbot.api.web import error_response, json_response, request

from .group_store import umo_of


class HistoryOps:
    """会话对话历史操作集：save / regenerate 两个 handler + 复制 / 级联删除。"""

    def __init__(self, context, get_group_mgr, runner, logger) -> None:
        self.context = context
        # group_mgr 经 getter 延迟取：调用方（main.py / 测试）可能重新绑定
        # plugin.group_mgr（测试用 tmp 目录替换持久化路径），构造时捕获快照
        # 会让本类一直指向旧的（空的）管理器，找不到任何会话。
        self._get_group_mgr = get_group_mgr
        self.runner = runner
        self.logger = logger

    @property
    def group_mgr(self):
        return self._get_group_mgr()

    async def save_history(self) -> Any:
        """整体替换会话的对话历史（/sessions/history/save handler 的完整逻辑）。

        接受 ``{id, conversations: [...]}``，其中每个对话为
        ``{conversation_id?, title?, history: [...]}``：

        - 带已存在 ``conversation_id`` 的对话更新其内容（历史与标题）；
        - 不带 ``conversation_id`` 的对象新建对话；
        - 带不存在的 ``conversation_id``（会话从未产生对话，或历史被重置/
          删除后编辑器仍是旧 JSON）同样新建对话——按整体替换语义落盘，
          而不是报错导致保存失败；
        - 数据库中未被列出的对话将被删除。

        语义与原生对话管理的一致：编辑器展示完整结构，保存即整体替换，
        编辑、新增、删除对话都通过直接修改 JSON 完成。
        """
        payload = await request.json(default={})
        session_id = payload.get("id")
        conversations = payload.get("conversations")
        if not isinstance(session_id, str) or not session_id:
            return error_response("id 不能为空", status_code=400)
        if not isinstance(conversations, list):
            return error_response("conversations 必须是数组", status_code=400)
        found = self.group_mgr.find_session(session_id)
        if found is None:
            return error_response("未找到该虚拟会话", status_code=404)
        group, session = found
        resolved = self.group_mgr.effective(group, session)
        umo = umo_of(resolved)
        conv_mgr = self.context.conversation_manager

        normalized: list[dict] = []
        for item in conversations:
            if not isinstance(item, dict):
                return error_response(
                    "conversations 中的每一项必须是对象", status_code=400
                )
            history = item.get("history")
            if not isinstance(history, list) or not all(
                isinstance(msg, dict) for msg in history
            ):
                return error_response(
                    "每个对话的 history 必须是对象数组", status_code=400
                )
            cid = item.get("conversation_id")
            title = item.get("title")
            normalized.append(
                {
                    "cid": cid if isinstance(cid, str) and cid else None,
                    "title": title if isinstance(title, str) else None,
                    "history": history,
                }
            )

        existing = await conv_mgr.get_conversations(umo)
        existing_cids = {conv.cid for conv in existing}
        # 失效 cid 首次出现时新建占位对话（新生成 id），记录原 cid → 新 cid；
        # 同一失效 cid 再次出现时更新首个占位对话，避免同一引用重复新建对话。
        placeholder_map: dict[str, str] = {}

        for item in normalized:
            if item["cid"]:
                if item["cid"] in existing_cids:
                    await conv_mgr.update_conversation(
                        umo, item["cid"], history=item["history"], title=item["title"]
                    )
                elif item["cid"] in placeholder_map:
                    await conv_mgr.update_conversation(
                        umo,
                        placeholder_map[item["cid"]],
                        history=item["history"],
                        title=item["title"],
                    )
                else:
                    # 引用的 conversation_id 在库中不存在：会话从未产生过对话，
                    # 或历史被重置/删除后编辑器里仍是旧 JSON。按整体替换语义
                    # 新建占位对话（新生成 id），而不是报错导致保存失败。
                    new_cid = await conv_mgr.new_conversation(
                        umo, content=item["history"], title=item["title"]
                    )
                    placeholder_map[item["cid"]] = new_cid
            else:
                await conv_mgr.new_conversation(
                    umo, content=item["history"], title=item["title"]
                )

        keep = {item["cid"] for item in normalized if item["cid"]}
        for conv in existing:
            if conv.cid not in keep:
                await conv_mgr.delete_conversation(umo, conv.cid)

        return json_response({"saved": len(normalized)})

    async def regenerate_history(self) -> Any:
        """重新生成指定轮次：截断该轮（含）之后的历史，重发该轮的 user 消息。"""
        payload = await request.json(default={})
        session_id = payload.get("id")
        index = payload.get("index")
        conversation_id = payload.get("conversation_id")
        if not isinstance(session_id, str) or not session_id:
            return error_response("id 不能为空", status_code=400)
        if not isinstance(index, int) or isinstance(index, bool) or index < 0:
            return error_response("index 必须是非负整数", status_code=400)
        if conversation_id is not None and not isinstance(conversation_id, str):
            return error_response("conversation_id 必须为字符串", status_code=400)
        found = self.group_mgr.find_session(session_id)
        if found is None:
            return error_response("未找到该虚拟会话", status_code=404)
        group, session = found
        resolved = self.group_mgr.effective(group, session)
        hist_info = await self._get_session_history(resolved, conversation_id)
        if hist_info is None:
            return error_response("该会话暂无对话历史", status_code=404)
        history, cid = hist_info
        if index >= len(history):
            return error_response(
                f"index 越界（历史共 {len(history)} 条）", status_code=400
            )
        turn_start = index
        while turn_start >= 0 and history[turn_start].get("role") != "user":
            turn_start -= 1
        if turn_start < 0:
            return error_response(
                "该消息之前没有用户发言，无法定位轮次", status_code=400
            )
        user_text = self._msg_text(history[turn_start])
        if not user_text.strip():
            return error_response("该轮用户消息为空，无法重新生成", status_code=400)
        new_history = history[:turn_start]
        await self.context.conversation_manager.update_conversation(
            umo_of(resolved), cid, history=new_history
        )
        try:
            test_id = await self.runner.start(sessions=[resolved], text=user_text)
        except ValueError as e:
            return error_response(str(e), status_code=400)
        except Exception as e:  # noqa: BLE001
            self.logger.error("重新生成启动失败", exc_info=True)
            return error_response(f"重新生成启动失败: {e}", status_code=500)
        return json_response({"test_id": test_id, "total": 1})

    async def _get_session_history(
        self, session: dict, conversation_id: str | None = None
    ) -> tuple[list[dict], str] | None:
        """返回 (history, conversation_id)；该会话无对话历史时返回 None。

        conversation_id 指定时只在该对话内定位（重新生成按对话定位），否则用
        当前对话——多对话历史下全局索引相对当前对话是错的，必须按对话取值。
        """
        conv_mgr = self.context.conversation_manager
        umo = umo_of(session)
        if conversation_id:
            cid = conversation_id
        else:
            cid = await conv_mgr.get_curr_conversation_id(umo)
            if not cid:
                return None
        conv = await conv_mgr.get_conversation(umo, cid)
        if not conv:
            return None
        try:
            history = json.loads(conv.history)
        except json.JSONDecodeError:
            history = []
        return history, cid

    @staticmethod
    def _msg_text(msg: dict) -> str:
        """提取消息的纯文本（content 可为字符串或 parts 数组）。"""
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, str):
                    parts.append(part)
                elif isinstance(part, dict):
                    parts.append(str(part.get("text") or part.get("content") or ""))
            return "\n".join(p for p in parts if p)
        return ""

    async def copy_history(self, source: dict, targets: list[dict]) -> int:
        """把 source 会话的全部对话历史深拷贝到每个 target 会话。

        目标会话的对话按内容整体新建，conversation_id 由系统重新生成——克隆/
        衍生的会话是独立实体，不沿用源会话的对话 id。返回新建对话总数。
        """
        conv_mgr = self.context.conversation_manager
        snapshot: list[tuple[str | None, list[dict]]] = []
        for conv in await conv_mgr.get_conversations(umo_of(source)):
            try:
                history = json.loads(conv.history) if conv.history else []
            except json.JSONDecodeError:
                history = []
            snapshot.append((conv.title, history))
        copied = 0
        for target in targets:
            for title, history in snapshot:
                await conv_mgr.new_conversation(
                    umo_of(target),
                    content=copy.deepcopy(history),
                    title=title,
                )
                copied += 1
        return copied

    async def delete_session_conversations(self, sessions: list[dict]) -> int:
        """级联删除虚拟会话在 AstrBot 原生的对话历史（按 umo），返回成功数。"""
        conv_mgr = self.context.conversation_manager
        deleted = 0
        for session in sessions:
            try:
                await conv_mgr.delete_conversations_by_user_id(umo_of(session))
                deleted += 1
            except Exception as e:  # noqa: BLE001
                self.logger.warning(f"删除会话 {session['id']} 的对话历史失败: {e}")
        return deleted
