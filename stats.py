"""耗时统计工具（纯函数）。"""

from __future__ import annotations

import math


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
