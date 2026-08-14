"""测试集测试：TestsetStore 清洗与持久化、TestsetRunner 段驱动（顺序/批量/
超时/中止/评审时机）、测试集报告生成。"""

import asyncio
import json
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

import astrbot_plugin_testbench.core.runner as runner_mod  # noqa: E402
import astrbot_plugin_testbench.core.testset_runner as tsr_mod  # noqa: E402
import astrbot_plugin_testbench.main as main_mod  # noqa: E402
import astrbot_plugin_testbench.store.group_store as gs_mod  # noqa: E402
import astrbot_plugin_testbench.store.report_store as rps_mod  # noqa: E402
import astrbot_plugin_testbench.store.reviewer_store as rvs_mod  # noqa: E402
import astrbot_plugin_testbench.store.testset_store as tss_mod  # noqa: E402
from astrbot.api.event import MessageChain  # noqa: E402

ReportStore = rps_mod.ReportStore
ReviewerStore = rvs_mod.ReviewerStore
TestsetRunner = tsr_mod.TestsetRunner
TestsetStore = tss_mod.TestsetStore
VirtualGroupManager = gs_mod.VirtualGroupManager
VirtualTestRunner = runner_mod.VirtualTestRunner
umo_of = gs_mod.umo_of

from fakes import (  # noqa: E402
    FakeContext,
    FakeCronManager,
    FakeLLMProvider,
    RecordingBus,
    _cron_job,
    _make_testset,
    consume,
    make_session,
    wait_testset_done,
    wait_testset_warnings,
)


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
    assert reloaded.get_testset(ts["id"])["messages"][0]["rules"] == [
        {"type": "contains", "value": "在"},
    ]

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
    assert ts["messages"][0] == {"text": "去空白", "rules": []}  # 非 dict rule → 空列表


def test_testset_store_message_auto_at(tmp_path):
    """消息级 auto@ 归一：bool 保留、非 bool 丢弃、缺省不落字段（发送时按 True）。"""
    store = TestsetStore(data_dir=tmp_path)
    ts = store.create_testset(
        "A",
        [
            {"text": "a", "auto_at": False},
            {"text": "b", "auto_at": True},
            {"text": "c", "auto_at": "yes"},
            {"text": "d"},
        ],
    )
    msgs = ts["messages"]
    assert msgs[0]["auto_at"] is False
    assert msgs[1]["auto_at"] is True
    assert "auto_at" not in msgs[2]  # 非 bool 丢弃
    assert "auto_at" not in msgs[3]  # 缺省不落字段（发送时按 True）


def test_normalize_messages_ignores_non_dict_items():
    """_normalize_messages 对非 dict 消息项直接跳过（数据损坏可见但不崩溃）。"""
    out = TestsetStore._normalize_messages(
        [{"text": "a"}, "junk", 42, None, {"text": "  b  "}]
    )
    assert out == [
        {"text": "a", "rules": []},
        {"text": "b", "rules": []},
    ]


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
async def test_testset_runner_message_auto_at():
    """测试集消息级 auto@ 透传：显式关闭的消息不带，缺省的消息按开启发送。"""
    queue = asyncio.Queue()
    context = FakeContext(queue)
    tsr = TestsetRunner(context, VirtualTestRunner(context))
    auto_ats: list[bool] = []

    async def handler(event):
        auto_ats.append(event.auto_at)
        event.cleanup_temporary_local_files()

    task = asyncio.create_task(consume(queue, handler))
    try:
        session = make_session(1)
        session["message_type"] = "GroupMessage"  # 群聊消息 auto@ 才生效
        testset = {
            "id": "ts_auto",
            "name": "自动@",
            "created_at": 0,
            "messages": [
                {"text": "m1", "rules": [], "auto_at": False},
                {"text": "m2", "rules": []},
            ],
            "batch_ranges": [],
        }
        run_id = tsr.start_run(testset, [session])
        rec = await wait_testset_done(tsr, run_id)
    finally:
        task.cancel()
    assert rec["status"] == "done"
    assert auto_ats == [False, True]  # 显式关闭 vs 缺省开启


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
async def test_testset_runner_step_failure_skips_review(monkeypatch):
    """TB-14: 单步段失败（置 error）后评审不执行——评审只针对完整执行的数据。

    语义锚定：`_drive_segments` 在单步失败后中止后续并跳过 `_review_phase`
    （批量段内错误不中止、评审照常，只评估 done 步骤）；失败运行不带
    final_verdicts、不经过 reviewing 阶段。
    """
    monkeypatch.setattr(tsr_mod, "TESTSET_STEP_TIMEOUT", 0.05)
    queue = asyncio.Queue()
    context = FakeContext(queue)
    tsr = TestsetRunner(context, VirtualTestRunner(context))

    # 步骤带规则 + final_rules：若评审执行会产出 verdicts，失败跳过则没有
    testset = _make_testset("ts_6", "失败跳过评审", [("m1", None), ("m2", None)])
    testset["messages"][0]["rules"] = [{"type": "contains", "value": "x"}]
    testset["final_rules"] = [
        {"rule": {"type": "contains", "value": "x"}, "scope": "all"}
    ]
    run_id = tsr.start_run(testset, [make_session(1)])
    rec = await wait_testset_done(tsr, run_id)
    # 收尾：放行悬挂的 _await_event
    while not queue.empty():
        queue.get_nowait().cleanup_temporary_local_files()
    await asyncio.sleep(0.01)

    assert rec["status"] == "error"
    assert rec["steps"][0]["status"] == "error"
    assert not rec.get("reviewing")
    assert not rec.get("final_verdicts")


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


