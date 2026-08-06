"""测试集接口：CRUD、批量段校验与运行编排入口。"""

from __future__ import annotations

from typing import Any

from astrbot.api.web import error_response, json_response, request

from ..store.testset_store import MAX_MESSAGES_PER_TESTSET


class TestsetsAPI:
    """测试集 handler 集合（挂在 Star 上，共享 self.testset_store / self.testset_runner）。"""

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
        """启动测试集运行（后端后台任务驱动，立即返回 run_id，进度经 status 查询）。

        测试集运行是耗时操作、可能与页面生命周期解耦：发送节奏由测试集内的
        批量发送范围决定（段内重叠、段外逐条），后台任务按段驱动，离开页面
        不影响执行；运行记录可经 ``/testsets/run/status`` 查询、
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
        if self.testset_runner.has_active_run():
            # 前端进度是单槽状态，同时只允许一个测试集运行，防止两个运行的
            # 事件流互相污染 activeRunId / 步骤去重集合
            return error_response(
                "已有测试集运行中，请先等待其完成或取消后再启动新的运行",
                status_code=400,
            )
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
