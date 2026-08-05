"""后端单元测试：虚拟会话测试平台插件。

测试代码随插件仓库维护。导入插件模块需要安装 astrbot（PyPI 包，插件本身的
运行时依赖）；未安装时整组跳过（见 importorskip）。
"""

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

import astrbot_plugin_testbench.group_store as gs_mod  # noqa: E402
import astrbot_plugin_testbench.main as main_mod  # noqa: E402
import astrbot_plugin_testbench.runner as runner_mod  # noqa: E402
import astrbot_plugin_testbench.stats as stats_mod  # noqa: E402
import astrbot_plugin_testbench.virtual_event as ve_mod  # noqa: E402
from astrbot.api.event import MessageChain  # noqa: E402
from astrbot.api.web import PluginRequest, bind_request_context  # noqa: E402
from starlette.requests import Request  # noqa: E402

VirtualMessageEvent = ve_mod.VirtualMessageEvent
VirtualGroupManager = gs_mod.VirtualGroupManager
VirtualTestRunner = runner_mod.VirtualTestRunner
duration_stats = stats_mod.duration_stats
umo_of = gs_mod.umo_of


def make_session(i: int, platform_id: str = "virtual_test") -> dict:
    """构造一个已解析最终配置的会话（供运行器测试直接使用）。"""
    return {
        "id": f"vs_{i}",
        "name": f"虚拟会话{i}",
        "platform_id": platform_id,
        "sender_id": "virtual_user",
        "sender_name": "虚拟用户",
        "created_at": 0,
    }


# ---------- 虚拟事件 ----------


def test_create_event_fields():
    ev = VirtualMessageEvent.create(
        session_id="vs_1",
        sender_id="u1",
        sender_name="用户1",
        text="你好",
        provider_id="prov_a",
        model="model-b",
    )
    assert ev.unified_msg_origin == "virtual_test:FriendMessage:vs_1"
    assert ev.message_str == "你好"
    assert ev.get_sender_id() == "u1"
    assert ev.get_sender_name() == "用户1"
    assert ev.get_extra("selected_provider") == "prov_a"
    assert ev.get_extra("selected_model") == "model-b"
    assert ev.get_message_type().value == "FriendMessage"


@pytest.mark.asyncio
async def test_send_captures_and_marks_done():
    ev = VirtualMessageEvent.create(
        session_id="vs_1", sender_id="u1", sender_name="用户1", text="hi"
    )
    await ev.send(MessageChain().message("你好，机器人"))
    assert len(ev.captured) == 1
    assert ev.done_event.is_set()
    assert ev.finished_at is not None
    summary = ev.result_summary()
    assert summary["status"] == "ok"
    assert summary["reply"] == "你好，机器人"
    assert summary["umo"] == "virtual_test:FriendMessage:vs_1"


@pytest.mark.asyncio
async def test_send_streaming_accumulates():
    async def gen():
        yield MessageChain().message("第一段")
        yield MessageChain().message("第二段")

    ev = VirtualMessageEvent.create(
        session_id="vs_1", sender_id="u1", sender_name="用户1", text="hi"
    )
    await ev.send_streaming(gen())
    assert ev.done_event.is_set()
    assert ev.result_summary()["reply"] == "第一段第二段"


@pytest.mark.asyncio
async def test_send_streaming_reasoning_separated():
    async def gen():
        reasoning = MessageChain().message("逐步思考")
        reasoning.type = "reasoning"
        yield reasoning
        yield MessageChain().message("最终答案")

    ev = VirtualMessageEvent.create(
        session_id="vs_1", sender_id="u1", sender_name="用户1", text="hi"
    )
    await ev.send_streaming(gen())
    summary = ev.result_summary()
    assert summary["reasoning"] == "逐步思考"
    assert summary["reply"] == "最终答案"


@pytest.mark.asyncio
async def test_pipeline_done_signal():
    ev = VirtualMessageEvent.create(
        session_id="vs_1", sender_id="u1", sender_name="用户1", text="hi"
    )
    assert not ev.pipeline_done_event.is_set()
    # cleanup_temporary_local_files 是 PipelineScheduler.execute finally 的唯一调用点
    ev.cleanup_temporary_local_files()
    assert ev.pipeline_done_event.is_set()
    assert ev.result_summary()["status"] == "no_reply"


