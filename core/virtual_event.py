"""虚拟消息事件。

虚拟会话的消息事件与真实平台事件走完全相同的 pipeline（唤醒检查 -> 白名单 ->
会话状态 -> 限流 -> 内容安全 -> 预处理 -> 插件分发 + LLM 请求 -> 回复装饰 ->
回复发送），唯一的区别是 send() / send_streaming() 不会把消息外发到真实平台，
而是捕获到内存中，供插件页面展示。

完成信号的选取：

- `done_event`：事件产生过一次回复（send 或流式结束后）时置位。
- `pipeline_done_event`：pipeline 全部执行完毕后置位。该信号复用基类的
  `cleanup_temporary_local_files()` —— 它是 `PipelineScheduler.execute()` 的
  finally 块中唯一调用点（astrbot/core/pipeline/scheduler.py:97），因此重写它
  可以精确知道"pipeline 已结束"（无论是否产生回复）。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator

from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import At, Plain
from astrbot.api.platform import (
    AstrBotMessage,
    MessageMember,
    MessageType,
    PlatformMetadata,
)

# 与 astrbot.core.astr_main_agent.LLM_ERROR_MESSAGE_EXTRA_KEY 保持一致
LLM_ERROR_MESSAGE_EXTRA_KEY = "_llm_error_message"
# 与 main.py 的 on_llm hook 约定的 extra 键：标记 LLM 阶段确实被触发
TESTBENCH_LLM_REQUESTED_EXTRA_KEY = "_testbench_llm_requested"
# 虚拟会话的机器人自身 id（模拟 @机器人 时 At 的目标）
BOT_SELF_ID = "virtual_bot"


class VirtualMessageEvent(AstrMessageEvent):
    """捕获输出、不真实外发的虚拟消息事件。"""

    def __init__(
        self,
        message_str: str,
        message_obj: AstrBotMessage,
        platform_meta: PlatformMetadata,
        session_id: str,
        done_event: asyncio.Event | None = None,
    ) -> None:
        super().__init__(message_str, message_obj, platform_meta, session_id)
        self.captured: list[MessageChain] = []
        """捕获到的回复消息链（可能有多条，如分段回复）"""
        self.reasoning_text = ""
        """流式输出中的推理内容（如有）"""
        self.started_at = time.time()
        """事件创建时间(Unix 时间戳)"""
        self.finished_at: float | None = None
        """首次产生回复的时间(Unix 时间戳)；未产生回复时为 None"""
        self.done_event = done_event or asyncio.Event()
        """产生回复后置位"""
        self.pipeline_done_event = asyncio.Event()
        """pipeline 执行完毕后置位"""
        self.entry_id: str | None = None
        """运行器登记的在途条目 id（LLM 阶段 hook 经它与条目关联）；由 runner.start() 赋值"""
        self.auto_at = False
        """是否为模拟「@机器人」发言（runner 解析消息类型后设置，用于写入消息流）"""

    @classmethod
    def create(
        cls,
        session_id: str,
        sender_id: str,
        sender_name: str,
        text: str,
        platform_id: str = "webchat",
        platform_name: str = "webchat",
        provider_id: str | None = None,
        model: str | None = None,
        done_event: asyncio.Event | None = None,
        message_type: MessageType | str = MessageType.FRIEND_MESSAGE,
        auto_at: bool = False,
    ) -> VirtualMessageEvent:
        """构造一条虚拟消息事件。

        Args:
            session_id: 虚拟会话 id（会成为 umo 中的 session_id 部分）。
            sender_id: 发送者 id，用于会话与权限判断。
            sender_name: 发送者昵称。
            text: 消息纯文本。
            platform_id: 平台 id，决定 umo 与配置档案路由；默认 webchat（与 AstrBot WebUI 一致）。
            platform_name: 平台类型名，通常与 platform_id 一致。
            provider_id: 可选，覆盖本次测试使用的 LLM provider。
            model: 可选，覆盖本次测试使用的模型名。
            done_event: 可选，外部传入的完成事件（测试时便于注入）。
            message_type: 消息类型（私聊 / 群聊），决定 umo 与唤醒检查分支。
            auto_at: 群聊消息是否模拟「@机器人」发言——开启时消息链以
                At(self_id) 开头，唤醒检查直接命中；关闭时以未唤醒状态进管道。
        """
        if isinstance(message_type, str):
            message_type = MessageType(message_type)
        abm = AstrBotMessage()
        abm.self_id = BOT_SELF_ID
        abm.sender = MessageMember(sender_id, sender_name)
        abm.type = message_type
        abm.session_id = session_id
        if auto_at:
            abm.message = [
                At(qq=abm.self_id, name=abm.self_id),
                *([Plain(text)] if text else []),
            ]
        else:
            abm.message = [Plain(text)] if text else []
        abm.message_str = text or ""
        abm.raw_message = None

        event = cls(
            message_str=abm.message_str,
            message_obj=abm,
            platform_meta=PlatformMetadata(
                name=platform_name,
                description="virtual session for testing",
                id=platform_id,
                support_streaming_message=True,
                support_proactive_message=False,
            ),
            session_id=session_id,
            done_event=done_event,
        )
        event.auto_at = auto_at
        if provider_id:
            event.set_extra("selected_provider", provider_id)
        if model:
            event.set_extra("selected_model", model)
        return event

    def _mark_finished(self) -> None:
        if self.finished_at is None:
            self.finished_at = time.time()
        self.done_event.set()

    async def send(self, message: MessageChain | None) -> None:
        """捕获一条回复（不真实外发）。"""
        if message is not None and message.chain:
            self.captured.append(message)
        self._mark_finished()
        await super().send(message if message is not None else MessageChain())

    async def send_streaming(
        self,
        generator: AsyncGenerator[MessageChain, None],
        use_fallback: bool = False,
    ) -> None:
        """消费流式生成器，把最终内容捕获为一条回复。

        参考 aiocqhttp/discord 的累积模式：把每个 chunk 的 chain 合并后
        squash_plain() 再交给 send()；reasoning 与 audio_chunk 单独处理，
        与 webchat 事件的约定一致。
        """
        # 显式置位发送操作标记（与真实适配器 tg/lark 一致）：空流路径不调
        # send()，若不置位，stage.py 会把空回复当作未回复再次触发 LLM。
        self._has_send_oper = True
        buffer: MessageChain | None = None
        reasoning_parts: list[str] = []
        async for chain in generator:
            chain_type = getattr(chain, "type", None)
            if chain_type == "reasoning":
                reasoning_parts.append(chain.get_plain_text())
                continue
            if chain_type == "audio_chunk":
                continue
            if buffer is None:
                buffer = chain
            else:
                buffer.chain.extend(chain.chain)
        if reasoning_parts:
            self.reasoning_text = " ".join(part for part in reasoning_parts if part)
        if buffer is not None:
            buffer.squash_plain()
            await self.send(buffer)
        else:
            # 流式输出为空时也标记完成，避免运行器等待超时
            self._mark_finished()
        # 不再调用 super().send_streaming()：基类实现不消费 generator（只置
        # _has_send_oper 并上报 Metric），传已耗尽的 generator 依赖其实现细节，
        # 且对虚拟事件上报 Metric 是无意义噪音。

    def cleanup_temporary_local_files(self) -> None:
        """pipeline 执行完毕的信号（由 PipelineScheduler.execute 的 finally 调用）。"""
        self.pipeline_done_event.set()
        super().cleanup_temporary_local_files()

    def result_summary(
        self,
        status: str | None = None,
        error: str | None = None,
    ) -> dict:
        """生成测试结果摘要。

        Args:
            status: 状态；缺省时根据是否捕获到回复推断（ok / no_reply）。
            error: 错误信息；缺省时读取 pipeline 写入的错误文案。

        Returns:
            包含 umo、session_id、status、duration、reply、reasoning、error、
            wake（唤醒状态）与 reason（no_reply 原因）的字典。
        """
        reply = "\n".join(
            chain.get_plain_text(with_other_comps_mark=True).strip()
            for chain in self.captured
            if chain is not None
        )
        if status is None:
            status = "ok" if self.captured else "no_reply"
        wake = {
            "woken": bool(getattr(self, "is_wake", False)),
            "at_or_wake": bool(getattr(self, "is_at_or_wake_command", False)),
            "stopped": bool(self.is_stopped()),
            "llm_requested": bool(
                self.get_extra(TESTBENCH_LLM_REQUESTED_EXTRA_KEY, False)
            ),
        }
        reason = None
        if status == "no_reply":
            reason = "not_woken" if not wake["woken"] else "woken_no_reply"
        return {
            "umo": self.unified_msg_origin,
            "session_id": self.session_id,
            "status": status,
            "duration": round((self.finished_at or time.time()) - self.started_at, 3),
            "reply": reply,
            "reasoning": self.reasoning_text,
            "error": error or self.get_extra(LLM_ERROR_MESSAGE_EXTRA_KEY),
            "wake": wake,
            "reason": reason,
        }
