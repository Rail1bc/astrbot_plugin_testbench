"""测试集运行编排器。

测试集运行是**耗时操作**且可能跨页面会话（用户中途离开），因此由后端后台
任务驱动，运行记录保存在内存中、可轮询、可找回、可取消。发送节奏由测试集
内的「批量发送范围」（batch_ranges）决定，按段驱动：

- **单步段**（不在任何批量范围内）：逐条发出 → 等待该步全部会话完成 → 再发
  下一条（上下文连续）；单步超时/异常 → 该步 error、run error、中止后续。
- **批量段**：段内消息立即连续发出（重叠），再逐个收集结果；段内单步超时/
  异常 → 该步 error、继续收其余步，**不中止后续段**。
- 单步超时安全阀（TESTSET_STEP_TIMEOUT）防止一条悬挂消息拖死整个测试集。
- abort 只置标记、不 cancel 任务：当前步骤照常完成并收结果，后续步骤不再发。
- 运行记录按 DONE_RUN_KEEP_SECONDS / STALE_RUN_TIMEOUT 清理。
"""

from __future__ import annotations

import asyncio
import logging
import time
from copy import deepcopy
from typing import TYPE_CHECKING

from .event_bus import EventBus
from .runner import VirtualTestRunner

if TYPE_CHECKING:
    from astrbot.api.star import Context

# 单步超时（秒）：一步的全部会话未在此时限内完成即标记该步失败并中止（单步段）
# 或继续收其余步（批量段），防止悬挂 pipeline 拖死整个测试集。
TESTSET_STEP_TIMEOUT = 600

# 已结束运行记录的保留时长（秒）
DONE_RUN_KEEP_SECONDS = 600

# 悬挂运行的安全阀（秒）：与 runner 一致，仅防内存累积。
STALE_RUN_TIMEOUT = 3600

logger = logging.getLogger(__name__)


