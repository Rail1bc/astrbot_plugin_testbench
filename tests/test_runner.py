"""运行器与虚拟事件测试：VirtualMessageEvent 捕获、VirtualTestRunner
并发/在途/路由/晚到检测、EventBus 与消息类型语义。"""

import asyncio
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
# 插件模块用相对导入（from .group_store import ...），必须以包形式加载。
# 与 AstrBot 在 data/plugins 下加载插件的方式一致：把插件根目录的父目录加入
# sys.path，以 namespace package（astrbot_plugin_testbench）导入。
sys.path.insert(0, str(REPO_ROOT.parent))

pytest.importorskip("astrbot")

import astrbot_plugin_testbench.core.conf_routes as cr_mod  # noqa: E402
import astrbot_plugin_testbench.core.event_bus as eb_mod  # noqa: E402
import astrbot_plugin_testbench.core.runner as runner_mod  # noqa: E402
import astrbot_plugin_testbench.core.virtual_event as ve_mod  # noqa: E402
import astrbot_plugin_testbench.main as main_mod  # noqa: E402
import astrbot_plugin_testbench.stats as stats_mod  # noqa: E402
import astrbot_plugin_testbench.store.group_store as gs_mod  # noqa: E402
import astrbot_plugin_testbench.store.identity_store as ids_mod  # noqa: E402
import astrbot_plugin_testbench.store.stream_store as stm_mod  # noqa: E402
from astrbot.api.event import MessageChain  # noqa: E402
from astrbot.api.message_components import At, Plain  # noqa: E402
from astrbot.api.platform import MessageType  # noqa: E402

ChatGroupStore = ids_mod.ChatGroupStore
EventBus = eb_mod.EventBus
IdentityStore = ids_mod.IdentityStore
StreamStore = stm_mod.StreamStore
VirtualGroupManager = gs_mod.VirtualGroupManager
VirtualMessageEvent = ve_mod.VirtualMessageEvent
VirtualTestRunner = runner_mod.VirtualTestRunner
duration_stats = stats_mod.duration_stats
umo_of = gs_mod.umo_of

from fakes import (  # noqa: E402
    FakeContext,
    FakeUCR,
    RecordingBus,
    _FailingReplyStreamStore,
    consume,
    make_session,
    wait_run_done,
    wait_until,
)


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
        # 路由恢复在全部完成后异步执行，等待其完成（轮询替代固定 sleep）
        await wait_until(lambda: ucr.umop_to_conf_id == {})
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
        # 覆盖结束后恢复原有持久路由（轮询替代固定 sleep）
        await wait_until(lambda: ucr.umop_to_conf_id == {umop: "conf_a"})
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


@pytest.mark.asyncio
@pytest.mark.framework_internal
async def test_conf_route_precedence_over_broad_fallback():
    """插件精确路由须优先于用户已配的「全部会话」兜底（真实 UCR 端到端）。

    直接依赖 AstrBot 内部模块（astrbot.core.umop_config_router，无版本契约），
    最低支持版矩阵下跳过（见 .github/workflows/pytest.yml）。

    AstrBot UCR 按 dict 插入顺序**首个匹配即返回**（get_conf_id_for_umop 顺序
    遍历），update_route 对新键追加到末尾——兜底路由先插入时，后追加的会话级
    精确路由会被遮蔽。put_route_front 表头插入后：绑定会话解析到精确档案，
    未绑定会话/其他类型仍落回兜底与平台级规则。
    """

    class FakeSP:
        def __init__(self) -> None:
            self._store: dict = {}

        async def global_put(self, key: str, value: object) -> None:
            self._store[key] = dict(value)

        async def get_async(self, key: str, default: object = None, **kwargs) -> object:
            return self._store.get(key, dict(default))

    from astrbot.core.umop_config_router import UmopConfigRouter

    ucr = UmopConfigRouter(FakeSP())
    await ucr.initialize()
    # 用户先配置平台级群聊规则，再配置「全部会话」兜底（规则相对顺序自此固定）
    await ucr.update_route("webchat:GroupMessage:*", "conf_group")
    await ucr.update_route("::", "conf_fallback")
    plugin = main_mod.VirtualSessionPlugin(FakeContext(ucr=ucr))
    session = {"id": "vs_abc", "platform_id": "webchat", "conf_id": "conf_specific"}
    await plugin._sync_conf_route(session)
    umop = umo_of(session)
    # 精确路由位于表头（先于兜底命中）
    assert list(ucr.umop_to_conf_id)[0] == umop
    assert ucr.get_conf_id_for_umop(umop) == "conf_specific"
    assert ucr.get_conf_id_for_umop("webchat:FriendMessage:vs_abc") == "conf_specific"
    # put_route_front 重排 dict 不破坏既有规则的相对顺序：未绑定私聊落回兜底、
    # 群聊仍走平台级规则（此处兜底在后，故不遮蔽群聊规则）
    assert ucr.get_conf_id_for_umop("webchat:FriendMessage:vs_other") == "conf_fallback"
    assert ucr.get_conf_id_for_umop("webchat:GroupMessage:vs_any") == "conf_group"
    # 无绑定档案时清理路由，兜底恢复生效
    session["conf_id"] = None
    await plugin._sync_conf_route(session)
    assert ucr.get_conf_id_for_umop(umop) == "conf_fallback"


