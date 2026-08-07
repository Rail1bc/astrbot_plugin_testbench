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

from ..eval.assessor import Assessor
from ..eval.reporting import build_report_data
from .cron_probe import collect_cron_warnings, target_sets
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
        reviewer_store=None,
        report_store=None,
    ) -> None:
        self.context = context
        self.runner = runner
        # 未注入时自建空总线：publish 到无订阅者的总线是 no-op，测试可省去该参数
        self.event_bus = event_bus or EventBus()
        # reviewer profile 存储（评审阶段按 id 读取快照；未注入时 LLM 规则
        # 无法解析，Assessor 返回「找不到评审 profile」的 error verdict）
        self.reviewer_store = reviewer_store
        # 报告存储（report_enabled 的测试集运行终态产出持久化报告；未注入时
        # 不生成报告——测试可省去该参数）
        self.report_store = report_store
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
        mode, identity, pool = self._resolve_identity(testset)
        run = {
            "run_id": run_id,
            "testset_id": testset["id"],
            "testset_name": testset["name"],
            "identity_mode": mode,
            "batch_ranges": testset.get("batch_ranges") or [],
            "sessions": sessions,
            "status": "running",
            "current_step": -1,
            "steps": [
                self._step_record(m, mode, identity, pool) for m in testset["messages"]
            ],
            # 运行记录按测试集启动时快照 final_rules：评审阶段从 run 读取（与
            # steps 快照消息一致），测试集事后修改不影响本次运行
            "final_rules": testset.get("final_rules") or [],
            "final_verdicts": [],
            "reviewing": False,
            # 按启动时快照报告开关：测试集事后修改 report_enabled 不影响
            # 本次运行；终态后 _drive 依此生成持久化报告
            "report_enabled": bool(testset.get("report_enabled", False)),
            "report_id": None,
            "warnings": [],
            "started_at": time.time(),
            "finished_at": None,
            "error": None,
        }
        self._runs[run_id] = run
        self._tasks[run_id] = asyncio.create_task(self._drive(run_id))
        self._prune_runs()
        # 后台探测 cron 任务（与驱动并行）：命中虚拟会话的任务附到运行记录，
        # 随后续 _publish_run 全量广播呈现；探测失败降级为无警告
        asyncio.create_task(self._probe_cron_warnings(run_id, sessions))
        self._publish_run(run_id)
        return run_id

    async def _probe_cron_warnings(self, run_id: str, sessions: list[dict]) -> None:
        """运行启动后枚举 cron 任务，把针对虚拟会话的任务作为警告附到运行记录。

        探测是增强：cron_manager 未初始化 / 枚举失败一律降级为无警告，不影响
        运行本身。警告随 ``_publish_run`` 全量广播（每次步骤变更都重发）呈现。
        """
        run = self._runs.get(run_id)
        if run is None:
            return
        umos, session_ids = target_sets(sessions)
        warnings = await collect_cron_warnings(
            getattr(self.context, "cron_manager", None), umos, session_ids
        )
        if not warnings:
            return
        run["warnings"] = warnings
        self._publish_run(run_id)

    @staticmethod
    def _resolve_identity(
        testset: dict,
    ) -> tuple[str, dict | None, dict | None]:
        """解析测试集身份配置 → (mode, 身份快照或 None, 身份池快照或 None)。

        single 模式取 identity_snapshot（内联自包含，身份被删仍可用）；pool
        模式取 pool_snapshot（群聊名 + 成员身份列表）。
        """
        mode = testset.get("identity_mode")
        if mode != "pool":
            mode = "single"
        if mode == "single":
            snapshot = testset.get("identity_snapshot")
            return mode, snapshot if isinstance(snapshot, dict) else None, None
        pool = testset.get("pool_snapshot")
        return mode, None, pool if isinstance(pool, dict) else None

    @staticmethod
    def _step_sender(
        message: dict,
        mode: str,
        identity: dict | None,
        pool: dict | None,
    ) -> tuple[str | None, str | None, bool | None]:
        """解析单条消息的发送者 → (sender_id, sender_name, sender_is_admin)。

        - single + 身份快照：恒用测试集身份（消息级 sender 忽略）；
        - single 无身份：回退消息级 sender（保持旧行为），is_admin 由 runner
          按身份库解析（sender_is_admin=None）；
        - pool：消息 sender_id 引用池内成员（先按身份 id、其次 sender_id
          字符串匹配）；未引用 / 未命中 → 默认身份（全部 None）。
        """
        if mode == "single":
            if identity and identity.get("sender_id"):
                return (
                    identity["sender_id"],
                    identity["sender_name"],
                    bool(identity.get("is_admin")),
                )
            return (message.get("sender_id"), message.get("sender_name"), None)
        ref = message.get("sender_id")
        member: dict | None = None
        if ref:
            members = (pool.get("members") if pool else None) or []
            member = next(
                (m for m in members if m.get("id") == ref and m.get("sender_id")),
                None,
            )
            if member is None:
                member = next(
                    (m for m in members if m.get("sender_id") == ref),
                    None,
                )
        if member and member.get("sender_id"):
            return (
                member["sender_id"],
                member.get("sender_name"),
                bool(member.get("is_admin")),
            )
        return (None, None, None)

    @staticmethod
    def _step_record(
        message: dict, mode: str, identity: dict | None, pool: dict | None
    ) -> dict:
        """构建单步记录：透传规则列表、解析后的发送者与显式管理员标记。"""
        sender_id, sender_name, sender_is_admin = TestsetRunner._step_sender(
            message, mode, identity, pool
        )
        rules = message.get("rules")
        if not rules and message.get("rule"):
            rules = [message["rule"]]  # 兼容旧数据单条 rule
        return {
            "text": message["text"],
            "rules": rules or [],
            "sender_id": sender_id,
            "sender_name": sender_name,
            "sender_is_admin": sender_is_admin,
            "auto_at": message.get("auto_at", True),
            "status": "pending",
            "test_id": None,
            "results": [],
            "error": None,
        }

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
                # 终态必须广播：前端的单槽进度靠事件流推进，异常路径若不发布，
                # 页面会一直停留在 running 且无法恢复。
                self._publish_run(run_id)
        finally:
            run["finished_at"] = time.time()
            await self._generate_report(run)
            # _drive_segments 已在终态发布过快照（此时 report_id 尚为 None）；
            # 报告生成（finally 内完成）后再广播一次，订阅者 / 轮询者才能拿到
            # 含 report_id 的最终终态快照。
            self._publish_run(run["run_id"])

    async def _generate_report(self, run: dict) -> None:
        """终态后按 report_enabled 生成持久化报告（异常只记日志，不改运行状态）。

        报告与测试集同生命周期、不受内存运行记录清理约束；数据为终态快照
        （deepcopy），生成后不再变化。写盘经 ``write``（实例锁内线程化）。
        """
        if not run.get("report_enabled") or run.get("status") == "running":
            return
        if self.report_store is None:
            return
        try:
            report = await self.report_store.write(
                self.report_store.add_report,
                run["testset_id"],
                run["run_id"],
                build_report_data(run),
            )
            run["report_id"] = report["id"]
        except Exception:
            logger.exception(f"测试集运行 {run['run_id']} 报告生成失败")

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
            # 评审阶段：全部步骤完成后统一触发。评审期间 status 保持 running
            # （has_active_run 持续生效 → 锁会话，禁止并发运行污染被评审记录），
            # 评审结束或失败才转终态解锁。
            await self._review_phase(run)
            if run["status"] == "running":
                run["status"] = (
                    "error"
                    if any(s["status"] == "error" for s in run["steps"])
                    else "done"
                )
        self._publish_run(run["run_id"])

    async def _review_phase(self, run: dict) -> None:
        """统一评审：评估全部已 done 步骤的消息规则与 final_rules（scope 切片）。

        无规则快速路径：没有消息规则也没有 final_rules 直接返回。verdicts 由
        Assessor 原地写入各步骤结果；final_verdicts 存 run 级。评审编排异常
        （非单条规则失败）→ run error「评审失败」，终态即解锁。
        """
        if not any(s.get("rules") for s in run["steps"]) and not run.get("final_rules"):
            return
        run["reviewing"] = True
        self._publish_run(run["run_id"])
        try:
            assessor = Assessor(self.context, self._profiles())
            run["final_verdicts"] = await assessor.assess(
                run["steps"], run.get("final_rules") or [], run["sessions"]
            )
        except Exception as e:
            logger.exception(f"测试集运行 {run['run_id']} 评审异常")
            run["status"] = "error"
            run["error"] = f"评审失败: {e}"
        finally:
            run["reviewing"] = False
            self._publish_run(run["run_id"])

    def _profiles(self) -> dict[str, dict]:
        """评审时点读取 reviewer profile 快照（配置事后修改仍生效）。"""
        if self.reviewer_store is None:
            return {}
        return {p["id"]: p for p in self.reviewer_store.list_profiles()}

    async def _drive_single_step(self, run: dict, index: int) -> None:
        """单步段：发 → 等待全部会话完成；超时/异常 → 该步 error、中止后续。"""
        step = run["steps"][index]
        step["status"] = "running"
        run["current_step"] = index
        self._publish_run(run["run_id"])
        try:
            test_id = await self.runner.start(
                sessions=run["sessions"],
                text=step["text"],
                assertion=step["rules"],
                sender_id=step.get("sender_id"),
                sender_name=step.get("sender_name"),
                sender_is_admin=step.get("sender_is_admin"),
                auto_at=step.get("auto_at", True),
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
                    assertion=step["rules"],
                    sender_id=step.get("sender_id"),
                    sender_name=step.get("sender_name"),
                    sender_is_admin=step.get("sender_is_admin"),
                    auto_at=step.get("auto_at", True),
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

    def has_active_run(self) -> bool:
        """是否存在运行中的测试集运行。

        前端进度是单槽状态（activeRunId / 取消按钮 / 去重集合都只支持一个
        运行），故同一时刻只允许一个测试集运行，由 run_testset 入口守卫。
        """
        return any(r["status"] == "running" for r in self._runs.values())

    def list_runs(self, limit: int = 10, testset_id: str | None = None) -> list[dict]:
        """最近运行摘要（按开始时间倒序；可选按测试集过滤）。"""
        self._prune_runs()
        runs = sorted(self._runs.values(), key=lambda r: r["started_at"], reverse=True)
        if testset_id:
            runs = [r for r in runs if r["testset_id"] == testset_id]
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
