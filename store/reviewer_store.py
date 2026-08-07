"""LLM 评审 profile 数据模型与持久化。

Reviewer profile 定义一次 LLM 评审：Provider / 模型（**测试集级显式配置**，
避免用被测模型自评）、用户编写的审查提示词（支持占位符）、输出契约
（metrics 声明——类型必须配置声明，不能运行时推断，报告聚合依赖它）。

    {"id": "rp_<uuid8>", "name", "note"?, "provider_id", "model",
     "system_prompt", "context": "reply|record|slice",
     "metrics": [{"key", "type", "enum_values"?, "pass_threshold"?,
                  "pass_categories"?}], "created_at"}

支持多个 profile：消息规则 / 最终断言按 profile_id 引用；存储层不强制唯一。
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


def _load_profiles(file: Path) -> list[dict]:
    """读取 ``{"profiles": [...]}`` 结构的 JSON 文件，损坏时返回空列表。"""
    if not file.exists():
        return []
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(data, dict) and isinstance(data.get("profiles"), list):
        return [p for p in data["profiles"] if isinstance(p, dict)]
    return []


class ReviewerStore(AsyncWriteMixin):
    """LLM 评审 profile 的创建、持久化与查询（全量写 JSON，同 identity_store 模式）。

    同步写方法保持同步签名，由 API 层经 ``write``（实例锁内线程化）执行，
    避免事件循环阻塞与并发写竞态。
    """

    # 类名以 Review 开头不触发 pytest 收集（非 Test 前缀），显式标记更稳
    __test__ = False

    def __init__(self, data_dir: Path | None = None) -> None:
        base = Path(get_astrbot_plugin_data_path()) if data_dir is None else data_dir
        directory = base / "virtual_session"
        directory.mkdir(parents=True, exist_ok=True)
        self._file = directory / "reviewers.json"
        self._lock = asyncio.Lock()
        self._profiles: list[dict] = _load_profiles(self._file)

    def _save(self) -> None:
        self._file.write_text(
            json.dumps({"profiles": self._profiles}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def list_profiles(self) -> list[dict]:
        return list(self._profiles)

    def get_profile(self, profile_id: str) -> dict | None:
        for profile in self._profiles:
            if profile["id"] == profile_id:
                return profile
        return None

    def create_profile(self, fields: dict) -> dict:
        profile = {
            "id": f"rp_{uuid.uuid4().hex[:8]}",
            "name": str(fields.get("name") or "").strip() or "评审",
            "note": str(fields.get("note") or "").strip() or None,
            "provider_id": str(fields.get("provider_id") or "").strip() or None,
            "model": str(fields.get("model") or "").strip() or None,
            "system_prompt": fields.get("system_prompt"),
            "context": fields.get("context") or "reply",
            "metrics": fields.get("metrics") or [],
            "created_at": int(time.time()),
        }
        self._profiles.append(profile)
        self._save()
        return profile

    def update_profile(self, profile_id: str, fields: dict) -> dict | None:
        """更新 profile；未传字段（None）保持不变。"""
        current = self.get_profile(profile_id)
        if current is None:
            return None
        updated: dict[str, Any] = {}
        if "name" in fields:
            updated["name"] = str(fields.get("name") or "").strip() or "评审"
        if "note" in fields:
            updated["note"] = str(fields.get("note") or "").strip() or None
        if "provider_id" in fields:
            updated["provider_id"] = (
                str(fields.get("provider_id") or "").strip() or None
            )
        if "model" in fields:
            updated["model"] = str(fields.get("model") or "").strip() or None
        if "system_prompt" in fields:
            updated["system_prompt"] = fields.get("system_prompt")
        if "context" in fields:
            updated["context"] = fields.get("context") or "reply"
        if "metrics" in fields:
            updated["metrics"] = fields.get("metrics") or []
        current.update(updated)
        self._save()
        return current

    def delete_profiles(self, ids: list[str]) -> int:
        id_set = set(ids)
        kept = [p for p in self._profiles if p["id"] not in id_set]
        removed = len(self._profiles) - len(kept)
        if removed:
            self._profiles = kept
            self._save()
        return removed
