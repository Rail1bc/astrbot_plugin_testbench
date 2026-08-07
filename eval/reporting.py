"""默认报告模板聚合（纯函数，零配置即聚合）。

评审产物统一为类型化指标 JSON，按 run 收集合并即得报告数据。聚合方式按
指标类型机械进行（类型已由 reviewer profile 声明，不运行时推断）：

- number → count / avg / min / max；
- enum → 各分类计数（按声明枚举或 distinct 值）；
- bool → 通过数 / 总数 / 通过率；
- text → 不进总览（仅详情）。

error / invalid 评审失败记录不计入聚合，单列 ``review_failures``（评审失败
≠ 不通过，混淆会把报告数据污染成「测试失败」的假象）。
"""

from __future__ import annotations

import copy
from typing import Any


def _collect_verdict(
    verdict: Any, per_key: dict[str, dict], review_failures: list[int]
) -> None:
    """把单条 verdict 的指标并入聚合桶；error/invalid 计入评审失败。"""
    if not isinstance(verdict, dict):
        return
    status = verdict.get("status")
    if status in ("error", "invalid"):
        review_failures[0] += 1
        return
    if status != "ok":
        return
    for m in verdict.get("metrics") or []:
        if not isinstance(m, dict):
            continue
        key = m.get("key")
        mtype = m.get("type")
        if not key or not mtype:
            continue
        bucket = per_key.setdefault(key, {"type": mtype, "values": []})
        bucket["values"].append(m.get("value"))


def _aggregate_bucket(key: str, bucket: dict) -> dict | None:
    """按指标类型机械聚合一个指标的取值桶；text 不产出聚合条目。"""
    mtype = bucket["type"]
    values = bucket["values"]
    if mtype == "number":
        nums = [
            v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)
        ]
        if not nums:
            return None
        total = sum(nums)
        return {
            "type": "number",
            "count": len(nums),
            "avg": round(total / len(nums), 4),
            "min": min(nums),
            "max": max(nums),
        }
    if mtype == "enum":
        counts: dict[str, int] = {}
        for v in values:
            if isinstance(v, str):
                counts[v] = counts.get(v, 0) + 1
        if not counts:
            return None
        return {"type": "enum", "counts": counts, "total": sum(counts.values())}
    if mtype == "bool":
        total = len(values)
        passed = sum(1 for v in values if v is True)
        return {
            "type": "bool",
            "pass": passed,
            "total": total,
            "rate": round(passed / total, 4) if total else 0,
        }
    return None  # text 不进总览


def build_metrics_summary(run: dict) -> dict:
    """聚合运行的全部 verdict 指标（消息级 + final_verdicts）为总览摘要。

    返回 ``{"review_failures": N, "metrics": {key: {...}}}``。review_failures
    统计 error / invalid 评审失败条数（含 final 级）；同一指标 key 跨级合并。
    """
    per_key: dict[str, dict] = {}
    review_failures = [0]
    for step in run.get("steps") or []:
        for result in step.get("results") or []:
            for verdict in result.get("verdicts") or []:
                _collect_verdict(verdict, per_key, review_failures)
    for final in run.get("final_verdicts") or []:
        for entry in final.get("results") or []:
            _collect_verdict(entry.get("verdict"), per_key, review_failures)
    metrics: dict[str, dict] = {}
    for key, bucket in per_key.items():
        agg = _aggregate_bucket(key, bucket)
        if agg is not None:
            metrics[key] = agg
    return {"review_failures": review_failures[0], "metrics": metrics}


def build_report_data(run: dict) -> dict:
    """组装报告数据（运行终态快照）：run 元数据 + 深拷贝产物 + 总览聚合。

    报告一经生成不再变化（run dict 之后随内存清理消失），steps / sessions /
    final_verdicts 必须深拷贝，否则已发布报告会漂移成运行后续状态。
    """
    return {
        "run_id": run["run_id"],
        "testset_id": run["testset_id"],
        "testset_name": run.get("testset_name"),
        "status": run.get("status"),
        "started_at": run.get("started_at"),
        "finished_at": run.get("finished_at"),
        "sessions": copy.deepcopy(run.get("sessions") or []),
        "steps": copy.deepcopy(run.get("steps") or []),
        "final_verdicts": copy.deepcopy(run.get("final_verdicts") or []),
        "metrics_summary": build_metrics_summary(run),
    }
