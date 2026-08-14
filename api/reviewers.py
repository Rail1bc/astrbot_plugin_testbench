"""LLM 评审 profile 接口。

Reviewer profile 是评审层 LLM 规则的配置实体（provider / 模型 / 提示词 /
输出契约）。支持多个 profile，由消息规则 / 最终断言按 profile_id 引用；
更新时按「现有 + 增量」合并后再校验（部分更新不能只校验传入字段，否则
可把 profile 改坏）。
"""

from __future__ import annotations

from astrbot.api.web import error_response, json_response

from ..eval.reviewer import metrics_contract_description, validate_profile
from .common import json_dict, validate_id_list

# profile 可更新字段（白名单；id / created_at 不可改）
_PROFILE_KEYS = (
    "name",
    "note",
    "provider_id",
    "model",
    "system_prompt",
    "context",
    "metrics",
)


def _candidate_from(payload: dict) -> dict:
    """从 payload 构建用于校验的候选 profile（只取白名单键）。"""
    return {k: payload.get(k) for k in _PROFILE_KEYS}


class ReviewersAPI:
    """LLM 评审 profile handler 集合（挂在 Star 上，共享 self.reviewer_store）。"""

    async def list_reviewers(self):
        """列出全部评审 profile。"""
        return json_response({"reviewers": self.reviewer_store.list_profiles()})

    async def create_reviewer(self):
        """创建评审 profile（校验输出契约；支持多个，按 id 引用）。"""
        payload = await json_dict()
        if payload is None:
            return error_response("请求体必须是 JSON 对象", status_code=400)
        candidate = _candidate_from(payload)
        errors = validate_profile(candidate)
        if errors:
            return error_response("；".join(errors), status_code=400)
        profile = await self.reviewer_store.write(
            self.reviewer_store.create_profile, candidate
        )
        return json_response(profile)

    async def update_reviewer(self, reviewer_id: str):
        """更新评审 profile（只更新传入字段；合并后校验输出契约）。"""
        payload = await json_dict()
        if payload is None:
            return error_response("请求体必须是 JSON 对象", status_code=400)
        existing = self.reviewer_store.get_profile(reviewer_id)
        if existing is None:
            return error_response("未找到该评审 profile", status_code=404)
        merged = {
            **existing,
            **{k: v for k, v in payload.items() if k in _PROFILE_KEYS},
        }
        errors = validate_profile(merged)
        if errors:
            return error_response("；".join(errors), status_code=400)
        updated = await self.reviewer_store.write(
            self.reviewer_store.update_profile, reviewer_id, payload
        )
        return json_response(updated)

    async def preview_reviewer_metrics(self):
        """预览 {{metrics}} 占位符展开后的输出契约描述（不落库）。

        直接复用评审运行时的 `metrics_contract_description`，保证表单预览与
        实际展开字节级一致（前端不镜像该格式化逻辑，避免双份实现漂移）。
        预览容忍半成品行：缺 key 的行丢弃（保存时 validate_profile 会拦截，
        这里只展示「已填好的合法行」展开成什么样）。
        """
        payload = await json_dict()
        if payload is None:
            return error_response("请求体必须是 JSON 对象", status_code=400)
        metrics = payload.get("metrics")
        if not isinstance(metrics, list):
            return error_response("metrics 必须是列表", status_code=400)
        valid = [
            m
            for m in metrics
            if isinstance(m, dict)
            and isinstance(m.get("key"), str)
            and m["key"].strip()
        ]
        return json_response({"description": metrics_contract_description(valid)})

    async def delete_reviewers(self):
        """删除评审 profile。"""
        payload = await json_dict()
        if payload is None:
            return error_response("请求体必须是 JSON 对象", status_code=400)
        ids = validate_id_list(payload.get("ids"))
        if ids is None:
            return error_response("ids 须为非空字符串列表", status_code=400)
        deleted = await self.reviewer_store.write(
            self.reviewer_store.delete_profiles, ids
        )
        return json_response({"deleted": deleted})
