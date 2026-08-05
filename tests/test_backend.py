"""后端单元测试：虚拟会话测试平台插件。

测试代码随插件仓库维护。导入插件模块需要安装 astrbot（PyPI 包，插件本身的
运行时依赖）；未安装时整组跳过（见 importorskip）。
"""

import asyncio
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
# 插件模块用相对导入（from .group_store import ...），必须以包形式加载。
# 与 AstrBot 在 data/plugins 下加载插件的方式一致：把插件根目录的父目录加入
# sys.path，以 namespace package（astrbot_plugin_testbench）导入。
sys.path.insert(0, str(REPO_ROOT.parent))

pytest.importorskip("astrbot")

import astrbot_plugin_testbench.assertions as asrt_mod  # noqa: E402
import astrbot_plugin_testbench.event_bus as eb_mod  # noqa: E402
import astrbot_plugin_testbench.group_store as gs_mod  # noqa: E402
import astrbot_plugin_testbench.main as main_mod  # noqa: E402
import astrbot_plugin_testbench.runner as runner_mod  # noqa: E402
import astrbot_plugin_testbench.stats as stats_mod  # noqa: E402
import astrbot_plugin_testbench.testset_runner as tsr_mod  # noqa: E402
import astrbot_plugin_testbench.testset_store as tss_mod  # noqa: E402
import astrbot_plugin_testbench.virtual_event as ve_mod  # noqa: E402
from astrbot.api.event import MessageChain  # noqa: E402
from astrbot.api.web import PluginRequest, bind_request_context  # noqa: E402
from starlette.requests import Request  # noqa: E402

EventBus = eb_mod.EventBus
VirtualMessageEvent = ve_mod.VirtualMessageEvent
VirtualGroupManager = gs_mod.VirtualGroupManager
VirtualTestRunner = runner_mod.VirtualTestRunner
TestsetStore = tss_mod.TestsetStore
TestsetRunner = tsr_mod.TestsetRunner
evaluate_rule = asrt_mod.evaluate_rule
duration_stats = stats_mod.duration_stats
umo_of = gs_mod.umo_of


