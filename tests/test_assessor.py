"""评估层测试：mechanical 规则、LLM 评审契约、Assessor 组合/短路/材料构造、
人格回退、报告聚合纯函数。"""

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
# 插件模块用相对导入（from .group_store import ...），必须以包形式加载。
# 与 AstrBot 在 data/plugins 下加载插件的方式一致：把插件根目录的父目录加入
# sys.path，以 namespace package（astrbot_plugin_testbench）导入。
sys.path.insert(0, str(REPO_ROOT.parent))

pytest.importorskip("astrbot")

import astrbot_plugin_testbench.core.virtual_event as ve_mod  # noqa: E402
import astrbot_plugin_testbench.eval.assessor as assr_mod  # noqa: E402
import astrbot_plugin_testbench.eval.mechanical as asrt_mod  # noqa: E402
import astrbot_plugin_testbench.eval.persona as psn_mod  # noqa: E402
import astrbot_plugin_testbench.eval.reporting as rpt_mod  # noqa: E402
import astrbot_plugin_testbench.eval.reviewer as rev_mod  # noqa: E402
import astrbot_plugin_testbench.main as main_mod  # noqa: E402
import astrbot_plugin_testbench.store.testset_store as tss_mod  # noqa: E402

Assessor = assr_mod.Assessor
TestsetStore = tss_mod.TestsetStore
VirtualMessageEvent = ve_mod.VirtualMessageEvent
build_assertion_stats = rpt_mod.build_assertion_stats
build_duration_stats = rpt_mod.build_duration_stats
build_input_text = assr_mod.build_input_text
build_metrics_summary = rpt_mod.build_metrics_summary
build_report_data = rpt_mod.build_report_data
call_reviewer = rev_mod.call_reviewer
derive_pass = rev_mod.derive_pass
evaluate_rule = asrt_mod.evaluate_rule
expand_prompt = rev_mod.expand_prompt
format_record = assr_mod.format_record
format_turn = assr_mod.format_turn
inject_system_prompt_block = assr_mod.inject_system_prompt_block
llm_verdict = rev_mod.llm_verdict
mechanical_verdict = rev_mod.mechanical_verdict
metrics_contract_description = rev_mod.metrics_contract_description
retry_llm_verdict = rev_mod.retry_llm_verdict
validate_metrics = rev_mod.validate_metrics
validate_profile = rev_mod.validate_profile

from fakes import (  # noqa: E402
    FakeContext,
    FakeLLMProvider,
    FakePersonaManager,
    _valid_profile,
    make_session,
)


def test_assertion_rule_none():
    assert evaluate_rule(None, "任意回复") is None


def test_assertion_contains_and_not_contains():
    assert evaluate_rule({"type": "contains", "value": "你好"}, "早上好，你好")["pass"]
    assert not evaluate_rule({"type": "contains", "value": "再见"}, "早上好")["pass"]
    assert evaluate_rule({"type": "contains", "value": ["a", "b"]}, "a b c")["pass"]
    assert not evaluate_rule({"type": "contains", "value": ["a", "d"]}, "a b c")["pass"]
    assert evaluate_rule({"type": "not_contains", "value": "脏话"}, "干净文本")["pass"]
    assert not evaluate_rule({"type": "not_contains", "value": "脏话"}, "带脏话")[
        "pass"
    ]
    # 空 value 列表视为数据损坏 → 不静默通过
    assert not evaluate_rule({"type": "not_contains", "value": []}, "x")["pass"]


def test_assertion_regex():
    assert evaluate_rule({"type": "regex", "value": r"\d+"}, "abc123")["pass"]
    assert not evaluate_rule({"type": "regex", "value": r"\d+"}, "abc")["pass"]
    # 无效 pattern → pass False（可见，不静默通过）
    assert not evaluate_rule({"type": "regex", "value": "["}, "x")["pass"]
    # 缺 value
    assert not evaluate_rule({"type": "regex"}, "x")["pass"]


def test_assertion_json_and_non_empty():
    assert evaluate_rule({"type": "json"}, '{"a": 1}')["pass"]
    assert not evaluate_rule({"type": "json"}, "not json")["pass"]
    assert not evaluate_rule({"type": "non_empty"}, "   ")["pass"]
    assert evaluate_rule({"type": "non_empty"}, "有内容")["pass"]


def test_assertion_json_lenient_extraction():
    """json 断言须容忍 LLM 常见包装：换行缩进 / 思维链前缀 / 说明文本 / 代码块围栏。"""
    # 换行缩进本身合法（json.loads 接受空白），直接解析即过
    assert evaluate_rule(
        {"type": "json"},
        '{\n  "a": 1,\n  "b": 2\n}',
    )["pass"]
    # 思维链前缀（AstrBot 开启思维链显示时回复链头会被装饰阶段注入）
    assert evaluate_rule({"type": "json"}, '🤔 思考: 先想一下\n\n────\n{"a": 1}')[
        "pass"
    ]
    # 前后说明文本
    assert evaluate_rule({"type": "json"}, '好的，结果如下：\n{"a": 1}')["pass"]
    assert evaluate_rule({"type": "json"}, '{"a": 1} 以上。')["pass"]
    # markdown 代码块围栏（带/不带语言标记）
    assert evaluate_rule({"type": "json"}, '```json\n{"a": 1}\n```')["pass"]
    assert evaluate_rule({"type": "json"}, '```\n{"a": 1}\n```')["pass"]
    # 数组兜底提取
    assert evaluate_rule({"type": "json"}, "结果是 [1, 2, 3]")["pass"]
    # 仍须拒绝：纯文本 / 花括号只是占位符 / 两个 JSON 对象拼在一起
    assert not evaluate_rule({"type": "json"}, "不是 json")["pass"]
    assert not evaluate_rule({"type": "json"}, "模板是 {name} 这样")["pass"]
    assert not evaluate_rule({"type": "json"}, '{"a": 1} 和 {"b": 2}')["pass"]


def test_assertion_len_prefix_suffix():
    assert evaluate_rule({"type": "min_len", "value": 3}, "你好啊")["pass"]
    assert not evaluate_rule({"type": "max_len", "value": 2}, "你好啊")["pass"]
    assert evaluate_rule({"type": "prefix", "value": "你好"}, "你好世界")["pass"]
    assert evaluate_rule({"type": "suffix", "value": "世界"}, "你好世界")["pass"]
    assert not evaluate_rule({"type": "suffix", "value": "不是"}, "你好世界")["pass"]
    # value 类型错误 / 缺失 → pass False
    assert not evaluate_rule({"type": "min_len", "value": "3"}, "x")["pass"]
    assert not evaluate_rule({"type": "prefix"}, "x")["pass"]


def test_assertion_unknown_type_and_missing_value():
    assert not evaluate_rule({"type": "nope"}, "x")["pass"]
    assert not evaluate_rule({"type": "contains"}, "x")["pass"]
    assert not evaluate_rule({"type": "min_len"}, "x")["pass"]


def test_validate_profile_ok_and_errors():
    assert validate_profile(_valid_profile()) == []
    # model 可选：省略即用 Provider 当前模型（评审 Profile 只配 Provider）
    nomodel = _valid_profile()
    del nomodel["model"]
    assert validate_profile(nomodel) == []
    errors = validate_profile({})
    assert "name 必填" in errors
    assert "provider_id 必填" in errors
    assert "system_prompt 必填" in errors
    assert "metrics 至少需要一个指标" in errors

    p = _valid_profile()
    p["context"] = "bad"
    assert validate_profile(p) == ["context 只能是 reply / record / slice"]

    p2 = _valid_profile()
    p2["metrics"] = [
        {"key": "a", "type": "number"},
        {"key": "a", "type": "text"},
    ]
    assert any("重复" in e for e in validate_profile(p2))

    p3 = _valid_profile()
    p3["metrics"] = [{"key": "a", "type": "enum", "enum_values": "x"}]
    assert validate_profile(p3)  # enum_values 须为字符串列表

    p4 = _valid_profile()
    p4["metrics"] = [{"key": "a", "type": "number", "pass_threshold": True}]
    assert validate_profile(p4)  # 阈值须为数字（非 bool）


def test_expand_prompt_placeholders():
    assert expand_prompt("{{metrics}} 好", {"metrics": "M"}) == "M 好"
    assert expand_prompt("未知 {{missing}}", {"metrics": "M"}) == "未知 {{missing}}"
    assert expand_prompt("{{ metrics }} 带空格", {"metrics": "M"}) == "M 带空格"
    assert expand_prompt(None, {}) == ""


def test_metrics_contract_description():
    # {{metrics}} 展开为逐字段取值要求 + 示例，不直接转储 schema——
    # schema 键名（enum_values / pass_categories）会诱导 LLM 回显契约
    metrics = [
        {
            "key": "身份",
            "type": "enum",
            "enum_values": ["一致", "不一致"],
            "pass_categories": ["一致"],
        },
        {"key": "性格", "type": "enum", "enum_values": ["一致", "不一致"]},
        {"key": "语气", "type": "number", "pass_threshold": 3},
        {"key": "建议", "type": "text"},
    ]
    desc = metrics_contract_description(metrics)
    assert desc.startswith("请只输出一个 JSON 对象，格式如下：")
    assert '"身份": "一致" | "不一致"' in desc
    assert '取 "一致" 判为通过' in desc
    assert '"语气": 数字' in desc
    assert "enum_values" not in desc and "pass_categories" not in desc
    assert (
        '示例输出：{"身份": "一致", "性格": "一致", "语气": 3, "建议": "..."}' in desc
    )