@pytest.mark.asyncio
async def test_conf_route_temporary_front_and_restore():
    """runner 临时路由同样表头优先于兜底；结束后恢复原路由/删除临时路由。"""
    ucr = FakeUCR()
    # 用户已有「全部会话」兜底，会话本身无绑定
    await ucr.update_route("webchat::", "conf_fallback")
    session = make_session(1)
    umop = umo_of(session)
    saved = await cr_mod.save_and_apply_routes(ucr, [session], "conf_tmp")
    assert list(ucr.umop_to_conf_id)[0] == umop
    assert ucr.umop_to_conf_id[umop] == "conf_tmp"
    assert saved == [(umop, None)]
    await cr_mod.restore_routes(ucr, saved)
    assert umop not in ucr.umop_to_conf_id
    assert ucr.umop_to_conf_id["webchat::"] == "conf_fallback"
    # 原本有持久绑定的会话：恢复原值（键在表头、值还原）
    await ucr.update_route(umop, "conf_persist")
    saved = await cr_mod.save_and_apply_routes(ucr, [session], "conf_tmp")
    assert saved == [(umop, "conf_persist")]
    await cr_mod.restore_routes(ucr, saved)
    assert ucr.umop_to_conf_id[umop] == "conf_persist"


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


@pytest.mark.asyncio
async def test_runner_multiple_sessions_inflight_simultaneously():
    """TB-28: 多会话同时 pending——并发是核心卖点：3 个会话的消息全部入队后
    才陆续完成，pending 中同时存在全部 submitted 条目（不是逐条串行）。"""
    queue = asyncio.Queue()
    runner = VirtualTestRunner(FakeContext(queue))
    gate = asyncio.Event()

    async def handler(event):
        await gate.wait()
        await event.send(MessageChain().message("ok"))
        event.cleanup_temporary_local_files()

    # 与真实 EventBus 一致：每个事件并行处理
    async def consume_parallel(queue, handler):
        while True:
            event = await queue.get()
            asyncio.create_task(handler(event))

    task = asyncio.create_task(consume_parallel(queue, handler))
    try:
        test_id = await runner.start(
            sessions=[make_session(1), make_session(2), make_session(3)],
            text="并发消息",
        )
        # 全部 3 条已入队（handler 都阻塞在 gate 上 → 不可能有 done）
        await wait_until(lambda: len(runner.pending_entries()) >= 3)
        entries = runner.pending_entries()
        assert {e["session_id"] for e in entries} == {"vs_1", "vs_2", "vs_3"}
        assert all(e["status"] == "submitted" for e in entries)
        gate.set()
        rec = await wait_run_done(runner, test_id)
    finally:
        task.cancel()
    assert rec["done"] is True
    assert {r["session_id"] for r in rec["results"]} == {"vs_1", "vs_2", "vs_3"}


