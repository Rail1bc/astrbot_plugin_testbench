"""定时任务探测：检查 cron 任务是否可能向虚拟会话发送主动消息（仅警告，不捕获）。

主动消息（cron 触发的问候 / 话题）经 `Context.send_message` 出站路径发送，
虚拟会话无法收到（见 docs/issue-proactive-message-capture.md 缺口 B）。本模块
在运行启动时枚举 cron 任务，把投递目标命中虚拟会话的任务作为警告项，随运行
记录 / 事件流呈现——检测是增强：cron_manager 未初始化或枚举失败一律降级为
无警告，不破坏测试本身。

匹配规则：

- `active_agent` 任务：`payload["session"]` 是投递目标（CronJobManager
  `_run_active_agent_job` 按它构造会话），与虚拟会话 umo 同格式
  （`platform:type:session_id`），做**精确字符串匹配**。
- `basic` 任务：payload 由注册方插件自定义、无固定 schema，只做浅层启发式
  扫描（值命中已知虚拟 umo / 会话 id 才警告），避免对任意 payload 误报。
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from ..store.group_store import umo_of

if TYPE_CHECKING:
    from astrbot.core.cron.manager import CronJobManager

# basic 任务 payload 启发式扫描的取值深度（只扫浅层字符串值）
_PAYLOAD_SCAN_MAX_DEPTH = 2


def target_sets(sessions: list[dict]) -> tuple[set[str], set[str]]:
    """从虚拟会话列表解析 (umo 集合, 会话 id 集合)。

    umo 集合用于精确匹配 cron 任务的投递目标（active_agent 的
    ``payload.session`` 与 ``MessageSession.from_str`` 同格式）；会话 id 集合
    用于 basic 任务 payload 的启发式扫描。
    """
    umos: set[str] = set()
    ids: set[str] = set()
    for s in sessions or []:
        if not isinstance(s, dict) or not s.get("id"):
            continue
        ids.add(s["id"])
        umos.add(umo_of(s))
    return umos, ids


def _payload_hits_virtual(
    value: Any, umos: set[str], session_ids: set[str], depth: int
) -> str | None:
    """递归扫描 payload，返回首个命中虚拟 umo / 会话 id 的字符串值（无则 None）。"""
    if isinstance(value, str):
        return value if value in umos or value in session_ids else None
    if depth <= 0 or value is None or isinstance(value, (int, float, bool)):
        return None
    if isinstance(value, dict):
        for v in value.values():
            hit = _payload_hits_virtual(v, umos, session_ids, depth - 1)
            if hit is not None:
                return hit
    elif isinstance(value, (list, tuple, set)):
        for v in value:
            hit = _payload_hits_virtual(v, umos, session_ids, depth - 1)
            if hit is not None:
                return hit
    return None


def cron_job_warning(job: dict, umos: set[str], session_ids: set[str]) -> dict | None:
    """纯函数：把一条已 dict 化的 cron 任务与虚拟会话比对，返回警告项或 None。

    job 须含 ``{job_id, name, job_type, cron_expression, payload, enabled}``；
    ``next_run_time`` 由调用方补入（读 scheduler 的活值）。active_agent 任务按
    ``payload.session`` 精确匹配 umo；basic 任务做浅层启发式扫描。
    """
    if not job.get("enabled"):
        return None
    payload = job.get("payload")
    if not isinstance(payload, dict):
        payload = {}
    jid = job.get("job_id")
    jname = job.get("name") or jid
    job_type = job.get("job_type")
    if job_type == "active_agent":
        session = str(payload.get("session") or "").strip()
        if session and session in umos:
            return {
                "kind": "cron_targets_virtual_session",
                "job_id": jid,
                "job_name": jname,
                "job_type": job_type,
                "cron": job.get("cron_expression"),
                "session": session,
                "next_run_time": job.get("next_run_time"),
                "message": (
                    f"定时任务「{jname}」（active_agent）的投递目标 {session} 是虚拟会话："
                    "触发时发送的主动消息经出站路径（Context.send_message），虚拟会话"
                    "无法收到，测试台无法捕获或断言。"
                ),
            }
    else:
        hit = _payload_hits_virtual(payload, umos, session_ids, _PAYLOAD_SCAN_MAX_DEPTH)
        if hit is not None:
            return {
                "kind": "cron_may_target_virtual_session",
                "job_id": jid,
                "job_name": jname,
                "job_type": job_type,
                "cron": job.get("cron_expression"),
                "session": hit,
                "next_run_time": job.get("next_run_time"),
                "message": (
                    f"basic 定时任务「{jname}」的 payload 含虚拟会话标识 {hit!r}："
                    "若该任务会向虚拟会话发送主动消息，虚拟会话无法收到、测试台无法捕获。"
                ),
            }
    return None


async def collect_cron_warnings(
    cron_manager: CronJobManager | None,
    umos: set[str],
    session_ids: set[str],
) -> list[dict]:
    """枚举 cron 任务并比对虚拟会话，返回警告项列表（无任务 / 无匹配 / 失败 → []）。

    cron_manager 可能未初始化（None）或 list_jobs 失败——一律降级为 []：
    检测是增强，不因探测异常破坏测试。
    """
    if cron_manager is None:
        return []
    try:
        jobs = await cron_manager.list_jobs()
    except Exception:  # noqa: BLE001
        return []
    warnings: list[dict] = []
    for job in jobs:
        try:
            d = {
                "job_id": job.job_id,
                "name": job.name,
                "job_type": job.job_type,
                "cron_expression": job.cron_expression,
                "payload": job.payload or {},
                "enabled": bool(job.enabled),
            }
            # scheduler 未启动时 get_next_run_time 可能抛 SchedulerNotRunningError，
            # 一并降级为无 next_run_time
            try:
                next_run = cron_manager.get_next_run_time(job.job_id)
                if isinstance(next_run, datetime):
                    d["next_run_time"] = next_run.isoformat()
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001
            continue
        warning = cron_job_warning(d, umos, session_ids)
        if warning is not None:
            warnings.append(warning)
    return warnings