def make_session(i: int, platform_id: str = "webchat") -> dict:
    """构造一个已解析最终配置的会话（供运行器测试直接使用）。"""
    return {
        "id": f"vs_{i}",
        "name": f"虚拟会话{i}",
        "platform_id": platform_id,
        "sender_id": "testbench",
        "sender_name": "测试台",
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
    assert ev.unified_msg_origin == "webchat:FriendMessage:vs_1"
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
    assert summary["umo"] == "webchat:FriendMessage:vs_1"


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
async def test_send_streaming_empty_stream_marks_finished_and_sets_send_oper():
    async def gen():
        if False:  # 使函数成为空 async generator（永不产出）
            yield

    ev = VirtualMessageEvent.create(
        session_id="vs_1", sender_id="u1", sender_name="用户1", text="hi"
    )
    await ev.send_streaming(gen())
    assert ev.done_event.is_set()
    assert ev.finished_at is not None
    assert ev.result_summary()["status"] == "no_reply"
    # 空流不调 send()，_has_send_oper 须显式置位，避免 stage.py 二次触发 LLM
    assert ev._has_send_oper is True


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
    assert umo_of(make_session(1)) == "webchat:FriendMessage:vs_1"
    assert (
        umo_of({"id": "vs_1", "platform_id": "aiocqhttp"})
        == "aiocqhttp:FriendMessage:vs_1"
    )


# ---------- 测试组管理 ----------


def test_group_manager_create_persist(tmp_path):
    mgr = VirtualGroupManager(data_dir=tmp_path)
    group = mgr.create_group("组A", count=3, platform_id="webchat", name_prefix="测试")
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


def test_group_manager_delete_group_with_no_sessions(tmp_path):
    """0 会话的测试组也必须能删除：删除条件不能依赖 removed 非空。

    组内会话可被逐个删光，此时 delete_groups 的 removed 恒为空列表，
    曾因此跳过 _save() 导致组永远删不掉。
    """
    mgr = VirtualGroupManager(data_dir=tmp_path)
    group = mgr.create_group("空组", count=1)
    mgr.delete_sessions([group["sessions"][0]["id"]])
    assert mgr.get_group(group["id"])["sessions"] == []

    removed = mgr.delete_groups([group["id"]])
    assert removed == []  # 无会话可清，返回空对
    assert mgr.list_groups() == []  # 组必须被真正删除

    # 重新加载（新实例）确认删除已持久化
    mgr2 = VirtualGroupManager(data_dir=tmp_path)
    assert mgr2.list_groups() == []


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


def test_effective_defaults(tmp_path):
    """无组配置时的默认值：平台 webchat，发送者 testbench / 测试台。"""
    mgr = VirtualGroupManager(data_dir=tmp_path)
    group = mgr.create_group("组A", count=1)
    eff = mgr.effective(group, group["sessions"][0])
    assert eff["platform_id"] == "webchat"
    assert eff["sender_id"] == "testbench"
    assert eff["sender_name"] == "测试台"
    assert umo_of(eff) == f"webchat:FriendMessage:{group['sessions'][0]['id']}"


def test_effective_many_order_and_skip(tmp_path):
    mgr = VirtualGroupManager(data_dir=tmp_path)
    group = mgr.create_group("组A", count=3)
    ids = [s["id"] for s in group["sessions"]]
    resolved = mgr.effective_many([ids[2], ids[0], "vs_none"])
    assert [r["id"] for r in resolved] == [ids[2], ids[0]]


def test_update_session_not_found(tmp_path):
    mgr = VirtualGroupManager(data_dir=tmp_path)
    assert mgr.update_session("vs_none", conf_id="x") is None


def test_group_update_fields(tmp_path):
    mgr = VirtualGroupManager(data_dir=tmp_path)
    group = mgr.create_group("组A", count=2, platform_id="webchat", conf_id="conf_a")
    gid = group["id"]

    updated = mgr.update_group(
        gid,
        name="组B",
        platform_id="telegram",
        conf_id="conf_b",
        sender_id="s1",
        sender_name="S1",
    )
    assert updated["name"] == "组B"
    assert updated["platform_id"] == "telegram"
    assert updated["conf_id"] == "conf_b"
    assert updated["sender_id"] == "s1"

    # 未单独覆盖的会话跟随组配置
    eff = mgr.effective(updated, updated["sessions"][0])
    assert eff["platform_id"] == "telegram"
    assert eff["conf_id"] == "conf_b"

    # 空平台/档案归一为 None；空组名回退默认
    mgr.update_group(gid, platform_id="", conf_id="", name="")
    g = mgr.get_group(gid)
    assert g["platform_id"] is None
    assert g["conf_id"] is None
    assert g["name"] == "测试组"

    # 会话覆盖优先于组配置
    mgr.update_session(updated["sessions"][0]["id"], platform_id="webchat")
    eff2 = mgr.effective(g, updated["sessions"][0])
    assert eff2["platform_id"] == "webchat"


def test_group_update_not_found(tmp_path):
    mgr = VirtualGroupManager(data_dir=tmp_path)
    assert mgr.update_group("g_none", name="x") is None


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
    """模拟 UmopConfigRouter：维护 umo -> conf_id 的精确路由表，并统计写入次数。"""

    def __init__(self) -> None:
        self.umop_to_conf_id: dict[str, str] = {}
        self.update_calls = 0
        self.delete_calls = 0

    async def update_route(self, umo: str, conf_id: str) -> None:
        self.update_calls += 1
        self.umop_to_conf_id[umo] = conf_id

    async def delete_route(self, umo: str) -> None:
        self.delete_calls += 1
        self.umop_to_conf_id.pop(umo, None)


class FakeConvManager:
    """模拟 ConversationManager：按 umo 存取对话历史。"""

    def __init__(self) -> None:
        self._convs: dict[str, list[object]] = {}
        self._seq = 0

    def add_history(self, umo: str, title: str, history: list[dict]) -> None:
        conv = SimpleNamespace(
            cid=f"cid_{len(self._convs.get(umo, []))}",
            title=title,
            history=json.dumps(history, ensure_ascii=False),
        )
        self._convs.setdefault(umo, []).append(conv)

    async def new_conversation(
        self,
        unified_msg_origin: str,
        platform_id: str | None = None,
        content: list[dict] | None = None,
        title: str | None = None,
        persona_id: str | None = None,
    ) -> str:
        self._seq += 1
        conv = SimpleNamespace(
            cid=f"new_cid_{self._seq}",
            title=title or "",
            history=json.dumps(content or [], ensure_ascii=False),
        )
        self._convs.setdefault(unified_msg_origin, []).append(conv)
        return conv.cid

    async def get_conversations(self, unified_msg_origin: str) -> list[object]:
        return list(self._convs.get(unified_msg_origin, []))

    async def delete_conversations_by_user_id(self, unified_msg_origin: str) -> int:
        removed = len(self._convs.get(unified_msg_origin, []))
        self._convs.pop(unified_msg_origin, None)
        return removed

    async def delete_conversation(
        self, unified_msg_origin: str, conversation_id: str
    ) -> None:
        convs = self._convs.get(unified_msg_origin, [])
        self._convs[unified_msg_origin] = [c for c in convs if c.cid != conversation_id]

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
        title: str | None = None,
        **kwargs,
    ) -> None:
        for conv in self._convs.get(unified_msg_origin, []):
            if conv.cid == conversation_id:
                if history is not None:
                    conv.history = json.dumps(history, ensure_ascii=False)
                if title is not None:
                    conv.title = title
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