@pytest.mark.asyncio
async def test_runner_out_of_order_completion():
    """TB-28: 乱序完成：各会话处理时长不同（完成顺序与入队顺序相反），
    结果按 session 正确归位、全部收敛。"""
    queue = asyncio.Queue()
    runner = VirtualTestRunner(FakeContext(queue))

    async def handler(event):
        # 会话 id 末尾数字越大等待越久 → 完成顺序与入队顺序相反
        await asyncio.sleep(0.02 * int(event.session_id.split("_")[1]))
        await event.send(MessageChain().message(f"回复 {event.session_id}"))
        event.cleanup_temporary_local_files()

    task = asyncio.create_task(consume(queue, handler))
    try:
        test_id = await runner.start(
            sessions=[make_session(1), make_session(2), make_session(3)],
            text="乱序",
        )
        rec = await wait_run_done(runner, test_id)
    finally:
        task.cancel()
    assert {r["session_id"] for r in rec["results"]} == {"vs_1", "vs_2", "vs_3"}
    for r in rec["results"]:
        assert r["reply"] == f"回复 {r['session_id']}"


def test_runner_multi_rule_assertion():
    """多规则断言聚合：无规则 → None；单条保持 {pass, detail}；多条 all-pass
    聚合 {pass, detail:[...]}。"""
    ev = VirtualTestRunner._evaluate_assertions
    assert ev(None, "x") is None
    assert ev([], "x") is None
    assert ev([None], "x") is None  # 规则全部无效 → None
    # 单条保持旧结构（向后兼容）
    assert ev({"type": "contains", "value": "好"}, "你好") == {
        "pass": True,
        "detail": "回复包含 '好'",
    }
    # 多条 all-pass 聚合
    assert ev(
        [{"type": "contains", "value": "好"}, {"type": "contains", "value": "不"}],
        "你好",
    ) == {
        "pass": False,
        "detail": [
            {"pass": True, "detail": "回复包含 '好'"},
            {"pass": False, "detail": "回复不包含 ['不']"},
        ],
    }


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
async def test_event_bus_publisher_not_blocked_by_slow_consumer():
    """TB-30: 慢消费者（从不消费）不阻塞发布者：publish 为同步调用、满则丢最旧，
    内存只保留最新 maxlen 条——页面切后台时发布者行为不变。
    """
    bus = EventBus(maxlen=3)
    q = bus.subscribe()
    for i in range(50):
        bus.publish({"type": "e", "i": i})  # 同步返回：不 await、不抛错
    remaining = []
    while not q.empty():
        remaining.append(q.get_nowait()["i"])
    assert remaining == [47, 48, 49]  # 只保留最新 3 条（maxlen）


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


def test_virtual_event_group_type():
    ev = VirtualMessageEvent.create(
        session_id="vs_1",
        sender_id="u1",
        sender_name="用户1",
        text="hi",
        message_type="GroupMessage",
    )
    assert ev.message_obj.type == MessageType.GROUP_MESSAGE
    assert ev.get_message_type().value == "GroupMessage"
    assert ev.unified_msg_origin == "webchat:GroupMessage:vs_1"


def test_virtual_event_auto_at_chain():
    """auto_at 开启：消息链以 At(self_id) 开头 + Plain(text)，message_str 保持纯文本。"""
    ev = VirtualMessageEvent.create(
        session_id="vs_1",
        sender_id="u1",
        sender_name="用户1",
        text="你好",
        message_type="GroupMessage",
        auto_at=True,
    )
    chain = ev.get_messages()
    assert isinstance(chain[0], At)
    assert chain[0].qq == "virtual_bot"
    assert isinstance(chain[1], Plain)
    assert chain[1].text == "你好"
    assert ev.message_str == "你好"

    # 关闭 auto_at：链只有 Plain
    ev2 = VirtualMessageEvent.create(
        session_id="vs_1",
        sender_id="u1",
        sender_name="用户1",
        text="hi",
        message_type="GroupMessage",
        auto_at=False,
    )
    chain2 = ev2.get_messages()
    assert len(chain2) == 1
    assert isinstance(chain2[0], Plain)
    assert ev2.message_str == "hi"


