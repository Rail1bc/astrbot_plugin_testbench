"""测试公共辅助与 Fake 实现（TB-09 拆分自 test_backend.py）。

被各后端测试文件共享：FakeContext 等模拟对象、请求/等待辅助与测试集构造器。
注意：本模块 import 插件模块，须在已 importorskip astrbot 的测试文件中导入。"""

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

import astrbot_plugin_testbench.core.testset_runner as tsr_mod  # noqa: E402
import astrbot_plugin_testbench.eval.reporting as rpt_mod  # noqa: E402
import astrbot_plugin_testbench.store.group_store as gs_mod  # noqa: E402
import astrbot_plugin_testbench.store.report_store as rps_mod  # noqa: E402
from astrbot.api.web import PluginRequest, bind_request_context  # noqa: E402
from starlette.requests import Request  # noqa: E402

ReportStore = rps_mod.ReportStore
TestsetRunner = tsr_mod.TestsetRunner
build_metrics_summary = rpt_mod.build_metrics_summary
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
            persona_id=persona_id,
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
        conf: dict | None = None,
    ) -> None:
        self._queue = queue or asyncio.Queue()
        self._providers = providers or []
        self.astrbot_config_mgr = SimpleNamespace(
            ucr=ucr or FakeUCR(),
            get_conf_list=lambda: list(conf_list or []),
            get_conf=lambda _umo: conf,  # 默认 None（无配置档案）
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


class FakePersonaManager:
    """模拟人格管理器：resolve_selected_persona 返回可配置 persona 并记录调用。"""

    def __init__(
        self,
        persona: dict | None = None,
        raise_on_call: bool = False,
    ) -> None:
        self.persona = persona
        self._raise = raise_on_call
        self.calls: list[dict] = []

    async def resolve_selected_persona(self, **kwargs) -> tuple:
        self.calls.append(kwargs)
        if self._raise:
            raise RuntimeError("人格解析失败")
        return ("p_id", self.persona, None, False)


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


async def wait_until(predicate, max_wait: float = 5.0) -> None:
    """轮询直到 ``predicate()`` 为真。

    替代固定 ``asyncio.sleep`` 等待异步工作完成（路由恢复、历史重生成等）：
    固定等待在慢调度 CI 上断言可能先于异步任务完成而间歇性失败（flaky），
    轮询 + 超时则快慢机器都稳定。
    """
    async with asyncio.timeout(max_wait):
        # noqa: ASYNC110 —— 轮询等待异步工作完成是测试辅助的意图（同 wait_run_done）
        while not predicate():  # noqa: ASYNC110
            await asyncio.sleep(0.01)


def _add_history(conv_mgr, sessions: list[dict]) -> list[str]:
    """给每个会话添加一条对话历史，返回对应 umo 列表。"""
    umops = [umo_of(s) for s in sessions]
    for umop in umops:
        conv_mgr.add_history(umop, "对话", [{"role": "user", "content": "hi"}])
    return umops


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


class _FailingReplyStreamStore:
    """append 正常返回固定 id；update_reply 抛 OSError（模拟流回填磁盘故障）。"""

    def __init__(self) -> None:
        self.appended: list[dict] = []

    async def append(self, session_id: str, message: dict) -> str:
        self.appended.append(message)
        return "m1"

    async def update_reply(self, session_id: str, message_id: str, status: str) -> None:
        raise OSError("模拟流写入磁盘故障")


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