class FakeProvider:
    """模拟 LLM Provider：meta()/get_models()/get_model()/provider_config。"""

    def __init__(
        self,
        provider_id: str,
        provider_type: str,
        models: list[str] | None = None,
        current_model: str | None = None,
        config: dict | None = None,
        raise_models: bool = False,
        raise_meta: bool = False,
        raise_get_model: bool = False,
    ) -> None:
        self._id = provider_id
        self._type = provider_type
        self._models = models or []
        self._current_model = current_model
        self.provider_config = config
        self._raise_models = raise_models
        self._raise_meta = raise_meta
        self._raise_get_model = raise_get_model

    def meta(self):
        if self._raise_meta:
            raise RuntimeError("broken meta")
        return SimpleNamespace(id=self._id, type=self._type)

    async def get_models(self) -> list[str]:
        if self._raise_models:
            raise RuntimeError("broken provider")
        return list(self._models)

    def get_model(self) -> str | None:
        if self._raise_get_model:
            raise RuntimeError("broken get_model")
        return self._current_model


class FakeContext:
    def __init__(
        self,
        queue: asyncio.Queue | None = None,
        ucr: FakeUCR | None = None,
        conv_mgr: FakeConvManager | None = None,
        platform_mgr: FakePlatformManager | None = None,
        providers: list[FakeProvider] | None = None,
        conf_list: list[dict] | None = None,
    ) -> None:
        self._queue = queue or asyncio.Queue()
        self._providers = providers or []
        self.astrbot_config_mgr = SimpleNamespace(
            ucr=ucr or FakeUCR(),
            get_conf_list=lambda: list(conf_list or []),
        )
        self.conversation_manager = conv_mgr or FakeConvManager()
        self.platform_manager = platform_mgr

    def get_event_queue(self) -> asyncio.Queue:
        return self._queue

    def get_all_providers(self) -> list[FakeProvider]:
        return list(self._providers)

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


async def wait_run_done(runner, test_id: str, max_wait: float = 5.0) -> dict:
    """轮询 status 直到 done（模拟前端轮询）。"""
    async with asyncio.timeout(max_wait):
        while True:
            rec = runner.status(test_id)
            if rec and rec["done"]:
                return rec
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


# ---------- 在途消息状态（重叠测试） ----------


@pytest.mark.asyncio
async def test_runner_pending_states():
    """start 登记在途条目，hook 推进状态，pipeline 完成后标记 done。"""
    queue = asyncio.Queue()
    runner = VirtualTestRunner(FakeContext(queue))
    test_id = await runner.start(sessions=[make_session(1)], text="重复追问")
    ev = queue.get_nowait()

    entries = runner.pending_entries()
    assert len(entries) == 1
    entry = entries[0]
    assert entry["session_id"] == "vs_1"
    assert entry["test_id"] == test_id
    assert entry["text"] == "重复追问"
    assert entry["status"] == "submitted"

    runner.mark_waiting_llm(ev.entry_id)
    assert runner.pending_entries()[0]["status"] == "waiting_llm"
    runner.mark_llm(ev.entry_id)
    assert runner.pending_entries()[0]["status"] == "llm"

    # 模拟 pipeline 结束（PipelineScheduler.execute 的 finally 调用）
    ev.cleanup_temporary_local_files()
    await asyncio.sleep(0)  # 让 _await_event 任务完成标记
    assert runner.pending_entries()[0]["status"] == "done"


def test_runner_pending_prune():
    """超时未完成与超时完成的在途条目被清理，未超时保留。"""
    runner = VirtualTestRunner(FakeContext())
    now = time.time()
    runner._pending = {
        "stale_inflight": {
            "entry_id": "stale_inflight",
            "status": "submitted",
            "created_at": now - runner_mod.STALE_RUN_TIMEOUT - 1,
            "status_at": now,
        },
        "stale_done": {
            "entry_id": "stale_done",
            "status": "done",
            "status_at": now - runner_mod.DONE_KEEP_SECONDS - 1,
        },
        "fresh": {
            "entry_id": "fresh",
            "status": "llm",
            "created_at": now,
            "status_at": now,
        },
    }
    runner._prune_runs()
    assert set(runner._pending) == {"fresh"}


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
    group = plugin.group_mgr.create_group("组A", count=2, platform_id="webchat")
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
    session = {"id": "vs_1", "platform_id": "webchat", "conf_id": "conf_x"}
    await plugin._sync_conf_route(session)
    assert ucr.umop_to_conf_id["webchat:FriendMessage:vs_1"] == "conf_x"
    # 无绑定档案时确保路由不存在
    session["conf_id"] = None
    await plugin._sync_conf_route(session)
    assert "webchat:FriendMessage:vs_1" not in ucr.umop_to_conf_id


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
        umop = f"webchat:FriendMessage:{s['id']}"
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