# ---------- umo ----------


def test_umo_of():
    assert umo_of(make_session(1)) == "virtual_test:FriendMessage:vs_1"
    assert (
        umo_of({"id": "vs_1", "platform_id": "aiocqhttp"})
        == "aiocqhttp:FriendMessage:vs_1"
    )


# ---------- 测试组管理 ----------


def test_group_manager_create_persist(tmp_path):
    mgr = VirtualGroupManager(data_dir=tmp_path)
    group = mgr.create_group(
        "组A", count=3, platform_id="virtual_test", name_prefix="测试"
    )
    assert len(group["sessions"]) == 3
    assert [s["name"] for s in group["sessions"]] == ["测试1", "测试2", "测试3"]

    # 重新加载（新实例）确认数据已持久化
    mgr2 = VirtualGroupManager(data_dir=tmp_path)
    assert len(mgr2.list_groups()) == 1
    g = mgr2.get_group(group["id"])
    assert g["name"] == "组A"
    assert len(g["sessions"]) == 3

    # 组内新增会话，编号接续
    added = mgr2.add_sessions(group["id"], 2, name_prefix="测试")
    assert [s["name"] for s in added] == ["测试4", "测试5"]
    assert len(mgr2.get_group(group["id"])["sessions"]) == 5

    # 删除单个会话
    sid = added[0]["id"]
    removed = mgr2.delete_sessions([sid])
    assert len(removed) == 1
    assert mgr2.find_session(sid) is None

    # 删除整个组
    deleted = mgr2.delete_groups([group["id"]])
    assert len(deleted) == 4
    assert mgr2.list_groups() == []
    assert (tmp_path / "virtual_session" / "groups.json").exists()


def test_group_create_stores_conf(tmp_path):
    mgr = VirtualGroupManager(data_dir=tmp_path)
    group = mgr.create_group("组A", count=2, conf_id="conf_a")
    assert group["conf_id"] == "conf_a"
    # 会话默认继承组配置（覆盖字段为 None）
    assert all(s["conf_id"] is None for s in group["sessions"])


def test_effective_resolution(tmp_path):
    mgr = VirtualGroupManager(data_dir=tmp_path)
    group = mgr.create_group(
        "组A",
        count=1,
        platform_id="aiocqhttp",
        conf_id="conf_a",
        sender_id="group_sender",
        sender_name="组发送者",
    )
    session = group["sessions"][0]
    eff = mgr.effective(group, session)
    assert eff["platform_id"] == "aiocqhttp"
    assert eff["conf_id"] == "conf_a"
    assert eff["sender_id"] == "group_sender"
    assert eff["sender_name"] == "组发送者"

    # 会话覆盖单个字段，其余继承组配置
    mgr.update_session(
        session["id"], platform_id="telegram", conf_id="conf_b", sender_id="me"
    )
    eff2 = mgr.effective(group, session)
    assert eff2["platform_id"] == "telegram"
    assert eff2["conf_id"] == "conf_b"
    assert eff2["sender_id"] == "me"
    assert eff2["sender_name"] == "组发送者"

    # 传 None 恢复继承组配置
    mgr.update_session(session["id"], platform_id=None, conf_id=None)
    eff3 = mgr.effective(group, session)
    assert eff3["platform_id"] == "aiocqhttp"
    assert eff3["conf_id"] == "conf_a"

    # conf_id 空串 = 显式使用默认配置档案（不绑定）
    mgr.update_session(session["id"], conf_id="")
    assert mgr.effective(group, session)["conf_id"] is None


def test_effective_many_order_and_skip(tmp_path):
    mgr = VirtualGroupManager(data_dir=tmp_path)
    group = mgr.create_group("组A", count=3)
    ids = [s["id"] for s in group["sessions"]]
    resolved = mgr.effective_many([ids[2], ids[0], "vs_none"])
    assert [r["id"] for r in resolved] == [ids[2], ids[0]]


