"""会话测试台插件。

通过插件页面创建"测试组"——一组共享同一套配置（平台来源/配置档案/发送者
id/昵称）的虚拟会话，组内单个会话可覆盖组配置。测试以组为单位：可并发向组内
（或跨组选中的）多个虚拟会话发送同一条消息，用于测试插件、提示词、模型与
整体稳定性。

消息注入路径：`context.get_event_queue()` -> EventBus -> PipelineScheduler，
与真实平台消息完全一致，回复由 `VirtualMessageEvent` 捕获并回传页面。
"""

from __future__ import annotations

import json
from typing import Any

from astrbot.api.star import Context, Star
from astrbot.api.web import error_response, json_response, request

from .conf_routes import (
    apply_routes,
    clear_routes,
    delete_route_if_exists,
    sync_route,
)
from .group_store import VirtualGroupManager, umo_of
from .runner import VirtualTestRunner

PLUGIN_NAME = "astrbot_plugin_testbench"

MAX_SESSIONS_PER_GROUP = 500

# 全部 Web API 端点集中于此：一处看全，新增端点只需加一行。
# handler 用方法名字符串 + getattr 解析，构造期即校验拼写（拼错直接抛 AttributeError）。
_ROUTES: tuple[tuple[str, str, list[str], str], ...] = (
    ("/providers", "list_providers", ["GET"], "列出可用的 LLM Provider 与模型"),
    ("/confs", "list_confs", ["GET"], "列出配置档案"),
    ("/platforms", "list_platforms", ["GET"], "列出已启用的平台适配器"),
    ("/groups", "list_groups", ["GET"], "列出测试组（含组内会话）"),
    ("/groups", "create_group", ["POST"], "创建测试组并生成组内虚拟会话"),
    ("/groups/delete", "delete_groups", ["POST"], "删除测试组"),
    (
        "/groups/<group_id>/sessions",
        "add_group_sessions",
        ["POST"],
        "向测试组内新增虚拟会话",
    ),
    ("/sessions", "list_sessions", ["GET"], "列出全部虚拟会话（已解析最终配置）"),
    (
        "/sessions/update",
        "update_session",
        ["POST"],
        "设置会话自身的配置（覆盖组配置）",
    ),
    ("/sessions/delete", "delete_sessions", ["POST"], "删除虚拟会话"),
    (
        "/sessions/<session_id>/history",
        "session_history",
        ["GET"],
        "查看虚拟会话的对话历史",
    ),
    ("/reset", "reset_sessions", ["POST"], "重置虚拟会话的对话历史"),
    (
        "/test/run",
        "run_test",
        ["POST"],
        "向多个虚拟会话投递消息（立即返回 test_id，结果轮询 status 接口）",
    ),
    (
        "/test/run/status",
        "test_run_status",
        ["GET"],
        "查询测试运行状态（已完成的会话逐个返回结果）",
    ),
    (
        "/sessions/history/save",
        "save_history",
        ["POST"],
        "整体替换会话的对话历史（JSON 编辑器保存；未列出的对话将被删除）",
    ),
    (
        "/sessions/history/regenerate",
        "regenerate_history",
        ["POST"],
        "重新生成指定轮次（截断该轮之后的历史并重发该轮用户消息）",
    ),
)


