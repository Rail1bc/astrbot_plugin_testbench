"""会话测试台插件。

通过插件页面创建"测试组"——一组共享同一套配置（平台来源/配置档案/发送者
id/昵称）的虚拟会话，组内单个会话可覆盖组配置。测试以组为单位：可并发向组内
（或跨组选中的）多个虚拟会话发送同一条消息，用于测试插件、提示词、模型与
整体稳定性。

消息注入路径：`context.get_event_queue()` -> EventBus -> PipelineScheduler，
与真实平台消息完全一致，回复由 `VirtualMessageEvent` 捕获并回传页面。
"""

from __future__ import annotations

import copy
import json
from typing import Any

from astrbot.api.event import AstrMessageEvent
from astrbot.api.event.filter import on_llm_request, on_waiting_llm_request
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
from .testset_runner import TestsetRunner
from .testset_store import MAX_MESSAGES_PER_TESTSET, TestsetStore
from .virtual_event import VirtualMessageEvent

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
    (
        "/groups/<group_id>/update",
        "update_group",
        ["POST"],
        "更新测试组配置（组配置变更同步应用到仍继承组配置的会话）",
    ),
    ("/sessions", "list_sessions", ["GET"], "列出全部虚拟会话（已解析最终配置）"),
    (
        "/sessions/pending",
        "session_pending",
        ["GET"],
        "查询全部在途/刚完成测试消息的实时状态",
    ),
    (
        "/sessions/update",
        "update_session",
        ["POST"],
        "设置会话自身的配置（覆盖组配置）",
    ),
    ("/sessions/delete", "delete_sessions", ["POST"], "删除虚拟会话"),
    (
        "/sessions/clone",
        "clone_sessions",
        ["POST"],
        "克隆会话：同测试组内新建 N 个会话并拷贝其对话历史",
    ),
    (
        "/sessions/derive",
        "derive_session",
        ["POST"],
        "衍生会话：基于某会话历史创建全新测试组（组内会话历史一致）",
    ),
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
    ("/testsets", "list_testsets", ["GET"], "列出全部测试集"),
    (
        "/testsets",
        "create_testset",
        ["POST"],
        "创建测试集（连续 user 消息序列，可带断言规则）",
    ),
    ("/testsets/delete", "delete_testsets", ["POST"], "删除测试集"),
    (
        "/testsets/<testset_id>/update",
        "update_testset",
        ["POST"],
        "更新测试集（名称与消息序列整体替换）",
    ),
    (
        "/testsets/run",
        "run_testset",
        ["POST"],
        "启动测试集运行（后端后台任务驱动，立即返回 run_id，结果轮询 status）",
    ),
    (
        "/testsets/run/status",
        "testset_run_status",
        ["GET"],
        "查询测试集运行状态（逐步骤进度与逐会话结果）",
    ),
    (
        "/testsets/run/abort",
        "abort_testset_run",
        ["POST"],
        "请求取消测试集运行（当前步骤完成即止）",
    ),
    ("/testsets/runs", "testset_runs", ["GET"], "最近测试集运行摘要列表"),
)