def test_result_summary_wake_fields():
    """唤醒状态与 no_reply 原因：未唤醒 / 已唤醒但无回复。"""
    ev = VirtualMessageEvent.create(
        session_id="vs_1", sender_id="u1", sender_name="用户1", text="hi"
    )
    ev.is_wake = True
    ev.is_at_or_wake_command = True
    ev.set_extra("_testbench_llm_requested", True)
    ev.cleanup_temporary_local_files()
    summary = ev.result_summary()
    assert summary["wake"]["woken"] is True
    assert summary["wake"]["at_or_wake"] is True
    assert summary["wake"]["stopped"] is False
    assert summary["wake"]["llm_requested"] is True
    # 已唤醒但无回复 → woken_no_reply
    assert summary["status"] == "no_reply"
    assert summary["reason"] == "woken_no_reply"
    # 有回复 → 无 reason
    assert ev.result_summary(status="ok")["reason"] is None

    # 未唤醒 → not_woken
    ev2 = VirtualMessageEvent.create(
        session_id="vs_2", sender_id="u1", sender_name="用户1", text="hi"
    )
    ev2.cleanup_temporary_local_files()
    s2 = ev2.result_summary()
    assert s2["wake"]["woken"] is False
    assert s2["wake"]["at_or_wake"] is False
    assert s2["reason"] == "not_woken"


def test_runner_sender_precedence(tmp_path):
    """发送者优先级：请求级 > 绑定群聊默认成员 > 手动 sender > 默认。"""
    identity_store = IdentityStore(data_dir=tmp_path)
    chat_group_store = ChatGroupStore(data_dir=tmp_path)
    member = identity_store.create_identity("群友A", "member_a", "群友A")
    identity_store.create_identity("群友B", "member_b", "群友B")
    cg = chat_group_store.create_chat_group("测试群", [member["id"]])
    runner = VirtualTestRunner(
        FakeContext(),
        identity_store=identity_store,
        chat_group_store=chat_group_store,
    )
    session = {
        "id": "vs_1",
        "sender_id": "manual",
        "sender_name": "手动发送者",
        "chat_group_id": cg["id"],
    }
    # 请求级 > 绑定群聊默认成员；仅给 sender_id 时昵称回退 sender_id
    assert runner._resolve_sender(session, "req", "请求者") == ("req", "请求者")
    assert runner._resolve_sender(session, "req", None) == ("req", "req")
    # 绑定群聊默认成员（成员池首个身份）
    assert runner._resolve_sender(session, None, None) == ("member_a", "群友A")
    # 未绑定群聊 → 手动 sender
    session2 = dict(session)
    session2["chat_group_id"] = None
    assert runner._resolve_sender(session2, None, None) == ("manual", "手动发送者")
    # 全缺 → 默认
    session3 = {"id": "vs_3"}
    assert runner._resolve_sender(session3, None, None) == ("testbench", "测试台")


@pytest.mark.asyncio
async def test_runner_writes_stream(tmp_path):
    """start → 流含 user 消息；pipeline 完成后流含 bot 回复并回填 reply_status。"""
    stream_store = StreamStore(data_dir=tmp_path)
    queue = asyncio.Queue()
    runner = VirtualTestRunner(FakeContext(queue), stream_store=stream_store)
    session = make_session(1)
    session["message_type"] = "GroupMessage"

    async def handler(event):
        await event.send(MessageChain().message("回复"))
        event.cleanup_temporary_local_files()

    task = asyncio.create_task(consume(queue, handler))
    try:
        test_id = await runner.start(
            sessions=[session],
            text="群聊消息",
            sender_id="xiaoming",
            sender_name="小明",
            auto_at=True,
        )
        # start 后（pipeline 完成前）user 消息已写入流
        msgs = await stream_store.read_stream("vs_1")
        assert len(msgs) == 1
        assert msgs[0]["role"] == "user"
        assert msgs[0]["sender_id"] == "xiaoming"
        assert msgs[0]["sender_name"] == "小明"
        assert msgs[0]["at_bot"] is True  # 群聊 + auto_at
        rec = await wait_run_done(runner, test_id)
    finally:
        task.cancel()
    assert rec["results"][0]["status"] == "ok"
    # 完成后 bot 回复写入流，user 消息回填 reply_status
    msgs = await stream_store.read_stream("vs_1")
    assert len(msgs) == 2
    assert msgs[0]["reply_status"] == "ok"
    assert msgs[1]["role"] == "bot"
    assert msgs[1]["sender_id"] == "virtual_bot"
    assert msgs[1]["text"] == "回复"