def test_derive_pass():
    assert derive_pass({"type": "number", "pass_threshold": 80}, 90) is True
    assert derive_pass({"type": "number", "pass_threshold": 80}, 70) is False
    assert derive_pass({"type": "number"}, 5) is None  # 未声明阈值 → None
    assert derive_pass({"type": "enum", "pass_categories": ["好"]}, "好") is True
    assert derive_pass({"type": "enum", "pass_categories": ["好"]}, "差") is False
    assert derive_pass({"type": "enum"}, "好") is None
    assert derive_pass({"type": "text"}, "任意") is None


def test_validate_metrics_contract():
    metrics = [{"key": "score", "type": "number"}]
    out, err = validate_metrics({"score": 88}, metrics)
    assert err is None and out == [{"key": "score", "type": "number", "value": 88}]

    assert validate_metrics("不是对象", metrics)[1] == "评审输出不是 JSON 对象"
    assert "缺少指标" in (validate_metrics({}, metrics)[1] or "")
    assert "应为数字" in (validate_metrics({"score": "高"}, metrics)[1] or "")

    enum_metrics = [{"key": "level", "type": "enum", "enum_values": ["好", "差"]}]
    assert "不在声明枚举内" in (
        validate_metrics({"level": "中"}, enum_metrics)[1] or ""
    )


@pytest.mark.asyncio
async def test_call_reviewer_ok_and_statuses():
    profile = _valid_profile()
    provider = FakeLLMProvider("prov_r", responses=['{"score": 88, "level": "好"}'])
    context = FakeContext(providers=[provider])
    metrics, error, status, raw = await call_reviewer(context, profile, "回复文本")
    assert status is None and error is None
    assert metrics == [
        {"key": "score", "type": "number", "value": 88},
        {"key": "level", "type": "enum", "value": "好"},
    ]
    # raw 保留评审 LLM 的原始返回文本
    assert raw == '{"score": 88, "level": "好"}'
    # 提示词展开：system_prompt 里 {{metrics}} 被替换为契约 JSON 描述
    assert "score" in provider.calls[0]["system_prompt"]

    # 无 model 的 profile → text_chat model=None（评审用 Provider 当前模型）
    nomodel = _valid_profile()
    nomodel["model"] = None
    nm_provider = FakeLLMProvider("prov_r", responses=['{"score": 90, "level": "好"}'])
    await call_reviewer(FakeContext(providers=[nm_provider]), nomodel, "x")
    assert nm_provider.calls[0]["model"] is None

    # 未找到评审 Provider → error（无输出，raw 为空串）
    metrics, error, status, raw = await call_reviewer(FakeContext(), profile, "x")
    assert status == "error" and "未找到评审 Provider" in error
    assert raw == ""

    # 输出不是合法 JSON → invalid（raw 保留原文——正是要看的）
    bad = FakeLLMProvider("prov_r", responses=["不是 JSON"])
    _, error, status, raw = await call_reviewer(
        FakeContext(providers=[bad]), profile, "x"
    )
    assert status == "invalid" and "不是合法 JSON" in error
    assert raw == "不是 JSON"

    # 调用异常 → error（无输出，raw 为空串）
    boom = FakeLLMProvider("prov_r", raise_on_call=True)
    _, error, status, raw = await call_reviewer(
        FakeContext(providers=[boom]), profile, "x"
    )
    assert status == "error" and "评审调用失败" in error
    assert raw == ""

    # JSON 对象但缺声明指标 → invalid（raw 保留原文）
    missing = FakeLLMProvider("prov_r", responses=['{"score": 88}'])
    _, error, status, raw = await call_reviewer(
        FakeContext(providers=[missing]), profile, "x"
    )
    assert status == "invalid" and "缺少指标" in error
    assert raw == '{"score": 88}'


def test_mechanical_verdict():
    v = mechanical_verdict(0, {"pass": True, "detail": "x"})
    assert v["status"] == "ok" and v["pass"] is True
    assert v["metrics"] == [{"key": "pass", "type": "bool", "value": True}]
    # 机械规则无 LLM 原始输出 / 评审上下文，profile_id 恒 None（不参与评审重试）
    assert v["raw"] is None and v["context_text"] is None
    assert v["profile_id"] is None
    v2 = mechanical_verdict(1, None)
    assert v2["status"] == "error" and v2["pass"] is None
    assert v2["raw"] is None and v2["context_text"] is None
    assert v2["profile_id"] is None


def test_llm_verdict():
    profile = _valid_profile()
    metrics = [
        {"key": "score", "type": "number", "value": 90},
        {"key": "level", "type": "enum", "value": "好"},
    ]
    v = llm_verdict(
        0,
        metrics,
        None,
        None,
        profile,
        raw='{"score": 90, "level": "好"}',
        context_text="第 1 步: 你好",
    )
    assert v["status"] == "ok" and v["pass"] is True
    assert v["raw"] == '{"score": 90, "level": "好"}'
    assert v["context_text"] == "第 1 步: 你好"
    # profile_id 取自 profile 定义（报告评审重试按它解析当前 profile）
    assert v["profile_id"] == profile["id"]

    v2 = llm_verdict(1, None, "调用失败", "error", profile, raw="", context_text="x")
    assert v2["status"] == "error" and v2["pass"] is None
    assert v2["detail"] == "调用失败"
    assert v2["raw"] == "" and v2["context_text"] == "x"
    assert v2["profile_id"] == profile["id"]

    # 无 pass 派生的指标（enum 无 pass_categories）不参与 all-pass
    no_cats = {
        **profile,
        "metrics": [
            {"key": "score", "type": "number", "pass_threshold": 80},
            {"key": "level", "type": "enum"},
        ],
    }
    v3 = llm_verdict(0, metrics, None, None, no_cats)
    assert v3["status"] == "ok" and v3["pass"] is True


@pytest.mark.asyncio
async def test_retry_llm_verdict():
    """重跑一条存储的评审 verdict：用存储的 context_text 重新喂给评审 LLM。"""
    provider = FakeLLMProvider("prov_r", responses=['{"score": 88, "level": "好"}'])
    context = FakeContext(providers=[provider])
    profile = _valid_profile()
    verdict = {
        "rule_index": 0,
        "status": "error",
        "pass": None,
        "metrics": [],
        "detail": "评审调用失败: boom",
        "raw": "",
        "context_text": "第 1 步: 你好",
        "profile_id": "rp_test",
    }
    new, err = await retry_llm_verdict(context, profile, verdict)
    assert err is None
    assert new["status"] == "ok" and new["pass"] is True
    assert new["metrics"] == [
        {"key": "score", "type": "number", "value": 88},
        {"key": "level", "type": "enum", "value": "好"},
    ]
    assert new["context_text"] == verdict["context_text"]
    assert new["profile_id"] == "rp_test"
    # 重试时喂给评审 LLM 的 prompt 即存储的评审上下文
    assert provider.calls[0]["prompt"] == "第 1 步: 你好"

    # 未存上下文 → 无法重试（原样返回 + error）
    v2 = {**verdict, "context_text": None}
    new2, err2 = await retry_llm_verdict(context, profile, v2)
    assert err2 is not None
    assert new2 is v2

    # 重跑再次失败 → 失败即结果（不在 error 报出，按新 verdict 的 status 呈现）
    boom = FakeLLMProvider("prov_r", raise_on_call=True)
    new3, err3 = await retry_llm_verdict(
        FakeContext(providers=[boom]), profile, verdict
    )
    assert err3 is None
    assert new3["status"] == "error" and new3["pass"] is None


@pytest.mark.asyncio
async def test_assessor_step_mechanical_short_circuit():
    """机械规则未通过 → 同步骤后续 LLM 规则跳过（短路，不调评审 LLM）。"""
    provider = FakeLLMProvider("prov_r", responses=['{"score": 88}'])
    context = FakeContext(providers=[provider])
    assessor = Assessor(context, {"rp_test": _valid_profile()})
    steps = [
        {
            "status": "done",
            "text": "q",
            "rules": [
                {"type": "contains", "value": "不存在"},
                {"kind": "llm", "profile_id": "rp_test", "context": "reply"},
            ],
            "results": [{"session_id": "vs_1", "reply": "回复", "status": "ok"}],
        }
    ]
    final_verdicts = await assessor.assess(steps, [], [{"id": "vs_1"}])
    assert final_verdicts == []
    result = steps[0]["results"][0]
    assert len(result["verdicts"]) == 1  # 短路的 LLM 规则不产生 verdict
    assert result["verdicts"][0]["status"] == "ok"
    assert result["verdicts"][0]["pass"] is False
    assert provider.calls == []  # 评审 LLM 未被调用