def test_testset_runner_step_sender_resolution():
    """步骤发送者解析：single+快照恒用测试集身份；single 无快照回退消息级
    sender；pool 按身份 id / sender_id 匹配池成员；未命中回默认身份。"""
    identity = {
        "id": "id_a",
        "name": "管理员",
        "sender_id": "root",
        "sender_name": "管理员",
        "is_admin": True,
    }
    pool = {
        "name": "测试群",
        "members": [
            {
                "id": "id_b",
                "name": "群友",
                "sender_id": "member_1",
                "sender_name": "群友",
                "is_admin": False,
            },
            {
                "id": "id_c",
                "name": "管理员2",
                "sender_id": "root2",
                "sender_name": "管理员2",
                "is_admin": True,
            },
        ],
    }
    step = TestsetRunner._step_sender
    # single + 快照：恒用测试集身份（消息级 sender 忽略），is_admin 显式生效
    assert step({"text": "m", "sender_id": "其他"}, "single", identity, None) == (
        "root",
        "管理员",
        True,
    )
    # single 无快照：回退消息级 sender，is_admin 由 runner 按身份库解析（None）
    assert step(
        {"text": "m", "sender_id": "u1", "sender_name": "用户"}, "single", None, None
    ) == ("u1", "用户", None)
    # pool：按身份 id 引用命中成员
    assert step({"text": "m", "sender_id": "id_b"}, "pool", None, pool) == (
        "member_1",
        "群友",
        False,
    )
    # pool：按 sender_id 字符串匹配（旧数据保险）
    assert step({"text": "m", "sender_id": "root2"}, "pool", None, pool) == (
        "root2",
        "管理员2",
        True,
    )
    # pool：未引用 / 未命中 → 默认身份（全部 None）
    assert step({"text": "m"}, "pool", None, pool) == (None, None, None)
    assert step({"text": "m", "sender_id": "nobody"}, "pool", None, pool) == (
        None,
        None,
        None,
    )


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


@pytest.mark.asyncio
async def test_testset_runner_drive_exception_publishes_terminal_event(monkeypatch):
    """_drive 内部异常：run 置 error 且必须广播终态快照，前端才不会停在 running。"""
    bus = RecordingBus()

    async def boom(self, run):
        raise RuntimeError("驱动段异常")

    monkeypatch.setattr(TestsetRunner, "_drive_segments", boom)
    tsr = TestsetRunner(FakeContext(), VirtualTestRunner(FakeContext()), event_bus=bus)
    testset = _make_testset("ts_err", "内部异常", [("问", None)])
    run_id = tsr.start_run(testset, [make_session(1)])
    rec = await wait_testset_done(tsr, run_id)

    assert rec["status"] == "error"
    assert rec["error"] == "运行器内部异常"
    assert rec["finished_at"] is not None
    # 终态快照必须已广播（旧实现缺失：页面会一直停在 running）
    testset_events = [e for e in bus.events if e["type"] == "testset"]
    assert testset_events, "未发布 testset 运行快照"
    assert testset_events[-1]["run"]["status"] == "error"
    assert testset_events[-1]["run"]["error"] == "运行器内部异常"
    # 步骤保持在 pending（异常发生在段驱动前），但 run 已落终态
    assert testset_events[-1]["run"]["steps"][0]["status"] == "pending"