class TestsetRunner:
    """测试集运行编排器：按批量发送范围驱动段式运行并保存运行记录。"""

    # 类名以 Test 开头会触发 pytest 收集，显式标记为非测试类
    __test__ = False

    def __init__(
        self,
        context: Context,
        runner: VirtualTestRunner,
        event_bus: EventBus | None = None,
    ) -> None:
        self.context = context
        self.runner = runner
        # 未注入时自建空总线：publish 到无订阅者的总线是 no-op，测试可省去该参数
        self.event_bus = event_bus or EventBus()
        self._runs: dict[str, dict] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._run_seq = 0

    def _publish_run(self, run_id: str) -> None:
        """广播测试集运行全量快照（幂等：新快照覆盖旧快照，丢旧无碍）。

        run dict 由驱动任务原地更新，快照须深拷贝，否则已发布事件里的 run 会
        随步骤推进漂移成最新状态，不再是发布时刻的进度。
        """
        run = self.status(run_id)
        if run is not None:
            self.event_bus.publish(
                {"type": "testset", "run_id": run_id, "run": deepcopy(run)}
            )

    def start_run(self, testset: dict, sessions: list[dict]) -> str:
        """为测试集创建运行记录并启动后台驱动任务，立即返回 run_id。"""
        self._run_seq += 1
        run_id = f"tr_{int(time.time() * 1000)}_{self._run_seq}"
        run = {
            "run_id": run_id,
            "testset_id": testset["id"],
            "testset_name": testset["name"],
            "batch_ranges": testset.get("batch_ranges") or [],
            "sessions": sessions,
            "status": "running",
            "current_step": -1,
            "steps": [
                {
                    "text": m["text"],
                    "rule": m.get("rule"),
                    "status": "pending",
                    "test_id": None,
                    "results": [],
                    "error": None,
                }
                for m in testset["messages"]
            ],
            "started_at": time.time(),
            "finished_at": None,
            "error": None,
        }
        self._runs[run_id] = run
        self._tasks[run_id] = asyncio.create_task(self._drive(run_id))
        self._prune_runs()
        self._publish_run(run_id)
        return run_id

    # ---------- 后台驱动 ----------

    async def _drive(self, run_id: str) -> None:
        run = self._runs[run_id]
        try:
            await self._drive_segments(run)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(f"测试集运行 {run_id} 异常")
            if run["status"] == "running":
                run["status"] = "error"
                run["error"] = "运行器内部异常"
        finally:
            run["finished_at"] = time.time()

    @staticmethod
    def _segments(run: dict) -> list[tuple[list[int], bool]]:
        """把消息索引切分为「单步段」与「批量段」。

        返回 [(索引列表, 是否批量), ...]：批量范围之外的每条消息是独立单步段，
        范围内的消息合并为批量段；按索引顺序排列。
        """
        n = len(run["steps"])
        segments: list[tuple[list[int], bool]] = []
        cursor = 0
        for start, end in run["batch_ranges"]:
            for i in range(cursor, start):
                segments.append(([i], False))
            segments.append((list(range(start, end + 1)), True))
            cursor = end + 1
        for i in range(cursor, n):
            segments.append(([i], False))
        return segments

    async def _drive_segments(self, run: dict) -> None:
        for indices, is_batch in self._segments(run):
            if run["status"] != "running":
                break  # 已 abort 或单步段已失败
            if is_batch:
                await self._drive_batch_segment(run, indices)
            else:
                await self._drive_single_step(run, indices[0])
        if run["status"] == "running":
            run["status"] = (
                "error" if any(s["status"] == "error" for s in run["steps"]) else "done"
            )
        self._publish_run(run["run_id"])

    async def _drive_single_step(self, run: dict, index: int) -> None:
        """单步段：发 → 等待全部会话完成；超时/异常 → 该步 error、中止后续。"""
        step = run["steps"][index]
        step["status"] = "running"
        run["current_step"] = index
        self._publish_run(run["run_id"])
        try:
            test_id = await self.runner.start(
                sessions=run["sessions"], text=step["text"], assertion=step["rule"]
            )
            step["test_id"] = test_id
            rec = await self.runner.wait_done(
                test_id, timeout_secs=TESTSET_STEP_TIMEOUT
            )
            step["results"] = rec["results"]
            step["status"] = "done"
        except TimeoutError:
            step["status"] = "error"
            step["error"] = f"步骤超时（> {TESTSET_STEP_TIMEOUT}s）"
            if run["status"] == "running":
                run["status"] = "error"
                run["error"] = step["error"]
        except Exception as e:
            step["status"] = "error"
            step["error"] = str(e)
            if run["status"] == "running":
                run["status"] = "error"
                run["error"] = f"步骤失败: {e}"
        self._publish_run(run["run_id"])

    async def _drive_batch_segment(self, run: dict, indices: list[int]) -> None:
        """批量段：先全部发出（重叠），再逐个收集；段内错误不中止后续段。"""
        run["current_step"] = indices[0]
        for i in indices:
            step = run["steps"][i]
            if run["status"] != "running":
                break  # 已 abort：未发出的步骤保持 pending
            step["status"] = "running"
            try:
                test_id = await self.runner.start(
                    sessions=run["sessions"],
                    text=step["text"],
                    assertion=step["rule"],
                )
                step["test_id"] = test_id
            except Exception as e:
                step["status"] = "error"
                step["error"] = str(e)
            self._publish_run(run["run_id"])
        for i in indices:
            step = run["steps"][i]
            if step["status"] != "running" or step["test_id"] is None:
                continue
            # 不因 abort 中断收集：段内步骤都已发出（消息已入队），必须全部收完，
            # 否则已发出的步骤永远卡在 running 且结果丢失。
            try:
                rec = await self.runner.wait_done(
                    step["test_id"], timeout_secs=TESTSET_STEP_TIMEOUT
                )
                step["results"] = rec["results"]
                step["status"] = "done"
            except TimeoutError:
                step["status"] = "error"
                step["error"] = f"步骤超时（> {TESTSET_STEP_TIMEOUT}s）"
            except Exception as e:
                step["status"] = "error"
                step["error"] = str(e)
            self._publish_run(run["run_id"])

    # ---------- 查询 / 取消 / 清理 ----------

    def status(self, run_id: str) -> dict | None:
        return self._runs.get(run_id)

    def list_runs(self, limit: int = 10) -> list[dict]:
        """最近运行摘要（按开始时间倒序）。"""
        self._prune_runs()
        runs = sorted(self._runs.values(), key=lambda r: r["started_at"], reverse=True)
        return [
            {
                "run_id": r["run_id"],
                "testset_id": r["testset_id"],
                "testset_name": r["testset_name"],
                "status": r["status"],
                "started_at": r["started_at"],
                "finished_at": r["finished_at"],
                "total_steps": len(r["steps"]),
                "done_steps": sum(
                    1 for s in r["steps"] if s["status"] in ("done", "error")
                ),
            }
            for r in runs[:limit]
        ]

    def abort(self, run_id: str) -> bool:
        """请求取消运行：当前步骤照常完成并收结果，后续步骤不再发。"""
        run = self._runs.get(run_id)
        if run is None or run["status"] != "running":
            return False
        run["status"] = "cancelled"
        self._publish_run(run_id)
        return True

    def _prune_runs(self) -> None:
        now = time.time()
        for run_id, run in list(self._runs.items()):
            if run["status"] == "running":
                if now - run["started_at"] > STALE_RUN_TIMEOUT:
                    self._runs.pop(run_id, None)
                    task = self._tasks.pop(run_id, None)
                    if task is not None:
                        # 停止孤儿驱动：运行记录已移除，无法再查询/中止，
                        # 若不禁用，后台任务会继续驱动真实测试消息。
                        task.cancel()
            elif now - (run["finished_at"] or now) > DONE_RUN_KEEP_SECONDS:
                self._runs.pop(run_id, None)
                self._tasks.pop(run_id, None)