@pytest.mark.asyncio
async def test_assessor_step_llm_ok_with_context_modes():
    """LLM 规则：context=record 时评审上下文为格式化对话记录而非单条回复。"""
    provider = FakeLLMProvider("prov_r", responses=['{"score": 90, "level": "好"}'])
    context = FakeContext(providers=[provider])
    assessor = Assessor(context, {"rp_test": _valid_profile()})
    steps = [
        {
            "status": "done",
            "text": "问",
            "rules": [{"kind": "llm", "profile_id": "rp_test", "context": "record"}],
            "results": [{"session_id": "vs_1", "reply": "回答", "status": "ok"}],
        }
    ]
    await assessor.assess(steps, [], [])
    verdict = steps[0]["results"][0]["verdicts"][0]
    assert verdict["status"] == "ok" and verdict["pass"] is True
    prompt = provider.calls[0]["prompt"]
    # 未捕获系统提示词 → 注入块占位文案仍存在，多轮记录随后
    assert prompt.startswith(
        "【以下是被测 Agent 系统提示词】\n（未捕获到被测 agent 系统提示词）\n"
        "【以上是被测 Agent 系统提示词】\n\n"
    )
    assert "第 1 步:" in prompt
    # 结构化评审材料：中文标签块标注身份与输入/输出分界
    assert "【输入 · user（测试台）】\n问" in prompt
    assert "【输出 · agent（virtual_bot）】\n回答" in prompt
    # 评审输入（喂给 LLM 的上下文）与评审输出（LLM 原始返回）随 verdict 落盘
    assert verdict["context_text"] == prompt
    assert verdict["raw"] == '{"score": 90, "level": "好"}'


@pytest.mark.asyncio
async def test_assessor_message_rule_slice_range():
    """LLM 规则 context=slice：slice_range 限定评审记录区间；缺省回退全部。

    消息规则在第 3 步，记录为第 1-3 步；slice_range 区间列表（前端多段输入
    3-4,10-12 解析产物）只喂对应步给评审 LLM；不配 slice_range 时与 record
    等效（全部记录）。_slice_entries 边界钳制同 _scope_indices（越界裁剪 /
    倒序段跳过 / 形状非法回退原样），列表形式与旧单段 dict 都支持。
    """

    def build_steps(rule: dict) -> list[dict]:
        return [
            {
                "status": "done",
                "text": "q1",
                "rules": [],
                "results": [{"session_id": "vs_1", "reply": "r1", "status": "ok"}],
            },
            {
                "status": "done",
                "text": "q2",
                "rules": [],
                "results": [{"session_id": "vs_1", "reply": "r2", "status": "ok"}],
            },
            {
                "status": "done",
                "text": "q3",
                "rules": [rule],
                "results": [{"session_id": "vs_1", "reply": "r3", "status": "ok"}],
            },
        ]

    # 切片 [{1,1}] → 评审上下文只含第 2 步（列表形式，前端多段输入产物）
    provider = FakeLLMProvider("prov_r", responses=['{"score": 90, "level": "好"}'])
    assessor = Assessor(
        FakeContext(providers=[provider]), {"rp_test": _valid_profile()}
    )
    steps = build_steps(
        {
            "kind": "llm",
            "profile_id": "rp_test",
            "context": "slice",
            "slice_range": [{"from": 1, "to": 1}],
        }
    )
    await assessor.assess(steps, [], [{"id": "vs_1"}])
    verdict = steps[2]["results"][0]["verdicts"][0]
    assert verdict["status"] == "ok" and verdict["pass"] is True
    # format_record 在切片后重新编号（从「第 1 步:」起），故按内容断言只含第 2 步
    prompt = provider.calls[0]["prompt"]
    assert "q2" in prompt and "r2" in prompt
    assert "q1" not in prompt and "q3" not in prompt

    # 多段 [{0,0},{2,2}] → 只含第 1 步与第 3 步（q2 排除）
    provider3 = FakeLLMProvider("prov_r", responses=['{"score": 90, "level": "好"}'])
    assessor3 = Assessor(
        FakeContext(providers=[provider3]), {"rp_test": _valid_profile()}
    )
    steps3 = build_steps(
        {
            "kind": "llm",
            "profile_id": "rp_test",
            "context": "slice",
            "slice_range": [{"from": 0, "to": 0}, {"from": 2, "to": 2}],
        }
    )
    await assessor3.assess(steps3, [], [{"id": "vs_1"}])
    prompt3 = provider3.calls[0]["prompt"]
    assert "q1" in prompt3 and "r1" in prompt3
    assert "q3" in prompt3 and "r3" in prompt3
    assert "q2" not in prompt3

    # 缺省（无 slice_range）→ 回退全部记录（与 record 等效）
    provider2 = FakeLLMProvider("prov_r", responses=['{"score": 90, "level": "好"}'])
    assessor2 = Assessor(
        FakeContext(providers=[provider2]), {"rp_test": _valid_profile()}
    )
    steps2 = build_steps({"kind": "llm", "profile_id": "rp_test", "context": "slice"})
    await assessor2.assess(steps2, [], [{"id": "vs_1"}])
    prompt2 = provider2.calls[0]["prompt"]
    assert "第 1 步:" in prompt2 and "第 2 步:" in prompt2
    assert "第 3 步:" in prompt2

    # _slice_entries 边界钳制：越界裁剪 / 倒序段跳过 / 形状非法回退原样；
    # 列表形式与旧单段 dict 都支持
    entries = ["a", "b", "c"]
    assert Assessor._slice_entries(entries, [{"from": 1, "to": 1}]) == ["b"]
    assert Assessor._slice_entries(
        entries, [{"from": 0, "to": 0}, {"from": 2, "to": 2}]
    ) == ["a", "c"]
    assert Assessor._slice_entries(
        entries, [{"from": 1, "to": 1}, {"from": 2, "to": 2}]
    ) == ["b", "c"]
    assert Assessor._slice_entries(entries, [{"from": 3, "to": 1}]) == []
    assert Assessor._slice_entries(entries, [{"from": 1, "to": 1}, "bad"]) == entries
    assert Assessor._slice_entries(entries, {"from": -3, "to": 10}) == entries
    assert Assessor._slice_entries(entries, {"from": 3, "to": 1}) == []
    assert Assessor._slice_entries(entries, None) == entries
    assert Assessor._slice_entries(entries, "all") == entries


@pytest.mark.asyncio
async def test_assessor_llm_missing_profile():
    """LLM 规则引用不存在的 profile → error verdict（不抛异常）。"""
    assessor = Assessor(FakeContext(), {})
    steps = [
        {
            "status": "done",
            "text": "q",
            "rules": [{"kind": "llm", "profile_id": "rp_ghost", "context": "reply"}],
            "results": [{"session_id": "vs_1", "reply": "回复", "status": "ok"}],
        }
    ]
    await assessor.assess(steps, [], [])
    verdict = steps[0]["results"][0]["verdicts"][0]
    assert verdict["status"] == "error"
    assert "找不到评审 profile" in verdict["detail"]


@pytest.mark.asyncio
async def test_assessor_any_group_mechanical():
    """any 组合：任一子规则通过即通过；metrics 拼接全部已评估子叶。"""

    def step(rules: list[dict]) -> list[dict]:
        return [
            {
                "status": "done",
                "text": "q",
                "rules": rules,
                "results": [{"session_id": "vs_1", "reply": "ab cx", "status": "ok"}],
            }
        ]

    assessor = Assessor(FakeContext(), {"rp_test": _valid_profile()})
    # 部分通过：任一子叶通过 → 组通过，metrics 拼接两个子叶的 bool 指标
    steps = step(
        [
            {
                "op": "any",
                "rules": [
                    {"type": "contains", "value": "ab"},
                    {"type": "contains", "value": "zz"},
                ],
            }
        ]
    )
    await assessor.assess(steps, [], [{"id": "vs_1"}])
    v = steps[0]["results"][0]["verdicts"][0]
    assert v["rule_index"] == 0
    assert v["status"] == "ok" and v["pass"] is True
    assert v["metrics"] == [
        {"key": "pass", "type": "bool", "value": True},
        {"key": "pass", "type": "bool", "value": False},
    ]
    assert "任意（至少一条通过）：1/2 子规则通过" in v["detail"]

    # 全部不通过 → 组不通过
    steps2 = step([{"op": "any", "rules": [{"type": "contains", "value": "zz"}]}])
    await assessor.assess(steps2, [], [{"id": "vs_1"}])
    v2 = steps2[0]["results"][0]["verdicts"][0]
    assert v2["pass"] is False and "0/1 子规则通过" in v2["detail"]

    # 全部通过 → 组通过
    steps3 = step(
        [
            {
                "op": "any",
                "rules": [
                    {"type": "contains", "value": "ab"},
                    {"type": "non_empty"},
                ],
            }
        ]
    )
    await assessor.assess(steps3, [], [{"id": "vs_1"}])
    v3 = steps3[0]["results"][0]["verdicts"][0]
    assert v3["pass"] is True and "2/2 子规则通过" in v3["detail"]


