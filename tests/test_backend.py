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

import astrbot_plugin_testbench.core.conf_routes as cr_mod  # noqa: E402
import astrbot_plugin_testbench.core.conf_tools as ct_mod  # noqa: E402
import astrbot_plugin_testbench.core.cron_probe as cp_mod  # noqa: E402
import astrbot_plugin_testbench.core.event_bus as eb_mod  # noqa: E402
import astrbot_plugin_testbench.core.runner as runner_mod  # noqa: E402
import astrbot_plugin_testbench.core.testset_runner as tsr_mod  # noqa: E402
import astrbot_plugin_testbench.core.virtual_event as ve_mod  # noqa: E402
import astrbot_plugin_testbench.eval.assessor as assr_mod  # noqa: E402
import astrbot_plugin_testbench.eval.mechanical as asrt_mod  # noqa: E402
import astrbot_plugin_testbench.eval.reporting as rpt_mod  # noqa: E402
import astrbot_plugin_testbench.eval.reviewer as rev_mod  # noqa: E402
import astrbot_plugin_testbench.history_ops as hops_mod  # noqa: E402
import astrbot_plugin_testbench.main as main_mod  # noqa: E402
import astrbot_plugin_testbench.stats as stats_mod  # noqa: E402
import astrbot_plugin_testbench.store.group_store as gs_mod  # noqa: E402
import astrbot_plugin_testbench.store.identity_store as ids_mod  # noqa: E402
import astrbot_plugin_testbench.store.report_store as rps_mod  # noqa: E402
import astrbot_plugin_testbench.store.reviewer_store as rvs_mod  # noqa: E402
import astrbot_plugin_testbench.store.stream_store as stm_mod  # noqa: E402
import astrbot_plugin_testbench.store.testset_store as tss_mod  # noqa: E402
from astrbot.api.event import MessageChain  # noqa: E402
from astrbot.api.message_components import At, Plain  # noqa: E402
from astrbot.api.platform import MessageType  # noqa: E402
from astrbot.api.web import PluginRequest, bind_request_context  # noqa: E402
from starlette.requests import Request  # noqa: E402

EventBus = eb_mod.EventBus
conf_tool_info = ct_mod.conf_tool_info
conf_has_callable_tools = ct_mod.conf_has_callable_tools
cron_job_warning = cp_mod.cron_job_warning
target_sets = cp_mod.target_sets
collect_cron_warnings = cp_mod.collect_cron_warnings
VirtualMessageEvent = ve_mod.VirtualMessageEvent
VirtualGroupManager = gs_mod.VirtualGroupManager
VirtualTestRunner = runner_mod.VirtualTestRunner
TestsetStore = tss_mod.TestsetStore
TestsetRunner = tsr_mod.TestsetRunner
IdentityStore = ids_mod.IdentityStore
ChatGroupStore = ids_mod.ChatGroupStore
ReviewerStore = rvs_mod.ReviewerStore
ReportStore = rps_mod.ReportStore
StreamStore = stm_mod.StreamStore
build_metrics_summary = rpt_mod.build_metrics_summary
build_report_data = rpt_mod.build_report_data
MAX_STREAM_MESSAGES = stm_mod.MAX_STREAM_MESSAGES
evaluate_rule = asrt_mod.evaluate_rule
duration_stats = stats_mod.duration_stats
umo_of = gs_mod.umo_of
Assessor = assr_mod.Assessor
build_input_text = assr_mod.build_input_text
format_turn = assr_mod.format_turn
format_record = assr_mod.format_record
validate_profile = rev_mod.validate_profile
metrics_contract_description = rev_mod.metrics_contract_description
expand_prompt = rev_mod.expand_prompt
derive_pass = rev_mod.derive_pass
validate_metrics = rev_mod.validate_metrics
call_reviewer = rev_mod.call_reviewer
retry_llm_verdict = rev_mod.retry_llm_verdict
mechanical_verdict = rev_mod.mechanical_verdict
llm_verdict = rev_mod.llm_verdict


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


