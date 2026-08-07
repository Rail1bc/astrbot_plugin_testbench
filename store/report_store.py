"""测试集运行报告持久化。

报告是**持久化产物**而非内存暂存：与测试集关联、随测试集删除级联删除，
不受运行记录内存清理（DONE_RUN_KEEP_SECONDS / STALE_RUN_TIMEOUT）约束。

    {"report_id": "rp_<uuid8>", "testset_id": "ts_...", "run_id": "tr_...",
     "created_at": int, "data": RunReportData}

data 为运行终态快照（deepcopy）：run 元数据 + sessions/steps/final_verdicts +
metrics_summary（默认模板聚合，见 eval/reporting.py）。生成后默认不再变化，
唯一例外是「评审重试」（retry_report_reviews）整体替换 data 以刷新 verdicts
与聚合（见 api/reports.py）。
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path

from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

from ._base import AsyncWriteMixin


def _load_reports(file: Path) -> list[dict]:
    """读取 ``{"reports": [...]}`` 结构的 JSON 文件，损坏时返回空列表。"""
    if not file.exists():
        return []
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(data, dict) and isinstance(data.get("reports"), list):
        return [r for r in data["reports"] if isinstance(r, dict)]
    return []


class ReportStore(AsyncWriteMixin):
    """测试集运行报告的创建、查询与级联删除（全量写 JSON，同 reviewer_store 模式）。

    同步写方法保持同步签名，由 API 层 / 运行器经 ``write``（实例锁内线程化）
    执行，避免事件循环阻塞与并发写竞态。
    """

    # 类名以 Report 开头不触发 pytest 收集，显式标记更稳
    __test__ = False

    def __init__(self, data_dir: Path | None = None) -> None:
        base = Path(get_astrbot_plugin_data_path()) if data_dir is None else data_dir
        directory = base / "virtual_session"
        directory.mkdir(parents=True, exist_ok=True)
        self._file = directory / "reports.json"
        self._lock = asyncio.Lock()
        self._reports: list[dict] = _load_reports(self._file)

    def _save(self) -> None:
        self._file.write_text(
            json.dumps({"reports": self._reports}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def list_reports(self, testset_id: str | None = None) -> list[dict]:
        """列出报告（可选按测试集过滤），按创建时间倒序。"""
        reports = self._reports
        if testset_id:
            reports = [r for r in reports if r.get("testset_id") == testset_id]
        return sorted(reports, key=lambda r: r.get("created_at") or 0, reverse=True)

    def get_report(self, report_id: str) -> dict | None:
        for report in self._reports:
            if report["id"] == report_id:
                return report
        return None

    def add_report(self, testset_id: str, run_id: str, data: dict) -> dict:
        report = {
            "id": f"rp_{uuid.uuid4().hex[:8]}",
            "testset_id": testset_id,
            "run_id": run_id,
            "created_at": int(time.time()),
            "data": data,
        }
        self._reports.append(report)
        self._save()
        return report

    def update_report(self, report_id: str, data: dict) -> dict | None:
        """整体替换报告的 data（评审重试后刷新 verdicts 与聚合）；不存在返回 None。"""
        for report in self._reports:
            if report["id"] == report_id:
                report["data"] = data
                self._save()
                return report
        return None

    def delete_reports(self, ids: list[str]) -> int:
        """删除指定 id 的报告；返回删除数量。"""
        id_set = set(ids)
        kept = [r for r in self._reports if r["id"] not in id_set]
        removed = len(self._reports) - len(kept)
        if removed:
            self._reports = kept
            self._save()
        return removed

    def delete_for_testsets(self, testset_ids: list[str]) -> int:
        """级联删除指定测试集产出的全部报告；返回删除数量。"""
        id_set = set(testset_ids)
        kept = [r for r in self._reports if r.get("testset_id") not in id_set]
        removed = len(self._reports) - len(kept)
        if removed:
            self._reports = kept
            self._save()
        return removed
