"""回复断言规则评估（纯函数）。

对 LLM 回复应用断言规则（正则 / 包含 / 格式等），返回 pass/detail。纯函数、
无副作用，与 stats.py 同风格，便于单测。规则 schema：

    {"type": "contains", "value": "str | [str]"}   全部出现
    {"type": "not_contains", "value": ...}         全部不出现
    {"type": "regex", "value": "pattern"}          re.search
    {"type": "json"}                               宽松判定为合法 JSON（剥代码块
                                                   围栏；直接解析失败则取首个
                                                   开括号到末个闭括号的子串再试，
                                                   容忍思维链 / 说明文本夹带）
    {"type": "non_empty"}                          strip 后非空
    {"type": "min_len", "value": int}
    {"type": "max_len", "value": int}
    {"type": "prefix", "value": "str"}
    {"type": "suffix", "value": "str"}
"""

from __future__ import annotations

import json
import re
from typing import Any

_VALUE_TYPES = {
    "contains",
    "not_contains",
    "regex",
    "min_len",
    "max_len",
    "prefix",
    "suffix",
}
_NO_VALUE_TYPES = {"json", "non_empty"}


def evaluate_rule(rule: dict | None, reply: str) -> dict | None:
    """评估断言规则。rule 为 None 时返回 None（无断言）。"""
    if rule is None:
        return None
    reply = reply or ""
    rule_type = rule.get("type")
    if rule_type in _NO_VALUE_TYPES:
        return _evaluate_no_value(rule_type, reply)
    if rule_type in _VALUE_TYPES:
        return _evaluate_value(rule_type, rule.get("value"), reply)
    return {
        "pass": False,
        "detail": f"未知断言类型: {rule_type!r}",
    }


def _no(detail: str) -> dict:
    return {"pass": False, "detail": detail}


def _yes(detail: str) -> dict:
    return {"pass": True, "detail": detail}


def _evaluate_no_value(rule_type: str, reply: str) -> dict:
    if rule_type == "json":
        return (
            _yes("回复是合法 JSON")
            if _parse_json_reply(reply)
            else _no("回复不是合法的 JSON")
        )
    if rule_type == "non_empty":
        return _yes("回复非空") if reply.strip() else _no("回复为空")
    return _no(f"未知断言类型: {rule_type!r}")


def parse_json_value(text: str) -> Any:
    """宽松解析文本中的 JSON 对象 / 数组，返回解析结果；失败返回 None。

    LLM 输出常把 JSON 包进 markdown 代码块围栏、或在前后夹带思维链 / 说明
    文本（如 AstrBot 开启思维链显示时，回复链头会被装饰阶段注入
    「🤔 思考: …」前缀）。换行缩进不影响 json.loads；先剥围栏直接解析，
    失败再取首个开括号到末个闭括号的子串解析。
    """
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)
    try:
        return json.loads(text)
    except ValueError:
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start >= 0 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except ValueError:
                pass
    return None


def _parse_json_reply(reply: str) -> bool:
    """宽松判定回复是否为（或包含）合法 JSON（见 parse_json_value）。"""
    return parse_json_value(reply) is not None


def _evaluate_value(rule_type: str, value: Any, reply: str) -> dict:
    if rule_type == "contains":
        needles = value if isinstance(value, list) else [value]
        if not needles:
            return _no("包含断言缺少 value")
        missing = [n for n in needles if not isinstance(n, str) or n not in reply]
        if missing:
            return _no(f"回复不包含 {missing!r}")
        return _yes(f"回复包含 {needles if len(needles) > 1 else needles[0]!r}")

    if rule_type == "not_contains":
        needles = value if isinstance(value, list) else [value]
        if not needles:
            return _no("不包含断言缺少 value")
        found = [n for n in needles if isinstance(n, str) and n in reply]
        if found:
            return _no(f"回复包含不应出现的内容 {found!r}")
        return _yes(f"回复不包含 {needles if len(needles) > 1 else needles[0]!r}")

    if rule_type == "regex":
        if not isinstance(value, str) or not value:
            return _no("正则断言缺少 value")
        try:
            pattern = re.compile(value)
        except re.error as e:
            return _no(f"正则无效: {e}")
        return (
            _yes(f"回复匹配正则 {value!r}")
            if pattern.search(reply)
            else _no(f"回复不匹配正则 {value!r}")
        )

    if rule_type == "min_len":
        if not isinstance(value, int) or isinstance(value, bool):
            return _no("最少字数断言 value 必须是整数")
        if len(reply) < value:
            return _no(f"回复长度 {len(reply)} 少于 {value}")
        return _yes(f"回复长度 {len(reply)} ≥ {value}")

    if rule_type == "max_len":
        if not isinstance(value, int) or isinstance(value, bool):
            return _no("最多字数断言 value 必须是整数")
        if len(reply) > value:
            return _no(f"回复长度 {len(reply)} 超过 {value}")
        return _yes(f"回复长度 {len(reply)} ≤ {value}")

    if rule_type == "prefix":
        if not isinstance(value, str) or not value:
            return _no("前缀断言缺少 value")
        return (
            _yes(f"回复以 {value!r} 开头")
            if reply.startswith(value)
            else _no(f"回复不以 {value!r} 开头")
        )

    if rule_type == "suffix":
        if not isinstance(value, str) or not value:
            return _no("后缀断言缺少 value")
        return (
            _yes(f"回复以 {value!r} 结尾")
            if reply.endswith(value)
            else _no(f"回复不以 {value!r} 结尾")
        )

    return _no(f"未知断言类型: {rule_type!r}")