class FakeLLMProvider:
    """模拟评审 Provider：text_chat 返回可配置的 completion_text 并记录调用。"""

    def __init__(
        self,
        provider_id: str,
        responses: list[str] | None = None,
        raise_on_call: bool = False,
    ) -> None:
        self.provider_config = {"id": provider_id}
        self._responses = list(responses or [])
        self._raise = raise_on_call
        self.calls: list[dict] = []  # 每次调用的参数快照

    async def text_chat(self, prompt="", system_prompt=None, model=None, **kwargs):
        self.calls.append(
            {"prompt": prompt, "system_prompt": system_prompt, "model": model}
        )
        if self._raise:
            raise RuntimeError("评审 LLM 调用失败")
        text = self._responses.pop(0) if self._responses else ""
        return SimpleNamespace(completion_text=text)


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

    def get_provider_by_id(self, provider_id: str):
        """按 provider_config["id"] 查找（镜像 context.py 的 get_provider_by_id）。"""
        for p in self._providers:
            cfg = getattr(p, "provider_config", None)
            if isinstance(cfg, dict) and cfg.get("id") == provider_id:
                return p
        return None

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


@pytest.mark.asyncio
async def test_conf_route_precedence_over_broad_fallback():
    """插件精确路由须优先于用户已配的「全部会话」兜底（真实 UCR 端到端）。

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
        await asyncio.sleep(0.05)
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
    """轮询测试集运行状态直到终态（模拟前端轮询）。

    status 变为非 running 时驱动任务的 finally 可能仍未执行完（报告生成 /
    report_id 写入发生在 finally 内），故还需等待驱动任务本身完成，轮询者
    才能观察到最终状态。
    """
    task = tsr._tasks.get(run_id)
    async with asyncio.timeout(max_wait):
        while True:
            rec = tsr.status(run_id)
            if rec and rec["status"] != "running" and (task is None or task.done()):
                return rec
            await asyncio.sleep(0.01)


async def wait_testset_warnings(
    tsr: TestsetRunner, run_id: str, max_wait: float = 2.0
) -> list:
    """轮询测试集运行直到 cron 警告附到运行记录（后台探测任务）。"""
    async with asyncio.timeout(max_wait):
        while True:
            warnings = tsr.status(run_id)["warnings"]
            if warnings:
                return warnings
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
        "messages": [{"text": t, "rules": [r] if r else []} for t, r in texts],
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


# ---------- 消息类型 / 自动@ / 测试身份 / 虚拟群聊 / 消息流 ----------


def test_umo_of_uses_message_type():
    """umo 格式随消息类型变化：FriendMessage 与 GroupMessage 的键不同。"""
    assert umo_of(make_session(1)) == "webchat:FriendMessage:vs_1"
    assert (
        umo_of({"id": "vs_1", "platform_id": "webchat", "message_type": "GroupMessage"})
        == "webchat:GroupMessage:vs_1"
    )
    assert (
        umo_of(
            {"id": "vs_1", "platform_id": "aiocqhttp", "message_type": "GroupMessage"}
        )
        == "aiocqhttp:GroupMessage:vs_1"
    )


def test_effective_resolves_message_type_and_chat_group(tmp_path):
    """message_type / chat_group_id 的三态解析（会话 → 组 → 默认）。

    auto@ 已改为发送时选项（群发栏 / 测试集消息级），不再属于有效配置。
    """
    mgr = VirtualGroupManager(data_dir=tmp_path)
    group = mgr.create_group(
        "群聊组",
        count=1,
        message_type="GroupMessage",
        chat_group_id="cg_1",
    )
    session = group["sessions"][0]
    eff = mgr.effective(group, session)
    assert eff["message_type"] == "GroupMessage"
    assert eff["chat_group_id"] == "cg_1"
    assert "auto_at" not in eff

    # 默认：私聊 + 无绑定
    group2 = mgr.create_group("默认组", count=1)
    eff2 = mgr.effective(group2, group2["sessions"][0])
    assert eff2["message_type"] == "FriendMessage"
    assert eff2["chat_group_id"] is None
    assert "auto_at" not in eff2

    # 会话覆盖组配置；None 恢复继承组
    mgr.update_session(session["id"], message_type="FriendMessage")
    eff3 = mgr.effective(group, session)
    assert eff3["message_type"] == "FriendMessage"
    mgr.update_session(session["id"], message_type=None, chat_group_id=None)
    eff4 = mgr.effective(group, session)
    assert eff4["message_type"] == "GroupMessage"
    assert eff4["chat_group_id"] == "cg_1"


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


# ---------- 身份 / 虚拟群聊 / 消息流 store ----------


def test_identity_store_crud(tmp_path):
    store = IdentityStore(data_dir=tmp_path)
    ident = store.create_identity("小明", "xiaoming", "小明同学")
    assert ident["sender_id"] == "xiaoming"
    assert ident["sender_name"] == "小明同学"

    # 重新加载（新实例）确认持久化
    store2 = IdentityStore(data_dir=tmp_path)
    assert store2.get_identity(ident["id"])["sender_id"] == "xiaoming"

    # sender 缺省回退名称
    ident2 = store2.create_identity("小红")
    assert ident2["sender_id"] == "小红"
    assert ident2["sender_name"] == "小红"

    # 更新；未传字段保持不变
    updated = store2.update_identity(ident["id"], name="小刚", sender_id="xiaogang")
    assert updated["sender_id"] == "xiaogang"
    assert updated["sender_name"] == "小明同学"

    # 空串重置为名称回退
    store2.update_identity(ident["id"], sender_id="")
    assert store2.get_identity(ident["id"])["sender_id"] == "小刚"

    # 删除
    assert store2.delete_identities([ident["id"]]) == 1
    assert store2.get_identity(ident["id"]) is None
    # 更新不存在的身份 → None
    assert store2.update_identity("id_none", name="x") is None


def test_chat_group_store_crud(tmp_path):
    store = ChatGroupStore(data_dir=tmp_path)
    grp = store.create_chat_group("测试群", ["id_a", "id_b"])
    assert grp["member_ids"] == ["id_a", "id_b"]
    # 清洗：非字符串 / 空串 / 重复去除
    grp2 = store.create_chat_group("空群", ["", None, "id_x", "id_x"])
    assert grp2["member_ids"] == ["id_x"]

    # 重新加载（新实例）确认持久化
    store2 = ChatGroupStore(data_dir=tmp_path)
    assert store2.get_chat_group(grp["id"])["name"] == "测试群"

    # 更新成员
    updated = store2.update_chat_group(grp["id"], member_ids=["id_c"])
    assert updated["member_ids"] == ["id_c"]

    # 删除
    assert store2.delete_chat_groups([grp["id"]]) == 1
    assert store2.get_chat_group(grp["id"]) is None


@pytest.mark.asyncio
async def test_stream_store_append_read_clear(tmp_path):
    store = StreamStore(data_dir=tmp_path)
    mid = await store.append(
        "vs_1",
        {
            "role": "user",
            "sender_id": "u1",
            "sender_name": "用户1",
            "text": "hi",
            "at_bot": True,
        },
    )
    msgs = await store.read_stream("vs_1")
    assert len(msgs) == 1
    assert msgs[0]["id"] == mid
    assert msgs[0]["text"] == "hi"
    assert msgs[0]["at_bot"] is True
    # 不存在的会话返回空列表
    assert await store.read_stream("vs_none") == []
    # 更新回复状态
    await store.update_reply("vs_1", mid, "ok")
    assert (await store.read_stream("vs_1"))[0]["reply_status"] == "ok"
    # 清空
    await store.clear("vs_1")
    assert await store.read_stream("vs_1") == []


@pytest.mark.asyncio
async def test_stream_store_truncate_oldest(tmp_path):
    store = StreamStore(data_dir=tmp_path)
    for i in range(MAX_STREAM_MESSAGES + 5):
        await store.append("vs_1", {"role": "user", "text": str(i)})
    msgs = await store.read_stream("vs_1")
    assert len(msgs) == MAX_STREAM_MESSAGES
    assert msgs[0]["text"] == str(5)  # 最旧 5 条被截断
    assert msgs[-1]["text"] == str(MAX_STREAM_MESSAGES + 4)


@pytest.mark.asyncio
async def test_stream_store_delete_sessions(tmp_path):
    store = StreamStore(data_dir=tmp_path)
    await store.append("vs_1", {"role": "user", "text": "a"})
    await store.append("vs_2", {"role": "user", "text": "b"})
    await store.delete_sessions(["vs_1"])
    assert await store.read_stream("vs_1") == []
    assert len(await store.read_stream("vs_2")) == 1


@pytest.mark.asyncio
async def test_stream_store_jsonl_reload(tmp_path):
    """JSONL 追加式：append/reply 逐行追加（无全量包裹），新实例重载重建内存态。"""
    store = StreamStore(data_dir=tmp_path)
    mid = await store.append("vs_1", {"role": "user", "sender_id": "u1", "text": "hi"})
    await store.append("vs_1", {"role": "bot", "text": "hello"})
    await store.update_reply("vs_1", mid, "ok")
    lines = (
        (tmp_path / "virtual_session" / "streams.jsonl")
        .read_text(encoding="utf-8")
        .strip()
        .splitlines()
    )
    assert len(lines) == 3  # 2 append + 1 reply
    assert all(json.loads(line)["op"] in ("append", "reply") for line in lines)
    store2 = StreamStore(data_dir=tmp_path)
    msgs = await store2.read_stream("vs_1")
    assert [m["text"] for m in msgs] == ["hi", "hello"]
    assert msgs[0]["reply_status"] == "ok"


@pytest.mark.asyncio
async def test_stream_store_concurrent_appends(tmp_path):
    """并发 append（重叠发送）经实例锁串行写：无丢行，重载后与内存态一致。"""
    store = StreamStore(data_dir=tmp_path)
    n = 50
    await asyncio.gather(
        *[store.append("vs_1", {"role": "user", "text": str(i)}) for i in range(n)]
    )
    msgs = await store.read_stream("vs_1")
    assert len(msgs) == n
    assert {m["text"] for m in msgs} == {str(i) for i in range(n)}
    store2 = StreamStore(data_dir=tmp_path)
    msgs2 = await store2.read_stream("vs_1")
    assert len(msgs2) == n
    assert {m["text"] for m in msgs2} == {str(i) for i in range(n)}


@pytest.mark.asyncio
async def test_stream_store_compaction(tmp_path, monkeypatch):
    """日志行数超阈值后 append 改为全量重写：日志有界，重载后内容正确。"""
    monkeypatch.setattr(stm_mod, "_COMPACT_LINES", 5)
    store = StreamStore(data_dir=tmp_path)
    for i in range(12):
        mid = await store.append("vs_1", {"role": "user", "text": str(i)})
        await store.update_reply("vs_1", mid, "ok")
    lines = (
        (tmp_path / "virtual_session" / "streams.jsonl")
        .read_text(encoding="utf-8")
        .strip()
        .splitlines()
    )
    # 不压缩时 12 append + 12 reply = 24 行；压缩后日志被重写收敛
    assert len(lines) < 24
    store2 = StreamStore(data_dir=tmp_path)
    msgs = await store2.read_stream("vs_1")
    assert len(msgs) == 12
    assert all(m["reply_status"] == "ok" for m in msgs)


@pytest.mark.asyncio
async def test_group_store_concurrent_write(tmp_path):
    """非流 store 并发写经 write（实例锁内线程化）：无丢失更新。"""
    mgr = VirtualGroupManager(data_dir=tmp_path)

    async def make(i: int) -> str:
        group = await mgr.write(mgr.create_group, f"组{i}", count=1)
        return group["id"]

    ids = await asyncio.gather(*[make(i) for i in range(20)])
    assert len(ids) == 20
    assert len(mgr.list_groups()) == 20
    mgr2 = VirtualGroupManager(data_dir=tmp_path)
    assert len(mgr2.list_groups()) == 20
    assert {g["name"] for g in mgr2.list_groups()} == {f"组{i}" for i in range(20)}


@pytest.mark.asyncio
async def test_identity_store_concurrent_write(tmp_path):
    """_ListStore 并发写经 write 串行：不丢身份，重载后一致。"""
    store = IdentityStore(data_dir=tmp_path)

    async def make(i: int) -> dict:
        return await store.write(store.create_identity, f"身份{i}")

    await asyncio.gather(*[make(i) for i in range(20)])
    assert len(store.list_identities()) == 20
    store2 = IdentityStore(data_dir=tmp_path)
    assert len(store2.list_identities()) == 20


# ---------- 运行器：发送者优先级与消息流写入 ----------


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


# ---------- 工具安全警告与身份管理员 ----------


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


def test_identity_is_admin_crud(tmp_path):
    """身份 is_admin：创建默认 False，可显式 True；更新显式 false 生效、未传不变。"""
    store = IdentityStore(data_dir=tmp_path)
    ident = store.create_identity("小明", "xiaoming", "小明同学")
    assert ident["is_admin"] is False
    admin = store.create_identity("管理员", "root", is_admin=True)
    assert admin["is_admin"] is True

    # 更新：显式 false 生效（前端取消勾选必须落盘）
    store.update_identity(admin["id"], is_admin=False)
    assert store.get_identity(admin["id"])["is_admin"] is False
    # 未传字段保持不变
    store.update_identity(ident["id"], name="小刚")
    assert store.get_identity(ident["id"])["is_admin"] is False

    # 重新加载确认持久化
    store2 = IdentityStore(data_dir=tmp_path)
    assert store2.get_identity(admin["id"])["is_admin"] is False
    assert store2.get_identity(ident["id"])["is_admin"] is False

    # 旧数据缺 is_admin 键 → 加载不崩溃，读取由调用方 .get 兜底
    import json as _json

    (tmp_path / "virtual_session" / "identities.json").write_text(
        _json.dumps(
            {
                "items": [
                    {
                        "id": "id_old",
                        "name": "旧身份",
                        "sender_id": "old",
                        "sender_name": "旧",
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    store3 = IdentityStore(data_dir=tmp_path)
    old = store3.get_identity("id_old")
    assert old.get("is_admin", False) is False


def test_identity_admin_index(tmp_path):
    """is_admin_of 惰性索引：命中任一管理员身份即真；create/update/delete 失效重建。"""
    store = IdentityStore(data_dir=tmp_path)
    assert store._admin_index is None  # 惰性：查询前不构建
    assert store.is_admin_of("nobody") is False  # 空库首次构建

    admin = store.create_identity("管理员", "admin_1", is_admin=True)
    store.create_identity("普通成员", "member_1")
    # 创建使索引失效，查询重建 → 命中新管理员
    assert store.is_admin_of("admin_1") is True
    assert store.is_admin_of("member_1") is False

    # 同一 sender_id 对应多个身份：任一管理员即真（与旧 _resolve_role 语义一致）
    store.create_identity("同名成员", "admin_1")
    assert store.is_admin_of("admin_1") is True

    # 更新降级：is_admin 显式 false → 索引重建后不再命中
    store.update_identity(admin["id"], is_admin=False)
    assert store.is_admin_of("admin_1") is False

    # 更新改 sender_id：旧 id 不再命中、新 id 命中
    store.update_identity(admin["id"], sender_id="boss", is_admin=True)
    assert store.is_admin_of("boss") is True
    assert store.is_admin_of("admin_1") is False

    # 删除管理员 → 不再命中
    store.delete_identities([admin["id"]])
    assert store.is_admin_of("boss") is False

    # 重新加载：索引从持久化数据重建
    store2 = IdentityStore(data_dir=tmp_path)
    assert store2.is_admin_of("boss") is False
    assert store2.is_admin_of("admin_1") is False

    # 空串 sender_id + is_admin：索引与旧逐条扫描谓词等价（`"" == ""` 命中）
    (tmp_path / "virtual_session" / "identities.json").write_text(
        json.dumps(
            {
                "items": [
                    {"id": "id_empty", "name": "空", "sender_id": "", "is_admin": True}
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    store3 = IdentityStore(data_dir=tmp_path)
    assert store3.is_admin_of("") is True
    assert store3.is_admin_of("anything") is False


def test_identity_admin_index_degenerate_data(tmp_path):
    """索引对脏数据健壮：非 str sender_id（is_admin 真）与旧数据缺 is_admin 键均不命中且不崩。"""
    (tmp_path / "virtual_session").mkdir(parents=True, exist_ok=True)
    (tmp_path / "virtual_session" / "identities.json").write_text(
        json.dumps(
            {
                "items": [
                    {"id": "n1", "name": "null", "sender_id": None, "is_admin": True},
                    {"id": "n2", "name": "num", "sender_id": 123, "is_admin": True},
                    {"id": "n3", "name": "list", "sender_id": ["x"], "is_admin": True},
                    {"id": "l1", "name": "旧身份", "sender_id": "legacy_admin"},
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    store = IdentityStore(data_dir=tmp_path)
    # 构建索引不崩；非 str sender_id 被 isinstance 守卫排除 → 查询不命中
    assert store.is_admin_of("123") is False
    assert store.is_admin_of(123) is False
    assert store.is_admin_of("x") is False
    # 旧数据缺 is_admin 键 → 非管理员
    assert store.is_admin_of("legacy_admin") is False


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


# ---------- M2：LLM 评审（reviewer / assessor / profile 存储 / final_rules） ----------


def _valid_profile() -> dict:
    """构造一个合法的评审 profile（provider_id 为 prov_r，与 FakeLLMProvider 对应）。"""
    return {
        "id": "rp_test",
        "name": "质量评审",
        "provider_id": "prov_r",
        "model": "review-model",
        "system_prompt": "请评审，输出 {{metrics}}",
        "context": "reply",
        "metrics": [
            {"key": "score", "type": "number", "pass_threshold": 80},
            {
                "key": "level",
                "type": "enum",
                "enum_values": ["好", "差"],
                "pass_categories": ["好"],
            },
        ],
    }


def test_validate_profile_ok_and_errors():
    assert validate_profile(_valid_profile()) == []
    errors = validate_profile({})
    assert "name 必填" in errors
    assert "provider_id 必填" in errors
    assert "model 必填" in errors
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
    assert prompt.startswith("第 1 步:")
    # 结构化评审材料：中文标签块标注身份与输入/输出分界
    assert "【输入 · user（测试台）】\n问" in prompt
    assert "【输出 · agent（virtual_bot）】\n回答" in prompt
    # 评审输入（喂给 LLM 的上下文）与评审输出（LLM 原始返回）随 verdict 落盘
    assert verdict["context_text"] == prompt
    assert verdict["raw"] == '{"score": 90, "level": "好"}'


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


# ---------- 实际输入快照与结构化评审材料 ----------


def test_snapshot_llm_input_renders_strings():
    """实际输入快照：prompt + extra parts（TextPart / ThinkPart）+ system_prompt。"""
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
async def test_plugin_on_llm_snapshots_actual_input():
    """on_llm hook 把实际输入快照写入事件 extra（评审材料的数据源）。"""
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
async def test_call_reviewer_agent_system_prompt_placeholder():
    """{{agent_system_prompt}}：传入展开为被测 agent 提示词，未传展开为空串。"""
    profile = {
        **_valid_profile(),
        "system_prompt": "结合被测提示词评审：{{agent_system_prompt}}",
    }
    provider = FakeLLMProvider("prov_r", responses=['{"score": 88, "level": "好"}'])
    _, error, status, _ = await call_reviewer(
        FakeContext(providers=[provider]),
        profile,
        "上下文",
        agent_system_prompt="被测 SP",
    )
    assert status is None and error is None
    assert provider.calls[0]["system_prompt"] == "结合被测提示词评审：被测 SP"

    # 未提供 → 空串（无字面量占位符残留）
    provider2 = FakeLLMProvider("prov_r", responses=['{"score": 88, "level": "好"}'])
    _, _, status2, _ = await call_reviewer(
        FakeContext(providers=[provider2]), profile, "上下文"
    )
    assert status2 is None
    assert "{{agent_system_prompt}}" not in provider2.calls[0]["system_prompt"]
    assert provider2.calls[0]["system_prompt"].endswith("：")


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
async def test_retry_llm_verdict_passes_agent_system_prompt():
    """报告评审重试透传 agent_system_prompt：重跑时占位符不再保持字面量。"""
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
    assert provider.calls[0]["system_prompt"] == "SP: 被测 SP"
    assert new["agent_system_prompt"] == "被测 SP"


def test_result_summary_carries_llm_input():
    """result_summary 携带实际输入快照（评审材料的数据源）。"""
    ev = VirtualMessageEvent.create(
        session_id="vs_1", sender_id="u1", sender_name="用户1", text="hi"
    )
    assert ev.result_summary()["llm_input"] is None
    snap = {"prompt": "p", "extra_parts": ["e"], "system_prompt": "sp"}
    ev.set_extra(ve_mod.TESTBENCH_LLM_INPUT_EXTRA_KEY, snap)
    assert ev.result_summary()["llm_input"] == snap


def test_reviewer_store_crud(tmp_path):
    store = ReviewerStore(data_dir=tmp_path)
    assert store.list_profiles() == []
    assert store.get_profile("rp_none") is None

    p = store.create_profile(
        {
            "name": "评审",
            "provider_id": "prov_r",
            "model": "m",
            "system_prompt": "提示词",
            "context": "record",
            "metrics": [{"key": "score", "type": "number"}],
        }
    )
    assert p["id"].startswith("rp_")
    assert store.get_profile(p["id"]) is not None

    # 缺省归一：空名回退「评审」、context 缺省 reply、metrics 缺省 []
    p2 = store.create_profile({"name": "  ", "metrics": []})
    assert p2["name"] == "评审"
    assert p2["context"] == "reply"
    assert p2["metrics"] == []

    # 部分更新 + 未传字段保持不变
    updated = store.update_profile(p["id"], {"name": "新名", "context": "reply"})
    assert updated["name"] == "新名" and updated["context"] == "reply"
    assert updated["provider_id"] == "prov_r"

    # 持久化
    reloaded = ReviewerStore(data_dir=tmp_path)
    assert len(reloaded.list_profiles()) == 2

    assert store.delete_profiles([p["id"], "rp_none"]) == 1
    assert store.delete_profiles([p2["id"]]) == 1
    assert store.list_profiles() == []


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

    # 列表（两个 profile）+ 按需删除（部分删除保留其余）
    listing = await plugin.list_reviewers()
    reviewers = json.loads(listing.body)["reviewers"]
    assert len(reviewers) == 2
    resp7 = await call_handler(plugin.delete_reviewers, {"ids": [profile["id"]]})
    assert json.loads(resp7.body)["deleted"] == 1
    listing2 = await plugin.list_reviewers()
    assert len(json.loads(listing2.body)["reviewers"]) == 1
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


# ---------- M3 报告层 ----------


def test_report_store_crud(tmp_path):
    """报告存储：创建 / 列表（按测试集过滤、倒序）/ 查询 / 删除 / 级联删除 / 持久化。"""
    store = ReportStore(data_dir=tmp_path)
    assert store.list_reports() == []
    assert store.get_report("rp_none") is None

    r1 = store.add_report("ts_1", "tr_1", {"status": "done"})
    assert r1["id"].startswith("rp_")
    store.add_report("ts_2", "tr_2", {"status": "done"})
    assert len(store.list_reports()) == 2

    # 按测试集过滤；其他测试集的报告不返回
    assert [r["id"] for r in store.list_reports(testset_id="ts_1")] == [r1["id"]]
    assert store.list_reports(testset_id="ts_none") == []

    assert store.get_report(r1["id"])["testset_id"] == "ts_1"

    # 删除（含不存在的 id 一并跳过）
    assert store.delete_reports([r1["id"], "rp_none"]) == 1
    assert store.get_report(r1["id"]) is None

    # 级联删除指定测试集产出的全部报告
    assert store.delete_for_testsets(["ts_2"]) == 1
    assert store.list_reports() == []

    # 持久化：重载后仍在
    store.add_report("ts_3", "tr_3", {"status": "error"})
    reloaded = ReportStore(data_dir=tmp_path)
    assert len(reloaded.list_reports()) == 1
    assert reloaded.list_reports()[0]["testset_id"] == "ts_3"


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


def _report_with_llm_verdicts(report_store: ReportStore, profile: dict) -> dict:
    """构造含机械 + 失败 LLM + 通过 LLM verdict 的报告并写入 store。"""
    verdict_failed = {
        "rule_index": 1,
        "status": "error",
        "pass": None,
        "metrics": [],
        "detail": "评审调用失败: boom",
        "raw": "",
        "context_text": "第 1 步: 问\n回复: 答",
        "profile_id": profile["id"],
    }
    verdict_ok = {
        "rule_index": 2,
        "status": "ok",
        "pass": True,
        "metrics": [{"key": "score", "type": "number", "value": 90}],
        "detail": None,
        "raw": '{"score": 90}',
        "context_text": "第 1 步: 问\n回复: 答",
        "profile_id": profile["id"],
    }
    data = {
        "run_id": "tr_1",
        "testset_id": "ts_1",
        "testset_name": "重试",
        "status": "done",
        "steps": [
            {
                "status": "done",
                "text": "问",
                "results": [
                    {
                        "session_id": "vs_1",
                        "reply": "答",
                        "status": "ok",
                        "verdicts": [
                            {
                                "rule_index": 0,
                                "status": "ok",
                                "pass": True,
                                "metrics": [
                                    {"key": "pass", "type": "bool", "value": True}
                                ],
                                "detail": None,
                                "raw": None,
                                "context_text": None,
                                "profile_id": None,
                            },
                            verdict_failed,
                            verdict_ok,
                        ],
                    }
                ],
            }
        ],
        "final_verdicts": [
            {
                "rule_index": 0,
                "scope": "all",
                "results": [
                    {
                        "session_id": "vs_1",
                        "verdict": {
                            "rule_index": 0,
                            "status": "invalid",
                            "pass": None,
                            "metrics": [],
                            "detail": "评审输出不是合法 JSON",
                            "raw": "不是 JSON",
                            "context_text": "第 1 步: 问\n回复: 答",
                            "profile_id": profile["id"],
                        },
                    }
                ],
            }
        ],
    }
    data["metrics_summary"] = build_metrics_summary(data)
    return report_store.add_report("ts_1", "tr_1", data)


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


# ---------- 定时任务探测（cron_probe）与异步补发检测 ----------


def _cron_job(
    job_id: str,
    job_type: str,
    payload: dict | None,
    enabled: bool = True,
    name: str | None = None,
    cron: str = "0 0 * * *",
) -> SimpleNamespace:
    """构造一条 dict 化的 cron 任务（cron_job_warning 与 FakeCronManager 共用）。"""
    return SimpleNamespace(
        job_id=job_id,
        name=name or job_id,
        job_type=job_type,
        cron_expression=cron,
        payload=payload,
        enabled=enabled,
    )


class FakeCronManager:
    """最小 cron_manager 替身：list_jobs 异步、get_next_run_time 可配。"""

    def __init__(self, jobs: list, next_run=None):
        self._jobs = jobs
        self._next_run = next_run

    async def list_jobs(self):
        return list(self._jobs)

    def get_next_run_time(self, job_id):
        if self._next_run is None:
            raise RuntimeError("scheduler not running")
        return self._next_run


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
    from datetime import datetime

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