@pytest.mark.asyncio
async def test_assessor_any_group_llm_short_circuit():
    """any 组内：机械子叶通过 → 后续 LLM 子叶跳过（不调评审 LLM）。"""
    provider = FakeLLMProvider("prov_r", responses=['{"score": 88, "level": "好"}'])
    assessor = Assessor(
        FakeContext(providers=[provider]), {"rp_test": _valid_profile()}
    )
    steps = [
        {
            "status": "done",
            "text": "q",
            "rules": [
                {
                    "op": "any",
                    "rules": [
                        {"type": "contains", "value": "ab"},
                        {"kind": "llm", "profile_id": "rp_test", "context": "reply"},
                    ],
                }
            ],
            "results": [{"session_id": "vs_1", "reply": "ab cx", "status": "ok"}],
        }
    ]
    await assessor.assess(steps, [], [{"id": "vs_1"}])
    verdicts = steps[0]["results"][0]["verdicts"]
    assert len(verdicts) == 1  # 组产 1 条 verdict
    v = verdicts[0]
    assert v["status"] == "ok" and v["pass"] is True
    assert v["metrics"] == [{"key": "pass", "type": "bool", "value": True}]
    assert provider.calls == []  # 组内 LLM 子叶被短路，未调用评审

    # 机械子叶未通过 → 组内 LLM 子叶仍评估（组未被决定为通过）
    provider2 = FakeLLMProvider("prov_r", responses=['{"score": 88, "level": "好"}'])
    assessor2 = Assessor(
        FakeContext(providers=[provider2]), {"rp_test": _valid_profile()}
    )
    steps2 = [
        {
            "status": "done",
            "text": "q",
            "rules": [
                {
                    "op": "any",
                    "rules": [
                        {"type": "contains", "value": "zz"},
                        {"kind": "llm", "profile_id": "rp_test", "context": "reply"},
                    ],
                }
            ],
            "results": [{"session_id": "vs_1", "reply": "ab cx", "status": "ok"}],
        }
    ]
    await assessor2.assess(steps2, [], [{"id": "vs_1"}])
    v2 = steps2[0]["results"][0]["verdicts"][0]
    assert v2["pass"] is True  # LLM 子叶评估并通过
    assert len(provider2.calls) == 1


@pytest.mark.asyncio
async def test_assessor_any_group_status_error_all_children_fail():
    """any 组全部子叶 error / invalid → 组 status error、pass None（评审失败单列）。"""
    steps = [
        {
            "status": "done",
            "text": "q",
            "rules": [
                {
                    "op": "any",
                    "rules": [
                        {"kind": "llm", "profile_id": "rp_ghost", "context": "reply"},
                        {"kind": "llm", "profile_id": "rp_ghost2", "context": "reply"},
                    ],
                }
            ],
            "results": [{"session_id": "vs_1", "reply": "回复", "status": "ok"}],
        }
    ]
    assessor = Assessor(FakeContext(), {})  # 无 profile → LLM 子叶 error
    await assessor.assess(steps, [], [])
    v = steps[0]["results"][0]["verdicts"][0]
    assert v["status"] == "error"
    assert v["pass"] is False  # 组合结果严格按「通过」判定：失败子叶视为未通过
    assert "0/2 子规则通过" in v["detail"]

    # 混合：机械子叶未通过（status ok）+ error LLM 子叶 → 组 status ok（非全部失败）
    steps2 = [
        {
            "status": "done",
            "text": "q",
            "rules": [
                {
                    "op": "any",
                    "rules": [
                        {"type": "contains", "value": "zz"},
                        {"kind": "llm", "profile_id": "rp_ghost", "context": "reply"},
                    ],
                }
            ],
            "results": [{"session_id": "vs_1", "reply": "ab cx", "status": "ok"}],
        }
    ]
    assessor2 = Assessor(FakeContext(), {})
    await assessor2.assess(steps2, [], [])
    v2 = steps2[0]["results"][0]["verdicts"][0]
    assert v2["status"] == "ok"  # 机械子叶 status ok → 组非全部失败
    assert v2["pass"] is False  # 两个子叶都未通过 → 组未通过
    assert "0/2 子规则通过" in v2["detail"]


@pytest.mark.asyncio
async def test_assessor_not_negation():
    """not 取反：pass 取反、metrics 保留、rule_index 为 entry 下标。"""
    assessor = Assessor(FakeContext(), {"rp_test": _valid_profile()})

    def step(rules: list[dict]) -> list[dict]:
        return [
            {
                "status": "done",
                "text": "q",
                "rules": rules,
                "results": [{"session_id": "vs_1", "reply": "ab cx", "status": "ok"}],
            }
        ]

    # 原规则通过 → 取反后未通过（机械子叶 detail 保留，带取反前缀）
    steps = step([{"op": "not", "rule": {"type": "contains", "value": "ab"}}])
    await assessor.assess(steps, [], [{"id": "vs_1"}])
    v = steps[0]["results"][0]["verdicts"][0]
    assert v["rule_index"] == 0
    assert v["status"] == "ok" and v["pass"] is False
    assert v["metrics"] == [{"key": "pass", "type": "bool", "value": True}]
    assert v["detail"] == "取反：回复包含 'ab'"

    # 原规则未通过 → 取反后通过（detail 带取反前缀）
    steps2 = step([{"op": "not", "rule": {"type": "contains", "value": "zz"}}])
    await assessor.assess(steps2, [], [{"id": "vs_1"}])
    v2 = steps2[0]["results"][0]["verdicts"][0]
    assert v2["pass"] is True
    assert v2["detail"] == "取反：回复不包含 ['zz']"


@pytest.mark.asyncio
async def test_assessor_not_llm_roundtrip():
    """not 内 LLM：取反 LLM 叶的 pass；error verdict 的 pass None 不取反。"""
    provider = FakeLLMProvider("prov_r", responses=['{"score": 88, "level": "好"}'])
    assessor = Assessor(
        FakeContext(providers=[provider]), {"rp_test": _valid_profile()}
    )
    steps = [
        {
            "status": "done",
            "text": "q",
            "rules": [
                {
                    "op": "not",
                    "rule": {
                        "kind": "llm",
                        "profile_id": "rp_test",
                        "context": "reply",
                    },
                }
            ],
            "results": [{"session_id": "vs_1", "reply": "回复", "status": "ok"}],
        }
    ]
    await assessor.assess(steps, [], [{"id": "vs_1"}])
    v = steps[0]["results"][0]["verdicts"][0]
    assert v["status"] == "ok"
    assert v["pass"] is False  # LLM 判定通过（score 88 ≥ 80）→ 取反未通过
    assert v["metrics"][0] == {"key": "score", "type": "number", "value": 88}
    assert v["context_text"] is not None  # 子 verdict 的评审上下文保留
    assert v["profile_id"] == "rp_test"
    assert (
        v["detail"] == "取反（原规则通过，取反后未通过）"
    )  # LLM ok 无 detail → 回退文案
    assert len(provider.calls) == 1
    assert provider.calls[0]["prompt"] == v["context_text"]

    # 评审失败（profile 缺失 → error）→ pass None 不取反
    steps2 = [
        {
            "status": "done",
            "text": "q",
            "rules": [
                {
                    "op": "not",
                    "rule": {
                        "kind": "llm",
                        "profile_id": "rp_ghost",
                        "context": "reply",
                    },
                }
            ],
            "results": [{"session_id": "vs_1", "reply": "回复", "status": "ok"}],
        }
    ]
    assessor2 = Assessor(FakeContext(), {})
    await assessor2.assess(steps2, [], [])
    v2 = steps2[0]["results"][0]["verdicts"][0]
    assert v2["status"] == "error" and v2["pass"] is None


@pytest.mark.asyncio
async def test_assessor_combo_skipped_when_top_level_short_circuit():
    """顶层短路：非 LLM entry 未通过 → 后续组合内 LLM 与直接 LLM 都跳过。"""
    provider = FakeLLMProvider("prov_r", responses=['{"score": 88, "level": "好"}'])
    assessor = Assessor(
        FakeContext(providers=[provider]), {"rp_test": _valid_profile()}
    )
    steps = [
        {
            "status": "done",
            "text": "q",
            "rules": [
                {"type": "contains", "value": "zz"},  # 未通过 → 短路
                {
                    "op": "not",
                    "rule": {
                        "kind": "llm",
                        "profile_id": "rp_test",
                        "context": "reply",
                    },
                },
                {"kind": "llm", "profile_id": "rp_test", "context": "reply"},
            ],
            "results": [{"session_id": "vs_1", "reply": "ab cx", "status": "ok"}],
        }
    ]
    await assessor.assess(steps, [], [{"id": "vs_1"}])
    verdicts = steps[0]["results"][0]["verdicts"]
    assert len(verdicts) == 1  # 组合与直接 LLM 都被短路
    assert verdicts[0]["status"] == "ok" and verdicts[0]["pass"] is False
    assert provider.calls == []


@pytest.mark.asyncio
async def test_assessor_any_group_under_top_level_short_circuit():
    """顶层短路下 any 组：LLM 子叶跳过、机械子叶仍评估。"""
    provider = FakeLLMProvider("prov_r", responses=['{"score": 88, "level": "好"}'])
    assessor = Assessor(
        FakeContext(providers=[provider]), {"rp_test": _valid_profile()}
    )
    steps = [
        {
            "status": "done",
            "text": "q",
            "rules": [
                {"type": "contains", "value": "zz"},  # 未通过 → 短路
                {
                    "op": "any",
                    "rules": [
                        {"kind": "llm", "profile_id": "rp_test", "context": "reply"},
                        {"type": "contains", "value": "ab"},
                    ],
                },
            ],
            "results": [{"session_id": "vs_1", "reply": "ab cx", "status": "ok"}],
        }
    ]
    await assessor.assess(steps, [], [{"id": "vs_1"}])
    verdicts = steps[0]["results"][0]["verdicts"]
    assert len(verdicts) == 2
    group = verdicts[1]
    assert group["pass"] is True  # 机械子叶仍评估并通过
    assert group["metrics"] == [{"key": "pass", "type": "bool", "value": True}]
    assert provider.calls == []  # 组内 LLM 子叶被跳过


