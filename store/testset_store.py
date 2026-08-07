"""测试集数据模型与持久化。

测试集是一组连续 user 消息序列（可选每条消息带断言规则列表），用于对单个
会话做多轮对话的纵深测试（命令 → 子命令 → 参数确认 → 结果）、提示词/插件
改动后的回归（同一序列反复跑）与连发/追问压测。测试集可配置发送身份
（single 单一身份 / pool 身份池），保存时把被引用身份的完整数据内联快照进
测试集（自包含，身份删除仍可用）。本模块负责测试集的创建、持久化与清洗，
不依赖 AstrBot 运行时状态。
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path
from typing import Any

from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

from ._base import AsyncWriteMixin

# 单测试集消息条数上限（与 MAX_SESSIONS_PER_GROUP 同风格的安全阀）
MAX_MESSAGES_PER_TESTSET = 100

# 身份快照白名单键（内联快照自包含：id/name/sender_id/sender_name/is_admin）
_SNAPSHOT_KEYS = ("id", "name", "sender_id", "sender_name", "is_admin")
# 身份池快照白名单键（{name, members:[Identity]}）
_POOL_KEYS = ("name", "members")


def _clean_rules(rules: Any, legacy_rule: Any = None) -> list[dict]:
    """清洗断言规则列表：rules 为 list 时逐项保留 dict；否则回退单条 legacy rule。"""
    if isinstance(rules, list):
        return [r for r in rules if isinstance(r, dict)]
    if isinstance(legacy_rule, dict):
        return [legacy_rule]
    return []


class TestsetStore(AsyncWriteMixin):
    """测试集的创建、持久化与清洗。

    数据保存到 data 目录下 `virtual_session/testsets.json`（与 groups.json 同
    目录），全量写 JSON。同步写方法保持同步签名，由 API 层经 ``write``
    （实例锁内线程化）执行，避免事件循环阻塞与并发写竞态。
    """

    # 类名以 Test 开头会触发 pytest 收集，显式标记为非测试类
    __test__ = False

    def __init__(self, data_dir: Path | None = None) -> None:
        base = Path(get_astrbot_plugin_data_path()) if data_dir is None else data_dir
        self._dir = base / "virtual_session"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._file = self._dir / "testsets.json"
        self._lock = asyncio.Lock()
        self._testsets: list[dict] = self._load()

    def _load(self) -> list[dict]:
        if self._file.exists():
            try:
                data = json.loads(self._file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                data = None
            if isinstance(data, dict) and isinstance(data.get("testsets"), list):
                testsets = [t for t in data["testsets"] if isinstance(t, dict)]
                for testset in testsets:
                    testset.setdefault("batch_ranges", [])
                    testset.setdefault("final_rules", [])
                    testset.setdefault("identity_mode", "single")
                    testset.setdefault("identity_id", None)
                    testset.setdefault("chat_group_id", None)
                    testset.setdefault("identity_snapshot", None)
                    testset.setdefault("pool_snapshot", None)
                    testset.setdefault("report_enabled", False)
                    # 旧数据迁移：单条 rule → rules 列表；残留 rule 键清理（防随
                    # 全量写 JSON 永久残留，同 group_store 对旧 auto_at 的处理）
                    for message in testset.get("messages") or []:
                        if not isinstance(message, dict):
                            continue
                        if "rules" not in message:
                            message["rules"] = _clean_rules(None, message.get("rule"))
                            message.pop("rule", None)
                return testsets
        return []

    def _save(self) -> None:
        self._file.write_text(
            json.dumps({"testsets": self._testsets}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @staticmethod
    def _normalize_messages(messages: list[dict]) -> list[dict]:
        """清洗消息列表：text 去首尾空白，空文本丢弃，rules 归一为 dict 列表。

        可选 is_command（是否命令，预期触发框架行为而非 LLM 回复）为 True 时
        保留（缺省 False）；可选 sender_id / sender_name（消息级测试身份）为
        非空字符串时保留；可选 auto_at（消息级是否模拟「@机器人」发言）为 bool
        时保留，缺省为 True（发送时再决定）。
        """
        out: list[dict] = []
        for item in messages:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            message: dict = {
                "text": text,
                "rules": _clean_rules(item.get("rules"), item.get("rule")),
            }
            if item.get("is_command") is True:
                message["is_command"] = True
            for key in ("sender_id", "sender_name"):
                value = item.get(key)
                if isinstance(value, str) and value:
                    message[key] = value
            auto_at = item.get("auto_at")
            if isinstance(auto_at, bool):
                message["auto_at"] = auto_at
            out.append(message)
        return out

    @staticmethod
    def _normalize_batch_ranges(
        batch_ranges: Any, message_count: int
    ) -> list[list[int]]:
        """清洗批量发送范围：仅保留合法且互不重叠的 [s, e] 整数闭区间。

        每项须为非 bool 的两个 int 且 0 ≤ s ≤ e < message_count；不满足的整段丢弃；
        与已保留段重叠的整段丢弃；先按 start 升序再贪心保留，保证结果与输入顺序
        无关。
        """
        if not isinstance(batch_ranges, list):
            return []
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
                continue
            start, end = item
            if not (0 <= start <= end < message_count):
                continue
            kept.append([start, end])
        kept.sort(key=lambda r: r[0])
        out: list[list[int]] = []
        for start, end in kept:
            if not out or start > out[-1][1]:
                out.append([start, end])
        return out

    @staticmethod
    def _normalize_final_rules(final_rules: Any) -> list[dict]:
        """清洗 final_rules：每项须为 {rule: Rule, scope}，rule 须为 dict。

        scope 为 "all"（默认）或 {from, to} 非 bool 整数闭区间；形状非法整项
        丢弃。scope 边界由评估层钳制（_scope_indices），此处只校验形状。
        """
        if not isinstance(final_rules, list):
            return []
        out: list[dict] = []
        for item in final_rules:
            if not isinstance(item, dict):
                continue
            rule = item.get("rule")
            if not isinstance(rule, dict):
                continue
            scope: Any = item.get("scope", "all")
            if isinstance(scope, dict):
                frm = scope.get("from")
                to = scope.get("to")
                if (
                    isinstance(frm, int)
                    and isinstance(to, int)
                    and not isinstance(frm, bool)
                    and not isinstance(to, bool)
                ):
                    scope = {"from": frm, "to": to}
                else:
                    scope = "all"
            elif scope != "all":
                scope = "all"
            out.append({"rule": rule, "scope": scope})
        return out

    @staticmethod
    def _normalize_identity_mode(mode: Any) -> str:
        """清洗身份模式：仅接受 "single" / "pool"，其余回退 "single"。"""
        return mode if mode in ("single", "pool") else "single"

    @staticmethod
    def _clean_optional_str(value: Any) -> str | None:
        """清洗可选字符串引用：非空字符串保留（去空白），其余归一为 None。"""
        if isinstance(value, str) and value.strip():
            return value.strip()
        return None

    @staticmethod
    def _clean_snapshot(snapshot: Any, keys: tuple[str, ...]) -> dict | None:
        """按白名单键清洗内联快照；非 dict 或无可保留键 → None。"""
        if not isinstance(snapshot, dict):
            return None
        out = {k: snapshot[k] for k in keys if snapshot.get(k) is not None}
        return out or None

    @staticmethod
    def _clean_pool_snapshot(snapshot: Any) -> dict | None:
        """清洗身份池快照 {name, members:[Identity]}；非 dict → None。"""
        if not isinstance(snapshot, dict):
            return None
        out: dict = {}
        if isinstance(snapshot.get("name"), str):
            out["name"] = snapshot["name"]
        members = snapshot.get("members")
        if isinstance(members, list):
            out["members"] = [m for m in members if isinstance(m, dict)]
        return out or None

    # ---------- 查询 ----------

    def list_testsets(self) -> list[dict]:
        return list(self._testsets)

    def get_testset(self, testset_id: str) -> dict | None:
        for testset in self._testsets:
            if testset["id"] == testset_id:
                return testset
        return None

    # ---------- 创建 / 更新 / 删除 ----------

    def create_testset(
        self,
        name: str,
        messages: list[dict],
        batch_ranges: Any = None,
        *,
        final_rules: Any = None,
        identity_mode: Any = None,
        identity_id: Any = None,
        chat_group_id: Any = None,
        identity_snapshot: Any = None,
        pool_snapshot: Any = None,
        report_enabled: Any = False,
    ) -> dict:
        normalized = self._normalize_messages(messages)
        testset = {
            "id": f"ts_{uuid.uuid4().hex[:8]}",
            "name": str(name or "").strip() or "测试集",
            "created_at": int(time.time()),
            "messages": normalized,
            "batch_ranges": self._normalize_batch_ranges(batch_ranges, len(normalized)),
            "final_rules": self._normalize_final_rules(final_rules),
            "identity_mode": self._normalize_identity_mode(identity_mode),
            "identity_id": self._clean_optional_str(identity_id),
            "chat_group_id": self._clean_optional_str(chat_group_id),
            "identity_snapshot": self._clean_snapshot(
                identity_snapshot, _SNAPSHOT_KEYS
            ),
            "pool_snapshot": self._clean_pool_snapshot(pool_snapshot),
            "report_enabled": bool(report_enabled),
        }
        self._testsets.append(testset)
        self._save()
        return testset

    def update_testset(
        self,
        testset_id: str,
        name: str,
        messages: list[dict],
        batch_ranges: Any = None,
        *,
        final_rules: Any = None,
        identity_mode: Any = None,
        identity_id: Any = None,
        chat_group_id: Any = None,
        identity_snapshot: Any = None,
        pool_snapshot: Any = None,
        report_enabled: Any = False,
    ) -> dict | None:
        testset = self.get_testset(testset_id)
        if testset is None:
            return None
        normalized = self._normalize_messages(messages)
        testset["name"] = str(name or "").strip() or "测试集"
        testset["messages"] = normalized
        testset["batch_ranges"] = self._normalize_batch_ranges(
            batch_ranges, len(normalized)
        )
        testset["final_rules"] = self._normalize_final_rules(final_rules)
        testset["identity_mode"] = self._normalize_identity_mode(identity_mode)
        testset["identity_id"] = self._clean_optional_str(identity_id)
        testset["chat_group_id"] = self._clean_optional_str(chat_group_id)
        testset["identity_snapshot"] = self._clean_snapshot(
            identity_snapshot, _SNAPSHOT_KEYS
        )
        testset["pool_snapshot"] = self._clean_pool_snapshot(pool_snapshot)
        testset["report_enabled"] = bool(report_enabled)
        self._save()
        return testset

    def delete_testsets(self, ids: list[str]) -> int:
        id_set = set(ids)
        kept = [t for t in self._testsets if t["id"] not in id_set]
        removed = len(self._testsets) - len(kept)
        if removed:
            self._testsets = kept
            self._save()
        return removed