def test_testset_store_passthrough_combo_rules(tmp_path):
    """store 透传组合节点：any / not 组合 dict 原样保留（清洗不做键白名单）。"""
    store = TestsetStore(data_dir=tmp_path)
    ts = store.create_testset(
        "组合",
        [
            {
                "text": "a",
                "rules": [
                    {"op": "any", "rules": [{"type": "contains", "value": "x"}]},
                    {"op": "not", "rule": {"type": "non_empty"}},
                ],
            }
        ],
    )
    rules = ts["messages"][0]["rules"]
    assert rules == [
        {"op": "any", "rules": [{"type": "contains", "value": "x"}]},
        {"op": "not", "rule": {"type": "non_empty"}},
    ]
    reloaded = TestsetStore(data_dir=tmp_path)
    assert reloaded.get_testset(ts["id"])["messages"][0]["rules"] == rules


def test_testset_store_final_rules(tmp_path):
    store = TestsetStore(data_dir=tmp_path)
    ts = store.create_testset(
        "终局",
        [{"text": "a"}, {"text": "b"}],
        final_rules=[
            {"rule": {"type": "contains", "value": "x"}, "scope": "all"},
            {
                "rule": {"kind": "llm", "profile_id": "rp_1", "context": "record"},
                "scope": {"from": 0, "to": 1},
            },
            {"rule": "不是字典"},  # 整项丢弃
            {
                "rule": {"type": "contains", "value": "y"},
                "scope": {"from": True, "to": 1},
            },  # bool 边界 → scope 回退 all
        ],
    )
    assert len(ts["final_rules"]) == 3
    assert ts["final_rules"][0] == {
        "rule": {"type": "contains", "value": "x"},
        "scope": "all",
    }
    assert ts["final_rules"][1]["scope"] == {"from": 0, "to": 1}
    assert ts["final_rules"][2]["scope"] == "all"

    # 非 list / 缺省 → []
    assert (
        store.create_testset("x", [{"text": "m"}], final_rules="bad")["final_rules"]
        == []
    )
    assert (
        store.create_testset("y", [{"text": "m"}], final_rules=None)["final_rules"]
        == []
    )

    # 更新整体替换 + 持久化 + 旧数据 setdefault
    updated = store.update_testset(ts["id"], "改", [{"text": "a"}], final_rules=[])
    assert updated["final_rules"] == []
    reloaded = TestsetStore(data_dir=tmp_path)
    assert reloaded.get_testset(ts["id"])["final_rules"] == []
    legacy = {"testsets": [{"id": "ts_old", "name": "旧", "messages": []}]}
    (tmp_path / "virtual_session" / "testsets.json").write_text(
        json.dumps(legacy), encoding="utf-8"
    )
    legacy_store = TestsetStore(data_dir=tmp_path)
    assert legacy_store.get_testset("ts_old")["final_rules"] == []


@pytest.mark.asyncio
async def test_testset_runner_review_phase_mechanical():
    """全部步骤完成后统一评审：机械规则 verdicts 写入步骤结果。"""
    queue = asyncio.Queue()
    context = FakeContext(queue)
    tsr = TestsetRunner(context, VirtualTestRunner(context))

    async def handler(event):
        await event.send(MessageChain().message(f"回复 {event.message_str}"))
        event.cleanup_temporary_local_files()

    task = asyncio.create_task(consume(queue, handler))
    try:
        testset = _make_testset(
            "ts_r1", "评审机械", [("问", {"type": "contains", "value": "回复 问"})]
        )
        run_id = tsr.start_run(testset, [make_session(1)])
        rec = await wait_testset_done(tsr, run_id)
    finally:
        task.cancel()
    assert rec["status"] == "done"
    assert rec["reviewing"] is False
    assert rec["final_verdicts"] == []
    assert rec["steps"][0]["results"][0]["verdicts"] == [
        {
            "rule_index": 0,
            "status": "ok",
            "pass": True,
            "metrics": [{"key": "pass", "type": "bool", "value": True}],
            "detail": "回复包含 '回复 问'",
            "raw": None,
            "context_text": None,
            "profile_id": None,
        }
    ]


