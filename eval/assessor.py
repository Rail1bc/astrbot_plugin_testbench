"""异步评审编排器：机械规则先跑、全部通过才调 LLM（短路）。

测试集运行完成后由 TestsetRunner 统一触发评审。评估范围：

- **消息规则**：每条已 done 步骤 × 每会话 × 每条规则。规则 entry 支持三类：
  机械叶 / LLM 叶（``kind=="llm"``）与组合算子 ``op=="any"``（任意组，组内
  子规则隐式 all，任一子规则通过即通过）/ ``op=="not"``（取反，单子规则，
  pass 取反）。机械叶同步评估；LLM 叶在 ``skip_llm`` 置位时跳过（短路——
  成本 / 不确定性控制）。verdicts 写入该步骤对应会话的 results。LLM 规则
  context=slice 时可配 ``rule.slice_range``（{from, to} 0 基闭区间列表，支持
  多段）限定喂给评审 LLM 的记录区间（未配时与 record 等效，即该步及之前
  全部记录）。LLM 规则可配 ``rule.inject_system_prompt``（缺省开启）：开启时
  在评审输入开头注入被测 agent 的（装饰后）系统提示词（占位符展开已废弃，
  注入 prompt 对所有 Provider 生效）。
- **final_rules**：测试集级跨轮评估。每条 final_rule × 每会话，按 scope 切片
  步骤后评估整段记录；机械规则评估切片回复的拼接文本，LLM 规则按
  context（reply / record / slice）取上下文（final rule 的范围由 scope 承担，
  不再配 slice_range）。产物存 run 级 final_verdicts。组合算子只支持消息规则
  （组合 final rule 走机械「未知断言类型」兜底 pass False）。

verdict 结构见 eval/reviewer.py：ok / error / invalid + 类型化 metrics + 派生 pass。

组合算子语义与短路规则（``_eval_entry`` / ``_eval_any`` / ``_eval_not``）：

- 顶层 ``skip_llm``：按序评估 entries，非 LLM entry（机械 / any / not）value
  为 False 时置位，同步骤后续 LLM 叶跳过——镜像现状「机械未过 → 后续 LLM
  跳过」；直接 LLM 叶 pass False / None 不触发短路（与现状一致）。
- any 组内部：机械子叶恒评估，LLM 子叶仅当「组尚未被已评估子叶决定为通过」
  时评估（任一子叶通过后后续 LLM 子叶跳过，机械子叶仍评估）。
- 每条 entry 产 1 条 verdict（rule_index = entry 在 rules 列表中的下标，组合
  节点内的叶共享该下标；verdict 数组位置是定位键，retry locator 不受影响）。
  any 组 verdict 的 metrics 为全部已评估子叶 metrics 拼接（保留完整指标值）；
  not 的 verdict 为子 verdict 副本（pass 取反、metrics / raw / context_text
  保留；子 pass 为 None 时不取反——评审失败不掩盖组合结果）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..core.virtual_event import BOT_SELF_ID
from .mechanical import evaluate_rule
from .persona import conversation_persona_id, resolve_agent_system_prompt
from .reviewer import (
    call_reviewer,
    llm_verdict,
    mechanical_verdict,
)

if TYPE_CHECKING:
    from astrbot.api.star import Context


def build_input_text(llm_input, fallback_text: str) -> str:
    """取实际喂给被测 LLM 的输入文本。

    框架 / 其他插件会在调用前改写 `req.prompt` 并追加 extra parts（如
    `<system_reminder>`、知识库结果），快照（来自 main.py 的 on_llm hook）即
    这份装饰后的输入；无快照时回退测试集原始文本。
    """
    if not isinstance(llm_input, dict) or not (
        llm_input.get("prompt") or llm_input.get("extra_parts")
    ):
        return fallback_text
    blocks = [llm_input.get("prompt") or ""]
    blocks.extend(llm_input.get("extra_parts") or [])
    return "\n".join(b for b in blocks if b)


def format_turn(input_text: str, reply: str, user_name: str, agent_name: str) -> str:
    """格式化单轮（实际输入 → 回复），用中文标签块标注身份与输入/输出分界。

    不用 XML 标签：框架 / 插件可能向输入注入 `<system_reminder>` 等 XML
    标记，中文标签块独占行可避免词法冲突。
    """
    return (
        f"【输入 · user（{user_name}）】\n{input_text}\n\n"
        f"【输出 · agent（{agent_name}）】\n{reply or '（无回复）'}"
    )


def format_record(entries: list[tuple[str, str, str, str]]) -> str:
    """格式化对话记录（实际输入 → 回复，带身份）供 LLM 评审上下文。

    entries 为 (input_text, reply, user_name, agent_name) 四元组列表。
    """
    blocks: list[str] = []
    for i, (input_text, reply, user_name, agent_name) in enumerate(entries, 1):
        blocks.append(
            f"第 {i} 步:\n" + format_turn(input_text, reply, user_name, agent_name)
        )
    return "\n\n".join(blocks)


def _session_agent_system_prompt(result: dict) -> str | None:
    """从步骤结果取被测 agent 的（装饰后）系统提示词；无快照 → None。"""
    llm_input = result.get("llm_input")
    if isinstance(llm_input, dict):
        sp = llm_input.get("system_prompt")
        if sp:
            return str(sp)
    return None


def inject_system_prompt_block(
    context_text: str, agent_system_prompt: str | None
) -> str:
    """把被测 agent 的（装饰后）系统提示词注入评审上下文开头（规则级可选）。

    ``{{agent_system_prompt}}`` 占位符展开已废弃（部分评审 Provider 不把
    system_prompt 真正发给评审 LLM，占位符内容到不了模型）；改为注入评审
    输入（prompt）开头——prompt 是所有 Provider 必传的。注入块恒存在：未捕获
    （无快照）或内容为空时显示占位文案，报告详情可直观确认注入链路状态，
    不静默吞掉。用前后闭合的「以下是 / 以上是」块包裹内容，长提示词也能
    清晰区分块边界（沿用中文标签块，避免与注入的 XML 标记冲突）。
    """
    content = agent_system_prompt or "（未捕获到被测 agent 系统提示词）"
    return (
        f"【以下是被测 Agent 系统提示词】\n{content}\n"
        f"【以上是被测 Agent 系统提示词】\n\n{context_text}"
    )


class Assessor:
    """异步评审编排器（每测试集运行构造一次，持有运行时 profile 快照）。"""

    def __init__(self, context: Context, profiles: dict[str, dict]) -> None:
        self.context = context
        self.profiles = profiles
        # 评审时回退解析人格的结果按 umo 记忆（同一次运行内多次命中只解析一次）
        self._persona_memo: dict[str, str | None] = {}

    async def _fallback_agent_system_prompt(self, result: dict) -> str | None:
        """评审阶段回退解析被测 agent 人格（捕获 hook 未留下快照时）。

        与 main.py 的 on_llm 捕获时回退共用 eval/persona.py 的实现：捕获链路
        未触发 / 快照系统提示词为空时，仍从会话配置档案解析人格补进评审输入，
        使评审材料不依赖捕获 hook。无 umo / 解析不到 → None（注入块显示占位）。
        """
        umo = result.get("umo")
        if not umo:
            return None
        if umo in self._persona_memo:
            return self._persona_memo[umo] or None
        conv_persona_id = await conversation_persona_id(self.context, umo)
        platform_name = str(umo).split(":", 1)[0] if isinstance(umo, str) else ""
        text = await resolve_agent_system_prompt(
            self.context,
            umo=umo,
            conv_persona_id=conv_persona_id,
            platform_name=platform_name,
        )
        self._persona_memo[umo] = text or None
        return self._persona_memo[umo]

    async def assess(
        self, steps: list[dict], final_rules: list[dict], sessions: list[dict]
    ) -> list[dict]:
        """评估全部消息规则与 final_rules。

        消息级 verdicts 写入各已 done 步骤的 results（原地）；返回 run 级
        final_verdicts 列表。
        """
        for si, step in enumerate(steps):
            if step["status"] != "done":
                continue
            await self._assess_step(steps, si, step, sessions)
        return await self._assess_final_rules(steps, final_rules, sessions)

    # ---------- 消息规则 ----------

    async def _assess_step(
        self, steps: list[dict], si: int, step: dict, sessions: list[dict]
    ) -> None:
        rules = step.get("rules") or []
        if not rules:
            return
        for result in step.get("results") or []:
            reply = result.get("reply") or ""
            entries = self._record_entries(steps, si, result)
            agent_system_prompt = _session_agent_system_prompt(result)
            if agent_system_prompt is None:
                agent_system_prompt = await self._fallback_agent_system_prompt(result)
            if entries:
                input_text = entries[-1][0]
                user_name = entries[-1][2]
            else:
                input_text = step["text"]
                user_name = step.get("sender_name") or step.get("sender_id") or "测试台"
            ctx = {
                "input_text": input_text,
                "reply": reply,
                "entries": entries,
                "agent_system_prompt": agent_system_prompt,
                "user_name": user_name,
            }
            verdicts: list[dict] = []
            skip_llm = False
            for i, rule in enumerate(rules):
                verdict, value = await self._eval_entry(rule, i, ctx, skip_llm)
                if verdict is not None:
                    verdicts.append(verdict)
                # 非 LLM entry（机械 / any / not）value 为 False → 后续 LLM 跳过；
                # 直接 LLM 叶 pass False / None 不触发短路（与现状一致）
                if value is False and rule.get("kind") != "llm":
                    skip_llm = True
            result["verdicts"] = verdicts

    async def _eval_entry(
        self,
        entry: object,
        i: int,
        ctx: dict,
        skip_llm: bool,
    ) -> tuple[dict | None, bool | None]:
        """递归评估单条规则 entry，返回 (verdict | None, value)。

        entry 形状：机械 / LLM 叶，或组合节点 ``{op: "any", rules: [...]}`` /
        ``{op: "not", rule: <叶>}``。value 语义：该 entry 对组合结果而言是否
        「通过」——True / False，或 None（被跳过：不产 verdict、不影响短路）。
        非 dict 项（数据损坏）与无 ops 的未知形状按叶处理：跳过不产 verdict
        （非 dict）或走机械「未知断言类型」兜底 pass False（未知 dict 形状）。
        """
        if not isinstance(entry, dict):
            return None, None
        op = entry.get("op")
        if op == "any":
            return await self._eval_any(entry, i, ctx, skip_llm)
        if op == "not":
            return await self._eval_not(entry, i, ctx, skip_llm)
        if entry.get("kind") == "llm":
            if skip_llm:
                return None, None
            verdict = await self._eval_llm_rule(
                i,
                entry,
                ctx["input_text"],
                ctx["reply"],
                ctx["entries"],
                ctx["agent_system_prompt"],
                ctx["user_name"],
            )
            return verdict, verdict["pass"] is True
        res = evaluate_rule(entry, ctx["reply"])
        return mechanical_verdict(i, res), bool(res and res.get("pass"))

    async def _eval_any(
        self,
        entry: dict,
        i: int,
        ctx: dict,
        skip_llm: bool,
    ) -> tuple[dict | None, bool | None]:
        """任意组：任一子规则通过即通过；产 1 条组 verdict（每 entry 一条）。

        机械子叶恒评估、LLM 子叶仅当「组尚未被已评估子叶决定为通过」时评估
        （任一子叶通过后后续 LLM 子叶跳过，机械子叶仍评估——与顶层短路同源
        的成本控制）。组 verdict 的 metrics 为全部已评估子叶 metrics 拼接
        （保留完整指标值，报告聚合与详情都得到全部子叶数据）；status 为
        "error"（全部子叶 error / invalid）否则 "ok"；pass 派生：任一子叶
        true → true、任一子叶 false → false、否则 None。全部子叶被跳过 /
        空组 → 不产 verdict。
        """
        children = entry.get("rules")
        if not isinstance(children, list):
            return None, None
        child_verdicts: list[dict] = []
        passed = 0
        any_true = False
        any_false = False
        any_ok = False
        for child in children:
            if not isinstance(child, dict):
                continue
            if child.get("kind") == "llm" and (skip_llm or any_true):
                continue
            verdict, value = await self._eval_entry(child, i, ctx, skip_llm or any_true)
            if verdict is None:
                continue
            child_verdicts.append(verdict)
            if verdict.get("status") not in ("error", "invalid"):
                any_ok = True
            if value is True:
                passed += 1
                any_true = True
            elif value is False:
                any_false = True
        if not child_verdicts:
            return None, None
        group_pass: bool | None = True if any_true else (False if any_false else None)
        metrics: list[dict] = []
        for cv in child_verdicts:
            metrics.extend(cv.get("metrics") or [])
        return (
            {
                "rule_index": i,
                "status": "ok" if any_ok else "error",
                "pass": group_pass,
                "metrics": metrics,
                "detail": f"任意（至少一条通过）：{passed}/{len(child_verdicts)} 子规则通过",
                "raw": None,
                "context_text": None,
                "profile_id": None,
            },
            group_pass is True,
        )

    async def _eval_not(
        self,
        entry: dict,
        i: int,
        ctx: dict,
        skip_llm: bool,
    ) -> tuple[dict | None, bool | None]:
        """取反：单子规则，pass 取反；子 pass 为 None（评审失败）时不取反。

        verdict 为子 verdict 副本（metrics / raw / context_text 保留，供详情
        查看与报告重试）；rule_index = entry 下标。子叶被跳过（LLM 短路）→
        not 也跳过（不产 verdict）。
        """
        child = entry.get("rule")
        if not isinstance(child, dict):
            return None, None
        verdict, _ = await self._eval_entry(child, i, ctx, skip_llm)
        if verdict is None:
            return None, None
        negated = dict(verdict)
        negated["rule_index"] = i
        if verdict.get("pass") is not None:
            negated["pass"] = not verdict["pass"]
        child_detail = verdict.get("detail")
        if child_detail:
            negated["detail"] = f"取反：{child_detail}"
        elif negated.get("pass") is False:
            negated["detail"] = "取反（原规则通过，取反后未通过）"
        elif negated.get("pass") is True:
            negated["detail"] = "取反（原规则未通过，取反后通过）"
        return negated, negated["pass"] is True

    def _record_entries(
        self, steps: list[dict], upto: int, result: dict
    ) -> list[tuple[str, str, str, str]]:
        """收集该会话从第 0 步到当前步（含）的
        （实际输入, 回复, 发送者名, agent 名）记录。"""
        session_id = result.get("session_id")
        entries: list[tuple[str, str, str, str]] = []
        for i, step in enumerate(steps):
            if i > upto:
                break
            if step["status"] != "done":
                continue
            r = next(
                (
                    x
                    for x in step.get("results") or []
                    if x.get("session_id") == session_id
                ),
                None,
            )
            if r is None:
                continue
            user_name = step.get("sender_name") or step.get("sender_id") or "测试台"
            entries.append(
                (
                    build_input_text(r.get("llm_input"), step["text"]),
                    r.get("reply") or "",
                    user_name,
                    BOT_SELF_ID,
                )
            )
        return entries

    # ---------- final_rules ----------

    @staticmethod
    def _scope_indices(scope: object, n: int) -> list[int]:
        """解析 final_rule 的 scope 切片；非 {from, to} 形状 → 全部步骤。"""
        if isinstance(scope, dict):
            frm = scope.get("from")
            to = scope.get("to")
            if (
                isinstance(frm, int)
                and isinstance(to, int)
                and not isinstance(frm, bool)
                and not isinstance(to, bool)
            ):
                return list(range(max(0, frm), min(n, to + 1)))
        return list(range(n))

    async def _assess_final_rules(
        self, steps: list[dict], final_rules: list[dict], sessions: list[dict]
    ) -> list[dict]:
        out: list[dict] = []
        for fi, fr in enumerate(final_rules):
            rule = fr.get("rule")
            if not isinstance(rule, dict):
                continue
            scope = fr.get("scope", "all")
            indices = self._scope_indices(scope, len(steps))
            per_session: list[dict] = []
            for session in sessions:
                # 测试集运行传的会话对象键为 "id"（effective 解析结果），
                # 与步骤结果里的 "session_id" 对齐
                session_id = session.get("session_id") or session.get("id")
                scoped: list[tuple[str, str, str, str]] = []
                system_prompts: list[str] = []
                for i in indices:
                    step = steps[i]
                    if step["status"] != "done":
                        continue
                    r = next(
                        (
                            x
                            for x in step.get("results") or []
                            if x.get("session_id") == session_id
                        ),
                        None,
                    )
                    if r is None:
                        continue
                    user_name = (
                        step.get("sender_name") or step.get("sender_id") or "测试台"
                    )
                    scoped.append(
                        (
                            build_input_text(r.get("llm_input"), step["text"]),
                            r.get("reply") or "",
                            user_name,
                            BOT_SELF_ID,
                        )
                    )
                    sp = _session_agent_system_prompt(r)
                    if sp is None:
                        sp = await self._fallback_agent_system_prompt(r)
                    if sp:
                        system_prompts.append(sp)
                if not scoped:
                    continue
                agent_system_prompt = system_prompts[0] if system_prompts else None
                if rule.get("kind") == "llm":
                    verdict = await self._eval_llm_rule(
                        fi,
                        rule,
                        scoped[-1][0],
                        scoped[-1][1],
                        scoped,
                        agent_system_prompt,
                        scoped[-1][2],
                    )
                else:
                    combined = "\n".join(reply for _, reply, _, _ in scoped)
                    verdict = mechanical_verdict(fi, evaluate_rule(rule, combined))
                per_session.append({"session_id": session_id, "verdict": verdict})
            out.append({"rule_index": fi, "scope": scope, "results": per_session})
        return out

    # ---------- LLM 规则 ----------

    @staticmethod
    def _slice_entries(entries: list, slice_range: object) -> list:
        """按规则 slice_range（0 基闭区间列表）切片记录；未配置 / 非法 → 原样。

        消息规则 context=slice 时用 ``rule.slice_range`` 限定喂给评审 LLM 的
        记录区间——前端多段输入（3-4,10-12）解析为 {from, to} 区间列表，
        这里逐段钳制后拼接。兼容旧版单个 {from, to} dict（数据迁移前的
        存量测试集）。边界语义同 `_scope_indices`：越界裁剪、倒序段跳过、
        形状非法（非 {from,to} 形状）回退全部。
        """
        if isinstance(slice_range, dict):
            slice_range = [slice_range]  # 旧数据单段兼容
        if isinstance(slice_range, list):
            n = len(entries)
            picked: list = []
            for item in slice_range:
                if not (
                    isinstance(item, dict)
                    and isinstance(item.get("from"), int)
                    and isinstance(item.get("to"), int)
                    and not isinstance(item["from"], bool)
                    and not isinstance(item["to"], bool)
                ):
                    return entries
                frm = max(0, item["from"])
                to = min(n - 1, item["to"])
                if frm > to:
                    continue
                picked.extend(entries[frm : to + 1])
            return picked
        return entries

    async def _eval_llm_rule(
        self,
        rule_index: int,
        rule: dict,
        input_text: str,
        reply: str,
        entries: list,
        agent_system_prompt: str | None = None,
        user_name: str = "测试台",
    ) -> dict:
        profile = self.profiles.get(rule.get("profile_id"))
        if profile is None:
            return {
                "rule_index": rule_index,
                "status": "error",
                "pass": None,
                "metrics": [],
                "detail": f"找不到评审 profile {rule.get('profile_id')!r}",
            }
        context_mode = rule.get("context") or profile.get("context") or "reply"
        if context_mode == "reply":
            context_text = format_turn(input_text, reply, user_name, BOT_SELF_ID)
        else:
            context_text = format_record(
                self._slice_entries(entries, rule.get("slice_range"))
            )
        # 规则级「注入被测 Agent 系统提示词」开关（缺省开启）：在评审输入开头
        # 注入被测 agent 的装饰后系统提示词，供评审 LLM 对照评估。占位符展开
        # 已废弃——注入 prompt 开头对所有 Provider 生效。
        if rule.get("inject_system_prompt", True):
            context_text = inject_system_prompt_block(context_text, agent_system_prompt)
        metrics, error, status, raw = await call_reviewer(
            self.context,
            profile,
            context_text,
        )
        return llm_verdict(
            rule_index,
            metrics,
            error,
            status,
            profile,
            raw=raw,
            context_text=context_text,
            agent_system_prompt=agent_system_prompt,
        )