@pytest.mark.asyncio
async def test_assessor_combo_tolerance_malformed():
    """空组 / 非 list rules / 非 dict 子规则 / 未知 op：容错不崩溃。"""
    assessor = Assessor(FakeContext(), {})
    steps = [
        {
            "status": "done",
            "text": "q",
            "rules": [
                {"op": "any", "rules": []},  # 空组 → 不产 verdict
                {"op": "any", "rules": "bad"},  # rules 非 list → 不产 verdict
                {"op": "not", "rule": "bad"},  # 子规则非 dict → 不产 verdict
                "坏数据",  # 非 dict entry → 不产 verdict、不短路
                {"op": "bogus", "rules": []},  # 未知 op → 按叶走机械未知类型兜底
            ],
            "results": [{"session_id": "vs_1", "reply": "回复", "status": "ok"}],
        }
    ]
    await assessor.assess(steps, [], [])
    verdicts = steps[0]["results"][0]["verdicts"]
    assert len(verdicts) == 1
    v = verdicts[0]
    assert v["status"] == "ok" and v["pass"] is False
    assert "未知断言类型" in v["detail"]


@pytest.mark.asyncio
async def test_assessor_llm_failure_does_not_short_circuit():
    """直接 LLM 叶 pass False 不触发短路（与现状一致）：后续 LLM 仍评估。"""
    provider = FakeLLMProvider(
        "prov_r",
        responses=['{"score": 10, "level": "差"}', '{"score": 88, "level": "好"}'],
    )
    assessor = Assessor(
        FakeContext(providers=[provider]), {"rp_test": _valid_profile()}
    )
    steps = [
        {
            "status": "done",
            "text": "q",
            "rules": [
                {"kind": "llm", "profile_id": "rp_test", "context": "reply"},
                {"kind": "llm", "profile_id": "rp_test", "context": "reply"},
            ],
            "results": [{"session_id": "vs_1", "reply": "回复", "status": "ok"}],
        }
    ]
    await assessor.assess(steps, [], [{"id": "vs_1"}])
    verdicts = steps[0]["results"][0]["verdicts"]
    assert len(verdicts) == 2
    assert verdicts[0]["pass"] is False and verdicts[1]["pass"] is True
    assert len(provider.calls) == 2


@pytest.mark.asyncio
async def test_assessor_final_rules_scope():
    """final_rules：按 scope 切片步骤评估，verdicts 存 run 级 final_verdicts。"""
    provider = FakeLLMProvider("prov_r", responses=['{"score": 90}'])
    context = FakeContext(providers=[provider])
    assessor = Assessor(context, {"rp_test": _valid_profile()})
    steps = [
        {
            "status": "done",
            "text": "q1",
            "rules": [],
            "results": [{"session_id": "vs_1", "reply": "r1", "status": "ok"}],
        },
        {
            "status": "done",
            "text": "q2",
            "rules": [],
            "results": [{"session_id": "vs_1", "reply": "r2", "status": "ok"}],
        },
        {
            "status": "done",
            "text": "q3",
            "rules": [],
            "results": [{"session_id": "vs_1", "reply": "r3", "status": "ok"}],
        },
    ]
    final_rules = [
        {
            "rule": {"kind": "llm", "profile_id": "rp_test", "context": "record"},
            "scope": {"from": 0, "to": 1},
        },
        {"rule": {"type": "contains", "value": "r3"}, "scope": "all"},
    ]
    out = await assessor.assess(steps, final_rules, [{"id": "vs_1"}])
    assert len(out) == 2
    # scope {0,1}：评审上下文只含前两步，最后一步不进
    assert out[0]["results"][0]["session_id"] == "vs_1"
    llm_prompt = provider.calls[0]["prompt"]
    assert "第 1 步:" in llm_prompt and "第 2 步:" in llm_prompt
    assert "第 3 步:" not in llm_prompt
    # 结构化评审材料：每轮带身份标注的输入/输出标签块
    assert llm_prompt.count("【输入 · user（测试台）】") == 2
    assert llm_prompt.count("【输出 · agent（virtual_bot）】") == 2
    # 机械 final rule（scope all）：全部步骤回复拼接后评估
    mech = out[1]
    assert mech["results"][0]["verdict"]["pass"] is True
    assert mech["results"][0]["verdict"]["metrics"][0]["value"] is True


def test_assessor_scope_indices_clamping():
    """_scope_indices 边界钳制：越界裁剪、倒序空、非 dict 回退全部。"""
    assert Assessor._scope_indices({"from": -3, "to": 10}, 5) == [0, 1, 2, 3, 4]
    assert Assessor._scope_indices({"from": 2, "to": 2}, 5) == [2]
    assert Assessor._scope_indices({"from": 3, "to": 1}, 5) == []
    assert Assessor._scope_indices("all", 3) == [0, 1, 2]


@pytest.mark.asyncio
async def test_assessor_final_rule_combo_falls_back_pass_false():
    """TB-30: final_rules 不支持组合——op:any 组合 final rule 走机械「未知断言
    类型」兜底 pass False（组合语义仅消息规则支持，final 保持单叶）。"""
    assessor = Assessor(FakeContext(), {})
    steps = [
        {
            "status": "done",
            "text": "q1",
            "rules": [],
            "results": [{"session_id": "vs_1", "reply": "r1", "status": "ok"}],
        }
    ]
    final_rules = [
        {
            "rule": {"op": "any", "rules": [{"type": "contains", "value": "r1"}]},
            "scope": "all",
        }
    ]
    out = await assessor.assess(steps, final_rules, [{"id": "vs_1"}])
    v = out[0]["results"][0]["verdict"]
    assert v["status"] == "ok"
    assert v["pass"] is False  # 未知断言类型兜底：不静默通过


@pytest.mark.framework_internal
def test_snapshot_llm_input_renders_strings():
    """实际输入快照：prompt + extra parts（TextPart / ThinkPart）+ system_prompt。

    直接依赖 AstrBot 内部模块（astrbot.core.agent.message，无版本契约），
    最低支持版矩阵下跳过（见 .github/workflows/pytest.yml）。
    """
    from astrbot.core.agent.message import TextPart, ThinkPart

    req = SimpleNamespace(
        prompt="装饰后的 prompt",
        extra_user_content_parts=[
            TextPart(text="<system_reminder>记住</system_reminder>"),
            ThinkPart(think="思考过程"),
        ],
        system_prompt="被测 agent 系统提示词",
    )
    snap = main_mod._snapshot_llm_input(req)
    assert snap == {
        "prompt": "装饰后的 prompt",
        "extra_parts": [
            "<system_reminder>记住</system_reminder>",
            "思考过程",
        ],
        "system_prompt": "被测 agent 系统提示词",
    }
    # 快照须为纯字符串（随 SSE / 报告 JSON 序列化），不是 ContentPart 引用
    assert all(isinstance(p, str) for p in snap["extra_parts"])
    # 防御式：缺字段的裸对象不抛异常（third_party 路径传裸 ProviderRequest）
    assert main_mod._snapshot_llm_input(SimpleNamespace()) == {
        "prompt": "",
        "extra_parts": [],
        "system_prompt": "",
    }


@pytest.mark.asyncio
@pytest.mark.framework_internal
async def test_plugin_on_llm_snapshots_actual_input():
    """on_llm hook 把实际输入快照写入事件 extra（评审材料的数据源）。

    直接依赖 AstrBot 内部模块（astrbot.core.agent.message，无版本契约），
    最低支持版矩阵下跳过（见 .github/workflows/pytest.yml）。
    """
    from astrbot.core.agent.message import TextPart

    queue = asyncio.Queue()
    plugin = main_mod.VirtualSessionPlugin(FakeContext(queue))
    await plugin.runner.start(sessions=[make_session(1)], text="hi")
    ev = queue.get_nowait()
    req = SimpleNamespace(
        prompt="装饰后 prompt",
        extra_user_content_parts=[
            TextPart(text="<system_reminder>x</system_reminder>")
        ],
        system_prompt="被测 SP",
    )
    await plugin.on_llm(ev, req)
    snap = ev.get_extra(ve_mod.TESTBENCH_LLM_INPUT_EXTRA_KEY)
    assert snap == {
        "prompt": "装饰后 prompt",
        "extra_parts": ["<system_reminder>x</system_reminder>"],
        "system_prompt": "被测 SP",
    }


