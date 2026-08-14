"""Web API 测试：各组/会话/运行/测试集/身份/评审/报告 handler、平台与
Provider 列表、cron 警告接入与 SSE 端点。"""

import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
# 插件模块用相对导入（from .group_store import ...），必须以包形式加载。
# 与 AstrBot 在 data/plugins 下加载插件的方式一致：把插件根目录的父目录加入
# sys.path，以 namespace package（astrbot_plugin_testbench）导入。
sys.path.insert(0, str(REPO_ROOT.parent))

pytest.importorskip("astrbot")

import astrbot_plugin_testbench.core.conf_tools as ct_mod  # noqa: E402
import astrbot_plugin_testbench.core.cron_probe as cp_mod  # noqa: E402
import astrbot_plugin_testbench.eval.reviewer as rev_mod  # noqa: E402
import astrbot_plugin_testbench.history_ops as hops_mod  # noqa: E402
import astrbot_plugin_testbench.main as main_mod  # noqa: E402
import astrbot_plugin_testbench.store.group_store as gs_mod  # noqa: E402
import astrbot_plugin_testbench.store.identity_store as ids_mod  # noqa: E402
import astrbot_plugin_testbench.store.report_store as rps_mod  # noqa: E402
import astrbot_plugin_testbench.store.reviewer_store as rvs_mod  # noqa: E402
import astrbot_plugin_testbench.store.testset_store as tss_mod  # noqa: E402
from astrbot.api.event import MessageChain  # noqa: E402
from astrbot.api.web import bind_request_context  # noqa: E402

ChatGroupStore = ids_mod.ChatGroupStore
IdentityStore = ids_mod.IdentityStore
ReportStore = rps_mod.ReportStore
ReviewerStore = rvs_mod.ReviewerStore
TestsetStore = tss_mod.TestsetStore
VirtualGroupManager = gs_mod.VirtualGroupManager
collect_cron_warnings = cp_mod.collect_cron_warnings
conf_has_callable_tools = ct_mod.conf_has_callable_tools
conf_tool_info = ct_mod.conf_tool_info
cron_job_warning = cp_mod.cron_job_warning
target_sets = cp_mod.target_sets
umo_of = gs_mod.umo_of

from fakes import (  # noqa: E402
    FakeContext,
    FakeConvManager,
    FakeCronManager,
    FakeLLMProvider,
    FakePlatformInst,
    FakePlatformManager,
    FakeProvider,
    FakeUCR,
    _add_history,
    _cron_job,
    _report_with_llm_verdicts,
    _valid_profile,
    call_handler,
    consume,
    make_plugin_request,
    make_session,
    wait_run_done,
    wait_testset_done,
    wait_until,
)


@pytest.mark.asyncio
async def test_plugin_create_group_ok(tmp_path):
    plugin = main_mod.VirtualSessionPlugin(FakeContext())
    plugin.group_mgr = VirtualGroupManager(data_dir=tmp_path)
    resp = await call_handler(plugin.create_group, {"name": "组A", "count": 2})
    assert resp.status_code == 200
    body = json.loads(resp.body)
    assert body["name"] == "组A"
    assert len(body["sessions"]) == 2
    assert all(s["platform_id"] is None for s in body["sessions"])


@pytest.mark.asyncio
async def test_plugin_create_group_applies_conf_route(tmp_path):
    context = FakeContext()
    plugin = main_mod.VirtualSessionPlugin(context)
    plugin.group_mgr = VirtualGroupManager(data_dir=tmp_path)
    ucr = context.astrbot_config_mgr.ucr
    resp = await call_handler(
        plugin.create_group, {"name": "组A", "count": 2, "conf_id": "conf_c"}
    )
    assert resp.status_code == 200
    body = json.loads(resp.body)
    for s in body["sessions"]:
        umop = f"webchat:FriendMessage:{s['id']}"
        assert ucr.umop_to_conf_id[umop] == "conf_c"


@pytest.mark.asyncio
async def test_plugin_create_group_zero_count_allowed(tmp_path):
    plugin = main_mod.VirtualSessionPlugin(FakeContext())
    plugin.group_mgr = VirtualGroupManager(data_dir=tmp_path)
    # count=0 允许：创建 0 会话的空测试组（前端「＋ 新建测试组」直接建空组
    # 不弹窗，用户后续在编辑弹窗按「会话数量」补齐）
    resp = await call_handler(plugin.create_group, {"name": "空组", "count": 0})
    assert resp.status_code == 200
    body = json.loads(resp.body)
    assert len(body["sessions"]) == 0
    # 负数仍拒绝
    resp = await call_handler(plugin.create_group, {"name": "组A", "count": -1})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_plugin_post_handler_non_dict_body_400(tmp_path):
    """POST handler 收到非 dict JSON 体（数组/标量/null）→ 400 而非 500。

    修复前 request.json(default={}) 只防解析失败，数组体直接 .get 触发
    AttributeError → 500；json_dict 统一把非 dict 体转 400。覆盖 api/ 与
    history_ops 的 save_history / regenerate_history 两处 handler。
    """
    plugin = main_mod.VirtualSessionPlugin(FakeContext())
    plugin.group_mgr = VirtualGroupManager(data_dir=tmp_path)
    plugin.report_store = rps_mod.ReportStore(data_dir=tmp_path)
    handlers = [plugin.delete_reports, plugin.save_history, plugin.regenerate_history]
    for bad_body in ([1, 2], "x", None):
        for handler in handlers:
            resp = await call_handler(handler, bad_body)
            assert resp.status_code == 400, (
                f"{handler.__name__} 非 dict 体 {bad_body!r} 应被拒绝"
            )


@pytest.mark.asyncio
async def test_plugin_add_group_sessions(tmp_path):
    context = FakeContext()
    plugin = main_mod.VirtualSessionPlugin(context)
    plugin.group_mgr = VirtualGroupManager(data_dir=tmp_path)
    group = plugin.group_mgr.create_group("组A", count=1, conf_id="conf_a")
    ucr = context.astrbot_config_mgr.ucr
    resp = await call_handler(plugin.add_group_sessions, {"count": 2}, group["id"])
    assert resp.status_code == 200
    created = json.loads(resp.body)
    assert len(created) == 2
    # 新会话继承组配置档案并应用路由
    for s in created:
        assert ucr.umop_to_conf_id[f"webchat:FriendMessage:{s['id']}"] == "conf_a"


@pytest.mark.asyncio
async def test_plugin_add_group_sessions_not_found(tmp_path):
    plugin = main_mod.VirtualSessionPlugin(FakeContext())
    plugin.group_mgr = VirtualGroupManager(data_dir=tmp_path)
    resp = await call_handler(plugin.add_group_sessions, {"count": 1}, "g_none")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_plugin_update_session_syncs_conf_route(tmp_path):
    context = FakeContext()
    plugin = main_mod.VirtualSessionPlugin(context)
    plugin.group_mgr = VirtualGroupManager(data_dir=tmp_path)
    group = plugin.group_mgr.create_group("组A", count=1, conf_id="conf_a")
    sid = group["sessions"][0]["id"]
    umop = f"webchat:FriendMessage:{sid}"
    ucr = context.astrbot_config_mgr.ucr
    # 模拟组创建时已应用 conf_a 路由
    await ucr.update_route(umop, "conf_a")

    resp = await call_handler(plugin.update_session, {"id": sid, "conf_id": "conf_b"})
    assert resp.status_code == 200
    assert ucr.umop_to_conf_id[umop] == "conf_b"

    # 显式默认档案 → 删除路由
    resp = await call_handler(plugin.update_session, {"id": sid, "conf_id": ""})
    assert resp.status_code == 200
    assert umop not in ucr.umop_to_conf_id


@pytest.mark.asyncio
async def test_plugin_update_session_platform_change_cleans_old_route(tmp_path):
    context = FakeContext()
    plugin = main_mod.VirtualSessionPlugin(context)
    plugin.group_mgr = VirtualGroupManager(data_dir=tmp_path)
    group = plugin.group_mgr.create_group("组A", count=1, conf_id="conf_a")
    sid = group["sessions"][0]["id"]
    old_umop = f"webchat:FriendMessage:{sid}"
    new_umop = f"telegram:FriendMessage:{sid}"
    ucr = context.astrbot_config_mgr.ucr
    await ucr.update_route(old_umop, "conf_a")

    resp = await call_handler(
        plugin.update_session, {"id": sid, "platform_id": "telegram"}
    )
    assert resp.status_code == 200
    # 旧 umo 路由已清理，新 umo 上应用组档案
    assert old_umop not in ucr.umop_to_conf_id
    assert ucr.umop_to_conf_id[new_umop] == "conf_a"


@pytest.mark.asyncio
async def test_plugin_update_session_not_found(tmp_path):
    plugin = main_mod.VirtualSessionPlugin(FakeContext())
    plugin.group_mgr = VirtualGroupManager(data_dir=tmp_path)
    resp = await call_handler(plugin.update_session, {"id": "vs_none", "conf_id": "x"})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_plugin_update_session_conf_empty_means_default(tmp_path):
    """conf_id=""（显式默认档案）时有效配置为不绑定档案，路由被清除。"""
    context = FakeContext()
    plugin = main_mod.VirtualSessionPlugin(context)
    plugin.group_mgr = VirtualGroupManager(data_dir=tmp_path)
    group = plugin.group_mgr.create_group("组A", count=1, conf_id="conf_a")
    sid = group["sessions"][0]["id"]
    umop = f"webchat:FriendMessage:{sid}"
    ucr = context.astrbot_config_mgr.ucr
    await ucr.update_route(umop, "conf_a")

    resp = await call_handler(plugin.update_session, {"id": sid, "conf_id": ""})
    assert resp.status_code == 200
    body = json.loads(resp.body)
    assert body["conf_id"] is None  # 有效配置不绑定档案
    assert umop not in ucr.umop_to_conf_id


@pytest.mark.asyncio
async def test_plugin_update_session_platform_change_cascades_conversations(tmp_path):
    """会话平台变更（umo 变化）时，旧 umo 的对话历史被级联删除（与删除会话一致）。"""
    context = FakeContext()
    plugin = main_mod.VirtualSessionPlugin(context)
    plugin.group_mgr = VirtualGroupManager(data_dir=tmp_path)
    group = plugin.group_mgr.create_group("组A", count=1, conf_id="conf_a")
    session = group["sessions"][0]
    old_umop = f"webchat:FriendMessage:{session['id']}"
    conv_mgr = context.conversation_manager
    conv_mgr.add_history(old_umop, "旧对话", [{"role": "user", "content": "hi"}])
    await context.astrbot_config_mgr.ucr.update_route(old_umop, "conf_a")

    resp = await call_handler(
        plugin.update_session, {"id": session["id"], "platform_id": "telegram"}
    )
    assert resp.status_code == 200
    assert await conv_mgr.get_conversations(old_umop) == []


@pytest.mark.asyncio
async def test_plugin_update_group_syncs_routes(tmp_path):
    context = FakeContext()
    plugin = main_mod.VirtualSessionPlugin(context)
    plugin.group_mgr = VirtualGroupManager(data_dir=tmp_path)
    group = plugin.group_mgr.create_group("组A", count=2, conf_id="conf_a")
    ucr = context.astrbot_config_mgr.ucr
    umops = [f"webchat:FriendMessage:{s['id']}" for s in group["sessions"]]
    for umop in umops:
        await ucr.update_route(umop, "conf_a")

    resp = await call_handler(
        plugin.update_group, {"id": group["id"], "conf_id": "conf_b"}, group["id"]
    )
    assert resp.status_code == 200
    # 继承组配置的会话全部切换到新档案
    assert all(ucr.umop_to_conf_id[umop] == "conf_b" for umop in umops)


@pytest.mark.asyncio
async def test_plugin_update_group_platform_change_cleans_old_routes(tmp_path):
    context = FakeContext()
    plugin = main_mod.VirtualSessionPlugin(context)
    plugin.group_mgr = VirtualGroupManager(data_dir=tmp_path)
    group = plugin.group_mgr.create_group("组A", count=1, conf_id="conf_a")
    sid = group["sessions"][0]["id"]
    old_umop = f"webchat:FriendMessage:{sid}"
    new_umop = f"telegram:FriendMessage:{sid}"
    ucr = context.astrbot_config_mgr.ucr
    await ucr.update_route(old_umop, "conf_a")

    resp = await call_handler(
        plugin.update_group, {"id": group["id"], "platform_id": "telegram"}, group["id"]
    )
    assert resp.status_code == 200
    # 旧 umo 路由已清理，新 umo 上应用组档案
    assert old_umop not in ucr.umop_to_conf_id
    assert ucr.umop_to_conf_id[new_umop] == "conf_a"


@pytest.mark.asyncio
async def test_plugin_update_group_respects_session_override(tmp_path):
    """会话单独覆盖的字段不随组配置变更。"""
    context = FakeContext()
    plugin = main_mod.VirtualSessionPlugin(context)
    plugin.group_mgr = VirtualGroupManager(data_dir=tmp_path)
    group = plugin.group_mgr.create_group("组A", count=2, conf_id="conf_a")
    plugin.group_mgr.update_session(group["sessions"][0]["id"], conf_id="conf_s")
    ucr = context.astrbot_config_mgr.ucr
    umop0 = f"webchat:FriendMessage:{group['sessions'][0]['id']}"
    umop1 = f"webchat:FriendMessage:{group['sessions'][1]['id']}"
    await ucr.update_route(umop0, "conf_s")
    await ucr.update_route(umop1, "conf_a")

    resp = await call_handler(
        plugin.update_group, {"id": group["id"], "conf_id": "conf_b"}, group["id"]
    )
    assert resp.status_code == 200
    # 会话0 保持自己的覆盖，会话1 跟随组变更
    assert ucr.umop_to_conf_id[umop0] == "conf_s"
    assert ucr.umop_to_conf_id[umop1] == "conf_b"


