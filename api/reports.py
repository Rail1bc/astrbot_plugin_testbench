"""测试集运行报告接口。

报告是持久化产物（report_store）：与测试集关联、随测试集删除级联删除，
不受内存运行记录清理约束。报告数据为运行终态快照（含 metrics_summary
默认模板聚合），列出 / 删除均按 report id 或 testset id 操作；「评审重试」
按报告内 verdict 存储的 profile_id + context_text 重新调用评审 LLM，
整体替换报告 data（同一份报告，verdicts 与聚合被刷新）；「LLM 报告」按
测试集级 report_llm 配置调用 Provider 生成 markdown 报告并落 data.llm_report。
"""

from __future__ import annotations

import json
import time

from astrbot.api.web import error_response, json_response

from ..eval.reporting import (
    build_assertion_stats,
    build_duration_stats,
    build_metrics_summary,
)
from ..eval.reviewer import retry_llm_verdict
from .common import json_dict, validate_id_list


def _iter_verdict_locators(data: dict):
    """遍历报告数据的每条 verdict，产出 (locator, verdict)。

    locator 为报告内定位：消息级 ``{kind:"m", step, session_id, verdict}``
    （step 为步骤索引、verdict 为结果内 verdict 序号）、跨轮级
    ``{kind:"f", rule, session_id}``（rule 为 final_rule 索引）。
    """
    for si, step in enumerate(data.get("steps") or []):
        for result in step.get("results") or []:
            for vi, v in enumerate(result.get("verdicts") or []):
                yield (
                    {
                        "kind": "m",
                        "step": si,
                        "session_id": result.get("session_id"),
                        "verdict": vi,
                    },
                    v,
                )
    for fi, final in enumerate(data.get("final_verdicts") or []):
        for entry in final.get("results") or []:
            v = entry.get("verdict")
            if isinstance(v, dict):
                yield (
                    {
                        "kind": "f",
                        "rule": fi,
                        "session_id": entry.get("session_id"),
                    },
                    v,
                )