@pytest.mark.asyncio
async def test_runner_stream_write_failure_still_completes():
    """流回填失败不阻断结果收集：完成判定恒执行，运行不挂死。

    修复前 _await_event 里流写入无 try/except：update_reply 抛错会让任务在
    完成判定之前死亡，done/test_done 不触发，运行挂到 STALE_RUN_TIMEOUT。
    """
    queue = asyncio.Queue()
    runner = VirtualTestRunner(
        FakeContext(queue), stream_store=_FailingReplyStreamStore()
    )
    session = make_session(1)

    async def handler(event):
        await event.send(MessageChain().message("回复"))
        event.cleanup_temporary_local_files()

    task = asyncio.create_task(consume(queue, handler))
    try:
        test_id = await runner.start(sessions=[session], text="hello")
        rec = await wait_run_done(runner, test_id)
    finally:
        task.cancel()
    assert rec["done"] is True
    assert rec["results"][0]["status"] == "ok"


@pytest.mark.asyncio
async def test_runner_auto_at_request_level():
    """auto@ 是请求级选项：默认开启，仅群聊消息生效，私聊恒不生效。"""
    queue = asyncio.Queue()
    runner = VirtualTestRunner(FakeContext(queue))
    captured = []

    async def handler(event):
        captured.append(event.auto_at)
        event.cleanup_temporary_local_files()

    task = asyncio.create_task(consume(queue, handler))
    try:
        # 默认开启：群聊消息带 auto_at，私聊消息不带
        group = make_session(1)
        group["message_type"] = "GroupMessage"
        tid = await runner.start(sessions=[group, make_session(2)], text="hi")
        await wait_run_done(runner, tid)
        assert captured == [True, False]

        # 显式关闭：群聊消息也不带
        captured.clear()
        tid2 = await runner.start(sessions=[group], text="hi", auto_at=False)
        await wait_run_done(runner, tid2)
        assert captured == [False]
    finally:
        task.cancel()


def test_virtual_event_role_admin():
    """虚拟事件按 is_admin 设置 event.role（基类默认 member）。"""
    ev = VirtualMessageEvent.create(
        session_id="vs_1", sender_id="u1", sender_name="用户1", text="hi"
    )
    assert ev.role == "member"
    assert ev.is_admin() is False
    admin = VirtualMessageEvent.create(
        session_id="vs_1",
        sender_id="root",
        sender_name="管理员",
        text="hi",
        is_admin=True,
    )
    assert admin.role == "admin"
    assert admin.is_admin() is True


def test_runner_resolve_role(tmp_path):
    """发送者角色解析：命中管理员身份 → admin，否则 member；无身份库恒 member。"""
    identity_store = IdentityStore(data_dir=tmp_path)
    identity_store.create_identity("管理员", "admin_1", is_admin=True)
    identity_store.create_identity("普通成员", "member_1")
    runner = VirtualTestRunner(FakeContext(), identity_store=identity_store)
    assert runner._resolve_role("admin_1") == "admin"
    assert runner._resolve_role("member_1") == "member"
    assert runner._resolve_role("unknown") == "member"
    # 无身份库 → 恒 member
    runner3 = VirtualTestRunner(FakeContext())
    assert runner3._resolve_role("admin_1") == "member"