@pytest.mark.asyncio
async def test_plugin_update_group_not_found(tmp_path):
    plugin = main_mod.VirtualSessionPlugin(FakeContext())
    plugin.group_mgr = VirtualGroupManager(data_dir=tmp_path)
    resp = await call_handler(plugin.update_group, {"name": "x"}, "g_none")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_plugin_update_group_no_config_change_no_route_write(tmp_path):
    """组配置未实际变化（仅改组名/发送者）时不写 UCR 路由。"""
    context = FakeContext()
    plugin = main_mod.VirtualSessionPlugin(context)
    plugin.group_mgr = VirtualGroupManager(data_dir=tmp_path)
    group = plugin.group_mgr.create_group("组A", count=2, conf_id="conf_a")
    ucr = context.astrbot_config_mgr.ucr
    umops = [f"webchat:FriendMessage:{s['id']}" for s in group["sessions"]]
    for umop in umops:
        await ucr.update_route(umop, "conf_a")
    ucr.update_calls = 0  # 只统计本次 handler 产生的写入
    ucr.delete_calls = 0

    resp = await call_handler(
        plugin.update_group, {"id": group["id"], "name": "新组名"}, group["id"]
    )
    assert resp.status_code == 200
    assert ucr.update_calls == 0
    assert ucr.delete_calls == 0
    assert all(ucr.umop_to_conf_id[umop] == "conf_a" for umop in umops)


@pytest.mark.asyncio
async def test_plugin_update_group_platform_change_cascades_conversations(tmp_path):
    """组平台变更（umo 变化）时，旧 umo 的对话历史被级联删除。"""
    context = FakeContext()
    plugin = main_mod.VirtualSessionPlugin(context)
    plugin.group_mgr = VirtualGroupManager(data_dir=tmp_path)
    group = plugin.group_mgr.create_group("组A", count=1, conf_id="conf_a")
    session = group["sessions"][0]
    old_umop = f"webchat:FriendMessage:{session['id']}"
    conv_mgr = context.conversation_manager
    conv_mgr.add_history(old_umop, "旧对话", [{"role": "user", "content": "hi"}])
    await context.astrbot_config_mgr.ucr.update_route(old_umop, "conf_a")

    resp = await call_handler(
        plugin.update_group, {"id": group["id"], "platform_id": "telegram"}, group["id"]
    )
    assert resp.status_code == 200
    assert await conv_mgr.get_conversations(old_umop) == []


@pytest.mark.asyncio
async def test_plugin_delete_sessions_cleans_routes(tmp_path):
    context = FakeContext()
    plugin = main_mod.VirtualSessionPlugin(context)
    plugin.group_mgr = VirtualGroupManager(data_dir=tmp_path)
    group = plugin.group_mgr.create_group("组A", count=2, conf_id="conf_a")
    ucr = context.astrbot_config_mgr.ucr
    umops = [f"webchat:FriendMessage:{s['id']}" for s in group["sessions"]]
    for umop in umops:
        await ucr.update_route(umop, "conf_a")

    resp = await call_handler(
        plugin.delete_sessions, {"ids": [group["sessions"][0]["id"]]}
    )
    assert resp.status_code == 200
    assert json.loads(resp.body)["deleted"] == 1
    assert umops[0] not in ucr.umop_to_conf_id
    assert umops[1] in ucr.umop_to_conf_id


@pytest.mark.asyncio
async def test_plugin_delete_groups_cleans_routes(tmp_path):
    context = FakeContext()
    plugin = main_mod.VirtualSessionPlugin(context)
    plugin.group_mgr = VirtualGroupManager(data_dir=tmp_path)
    group = plugin.group_mgr.create_group("组A", count=2, conf_id="conf_a")
    ucr = context.astrbot_config_mgr.ucr
    umops = [f"webchat:FriendMessage:{s['id']}" for s in group["sessions"]]
    for umop in umops:
        await ucr.update_route(umop, "conf_a")

    resp = await call_handler(plugin.delete_groups, {"ids": [group["id"]]})
    assert resp.status_code == 200
    assert json.loads(resp.body)["deleted"] == 2
    assert all(umop not in ucr.umop_to_conf_id for umop in umops)


@pytest.mark.asyncio
async def test_plugin_delete_group_with_no_sessions(tmp_path):
    context = FakeContext()
    plugin = main_mod.VirtualSessionPlugin(context)
    plugin.group_mgr = VirtualGroupManager(data_dir=tmp_path)
    group = plugin.group_mgr.create_group("空组", count=1)
    # 先删光组内会话，使组内剩 0 会话
    await call_handler(plugin.delete_sessions, {"ids": [group["sessions"][0]["id"]]})

    resp = await call_handler(plugin.delete_groups, {"ids": [group["id"]]})
    assert resp.status_code == 200
    assert json.loads(resp.body)["deleted"] == 0  # 无会话可级联清理
    assert plugin.group_mgr.list_groups() == []  # 组已删除


@pytest.mark.asyncio
async def test_plugin_delete_sessions_cascades_conversations(tmp_path):
    context = FakeContext()
    plugin = main_mod.VirtualSessionPlugin(context)
    plugin.group_mgr = VirtualGroupManager(data_dir=tmp_path)
    group = plugin.group_mgr.create_group("组A", count=2, conf_id="conf_a")
    sessions = [plugin.group_mgr.effective(group, s) for s in group["sessions"]]
    umops = _add_history(context.conversation_manager, sessions)

    resp = await call_handler(
        plugin.delete_sessions, {"ids": [group["sessions"][0]["id"]]}
    )
    assert resp.status_code == 200
    conv_mgr = context.conversation_manager
    assert await conv_mgr.get_conversations(umops[0]) == []  # 已级联删除
    assert len(await conv_mgr.get_conversations(umops[1])) == 1  # 其余会话保留


@pytest.mark.asyncio
async def test_plugin_delete_groups_cascades_conversations(tmp_path):
    context = FakeContext()
    plugin = main_mod.VirtualSessionPlugin(context)
    plugin.group_mgr = VirtualGroupManager(data_dir=tmp_path)
    group = plugin.group_mgr.create_group("组A", count=2, conf_id="conf_a")
    sessions = [plugin.group_mgr.effective(group, s) for s in group["sessions"]]
    umops = _add_history(context.conversation_manager, sessions)

    resp = await call_handler(plugin.delete_groups, {"ids": [group["id"]]})
    assert resp.status_code == 200
    conv_mgr = context.conversation_manager
    remaining = [await conv_mgr.get_conversations(umop) for umop in umops]
    assert all(r == [] for r in remaining)  # 组内全部会话级联删除


@pytest.mark.asyncio
async def test_plugin_reset_sessions(tmp_path):
    context = FakeContext()
    plugin = main_mod.VirtualSessionPlugin(context)
    plugin.group_mgr = VirtualGroupManager(data_dir=tmp_path)
    group = plugin.group_mgr.create_group("组A", count=2)
    sessions = [plugin.group_mgr.effective(group, s) for s in group["sessions"]]
    umops = _add_history(context.conversation_manager, sessions)

    resp = await call_handler(
        plugin.reset_sessions, {"ids": [group["sessions"][0]["id"]]}
    )
    assert resp.status_code == 200
    assert json.loads(resp.body)["reset"] == 1
    conv_mgr = context.conversation_manager
    assert await conv_mgr.get_conversations(umops[0]) == []
    assert len(await conv_mgr.get_conversations(umops[1])) == 1


@pytest.mark.asyncio
async def test_plugin_list_platforms_ok(tmp_path):
    context = FakeContext(
        platform_mgr=FakePlatformManager(
            [
                FakePlatformInst("aiocqhttp", name="aiocqhttp"),
                FakePlatformInst(
                    "webchat", name="webchat", adapter_display_name="Web 聊天"
                ),
            ]
        )
    )
    plugin = main_mod.VirtualSessionPlugin(context)
    plugin.group_mgr = VirtualGroupManager(data_dir=tmp_path)

    resp = await plugin.list_platforms()
    assert resp.status_code == 200
    body = json.loads(resp.body)
    assert [p["id"] for p in body] == ["aiocqhttp", "webchat"]
    assert body[1]["display_name"] == "Web 聊天"  # adapter_display_name 优先
    assert body[0]["display_name"] == "aiocqhttp"  # 缺失时回落 name


@pytest.mark.asyncio
async def test_plugin_list_platforms_skips_broken(tmp_path):
    context = FakeContext(
        platform_mgr=FakePlatformManager(
            [
                FakePlatformInst("ok", name="ok"),
                FakePlatformInst("broken", name="broken", raise_on_meta=True),
            ]
        )
    )
    plugin = main_mod.VirtualSessionPlugin(context)
    plugin.group_mgr = VirtualGroupManager(data_dir=tmp_path)

    resp = await plugin.list_platforms()
    assert resp.status_code == 200
    body = json.loads(resp.body)
    assert [p["id"] for p in body] == ["ok"]  # 异常适配器被跳过，接口不失败


@pytest.mark.asyncio
async def test_plugin_list_platforms_empty(tmp_path):
    context = FakeContext(platform_mgr=FakePlatformManager())
    plugin = main_mod.VirtualSessionPlugin(context)
    plugin.group_mgr = VirtualGroupManager(data_dir=tmp_path)

    resp = await plugin.list_platforms()
    assert resp.status_code == 200
    assert json.loads(resp.body) == []


@pytest.mark.asyncio
async def test_plugin_list_platforms_missing_manager(tmp_path):
    # context 无 platform_manager 时接口应返回空列表而非抛异常
    plugin = main_mod.VirtualSessionPlugin(FakeContext())
    plugin.group_mgr = VirtualGroupManager(data_dir=tmp_path)

    resp = await plugin.list_platforms()
    assert resp.status_code == 200
    assert json.loads(resp.body) == []


@pytest.mark.asyncio
async def test_plugin_list_providers_ok(tmp_path):
    context = FakeContext(
        providers=[
            FakeProvider(
                "prov_a",
                "openai",
                models=["m1", "m2"],
                current_model="m1",
                config={"id": "prov_a", "name": "Provider A"},
            ),
            FakeProvider("prov_b", "anthropic", models=[], current_model=None),
            # 新 UI 的多个同 type 来源靠 provider_source_id（WebUI 展示名）区分
            FakeProvider(
                "prov_c",
                "openai",
                models=["m3"],
                current_model="m3",
                config={"id": "prov_c", "provider_source_id": "deepseek-main"},
            ),
        ]
    )
    plugin = main_mod.VirtualSessionPlugin(context)
    plugin.group_mgr = VirtualGroupManager(data_dir=tmp_path)

    resp = await plugin.list_providers()
    assert resp.status_code == 200
    body = json.loads(resp.body)
    assert body[0]["id"] == "prov_a"
    assert body[0]["name"] == "Provider A"  # provider_config.name 优先
    assert body[0]["type"] == "openai"
    assert body[0]["current_model"] == "m1"
    assert body[0]["models"] == ["m1", "m2"]
    # 无 provider_config 时回落 meta 的 id（再到底才是 type）
    assert body[1]["id"] == "prov_b"
    assert body[1]["name"] == "prov_b"
    assert body[1]["type"] == "anthropic"
    # provider_source_id 优先于 provider id / type，同名 type 的多个来源可区分
    assert body[2]["id"] == "prov_c"
    assert body[2]["name"] == "deepseek-main"
    assert body[2]["type"] == "openai"


@pytest.mark.asyncio
async def test_plugin_list_providers_models_failure(tmp_path):
    # get_models 抛异常时该 provider 的模型列表为空，接口不失败
    context = FakeContext(
        providers=[FakeProvider("prov_a", "openai", raise_models=True)]
    )
    plugin = main_mod.VirtualSessionPlugin(context)
    plugin.group_mgr = VirtualGroupManager(data_dir=tmp_path)

    resp = await plugin.list_providers()
    assert resp.status_code == 200
    body = json.loads(resp.body)
    assert body[0]["models"] == []


@pytest.mark.asyncio
async def test_plugin_list_providers_meta_and_get_model_failure(tmp_path):
    # meta 抛异常的 provider 被跳过（不 500）；get_model 抛异常时降级为 None
    context = FakeContext(
        providers=[
            FakeProvider("prov_bad_meta", "openai", raise_meta=True),
            FakeProvider("prov_bad_model", "anthropic", raise_get_model=True),
            FakeProvider("prov_ok", "deepseek", models=["m1"], current_model="m1"),
        ]
    )
    plugin = main_mod.VirtualSessionPlugin(context)
    plugin.group_mgr = VirtualGroupManager(data_dir=tmp_path)

    resp = await plugin.list_providers()
    assert resp.status_code == 200
    body = json.loads(resp.body)
    assert [p["id"] for p in body] == ["prov_bad_model", "prov_ok"]
    assert body[0]["current_model"] is None  # get_model 失败降级
    assert body[0]["models"] == []
    assert body[1]["current_model"] == "m1"


@pytest.mark.asyncio
async def test_plugin_list_providers_empty(tmp_path):
    plugin = main_mod.VirtualSessionPlugin(FakeContext())
    plugin.group_mgr = VirtualGroupManager(data_dir=tmp_path)

    resp = await plugin.list_providers()
    assert resp.status_code == 200
    assert json.loads(resp.body) == []


@pytest.mark.asyncio
async def test_plugin_list_confs_ok_and_defensive(tmp_path):
    # 缺 id/name/path 的档案对象也能被安全列出（防御式 .get，不 500）
    context = FakeContext(
        conf_list=[
            {"id": "conf_a", "name": "档案A", "path": "/a"},
            {"name": "只有名字"},
            {"id": "conf_c"},
        ]
    )
    plugin = main_mod.VirtualSessionPlugin(context)
    plugin.group_mgr = VirtualGroupManager(data_dir=tmp_path)

    resp = await plugin.list_confs()
    assert resp.status_code == 200
    body = json.loads(resp.body)
    # FakeContext 无 confs 内容 → 宽松判定无工具
    assert body[0] == {
        "id": "conf_a",
        "name": "档案A",
        "path": "/a",
        "has_callable_tools": False,
    }
    # 缺 id 回落 name，缺 name 回落 id，缺 path 为 None
    assert body[1] == {
        "id": "只有名字",
        "name": "只有名字",
        "path": None,
        "has_callable_tools": False,
    }
    assert body[2] == {
        "id": "conf_c",
        "name": "conf_c",
        "path": None,
        "has_callable_tools": False,
    }