class ReportsAPI:
    """测试集运行报告 handler 集合（挂在 Star 上，共享 self.report_store）。"""

    async def list_reports(self, testset_id: str):
        """列出指定测试集的全部报告（按创建时间倒序，含完整数据）。"""
        return json_response(
            {"reports": self.report_store.list_reports(testset_id=testset_id)}
        )

    async def delete_reports(self):
        """删除报告（按 report id）。"""
        payload = await json_dict()
        if payload is None:
            return error_response("请求体必须是 JSON 对象", status_code=400)
        ids = validate_id_list(payload.get("ids"))
        if ids is None:
            return error_response("ids 须为非空字符串列表", status_code=400)
        deleted = await self.report_store.write(self.report_store.delete_reports, ids)
        return json_response({"deleted": deleted})

    async def retry_report_reviews(self, report_id: str):
        """重试报告中的 LLM 评审，返回更新后的报告。

        payload 二选一：``{"scope": "failed"|"all"}`` 批量（failed 只重跑
        error/invalid 评审，all 重跑全部 LLM 评审）或 ``{"targets": [locator]}``
        单条（评审详情弹窗的重试按钮）。机械 verdict（profile_id 为 None）与
        未存上下文 / profile 已删除的 verdict 跳过并计入 failed。重试后重建
        metrics_summary 并整体替换报告 data。
        """
        payload = await json_dict()
        if payload is None:
            return error_response("请求体必须是 JSON 对象", status_code=400)
        scope = payload.get("scope")
        targets = payload.get("targets")
        if scope not in ("failed", "all") and not isinstance(targets, list):
            return error_response(
                "需要 scope(failed|all) 或 targets 列表", status_code=400
            )
        report = self.report_store.get_report(report_id)
        if report is None:
            return error_response("报告不存在", status_code=404)
        data = report["data"]
        if not isinstance(data, dict):
            return error_response("报告数据损坏", status_code=400)
        profiles = {
            p["id"]: p
            for p in (
                self.reviewer_store.list_profiles() if self.reviewer_store else []
            )
        }
        target_keys = None
        if isinstance(targets, list):
            try:
                target_keys = {
                    (
                        t.get("kind"),
                        t.get("step"),
                        t.get("rule"),
                        t.get("session_id"),
                        t.get("verdict"),
                    )
                    for t in targets
                    if isinstance(t, dict)
                }
            except TypeError:
                # 元素字段为 dict/list 等不可哈希值时元组不可哈希 → 500，
                # 与 ids 元素校验同口径，入口拦截
                return error_response(
                    "targets 元素须为含字符串/整数字段的对象", status_code=400
                )

        def _wanted(locator: dict, verdict: dict) -> bool:
            if target_keys is not None:
                key = (
                    locator["kind"],
                    locator.get("step"),
                    locator.get("rule"),
                    locator.get("session_id"),
                    locator.get("verdict"),
                )
                if key not in target_keys:
                    return False
            if scope == "failed":
                return verdict.get("status") in ("error", "invalid")
            if scope == "all":
                return bool(verdict.get("profile_id"))
            return True

        updated = 0
        failed = 0
        errors: list[dict] = []
        for locator, verdict in _iter_verdict_locators(data):
            if not _wanted(locator, verdict):
                continue
            profile = profiles.get(verdict.get("profile_id"))
            if profile is None:
                failed += 1
                errors.append(
                    {
                        **locator,
                        "error": f"找不到评审 profile {verdict.get('profile_id')!r}",
                    }
                )
                continue
            new_verdict, err = await retry_llm_verdict(self.context, profile, verdict)
            if err is not None:
                failed += 1
                errors.append({**locator, "error": err})
                continue
            verdict.clear()
            verdict.update(new_verdict)
            updated += 1
        data["metrics_summary"] = build_metrics_summary(data)
        data["assertions"] = build_assertion_stats(data)
        data["durations"] = build_duration_stats(data)
        await self.report_store.write(self.report_store.update_report, report_id, data)
        return json_response(
            {"updated": updated, "failed": failed, "errors": errors, "report": data}
        )

    async def generate_llm_report(self, report_id: str):
        """为报告生成 LLM 报告，返回更新后的报告数据（data.llm_report 落库）。

        报告 LLM 是**测试集级持久化配置**（report_llm：Provider + 生成提示词，
        缺省模型用 Provider 当前模型，与评审 profile 一致）。报告数据整体作为
        prompt 传给 Provider（JSON 转义，确保中文可读）；成功 →
        ``data.llm_report = {status:"ok", text, provider_id, model, generated_at}``
        并 ``update_report`` 持久化（重新生成覆盖旧产物）；失败 → error_response
        （不落库，报告保持原样）。
        """
        report = self.report_store.get_report(report_id)
        if report is None:
            return error_response("报告不存在", status_code=404)
        data = report["data"]
        if not isinstance(data, dict):
            return error_response("报告数据损坏", status_code=400)
        testset = None
        if self.testset_store is not None:
            testset = self.testset_store.get_testset(data.get("testset_id"))
        report_llm = (testset or {}).get("report_llm")
        if not isinstance(report_llm, dict) or not report_llm.get("provider_id"):
            return error_response("该测试集未配置报告 LLM", status_code=400)
        provider = self.context.get_provider_by_id(report_llm["provider_id"])
        if provider is None:
            return error_response("找不到报告 Provider", status_code=400)
        try:
            resp = await provider.text_chat(
                prompt=json.dumps(data, ensure_ascii=False, indent=2),
                system_prompt=report_llm.get("system_prompt") or None,
                model=report_llm.get("model") or None,
            )
        except Exception as e:  # noqa: BLE001
            return error_response(f"报告生成失败: {e}", status_code=400)
        text = getattr(resp, "completion_text", None) or ""
        data["llm_report"] = {
            "status": "ok",
            "text": text,
            "provider_id": report_llm["provider_id"],
            "model": report_llm.get("model"),
            "generated_at": int(time.time()),
        }
        await self.report_store.write(self.report_store.update_report, report_id, data)
        return json_response(data)