def test_format_persona_snapshot():
    """人格快照文本：prompt + 开场对话；两者都空 → 空串。"""
    assert psn_mod.format_persona_snapshot({}) == ""
    persona = {
        "prompt": "你是寒露",
        "_begin_dialogs_processed": [
            {"role": "user", "content": "你好", "_no_save": True},
            {"role": "assistant", "content": "寒露在呢", "_no_save": True},
        ],
    }
    out = psn_mod.format_persona_snapshot(persona)
    assert out.startswith("# Persona Instructions\n\n你是寒露\n")
    assert "# 开场对话（begin_dialogs）\n\nuser: 你好\nassistant: 寒露在呢" in out
    # 只有开场对话（begin_dialogs 型人格）→ 只出开场对话段
    out2 = psn_mod.format_persona_snapshot(
        {"prompt": "", "_begin_dialogs_processed": [{"role": "user", "content": "hi"}]}
    )
    assert "Persona Instructions" not in out2
    assert "user: hi" in out2


@pytest.mark.asyncio
async def test_resolve_persona_system_prompt_defensive():
    """回退解析防御式：无 persona_manager / 解析异常 → 空串（评审占位兜底）。"""
    queue = asyncio.Queue()
    plugin = main_mod.VirtualSessionPlugin(FakeContext(queue))
    ev = VirtualMessageEvent.create(
        session_id="vs_1", sender_id="u1", sender_name="用户1", text="hi"
    )
    req = SimpleNamespace(conversation=None)
    # FakeContext 无 persona_manager → 空串
    assert await plugin._resolve_persona_system_prompt(ev, req) == ""
    # persona_manager 解析异常 → 空串
    plugin.context.persona_manager = FakePersonaManager(raise_on_call=True)
    assert await plugin._resolve_persona_system_prompt(ev, req) == ""
    # 无人格 → 空串
    plugin.context.persona_manager = FakePersonaManager(persona=None)
    assert await plugin._resolve_persona_system_prompt(ev, req) == ""


@pytest.mark.asyncio
async def test_resolve_persona_system_prompt_from_conf():
    """回退解析从配置档案解析人格：prompt + 开场对话合入快照系统提示词。"""
    queue = asyncio.Queue()
    plugin = main_mod.VirtualSessionPlugin(
        FakeContext(queue, conf={"provider_settings": {"default_personality": "p_x"}})
    )
    plugin.context.persona_manager = FakePersonaManager(
        persona={
            "prompt": "你是寒露",
            "_begin_dialogs_processed": [{"role": "user", "content": "你好"}],
        }
    )
    ev = VirtualMessageEvent.create(
        session_id="vs_1", sender_id="u1", sender_name="用户1", text="hi"
    )
    req = SimpleNamespace(conversation=SimpleNamespace(persona_id="p_hanlu"))
    out = await plugin._resolve_persona_system_prompt(ev, req)
    assert out.startswith("# Persona Instructions\n\n你是寒露\n")
    assert "user: 你好" in out
    # 会话级 persona_id、umo 与档案 provider_settings 都传给解析
    # （镜像 _ensure_persona_and_skills 的入参）
    call = plugin.context.persona_manager.calls[0]
    assert call["umo"] == "webchat:FriendMessage:vs_1"
    assert call["conversation_persona_id"] == "p_hanlu"
    assert call["provider_settings"] == {"default_personality": "p_x"}


@pytest.mark.asyncio
async def test_plugin_on_llm_persona_fallback():
    """on_llm：req.system_prompt 为空时回退解析人格（begin_dialogs 型会话）。"""
    queue = asyncio.Queue()
    plugin = main_mod.VirtualSessionPlugin(FakeContext(queue))
    plugin.context.persona_manager = FakePersonaManager(
        persona={
            "prompt": "",
            "_begin_dialogs_processed": [
                {"role": "user", "content": "你好", "_no_save": True},
                {"role": "assistant", "content": "寒露在呢", "_no_save": True},
            ],
        }
    )
    await plugin.runner.start(sessions=[make_session(1)], text="hi")
    ev = queue.get_nowait()
    req = SimpleNamespace(
        prompt="装饰后 prompt", extra_user_content_parts=[], system_prompt=""
    )
    await plugin.on_llm(ev, req)
    snap = ev.get_extra(ve_mod.TESTBENCH_LLM_INPUT_EXTRA_KEY)
    assert snap["prompt"] == "装饰后 prompt"
    assert (
        snap["system_prompt"]
        == "# 开场对话（begin_dialogs）\n\nuser: 你好\nassistant: 寒露在呢"
    )
    # 回退解析不影响非空 system_prompt 的捕获（原样保留）
    req2 = SimpleNamespace(
        prompt="p2", extra_user_content_parts=[], system_prompt="真实 SP"
    )
    await plugin.on_llm(ev, req2)
    snap2 = ev.get_extra(ve_mod.TESTBENCH_LLM_INPUT_EXTRA_KEY)
    assert snap2["system_prompt"] == "真实 SP"


@pytest.mark.asyncio
async def test_assessor_persona_fallback():
    """评审阶段回退解析人格：捕获 hook 未留下快照时，Assessor 从配置档案补上。

    结果无 llm_input 快照（捕获链路未触发）→ 评审输入仍以注入块带上被测
    agent 人格；会话级 persona_id 经对话存储回查（`conversation_persona_id`，
    镜像框架装饰路径入参；无会话时回落档案 default_personality）；同 umo
    的多个结果只解析一次（memo）。
    """
    provider = FakeLLMProvider(
        "prov_r",
        responses=['{"score": 90, "level": "好"}', '{"score": 85, "level": "好"}'],
    )
    context = FakeContext(
        providers=[provider],
        conf={"provider_settings": {"default_personality": "p_hanlu"}},
    )
    context.persona_manager = FakePersonaManager(
        persona={
            "prompt": "你是寒露",
            "_begin_dialogs_processed": [{"role": "user", "content": "你好"}],
        }
    )
    # vs_1 有会话级人格（对话存储回查命中）；vs_2 无会话 → 回落档案
    await context.conversation_manager.new_conversation(
        "webchat:FriendMessage:vs_1", persona_id="p_hanlu"
    )
    assessor = Assessor(context, {"rp_test": _valid_profile()})
    steps = [
        {
            "status": "done",
            "text": "问",
            "rules": [{"kind": "llm", "profile_id": "rp_test", "context": "record"}],
            "results": [
                {
                    "session_id": "vs_1",
                    "reply": "回答",
                    "status": "ok",
                    "umo": "webchat:FriendMessage:vs_1",
                },
                {
                    "session_id": "vs_2",
                    "reply": "回答2",
                    "status": "ok",
                    "umo": "webchat:FriendMessage:vs_2",
                },
            ],
        }
    ]
    await assessor.assess(steps, [], [])
    prompt = provider.calls[0]["prompt"]
    assert prompt.startswith(
        "【以下是被测 Agent 系统提示词】\n# Persona Instructions\n\n你是寒露\n"
    )
    assert "【以上是被测 Agent 系统提示词】" in prompt
    # 解析入参：会话级 persona_id 回查命中（vs_1）/ 回落档案（vs_2），
    # 档案 provider_settings 透传；不同 umo 各解析一次（memo 按 umo 记忆）
    call1 = context.persona_manager.calls[0]
    assert call1["umo"] == "webchat:FriendMessage:vs_1"
    assert call1["conversation_persona_id"] == "p_hanlu"
    assert call1["provider_settings"] == {"default_personality": "p_hanlu"}
    call2 = context.persona_manager.calls[1]
    assert call2["umo"] == "webchat:FriendMessage:vs_2"
    assert call2["conversation_persona_id"] is None
    assert len(context.persona_manager.calls) == 2


def test_build_input_text():
    """实际输入 = prompt + extra parts 拼接；无快照回退原始文本。"""
    text = build_input_text(
        {"prompt": "p1", "extra_parts": ["e1", "e2"], "system_prompt": "sp"},
        "回退",
    )
    assert text == "p1\ne1\ne2"
    # prompt 空、只有 extra parts
    assert build_input_text({"prompt": "", "extra_parts": ["x"]}, "回退") == "x"
    # 无快照（None / 非 dict / 全空）→ 回退原始文本
    assert build_input_text(None, "回退") == "回退"
    assert build_input_text("字符串", "回退") == "回退"
    assert build_input_text({}, "回退") == "回退"


def test_format_turn_and_record():
    """结构化评审材料：中文标签块标注身份与输入/输出分界，多轮带「第 N 步:」前缀。"""
    turn = format_turn("输入", "回复", "小明", "virtual_bot")
    assert turn == (
        "【输入 · user（小明）】\n输入\n\n【输出 · agent（virtual_bot）】\n回复"
    )
    # 无回复 → （无回复）占位
    turn2 = format_turn("输入", "", "测试台", "virtual_bot")
    assert turn2.endswith("【输出 · agent（virtual_bot）】\n（无回复）")

    entries = [
        ("in1", "r1", "小明", "virtual_bot"),
        ("in2", "", "小红", "virtual_bot"),
    ]
    rec = format_record(entries)
    assert rec.startswith("第 1 步:\n")
    assert "\n\n第 2 步:\n" in rec
    assert "【输入 · user（小红）】" in rec