# ---------- Provider / 配置档案列表 ----------


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
    # 无 provider_config 时回落 meta 的 id / type
    assert body[1]["id"] == "prov_b"
    assert body[1]["name"] == "anthropic"


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
    assert body[0] == {"id": "conf_a", "name": "档案A", "path": "/a"}
    # 缺 id 回落 name，缺 name 回落 id，缺 path 为 None
    assert body[1] == {"id": "只有名字", "name": "只有名字", "path": None}
    assert body[2] == {"id": "conf_c", "name": "conf_c", "path": None}


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


def test_msg_text_parts_array():
    """_msg_text 对 content 为 parts 数组（字符串/对象混合）的提取。"""
    plugin_cls = main_mod.VirtualSessionPlugin
    assert plugin_cls._msg_text({"content": "纯字符串"}) == "纯字符串"
    assert plugin_cls._msg_text({"content": None}) == ""
    assert plugin_cls._msg_text({}) == ""
    msg = {
        "content": [
            "纯文本段",
            {"text": "对象文本段"},
            {"content": "content 键"},
            {"type": "image", "url": "..."},  # 无 text/content → 空串，被过滤
            {"text": "末尾段"},
        ]
    }
    assert plugin_cls._msg_text(msg) == "纯文本段\n对象文本段\ncontent 键\n末尾段"


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


# ---------- 回复断言（assertions 纯函数） ----------


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


# ---------- 测试集存储 ----------


def test_testset_store_crud_persist(tmp_path):
    store = TestsetStore(data_dir=tmp_path)
    ts = store.create_testset(
        "回归",
        [
            {"text": "第一问", "rule": {"type": "contains", "value": "在"}},
            {"text": "第二问"},
        ],
    )
    assert ts["id"].startswith("ts_")
    assert ts["name"] == "回归"
    assert len(store.list_testsets()) == 1

    # 重载实例断言持久化
    reloaded = TestsetStore(data_dir=tmp_path)
    assert len(reloaded.list_testsets()) == 1
    assert reloaded.get_testset(ts["id"])["messages"][0]["rule"] == {
        "type": "contains",
        "value": "在",
    }

    updated = store.update_testset(ts["id"], "改名", [{"text": "新问"}])
    assert updated["name"] == "改名"
    assert len(updated["messages"]) == 1
    assert store.update_testset("ts_none", "x", [{"text": "x"}]) is None

    assert store.delete_testsets([ts["id"]]) == 1
    assert store.list_testsets() == []


def test_testset_store_normalize_and_default_name(tmp_path):
    store = TestsetStore(data_dir=tmp_path)
    ts = store.create_testset(
        "  ",
        [
            {"text": "  去空白  ", "rule": "不是字典"},
            {"text": "  "},  # 空文本丢弃
        ],
    )
    assert ts["name"] == "测试集"  # 空名回退
    assert len(ts["messages"]) == 1
    assert ts["messages"][0] == {"text": "去空白", "rule": None}


def test_testset_store_delete_unknown(tmp_path):
    store = TestsetStore(data_dir=tmp_path)
    store.create_testset("A", [{"text": "m"}])
    assert store.delete_testsets(["ts_none"]) == 0
    assert len(store.list_testsets()) == 1


def test_testset_store_batch_ranges_normalize(tmp_path):
    store = TestsetStore(data_dir=tmp_path)
    # 合法：排序 + 去重保序（乱序输入按 start 升序）
    ts = store.create_testset(
        "批量",
        [{"text": f"m{i}"} for i in range(4)],
        batch_ranges=[[2, 3], [0, 0]],
    )
    assert ts["batch_ranges"] == [[0, 0], [2, 3]]

    # 单条不合法（越界 / 倒序 / bool / 非整数对 / 非 list）→ 整段丢弃或清空
    cases = [
        [[-1, 1]],
        [[0, 4]],  # 越界（message_count=4）
        [[2, 1]],  # s > e
        [[0, True]],
        [[0]],
        "not-a-list",
    ]
    for ranges in cases:
        assert (
            store.create_testset("x", [{"text": "m"}] * 4, ranges)["batch_ranges"] == []
        ), ranges

    # 部分不合法 → 合法段保留（重叠段丢弃 / 非法项丢弃，结果与输入顺序无关）
    assert store.create_testset("x", [{"text": "m"}] * 4, [[0, 1], [1, 2]])[
        "batch_ranges"
    ] == [[0, 1]]
    assert store.create_testset("x", [{"text": "m"}] * 4, [[1, 2], [0, 1]])[
        "batch_ranges"
    ] == [[0, 1]]
    assert store.create_testset("x", [{"text": "m"}] * 4, [[0, 1], "x"])[
        "batch_ranges"
    ] == [[0, 1]]

    # 更新时按新消息数重新规范化（索引基于存储后的消息序列）
    ts2 = store.create_testset("再", [{"text": "a"}, {"text": "b"}], [[0, 1]])
    updated = store.update_testset(ts2["id"], "再改", [{"text": "a"}], [[0, 1]])
    assert updated["batch_ranges"] == []  # 消息只剩 1 条，越界丢弃

    # 持久化 + 旧数据 setdefault
    reloaded = TestsetStore(data_dir=tmp_path)
    assert reloaded.get_testset(ts["id"])["batch_ranges"] == [[0, 0], [2, 3]]
    legacy = {"testsets": [{"id": "ts_old", "name": "旧", "messages": []}]}
    (tmp_path / "virtual_session" / "testsets.json").write_text(
        json.dumps(legacy), encoding="utf-8"
    )
    legacy_store = TestsetStore(data_dir=tmp_path)
    assert legacy_store.get_testset("ts_old")["batch_ranges"] == []