def test_update_session_not_found(tmp_path):
    mgr = VirtualGroupManager(data_dir=tmp_path)
    assert mgr.update_session("vs_none", conf_id="x") is None


def test_add_sessions_unknown_group(tmp_path):
    mgr = VirtualGroupManager(data_dir=tmp_path)
    with pytest.raises(KeyError):
        mgr.add_sessions("g_none", 1)


def test_group_migration_legacy(tmp_path):
    sess_dir = tmp_path / "virtual_session"
    sess_dir.mkdir(parents=True)
    (sess_dir / "sessions.json").write_text(
        json.dumps(
            [
                {
                    "id": "vs_1",
                    "name": "旧会话",
                    "platform_id": "aiocqhttp",
                    "conf_id": "conf_a",
                    "created_at": 0,
                }
            ]
        ),
        encoding="utf-8",
    )
    mgr = VirtualGroupManager(data_dir=tmp_path)
    assert len(mgr.list_groups()) == 1
    group = mgr.list_groups()[0]
    assert group["name"] == "默认测试组"
    assert len(group["sessions"]) == 1
    eff = mgr.effective(group, group["sessions"][0])
    assert eff["platform_id"] == "aiocqhttp"
    assert eff["conf_id"] == "conf_a"
    assert (tmp_path / "virtual_session" / "groups.json").exists()


# ---------- 运行器 ----------


class FakeUCR:
    """模拟 UmopConfigRouter：维护 umo -> conf_id 的精确路由表。"""

    def __init__(self) -> None:
        self.umop_to_conf_id: dict[str, str] = {}

    async def update_route(self, umo: str, conf_id: str) -> None:
        self.umop_to_conf_id[umo] = conf_id

    async def delete_route(self, umo: str) -> None:
        self.umop_to_conf_id.pop(umo, None)


class FakeConvManager:
    """模拟 ConversationManager：按 umo 存取对话历史。"""

    def __init__(self) -> None:
        self._convs: dict[str, list[object]] = {}

    def add_history(self, umo: str, title: str, history: list[dict]) -> None:
        conv = SimpleNamespace(
            cid=f"cid_{len(self._convs.get(umo, []))}",
            title=title,
            history=json.dumps(history, ensure_ascii=False),
        )
        self._convs.setdefault(umo, []).append(conv)

    async def get_conversations(self, unified_msg_origin: str) -> list[object]:
        return list(self._convs.get(unified_msg_origin, []))

    async def delete_conversations_by_user_id(self, unified_msg_origin: str) -> int:
        removed = len(self._convs.get(unified_msg_origin, []))
        self._convs.pop(unified_msg_origin, None)
        return removed

    async def get_curr_conversation_id(self, unified_msg_origin: str) -> str | None:
        convs = self._convs.get(unified_msg_origin, [])
        return convs[-1].cid if convs else None

    async def get_conversation(
        self, unified_msg_origin: str, conversation_id: str
    ) -> object | None:
        for conv in self._convs.get(unified_msg_origin, []):
            if conv.cid == conversation_id:
                return conv
        return None

    async def update_conversation(
        self,
        unified_msg_origin: str,
        conversation_id: str,
        history: list[dict] | None = None,
        **kwargs,
    ) -> None:
        for conv in self._convs.get(unified_msg_origin, []):
            if conv.cid == conversation_id:
                if history is not None:
                    conv.history = json.dumps(history, ensure_ascii=False)
                return


class FakePlatformManager:
    """模拟 PlatformManager：暴露 platform_insts 列表（适配器实例）。"""

    def __init__(self, insts: list[object] | None = None) -> None:
        self.platform_insts = insts or []


class FakePlatformInst:
    """模拟平台适配器实例：meta() 返回可配置的元数据或抛异常。"""

    def __init__(
        self,
        platform_id: str,
        name: str | None = None,
        adapter_display_name: str | None = None,
        raise_on_meta: bool = False,
    ) -> None:
        self._id = platform_id
        self._name = name if name is not None else platform_id
        self._adapter_display_name = adapter_display_name
        self._raise = raise_on_meta

    def meta(self):
        if self._raise:
            raise RuntimeError("broken adapter")
        return SimpleNamespace(
            id=self._id,
            name=self._name,
            adapter_display_name=self._adapter_display_name,
        )


