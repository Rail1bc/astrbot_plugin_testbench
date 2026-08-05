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
                return [t for t in data["testsets"] if isinstance(t, dict)]
        return []

    def _save(self) -> None:
        self._file.write_text(
            json.dumps({"testsets": self._testsets}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @staticmethod
    def _normalize_messages(messages: list[dict]) -> list[dict]:
        """清洗消息列表：text 去首尾空白，空文本丢弃，rule 归一为 dict 或 None。"""
        out: list[dict] = []
        for item in messages:
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            out.append({"text": text, "rule": _clean_rule(item.get("rule"))})
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

    def create_testset(self, name: str, messages: list[dict]) -> dict:
        testset = {
            "id": f"ts_{uuid.uuid4().hex[:8]}",
            "name": str(name or "").strip() or "测试集",
            "created_at": int(time.time()),
            "messages": self._normalize_messages(messages),
        }
        self._testsets.append(testset)
        self._save()
        return testset

    def update_testset(
        self, testset_id: str, name: str, messages: list[dict]
    ) -> dict | None:
        testset = self.get_testset(testset_id)
        if testset is None:
            return None
        testset["name"] = str(name or "").strip() or "测试集"
        testset["messages"] = self._normalize_messages(messages)
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
