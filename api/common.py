"""Web API 层共享常量与辅助 mixin（供 groups / sessions 两个 handler 集合复用）。"""

from __future__ import annotations

from ..core.conf_routes import apply_routes, clear_routes, sync_route

# 单测试组会话数上限（与 store/testset_store 的 MAX_MESSAGES_PER_TESTSET 同风格安全阀）
MAX_SESSIONS_PER_GROUP = 500


class ConfRouteMixin:
    """UCR 配置档案路由的薄包装（_apply / _clear / _sync 三个动作）。

    由 GroupsAPI / SessionsAPI 复用：handler 内部直接调用，避免在两组 handler
    里各自实现对 UCR API 的调用。
    """

    async def _apply_conf_routes(self, sessions: list[dict], conf_id: str) -> None:
        """把每个会话路由到指定配置档案（精确到 umo，不互相影响）。"""
        await apply_routes(self.context.astrbot_config_mgr.ucr, sessions, conf_id)

    async def _clear_conf_routes(self, sessions: list[dict]) -> None:
        """删除会话对应的配置档案路由。"""
        await clear_routes(self.context.astrbot_config_mgr.ucr, sessions)

    async def _sync_conf_route(self, session: dict) -> None:
        """按会话的有效配置档案同步 UCR 路由（无绑定则确保路由不存在）。"""
        await sync_route(self.context.astrbot_config_mgr.ucr, session)