# ---------- runner.wait_done ----------


@pytest.mark.asyncio
async def test_runner_wait_done_returns_status():
    queue = asyncio.Queue()
    runner = VirtualTestRunner(FakeContext(queue))

    async def handler(event):
        await event.send(MessageChain().message("ok"))
        event.cleanup_temporary_local_files()

    task = asyncio.create_task(consume(queue, handler))
    try:
        test_id = await runner.start(sessions=[make_session(1)], text="hi")
        rec = await runner.wait_done(test_id, timeout_secs=5.0)
        assert rec["done"] is True
        assert rec["results"][0]["status"] == "ok"
    finally:
        task.cancel()


@pytest.mark.asyncio
async def test_runner_wait_done_timeout_and_unknown():
    queue = asyncio.Queue()
    runner = VirtualTestRunner(FakeContext(queue))
    # 无人消费：pipeline 永不完成 → 超时抛 asyncio.TimeoutError
    test_id = await runner.start(sessions=[make_session(1)], text="hi")
    with pytest.raises(TimeoutError):
        await runner.wait_done(test_id, timeout_secs=0.05)
    with pytest.raises(KeyError):
        await runner.wait_done("t_none", timeout_secs=0.05)
    # 收尾：放行悬挂的 _await_event，避免挂起任务泄漏
    queue.get_nowait().cleanup_temporary_local_files()
    await asyncio.sleep(0.01)


# ---------- 测试集运行编排器（TestsetRunner） ----------


async def wait_testset_done(
    tsr: TestsetRunner, run_id: str, max_wait: float = 5.0
) -> dict:
    """轮询测试集运行状态直到终态（模拟前端轮询）。"""
    async with asyncio.timeout(max_wait):
        while True:
            rec = tsr.status(run_id)
            if rec and rec["status"] != "running":
                return rec
            await asyncio.sleep(0.01)


def _make_testset(
    testset_id: str,
    name: str,
    texts: list[tuple[str, dict | None]],
    batch_ranges: list[list[int]] | None = None,
) -> dict:
    return {
        "id": testset_id,
        "name": name,
        "created_at": 0,
        "messages": [{"text": t, "rule": r} for t, r in texts],
        "batch_ranges": batch_ranges or [],
    }


@pytest.mark.asyncio
async def test_testset_runner_sequential():
    queue = asyncio.Queue()
    context = FakeContext(queue)
    tsr = TestsetRunner(context, VirtualTestRunner(context))
    processed: list[str] = []

    async def handler(event):
        processed.append(event.message_str)
        await event.send(MessageChain().message(f"回复 {event.message_str}"))
        event.cleanup_temporary_local_files()

    task = asyncio.create_task(consume(queue, handler))
    try:
        testset = _make_testset(
            "ts_1",
            "顺序测试",
            [
                ("第一问", {"type": "contains", "value": "回复 第一问"}),
                ("第二问", None),
            ],
        )
        run_id = tsr.start_run(testset, [make_session(1), make_session(2)])
        rec = await wait_testset_done(tsr, run_id)
    finally:
        task.cancel()
    assert rec["status"] == "done"
    # 无批量段：每步全部会话完成才发下一条
    assert processed == ["第一问", "第一问", "第二问", "第二问"]
    assert [s["status"] for s in rec["steps"]] == ["done", "done"]
    assert rec["steps"][0]["results"][0]["assertion"]["pass"] is True


