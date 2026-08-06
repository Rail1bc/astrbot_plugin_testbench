"""测试集数据模型与持久化。

测试集是一组连续 user 消息序列（可选每条消息带回复断言规则），用于对单个
会话做多轮对话的纵深测试（命令 → 子命令 → 参数确认 → 结果）、提示词/插件
改动后的回归（同一序列反复跑）与连发/追问压测。本模块负责测试集的创建、
持久化与清洗，不依赖 AstrBot 运行时状态。
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

# 单测试集消息条数上限（与 MAX_SESSIONS_PER_GROUP 同风格的安全阀）
MAX_MESSAGES_PER_TESTSET = 100


def _clean_rule(rule: Any) -> dict | None:
    """清洗断言规则：dict 保留，其余（含 None）归一为 None。"""
    if not isinstance(rule, dict):
        return None
    return rule


class TestsetStore:
    """测试集的创建、持久化与清洗。

    数据保存到 data 目录下 `virtual_session/testsets.json`（与 groups.json 同
    目录），全量写 JSON。
    """

    # 类名以 Test 开头会触发 pytest 收集，显式标记为非测试类
    __test__ = False

    def __init__(self, data_dir: Path | None = None) -> None:
        base = Path(get_astrbot_plugin_data_path()) if data_dir is None else data_dir
        self._dir = base / "virtual_session"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._file = self._dir / "testsets.json"
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
                return testsets
        return []

    def _save(self) -> None:
        self._file.write_text(
            json.dumps({"testsets": self._testsets}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @staticmethod
    def _normalize_messages(messages: list[dict]) -> list[dict]:
        """清洗消息列表：text 去首尾空白，空文本丢弃，rule 归一为 dict 或 None。

        可选的 sender_id / sender_name（消息级测试身份）为非空字符串时保留；
        可选的 auto_at（消息级是否模拟「@机器人」发言）为 bool 时保留，缺省
        为 True（发送时再决定）。
        """
        out: list[dict] = []
        for item in messages:
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            message: dict = {"text": text, "rule": _clean_rule(item.get("rule"))}
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
        self, name: str, messages: list[dict], batch_ranges: Any = None
    ) -> dict:
        normalized = self._normalize_messages(messages)
        testset = {
            "id": f"ts_{uuid.uuid4().hex[:8]}",
            "name": str(name or "").strip() or "测试集",
            "created_at": int(time.time()),
            "messages": normalized,
            "batch_ranges": self._normalize_batch_ranges(batch_ranges, len(normalized)),
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