@pytest.mark.asyncio
async def test_plugin_list_groups_and_sessions(tmp_path):
    plugin = main_mod.VirtualSessionPlugin(FakeContext())
    plugin.group_mgr = VirtualGroupManager(data_dir=tmp_path)
    group = plugin.group_mgr.create_group(
        "组A", count=2, platform_id="aiocqhttp", conf_id="conf_a"
    )
    plugin.group_mgr.update_session(group["sessions"][0]["id"], platform_id="telegram")

    resp = await plugin.list_groups()
    assert resp.status_code == 200
    body = json.loads(resp.body)
    assert len(body["groups"]) == 1
    assert body["groups"][0]["name"] == "组A"

    resp2 = await plugin.list_sessions()
    flat = json.loads(resp2.body)
    assert len(flat) == 2
    assert flat[0]["group_id"] == group["id"]
    assert flat[0]["platform_id"] == "telegram"  # 已解析会话覆盖
    assert flat[1]["platform_id"] == "aiocqhttp"
    assert flat[0]["conf_id"] == "conf_a"  # 覆盖后仍继承组档案


@pytest.mark.asyncio
async def test_plugin_run_test_missing_and_duplicate_ids(tmp_path):
    context = FakeContext()
    plugin = main_mod.VirtualSessionPlugin(context)
    plugin.group_mgr = VirtualGroupManager(data_dir=tmp_path)
    group = plugin.group_mgr.create_group("组A", count=2)
    sid_a = group["sessions"][0]["id"]

    # 完全缺失 → 404，错误消息列出缺失 id
    resp = await call_handler(plugin.run_test, {"sessions": ["vs_none"], "text": "hi"})
    assert resp.status_code == 404
    assert "vs_none" in json.loads(resp.body)["message"]

    # 重复 id + 缺失 id → 404，缺失列表去重、只报真正缺失的 id
    resp = await call_handler(
        plugin.run_test, {"sessions": [sid_a, sid_a, "vs_missing"], "text": "hi"}
    )
    assert resp.status_code == 404
    msg = json.loads(resp.body)["message"]
    assert msg.count("vs_missing") == 1


@pytest.mark.asyncio
async def test_plugin_run_test_text_must_be_string(tmp_path):
    """非字符串 text 直接 400，不再被静默 str() 强制转换（null → "None"、数字 → "123"）。"""
    plugin = main_mod.VirtualSessionPlugin(FakeContext())
    plugin.group_mgr = VirtualGroupManager(data_dir=tmp_path)
    group = plugin.group_mgr.create_group("组A", count=1)
    sid = group["sessions"][0]["id"]

    for bad in (123, None, ["hi"]):
        resp = await call_handler(plugin.run_test, {"sessions": [sid], "text": bad})
        assert resp.status_code == 400
        assert "text 必须是字符串" in json.loads(resp.body)["message"]


@pytest.mark.asyncio
async def test_plugin_run_test_returns_test_id(tmp_path):
    queue = asyncio.Queue()
    context = FakeContext(queue)
    plugin = main_mod.VirtualSessionPlugin(context)
    plugin.group_mgr = VirtualGroupManager(data_dir=tmp_path)
    group = plugin.group_mgr.create_group("组A", count=2)
    ids = [s["id"] for s in group["sessions"]]

    async def handler(event):
        await event.send(MessageChain().message("hi"))
        event.cleanup_temporary_local_files()

    task = asyncio.create_task(consume(queue, handler))
    try:
        resp = await call_handler(plugin.run_test, {"sessions": ids, "text": "你好"})
    finally:
        task.cancel()
    body = json.loads(resp.body)
    assert body["total"] == 2
    assert body["test_id"]


@pytest.mark.asyncio
async def test_plugin_test_run_status_not_found():
    plugin = main_mod.VirtualSessionPlugin(FakeContext())
    req = make_plugin_request({}, query="test_id=t_none")
    with bind_request_context(req):
        resp = await plugin.test_run_status()
    assert resp.status_code == 404
    assert json.loads(resp.body)["status"] == "error"


@pytest.mark.asyncio
async def test_plugin_session_pending_endpoint():
    """session_pending 返回全部在途条目（含会话与测试归属）。"""
    queue = asyncio.Queue()
    plugin = main_mod.VirtualSessionPlugin(FakeContext(queue))
    test_id = await plugin.runner.start(
        sessions=[make_session(1), make_session(2)], text="hi"
    )
    resp = await plugin.session_pending()
    body = json.loads(resp.body)
    assert {e["test_id"] for e in body["pending"]} == {test_id}
    assert {e["session_id"] for e in body["pending"]} == {"vs_1", "vs_2"}
    assert all(e["status"] == "submitted" for e in body["pending"])


@pytest.mark.asyncio
async def test_plugin_hook_handlers_track_llm_stages():
    """on_waiting_llm / on_llm hook 推进在途状态；非虚拟事件被忽略。"""
    queue = asyncio.Queue()
    plugin = main_mod.VirtualSessionPlugin(FakeContext(queue))
    await plugin.runner.start(sessions=[make_session(1)], text="hi")
    ev = queue.get_nowait()

    await plugin.on_waiting_llm(ev)
    assert plugin.runner.pending_entries()[0]["status"] == "waiting_llm"
    await plugin.on_llm(ev, SimpleNamespace())
    assert plugin.runner.pending_entries()[0]["status"] == "llm"

    # 真实平台消息（非 VirtualMessageEvent）静默忽略，状态不变
    foreign = SimpleNamespace(entry_id=ev.entry_id)
    await plugin.on_waiting_llm(foreign)
    assert plugin.runner.pending_entries()[0]["status"] == "llm"


@pytest.mark.asyncio
async def test_plugin_save_history_updates_existing(tmp_path):
    conv_mgr = FakeConvManager()
    plugin = main_mod.VirtualSessionPlugin(FakeContext(conv_mgr=conv_mgr))
    plugin.group_mgr = VirtualGroupManager(data_dir=tmp_path)
    group = plugin.group_mgr.create_group("组A", count=1)
    session = group["sessions"][0]
    umo = umo_of(plugin.group_mgr.effective(group, session))
    conv_mgr.add_history(
        umo,
        "测试",
        [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "在的"},
        ],
    )
    cid = (await conv_mgr.get_conversations(umo))[0].cid

    resp = await call_handler(
        plugin.save_history,
        {
            "id": session["id"],
            "conversations": [
                {
                    "conversation_id": cid,
                    "title": "改标题",
                    "history": [
                        {"role": "user", "content": "改过了"},
                        {"role": "assistant", "content": "在的"},
                    ],
                }
            ],
        },
    )
    assert resp.status_code == 200
    assert json.loads(resp.body)["saved"] == 1

    convs = await conv_mgr.get_conversations(umo)
    assert convs[0].title == "改标题"
    assert json.loads(convs[0].history)[0]["content"] == "改过了"


@pytest.mark.asyncio
async def test_plugin_save_history_adds_conversation(tmp_path):
    conv_mgr = FakeConvManager()
    plugin = main_mod.VirtualSessionPlugin(FakeContext(conv_mgr=conv_mgr))
    plugin.group_mgr = VirtualGroupManager(data_dir=tmp_path)
    group = plugin.group_mgr.create_group("组A", count=1)
    session = group["sessions"][0]
    umo = umo_of(plugin.group_mgr.effective(group, session))
    conv_mgr.add_history(umo, "旧对话", [{"role": "user", "content": "保留"}])

    resp = await call_handler(
        plugin.save_history,
        {
            "id": session["id"],
            "conversations": [
                {
                    "conversation_id": (await conv_mgr.get_conversations(umo))[0].cid,
                    "history": [{"role": "user", "content": "保留"}],
                },
                {"title": "新对话", "history": [{"role": "user", "content": "新增"}]},
            ],
        },
    )
    assert resp.status_code == 200
    convs = await conv_mgr.get_conversations(umo)
    assert len(convs) == 2
    assert any(c.title == "新对话" for c in convs)
    assert any(
        json.loads(c.history) == [{"role": "user", "content": "新增"}] for c in convs
    )


@pytest.mark.asyncio
async def test_plugin_save_history_deletes_unlisted(tmp_path):
    conv_mgr = FakeConvManager()
    plugin = main_mod.VirtualSessionPlugin(FakeContext(conv_mgr=conv_mgr))
    plugin.group_mgr = VirtualGroupManager(data_dir=tmp_path)
    group = plugin.group_mgr.create_group("组A", count=1)
    session = group["sessions"][0]
    umo = umo_of(plugin.group_mgr.effective(group, session))
    conv_mgr.add_history(umo, "对话A", [{"role": "user", "content": "a"}])
    conv_mgr.add_history(umo, "对话B", [{"role": "user", "content": "b"}])
    cid_a, cid_b = [c.cid for c in (await conv_mgr.get_conversations(umo))]

    # 只保留对话A：对话B 未列出 → 删除
    resp = await call_handler(
        plugin.save_history,
        {
            "id": session["id"],
            "conversations": [
                {
                    "conversation_id": cid_a,
                    "history": [{"role": "user", "content": "a"}],
                }
            ],
        },
    )
    assert resp.status_code == 200
    remaining = [c.cid for c in await conv_mgr.get_conversations(umo)]
    assert remaining == [cid_a]
    assert cid_b not in remaining


@pytest.mark.asyncio
async def test_plugin_save_history_invalid(tmp_path):
    conv_mgr = FakeConvManager()
    plugin = main_mod.VirtualSessionPlugin(FakeContext(conv_mgr=conv_mgr))
    plugin.group_mgr = VirtualGroupManager(data_dir=tmp_path)
    group = plugin.group_mgr.create_group("组A", count=1)
    session = group["sessions"][0]

    # conversations 不是数组
    resp = await call_handler(
        plugin.save_history, {"id": session["id"], "conversations": "not-a-list"}
    )
    assert resp.status_code == 400

    # history 不是对象数组
    resp = await call_handler(
        plugin.save_history,
        {"id": session["id"], "conversations": [{"history": ["bad"]}]},
    )
    assert resp.status_code == 400

    # 引用了不存在的 conversation_id 不再报错（占位新建，见
    # test_plugin_save_history_creates_placeholder_for_missing_cid）
    resp = await call_handler(
        plugin.save_history,
        {
            "id": session["id"],
            "conversations": [
                {
                    "conversation_id": "no_such_cid",
                    "history": [{"role": "user", "content": "x"}],
                }
            ],
        },
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_plugin_save_history_creates_placeholder_for_missing_cid(tmp_path):
    """会话从未产生对话（或历史被重置/删除）时，引用不存在的 conversation_id
    按整体替换语义新建占位对话，而不是报错。"""
    conv_mgr = FakeConvManager()
    plugin = main_mod.VirtualSessionPlugin(FakeContext(conv_mgr=conv_mgr))
    plugin.group_mgr = VirtualGroupManager(data_dir=tmp_path)
    group = plugin.group_mgr.create_group("组A", count=1)
    session = group["sessions"][0]
    umo = umo_of(plugin.group_mgr.effective(group, session))
    assert await conv_mgr.get_conversations(umo) == []

    resp = await call_handler(
        plugin.save_history,
        {
            "id": session["id"],
            "conversations": [
                {
                    "conversation_id": "phantom_cid",
                    "title": "占位对话",
                    "history": [{"role": "user", "content": "你好"}],
                }
            ],
        },
    )
    assert resp.status_code == 200
    convs = await conv_mgr.get_conversations(umo)
    assert len(convs) == 1
    assert convs[0].title == "占位对话"
    assert json.loads(convs[0].history) == [{"role": "user", "content": "你好"}]


@pytest.mark.asyncio
async def test_plugin_save_history_deduplicates_stale_cid(tmp_path):
    """同一失效 cid 在编辑器中重复出现时只新建一个占位对话，后续引用更新到它。"""
    conv_mgr = FakeConvManager()
    plugin = main_mod.VirtualSessionPlugin(FakeContext(conv_mgr=conv_mgr))
    plugin.group_mgr = VirtualGroupManager(data_dir=tmp_path)
    group = plugin.group_mgr.create_group("组A", count=1)
    session = group["sessions"][0]
    umo = umo_of(plugin.group_mgr.effective(group, session))

    resp = await call_handler(
        plugin.save_history,
        {
            "id": session["id"],
            "conversations": [
                {
                    "conversation_id": "phantom_cid",
                    "title": "对话一",
                    "history": [{"role": "user", "content": "一"}],
                },
                {
                    "conversation_id": "phantom_cid",
                    "title": "对话二",
                    "history": [{"role": "user", "content": "二"}],
                },
            ],
        },
    )
    assert resp.status_code == 200
    assert json.loads(resp.body)["saved"] == 2
    convs = await conv_mgr.get_conversations(umo)
    assert len(convs) == 1  # 同一引用只落盘一个占位对话
    assert convs[0].title == "对话二"  # 第二个对象的内容更新到首个占位对话
    assert json.loads(convs[0].history) == [{"role": "user", "content": "二"}]


@pytest.mark.asyncio
async def test_plugin_clone_sessions_copies_history(tmp_path):
    """克隆会话：同组内新建 N 个会话，每个新会话的历史与源会话一致（新 cid）。"""
    conv_mgr = FakeConvManager()
    plugin = main_mod.VirtualSessionPlugin(FakeContext(conv_mgr=conv_mgr))
    plugin.group_mgr = VirtualGroupManager(data_dir=tmp_path)
    group = plugin.group_mgr.create_group("组A", count=1)
    session = group["sessions"][0]
    resolved = plugin.group_mgr.effective(group, session)
    conv_mgr.add_history(
        umo_of(resolved),
        "对话",
        [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "在的"},
        ],
    )

    resp = await call_handler(
        plugin.clone_sessions, {"session_id": session["id"], "count": 2}
    )
    assert resp.status_code == 200
    body = json.loads(resp.body)
    assert body["group_id"] == group["id"]
    assert len(body["session_ids"]) == 2
    assert body["copied"] == 2

    # 同组会话数 1 → 3，新会话继承组配置
    updated = plugin.group_mgr.get_group(group["id"])
    assert len(updated["sessions"]) == 3
    for new_session in updated["sessions"][1:]:
        new_umo = umo_of(plugin.group_mgr.effective(updated, new_session))
        convs = await conv_mgr.get_conversations(new_umo)
        assert len(convs) == 1  # 新 cid（new_cid_*），不沿用源会话 cid
        assert json.loads(convs[0].history) == [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "在的"},
        ]
    # 源会话历史不受影响
    src_convs = await conv_mgr.get_conversations(umo_of(resolved))
    assert json.loads(src_convs[0].history)[0]["content"] == "hi"