class FakeContext:
    def __init__(
        self,
        queue: asyncio.Queue | None = None,
        ucr: FakeUCR | None = None,
        conv_mgr: FakeConvManager | None = None,
        platform_mgr: FakePlatformManager | None = None,
    ) -> None:
        self._queue = queue or asyncio.Queue()
        self.astrbot_config_mgr = SimpleNamespace(ucr=ucr or FakeUCR())
        self.conversation_manager = conv_mgr or FakeConvManager()
        self.platform_manager = platform_mgr

    def get_event_queue(self) -> asyncio.Queue:
        return self._queue

    def register_web_api(self, *args, **kwargs) -> None:
        """插件注册 Web API 时静默忽略（测试不需要真实注册）。"""


def make_plugin_request(body: dict = None, query: str = "") -> PluginRequest:
    """构造一个带 JSON body 的 PluginRequest（需要与 handler 在同一异步上下文）。"""

    async def receive() -> dict:
        return {
            "type": "http.request",
            "body": json.dumps(body or {}).encode("utf-8"),
            "more_body": False,
        }

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/x",
        "raw_path": b"/x",
        "query_string": query.encode("utf-8"),
        "root_path": "",
        "headers": [(b"content-type", b"application/json")],
        "client": ("127.0.0.1", 1234),
        "server": ("testserver", 80),
    }
    return PluginRequest(Request(scope, receive))


async def call_handler(handler, body: dict, *args):
    """把请求绑定到当前异步上下文后调用 handler。"""
    req = make_plugin_request(body)
    with bind_request_context(req):
        return await handler(*args)


async def consume(queue: asyncio.Queue, handler) -> None:
    while True:
        event = await queue.get()
        await handler(event)


async def wait_run_done(runner, test_id: str, timeout: float = 5.0) -> dict:
    """轮询 status 直到 done（模拟前端轮询）。"""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        rec = runner.status(test_id)
        if rec and rec["done"]:
            return rec
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("测试运行未在限时内完成")
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_runner_start_and_status_ok():
    queue = asyncio.Queue()
    runner = VirtualTestRunner(FakeContext(queue))

    async def handler(event):
        await asyncio.sleep(0.01)  # 模拟耗时处理，保证事件被并发消费
        await event.send(MessageChain().message("ok"))
        # 模拟 pipeline 结束：PipelineScheduler.execute 的 finally 会调用此方法
        event.cleanup_temporary_local_files()

    task = asyncio.create_task(consume(queue, handler))
    try:
        test_id = await runner.start(
            sessions=[make_session(1), make_session(2), make_session(3)],
            text="测试消息",
        )
        # start 立即返回，结果异步累积
        rec0 = runner.status(test_id)
        assert rec0["total"] == 3
        assert rec0["done"] is False
        rec = await wait_run_done(runner, test_id)
    finally:
        task.cancel()
    assert rec["done"] is True
    assert len(rec["results"]) == 3
    assert all(r["status"] == "ok" for r in rec["results"])
    assert all(r["reply"] == "ok" for r in rec["results"])


@pytest.mark.asyncio
async def test_runner_no_reply():
    queue = asyncio.Queue()
    runner = VirtualTestRunner(FakeContext(queue))

    async def handler(event):
        # pipeline 结束但未产生回复
        event.cleanup_temporary_local_files()

    task = asyncio.create_task(consume(queue, handler))
    try:
        test_id = await runner.start(sessions=[make_session(1)], text="x")
        rec = await wait_run_done(runner, test_id)
    finally:
        task.cancel()
    assert rec["results"][0]["status"] == "no_reply"


@pytest.mark.asyncio
async def test_runner_status_unknown():
    queue = asyncio.Queue()
    runner = VirtualTestRunner(FakeContext(queue))
    assert runner.status("t_none") is None


@pytest.mark.asyncio
async def test_runner_requires_text():
    queue = asyncio.Queue()
    runner = VirtualTestRunner(FakeContext(queue))
    with pytest.raises(ValueError):
        await runner.start(sessions=[make_session(1)], text="")