@pytest.mark.asyncio
async def test_testset_runner_batch_segment():
    queue = asyncio.Queue()
    context = FakeContext(queue)
    tsr = TestsetRunner(context, VirtualTestRunner(context))
    processed: list[str] = []

    async def handler(event):
        processed.append(event.message_str)
        await event.send(MessageChain().message(f"回复 {event.message_str}"))
        event.cleanup_temporary_local_files()

    task = asyncio.create_task(consume(queue, handler))
    try:
        testset = _make_testset(
            "ts_2",
            "批量测试",
            [("b1", None), ("b2", {"type": "not_contains", "value": "绝对不存在"})],
            batch_ranges=[[0, 1]],
        )
        run_id = tsr.start_run(testset, [make_session(1)])
        rec = await wait_testset_done(tsr, run_id)
    finally:
        task.cancel()
    assert rec["status"] == "done"
    assert sorted(processed) == ["b1", "b2"]  # 批量段内两条消息均已发出（重叠）
    assert rec["steps"][1]["results"][0]["assertion"]["pass"] is True


@pytest.mark.asyncio
async def test_testset_runner_mixed_segments():
    queue = asyncio.Queue()
    context = FakeContext(queue)
    tsr = TestsetRunner(context, VirtualTestRunner(context))
    processed: list[str] = []

    async def handler(event):
        processed.append(event.message_str)
        if event.message_str == "B":
            # B 在回复前等 C 已入队 → 证明 B、C 同时发出（批量段重叠）；
            # 段外消息 A 完成前 B 不会入队（逐条等待）。runner 是黑盒、没有
            # 外部信号可等，只能轮询队列深度观察入队时序
            async with asyncio.timeout(5.0):
                while queue.qsize() == 0:  # noqa: ASYNC110
                    await asyncio.sleep(0.001)
        await event.send(MessageChain().message(f"回复 {event.message_str}"))
        event.cleanup_temporary_local_files()

    task = asyncio.create_task(consume(queue, handler))
    try:
        testset = _make_testset(
            "ts_5",
            "混合节奏",
            [("A", None), ("B", None), ("C", None), ("D", None)],
            batch_ranges=[[1, 2]],
        )
        run_id = tsr.start_run(testset, [make_session(1)])
        rec = await wait_testset_done(tsr, run_id)
    finally:
        task.cancel()
    assert rec["status"] == "done"
    assert rec["batch_ranges"] == [[1, 2]]
    # A 是单步段：先于 B、C 完成；B、C 是批量段：D 之前完成
    assert processed.index("A") < processed.index("B")
    assert processed.index("A") < processed.index("C")
    assert processed.index("B") < processed.index("D")
    assert processed.index("C") < processed.index("D")
    assert all(s["status"] == "done" for s in rec["steps"])


@pytest.mark.asyncio
async def test_testset_runner_step_timeout(monkeypatch):
    monkeypatch.setattr(tsr_mod, "TESTSET_STEP_TIMEOUT", 0.05)
    queue = asyncio.Queue()
    context = FakeContext(queue)
    tsr = TestsetRunner(context, VirtualTestRunner(context))

    testset = _make_testset("ts_3", "超时测试", [("m1", None), ("m2", None)])
    run_id = tsr.start_run(testset, [make_session(1)])
    rec = await wait_testset_done(tsr, run_id)
    # 收尾：放行悬挂的 _await_event
    while not queue.empty():
        queue.get_nowait().cleanup_temporary_local_files()
    await asyncio.sleep(0.01)

    assert rec["status"] == "error"
    assert rec["steps"][0]["status"] == "error"
    assert "超时" in rec["steps"][0]["error"]
    assert rec["steps"][1]["status"] == "pending"  # 后续步骤未发
    assert "超时" in rec["error"]


@pytest.mark.asyncio
async def test_testset_runner_abort():
    queue = asyncio.Queue()
    context = FakeContext(queue)
    tsr = TestsetRunner(context, VirtualTestRunner(context))
    gate = asyncio.Event()

    async def handler(event):
        if event.message_str == "第一步":
            await gate.wait()  # 阻塞当前步骤，直到 abort 确认后再放行
        await event.send(MessageChain().message(f"回复 {event.message_str}"))
        event.cleanup_temporary_local_files()

    task = asyncio.create_task(consume(queue, handler))
    try:
        testset = _make_testset(
            "ts_4", "取消测试", [("第一步", None), ("第二步", None)]
        )
        run_id = tsr.start_run(testset, [make_session(1)])
        async with asyncio.timeout(5.0):
            while True:
                if tsr.status(run_id)["current_step"] == 0:
                    break
                await asyncio.sleep(0.01)
        assert tsr.abort(run_id) is True
        gate.set()  # 放行当前步骤
        # abort 只置标记：run 状态立即变 cancelled，但当前步骤仍在异步完成——
        # 因此轮询「步骤 0 落定」而非 run 状态。
        async with asyncio.timeout(5.0):
            while True:
                rec = tsr.status(run_id)
                if rec["steps"][0]["status"] == "done":
                    break
                await asyncio.sleep(0.01)
    finally:
        task.cancel()
    assert rec["status"] == "cancelled"
    assert rec["steps"][0]["status"] == "done"  # 当前步骤照常完成并收结果
    assert rec["steps"][1]["status"] == "pending"  # 后续不再发
    assert rec["steps"][1]["test_id"] is None