@pytest.mark.asyncio
async def test_testset_runner_review_phase_final_rules():
    """start_run 须携带 final_rules：评审阶段产出 run 级 final_verdicts。

    回归：start_run 构造运行记录时曾丢弃 final_rules（`_review_phase` 恒读到
    空列表，最终断言在真实流程中从不评估）——所有端到端运行测试原先都用
    final_rules=[] 掩盖了该缺陷。
    """
    queue = asyncio.Queue()
    context = FakeContext(queue)
    tsr = TestsetRunner(context, VirtualTestRunner(context))

    async def handler(event):
        await event.send(MessageChain().message(f"回复 {event.message_str}"))
        event.cleanup_temporary_local_files()

    task = asyncio.create_task(consume(queue, handler))
    try:
        testset = _make_testset(
            "ts_fr", "最终断言", [("问", {"type": "contains", "value": "回复 问"})]
        )
        testset["final_rules"] = [
            {
                "rule": {"type": "contains", "value": "回复 问"},
                "scope": {"from": 0, "to": 0},
            }
        ]
        run_id = tsr.start_run(testset, [make_session(1)])
        rec = await wait_testset_done(tsr, run_id)
    finally:
        task.cancel()
    assert rec["status"] == "done"
    assert rec["reviewing"] is False
    assert rec["steps"][0]["results"][0]["verdicts"][0]["pass"] is True
    assert len(rec["final_verdicts"]) == 1
    fv = rec["final_verdicts"][0]
    assert fv["rule_index"] == 0
    assert fv["scope"] == {"from": 0, "to": 0}
    assert len(fv["results"]) == 1
    assert fv["results"][0]["session_id"] == "vs_1"
    assert fv["results"][0]["verdict"]["status"] == "ok"
    assert fv["results"][0]["verdict"]["pass"] is True


@pytest.mark.asyncio
async def test_testset_runner_review_phase_llm(tmp_path):
    """评审阶段调用评审 LLM：verdicts 按 profile 契约校验并派生 pass。"""
    provider = FakeLLMProvider("prov_r", responses=['{"score": 90}'])
    queue = asyncio.Queue()
    context = FakeContext(queue, providers=[provider])
    reviewer_store = ReviewerStore(data_dir=tmp_path)
    profile = reviewer_store.create_profile(
        {
            "name": "评审",
            "provider_id": "prov_r",
            "model": "review-model",
            "system_prompt": "评审 {{metrics}}",
            "metrics": [{"key": "score", "type": "number", "pass_threshold": 80}],
        }
    )
    tsr = TestsetRunner(
        context, VirtualTestRunner(context), reviewer_store=reviewer_store
    )

    async def handler(event):
        await event.send(MessageChain().message("回答"))
        event.cleanup_temporary_local_files()

    task = asyncio.create_task(consume(queue, handler))
    try:
        testset = {
            "id": "ts_llm",
            "name": "评审",
            "created_at": 0,
            "messages": [
                {
                    "text": "问",
                    "rules": [
                        {
                            "kind": "llm",
                            "profile_id": profile["id"],
                            "context": "reply",
                        }
                    ],
                }
            ],
            "batch_ranges": [],
            "final_rules": [],
        }
        run_id = tsr.start_run(testset, [make_session(1)])
        rec = await wait_testset_done(tsr, run_id)
    finally:
        task.cancel()
    assert rec["status"] == "done"
    assert rec["reviewing"] is False
    verdict = rec["steps"][0]["results"][0]["verdicts"][0]
    assert verdict["status"] == "ok" and verdict["pass"] is True
    assert verdict["metrics"] == [{"key": "score", "type": "number", "value": 90}]
    # 评审上下文为单轮结构化材料（context=reply：输入 + 回复，带身份标注）
    prompt = provider.calls[0]["prompt"]
    assert "【输入 · user（测试台）】\n问" in prompt
    assert "【输出 · agent（virtual_bot）】\n回答" in prompt
    # 机械断言路径不产生 assertion 键（规则全是 LLM 类）
    assert "assertion" not in rec["steps"][0]["results"][0]


@pytest.mark.asyncio
async def test_testset_runner_review_failure_marks_error(monkeypatch):
    """评审编排异常 → run error「评审失败」（终态即解锁）。"""
    queue = asyncio.Queue()
    context = FakeContext(queue)
    tsr = TestsetRunner(context, VirtualTestRunner(context))

    async def handler(event):
        await event.send(MessageChain().message("ok"))
        event.cleanup_temporary_local_files()

    async def boom(self, steps, final_rules, sessions):
        raise RuntimeError("评审器崩溃")

    monkeypatch.setattr(tsr_mod.Assessor, "assess", boom)
    task = asyncio.create_task(consume(queue, handler))
    try:
        testset = _make_testset(
            "ts_fail", "评审失败", [("问", {"type": "contains", "value": "ok"})]
        )
        run_id = tsr.start_run(testset, [make_session(1)])
        rec = await wait_testset_done(tsr, run_id)
    finally:
        task.cancel()
    assert rec["status"] == "error"
    assert "评审失败" in rec["error"]
    assert rec["reviewing"] is False


