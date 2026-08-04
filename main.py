"""虚拟会话测试平台插件。

通过插件页面创建与真实会话走完全相同处理路径的虚拟会话，可并发向多个虚拟会话
发送同一条消息，用于测试插件、提示词、模型与整体稳定性。

消息注入路径：`context.get_event_queue()` -> EventBus -> PipelineScheduler，
与真实平台消息完全一致，回复由 `VirtualMessageEvent` 捕获并回传页面。
"""

from __future__ import annotations

from astrbot.api.star import Context, Star
from astrbot.api.web import error_response, json_response, request

from .runner import VirtualSessionManager, VirtualTestRunner, umo_of

PLUGIN_NAME = "astrbot_plugin_virtual_session"

MAX_SESSIONS_PER_BATCH = 500


class VirtualSessionPlugin(Star):
    def __init__(self, context: Context) -> None:
        super().__init__(context)
        self.session_mgr = VirtualSessionManager()
        self.runner = VirtualTestRunner(context)

        context.register_web_api(
            f"/{PLUGIN_NAME}/providers",
            self.list_providers,
            ["GET"],
            "列出可用的 LLM Provider 与模型",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/confs",
            self.list_confs,
            ["GET"],
            "列出配置档案",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/platforms",
            self.list_platforms,
            ["GET"],
            "列出已启用的平台适配器",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/sessions",
            self.list_sessions,
            ["GET"],
            "列出虚拟会话",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/sessions",
            self.create_sessions,
            ["POST"],
            "批量创建虚拟会话",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/sessions/delete",
            self.delete_sessions,
            ["POST"],
            "删除虚拟会话",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/reset",
            self.reset_sessions,
            ["POST"],
            "重置虚拟会话的对话历史",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/test/run",
            self.run_test,
            ["POST"],
            "并发向多个虚拟会话发送消息并汇总结果",
        )

    async def list_providers(self):
        """列出可用的对话 LLM Provider 及其模型。"""
        providers = []
        for prov in self.context.get_all_providers():
            models: list[str] = []
            try:
                models = await prov.get_models()
            except Exception as e:  # noqa: BLE001
                self.logger.warning(
                    f"获取 Provider {prov.meta().id} 的模型列表失败: {e}"
                )
            meta = prov.meta()
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
        """列出已启用的平台适配器（虚拟会话可模拟其平台上下文）。"""
        platforms = []
        for inst in self.context.platform_manager.platform_insts:
            meta = inst.meta()
            platforms.append(
                {
                    "id": meta.id,
                    "name": meta.name,
                    "display_name": meta.adapter_display_name or meta.name,
                }
            )
        return json_response(platforms)

    async def list_sessions(self):
        """列出已保存的虚拟会话。"""
        return json_response(self.session_mgr.list())

    async def create_sessions(self):
        """批量创建虚拟会话。"""
        payload = await request.json(default={})
        count = payload.get("count", 1)
        if not isinstance(count, int) or isinstance(count, bool):
            return error_response("count 必须是整数", status_code=400)
        if count < 1 or count > MAX_SESSIONS_PER_BATCH:
            return error_response(
                f"count 必须在 1-{MAX_SESSIONS_PER_BATCH} 之间",
                status_code=400,
            )
        created = self.session_mgr.create_many(
            count=count,
            platform_id=payload.get("platform_id"),
            sender_id=payload.get("sender_id"),
            sender_name=payload.get("sender_name"),
            name_prefix=payload.get("name_prefix"),
        )
        return json_response(created)

    async def delete_sessions(self):
        """删除虚拟会话。"""
        payload = await request.json(default={})
        ids = payload.get("ids")
        if not isinstance(ids, list) or not ids:
            return error_response("ids 不能为空", status_code=400)
        deleted = self.session_mgr.delete(ids)
        return json_response({"deleted": deleted})

    async def reset_sessions(self):
        """重置虚拟会话的对话历史（删除该 umo 下的全部对话）。"""
        payload = await request.json(default={})
        ids = payload.get("ids")
        if not isinstance(ids, list) or not ids:
            return error_response("ids 不能为空", status_code=400)
        sessions = self.session_mgr.get_many(ids)
        conv_mgr = self.context.conversation_manager
        reset = 0
        for session in sessions:
            try:
                await conv_mgr.delete_conversations_by_user_id(umo_of(session))
                reset += 1
            except Exception as e:  # noqa: BLE001
                self.logger.warning(f"重置会话 {session['id']} 的对话历史失败: {e}")
        return json_response({"reset": reset})

    async def run_test(self):
        """并发向多个虚拟会话发送同一条消息并汇总结果。"""
        payload = await request.json(default={})
        sessions = payload.get("sessions")
        text = payload.get("text", "")
        if not isinstance(sessions, list) or not sessions:
            return error_response("sessions 不能为空", status_code=400)
        session_objs = self.session_mgr.get_many(sessions)
        if not session_objs:
            return error_response("未找到指定的虚拟会话", status_code=404)

        try:
            timeout = float(payload.get("timeout", 120) or 120)
            batch_size = int(payload.get("batch_size", 10) or 10)
            batch_interval = float(payload.get("batch_interval", 0) or 0)
        except (TypeError, ValueError):
            return error_response(
                "timeout/batch_size/batch_interval 参数不合法", status_code=400
            )
        if timeout <= 0:
            return error_response("timeout 必须大于 0", status_code=400)
        if batch_size < 1:
            return error_response("batch_size 必须大于等于 1", status_code=400)

        try:
            result = await self.runner.run(
                sessions=session_objs,
                text=str(text),
                provider_id=payload.get("provider_id"),
                model=payload.get("model"),
                conf_id=payload.get("conf_id"),
                timeout=timeout,
                batch_size=batch_size,
                batch_interval=batch_interval,
            )
        except ValueError as e:
            return error_response(str(e), status_code=400)
        except Exception as e:  # noqa: BLE001
            self.logger.error("并发测试运行失败", exc_info=True)
            return error_response(f"并发测试运行失败: {e}", status_code=500)
        return json_response(result)