@pytest.mark.asyncio
async def test_runner_start_sets_event_role(tmp_path):
    """start() 构造的事件按发送身份自动设置 role（队列捕获验证）。"""
    identity_store = IdentityStore(data_dir=tmp_path)
    identity_store.create_identity("管理员", "admin_1", is_admin=True)
    queue = asyncio.Queue()
    runner = VirtualTestRunner(FakeContext(queue), identity_store=identity_store)
    captured = []

    async def handler(event):
        captured.append(event.role)
        event.cleanup_temporary_local_files()

    task = asyncio.create_task(consume(queue, handler))
    try:
        s1 = make_session(1)
        s1["sender_id"] = "admin_1"  # 命中管理员身份
        s2 = make_session(2)  # 默认 testbench → 普通成员
        tid = await runner.start(sessions=[s1, s2], text="hi")
        await wait_run_done(runner, tid)
        assert captured == ["admin", "member"]
    finally:
        task.cancel()


def test_runner_assertions_skip_llm_rules():
    """机械断言路径跳过 LLM 规则（LLM 规则由评审阶段评估）。"""
    res = VirtualTestRunner._evaluate_assertions(
        [
            {"type": "contains", "value": "好"},
            {"kind": "llm", "profile_id": "rp_1", "context": "reply"},
        ],
        "很好",
    )
    assert res["pass"] is True
    # 只有 LLM 规则 → 无机械断言可评 → None（结果摘要不出现 assertion 键）
    assert (
        VirtualTestRunner._evaluate_assertions(
            [{"kind": "llm", "profile_id": "rp_1"}], "任意"
        )
        is None
    )


@pytest.mark.asyncio
async def test_runner_late_send_detection():
    """开启检测窗口时，pipeline 结束后窗口内到达的异步补发被标记警告。"""
    queue = asyncio.Queue()
    runner = VirtualTestRunner(FakeContext(queue), late_send_detect_window=0.3)

    async def handler(event):
        await event.send(MessageChain().message("先到的回复"))

        async def late():
            await asyncio.sleep(0.1)
            await event.send(MessageChain().message("异步补发"))

        asyncio.create_task(late())
        event.cleanup_temporary_local_files()

    task = asyncio.create_task(consume(queue, handler))
    try:
        test_id = await runner.start(sessions=[make_session(1)], text="hi")
        rec = await wait_run_done(runner, test_id)
    finally:
        task.cancel()
    assert rec["results"][0]["reply"] == "先到的回复"  # 补发不计入结果
    assert rec["results"][0]["warning"].startswith("pipeline 结束后又有 1 条回复到达")


@pytest.mark.asyncio
async def test_runner_late_send_no_detection_default():
    """默认窗口 0：行为与旧版一致，不睡眠、不产生警告。"""
    queue = asyncio.Queue()
    runner = VirtualTestRunner(FakeContext(queue))

    async def handler(event):
        await event.send(MessageChain().message("ok"))
        event.cleanup_temporary_local_files()

    task = asyncio.create_task(consume(queue, handler))
    try:
        test_id = await runner.start(sessions=[make_session(1)], text="hi")
        rec = await wait_run_done(runner, test_id)
    finally:
        task.cancel()
    assert "warning" not in rec["results"][0]


@pytest.mark.asyncio
async def test_runner_start_warnings():
    """start 的 warnings 参数随 status() 返回；缺省为 []。"""
    queue = asyncio.Queue()
    runner = VirtualTestRunner(FakeContext(queue))
    warning = {
        "kind": "cron_targets_virtual_session",
        "job_id": "j1",
        "job_name": "问候",
        "message": "测试",
    }
    test_id = await runner.start(
        sessions=[make_session(1)], text="hi", warnings=[warning]
    )
    assert runner.status(test_id)["warnings"] == [warning]
    test_id2 = await runner.start(sessions=[make_session(1)], text="hi")
    assert runner.status(test_id2)["warnings"] == []

    # 收尾：放行悬挂的 _await_event
    while not queue.empty():
        queue.get_nowait().cleanup_temporary_local_files()
    await asyncio.sleep(0.01)