@pytest.mark.asyncio
async def test_testset_runner_review_skipped_without_rules():
    """无消息规则且无 final_rules → 跳过评审阶段（快速路径）。"""
    queue = asyncio.Queue()
    context = FakeContext(queue)
    tsr = TestsetRunner(context, VirtualTestRunner(context))

    async def handler(event):
        await event.send(MessageChain().message("ok"))
        event.cleanup_temporary_local_files()

    task = asyncio.create_task(consume(queue, handler))
    try:
        testset = _make_testset("ts_nr", "无规则", [("问", None)])
        run_id = tsr.start_run(testset, [make_session(1)])
        rec = await wait_testset_done(tsr, run_id)
    finally:
        task.cancel()
    assert rec["status"] == "done"
    assert rec["reviewing"] is False
    assert rec["final_verdicts"] == []
    assert "verdicts" not in rec["steps"][0]["results"][0]


@pytest.mark.asyncio
async def test_testset_runner_report_generation(tmp_path):
    """report_enabled 的运行终态产出持久化报告（含聚合数据）；缺省不产出。"""
    queue = asyncio.Queue()
    context = FakeContext(queue)
    report_store = ReportStore(data_dir=tmp_path)
    tsr = TestsetRunner(context, VirtualTestRunner(context), report_store=report_store)

    async def handler(event):
        await event.send(MessageChain().message(f"回复 {event.message_str}"))
        event.cleanup_temporary_local_files()

    task = asyncio.create_task(consume(queue, handler))
    try:
        testset = _make_testset(
            "ts_rpt", "报告测试", [("问", {"type": "contains", "value": "回复 问"})]
        )
        testset["report_enabled"] = True
        run_id = tsr.start_run(testset, [make_session(1)])
        rec = await wait_testset_done(tsr, run_id)
    finally:
        task.cancel()
    assert rec["status"] == "done"
    assert rec["report_id"] is not None  # 已产出报告
    reports = report_store.list_reports(testset_id="ts_rpt")
    assert len(reports) == 1
    data = reports[0]["data"]
    assert data["run_id"] == run_id
    assert data["testset_name"] == "报告测试"
    assert data["status"] == "done"
    # 机械断言 → bool 指标「pass」进入聚合
    assert data["metrics_summary"]["metrics"]["pass"] == {
        "type": "bool",
        "pass": 1,
        "total": 1,
        "rate": 1.0,
    }
    assert data["metrics_summary"]["review_failures"] == 0

    # report_enabled 缺省 False → 不产出报告
    queue2 = asyncio.Queue()
    tsr2 = TestsetRunner(
        FakeContext(queue2),
        VirtualTestRunner(FakeContext(queue2)),
        report_store=report_store,
    )

    async def handler2(event):
        await event.send(MessageChain().message("ok"))
        event.cleanup_temporary_local_files()

    task2 = asyncio.create_task(consume(queue2, handler2))
    try:
        testset2 = _make_testset("ts_norpt", "无报告", [("问", None)])
        run2 = tsr2.start_run(testset2, [make_session(1)])
        rec2 = await wait_testset_done(tsr2, run2)
    finally:
        task2.cancel()
    assert rec2["status"] == "done"
    assert rec2["report_id"] is None
    assert report_store.list_reports(testset_id="ts_norpt") == []


@pytest.mark.asyncio
async def test_testset_run_attaches_cron_warnings(tmp_path):
    """测试集运行：后台探测任务把针对虚拟会话的 cron 任务附到运行记录。"""
    queue = asyncio.Queue()
    context = FakeContext(queue)
    plugin = main_mod.VirtualSessionPlugin(context)
    plugin.group_mgr = VirtualGroupManager(data_dir=tmp_path)
    plugin.testset_store = TestsetStore(data_dir=tmp_path)
    group = plugin.group_mgr.create_group("组A", count=1)
    sid = group["sessions"][0]["id"]
    umo = umo_of(plugin.group_mgr.effective_many([sid])[0])
    context.cron_manager = FakeCronManager(
        [_cron_job("j1", "active_agent", {"session": umo})]
    )
    ts = plugin.testset_store.create_testset("T", [{"text": "m1"}])

    run_id = plugin.testset_runner.start_run(ts, plugin.group_mgr.effective_many([sid]))
    warnings = await wait_testset_warnings(plugin.testset_runner, run_id)
    assert warnings[0]["job_id"] == "j1"

    # 收尾：放行悬挂的 _await_event
    while not queue.empty():
        queue.get_nowait().cleanup_temporary_local_files()
    await asyncio.sleep(0.01)