# ---------- 配置档案绑定（UCR 路由） ----------


@pytest.mark.asyncio
async def test_runner_applies_and_restores_conf_route():
    queue = asyncio.Queue()
    ucr = FakeUCR()
    runner = VirtualTestRunner(FakeContext(queue, ucr=ucr))

    async def handler(event):
        event.cleanup_temporary_local_files()

    task = asyncio.create_task(consume(queue, handler))
    try:
        test_id = await runner.start(
            sessions=[make_session(1), make_session(2)],
            text="x",
            conf_id="conf_b",
        )
        await wait_run_done(runner, test_id)
        # 路由恢复在全部完成后异步执行，等待其完成
        await asyncio.sleep(0.05)
    finally:
        task.cancel()
    # 测试结束后的临时路由不残留
    assert ucr.umop_to_conf_id == {}


@pytest.mark.asyncio
async def test_runner_restores_previous_route():
    queue = asyncio.Queue()
    ucr = FakeUCR()
    session = make_session(1)
    umop = umo_of(session)
    # 会话原本持久绑定 conf_a
    await ucr.update_route(umop, "conf_a")
    runner = VirtualTestRunner(FakeContext(queue, ucr=ucr))

    async def handler(event):
        event.cleanup_temporary_local_files()

    task = asyncio.create_task(consume(queue, handler))
    try:
        test_id = await runner.start(sessions=[session], text="x", conf_id="conf_b")
        await wait_run_done(runner, test_id)
        await asyncio.sleep(0.05)
    finally:
        task.cancel()
    # 覆盖结束后恢复原有持久路由
    assert ucr.umop_to_conf_id == {umop: "conf_a"}


@pytest.mark.asyncio
async def test_plugin_apply_and_clear_conf_routes(tmp_path):
    context = FakeContext()
    plugin = main_mod.VirtualSessionPlugin(context)
    plugin.group_mgr = VirtualGroupManager(data_dir=tmp_path)
    group = plugin.group_mgr.create_group("组A", count=2, platform_id="virtual_test")
    sessions = [plugin.group_mgr.effective(group, s) for s in group["sessions"]]
    ucr = context.astrbot_config_mgr.ucr

    await plugin._apply_conf_routes(sessions, "conf_c")
    assert all(ucr.umop_to_conf_id[umo_of(s)] == "conf_c" for s in sessions)

    await plugin._clear_conf_routes(sessions)
    assert all(umo_of(s) not in ucr.umop_to_conf_id for s in sessions)


@pytest.mark.asyncio
async def test_plugin_sync_conf_route(tmp_path):
    context = FakeContext()
    plugin = main_mod.VirtualSessionPlugin(context)
    ucr = context.astrbot_config_mgr.ucr
    session = {"id": "vs_1", "platform_id": "virtual_test", "conf_id": "conf_x"}
    await plugin._sync_conf_route(session)
    assert ucr.umop_to_conf_id["virtual_test:FriendMessage:vs_1"] == "conf_x"
    # 无绑定档案时确保路由不存在
    session["conf_id"] = None
    await plugin._sync_conf_route(session)
    assert "virtual_test:FriendMessage:vs_1" not in ucr.umop_to_conf_id


# ---------- 插件 Web 接口（测试组） ----------


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
        umop = f"virtual_test:FriendMessage:{s['id']}"
        assert ucr.umop_to_conf_id[umop] == "conf_c"