@pytest.mark.asyncio
async def test_assessor_uses_actual_input_and_identity():
    """评审材料用实际输入（llm_input 快照）而非原始文本，身份回退 sender_id。"""
    provider = FakeLLMProvider("prov_r", responses=['{"score": 90, "level": "好"}'])
    context = FakeContext(providers=[provider])
    assessor = Assessor(context, {"rp_test": _valid_profile()})
    steps = [
        {
            "status": "done",
            "text": "原始文本",
            "sender_id": "u42",
            "sender_name": None,
            "rules": [{"kind": "llm", "profile_id": "rp_test", "context": "reply"}],
            "results": [
                {
                    "session_id": "vs_1",
                    "reply": "回答",
                    "status": "ok",
                    "llm_input": {
                        "prompt": "实际输入",
                        "extra_parts": ["<system_reminder>r</system_reminder>"],
                        "system_prompt": "被测 SP",
                    },
                }
            ],
        }
    ]
    await assessor.assess(steps, [], [])
    verdict = steps[0]["results"][0]["verdicts"][0]
    assert verdict["status"] == "ok"
    prompt = provider.calls[0]["prompt"]
    assert "实际输入" in prompt and "原始文本" not in prompt
    assert "<system_reminder>r</system_reminder>" in prompt
    # sender_name 为 None → 回退 sender_id；agent 身份恒 virtual_bot
    assert "【输入 · user（u42）】" in prompt
    assert "【输出 · agent（virtual_bot）】" in prompt
    # verdict 存储被测 agent 系统提示词（报告评审重试自包含）
    assert verdict["agent_system_prompt"] == "被测 SP"


@pytest.mark.asyncio
async def test_call_reviewer_deprecated_agent_system_prompt_placeholder_cleared():
    """{{agent_system_prompt}} 占位符已废弃：残留字面量被清空，不再展开。"""
    profile = {
        **_valid_profile(),
        "system_prompt": "结合被测提示词评审：{{agent_system_prompt}}",
    }
    provider = FakeLLMProvider("prov_r", responses=['{"score": 88, "level": "好"}'])
    _, error, status, _ = await call_reviewer(
        FakeContext(providers=[provider]), profile, "上下文"
    )
    assert status is None and error is None
    # 占位符不再展开为被测 agent 提示词（该机制已废弃），字面量清成空串
    assert provider.calls[0]["system_prompt"] == "结合被测提示词评审："
    assert "{{" not in provider.calls[0]["system_prompt"]


def test_llm_verdict_stores_agent_system_prompt():
    """verdict 存储 agent_system_prompt（ok 与 error 分支都存）。"""
    profile = _valid_profile()
    v = llm_verdict(
        0,
        [{"key": "score", "type": "number", "value": 90}],
        None,
        None,
        profile,
        raw="x",
        context_text="c",
        agent_system_prompt="被测 SP",
    )
    assert v["status"] == "ok"
    assert v["agent_system_prompt"] == "被测 SP"
    v2 = llm_verdict(
        1,
        None,
        "调用失败",
        "error",
        profile,
        raw="",
        context_text="c",
        agent_system_prompt="被测 SP",
    )
    assert v2["status"] == "error"
    assert v2["agent_system_prompt"] == "被测 SP"
    # 未提供 → None
    assert llm_verdict(0, [], None, None, profile)["agent_system_prompt"] is None


@pytest.mark.asyncio
async def test_retry_llm_verdict_keeps_agent_system_prompt_field():
    """报告评审重试保留 agent_system_prompt 字段（信息用）；占位符字面量清空。"""
    profile = {
        **_valid_profile(),
        "system_prompt": "SP: {{agent_system_prompt}}",
    }
    provider = FakeLLMProvider("prov_r", responses=['{"score": 88, "level": "好"}'])
    verdict = {
        "rule_index": 0,
        "status": "error",
        "pass": None,
        "metrics": [],
        "detail": "boom",
        "raw": "",
        "context_text": "上下文",
        "profile_id": "rp_test",
        "agent_system_prompt": "被测 SP",
    }
    new, err = await retry_llm_verdict(
        FakeContext(providers=[provider]), profile, verdict
    )
    assert err is None and new["status"] == "ok"
    # 占位符不再展开（已废弃）——字面量清空；重跑用存储的 context_text 喂 prompt
    assert provider.calls[0]["system_prompt"] == "SP: "
    assert provider.calls[0]["prompt"] == "上下文"
    assert new["agent_system_prompt"] == "被测 SP"


def test_inject_system_prompt_block():
    """注入块纯函数：前后闭合的「以下是/以上是」块包裹；未捕获 → 占位文案块。"""
    ctx = "【输入 · user（测试台）】\n问\n\n【输出 · agent（virtual_bot）】\n答"
    out = inject_system_prompt_block(ctx, "你是助手")
    assert out == (
        "【以下是被测 Agent 系统提示词】\n你是助手\n"
        "【以上是被测 Agent 系统提示词】\n\n" + ctx
    )
    # 未捕获（None / 空串）→ 注入块仍存在，显示占位文案（详情可确认链路状态）
    assert inject_system_prompt_block(ctx, None) == (
        "【以下是被测 Agent 系统提示词】\n（未捕获到被测 agent 系统提示词）\n"
        "【以上是被测 Agent 系统提示词】\n\n" + ctx
    )
    assert inject_system_prompt_block(ctx, "") == (
        "【以下是被测 Agent 系统提示词】\n（未捕获到被测 agent 系统提示词）\n"
        "【以上是被测 Agent 系统提示词】\n\n" + ctx
    )


@pytest.mark.asyncio
async def test_assessor_inject_system_prompt_rule_level():
    """LLM 断言规则级注入开关：缺省开启、inject_system_prompt=false 关闭。"""
    profile = _valid_profile()

    def make_provider():
        return FakeLLMProvider("prov_r", responses=['{"score": 90, "level": "好"}'])

    def make_steps(rule: dict) -> list[dict]:
        return [
            {
                "status": "done",
                "text": "问",
                "rules": [rule],
                "results": [
                    {
                        "session_id": "vs_1",
                        "reply": "回答",
                        "status": "ok",
                        "llm_input": {
                            "prompt": "实际输入",
                            "extra_parts": [],
                            "system_prompt": "被测 SP",
                        },
                    }
                ],
            }
        ]

    # 缺省（不写字段）→ 注入被测 agent 系统提示词到评审输入开头
    provider = make_provider()
    assessor = Assessor(FakeContext(providers=[provider]), {"rp_test": profile})
    steps = make_steps({"kind": "llm", "profile_id": "rp_test", "context": "reply"})
    await assessor.assess(steps, [], [])
    prompt = provider.calls[0]["prompt"]
    assert prompt.startswith(
        "【以下是被测 Agent 系统提示词】\n被测 SP\n【以上是被测 Agent 系统提示词】\n\n"
    )
    # 注入后的上下文随 verdict 落盘（报告评审重试据此自包含）
    verdict = steps[0]["results"][0]["verdicts"][0]
    assert verdict["context_text"] == prompt

    # inject_system_prompt=false → 不注入（评审输入即原结构化材料）
    provider2 = make_provider()
    assessor2 = Assessor(FakeContext(providers=[provider2]), {"rp_test": profile})
    steps2 = make_steps(
        {
            "kind": "llm",
            "profile_id": "rp_test",
            "context": "reply",
            "inject_system_prompt": False,
        }
    )
    await assessor2.assess(steps2, [], [])
    prompt2 = provider2.calls[0]["prompt"]
    assert "被测 Agent 系统提示词" not in prompt2
    assert prompt2.startswith("【输入 · user（测试台）】")

    # 开启但未捕获系统提示词（无 llm_input 快照）→ 注入占位块（详情可见链路状态）
    provider3 = make_provider()
    assessor3 = Assessor(FakeContext(providers=[provider3]), {"rp_test": profile})
    steps3 = [
        {
            "status": "done",
            "text": "问",
            "rules": [{"kind": "llm", "profile_id": "rp_test", "context": "reply"}],
            "results": [{"session_id": "vs_1", "reply": "回答", "status": "ok"}],
        }
    ]
    await assessor3.assess(steps3, [], [])
    prompt3 = provider3.calls[0]["prompt"]
    assert prompt3.startswith(
        "【以下是被测 Agent 系统提示词】\n（未捕获到被测 agent 系统提示词）\n"
        "【以上是被测 Agent 系统提示词】\n\n"
    )
    assert "【输入 · user（测试台）】" in prompt3


def test_result_summary_carries_llm_input():
    """result_summary 携带实际输入快照（评审材料的数据源）。"""
    ev = VirtualMessageEvent.create(
        session_id="vs_1", sender_id="u1", sender_name="用户1", text="hi"
    )
    assert ev.result_summary()["llm_input"] is None
    snap = {"prompt": "p", "extra_parts": ["e"], "system_prompt": "sp"}
    ev.set_extra(ve_mod.TESTBENCH_LLM_INPUT_EXTRA_KEY, snap)
    assert ev.result_summary()["llm_input"] == snap


