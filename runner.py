"""虚拟会话测试组管理与并发测试运行器。

核心思路：测试以"测试组"为单位 —— 一个组是一组共享同一套配置（平台来源、
配置档案、发送者 id/昵称）的虚拟会话，组内单个会话可覆盖组配置。虚拟会话 =
一个专属的 platform_id + session_id。把 `VirtualMessageEvent` 直接投递到
AstrBot 的事件队列（`context.get_event_queue()`），事件总线会像处理真实平台
消息一样，按 umo 解析配置档案并交给 pipeline 调度器执行，因此虚拟会话与真实
会话共享完全相同的处理路径。回复由事件自身的 send()/send_streaming() 捕获。
"""

from __future__ import annotations

import asyncio
import json
import math
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

from .virtual_event import VirtualMessageEvent

if TYPE_CHECKING:
    from astrbot.api.star import Context

DEFAULT_PLATFORM_ID = "virtual_test"
DEFAULT_SENDER_ID = "virtual_user"
DEFAULT_SENDER_NAME = "虚拟用户"
MESSAGE_TYPE = "FriendMessage"

# 用于区分"未传该字段"与"显式传 null（恢复继承组配置）"
_UNSET = object()


def umo_of(session: dict) -> str:
    """根据（已解析的）虚拟会话数据计算 unified_msg_origin。"""
    platform_id = session.get("platform_id") or DEFAULT_PLATFORM_ID
    return f"{platform_id}:{MESSAGE_TYPE}:{session['id']}"