@pytest.mark.asyncio
async def test_plugin_create_group_invalid_count(tmp_path):
    plugin = main_mod.VirtualSessionPlugin(FakeContext())
    plugin.group_mgr = VirtualGroupManager(data_dir=tmp_path)
    resp = await call_handler(plugin.create_group, {"name": "组A", "count": 0})
    assert resp.status_code == 400


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
        assert ucr.umop_to_conf_id[f"virtual_test:FriendMessage:{s['id']}"] == "conf_a"


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
    umop = f"virtual_test:FriendMessage:{sid}"
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
    old_umop = f"virtual_test:FriendMessage:{sid}"
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
async def test_plugin_delete_sessions_cleans_routes(tmp_path):
    context = FakeContext()
    plugin = main_mod.VirtualSessionPlugin(context)
    plugin.group_mgr = VirtualGroupManager(data_dir=tmp_path)
    group = plugin.group_mgr.create_group("组A", count=2, conf_id="conf_a")
    ucr = context.astrbot_config_mgr.ucr
    umops = [f"virtual_test:FriendMessage:{s['id']}" for s in group["sessions"]]
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
    umops = [f"virtual_test:FriendMessage:{s['id']}" for s in group["sessions"]]
    for umop in umops:
        await ucr.update_route(umop, "conf_a")

    resp = await call_handler(plugin.delete_groups, {"ids": [group["id"]]})
    assert resp.status_code == 200
    assert json.loads(resp.body)["deleted"] == 2
    assert all(umop not in ucr.umop_to_conf_id for umop in umops)


def _add_history(conv_mgr, sessions: list[dict]) -> list[str]:
    """给每个会话添加一条对话历史，返回对应 umo 列表。"""
    umops = [umo_of(s) for s in sessions]
    for umop in umops:
        conv_mgr.add_history(umop, "对话", [{"role": "user", "content": "hi"}])
    return umops


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


# ---------- 平台列表 ----------


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
async def test_plugin_edit_history(tmp_path):
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

    resp = await call_handler(
        plugin.edit_history,
        {"id": session["id"], "index": 1, "content": "改过了"},
    )
    body = json.loads(resp.body)
    assert body["updated"] == 1
    assert body["history"][1]["content"] == "改过了"

    convs = await conv_mgr.get_conversations(umo)
    assert json.loads(convs[0].history)[1]["content"] == "改过了"


@pytest.mark.asyncio
async def test_plugin_edit_history_parts_content(tmp_path):
    conv_mgr = FakeConvManager()
    plugin = main_mod.VirtualSessionPlugin(FakeContext(conv_mgr=conv_mgr))
    plugin.group_mgr = VirtualGroupManager(data_dir=tmp_path)
    group = plugin.group_mgr.create_group("组A", count=1)
    session = group["sessions"][0]
    umo = umo_of(plugin.group_mgr.effective(group, session))
    conv_mgr.add_history(
        umo,
        "测试",
        [{"role": "user", "content": [{"type": "text", "text": "原文"}]}],
    )

    resp = await call_handler(
        plugin.edit_history,
        {"id": session["id"], "index": 0, "content": "新文本"},
    )
    body = json.loads(resp.body)
    assert body["history"][0]["content"] == [{"type": "text", "text": "新文本"}]


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
        await asyncio.sleep(0.05)
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


# ---------- 统计 ----------


def test_duration_stats_percentiles():
    stats = duration_stats([1.0, 2.0, 3.0, 4.0, 5.0])
    assert stats["min"] == 1.0
    assert stats["max"] == 5.0
    assert stats["avg"] == 3.0
    assert stats["p50"] == 3.0
    assert stats["p95"] == 4.8


def test_duration_stats_empty():
    stats = duration_stats([])
    assert stats == {"min": 0.0, "max": 0.0, "avg": 0.0, "p50": 0.0, "p95": 0.0}


# ---------- 插件模块 ----------


def test_main_module_importable():
    assert main_mod.PLUGIN_NAME == "astrbot_plugin_testbench"
    assert main_mod.VirtualSessionPlugin is not None


def test_session_history_endpoint(tmp_path):
    conv_mgr = FakeConvManager()
    plugin = main_mod.VirtualSessionPlugin(FakeContext(conv_mgr=conv_mgr))
    plugin.group_mgr = VirtualGroupManager(data_dir=tmp_path)
    group = plugin.group_mgr.create_group("组A", count=1, platform_id="virtual_test")
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
    group = plugin.group_mgr.create_group("组A", count=1, platform_id="virtual_test")
    session = group["sessions"][0]

    resp = asyncio.run(plugin.session_history(session["id"]))
    assert resp.status_code == 200
    assert json.loads(resp.body)["conversations"] == []