def test_build_metrics_summary_aggregation():
    """默认模板聚合：number 均值/极值、enum 分类计数、bool 通过率、text 不入
    总览、error/invalid 单列评审失败（消息级 + final 级）。"""
    run = {
        "steps": [
            {
                "status": "done",
                "results": [
                    {
                        "session_id": "vs_1",
                        "verdicts": [
                            {
                                "rule_index": 0,
                                "status": "ok",
                                "pass": True,
                                "metrics": [
                                    {"key": "score", "type": "number", "value": 90},
                                    {"key": "level", "type": "enum", "value": "好"},
                                    {"key": "ok_flag", "type": "bool", "value": True},
                                ],
                            },
                            {
                                "rule_index": 1,
                                "status": "ok",
                                "pass": True,
                                "metrics": [
                                    {"key": "score", "type": "number", "value": 80},
                                    {"key": "note", "type": "text", "value": "说明"},
                                ],
                            },
                        ],
                    }
                ],
            },
            {
                "status": "done",
                "results": [
                    {
                        "session_id": "vs_1",
                        "verdicts": [
                            # 评审失败：invalid（pass 为 null）——计入 review_failures
                            {
                                "rule_index": 0,
                                "status": "invalid",
                                "pass": None,
                                "metrics": [],
                            },
                            {
                                "rule_index": 1,
                                "status": "ok",
                                "pass": True,
                                "metrics": [
                                    {"key": "score", "type": "number", "value": 70},
                                    {"key": "level", "type": "enum", "value": "差"},
                                    {"key": "ok_flag", "type": "bool", "value": False},
                                ],
                            },
                        ],
                    }
                ],
            },
        ],
        "final_verdicts": [
            {
                "rule_index": 0,
                "results": [
                    {
                        "session_id": "vs_1",
                        # 评审失败：error（调用异常）——同样计入 review_failures
                        "verdict": {
                            "rule_index": 0,
                            "status": "error",
                            "pass": None,
                            "metrics": [],
                        },
                    }
                ],
            }
        ],
    }
    summary = build_metrics_summary(run)
    assert summary["review_failures"] == 2  # invalid + error
    metrics = summary["metrics"]
    assert metrics["score"] == {
        "type": "number",
        "count": 3,
        "avg": 80.0,
        "min": 70,
        "max": 90,
    }
    assert metrics["level"] == {
        "type": "enum",
        "counts": {"好": 1, "差": 1},
        "total": 2,
    }
    assert metrics["ok_flag"] == {"type": "bool", "pass": 1, "total": 2, "rate": 0.5}
    assert "note" not in metrics  # text 不进总览


def test_build_report_data_snapshot():
    """报告数据为运行终态快照：元数据 + 深拷贝产物 + 派生总览，源运行后续
    变化不影响已生成报告。"""
    run = {
        "run_id": "tr_1",
        "testset_id": "ts_1",
        "testset_name": "报告测试",
        "status": "done",
        "started_at": 100,
        "finished_at": 200,
        "sessions": [{"id": "vs_1"}],
        "steps": [{"status": "done", "results": []}],
        "final_verdicts": [{"rule_index": 0, "results": []}],
    }
    data = build_report_data(run)
    assert data["run_id"] == "tr_1"
    assert data["testset_id"] == "ts_1"
    assert data["testset_name"] == "报告测试"
    assert data["status"] == "done"
    assert data["started_at"] == 100 and data["finished_at"] == 200
    assert "metrics_summary" in data
    assert data["metrics_summary"]["review_failures"] == 0

    # 源 run 后续变化不影响已生成报告（deepcopy）
    run["status"] = "error"
    run["sessions"].append({"id": "vs_2"})
    run["steps"][0]["results"].append({"session_id": "vs_1"})
    assert data["status"] == "done"
    assert len(data["sessions"]) == 1
    assert data["steps"][0]["results"] == []
    # 报告 3 类组织：断言 / 耗时统计随默认模板一并产出
    assert data["assertions"] == {"total": 0, "passed": 0, "failed": 0}
    assert data["durations"]["count"] == 0
    assert data["durations"]["min"] == 0.0


def test_build_assertion_stats():
    """断言统计：pass 为 bool 才计入，error/invalid（pass None）与短路跳过的
    LLM（不产 verdict）不计入；final 级 verdict 一并统计。"""
    run = {
        "steps": [
            {
                "status": "done",
                "results": [
                    {
                        "session_id": "vs_1",
                        "verdicts": [
                            {"rule_index": 0, "status": "ok", "pass": True},
                            {"rule_index": 1, "status": "ok", "pass": False},
                            # 评审失败：pass 为 None，不计入断言计数
                            {"rule_index": 2, "status": "error", "pass": None},
                            {"rule_index": 3, "status": "invalid", "pass": None},
                        ],
                    }
                ],
            }
        ],
        "final_verdicts": [
            {
                "rule_index": 0,
                "results": [
                    {"session_id": "vs_1", "verdict": {"status": "ok", "pass": True}},
                    {
                        "session_id": "vs_2",
                        "verdict": {"status": "error", "pass": None},
                    },
                ],
            }
        ],
    }
    assert build_assertion_stats(run) == {"total": 3, "passed": 2, "failed": 1}


def test_build_duration_stats():
    """耗时统计：只取 status=done 步骤的 results，耗时须为非 bool 数字；
    数字但为 bool（True/False）排除。"""
    run = {
        "steps": [
            {
                "status": "done",
                "results": [
                    {"session_id": "vs_1", "duration": 1.5},
                    {"session_id": "vs_2", "duration": 2.5},
                ],
            },
            {
                "status": "done",
                "results": [
                    {"session_id": "vs_1", "duration": 1.0},
                    {"session_id": "vs_2", "duration": "n/a"},  # 非数字排除
                    {"session_id": "vs_3", "duration": True},  # bool 排除
                ],
            },
            {
                "status": "error",  # 未完成步骤的 results 不计入
                "results": [{"session_id": "vs_1", "duration": 99.0}],
            },
        ]
    }
    stats = build_duration_stats(run)
    assert stats["count"] == 3
    assert stats["min"] == 1.0
    assert stats["max"] == 2.5
    assert stats["avg"] == round(5.0 / 3, 3)


def test_testset_store_report_enabled(tmp_path):
    """测试集存储 report_enabled：缺省 False、显式 True 落盘、更新生效、旧数据迁移。"""
    store = TestsetStore(data_dir=tmp_path)
    ts = store.create_testset("默认", [{"text": "m"}])
    assert ts["report_enabled"] is False
    ts2 = store.create_testset("开报告", [{"text": "m"}], report_enabled=True)
    assert ts2["report_enabled"] is True

    updated = store.update_testset(
        ts["id"], "默认", [{"text": "m"}], report_enabled=True
    )
    assert updated["report_enabled"] is True

    # 旧数据缺键 → setdefault False
    with (tmp_path / "virtual_session" / "testsets.json").open(
        "w", encoding="utf-8"
    ) as f:
        json.dump(
            {
                "testsets": [
                    {
                        "id": "ts_old",
                        "name": "旧",
                        "created_at": 0,
                        "messages": [],
                        "batch_ranges": [],
                    }
                ]
            },
            f,
        )
    reloaded = TestsetStore(data_dir=tmp_path)
    assert reloaded.get_testset("ts_old")["report_enabled"] is False


def test_testset_store_report_llm(tmp_path):
    """测试集存储 report_llm：缺省 None、合法配置落盘（provider_id 去空白）、
    非 dict / 缺 provider_id → None、system_prompt 非字符串 → ""、更新整体替换、
    旧数据缺键 setdefault None。"""
    store = TestsetStore(data_dir=tmp_path)
    ts = store.create_testset("默认", [{"text": "m"}])
    assert ts["report_llm"] is None

    ts2 = store.create_testset(
        "带报告 LLM",
        [{"text": "m"}],
        report_llm={"provider_id": " prov_x ", "system_prompt": "总结", "model": "m1"},
    )
    assert ts2["report_llm"] == {
        "provider_id": "prov_x",
        "system_prompt": "总结",
        "model": "m1",
    }
    # 未提供 model：清洗后不含该键（生成时用 Provider 当前模型）
    ts3 = store.create_testset(
        "无模型", [], report_llm={"provider_id": "p", "system_prompt": "s"}
    )
    assert ts3["report_llm"] == {"provider_id": "p", "system_prompt": "s"}

    # 清洗：非 dict → None；缺 provider_id → None；system_prompt 非字符串 → ""
    assert store.create_testset("T", [], report_llm="nope")["report_llm"] is None
    assert (
        store.create_testset("T", [], report_llm={"system_prompt": "x"})["report_llm"]
        is None
    )
    ts4 = store.create_testset(
        "T", [], report_llm={"provider_id": "p", "system_prompt": 123}
    )
    assert ts4["report_llm"] == {"provider_id": "p", "system_prompt": ""}

    # 更新整体替换；缺省 → 置 None（清空旧配置）
    updated = store.update_testset(
        ts2["id"], "带报告 LLM", [], report_llm={"provider_id": "p2"}
    )
    assert updated["report_llm"] == {"provider_id": "p2", "system_prompt": ""}
    updated2 = store.update_testset(ts2["id"], "带报告 LLM", [])
    assert updated2["report_llm"] is None

    # 持久化 + 旧数据缺键 → setdefault None
    with (tmp_path / "virtual_session" / "testsets.json").open(
        "w", encoding="utf-8"
    ) as f:
        json.dump(
            {
                "testsets": [
                    {
                        "id": "ts_old",
                        "name": "旧",
                        "created_at": 0,
                        "messages": [],
                        "batch_ranges": [],
                    }
                ]
            },
            f,
        )
    reloaded = TestsetStore(data_dir=tmp_path)
    assert reloaded.get_testset("ts_old")["report_llm"] is None
