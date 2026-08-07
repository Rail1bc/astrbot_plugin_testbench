"""测试运行接口：单条消息并发投递、状态查询与在途消息。"""

from __future__ import annotations

from astrbot.api.web import error_response, json_response, request

from ..core.cron_probe import collect_cron_warnings, target_sets
from .common import json_dict


class RunsAPI:
    """手动群发（/test/run）与其状态/在途查询（LLM 阶段 hook 仍在 main.py）。"""

    async def run_test(self):
        """向多个虚拟会话投递同一条消息，立即返回 test_id（结果经 status 接口查询）。

        与真实平台一致：不设总超时、不分批投递，完全由 AstrBot 原生 pipeline 处理。
        """
        payload = await json_dict()
        if payload is None:
            return error_response("请求体必须是 JSON 对象", status_code=400)
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
        sender_id = payload.get("sender_id")
        sender_name = payload.get("sender_name")
        if sender_id is not None and not isinstance(sender_id, str):
            return error_response("sender_id 必须是字符串", status_code=400)
        if sender_name is not None and not isinstance(sender_name, str):
            return error_response("sender_name 必须是字符串", status_code=400)
        auto_at = payload.get("auto_at", True)
        if not isinstance(auto_at, bool):
            return error_response("auto_at 必须是布尔值", status_code=400)
        try:
            # 启动前探测 cron 任务：把可能向虚拟会话发送主动消息的任务作为
            # 运行级警告随 status()/事件流呈现（检测是增强，失败降级为无警告）
            umos, session_ids = target_sets(session_objs)
            warnings = await collect_cron_warnings(
                getattr(self.context, "cron_manager", None), umos, session_ids
            )
            test_id = await self.runner.start(
                sessions=session_objs,
                text=text,
                provider_id=payload.get("provider_id"),
                model=payload.get("model"),
                conf_id=payload.get("conf_id"),
                assertion=assertion,
                sender_id=sender_id,
                sender_name=sender_name,
                auto_at=auto_at,
                warnings=warnings,
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

    async def session_pending(self):
        """查询全部在途（未完成或刚完成）测试消息的实时状态。

        前端面板据此显示每条消息「已入队 / 排队等待 LLM / LLM 生成中 /
        完成」；中间两个状态由 main.py 的 LLM 阶段 hook 推进。
        """
        return json_response({"pending": self.runner.pending_entries()})