@pytest.mark.asyncio
async def test_testset_runner_batch_segment_abort_collects_started():
    """批量段收集中途 abort：段内已发出的步骤必须全部收完结果，不能卡在 running。"""
    queue = asyncio.Queue()
    context = FakeContext(queue)
    tsr = TestsetRunner(context, VirtualTestRunner(context))
    gate = asyncio.Event()
    processed: list[str] = []

    async def handler(event):
        processed.append(event.message_str)
        await gate.wait()  # 两条都阻塞：保证 abort 落在「收集中」（wait_done 在等）
        await event.send(MessageChain().message(f"回复 {event.message_str}"))
        event.cleanup_temporary_local_files()

    # 与真实 EventBus 一致：每个事件并行处理（串行 consume 会卡在阻塞的 b1 上）
    async def consume_parallel(queue, handler):
        while True:
            event = await queue.get()
            asyncio.create_task(handler(event))

    task = asyncio.create_task(consume_parallel(queue, handler))
    try:
        testset = _make_testset(
            "ts_6", "批量段取消", [("b1", None), ("b2", None)], batch_ranges=[[0, 1]]
        )
        run_id = tsr.start_run(testset, [make_session(1)])
        # 两条已同时发出（批量段重叠）；两条 handler 都在等 gate ⇒ 收集循环必然
        # 已阻塞在 wait_done 上，abort 精确落在「收集中」
        async with asyncio.timeout(5.0):
            while len(processed) < 2:  # noqa: ASYNC110
                await asyncio.sleep(0.01)
        assert tsr.abort(run_id) is True
        gate.set()
        async with asyncio.timeout(5.0):
            while True:
                rec = tsr.status(run_id)
                if rec["steps"][0]["status"] == "done":
                    break
                await asyncio.sleep(0.01)
    finally:
        task.cancel()
    assert rec["status"] == "cancelled"
    # 已发出的两条都要落定；旧实现收集循环遇 abort 提前 break，步骤 1 永远 running
    assert [s["status"] for s in rec["steps"]] == ["done", "done"]
    assert all(s["test_id"] for s in rec["steps"])


def test_testset_runner_segments_edge_cases():
    # _segments 是纯切分：单条批量段 [i,i] 与完全平铺 [[0,n-1]] 的边界
    tsr = TestsetRunner(FakeContext(), VirtualTestRunner(FakeContext()))
    run = {"steps": [{} for _ in range(4)], "batch_ranges": [[1, 1]]}
    assert tsr._segments(run) == [
        ([0], False),
        ([1], True),
        ([2], False),
        ([3], False),
    ]
    run = {"steps": [{} for _ in range(3)], "batch_ranges": [[0, 2]]}
    assert tsr._segments(run) == [([0, 1, 2], True)]
    run = {"steps": [{} for _ in range(3)], "batch_ranges": []}
    assert tsr._segments(run) == [([0], False), ([1], False), ([2], False)]


def test_testset_runner_list_runs_limit():
    context = FakeContext()
    tsr = TestsetRunner(context, VirtualTestRunner(context))
    now = time.time()
    base = {
        "run_id": "",
        "testset_id": "ts",
        "testset_name": "",
        "batch_ranges": [],
        "status": "done",
        "current_step": -1,
        "steps": [],
        "started_at": 0,
        "finished_at": None,
        "error": None,
    }
    tsr._runs = {
        f"tr_{i}": dict(base, run_id=f"tr_{i}", started_at=now + i) for i in range(3)
    }
    runs = tsr.list_runs(limit=2)
    assert [r["run_id"] for r in runs] == ["tr_2", "tr_1"]  # 倒序 + limit 截断


def test_testset_runner_list_runs_and_prune():
    context = FakeContext()
    tsr = TestsetRunner(context, VirtualTestRunner(context))
    now = time.time()
    base = {
        "run_id": "",
        "testset_id": "ts",
        "testset_name": "旧完成",
        "batch_ranges": [],
        "status": "done",
        "current_step": -1,
        "steps": [],
        "started_at": 0,
        "finished_at": None,
        "error": None,
    }
    old_done = dict(
        base, run_id="tr_done_old", started_at=now - 3600, finished_at=now - 661
    )
    stale = dict(
        base,
        run_id="tr_running_stale",
        testset_name="悬挂",
        status="running",
        started_at=now - 3601,
        finished_at=None,
    )
    fresh = dict(
        base,
        run_id="tr_fresh",
        testset_name="新鲜",
        status="running",
        started_at=now,
        finished_at=None,
    )
    tsr._runs = {r["run_id"]: r for r in (old_done, stale, fresh)}

    runs = tsr.list_runs()
    assert [r["run_id"] for r in runs] == ["tr_fresh"]  # 过期完成与悬挂运行被清理