class VirtualGroupManager:
    """测试组的创建、持久化与配置解析。

    数据保存到 data 目录下 `virtual_session/groups.json`，符合 "插件持久化
    数据存 data 目录" 的规范。旧版平铺会话文件（sessions.json）会自动迁移为
    一个"默认测试组"。
    """

    def __init__(self, data_dir: Path | None = None) -> None:
        base = Path(get_astrbot_plugin_data_path()) if data_dir is None else data_dir
        self._dir = base / "virtual_session"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._file = self._dir / "groups.json"
        self._legacy_file = self._dir / "sessions.json"
        self._groups: list[dict] = self._load()

    # ---------- 持久化 ----------

    def _load(self) -> list[dict]:
        if self._file.exists():
            try:
                data = json.loads(self._file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                data = None
            if isinstance(data, dict) and isinstance(data.get("groups"), list):
                return [g for g in data["groups"] if isinstance(g, dict)]
        return self._migrate_legacy()

    def _migrate_legacy(self) -> list[dict]:
        """旧版 sessions.json（平铺会话列表）迁移为单个默认测试组。"""
        if not self._legacy_file.exists():
            return []
        try:
            legacy = json.loads(self._legacy_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(legacy, list) or not legacy:
            return []
        group = {
            "id": f"g_{uuid.uuid4().hex[:8]}",
            "name": "默认测试组",
            "platform_id": None,
            "conf_id": None,
            "sender_id": None,
            "sender_name": None,
            "created_at": int(time.time()),
            "sessions": legacy,
        }
        self._groups = [group]
        self._save()
        return self._groups

    def _save(self) -> None:
        self._file.write_text(
            json.dumps({"groups": self._groups}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # ---------- 查询 ----------

    def list_groups(self) -> list[dict]:
        """返回全部测试组。"""
        return list(self._groups)

    def get_group(self, group_id: str) -> dict | None:
        for group in self._groups:
            if group["id"] == group_id:
                return group
        return None

    def find_session(self, session_id: str) -> tuple[dict, dict] | None:
        """按 id 查找会话，返回 (所属组, 会话) 或 None。"""
        for group in self._groups:
            for session in group.get("sessions", []):
                if session["id"] == session_id:
                    return group, session
        return None

    def all_sessions(self) -> list[tuple[dict, dict]]:
        """返回全部 (组, 会话) 对。"""
        return [
            (group, session)
            for group in self._groups
            for session in group.get("sessions", [])
        ]

    # ---------- 创建 / 删除 ----------

    def create_group(
        self,
        name: str,
        count: int = 1,
        platform_id: str | None = None,
        conf_id: str | None = None,
        sender_id: str | None = None,
        sender_name: str | None = None,
        name_prefix: str | None = None,
    ) -> dict:
        """创建测试组并生成 count 个继承组配置的虚拟会话。"""
        group = {
            "id": f"g_{uuid.uuid4().hex[:8]}",
            "name": str(name or "").strip() or "测试组",
            "platform_id": platform_id,
            "conf_id": conf_id,
            "sender_id": sender_id,
            "sender_name": sender_name,
            "created_at": int(time.time()),
            "sessions": [],
        }
        group["sessions"] = self._new_sessions(group, count, name_prefix)
        self._groups.append(group)
        self._save()
        return group

    def add_sessions(
        self,
        group_id: str,
        count: int,
        name_prefix: str | None = None,
    ) -> list[dict]:
        """向组内新增会话（默认继承组配置），返回新增的会话。"""
        group = self.get_group(group_id)
        if group is None:
            raise KeyError(group_id)
        created = self._new_sessions(group, count, name_prefix)
        group["sessions"].extend(created)
        self._save()
        return created

    def _new_sessions(
        self, group: dict, count: int, name_prefix: str | None
    ) -> list[dict]:
        prefix = str(name_prefix or "").strip() or group.get("name") or "虚拟会话"
        base = len(group.get("sessions", []))
        return [
            {
                "id": f"vs_{uuid.uuid4().hex[:8]}",
                "name": f"{prefix}{base + i + 1}",
                "created_at": int(time.time()),
                # 四键均为 None 时表示继承组配置
                "platform_id": None,
                "conf_id": None,
                "sender_id": None,
                "sender_name": None,
            }
            for i in range(count)
        ]

    def delete_groups(self, ids: list[str]) -> list[tuple[dict, dict]]:
        """删除测试组，返回被删除的 (组, 会话) 对（用于清理路由）。"""
        id_set = set(ids)
        removed: list[tuple[dict, dict]] = []
        kept: list[dict] = []
        for group in self._groups:
            if group["id"] in id_set:
                for session in group.get("sessions", []):
                    removed.append((group, session))
            else:
                kept.append(group)
        if removed:
            self._groups = kept
            self._save()
        return removed

    def delete_sessions(self, ids: list[str]) -> list[tuple[dict, dict]]:
        """删除会话（从其所属组中移除），返回被删除的 (组, 会话) 对。"""
        id_set = set(ids)
        removed: list[tuple[dict, dict]] = []
        for group in self._groups:
            keep: list[dict] = []
            for session in group.get("sessions", []):
                if session["id"] in id_set:
                    removed.append((group, session))
                else:
                    keep.append(session)
            if len(keep) != len(group.get("sessions", [])):
                group["sessions"] = keep
        if removed:
            self._save()
        return removed

    # ---------- 会话配置覆盖 ----------

    def update_session(
        self,
        session_id: str,
        *,
        platform_id: Any = _UNSET,
        conf_id: Any = _UNSET,
        sender_id: Any = _UNSET,
        sender_name: Any = _UNSET,
    ) -> tuple[dict, dict] | None:
        """更新会话的配置覆盖。

        字段值为 None 表示恢复"继承组配置"；conf_id 为 "" 表示显式使用
        默认配置档案（不绑定）。返回 (组, 会话)，会话不存在时返回 None。
        """
        found = self.find_session(session_id)
        if found is None:
            return None
        group, session = found
        changed = False
        for key, value in {
            "platform_id": platform_id,
            "conf_id": conf_id,
            "sender_id": sender_id,
            "sender_name": sender_name,
        }.items():
            if value is not _UNSET:
                session[key] = value
                changed = True
        if changed:
            self._save()
        return group, session

    # ---------- 配置解析 ----------

    @staticmethod
    def effective(group: dict, session: dict) -> dict:
        """把组共享配置与会话覆盖解析为最终配置（会话覆盖优先）。"""
        conf_id = session.get("conf_id")
        if conf_id is None:
            conf_id = group.get("conf_id") or None
        else:
            conf_id = conf_id or None  # "" 表示显式使用默认配置档案
        return {
            "id": session["id"],
            "name": session.get("name", session["id"]),
            "platform_id": (
                session.get("platform_id")
                or group.get("platform_id")
                or DEFAULT_PLATFORM_ID
            ),
            "sender_id": session.get("sender_id")
            or group.get("sender_id")
            or DEFAULT_SENDER_ID,
            "sender_name": (
                session.get("sender_name")
                or group.get("sender_name")
                or DEFAULT_SENDER_NAME
            ),
            "conf_id": conf_id,
            "created_at": session.get("created_at", 0),
        }

    def effective_many(self, ids: list[str]) -> list[dict]:
        """按 id 批量解析会话为最终配置（保持传入顺序，缺失的跳过）。"""
        wanted = set(ids)
        found = {
            session["id"]: (group, session)
            for group, session in self.all_sessions()
            if session["id"] in wanted
        }
        return [self.effective(*found[sid]) for sid in ids if sid in found]

    def flat_sessions(self) -> list[dict]:
        """返回全部会话的展平列表（已解析最终配置，附组信息）。"""
        out: list[dict] = []
        for group, session in self.all_sessions():
            resolved = self.effective(group, session)
            resolved["group_id"] = group["id"]
            resolved["group_name"] = group["name"]
            out.append(resolved)
        return out


def _percentile(sorted_values: list[float], p: float) -> float:
    """线性插值分位数（sorted_values 已升序）。"""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = (len(sorted_values) - 1) * p
    low = int(math.floor(pos))
    high = int(math.ceil(pos))
    if low == high:
        return sorted_values[low]
    frac = pos - low
    return sorted_values[low] + (sorted_values[high] - sorted_values[low]) * frac


def duration_stats(durations: list[float]) -> dict:
    """计算耗时统计（min/max/avg/p50/p95）。"""
    if not durations:
        return {"min": 0.0, "max": 0.0, "avg": 0.0, "p50": 0.0, "p95": 0.0}
    sorted_d = sorted(durations)
    return {
        "min": round(min(durations), 3),
        "max": round(max(durations), 3),
        "avg": round(sum(durations) / len(durations), 3),
        "p50": round(_percentile(sorted_d, 0.5), 3),
        "p95": round(_percentile(sorted_d, 0.95), 3),
    }


class VirtualTestRunner:
    """把一条消息投递到多个虚拟会话，逐个流式汇总结果。

    与真实平台一致：不设总超时、不分批投递，事件入队后完全由 AstrBot 原生
    pipeline 处理。每次 ``start()`` 投递后立即返回 test_id，后台逐个等待会话
    完成并记录，前端轮询 ``status()`` 即可实现"每个会话窗口独立刷新"。
    不带 conf_id 的测试完全并行；带 conf_id 的测试之间通过路由锁串行，避免
    临时 UCR 路由互相污染。
    """

    def __init__(self, context: Context) -> None:
        self.context = context
        self._route_lock = asyncio.Lock()
        self._saved_routes: list[tuple[str, str | None]] = []
        self._runs: dict[str, dict] = {}
        self._run_seq = 0

    async def _apply_conf_route(self, sessions: list[dict], conf_id: str) -> None:
        """为每个会话设置精确的 UCR 路由（umo → conf_id），并保存原路由以便恢复。

        使用会话级精确路由（而非平台级 `platform_id::`），避免影响同平台其他会话，
        且不覆盖会话创建时绑定的持久配置。
        """
        ucr = self.context.astrbot_config_mgr.ucr
        self._saved_routes = []
        for session in sessions:
            umop = umo_of(session)
            self._saved_routes.append((umop, ucr.umop_to_conf_id.get(umop)))
            await ucr.update_route(umop, conf_id)

    async def _restore_conf_route(self) -> None:
        if not self._saved_routes:
            return
        ucr = self.context.astrbot_config_mgr.ucr
        for umop, prev_conf_id in self._saved_routes:
            if prev_conf_id is None:
                if umop in ucr.umop_to_conf_id:
                    await ucr.delete_route(umop)
            else:
                await ucr.update_route(umop, prev_conf_id)
        self._saved_routes = []

    async def start(
        self,
        sessions: list[dict],
        text: str,
        provider_id: str | None = None,
        model: str | None = None,
        conf_id: str | None = None,
    ) -> str:
        """投递消息并立即返回 test_id（不等待回复）。

        Args:
            sessions: 已解析最终配置的虚拟会话数据列表。
            text: 要发送的消息文本（所有会话相同）。
            provider_id: 可选，覆盖 LLM provider。
            model: 可选，覆盖模型名。
            conf_id: 可选，临时把会话的配置档案路由到指定档案（测试提示词/系统设定）。

        Returns:
            test_id，用于轮询 status()。
        """
        if not sessions:
            raise ValueError("sessions 不能为空")
        if not text or not text.strip():
            raise ValueError("text 不能为空")

        if conf_id:
            await self._route_lock.acquire()
            try:
                await self._apply_conf_route(sessions, conf_id)
            except Exception:
                self._route_lock.release()
                raise

        events = [
            VirtualMessageEvent.create(
                session_id=s["id"],
                sender_id=s.get("sender_id") or DEFAULT_SENDER_ID,
                sender_name=s.get("sender_name") or DEFAULT_SENDER_NAME,
                text=text,
                platform_id=s.get("platform_id") or DEFAULT_PLATFORM_ID,
                platform_name=s.get("platform_id") or DEFAULT_PLATFORM_ID,
                provider_id=provider_id,
                model=model,
            )
            for s in sessions
        ]

        self._run_seq += 1
        test_id = f"t_{int(time.time() * 1000)}_{self._run_seq}"
        record = {
            "id": test_id,
            "total": len(events),
            "results": {},  # session_id -> 结果摘要
            "created_at": time.time(),
            "finished_at": None,
            "done": False,
            "all_done": asyncio.Event(),
        }
        self._runs[test_id] = record
        self._prune_runs()

        queue = self.context.get_event_queue()
        for event in events:
            queue.put_nowait(event)
        for event in events:
            asyncio.create_task(self._await_event(test_id, event))
        if conf_id:
            asyncio.create_task(self._release_route_after(test_id))
        return test_id

    async def _await_event(self, test_id: str, event: VirtualMessageEvent) -> None:
        await event.pipeline_done_event.wait()
        record = self._runs.get(test_id)
        if record is None:
            return
        record["results"][event.session_id] = event.result_summary()
        if len(record["results"]) >= record["total"] and not record["done"]:
            record["done"] = True
            record["finished_at"] = time.time()
            record["all_done"].set()

    async def _release_route_after(self, test_id: str) -> None:
        record = self._runs.get(test_id)
        if record is None:
            return
        await record["all_done"].wait()
        await self._restore_conf_route()
        self._route_lock.release()

    def status(self, test_id: str) -> dict | None:
        """查询运行状态（含已完成会话的结果与统计）。"""
        record = self._runs.get(test_id)
        if record is None:
            return None
        results = list(record["results"].values())
        return {
            "test_id": test_id,
            "total": record["total"],
            "done": record["done"],
            "results": results,
            "stats": duration_stats([r["duration"] for r in results]),
        }

    def _prune_runs(self) -> None:
        """清理已完成超过 10 分钟的运行记录，避免内存累积。"""
        now = time.time()
        expired = [
            tid
            for tid, r in self._runs.items()
            if r["done"] and (now - (r["finished_at"] or now)) > 600
        ]
        for tid in expired:
            self._runs.pop(tid, None)