class VirtualSessionPlugin(Star):
    def __init__(self, context: Context) -> None:
        super().__init__(context)
        self.group_mgr = VirtualGroupManager()
        self.runner = VirtualTestRunner(context)
        self.testset_store = TestsetStore()
        self.testset_runner = TestsetRunner(context, self.runner)
        for path, handler, methods, desc in _ROUTES:
            context.register_web_api(
                f"/{PLUGIN_NAME}{path}", getattr(self, handler), methods, desc
            )

    async def list_providers(self):
        """列出可用的对话 LLM Provider 及其模型。

        与 list_platforms 一致采用防御式读取：单个 Provider 的元数据读取失败时
        跳过该 Provider，get_model 失败时降级为 None，不因单个异常拖垮整个接口。
        """
        providers = []
        for prov in self.context.get_all_providers():
            try:
                meta = prov.meta()
            except Exception as e:  # noqa: BLE001
                self.logger.warning(f"读取 Provider 元数据失败: {e}")
                continue
            models: list[str] = []
            try:
                models = await prov.get_models()
            except Exception as e:  # noqa: BLE001
                self.logger.warning(f"获取 Provider {meta.id} 的模型列表失败: {e}")
            try:
                current_model = prov.get_model()
            except Exception as e:  # noqa: BLE001
                self.logger.warning(f"读取 Provider {meta.id} 当前模型失败: {e}")
                current_model = None
            providers.append(
                {
                    "id": (prov.provider_config or {}).get("id") or meta.id,
                    "name": (prov.provider_config or {}).get("name") or meta.type,
                    "type": meta.type,
                    "current_model": current_model,
                    "models": models,
                }
            )
        return json_response(providers)

    async def list_confs(self):
        """列出配置档案（用于测试提示词/系统设定）。

        与 list_platforms 一致采用防御式读取：单个档案对象缺字段时回退默认值，
        不因个别档案结构异常而拖垮整个列表接口。
        """
        confs = []
        for conf in self.context.astrbot_config_mgr.get_conf_list():
            confs.append(
                {
                    "id": conf.get("id") or conf.get("name") or "",
                    "name": conf.get("name") or conf.get("id") or "",
                    "path": conf.get("path"),
                }
            )
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

    async def update_group(self, group_id: str):
        """更新测试组配置；组平台/档案变更会同步应用到仍继承组配置的会话。

        会话已单独覆盖的字段不受组配置变更影响（会话覆盖优先）。平台变更使
        umo 变化：清理旧 umo 的路由，再按新的有效配置同步。
        """
        payload = await request.json(default={})
        group = self.group_mgr.get_group(group_id)
        if group is None:
            return error_response("未找到该测试组", status_code=404)

        updates: dict[str, Any] = {}
        for key in ("name", "platform_id", "conf_id", "sender_id", "sender_name"):
            if key not in payload:
                continue
            value = payload[key]
            updates[key] = value if isinstance(value, str) and value else None

        old_sessions = [self.group_mgr.effective(group, s) for s in group["sessions"]]
        self.group_mgr.update_group(group_id, **updates)
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
            platform_changed = old["platform_id"] != new["platform_id"]
            conf_changed = old["conf_id"] != new["conf_id"]
            if platform_changed:
                await delete_route_if_exists(
                    self.context.astrbot_config_mgr.ucr, umo_of(old)
                )
                await self._delete_session_conversations([old])
            if platform_changed or conf_changed:
                await self._sync_conf_route(new)
        return json_response(updated)

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

        # 平台变更会使 umo 变化：清理旧 umo 的路由与对话历史，再按新 umo 同步
        platform_changed = old_session["platform_id"] != new_session["platform_id"]
        conf_changed = old_session["conf_id"] != new_session["conf_id"]
        if platform_changed:
            await delete_route_if_exists(
                self.context.astrbot_config_mgr.ucr, umo_of(old_session)
            )
            await self._delete_session_conversations([old_session])
        if platform_changed or conf_changed:
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

    async def clone_sessions(self):
        """克隆会话：在源会话所属测试组内新建 count 个会话，并把源会话的
        对话历史拷贝给每个新会话——同一历史起点，可分别改配置/模型测试。"""
        payload = await request.json(default={})
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
        created = self.group_mgr.add_sessions(
            group["id"], count, name_prefix=session.get("name")
        )
        resolved_created = [self.group_mgr.effective(group, s) for s in created]
        conf_id = group.get("conf_id") or None
        if conf_id:
            await self._apply_conf_routes(resolved_created, conf_id)
        copied = await self._copy_history(
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
        payload = await request.json(default={})
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
        new_group = self.group_mgr.create_group(
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
        copied = await self._copy_history(
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
        text = payload.get("text")
        if not isinstance(sessions, list) or not sessions:
            return error_response("sessions 不能为空", status_code=400)
        if not isinstance(text, str):
            return error_response("text 必须是字符串", status_code=400)
        requested = list(dict.fromkeys(sessions))  # 去重，保持顺序
        session_objs = self.group_mgr.effective_many(requested)
        if len(session_objs) != len(requested):
            found = {r["id"] for r in session_objs}
            missing = [sid for sid in requested if sid not in found]
            return error_response(f"未找到指定的虚拟会话: {missing}", status_code=404)

        assertion = payload.get("assertion")
        if assertion is not None and not isinstance(assertion, dict):
            return error_response("assertion 必须是对象", status_code=400)
        try:
            test_id = await self.runner.start(
                sessions=session_objs,
                text=text,
                provider_id=payload.get("provider_id"),
                model=payload.get("model"),
                conf_id=payload.get("conf_id"),
                assertion=assertion,
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

    # ---------- 在途消息状态（LLM 阶段 hook） ----------

    async def session_pending(self):
        """查询全部在途（未完成或刚完成）测试消息的实时状态。

        前端面板据此显示每条消息「已入队 / 排队等待 LLM / LLM 生成中 /
        完成」；中间两个状态由下面的 hook 推进。
        """
        return json_response({"pending": self.runner.pending_entries()})

    @on_waiting_llm_request()
    async def on_waiting_llm(self, event: AstrMessageEvent) -> None:
        """消息已到达 LLM 阶段、正在等待会话锁（「重复追问」排队等待时触发）。"""
        if isinstance(event, VirtualMessageEvent) and event.entry_id:
            self.runner.mark_waiting_llm(event.entry_id)

    @on_llm_request()
    async def on_llm(self, event: AstrMessageEvent, req) -> None:
        """消息正在调用 LLM（会话锁内、流式/非流式分叉之前触发）。"""
        if isinstance(event, VirtualMessageEvent) and event.entry_id:
            self.runner.mark_llm(event.entry_id)

    # ---------- 测试集 ----------

    @staticmethod
    def _validate_messages(messages: Any) -> list[dict] | None:
        """校验并清洗测试集消息；无效返回 None（调用方转 400）。

        messages 必须为 list（可空——先建命名条目、再在窗口里加消息）；
        每条已含消息必须含非空字符串 text，rule 必须为 dict 或 null。
        """
        if not isinstance(messages, list):
            return None
        out: list[dict] = []
        for item in messages:
            if not isinstance(item, dict):
                return None
            text = item.get("text")
            rule = item.get("rule")
            if not isinstance(text, str) or not text.strip():
                return None
            if rule is not None and not isinstance(rule, dict):
                return None
            out.append({"text": text, "rule": rule})
        return out

    @staticmethod
    def _validate_batch_ranges(
        batch_ranges: Any, message_count: int
    ) -> list[list[int]] | None:
        """严格校验批量发送范围；非法返回 None（调用方转 400）。

        非 list、项非两个整数、越界、s>e、互相重叠都拒绝；合法返回规范化
        列表（按 start 升序）。handler 校验的 messages 逐条非空 ⇒ 存储层不丢
        消息 ⇒ 索引稳定，此处校验与存储层规范化结果一致。
        """
        if not isinstance(batch_ranges, list):
            return None
        kept: list[list[int]] = []
        for item in batch_ranges:
            if (
                not isinstance(item, list)
                or len(item) != 2
                or isinstance(item[0], bool)
                or isinstance(item[1], bool)
                or not isinstance(item[0], int)
                or not isinstance(item[1], int)
            ):
                return None
            start, end = item
            if not (0 <= start <= end < message_count):
                return None
            if any(not (end < s or e < start) for s, e in kept):
                return None
            kept.append([start, end])
        kept.sort(key=lambda r: r[0])
        return kept

    async def list_testsets(self):
        """列出全部测试集。"""
        return json_response({"testsets": self.testset_store.list_testsets()})

    async def create_testset(self):
        """创建测试集（名称 + 连续 user 消息序列，消息可带回复断言规则）。"""
        payload = await request.json(default={})
        messages = self._validate_messages(payload.get("messages"))
        if messages is None:
            return error_response("messages 必须是消息数组", status_code=400)
        if len(messages) > MAX_MESSAGES_PER_TESTSET:
            return error_response(
                f"messages 数量不能超过 {MAX_MESSAGES_PER_TESTSET}",
                status_code=400,
            )
        batch_ranges = self._validate_batch_ranges(
            payload.get("batch_ranges") or [], len(messages)
        )
        if batch_ranges is None:
            return error_response("batch_ranges 格式无效", status_code=400)
        testset = self.testset_store.create_testset(
            name=payload.get("name"),
            messages=messages,
            batch_ranges=batch_ranges,
        )
        return json_response(testset)

    async def update_testset(self, testset_id: str):
        """更新测试集（名称、消息序列与批量发送范围整体替换）。"""
        payload = await request.json(default={})
        messages = self._validate_messages(payload.get("messages"))
        if messages is None:
            return error_response("messages 必须是消息数组", status_code=400)
        if len(messages) > MAX_MESSAGES_PER_TESTSET:
            return error_response(
                f"messages 数量不能超过 {MAX_MESSAGES_PER_TESTSET}",
                status_code=400,
            )
        batch_ranges = self._validate_batch_ranges(
            payload.get("batch_ranges") or [], len(messages)
        )
        if batch_ranges is None:
            return error_response("batch_ranges 格式无效", status_code=400)
        testset = self.testset_store.update_testset(
            testset_id,
            name=payload.get("name"),
            messages=messages,
            batch_ranges=batch_ranges,
        )
        if testset is None:
            return error_response("未找到该测试集", status_code=404)
        return json_response(testset)

    async def delete_testsets(self):
        """删除测试集。"""
        payload = await request.json(default={})
        ids = payload.get("ids")
        if not isinstance(ids, list) or not ids:
            return error_response("ids 不能为空", status_code=400)
        deleted = self.testset_store.delete_testsets(ids)
        return json_response({"deleted": deleted})

    async def run_testset(self):
        """启动测试集运行（后端后台任务驱动，立即返回 run_id，结果轮询 status）。

        测试集运行是耗时操作、可能与页面生命周期解耦：发送节奏由测试集内的
        批量发送范围决定（段内重叠、段外逐条），后台任务按段驱动，离开页面
        不影响执行；运行记录可经 ``/testsets/run/status`` 轮询、
        ``/testsets/runs`` 找回、``abort`` 取消。
        """
        payload = await request.json(default={})
        testset_id = payload.get("testset_id")
        if not isinstance(testset_id, str) or not testset_id:
            return error_response("testset_id 不能为空", status_code=400)
        testset = self.testset_store.get_testset(testset_id)
        if testset is None:
            return error_response("未找到该测试集", status_code=404)
        if not testset.get("messages"):
            return error_response("该测试集没有消息", status_code=400)
        sessions = payload.get("sessions")
        if not isinstance(sessions, list) or not sessions:
            return error_response("sessions 不能为空", status_code=400)
        requested = list(dict.fromkeys(sessions))  # 去重，保持顺序
        session_objs = self.group_mgr.effective_many(requested)
        if len(session_objs) != len(requested):
            found = {s["id"] for s in session_objs}
            missing = [sid for sid in requested if sid not in found]
            return error_response(f"未找到指定的虚拟会话: {missing}", status_code=404)
        run_id = self.testset_runner.start_run(testset, session_objs)
        return json_response({"run_id": run_id, "steps": len(testset["messages"])})

    async def testset_run_status(self):
        """查询测试集运行状态（逐步骤进度与逐会话结果）。"""
        run_id = request.query.get("run_id")
        if not run_id:
            return error_response("run_id 不能为空", status_code=400)
        record = self.testset_runner.status(run_id)
        if record is None:
            return error_response("未找到该测试集运行", status_code=404)
        return json_response(record)

    async def abort_testset_run(self):
        """请求取消测试集运行：当前步骤照常完成并收结果，后续步骤不再发。"""
        payload = await request.json(default={})
        run_id = payload.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            return error_response("run_id 不能为空", status_code=400)
        cancelled = self.testset_runner.abort(run_id)
        return json_response({"cancelled": cancelled})

    async def testset_runs(self):
        """最近测试集运行摘要列表（页面重开后找回运行结果）。"""
        return json_response({"runs": self.testset_runner.list_runs()})

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

    async def _copy_history(self, source: dict, targets: list[dict]) -> int:
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