# ---------- 测试集 Web 接口 ----------


@pytest.mark.asyncio
async def test_plugin_testset_crud(tmp_path):
    plugin = main_mod.VirtualSessionPlugin(FakeContext())
    plugin.testset_store = TestsetStore(data_dir=tmp_path)

    resp = await call_handler(
        plugin.create_testset,
        {
            "name": "回归测试",
            "messages": [
                {"text": "第一问", "rule": {"type": "contains", "value": "你好"}},
                {"text": "第二问"},
            ],
        },
    )
    assert resp.status_code == 200
    ts = json.loads(resp.body)
    assert ts["id"].startswith("ts_")
    assert len(ts["messages"]) == 2
    assert ts["messages"][1]["rule"] is None  # 缺 rule → None

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

    # runs 列表包含该运行
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


# ---------- 事件总线 / SSE 推送 ----------


class RecordingBus:
    """记录全部发布事件的替身总线（duck-typed，仅需 publish）。"""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def subscribe(self):
        return None

    def unsubscribe(self, queue) -> None:
        pass

    def publish(self, event: dict) -> None:
        self.events.append(event)


@pytest.mark.asyncio
async def test_event_bus_broadcast_and_drop_oldest():
    bus = EventBus(maxlen=2)
    q1 = bus.subscribe()
    q2 = bus.subscribe()
    bus.publish({"type": "a"})
    bus.publish({"type": "b"})
    # 双订阅者各收全量
    assert q1.get_nowait() == {"type": "a"}
    assert q2.get_nowait() == {"type": "a"}
    assert q1.get_nowait() == {"type": "b"}
    # 队列满（容量 2）→ 丢最旧 "a"，最新 "c"/"d" 仍送达
    bus.publish({"type": "c"})
    bus.publish({"type": "d"})
    assert q1.get_nowait() == {"type": "c"}
    assert q1.get_nowait() == {"type": "d"}
    assert q1.empty()


@pytest.mark.asyncio
async def test_event_bus_unsubscribe():
    bus = EventBus()
    q = bus.subscribe()
    bus.unsubscribe(q)
    bus.publish({"type": "a"})
    assert q.empty()


@pytest.mark.asyncio
async def test_runner_publishes_pending_session_test_events():
    bus = RecordingBus()
    queue = asyncio.Queue()
    runner = VirtualTestRunner(FakeContext(queue), event_bus=bus)

    async def handler(event):
        await event.send(MessageChain().message("ok"))
        event.cleanup_temporary_local_files()

    task = asyncio.create_task(consume(queue, handler))
    try:
        test_id = await runner.start(sessions=[make_session(1)], text="你好")
        rec = await wait_run_done(runner, test_id)
    finally:
        task.cancel()
    assert rec["done"] is True
    # start → 在途快照（submitted）→ … → 完成快照（done）
    pending = [e for e in bus.events if e["type"] == "pending"]
    assert pending, "start 后未发布在途快照"
    assert pending[0]["entries"][0]["status"] == "submitted"
    assert pending[-1]["entries"][0]["status"] == "done"
    # 会话完成事件 → 含结果摘要；测试完成事件 → 含完整 status()
    session_done = [e for e in bus.events if e["type"] == "session_done"]
    assert session_done, "未发布 session_done"
    assert session_done[0]["test_id"] == test_id
    assert session_done[0]["summary"]["session_id"] == "vs_1"
    test_done = [e for e in bus.events if e["type"] == "test_done"]
    assert test_done, "未发布 test_done"
    assert test_done[0]["record"]["done"] is True
    assert test_done[0]["record"]["results"][0]["reply"] == "ok"


@pytest.mark.asyncio
async def test_testset_runner_publishes_run_events():
    bus = RecordingBus()
    queue = asyncio.Queue()
    context = FakeContext(queue)
    tsr = TestsetRunner(context, VirtualTestRunner(context), event_bus=bus)

    async def handler(event):
        await event.send(MessageChain().message("回复"))
        event.cleanup_temporary_local_files()

    task = asyncio.create_task(consume(queue, handler))
    try:
        testset = _make_testset("ts_ev", "事件测试", [("问", None)])
        run_id = tsr.start_run(testset, [make_session(1)])
        rec = await wait_testset_done(tsr, run_id)
    finally:
        task.cancel()
    assert rec["status"] == "done"
    # testset 事件为完整 run 快照：先 running，末条终态 done
    testset_events = [e for e in bus.events if e["type"] == "testset"]
    assert testset_events, "未发布 testset 运行快照"
    assert testset_events[0]["run_id"] == run_id
    assert testset_events[0]["run"]["status"] == "running"
    assert testset_events[-1]["run"]["status"] == "done"
    assert testset_events[-1]["run"]["steps"][0]["status"] == "done"