@pytest.mark.asyncio
async def test_plugin_clone_sessions_validation(tmp_path):
    """克隆会话的参数校验：会话不存在 404、count 非法 400。"""
    plugin = main_mod.VirtualSessionPlugin(FakeContext())
    plugin.group_mgr = VirtualGroupManager(data_dir=tmp_path)
    group = plugin.group_mgr.create_group("组A", count=1)
    session = group["sessions"][0]

    resp = await call_handler(
        plugin.clone_sessions, {"session_id": "vs_none", "count": 1}
    )
    assert resp.status_code == 404

    for bad_count in (0, -1, "x", True, None):
        resp = await call_handler(
            plugin.clone_sessions, {"session_id": session["id"], "count": bad_count}
        )
        assert resp.status_code == 400, f"count={bad_count!r} 应被拒绝"

    resp = await call_handler(plugin.clone_sessions, {"session_id": session["id"]})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_plugin_clone_sessions_group_overflow(tmp_path):
    """克隆后会话数超过测试组上限时拒绝。"""
    plugin = main_mod.VirtualSessionPlugin(FakeContext())
    plugin.group_mgr = VirtualGroupManager(data_dir=tmp_path)
    group = plugin.group_mgr.create_group("组A", count=499)
    session = group["sessions"][0]

    resp = await call_handler(
        plugin.clone_sessions, {"session_id": session["id"], "count": 2}
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_plugin_derive_session_copies_history(tmp_path):
    """衍生会话：创建全新测试组，组内每个会话的历史都与源会话一致。"""
    conv_mgr = FakeConvManager()
    plugin = main_mod.VirtualSessionPlugin(FakeContext(conv_mgr=conv_mgr))
    plugin.group_mgr = VirtualGroupManager(data_dir=tmp_path)
    group = plugin.group_mgr.create_group("原组", count=1)
    session = group["sessions"][0]
    resolved = plugin.group_mgr.effective(group, session)
    conv_mgr.add_history(umo_of(resolved), "对话", [{"role": "user", "content": "hi"}])

    resp = await call_handler(
        plugin.derive_session,
        {"session_id": session["id"], "count": 3, "name": "衍生组"},
    )
    assert resp.status_code == 200
    body = json.loads(resp.body)
    assert body["group_name"] == "衍生组"
    assert len(body["session_ids"]) == 3
    assert body["copied"] == 3

    new_group = plugin.group_mgr.get_group(body["group_id"])
    assert new_group is not None
    assert new_group["id"] != group["id"]  # 全新测试组
    for new_session in new_group["sessions"]:
        new_umo = umo_of(plugin.group_mgr.effective(new_group, new_session))
        convs = await conv_mgr.get_conversations(new_umo)
        assert len(convs) == 1
        assert json.loads(convs[0].history) == [{"role": "user", "content": "hi"}]
    # 源组与会话不受影响
    assert len(plugin.group_mgr.get_group(group["id"])["sessions"]) == 1


@pytest.mark.asyncio
async def test_plugin_derive_session_default_name_and_config(tmp_path):
    """衍生组默认名「<原组名> 衍生」，并继承源组的配置（含 conf_id 路由应用）。"""
    ucr = FakeUCR()
    plugin = main_mod.VirtualSessionPlugin(FakeContext(ucr=ucr))
    plugin.group_mgr = VirtualGroupManager(data_dir=tmp_path)
    group = plugin.group_mgr.create_group(
        "提示词A", count=1, platform_id="telegram", conf_id="conf_1"
    )
    session = group["sessions"][0]

    resp = await call_handler(
        plugin.derive_session, {"session_id": session["id"], "count": 2}
    )
    assert resp.status_code == 200
    body = json.loads(resp.body)
    assert body["group_name"] == "提示词A 衍生"

    new_group = plugin.group_mgr.get_group(body["group_id"])
    assert new_group["platform_id"] == "telegram"
    assert new_group["conf_id"] == "conf_1"
    # 组配置档案路由已应用到新会话
    new_umo = umo_of(plugin.group_mgr.effective(new_group, new_group["sessions"][0]))
    assert ucr.umop_to_conf_id.get(new_umo) == "conf_1"


@pytest.mark.asyncio
async def test_plugin_derive_session_validation(tmp_path):
    """衍生会话的参数校验：会话不存在 404、count 非法 400。"""
    plugin = main_mod.VirtualSessionPlugin(FakeContext())
    plugin.group_mgr = VirtualGroupManager(data_dir=tmp_path)
    group = plugin.group_mgr.create_group("组A", count=1)
    session = group["sessions"][0]

    resp = await call_handler(
        plugin.derive_session, {"session_id": "vs_none", "count": 1}
    )
    assert resp.status_code == 404
    resp = await call_handler(
        plugin.derive_session, {"session_id": session["id"], "count": 0}
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_plugin_regenerate_history(tmp_path):
    queue = asyncio.Queue()
    context = FakeContext(queue)
    plugin = main_mod.VirtualSessionPlugin(context)
    plugin.group_mgr = VirtualGroupManager(data_dir=tmp_path)
    group = plugin.group_mgr.create_group("组A", count=1)
    session = group["sessions"][0]
    umo = umo_of(plugin.group_mgr.effective(group, session))
    context.conversation_manager.add_history(
        umo,
        "测试",
        [
            {"role": "user", "content": "第一问"},
            {"role": "assistant", "content": "回答一"},
            {"role": "user", "content": "第二问"},
            {"role": "assistant", "content": "回答二"},
        ],
    )

    received = []

    async def handler(event):
        received.append(event.message_str)
        event.cleanup_temporary_local_files()

    task = asyncio.create_task(consume(queue, handler))
    try:
        # 点击第 4 条（assistant 回复"回答二"）重新生成该轮
        resp = await call_handler(
            plugin.regenerate_history, {"id": session["id"], "index": 3}
        )
        # 重发是异步入队执行，轮询等待 handler 收到该消息（替代固定 sleep）
        await wait_until(lambda: received == ["第二问"])
    finally:
        task.cancel()
    body = json.loads(resp.body)
    assert body["total"] == 1
    # 该轮（第二问）及其之后的历史被截断
    convs = await context.conversation_manager.get_conversations(umo)
    assert json.loads(convs[0].history) == [
        {"role": "user", "content": "第一问"},
        {"role": "assistant", "content": "回答一"},
    ]
    # 重发该轮 user 消息
    assert received == ["第二问"]


@pytest.mark.asyncio
async def test_plugin_regenerate_history_no_history(tmp_path):
    queue = asyncio.Queue()
    context = FakeContext(queue)
    plugin = main_mod.VirtualSessionPlugin(context)
    plugin.group_mgr = VirtualGroupManager(data_dir=tmp_path)
    group = plugin.group_mgr.create_group("组A", count=1)
    session = group["sessions"][0]

    resp = await call_handler(
        plugin.regenerate_history, {"id": session["id"], "index": 0}
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_plugin_regenerate_history_index_out_of_range(tmp_path):
    queue = asyncio.Queue()
    context = FakeContext(queue)
    plugin = main_mod.VirtualSessionPlugin(context)
    plugin.group_mgr = VirtualGroupManager(data_dir=tmp_path)
    group = plugin.group_mgr.create_group("组A", count=1)
    session = group["sessions"][0]
    umo = umo_of(plugin.group_mgr.effective(group, session))
    context.conversation_manager.add_history(
        umo, "测试", [{"role": "user", "content": "问"}]
    )

    resp = await call_handler(
        plugin.regenerate_history, {"id": session["id"], "index": 5}
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_plugin_regenerate_history_no_user_before(tmp_path):
    """index 之前没有 user 发言（历史以 assistant 开头）时无法定位轮次。"""
    queue = asyncio.Queue()
    context = FakeContext(queue)
    plugin = main_mod.VirtualSessionPlugin(context)
    plugin.group_mgr = VirtualGroupManager(data_dir=tmp_path)
    group = plugin.group_mgr.create_group("组A", count=1)
    session = group["sessions"][0]
    umo = umo_of(plugin.group_mgr.effective(group, session))
    context.conversation_manager.add_history(
        umo, "测试", [{"role": "assistant", "content": "在的"}]
    )

    resp = await call_handler(
        plugin.regenerate_history, {"id": session["id"], "index": 0}
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_plugin_regenerate_history_empty_user_text(tmp_path):
    """命中的轮次 user 消息内容为空（parts 全为空串）时拒绝重新生成。"""
    queue = asyncio.Queue()
    context = FakeContext(queue)
    plugin = main_mod.VirtualSessionPlugin(context)
    plugin.group_mgr = VirtualGroupManager(data_dir=tmp_path)
    group = plugin.group_mgr.create_group("组A", count=1)
    session = group["sessions"][0]
    umo = umo_of(plugin.group_mgr.effective(group, session))
    context.conversation_manager.add_history(
        umo,
        "测试",
        [
            {"role": "user", "content": [{"text": ""}]},
            {"role": "assistant", "content": "在的"},
        ],
    )

    resp = await call_handler(
        plugin.regenerate_history, {"id": session["id"], "index": 1}
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_plugin_regenerate_history_with_conversation_id(tmp_path):
    """多对话历史：conversation_id 定位到指定对话截断并重发，其他对话不受影响。"""
    queue = asyncio.Queue()
    context = FakeContext(queue)
    plugin = main_mod.VirtualSessionPlugin(context)
    plugin.group_mgr = VirtualGroupManager(data_dir=tmp_path)
    group = plugin.group_mgr.create_group("组A", count=1)
    session = group["sessions"][0]
    umo = umo_of(plugin.group_mgr.effective(group, session))
    context.conversation_manager.add_history(
        umo,
        "对话一",
        [
            {"role": "user", "content": "旧问"},
            {"role": "assistant", "content": "旧答"},
        ],
    )
    context.conversation_manager.add_history(
        umo,
        "对话二",
        [
            {"role": "user", "content": "新问"},
            {"role": "assistant", "content": "新答"},
        ],
    )
    convs = await context.conversation_manager.get_conversations(umo)
    old_cid, new_cid = convs[0].cid, convs[1].cid

    received = []

    async def handler(event):
        received.append(event.message_str)
        event.cleanup_temporary_local_files()

    task = asyncio.create_task(consume(queue, handler))
    try:
        # 对非当前对话（对话一）重新生成第 2 条 → 定位该对话截断重发"旧问"
        resp = await call_handler(
            plugin.regenerate_history,
            {"id": session["id"], "index": 1, "conversation_id": old_cid},
        )
        # 重发是异步入队执行，轮询等待 handler 收到该消息（替代固定 sleep）
        await wait_until(lambda: received == ["旧问"])
    finally:
        task.cancel()
    assert resp.status_code == 200
    convs = await context.conversation_manager.get_conversations(umo)
    by_cid = {c.cid: c for c in convs}
    assert json.loads(by_cid[old_cid].history) == []
    assert received == ["旧问"]
    # 对话二（当前对话）不受影响
    assert json.loads(by_cid[new_cid].history) == [
        {"role": "user", "content": "新问"},
        {"role": "assistant", "content": "新答"},
    ]


@pytest.mark.asyncio
async def test_plugin_regenerate_history_bad_conversation_id(tmp_path):
    """conversation_id 类型不合法时拒绝（400），而非静默忽略。"""
    queue = asyncio.Queue()
    context = FakeContext(queue)
    plugin = main_mod.VirtualSessionPlugin(context)
    plugin.group_mgr = VirtualGroupManager(data_dir=tmp_path)
    group = plugin.group_mgr.create_group("组A", count=1)
    session = group["sessions"][0]

    resp = await call_handler(
        plugin.regenerate_history,
        {"id": session["id"], "index": 0, "conversation_id": 123},
    )
    assert resp.status_code == 400


def test_main_module_importable():
    assert main_mod.PLUGIN_NAME == "astrbot_plugin_testbench"
    assert main_mod.VirtualSessionPlugin is not None


def test_msg_text_parts_array():
    """_msg_text 对 content 为 parts 数组（字符串/对象混合）的提取。"""
    ops_cls = hops_mod.HistoryOps
    assert ops_cls._msg_text({"content": "纯字符串"}) == "纯字符串"
    assert ops_cls._msg_text({"content": None}) == ""
    assert ops_cls._msg_text({}) == ""
    msg = {
        "content": [
            "纯文本段",
            {"text": "对象文本段"},
            {"content": "content 键"},
            {"type": "image", "url": "..."},  # 无 text/content → 空串，被过滤
            {"text": "末尾段"},
        ]
    }
    assert ops_cls._msg_text(msg) == "纯文本段\n对象文本段\ncontent 键\n末尾段"


def test_session_history_endpoint(tmp_path):
    conv_mgr = FakeConvManager()
    plugin = main_mod.VirtualSessionPlugin(FakeContext(conv_mgr=conv_mgr))
    plugin.group_mgr = VirtualGroupManager(data_dir=tmp_path)
    group = plugin.group_mgr.create_group("组A", count=1, platform_id="webchat")
    session = group["sessions"][0]

    conv_mgr.add_history(
        umo_of(session),
        "测试会话",
        [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好呀"},
        ],
    )

    resp = asyncio.run(plugin.session_history(session["id"]))
    assert resp.status_code == 200
    body = json.loads(resp.body)
    assert len(body["conversations"]) == 1
    conv = body["conversations"][0]
    assert conv["title"] == "测试会话"
    assert conv["history"][0]["role"] == "user"
    assert conv["history"][1]["content"] == "你好呀"


def test_session_history_endpoint_not_found(tmp_path):
    plugin = main_mod.VirtualSessionPlugin(FakeContext())
    plugin.group_mgr = VirtualGroupManager(data_dir=tmp_path)
    resp = asyncio.run(plugin.session_history("vs_none"))
    assert resp.status_code == 404


def test_session_history_empty_conversations(tmp_path):
    conv_mgr = FakeConvManager()
    plugin = main_mod.VirtualSessionPlugin(FakeContext(conv_mgr=conv_mgr))
    plugin.group_mgr = VirtualGroupManager(data_dir=tmp_path)
    group = plugin.group_mgr.create_group("组A", count=1, platform_id="webchat")
    session = group["sessions"][0]

    resp = asyncio.run(plugin.session_history(session["id"]))
    assert resp.status_code == 200
    assert json.loads(resp.body)["conversations"] == []


@pytest.mark.asyncio
async def test_plugin_testset_crud(tmp_path):
    plugin = main_mod.VirtualSessionPlugin(FakeContext())
    plugin.testset_store = TestsetStore(data_dir=tmp_path)

    resp = await call_handler(
        plugin.create_testset,
        {
            "name": "回归测试",
            "messages": [
                {
                    "text": "第一问",
                    "rule": {"type": "contains", "value": "你好"},
                    "auto_at": False,
                },
                {"text": "第二问"},
            ],
        },
    )
    assert resp.status_code == 200
    ts = json.loads(resp.body)
    assert ts["id"].startswith("ts_")
    assert len(ts["messages"]) == 2
    assert ts["messages"][0]["auto_at"] is False  # 消息级 auto@ 保留
    assert ts["messages"][1]["rules"] == []  # 缺 rule → 空列表
    assert "auto_at" not in ts["messages"][1]  # 缺省不落字段（发送时按 True）

    resp = await plugin.list_testsets()
    assert len(json.loads(resp.body)["testsets"]) == 1

    resp = await call_handler(
        plugin.update_testset,
        {"name": "改名", "messages": [{"text": "新问"}]},
        ts["id"],
    )
    body = json.loads(resp.body)
    assert body["name"] == "改名"
    assert len(body["messages"]) == 1

    resp = await call_handler(plugin.delete_testsets, {"ids": [ts["id"]]})
    assert json.loads(resp.body)["deleted"] == 1
    assert len(plugin.testset_store.list_testsets()) == 0


@pytest.mark.asyncio
async def test_plugin_testset_update_empty_messages(tmp_path):
    # 已存在测试集允许整体替换为空消息序列（清空内容、保留命名条目）
    plugin = main_mod.VirtualSessionPlugin(FakeContext())
    plugin.testset_store = TestsetStore(data_dir=tmp_path)
    ts = plugin.testset_store.create_testset("T", [{"text": "m1"}, {"text": "m2"}])

    resp = await call_handler(
        plugin.update_testset,
        {"name": "清空", "messages": []},
        ts["id"],
    )
    assert resp.status_code == 200
    body = json.loads(resp.body)
    assert body["name"] == "清空"
    assert body["messages"] == []
    assert body["batch_ranges"] == []


@pytest.mark.asyncio
async def test_plugin_testset_crud_validation(tmp_path):
    plugin = main_mod.VirtualSessionPlugin(FakeContext())
    plugin.testset_store = TestsetStore(data_dir=tmp_path)

    cases = [
        {"name": "x", "messages": "不是数组"},
        {"name": "x", "messages": [{"text": "  "}]},
        {"name": "x", "messages": [{"text": "ok", "rule": "regex"}]},
        {
            "name": "x",
            "messages": [{"text": "ok", "auto_at": "yes"}],
        },  # auto@ 须为 bool
        {
            "name": "x",
            "messages": [
                {"text": f"m{i}"} for i in range(tss_mod.MAX_MESSAGES_PER_TESTSET + 1)
            ],
        },
    ]
    for payload in cases:
        resp = await call_handler(plugin.create_testset, payload)
        assert resp.status_code == 400, payload

    # 空消息允许创建（先建命名条目、再在窗口里加消息）
    resp = await call_handler(plugin.create_testset, {"name": "空建", "messages": []})
    assert resp.status_code == 200
    assert json.loads(resp.body)["messages"] == []

    resp = await call_handler(
        plugin.update_testset, {"name": "x", "messages": [{"text": "ok"}]}, "ts_none"
    )
    assert resp.status_code == 404

    resp = await call_handler(plugin.delete_testsets, {"ids": []})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_plugin_run_testset_validation(tmp_path):
    plugin = main_mod.VirtualSessionPlugin(FakeContext())
    plugin.group_mgr = VirtualGroupManager(data_dir=tmp_path)
    plugin.testset_store = TestsetStore(data_dir=tmp_path)
    ts = plugin.testset_store.create_testset("T", [{"text": "m1"}])
    ts_empty = plugin.testset_store.create_testset("空", [{"text": "  "}])

    resp = await call_handler(plugin.run_testset, {"sessions": ["vs_1"]})
    assert resp.status_code == 400  # 缺 testset_id

    resp = await call_handler(
        plugin.run_testset, {"testset_id": "ts_none", "sessions": ["vs_1"]}
    )
    assert resp.status_code == 404

    resp = await call_handler(
        plugin.run_testset, {"testset_id": ts_empty["id"], "sessions": ["vs_1"]}
    )
    assert resp.status_code == 400  # 测试集没有消息

    resp = await call_handler(
        plugin.run_testset, {"testset_id": ts["id"], "sessions": ["vs_missing"]}
    )
    assert resp.status_code == 404  # 会话缺失


@pytest.mark.asyncio
async def test_plugin_testset_batch_ranges_validation(tmp_path):
    plugin = main_mod.VirtualSessionPlugin(FakeContext())
    plugin.testset_store = TestsetStore(data_dir=tmp_path)

    # 非法 batch_ranges → 400
    invalid = [
        "不是数组",
        [["a", 1]],
        [[True, 1]],
        [[0, 2]],  # 越界（仅 2 条消息，最大索引 1）
        [[1, 0]],  # s > e
        [[0, 1], [1, 1]],  # 重叠
    ]
    for br in invalid:
        payload = {
            "name": "T",
            "messages": [{"text": "m1"}, {"text": "m2"}],
            "batch_ranges": br,
        }
        resp = await call_handler(plugin.create_testset, payload)
        assert resp.status_code == 400, br

    # 合法 → 200 且返回规范化（按 start 排序）
    resp = await call_handler(
        plugin.create_testset,
        {
            "name": "T",
            "messages": [{"text": "m1"}, {"text": "m2"}, {"text": "m3"}],
            "batch_ranges": [[2, 2], [0, 0]],
        },
    )
    assert resp.status_code == 200
    body = json.loads(resp.body)
    assert body["batch_ranges"] == [[0, 0], [2, 2]]

    # 更新也校验 batch_ranges 并透传
    resp = await call_handler(
        plugin.update_testset,
        {
            "name": "T",
            "messages": [{"text": "m1"}, {"text": "m2"}],
            "batch_ranges": [[0, 1]],
        },
        body["id"],
    )
    assert resp.status_code == 200
    assert json.loads(resp.body)["batch_ranges"] == [[0, 1]]

    resp = await call_handler(
        plugin.update_testset,
        {
            "name": "T",
            "messages": [{"text": "m1"}, {"text": "m2"}],
            "batch_ranges": [[0, 5]],
        },
        body["id"],
    )
    assert resp.status_code == 400


def test_testset_store_rules_and_is_command(tmp_path):
    """rules 列表归一（dict 保留 / 非 dict 丢弃）、is_command 仅 True 落盘、
    旧单条 rule 迁移为 rules 列表。"""
    store = TestsetStore(data_dir=tmp_path)
    ts = store.create_testset(
        "规则",
        [
            {"text": "a", "rules": [{"type": "contains", "value": "x"}, None, "坏"]},
            {"text": "b", "is_command": True},
            {"text": "c", "is_command": False},
        ],
    )
    msgs = ts["messages"]
    assert msgs[0]["rules"] == [{"type": "contains", "value": "x"}]  # 非 dict 丢弃
    assert msgs[1]["is_command"] is True
    assert "is_command" not in msgs[2]  # False 不落字段（缺省 False）
    # 旧格式单条 rule → rules 单元素列表
    ts2 = store.create_testset("旧格式", [{"text": "d", "rule": {"type": "non_empty"}}])
    assert ts2["messages"][0]["rules"] == [{"type": "non_empty"}]

    # 旧数据迁移：_load 把 rule 键迁移为 rules 并清理残留（防全量写 JSON 残留）
    (tmp_path / "virtual_session" / "testsets.json").write_text(
        json.dumps(
            {
                "testsets": [
                    {
                        "id": "ts_old",
                        "name": "旧",
                        "created_at": 0,
                        "messages": [
                            {"text": "m", "rule": {"type": "contains", "value": "y"}}
                        ],
                        "batch_ranges": [],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    reloaded = TestsetStore(data_dir=tmp_path)
    old_msgs = reloaded.get_testset("ts_old")["messages"]
    assert old_msgs[0]["rules"] == [{"type": "contains", "value": "y"}]
    assert "rule" not in old_msgs[0]


def test_testset_store_identity_fields(tmp_path):
    """身份配置落盘：模式归一、id 引用清洗、快照白名单清洗（显式 false 保留）。"""
    store = TestsetStore(data_dir=tmp_path)
    ts = store.create_testset(
        "身份",
        [{"text": "m"}],
        identity_mode="pool",
        identity_id="   ",
        chat_group_id="cg_1",
        identity_snapshot={
            "id": "id_1",
            "name": "小明",
            "extra": "丢",
            "is_admin": False,
        },
        pool_snapshot={
            "name": "测试群",
            "members": [{"id": "id_1", "name": "小明"}, "坏"],
        },
    )
    assert ts["identity_mode"] == "pool"
    assert ts["chat_group_id"] == "cg_1"
    assert ts["identity_id"] is None  # 空白 id 引用归一 None
    assert ts["identity_snapshot"] == {
        "id": "id_1",
        "name": "小明",
        "is_admin": False,  # 显式 false 保留（快照白名单键）
    }
    assert ts["pool_snapshot"]["name"] == "测试群"
    assert ts["pool_snapshot"]["members"] == [{"id": "id_1", "name": "小明"}]

    # 非法模式回退 single；非 dict 快照 → None
    ts2 = store.create_testset(
        "B",
        [{"text": "m"}],
        identity_mode="bad",
        identity_snapshot={"id": "id_x", "name": "x", "sender_id": "sx"},
        pool_snapshot="不是dict",
    )
    assert ts2["identity_mode"] == "single"
    assert ts2["identity_snapshot"] == {"id": "id_x", "name": "x", "sender_id": "sx"}
    assert ts2["pool_snapshot"] is None


@pytest.mark.asyncio
async def test_plugin_testset_api_identity_snapshot(tmp_path):
    """API 层快照解析：single 按 identity_id、pool 按 chat_group_id 从身份库 /
    群聊库解析；导入路径（payload 携带快照）优先使用携带快照。"""
    plugin = main_mod.VirtualSessionPlugin(FakeContext())
    plugin.testset_store = TestsetStore(data_dir=tmp_path)
    plugin.identity_store = IdentityStore(data_dir=tmp_path)
    plugin.chat_group_store = ChatGroupStore(data_dir=tmp_path)

    admin = plugin.identity_store.create_identity("管理员", "root", is_admin=True)
    member = plugin.identity_store.create_identity("群友", "member_1")
    cg = plugin.chat_group_store.create_chat_group("测试群", [member["id"]])

    # single：payload 无快照 → 按 identity_id 从身份库解析内联快照
    resp = await call_handler(
        plugin.create_testset,
        {
            "name": "单身份",
            "messages": [{"text": "m"}],
            "identity_mode": "single",
            "identity_id": admin["id"],
        },
    )
    assert resp.status_code == 200
    ts = json.loads(resp.body)
    assert ts["identity_mode"] == "single"
    assert ts["identity_id"] == admin["id"]
    assert ts["identity_snapshot"] == {
        "id": admin["id"],
        "name": "管理员",
        "sender_id": "root",
        "sender_name": "管理员",
        "is_admin": True,
    }

    # pool：无快照 → 按 chat_group_id 从群聊库解析成员身份池
    resp = await call_handler(
        plugin.create_testset,
        {
            "name": "池",
            "messages": [{"text": "m"}],
            "identity_mode": "pool",
            "chat_group_id": cg["id"],
        },
    )
    ts = json.loads(resp.body)
    assert ts["identity_mode"] == "pool"
    assert ts["chat_group_id"] == cg["id"]
    assert ts["pool_snapshot"]["name"] == "测试群"
    assert ts["pool_snapshot"]["members"] == [
        {
            "id": member["id"],
            "name": "群友",
            "sender_id": "member_1",
            "sender_name": "群友",
            "is_admin": False,
        }
    ]

    # 导入路径：payload 携带快照优先（身份库可能没有该记录），不解析本地库
    resp = await call_handler(
        plugin.create_testset,
        {
            "name": "导入",
            "messages": [{"text": "m"}],
            "identity_snapshot": {
                "id": "id_imported",
                "name": "导入身份",
                "sender_id": "imp",
            },
        },
    )
    ts = json.loads(resp.body)
    assert ts["identity_snapshot"] == {
        "id": "id_imported",
        "name": "导入身份",
        "sender_id": "imp",
    }
    assert ts["identity_id"] is None  # 未提供 id 引用，仅内联快照自包含


@pytest.mark.asyncio
async def test_plugin_run_testset_ok(tmp_path):
    queue = asyncio.Queue()
    context = FakeContext(queue)
    plugin = main_mod.VirtualSessionPlugin(context)
    plugin.group_mgr = VirtualGroupManager(data_dir=tmp_path)
    plugin.testset_store = TestsetStore(data_dir=tmp_path)
    group = plugin.group_mgr.create_group("组A", count=1)
    sid = group["sessions"][0]["id"]
    ts = plugin.testset_store.create_testset("T", [{"text": "m1"}])

    async def handler(event):
        await event.send(MessageChain().message("ok"))
        event.cleanup_temporary_local_files()

    task = asyncio.create_task(consume(queue, handler))
    try:
        resp = await call_handler(
            plugin.run_testset, {"testset_id": ts["id"], "sessions": [sid]}
        )
        assert resp.status_code == 200
        body = json.loads(resp.body)
        assert body["run_id"].startswith("tr_")
        assert body["steps"] == 1
        rec = await wait_testset_done(plugin.testset_runner, body["run_id"])
    finally:
        task.cancel()
    assert rec["status"] == "done"
    assert rec["steps"][0]["results"][0]["status"] == "ok"


@pytest.mark.asyncio
async def test_plugin_run_testset_rejects_concurrent_run(tmp_path):
    """并发测试集运行守卫：已有运行中时启动新运行返回 400。

    前端进度是单槽状态（activeRunId / 取消按钮 / 步骤去重集合只支持一个
    运行），两个运行的事件流会互相污染，故 run_testset 入口必须拒绝。
    """
    queue = asyncio.Queue()
    plugin = main_mod.VirtualSessionPlugin(FakeContext(queue))
    plugin.group_mgr = VirtualGroupManager(data_dir=tmp_path)
    plugin.testset_store = TestsetStore(data_dir=tmp_path)
    group = plugin.group_mgr.create_group("组A", count=1)
    sid = group["sessions"][0]["id"]
    ts = plugin.testset_store.create_testset("T", [{"text": "m1"}])

    # 不消费队列启动运行 → 步骤悬挂，run 保持 running
    resp = await call_handler(
        plugin.run_testset, {"testset_id": ts["id"], "sessions": [sid]}
    )
    assert resp.status_code == 200
    assert plugin.testset_runner.has_active_run() is True

    # 已有运行中 → 第二个运行被拒绝（400）
    resp2 = await call_handler(
        plugin.run_testset, {"testset_id": ts["id"], "sessions": [sid]}
    )
    assert resp2.status_code == 400

    # 收尾：放行悬挂的 _await_event（事件入队但无人消费，消息已清理即完成）
    while not queue.empty():
        queue.get_nowait().cleanup_temporary_local_files()
    await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_plugin_testset_run_status_abort_runs(tmp_path):
    queue = asyncio.Queue()
    plugin = main_mod.VirtualSessionPlugin(FakeContext(queue))
    plugin.group_mgr = VirtualGroupManager(data_dir=tmp_path)
    plugin.testset_store = TestsetStore(data_dir=tmp_path)
    group = plugin.group_mgr.create_group("组A", count=1)
    sid = group["sessions"][0]["id"]
    ts = plugin.testset_store.create_testset("T", [{"text": "m1"}])

    # 缺 run_id → 400
    req = make_plugin_request({}, query="")
    with bind_request_context(req):
        resp = await plugin.testset_run_status()
    assert resp.status_code == 400

    # 未知 run_id → 404
    req = make_plugin_request({}, query="run_id=tr_none")
    with bind_request_context(req):
        resp = await plugin.testset_run_status()
    assert resp.status_code == 404

    # 启动运行 → status 可查询
    run_id = plugin.testset_runner.start_run(ts, plugin.group_mgr.effective_many([sid]))
    req = make_plugin_request({}, query=f"run_id={run_id}")
    with bind_request_context(req):
        resp = await plugin.testset_run_status()
    assert resp.status_code == 200
    assert json.loads(resp.body)["run_id"] == run_id

    # abort：存在 → True；未知 → False
    resp = await call_handler(plugin.abort_testset_run, {"run_id": run_id})
    assert json.loads(resp.body)["cancelled"] is True
    resp = await call_handler(plugin.abort_testset_run, {"run_id": "tr_none"})
    assert json.loads(resp.body)["cancelled"] is False

    # runs 列表包含该运行（testset_id 可选 query，不带则返回全部）
    req = make_plugin_request({}, query="")
    with bind_request_context(req):
        resp = await plugin.testset_runs()
    assert any(r["run_id"] == run_id for r in json.loads(resp.body)["runs"])

    # 收尾：放行悬挂的 _await_event
    while not queue.empty():
        queue.get_nowait().cleanup_temporary_local_files()
    await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_plugin_run_test_with_assertion(tmp_path):
    queue = asyncio.Queue()
    plugin = main_mod.VirtualSessionPlugin(FakeContext(queue))
    plugin.group_mgr = VirtualGroupManager(data_dir=tmp_path)
    group = plugin.group_mgr.create_group("组A", count=1)
    sid = group["sessions"][0]["id"]

    async def handler(event):
        await event.send(MessageChain().message("回复内容"))
        event.cleanup_temporary_local_files()

    task = asyncio.create_task(consume(queue, handler))
    try:
        resp = await call_handler(
            plugin.run_test,
            {
                "sessions": [sid],
                "text": "hi",
                "assertion": {"type": "contains", "value": "回复内容"},
            },
        )
        body = json.loads(resp.body)
        assert resp.status_code == 200
        rec = await wait_run_done(plugin.runner, body["test_id"])
    finally:
        task.cancel()
    assert rec["results"][0]["assertion"]["pass"] is True

    # 非 dict assertion → 400
    resp = await call_handler(
        plugin.run_test, {"sessions": [sid], "text": "hi", "assertion": "regex"}
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_events_endpoint_sse_format_and_unsubscribe():
    """TB-30: /events SSE 端点：事件序列化为 data: 行；generator 结束自动退订。"""
    plugin = main_mod.VirtualSessionPlugin(FakeContext())
    resp = await plugin.events()
    chunks: list[str] = []

    async def drain():
        async for chunk in resp.body_iterator:
            chunks.append(chunk)
            if "data:" in chunk:
                break
        # async for 的 break/return 不会自动 aclose 生成器——显式关闭，
        # 触发 gen 的 finally 退订（否则消费者永久滞留、后续 wait_until 超时）
        await resp.body_iterator.aclose()

    task = asyncio.create_task(drain())
    # 等订阅建立（generator 首轮迭代在 subscribe 之后才可发布）
    await wait_until(lambda: len(plugin.event_bus._consumers) == 1)
    plugin.event_bus.publish({"type": "test_ping", "n": 1})
    await asyncio.wait_for(task, timeout=5)
    assert any('"type": "test_ping"' in c for c in chunks)
    # generator 关闭 → finally 退订，后续事件不再送达该消费者
    await wait_until(lambda: len(plugin.event_bus._consumers) == 0)


def test_conf_tool_info():
    """配置档案工具能力判定：与 AstrBot 运行时挂载逻辑一致。"""
    # 非 dict / None → 全 False（宽松）
    assert conf_tool_info(None)["has_callable_tools"] is False
    assert conf_tool_info([])["has_callable_tools"] is False
    # 空 dict：cron 工具默认开启（add_cron_tools 缺省 True）→ 命中告警
    assert conf_tool_info({})["has_callable_tools"] is True
    assert conf_tool_info({})["cron_tools"] is True
    # 全部显式关闭 → 无工具
    off = {
        "provider_settings": {
            "computer_use_runtime": "none",
            "web_search": False,
            "proactive_capability": {"add_cron_tools": False},
        },
        "kb_agentic_mode": False,
    }
    assert conf_has_callable_tools(off) is False
    # 各开关单独命中
    for runtime in ("local", "sandbox"):
        info = conf_tool_info({"provider_settings": {"computer_use_runtime": runtime}})
        assert info["has_callable_tools"] is True
        assert info["computer_use_runtime"] == runtime
    assert conf_has_callable_tools(
        {"provider_settings": {"computer_use_runtime": "none", "web_search": True}}
    )
    assert conf_has_callable_tools({"kb_agentic_mode": True})
    assert conf_has_callable_tools(
        {
            "provider_settings": {
                "web_search": True,
                "proactive_capability": {"add_cron_tools": False},
            }
        }
    )


def test_conf_tool_info_wrong_typed_nested():
    """嵌套键类型错误（配置被手改坏）不崩溃：非 dict 按空对象处理。

    与缺键同语义——空配置 cron 工具缺省 True，故坏值也不改变判定，
    只是不再 AttributeError。
    """
    assert conf_tool_info({"provider_settings": "x"})["has_callable_tools"] is True
    info = conf_tool_info({"provider_settings": {"proactive_capability": 5}})
    assert (
        info["has_callable_tools"] is True
    )  # proactive_capability 非 dict → cron 缺省 True
    assert info["cron_tools"] is True


@pytest.mark.asyncio
async def test_list_confs_has_callable_tools(tmp_path):
    """list_confs 暴露每档案的 has_callable_tools（按 confs 内容实时判定）。"""
    context = FakeContext(
        conf_list=[
            {"id": "default", "name": "默认", "path": "/d"},
            {"id": "conf_x", "name": "危险", "path": "/x"},
        ]
    )
    context.astrbot_config_mgr.confs = {
        "default": {},
        "conf_x": {"provider_settings": {"computer_use_runtime": "local"}},
    }
    plugin = main_mod.VirtualSessionPlugin(context)
    plugin.group_mgr = VirtualGroupManager(data_dir=tmp_path)

    resp = await plugin.list_confs()
    body = json.loads(resp.body)
    by_id = {c["id"]: c for c in body}
    assert by_id["default"]["has_callable_tools"] is True  # cron 默认开启
    assert by_id["conf_x"]["has_callable_tools"] is True

    # 档案对象在 conf_list 中但 confs 字典无内容 → 宽松 False（显示用途不误报）
    ghost = FakeContext(conf_list=[{"id": "ghost", "name": "无内容"}])
    plugin2 = main_mod.VirtualSessionPlugin(ghost)
    plugin2.group_mgr = VirtualGroupManager(data_dir=tmp_path)
    resp2 = await plugin2.list_confs()
    assert json.loads(resp2.body)[0]["has_callable_tools"] is False


@pytest.mark.asyncio
async def test_identity_api_is_admin(tmp_path):
    """API 级身份创建/更新透传 is_admin。"""
    plugin = main_mod.VirtualSessionPlugin(FakeContext())
    plugin.identity_store = IdentityStore(data_dir=tmp_path)

    resp = await call_handler(plugin.create_identity, {"name": "小明"})
    assert resp.status_code == 200
    ident = json.loads(resp.body)
    assert ident["is_admin"] is False

    resp2 = await call_handler(
        plugin.create_identity, {"name": "管理员", "is_admin": True}
    )
    admin = json.loads(resp2.body)
    assert admin["is_admin"] is True

    resp3 = await call_handler(
        plugin.update_identity, {"name": "小刚", "is_admin": False}, admin["id"]
    )
    assert json.loads(resp3.body)["is_admin"] is False


@pytest.mark.asyncio
async def test_list_groups_security_warning(tmp_path):
    """组安全标记按有效配置实时计算，派生键不写回 store。"""
    context = FakeContext()
    context.astrbot_config_mgr.confs = {
        "default": {
            "provider_settings": {
                "computer_use_runtime": "none",
                "web_search": False,
                "proactive_capability": {"add_cron_tools": False},
            }
        },
        "conf_safe": {
            "provider_settings": {
                "computer_use_runtime": "none",
                "web_search": False,
                "proactive_capability": {"add_cron_tools": False},
            }
        },
        "conf_risky": {"provider_settings": {"computer_use_runtime": "local"}},
    }
    plugin = main_mod.VirtualSessionPlugin(context)
    plugin.group_mgr = VirtualGroupManager(data_dir=tmp_path)

    g_risky = plugin.group_mgr.create_group("危险组", count=1, conf_id="conf_risky")
    g_safe = plugin.group_mgr.create_group("安全组", count=1, conf_id="conf_safe")
    g_default = plugin.group_mgr.create_group("默认组", count=1)

    resp = await plugin.list_groups()
    body = json.loads(resp.body)
    by_id = {g["id"]: g for g in body["groups"]}
    assert by_id[g_risky["id"]]["security_warning"] is True
    assert by_id[g_safe["id"]]["security_warning"] is False
    assert by_id[g_default["id"]]["security_warning"] is False

    # 会话级 conf 覆盖为危险 → 组标记（会话优先于组配置）
    plugin.group_mgr.update_session(g_safe["sessions"][0]["id"], conf_id="conf_risky")
    resp2 = await plugin.list_groups()
    by_id2 = {g["id"]: g for g in json.loads(resp2.body)["groups"]}
    assert by_id2[g_safe["id"]]["security_warning"] is True

    # 绑定已删除的档案 → 回退默认配置判定（镜像 get_conf 运行时语义）
    g_ghost = plugin.group_mgr.create_group("幽灵组", count=1, conf_id="已删除档案")
    resp3 = await plugin.list_groups()
    by_id3 = {g["id"]: g for g in json.loads(resp3.body)["groups"]}
    assert by_id3[g_ghost["id"]]["security_warning"] is False  # 默认配置安全

    # 派生键不写回 store：list_groups 返回的组 dict 无 security_warning 键
    raw = plugin.group_mgr.list_groups()
    assert all("security_warning" not in g for g in raw)


@pytest.mark.asyncio
async def test_plugin_reviewer_crud(tmp_path):
    plugin = main_mod.VirtualSessionPlugin(FakeContext())
    plugin.reviewer_store = ReviewerStore(data_dir=tmp_path)

    payload = {
        "name": "质量评审",
        "provider_id": "prov_r",
        "model": "review-model",
        "system_prompt": "请评审 {{metrics}}",
        "metrics": [{"key": "score", "type": "number", "pass_threshold": 80}],
    }
    resp = await call_handler(plugin.create_reviewer, payload)
    assert resp.status_code == 200
    profile = json.loads(resp.body)
    assert profile["id"].startswith("rp_")
    assert profile["context"] == "reply"  # 缺省

    # 不配 model（只配 Provider）→ 200，model 落 None（评审用 Provider 当前模型）
    resp_nm = await call_handler(
        plugin.create_reviewer, {k: v for k, v in payload.items() if k != "model"}
    )
    assert resp_nm.status_code == 200
    assert json.loads(resp_nm.body)["model"] is None

    # 支持多个 profile：再创建 → 200（消息规则 / 最终断言按 profile_id 引用）
    resp2 = await call_handler(
        plugin.create_reviewer, {**payload, "name": "二审", "model": "review-2"}
    )
    assert resp2.status_code == 200

    # 部分更新
    resp3 = await call_handler(plugin.update_reviewer, {"name": "新名"}, profile["id"])
    assert resp3.status_code == 200
    body = json.loads(resp3.body)
    assert body["name"] == "新名" and body["model"] == "review-model"

    # 更新把契约改坏 → 400（合并后校验）
    resp4 = await call_handler(plugin.update_reviewer, {"metrics": []}, profile["id"])
    assert resp4.status_code == 400

    # 更新不存在的 profile → 404
    resp5 = await call_handler(plugin.update_reviewer, {"name": "x"}, "rp_none")
    assert resp5.status_code == 404

    # 创建契约不合法 → 400
    resp6 = await call_handler(
        plugin.create_reviewer, {"name": "缺字段", "metrics": []}
    )
    assert resp6.status_code == 400

    # 列表（三个 profile）+ 按需删除（部分删除保留其余）
    listing = await plugin.list_reviewers()
    reviewers = json.loads(listing.body)["reviewers"]
    assert len(reviewers) == 3
    resp7 = await call_handler(plugin.delete_reviewers, {"ids": [profile["id"]]})
    assert json.loads(resp7.body)["deleted"] == 1
    listing2 = await plugin.list_reviewers()
    assert len(json.loads(listing2.body)["reviewers"]) == 2
    resp8 = await call_handler(plugin.delete_reviewers, {"ids": []})
    assert resp8.status_code == 400


@pytest.mark.asyncio
async def test_reviewer_preview_metrics_endpoint():
    """POST /reviewers/preview 返回 {{metrics}} 展开内容，与运行时构造级一致。

    预览直接复用 `metrics_contract_description`（评审运行时的同款函数），断言
    用构造级相等而非手写期望串——表单预览与实际展开必须字节级一致，防前端
    镜像逻辑漂移。残缺行（无 key）过滤后仍 200（预览容忍半成品输入）。
    """
    plugin = main_mod.VirtualSessionPlugin(FakeContext())
    metrics = _valid_profile()["metrics"]
    resp = await call_handler(plugin.preview_reviewer_metrics, {"metrics": metrics})
    assert resp.status_code == 200
    body = json.loads(resp.body)
    assert body["description"] == rev_mod.metrics_contract_description(metrics)
    assert "score" in body["description"]

    # 残缺行（无 key）→ 过滤后正常展开，不因半成品输入 500
    resp2 = await call_handler(
        plugin.preview_reviewer_metrics, {"metrics": [{"type": "number"}]}
    )
    assert resp2.status_code == 200
    body2 = json.loads(resp2.body)
    assert body2["description"] == rev_mod.metrics_contract_description([])

    # 非列表 → 400
    resp3 = await call_handler(plugin.preview_reviewer_metrics, {"metrics": "x"})
    assert resp3.status_code == 400


@pytest.mark.asyncio
async def test_plugin_testset_final_rules_validation(tmp_path):
    plugin = main_mod.VirtualSessionPlugin(FakeContext())
    plugin.testset_store = TestsetStore(data_dir=tmp_path)
    base = {"name": "T", "messages": [{"text": "m1"}, {"text": "m2"}]}

    resp = await call_handler(
        plugin.create_testset,
        {
            **base,
            "final_rules": [
                {"rule": {"type": "contains", "value": "x"}, "scope": "all"},
                {
                    "rule": {"kind": "llm", "profile_id": "rp_1"},
                    "scope": {"from": 0, "to": 1},
                },
            ],
        },
    )
    assert resp.status_code == 200
    assert len(json.loads(resp.body)["final_rules"]) == 2

    for bad in [
        "不是列表",
        [{"rule": "不是字典"}],
        [{"rule": {}, "scope": "全部"}],
        [{"rule": {}, "scope": {"from": 0}}],
    ]:
        resp = await call_handler(plugin.create_testset, {**base, "final_rules": bad})
        assert resp.status_code == 400, bad


@pytest.mark.asyncio
async def test_plugin_testset_report_llm_validation(tmp_path):
    """测试集 API 的 report_llm：非法形状 → 400；合法配置落盘；更新生效。"""
    plugin = main_mod.VirtualSessionPlugin(FakeContext())
    plugin.testset_store = TestsetStore(data_dir=tmp_path)

    resp = await call_handler(
        plugin.create_testset, {"name": "T", "messages": [], "report_llm": "nope"}
    )
    assert resp.status_code == 400
    resp = await call_handler(
        plugin.create_testset,
        {"name": "T", "messages": [], "report_llm": {"system_prompt": "x"}},
    )
    assert resp.status_code == 400

    resp = await call_handler(
        plugin.create_testset,
        {"name": "T", "messages": [], "report_llm": {"provider_id": "prov_x"}},
    )
    assert resp.status_code == 200
    ts = json.loads(resp.body)
    assert ts["report_llm"] == {"provider_id": "prov_x", "system_prompt": ""}

    # 更新：非法 → 400；合法生效
    resp = await call_handler(
        plugin.update_testset,
        {"name": "x", "messages": [], "report_llm": []},
        ts["id"],
    )
    assert resp.status_code == 400
    resp = await call_handler(
        plugin.update_testset,
        {"name": "x", "messages": [], "report_llm": {"provider_id": "p2"}},
        ts["id"],
    )
    assert resp.status_code == 200
    assert json.loads(resp.body)["report_llm"] == {
        "provider_id": "p2",
        "system_prompt": "",
    }


@pytest.mark.asyncio
async def test_plugin_generate_llm_report(tmp_path):
    """LLM 报告生成端点：成功产物落 data.llm_report 并持久化（重新生成覆盖）；
    未配置报告 LLM / Provider 缺失 → 400；报告不存在 → 404；调用异常 → 400。"""
    provider = FakeLLMProvider("prov_r", responses=["# 报告\n\n**总结**", "第二版"])
    plugin = main_mod.VirtualSessionPlugin(FakeContext(providers=[provider]))
    plugin.testset_store = TestsetStore(data_dir=tmp_path)
    plugin.report_store = ReportStore(data_dir=tmp_path)
    ts = plugin.testset_store.create_testset(
        "带报告 LLM",
        [{"text": "问"}],
        report_llm={"provider_id": "prov_r", "system_prompt": "生成报告"},
    )
    report = plugin.report_store.add_report(
        ts["id"], "tr_1", {"status": "done", "testset_id": ts["id"]}
    )

    resp = await call_handler(plugin.generate_llm_report, {}, report["id"])
    assert resp.status_code == 200
    body = json.loads(resp.body)
    llm = body["llm_report"]
    assert llm["status"] == "ok"
    assert llm["text"] == "# 报告\n\n**总结**"
    assert llm["provider_id"] == "prov_r"
    assert llm["model"] is None  # 未配模型 → Provider 当前模型
    assert isinstance(llm["generated_at"], int)
    # 报告数据整体作为 prompt 传给报告 LLM（含 testset_id，JSON 可读）
    assert len(provider.calls) == 1
    assert provider.calls[0]["system_prompt"] == "生成报告"
    assert provider.calls[0]["model"] is None
    assert json.loads(provider.calls[0]["prompt"])["testset_id"] == ts["id"]
    # 持久化：重开 store 仍可读（update_report 整体替换 data）
    assert (
        plugin.report_store.get_report(report["id"])["data"]["llm_report"] is not None
    )

    # 重新生成覆盖旧产物（第二次调用）
    resp2 = await call_handler(plugin.generate_llm_report, {}, report["id"])
    assert json.loads(resp2.body)["llm_report"]["text"] == "第二版"
    assert len(provider.calls) == 2

    # 测试集未配置报告 LLM → 400（报告存在但无配置）
    ts_no = plugin.testset_store.create_testset("无配置", [{"text": "问"}])
    report_no = plugin.report_store.add_report(
        ts_no["id"], "tr_2", {"status": "done", "testset_id": ts_no["id"]}
    )
    resp3 = await call_handler(plugin.generate_llm_report, {}, report_no["id"])
    assert resp3.status_code == 400
    assert "未配置报告 LLM" in resp3.body.decode("utf-8")

    # 配置的 Provider 缺失 → 400
    ts_missing = plugin.testset_store.create_testset(
        "缺 Provider",
        [{"text": "问"}],
        report_llm={"provider_id": "prov_gone", "system_prompt": ""},
    )
    report_missing = plugin.report_store.add_report(
        ts_missing["id"], "tr_3", {"status": "done", "testset_id": ts_missing["id"]}
    )
    resp4 = await call_handler(plugin.generate_llm_report, {}, report_missing["id"])
    assert resp4.status_code == 400
    assert "找不到报告 Provider" in resp4.body.decode("utf-8")

    # 报告不存在 → 404；数据损坏（非 dict）→ 400
    resp5 = await call_handler(plugin.generate_llm_report, {}, "rp_none")
    assert resp5.status_code == 404
    report_bad = plugin.report_store.add_report(ts["id"], "tr_4", "不是 dict")
    resp6 = await call_handler(plugin.generate_llm_report, {}, report_bad["id"])
    assert resp6.status_code == 400
    assert "报告数据损坏" in resp6.body.decode("utf-8")

    # 报告 LLM 调用异常 → 400（不落库）
    boom = FakeLLMProvider("prov_r", raise_on_call=True)
    plugin2 = main_mod.VirtualSessionPlugin(FakeContext(providers=[boom]))
    plugin2.testset_store = TestsetStore(data_dir=tmp_path)
    plugin2.report_store = ReportStore(data_dir=tmp_path)
    ts_boom = plugin2.testset_store.create_testset(
        "异常",
        [{"text": "问"}],
        report_llm={"provider_id": "prov_r", "system_prompt": ""},
    )
    report_boom = plugin2.report_store.add_report(
        ts_boom["id"], "tr_5", {"status": "done", "testset_id": ts_boom["id"]}
    )
    resp7 = await call_handler(plugin2.generate_llm_report, {}, report_boom["id"])
    assert resp7.status_code == 400
    assert "报告生成失败" in resp7.body.decode("utf-8")
    # 失败不落库：报告保持原样（无 llm_report）
    assert (
        "llm_report" not in plugin2.report_store.get_report(report_boom["id"])["data"]
    )


@pytest.mark.asyncio
async def test_plugin_testset_report_enabled_validation(tmp_path):
    """测试集 API 的 report_enabled：非布尔 → 400；缺省 False；显式 True 落盘。"""
    plugin = main_mod.VirtualSessionPlugin(FakeContext())
    plugin.testset_store = TestsetStore(data_dir=tmp_path)

    resp = await call_handler(
        plugin.create_testset,
        {"name": "T", "messages": [], "report_enabled": "yes"},
    )
    assert resp.status_code == 400

    resp = await call_handler(plugin.create_testset, {"name": "T", "messages": []})
    assert resp.status_code == 200
    ts_id = json.loads(resp.body)["id"]
    assert json.loads(resp.body)["report_enabled"] is False

    resp = await call_handler(
        plugin.create_testset, {"name": "T2", "messages": [], "report_enabled": True}
    )
    assert json.loads(resp.body)["report_enabled"] is True

    # 更新：非布尔 → 400；显式 True 生效
    resp = await call_handler(
        plugin.update_testset,
        {"name": "x", "messages": [], "report_enabled": 1},
        ts_id,
    )
    assert resp.status_code == 400
    resp = await call_handler(
        plugin.update_testset,
        {"name": "x", "messages": [], "report_enabled": True},
        ts_id,
    )
    assert resp.status_code == 200
    assert json.loads(resp.body)["report_enabled"] is True


@pytest.mark.asyncio
async def test_plugin_report_api(tmp_path):
    """报告接口：按测试集列出 + 按 id 删除（空 ids → 400）。"""
    plugin = main_mod.VirtualSessionPlugin(FakeContext())
    plugin.report_store = ReportStore(data_dir=tmp_path)
    r1 = plugin.report_store.add_report(
        "ts_1", "tr_1", {"status": "done", "testset_name": "A"}
    )
    plugin.report_store.add_report(
        "ts_2", "tr_2", {"status": "done", "testset_name": "B"}
    )

    resp = await call_handler(plugin.list_reports, {}, "ts_1")
    assert resp.status_code == 200
    reports = json.loads(resp.body)["reports"]
    assert [r["id"] for r in reports] == [r1["id"]]
    assert reports[0]["data"]["testset_name"] == "A"

    resp = await call_handler(plugin.delete_reports, {"ids": [r1["id"]]})
    assert json.loads(resp.body)["deleted"] == 1
    assert plugin.report_store.get_report(r1["id"]) is None

    resp = await call_handler(plugin.delete_reports, {"ids": []})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_plugin_delete_testset_cascades_reports(tmp_path):
    """删除测试集级联删除其产出的全部报告（其他测试集的报告保留）。"""
    plugin = main_mod.VirtualSessionPlugin(FakeContext())
    plugin.testset_store = TestsetStore(data_dir=tmp_path)
    plugin.report_store = ReportStore(data_dir=tmp_path)
    ts = plugin.testset_store.create_testset("带报告", [{"text": "m"}])
    plugin.report_store.add_report(ts["id"], "tr_1", {"status": "done"})
    plugin.report_store.add_report(ts["id"], "tr_2", {"status": "done"})
    plugin.report_store.add_report("ts_other", "tr_3", {"status": "done"})

    resp = await call_handler(plugin.delete_testsets, {"ids": [ts["id"]]})
    body = json.loads(resp.body)
    assert body["deleted"] == 1
    assert body["reports_deleted"] == 2
    assert plugin.report_store.list_reports(testset_id=ts["id"]) == []
    assert len(plugin.report_store.list_reports()) == 1  # 其他测试集的报告保留


@pytest.mark.asyncio
async def test_plugin_retry_report_reviews_failed_scope(tmp_path):
    """scope=failed：只重跑 error/invalid 的 LLM 评审，机械 verdict 不动，
    重试后聚合刷新并持久化。"""
    # failed 范围重跑 2 条失败 verdict（消息级 error + 跨轮级 invalid）
    provider = FakeLLMProvider("prov_r", responses=['{"score": 88}'] * 2)
    plugin = main_mod.VirtualSessionPlugin(FakeContext(providers=[provider]))
    plugin.reviewer_store = ReviewerStore(data_dir=tmp_path)
    plugin.report_store = ReportStore(data_dir=tmp_path)
    profile = plugin.reviewer_store.create_profile(
        {
            "name": "评审",
            "provider_id": "prov_r",
            "model": "review-model",
            "system_prompt": "评审 {{metrics}}",
            "metrics": [{"key": "score", "type": "number", "pass_threshold": 80}],
        }
    )
    report = _report_with_llm_verdicts(plugin.report_store, profile)

    resp = await call_handler(
        plugin.retry_report_reviews, {"scope": "failed"}, report["id"]
    )
    assert resp.status_code == 200
    body = json.loads(resp.body)
    assert body["updated"] == 2  # 消息级失败 + 跨轮级 invalid 被重跑
    assert body["failed"] == 0
    assert body["errors"] == []
    step_verdicts = body["report"]["steps"][0]["results"][0]["verdicts"]
    assert step_verdicts[0]["status"] == "ok"  # 机械 verdict 未被重跑
    assert step_verdicts[1]["status"] == "ok" and step_verdicts[1]["pass"] is True
    assert step_verdicts[1]["metrics"] == [
        {"key": "score", "type": "number", "value": 88}
    ]
    # 已通过的 LLM verdict 未被重跑（calls 只有 2 次：两个失败的 verdict）
    assert len(provider.calls) == 2
    final_verdict = body["report"]["final_verdicts"][0]["results"][0]["verdict"]
    assert final_verdict["status"] == "ok"
    # 聚合刷新：score 平均 = (88 + 90 + 88) / 3，评审失败清零
    summary = body["report"]["metrics_summary"]
    assert summary["metrics"]["score"]["avg"] == pytest.approx(88.6667, abs=0.001)
    assert summary["review_failures"] == 0
    # 持久化：重读报告数据与响应一致
    persisted = plugin.report_store.get_report(report["id"])["data"]
    assert persisted["metrics_summary"]["review_failures"] == 0
    assert persisted["steps"][0]["results"][0]["verdicts"][1]["pass"] is True


@pytest.mark.asyncio
async def test_plugin_retry_report_reviews_all_and_targets(tmp_path):
    """scope=all 重跑全部 LLM 评审（含已通过）；targets 单条定位到具体 verdict。"""
    # all 重跑 3 条 + 单条重试 1 次 = 4 次调用
    provider = FakeLLMProvider("prov_r", responses=['{"score": 88}'] * 4)
    plugin = main_mod.VirtualSessionPlugin(FakeContext(providers=[provider]))
    plugin.reviewer_store = ReviewerStore(data_dir=tmp_path)
    plugin.report_store = ReportStore(data_dir=tmp_path)
    profile = plugin.reviewer_store.create_profile(
        {
            "name": "评审",
            "provider_id": "prov_r",
            "model": "review-model",
            "system_prompt": "评审 {{metrics}}",
            "metrics": [{"key": "score", "type": "number", "pass_threshold": 80}],
        }
    )
    report = _report_with_llm_verdicts(plugin.report_store, profile)

    resp = await call_handler(
        plugin.retry_report_reviews, {"scope": "all"}, report["id"]
    )
    body = json.loads(resp.body)
    # 3 条 LLM verdict（消息级失败 + 通过 + 跨轮级 invalid）全部重跑
    assert body["updated"] == 3
    assert len(provider.calls) == 3
    # 机械 verdict 仍不参与（无 profile_id）
    assert body["report"]["steps"][0]["results"][0]["verdicts"][0]["status"] == "ok"
    # 断言 / 耗时聚合随评审重试一并重建（与 metrics_summary 同批 update_report）
    # 4 条 verdict 全部重试为 pass=True（机械 + 3 条 LLM 均通过）
    assert body["report"]["assertions"] == {"total": 4, "passed": 4, "failed": 0}
    assert body["report"]["durations"]["count"] == 0

    # 单条重试：targets 定位消息级第 2 条 LLM verdict
    resp2 = await call_handler(
        plugin.retry_report_reviews,
        {"targets": [{"kind": "m", "step": 0, "session_id": "vs_1", "verdict": 2}]},
        report["id"],
    )
    body2 = json.loads(resp2.body)
    assert body2["updated"] == 1
    assert len(provider.calls) == 4

    # 无效 targets 形状 → 400；scope/targets 都缺 → 400
    resp3 = await call_handler(
        plugin.retry_report_reviews, {"scope": "nope"}, report["id"]
    )
    assert resp3.status_code == 400
    resp4 = await call_handler(plugin.retry_report_reviews, {}, report["id"])
    assert resp4.status_code == 400

    # 报告不存在 → 404
    resp5 = await call_handler(
        plugin.retry_report_reviews, {"scope": "failed"}, "rp_none"
    )
    assert resp5.status_code == 404


@pytest.mark.asyncio
async def test_plugin_retry_report_reviews_profile_missing(tmp_path):
    """profile 已删除的 verdict 无法重试：计入 failed，其余照常重跑。"""
    provider = FakeLLMProvider("prov_r", responses=['{"score": 88}'])
    plugin = main_mod.VirtualSessionPlugin(FakeContext(providers=[provider]))
    plugin.reviewer_store = ReviewerStore(data_dir=tmp_path)
    plugin.report_store = ReportStore(data_dir=tmp_path)
    profile = plugin.reviewer_store.create_profile(
        {
            "name": "评审",
            "provider_id": "prov_r",
            "model": "review-model",
            "system_prompt": "评审 {{metrics}}",
            "metrics": [{"key": "score", "type": "number", "pass_threshold": 80}],
        }
    )
    report = _report_with_llm_verdicts(plugin.report_store, profile)
    # 删除 profile：全部 LLM verdict 失去可解析的 profile
    plugin.reviewer_store.delete_profiles([profile["id"]])

    resp = await call_handler(
        plugin.retry_report_reviews, {"scope": "failed"}, report["id"]
    )
    body = json.loads(resp.body)
    assert body["updated"] == 0
    assert body["failed"] == 2
    assert len(body["errors"]) == 2
    assert "找不到评审 profile" in body["errors"][0]["error"]
    # 未重试任何评审 LLM
    assert provider.calls == []


@pytest.mark.asyncio
async def test_plugin_testset_runs_filter_by_testset(tmp_path):
    """testset_runs 支持按 testset_id 过滤（报告视图顶部按测试集列最近运行）。"""
    queue = asyncio.Queue()
    plugin = main_mod.VirtualSessionPlugin(FakeContext(queue))
    plugin.group_mgr = VirtualGroupManager(data_dir=tmp_path)
    plugin.testset_store = TestsetStore(data_dir=tmp_path)
    group = plugin.group_mgr.create_group("组A", count=1)
    sid = group["sessions"][0]["id"]
    ts1 = plugin.testset_store.create_testset("T1", [{"text": "m1"}])
    ts2 = plugin.testset_store.create_testset("T2", [{"text": "m1"}])

    run1 = plugin.testset_runner.start_run(ts1, plugin.group_mgr.effective_many([sid]))
    run2 = plugin.testset_runner.start_run(ts2, plugin.group_mgr.effective_many([sid]))

    req = make_plugin_request({}, query=f"testset_id={ts1['id']}")
    with bind_request_context(req):
        resp = await plugin.testset_runs()
    runs = json.loads(resp.body)["runs"]
    assert any(r["run_id"] == run1 for r in runs)
    assert not any(r["run_id"] == run2 for r in runs)

    # 收尾：放行悬挂的 _await_event
    while not queue.empty():
        queue.get_nowait().cleanup_temporary_local_files()
    await asyncio.sleep(0.01)


def test_cron_target_sets():
    """target_sets 解析会话 umo 与 id 集合；非法条目（缺 id）跳过。"""
    umos, ids = target_sets([make_session(1), make_session(2)])
    assert umos == {
        "webchat:FriendMessage:vs_1",
        "webchat:FriendMessage:vs_2",
    }
    assert ids == {"vs_1", "vs_2"}
    umos2, ids2 = target_sets([{}, {"id": "vs_3"}])
    assert umos2 == {"webchat:FriendMessage:vs_3"}
    assert ids2 == {"vs_3"}


def test_cron_job_warning_active_agent_match():
    """active_agent 任务 payload.session 精确命中虚拟 umo → 警告项。"""
    umos, ids = target_sets([make_session(1)])
    job = _cron_job("j1", "active_agent", {"session": "webchat:FriendMessage:vs_1"})
    w = cron_job_warning(vars(job), umos, ids)
    assert w is not None
    assert w["kind"] == "cron_targets_virtual_session"
    assert w["job_id"] == "j1"
    assert w["session"] == "webchat:FriendMessage:vs_1"
    assert "定时任务" in w["message"]


def test_cron_job_warning_active_agent_no_match():
    """active_agent 投递目标是真实会话 → 无警告。"""
    umos, ids = target_sets([make_session(1)])
    job = _cron_job("j1", "active_agent", {"session": "webchat:FriendMessage:u1"})
    assert cron_job_warning(vars(job), umos, ids) is None


def test_cron_job_warning_disabled():
    """enabled=False 的任务即使命中也不警告（未生效的任务不会发消息）。"""
    umos, ids = target_sets([make_session(1)])
    job = _cron_job(
        "j1", "active_agent", {"session": "webchat:FriendMessage:vs_1"}, enabled=False
    )
    assert cron_job_warning(vars(job), umos, ids) is None


def test_cron_job_warning_basic_payload_hit():
    """basic 任务 payload 浅层扫描命中虚拟会话 id（含嵌套）→ 启发式警告。"""
    umos, ids = target_sets([make_session(1)])
    job = _cron_job("j1", "basic", {"nested": {"target": "vs_1"}})
    w = cron_job_warning(vars(job), umos, ids)
    assert w is not None
    assert w["kind"] == "cron_may_target_virtual_session"
    assert w["session"] == "vs_1"


def test_cron_job_warning_basic_payload_miss():
    """basic 任务 payload 不含虚拟会话标识 → 无警告；非 dict payload 同样。"""
    umos, ids = target_sets([make_session(1)])
    assert (
        cron_job_warning(vars(_cron_job("j1", "basic", {"text": "问候"})), umos, ids)
        is None
    )
    assert cron_job_warning(vars(_cron_job("j1", "basic", None)), umos, ids) is None


@pytest.mark.asyncio
async def test_collect_cron_warnings():
    """collect_cron_warnings 枚举任务并补入活值 next_run_time。"""
    umos, ids = target_sets([make_session(1)])
    mgr = FakeCronManager(
        [
            _cron_job("j1", "active_agent", {"session": "webchat:FriendMessage:vs_1"}),
            _cron_job("j2", "basic", {"text": "hello"}),
        ],
        next_run=datetime(2026, 1, 1, 8, 0, 0),
    )
    warnings = await collect_cron_warnings(mgr, umos, ids)
    assert len(warnings) == 1
    assert warnings[0]["job_id"] == "j1"
    assert warnings[0]["next_run_time"] == "2026-01-01T08:00:00"


@pytest.mark.asyncio
async def test_collect_cron_warnings_degrade():
    """cron_manager 未初始化 / list_jobs 失败 → 降级为无警告；scheduler 未启动
    （next_run 取不到）→ 警告保留、next_run_time 为空。"""
    umos, ids = target_sets([make_session(1)])
    assert await collect_cron_warnings(None, umos, ids) == []

    class Boom:
        async def list_jobs(self):
            raise RuntimeError("boom")

    assert await collect_cron_warnings(Boom(), umos, ids) == []

    class NoNext:
        async def list_jobs(self):
            return [
                _cron_job(
                    "j1", "active_agent", {"session": "webchat:FriendMessage:vs_1"}
                )
            ]

        def get_next_run_time(self, job_id):
            raise RuntimeError("scheduler not running")

    warnings = await collect_cron_warnings(NoNext(), umos, ids)
    assert len(warnings) == 1
    assert warnings[0]["next_run_time"] is None


@pytest.mark.asyncio
async def test_plugin_run_test_attaches_cron_warnings(tmp_path):
    """手动群发入口：启动前探测 cron 任务，警告随运行记录呈现。"""
    queue = asyncio.Queue()
    context = FakeContext(queue)
    plugin = main_mod.VirtualSessionPlugin(context)
    plugin.group_mgr = VirtualGroupManager(data_dir=tmp_path)
    group = plugin.group_mgr.create_group("组A", count=1)
    sid = group["sessions"][0]["id"]
    umo = umo_of(plugin.group_mgr.effective_many([sid])[0])
    context.cron_manager = FakeCronManager(
        [_cron_job("j1", "active_agent", {"session": umo})]
    )

    resp = await call_handler(plugin.run_test, {"sessions": [sid], "text": "hi"})
    body = json.loads(resp.body)
    assert resp.status_code == 200
    warnings = plugin.runner.status(body["test_id"])["warnings"]
    assert len(warnings) == 1
    assert warnings[0]["job_id"] == "j1"

    # 收尾：放行悬挂的 _await_event
    while not queue.empty():
        queue.get_nowait().cleanup_temporary_local_files()
    await asyncio.sleep(0.01)