class VirtualSessionPlugin(Star):
    def __init__(self, context: Context) -> None:
        super().__init__(context)
        self.group_mgr = VirtualGroupManager()
        self.runner = VirtualTestRunner(context)
        for path, handler, methods, desc in _ROUTES:
            context.register_web_api(
                f"/{PLUGIN_NAME}{path}", getattr(self, handler), methods, desc
            )

    async def list_providers(self):
        """列出可用的对话 LLM Provider 及其模型。"""
        providers = []
        for prov in self.context.get_all_providers():
            meta = prov.meta()
            models: list[str] = []
            try:
                models = await prov.get_models()
            except Exception as e:  # noqa: BLE001
                self.logger.warning(f"获取 Provider {meta.id} 的模型列表失败: {e}")
            providers.append(
                {
                    "id": (prov.provider_config or {}).get("id") or meta.id,
                    "name": (prov.provider_config or {}).get("name") or meta.type,
                    "type": meta.type,
                    "current_model": prov.get_model(),
                    "models": models,
                }
            )
        return json_response(providers)

    async def list_confs(self):
        """列出配置档案（用于测试提示词/系统设定）。"""
        confs = []
        for conf in self.context.astrbot_config_mgr.get_conf_list():
            confs.append({"id": conf["id"], "name": conf["name"], "path": conf["path"]})
        return json_response(confs)

    async def list_platforms(self):
        """列出已启用的平台适配器（虚拟会话可模拟其平台上下文）。

        单个适配器元数据读取失败时跳过该适配器，保证单个异常不会导致整个
        列表接口失败（前端下拉框因此为空）。
        """
        platforms = []
        manager = getattr(self.context, "platform_manager", None)
        insts = getattr(manager, "platform_insts", None) if manager else None
        if not insts:
            return json_response(platforms)
        for inst in insts:
            try:
                meta = inst.meta()
                platform_id = getattr(meta, "id", None)
                if not platform_id:
                    continue
                name = getattr(meta, "name", None)
                platforms.append(
                    {
                        "id": platform_id,
                        "name": name,
                        "display_name": getattr(meta, "adapter_display_name", None)
                        or name,
                    }
                )
            except Exception as e:  # noqa: BLE001
                self.logger.warning(f"读取平台适配器元数据失败: {e}")
        return json_response(platforms)

    # ---------- 测试组 ----------

    async def list_groups(self):
        """列出全部测试组（含组内会话，会话字段为原始覆盖值）。"""
        return json_response({"groups": self.group_mgr.list_groups()})

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
        group = self.group_mgr.create_group(
            name=payload.get("name"),
            count=count,
            platform_id=payload.get("platform_id"),
            conf_id=conf_id,
            sender_id=payload.get("sender_id"),
            sender_name=payload.get("sender_name"),
            name_prefix=payload.get("name_prefix"),
        )
        if conf_id:
            sessions = [self.group_mgr.effective(group, s) for s in group["sessions"]]
            await self._apply_conf_routes(sessions, conf_id)
        return json_response(group)

    async def delete_groups(self):
        """删除测试组，并联动清理组内会话的配置档案路由与原生对话历史。"""
        payload = await request.json(default={})
        ids = payload.get("ids")
        if not isinstance(ids, list) or not ids:
            return error_response("ids 不能为空", status_code=400)
        removed = self.group_mgr.delete_groups(ids)
        sessions = [self.group_mgr.effective(group, s) for group, s in removed]
        await self._clear_conf_routes(sessions)
        await self._delete_session_conversations(sessions)
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
        created = self.group_mgr.add_sessions(
            group_id,
            count,
            payload.get("name_prefix"),
        )
        conf_id = group.get("conf_id") or None
        if conf_id:
            sessions = [self.group_mgr.effective(group, s) for s in created]
            await self._apply_conf_routes(sessions, conf_id)
        return json_response(created)

    # ---------- 会话 ----------

    async def list_sessions(self):
        """列出全部虚拟会话（已解析最终配置，附组信息）。"""
        return json_response(self.group_mgr.flat_sessions())

    async def update_session(self):
        """设置会话自身的配置覆盖；传 null 恢复继承组配置，conf_id 传空串显式使用默认档案。"""
        payload = await request.json(default={})
        session_id = payload.get("id")
        if not isinstance(session_id, str) or not session_id:
            return error_response("id 不能为空", status_code=400)
        found = self.group_mgr.find_session(session_id)
        if found is None:
            return error_response("未找到该虚拟会话", status_code=404)
        group, session = found
        old_session = self.group_mgr.effective(group, session)

        overrides: dict[str, Any] = {}
        for key in ("platform_id", "conf_id", "sender_id", "sender_name"):
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

        self.group_mgr.update_session(session_id, **overrides)
        new_session = self.group_mgr.effective(group, session)

        # 平台变更会使 umo 变化：清理旧 umo 的路由，再按新 umo 同步
        if old_session["platform_id"] != new_session["platform_id"]:
            await delete_route_if_exists(
                self.context.astrbot_config_mgr.ucr, umo_of(old_session)
            )
        await self._sync_conf_route(new_session)
        return json_response(new_session)

    async def delete_sessions(self):
        """删除虚拟会话，并联动清理其配置档案路由与原生对话历史。"""
        payload = await request.json(default={})
        ids = payload.get("ids")
        if not isinstance(ids, list) or not ids:
            return error_response("ids 不能为空", status_code=400)
        removed = self.group_mgr.delete_sessions(ids)
        sessions = [self.group_mgr.effective(group, s) for group, s in removed]
        await self._clear_conf_routes(sessions)
        await self._delete_session_conversations(sessions)
        return json_response({"deleted": len(sessions)})

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
        """重置虚拟会话的对话历史（删除该 umo 下的全部对话）。"""
        payload = await request.json(default={})
        ids = payload.get("ids")
        if not isinstance(ids, list) or not ids:
            return error_response("ids 不能为空", status_code=400)
        sessions = self.group_mgr.effective_many(ids)
        reset = await self._delete_session_conversations(sessions)
        return json_response({"reset": reset})

    # ---------- 测试 ----------

    async def run_test(self):
        """向多个虚拟会话投递同一条消息，立即返回 test_id（结果轮询 status 接口）。

        与真实平台一致：不设总超时、不分批投递，完全由 AstrBot 原生 pipeline 处理。
        """
        payload = await request.json(default={})
        sessions = payload.get("sessions")
        text = payload.get("text", "")
        if not isinstance(sessions, list) or not sessions:
            return error_response("sessions 不能为空", status_code=400)
        requested = list(dict.fromkeys(sessions))  # 去重，保持顺序
        session_objs = self.group_mgr.effective_many(requested)
        if len(session_objs) != len(requested):
            found = {r["id"] for r in session_objs}
            missing = [sid for sid in requested if sid not in found]
            return error_response(f"未找到指定的虚拟会话: {missing}", status_code=404)

        try:
            test_id = await self.runner.start(
                sessions=session_objs,
                text=str(text),
                provider_id=payload.get("provider_id"),
                model=payload.get("model"),
                conf_id=payload.get("conf_id"),
            )
        except ValueError as e:
            return error_response(str(e), status_code=400)
        except Exception as e:  # noqa: BLE001
            self.logger.error("并发测试启动失败", exc_info=True)
            return error_response(f"并发测试启动失败: {e}", status_code=500)
        return json_response({"test_id": test_id, "total": len(session_objs)})

    async def test_run_status(self):
        """查询测试运行状态（已完成会话的结果与统计）。"""
        test_id = request.query.get("test_id")
        if not test_id:
            return error_response("test_id 不能为空", status_code=400)
        record = self.runner.status(test_id)
        if record is None:
            return error_response("未找到该测试运行", status_code=404)
        return json_response(record)

    async def save_history(self):
        """整体替换会话的对话历史（JSON 编辑器保存）。

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

        for item in normalized:
            if item["cid"]:
                if item["cid"] in existing_cids:
                    await conv_mgr.update_conversation(
                        umo, item["cid"], history=item["history"], title=item["title"]
                    )
                else:
                    # 引用的 conversation_id 在库中不存在：会话从未产生过对话，
                    # 或历史被重置/删除后编辑器里仍是旧 JSON。按整体替换语义
                    # 新建占位对话（新生成 id），而不是报错导致保存失败。
                    await conv_mgr.new_conversation(
                        umo, content=item["history"], title=item["title"]
                    )
            else:
                await conv_mgr.new_conversation(
                    umo, content=item["history"], title=item["title"]
                )

        keep = {item["cid"] for item in normalized if item["cid"]}
        for conv in existing:
            if conv.cid not in keep:
                await conv_mgr.delete_conversation(umo, conv.cid)

        return json_response({"saved": len(normalized)})

    async def regenerate_history(self):
        """重新生成指定轮次：截断该轮（含）之后的历史，重发该轮的 user 消息。"""
        payload = await request.json(default={})
        session_id = payload.get("id")
        index = payload.get("index")
        if not isinstance(session_id, str) or not session_id:
            return error_response("id 不能为空", status_code=400)
        if not isinstance(index, int) or isinstance(index, bool) or index < 0:
            return error_response("index 必须是非负整数", status_code=400)
        found = self.group_mgr.find_session(session_id)
        if found is None:
            return error_response("未找到该虚拟会话", status_code=404)
        group, session = found
        resolved = self.group_mgr.effective(group, session)
        hist_info = await self._get_session_history(resolved)
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

    # ---------- 会话历史辅助 ----------

    async def _get_session_history(
        self, session: dict
    ) -> tuple[list[dict], str] | None:
        """返回 (history, conversation_id)；该会话无对话历史时返回 None。"""
        conv_mgr = self.context.conversation_manager
        umo = umo_of(session)
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

    async def _delete_session_conversations(self, sessions: list[dict]) -> int:
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

    # ---------- UCR 路由辅助 ----------

    async def _apply_conf_routes(self, sessions: list[dict], conf_id: str) -> None:
        """把每个会话路由到指定配置档案（精确到 umo，不互相影响）。"""
        await apply_routes(self.context.astrbot_config_mgr.ucr, sessions, conf_id)

    async def _clear_conf_routes(self, sessions: list[dict]) -> None:
        """删除会话对应的配置档案路由。"""
        await clear_routes(self.context.astrbot_config_mgr.ucr, sessions)

    async def _sync_conf_route(self, session: dict) -> None:
        """按会话的有效配置档案同步 UCR 路由（无绑定则确保路由不存在）。"""
        await sync_route(self.context.astrbot_config_mgr.ucr, session)
