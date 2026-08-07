"""LLM 评审（reviewer）：profile 输出契约校验、提示词占位符、评审调用与 verdict。

评审层把「机械规则恒 bool」与「LLM 评审 JSON 指标」统一为类型化 verdict：

    {"rule_index": int, "status": "ok" | "error" | "invalid",
     "pass": bool | None, "metrics": [{"key", "type", "value"}], "detail": str | None}

- ok：评审成功；metrics 为按契约校验后的类型化指标，pass 只在 ok 上派生。
- error：评审调用失败（Provider 缺失 / 调用异常）。
- invalid：调用成功但输出不是合法 JSON / 不符合声明的输出契约。
- 评审失败 ≠ 评审结果为不通过：error / invalid 不计入聚合，报告单列「评审失败 N」。

profile 输出契约声明（指标类型必须配置声明、不能运行时推断——报告模板要算
avg/min/max 就必须知道哪个字段是数字）：

    {"provider_id", "model"?, "system_prompt", "context": "reply|record|slice",
     "metrics": [{"key", "type": "number|enum|text",
                  "enum_values"?, "pass_threshold"?, "pass_categories"?}]}

``model`` 可省略（评审 Profile 只配 Provider 即可，省略时调用评审 LLM 传
``model=None`` 使用 Provider 当前模型）；旧数据保留的显式 model 仍生效。

system_prompt 支持占位符 ``{{metrics}}``（自动展开为逐字段取值要求 + 示例的
简明输出契约描述，无需用户在提示词里手工维护）；也支持
``{{agent_system_prompt}}``（被测 agent 的装饰后系统提示词，由用户在评审
提示词中自行编排；未捕获时为空串）。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from .mechanical import parse_json_value

_METRIC_TYPES = ("number", "enum", "text")
_CONTEXT_MODES = ("reply", "record", "slice")


def validate_profile(profile: dict) -> list[str]:
    """校验 reviewer profile 的输出契约；返回错误列表（空 = 合法）。"""
    errors: list[str] = []
    if not isinstance(profile.get("name"), str) or not profile["name"].strip():
        errors.append("name 必填")
    if (
        not isinstance(profile.get("provider_id"), str)
        or not profile["provider_id"].strip()
    ):
        errors.append("provider_id 必填")
    # model 可选：省略时评审用 Provider 当前模型（`call_reviewer` 传 None）
    if (
        not isinstance(profile.get("system_prompt"), str)
        or not profile["system_prompt"].strip()
    ):
        errors.append("system_prompt 必填")
    context = profile.get("context")
    if context is not None and context not in _CONTEXT_MODES:
        errors.append(f"context 只能是 {' / '.join(_CONTEXT_MODES)}")
    metrics = profile.get("metrics")
    if not isinstance(metrics, list) or not metrics:
        errors.append("metrics 至少需要一个指标")
        return errors
    seen: set[str] = set()
    for i, m in enumerate(metrics):
        label = f"metrics[{i}]"
        if not isinstance(m, dict):
            errors.append(f"{label} 必须是对象")
            continue
        key = m.get("key")
        if not isinstance(key, str) or not key.strip():
            errors.append(f"{label}.key 必填")
            continue
        if key in seen:
            errors.append(f"{label}.key {key!r} 重复")
        seen.add(key)
        mtype = m.get("type")
        if mtype not in _METRIC_TYPES:
            errors.append(f"{label}.type 只能是 {' / '.join(_METRIC_TYPES)}")
        if mtype == "enum":
            values = m.get("enum_values")
            if values is not None and (
                not isinstance(values, list)
                or not all(isinstance(v, str) and v for v in values)
            ):
                errors.append(f"{label}.enum_values 必须是字符串列表")
            cats = m.get("pass_categories")
            if cats is not None and (
                not isinstance(cats, list)
                or not all(isinstance(c, str) and c for c in cats)
            ):
                errors.append(f"{label}.pass_categories 必须是字符串列表")
        threshold = m.get("pass_threshold")
        if threshold is not None and (
            isinstance(threshold, bool) or not isinstance(threshold, (int, float))
        ):
            errors.append(f"{label}.pass_threshold 必须是数字")
    return errors


def expand_prompt(template: str, ctx: Mapping[str, str]) -> str:
    """展开提示词占位符 ``{{key}}``；未提供的占位符保留原样（不静默吞掉）。"""

    def _repl(match: re.Match[str]) -> str:
        key = match.group(1).strip()
        return ctx.get(key, match.group(0))

    return re.sub(r"\{\{\s*(\w+)\s*\}\}", _repl, template or "")


def metrics_contract_description(metrics: list[dict]) -> str:
    """输出契约的简明描述（喂给 LLM 的输出要求，经 {{metrics}} 展开）。

    逐指标列出 key / 取值要求 / 通过判定并附示例输出——不直接转储完整
    schema：enum_values / pass_categories 等 schema 键名会诱导 LLM 回显
    契约本身（原始返回 = 契约 schema）而不是输出实际评估值。
    """
    lines: list[str] = []
    example: dict[str, Any] = {}
    for i, m in enumerate(metrics):
        key = m["key"]
        mtype = m.get("type")
        note = ""
        if mtype == "number":
            spec = "数字"
            threshold = m.get("pass_threshold")
            if isinstance(threshold, (int, float)) and not isinstance(threshold, bool):
                note = f"≥ {threshold} 判为通过"
                example[key] = threshold
            else:
                example[key] = 0
        elif mtype == "enum":
            values = m.get("enum_values") or []
            spec = " | ".join(json.dumps(v, ensure_ascii=False) for v in values)
            if not spec:
                spec = "字符串"
            cats = m.get("pass_categories")
            if isinstance(cats, list) and cats:
                note = f"取 {'、'.join(json.dumps(c, ensure_ascii=False) for c in cats)} 判为通过"
            example[key] = cats[0] if cats else (values[0] if values else "")
        else:  # text
            spec = "字符串"
            example[key] = "..."
        line = f'  "{key}": {spec}'
        if i < len(metrics) - 1:
            line += ","
        if note:
            line += f"   // {note}"
        lines.append(line)
    return (
        "请只输出一个 JSON 对象，格式如下：\n"
        + "{\n"
        + "\n".join(lines)
        + "\n}\n\n示例输出："
        + json.dumps(example, ensure_ascii=False)
    )


def derive_pass(metric_def: dict, value: Any) -> bool | None:
    """按指标声明派生 pass：number → 阈值，enum → 分类集合；未声明 → None。"""
    mtype = metric_def.get("type")
    if mtype == "number":
        threshold = metric_def.get("pass_threshold")
        if isinstance(threshold, (int, float)) and not isinstance(threshold, bool):
            return (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and value >= threshold
            )
        return None
    if mtype == "enum":
        cats = metric_def.get("pass_categories")
        if isinstance(cats, list) and cats:
            return value in cats
        return None
    return None


def validate_metrics(
    parsed: Any, metrics: list[dict]
) -> tuple[list[dict] | None, str | None]:
    """按契约校验并归一化 LLM 输出指标；返回 (metrics, error)。

    校验是轻校验（类型 + 声明枚举成员），不搞运行时推断——聚合方式是机械的，
    前提是类型已由 profile 声明。
    """
    if not isinstance(parsed, dict):
        return None, "评审输出不是 JSON 对象"
    out: list[dict] = []
    for m in metrics:
        key = m["key"]
        if key not in parsed:
            return None, f"评审输出缺少指标 {key!r}"
        value = parsed[key]
        mtype = m.get("type")
        if mtype == "number":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return None, f"指标 {key!r} 应为数字"
        elif mtype == "enum":
            if not isinstance(value, str):
                return None, f"指标 {key!r} 应为字符串"
            enum_values = m.get("enum_values")
            if (
                isinstance(enum_values, list)
                and enum_values
                and value not in enum_values
            ):
                return None, f"指标 {key!r} 取值 {value!r} 不在声明枚举内"
        elif mtype == "text":
            if not isinstance(value, str):
                return None, f"指标 {key!r} 应为文本"
        out.append({"key": key, "type": mtype, "value": value})
    return out, None


async def call_reviewer(
    context,
    profile: dict,
    context_text: str,
    agent_system_prompt: str | None = None,
) -> tuple[list[dict] | None, str | None, str | None, str]:
    """调用评审 LLM 并解析校验输出。返回 (metrics, error, status, raw)。

    status ∈ ("error" | "invalid" | None)；metrics 仅在 ok（status=None）时非空。
    raw 为评审 LLM 的原始返回文本（error 无输出时为空串）。

    agent_system_prompt：被测 agent 的（装饰后）系统提示词，供 ``{{agent_system_prompt}}``
    占位符展开；未捕获时为空串（展开 ctx 恒含该键，避免字面量占位符残留）。
    """
    provider = None
    provider_id = profile.get("provider_id")
    if isinstance(provider_id, str) and provider_id:
        provider = context.get_provider_by_id(provider_id)
    if provider is None:
        return None, "未找到评审 Provider", "error", ""
    system_prompt = expand_prompt(
        profile.get("system_prompt") or "",
        {
            "metrics": metrics_contract_description(profile.get("metrics") or []),
            "agent_system_prompt": agent_system_prompt or "",
        },
    )
    try:
        resp = await provider.text_chat(
            prompt=context_text,
            system_prompt=system_prompt,
            model=profile.get("model") or None,
        )
    except Exception as e:  # noqa: BLE001
        return None, f"评审调用失败: {e}", "error", ""
    text = getattr(resp, "completion_text", None) or ""
    parsed = parse_json_value(text)
    if parsed is None:
        return None, "评审输出不是合法 JSON", "invalid", text
    metrics, err = validate_metrics(parsed, profile.get("metrics") or [])
    if err is not None:
        return None, err, "invalid", text
    return metrics, None, None, text


def mechanical_verdict(rule_index: int, result: dict | None) -> dict:
    """机械规则 verdict：{pass, detail} 包装为类型化指标（bool 指标）。

    profile_id 恒 None（机械规则不走 LLM，报告评审重试按它跳过机械 verdict）。
    """
    if result is None:
        return {
            "rule_index": rule_index,
            "status": "error",
            "pass": None,
            "metrics": [],
            "detail": "规则未评估（数据损坏）",
            "raw": None,
            "context_text": None,
            "profile_id": None,
        }
    return {
        "rule_index": rule_index,
        "status": "ok",
        "pass": bool(result["pass"]),
        "metrics": [{"key": "pass", "type": "bool", "value": bool(result["pass"])}],
        "detail": result.get("detail"),
        "raw": None,
        "context_text": None,
        "profile_id": None,
    }


def llm_verdict(
    rule_index: int,
    metrics: list[dict] | None,
    error: str | None,
    status: str | None,
    profile: dict,
    raw: str | None = None,
    context_text: str | None = None,
    agent_system_prompt: str | None = None,
) -> dict:
    """LLM 规则 verdict：ok 时按契约派生 pass，error / invalid 时 pass 为 None。

    raw 为评审 LLM 原始返回文本，context_text 为评审时喂给 LLM 的上下文，
    两者供前端详情查看（error 无输出时为空串 / None）；profile_id 供报告
    评审重试按 id 解析当前 profile 重新调用；agent_system_prompt 随 verdict
    存储，使报告评审重试自包含（重跑时仍能展开 ``{{agent_system_prompt}}``）。
    """
    if status is not None or error is not None:
        return {
            "rule_index": rule_index,
            "status": status or "error",
            "pass": None,
            "metrics": [],
            "detail": error or "评审失败",
            "raw": raw,
            "context_text": context_text,
            "profile_id": profile.get("id"),
            "agent_system_prompt": agent_system_prompt,
        }
    values = {m["key"]: m["value"] for m in metrics or []}
    passed: list[bool] = []
    for metric_def in profile.get("metrics") or []:
        derived = derive_pass(metric_def, values.get(metric_def["key"]))
        if derived is not None:
            passed.append(derived)
    return {
        "rule_index": rule_index,
        "status": "ok",
        "pass": all(passed) if passed else None,
        "metrics": metrics or [],
        "detail": None,
        "raw": raw,
        "context_text": context_text,
        "profile_id": profile.get("id"),
        "agent_system_prompt": agent_system_prompt,
    }


async def retry_llm_verdict(
    context, profile: dict, verdict: dict
) -> tuple[dict, str | None]:
    """用 verdict 里存储的评审上下文重跑一条 LLM 评审。

    返回 (新 verdict, error)。error 非空表示无法重试（如未存评审上下文），
    新 verdict 原样返回；重跑本身失败（error / invalid）不在 error 报出——
    失败就是重试的结果，由调用方按新 verdict 的 status 呈现。
    """
    context_text = verdict.get("context_text")
    if not context_text:
        return verdict, "该评审未保存上下文，无法重试"
    agent_system_prompt = verdict.get("agent_system_prompt")
    metrics, error, status, raw = await call_reviewer(
        context,
        profile,
        context_text,
        agent_system_prompt=agent_system_prompt,
    )
    return (
        llm_verdict(
            verdict.get("rule_index", 0),
            metrics,
            error,
            status,
            profile,
            raw=raw,
            context_text=context_text,
            agent_system_prompt=agent_system_prompt,
        ),
        None,
    )
