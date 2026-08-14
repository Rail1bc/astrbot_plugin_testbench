# 详细评估：缺口 A —— 出方向异步回复捕获（settle 静默窗口）

- 评估日期：2026-08-07
- 状态：**评估完成；A1（settle 合并静默窗）未按原方案实施，2026-08-07 落地了轻量检测变体**
- 结论：可修复，推荐方案 **A1（settle 静默窗口，防抖 + 上限）**，纯插件内改动、默认关闭
  零回归。与缺口 B（主动推送捕获，见 `docs/issue-proactive-message-capture.md`）相互独立。
- **落地变体（非 A1）**：`core/runner.py` 的 `late_send_detect_window`
  （`LATE_SEND_DETECT_WINDOW=1.0`，main.py 装配传入，构造缺省 0 保持测试零延迟）——pipeline
  结束后睡该窗口观察 `event.captured` 增长，窗口内 fire-and-forget 补发的回复**不计入结果**，
  只在 `summary["warning"]` 标记「pipeline 结束后又有 N 条回复到达」；与 A1「延后定稿、并入
  结果」语义不同（检测不是捕获），长延时 / 定时任务来源由 `core/cron_probe.py` 兜住计划根源。

## 1. 现状与问题

虚拟事件的"生命周期"= pipeline 生命周期：

- `pipeline_done_event` 在 `PipelineScheduler.execute` 的 finally 块里置位
  （`astrbot/core/pipeline/scheduler.py:96-98` → `core/virtual_event.py:195-198`）。
- `runner._await_event` 等它置位后**立即** `result_summary()`、写消息流、发 `session_done`
  （`core/runner.py:326-363`），无宽限期。

因此插件在 handler 内 **fire-and-forget**（`asyncio.create_task` 后台补发）或延迟任务在
pipeline 结束后才 `send()` 的回复，虽然仍会被 append 进 `event.captured`
（`core/virtual_event.py:149-153`），但摘要快照已冻结：

- 运行结果 / 消息流 / 评审（机械规则与 LLM 评审） / 面板反馈全部只看到 pipeline 内完成的部分；
- 典型症状："正在处理"（同步）在，而"处理结果：xxx"（异步）丢失。

## 2. 目标行为

可选地（默认关闭）把"pipeline 结束后、静默期内到达的回复"并入最终结果，使：

- 运行结果 `reply` 为定稿后的完整拼接文本；
- 消息流写入完整回复；
- 评审 / 断言读取完整回复；
- 面板在静默期结束后展示最终回复。

## 3. 方案 A1 设计

### 3.1 信号：捕获计数 + 捕获事件（VirtualMessageEvent 内）

```python
# core/virtual_event.py
self._capture_gen = 0            # send() 每追加一条 captured 自增
self._capture_event = asyncio.Event()  # send() 追加后置位

async def send(self, message: MessageChain | None) -> None:
    if message is not None and message.chain:
        self.captured.append(message)
        self._capture_gen += 1
        self._capture_event.set()
    self._mark_finished()
    await super().send(message if message is not None else MessageChain())

async def settle(self, quiet: float, cap: float) -> None:
    """pipeline 结束后等待静默：quiet 秒内无新捕获即定稿；有则重置计时；至多 cap。"""
    deadline = time.monotonic() + cap
    while True:
        self._capture_event.clear()
        baseline = self._capture_gen
        remaining = min(quiet, deadline - time.monotonic())
        if remaining <= 0:
            break
        try:
            await asyncio.wait_for(self._capture_event.wait(), remaining)
        except asyncio.TimeoutError:
            break  # 静默期满 → 定稿
        if self._capture_gen == baseline:
            break  # 防御：被非 send 路径置位（不应发生）
```

要点：

- 计数（generation）而非仅 `len(captured)`，避免清事件与读长度的竞态窗口；
- pipeline 期间的 `send()` 也会置位事件，但 settle 在 `pipeline_done_event` **之后**才调用，
  且进入循环先 clear + 记 baseline，因此只对"之后新增"的捕获敏感；
- 空流路径（`send_streaming` 只有 reasoning / 空 buffer → 只 `_mark_finished`，`virtual_event.py:188-190`）
  不置位捕获事件：无回复即不产生信号，settle 会自然等满 quiet 后按 no_reply 定稿。

### 3.2 接线：runner `_await_event`

```python
async def _await_event(self, test_id, event):
    await event.pipeline_done_event.wait()
    if self._settle_grace > 0:
        await event.settle(self._settle_grace, SETTLE_CAP_SECONDS)
    entry = self._pending.get(event.entry_id)
    if entry is not None:
        entry["status"] = "done"   # 移到 settle 之后：静默期内在途条保持"LLM 生成中"
        ...
    summary = event.result_summary()
    ...
```

顺序变化只有两处：settle 插在 `pipeline_done_event.wait()` 之后；`entry["status"]="done"`
从"刚进 `_await_event`"移到 settle 之后（前端在途条在静默期内维持进行中状态）。

### 3.3 配置

插件目前**没有** `_conf_schema.json`，`VirtualSessionPlugin.__init__` 也不注入 config
（`main.py:63`）。新增全局配置（schema + `__init__(context, config)` 改造）成本较高且影响面大，
不推荐。推荐**测试集级字段**，与既有 `report_enabled` 开关（`store/testset_store.py`、
`api/testsets.py` 校验、`core/testset_runner.py:109` 启动时快照）同一模式：

- 字段：`settle_grace_seconds`（number，≥ 0，缺省 0 = 关闭）。
- 校验：API 创建 / 更新时非布尔数字且 ≥ 0，非法 → 400；store 落盘数值化。
- 传递链：`TestsetRunner.start_run` 按启动时快照（如 `report_enabled`）→ 存 run →
  每步 `runner.start(..., settle_grace=...)` → runner 存 run record → `_await_event` 读取。
- 手动群发（`api/runs.py`）暂不接配置，恒为 0。

上限 `SETTLE_CAP_SECONDS` 用代码常量（如 60s），不进配置，避免配置膨胀：防止心跳式
持续发送的插件把单步拖到步骤超时。cap ≥ quiet 恒成立（`cap = max(60, quiet)` 亦可）。

### 3.4 时序示例（quiet=3s, cap=60s）

```
T0  runner.start → 事件入队
T1  pipeline 内：插件 send("正在处理")            captured=[正在处理]
T2  pipeline 结束 → pipeline_done_event 置位
T3  settle 开始（baseline=1）→ 等待 3s
T3+0.5s  后台任务 send("处理完成")                captured=[正在处理,处理完成]  → 重置 3s
T3+1s     后台任务 send("处理结果：xxx")           captured=[...,处理结果：xxx]  → 重置 3s
T3+3s     静默期满 → 定稿
T4  result_summary → reply="正在处理\n处理完成\n处理结果：xxx"
T5  写消息流 / 发 session_done / all_done → wait_done 返回
```

## 4. 边界情况逐条分析

| 场景 | 行为 | 判定 |
|---|---|---|
| 常规同步回复 | pipeline 内已捕获；settle 只是空等 quiet | 开启后每步至多 +quiet 延迟；关闭时零影响 |
| 纯异步插件（pipeline 内无回复） | settle 等到首条晚到回复后重置；超 cap 仍无 → no_reply | 正确：晚到回复被并入；超时仍判 no_reply 不误报 |
| "先回进度、结果异步" | 晚到回复重置计时，burst 全部并入 | 比"仅空捕获才等待"策略完整（见 6.2 取舍） |
| 心跳式持续发送 | 每次重置 → 撞 cap 强制定稿 | 有界，不拖死步骤（步骤超时 600s 兜底） |
| 评审 / 断言 | 读 `step["results"]`（wait_done 返回）→ 定稿后结果 | 机械规则与 LLM 评审都拿到完整文本 |
| 消息流 | `_write_stream_reply` 在 settle 后执行（`runner.py:392-412`） | 完整回复入库 |
| 批量段 | 多步并发入队，各事件独立 settle，等待互相重叠 | 墙钟 ≈ max(settle) 而非求和 |
| 步骤超时 | `wait_done(timeout_secs=600)` 覆盖 settle 等待 | 无冲突 |
| `duration` 语义 | `finished_at` 仍是首条回复到达时间（`virtual_event.py:144-147`） | 诚实口径：含等待首条回复的时间；无需改 |
| 空流回复 | `send_streaming` 空 buffer → 只 `_mark_finished`，无捕获信号 | settle 等满 quiet 后按 no_reply 定稿 |
| 前端在途条 | 静默期 `status` 保持"LLM 生成中"，settle 后置 done | 视觉上等待补发，合理 |
| cron / 主动推送 | 走 `Context.send_message` 出站路径，与本方案无关 | 缺口 B，另文档 |

## 5. 兼容性与改动面

**兼容性**：`settle_grace_seconds` 缺省 0 → 全部现有行为不变（零回归）。开启只是把"定稿时刻"
延后到静默期后。

**改动文件清单**：

| 文件 | 改动 |
|---|---|
| `core/virtual_event.py` | `_capture_gen` / `_capture_event`；`send()` 追加信号；`settle(quiet, cap)` |
| `core/runner.py` | `start(..., settle_grace=0)` 参数；run record 存值；`_await_event` 接线 + 移动 done 标记；`SETTLE_CAP_SECONDS` 常量 |
| `core/testset_runner.py` | `start_run` 快照 `settle_grace_seconds` → run；每步传给 `runner.start` |
| `store/testset_store.py` | 模型字段 + `_normalize_messages` 外的顶层字段清洗（数值化、缺省 0） |
| `api/testsets.py` | 创建 / 更新校验（number ≥ 0，非法 400） |
| `tests/test_backend.py` | 见第 6 节 |
| `docs/`（CLAUDE.md 运行器小节）+ `CHANGELOG.md` | 记录配置与语义 |

前端：**无需改动**（面板 / 报告视图消费的是定稿后的结果）。可选打磨：在途条显示"等待异步回复"
文案，非必需。

## 6. 测试计划

1. **settle 单元测试**（直接构造 `VirtualMessageEvent`）：
   - 同步回复：settle 立即返回（baseline 不增长时静默期满即定稿，不 double-wait）；
   - 晚到单条回复：quiet 内到来 → 并入后定稿；
   - 晚到 burst：多次 reset，全部并入；
   - cap 上限：持续置位 → 撞 cap 定稿；
   - `send_streaming` 空流路径不置位信号。
2. **`_await_event` 集成测试**（复用 `FakeContext` + `VirtualSessionPlugin`）：
   - grace=0：现状行为不变；
   - grace>0：pipeline 结束后 `asyncio.create_task` 补发 → `wait_done` 返回的
     `results[].reply` 含补发文本；`stream_store` 含完整 bot 消息；断言规则命中补发文本；
   - 超 cap：`reply` 不含 cap 后补发的文本。
3. **测试集端到端**：带 `settle_grace_seconds` 的测试集跑通，`report_enabled` 报告中的
   verdict `context_text` 为定稿文本。
4. **回归**：全量 `tests/test_backend.py`（188 个）+ `test_frontend.py`。

## 7. 风险与取舍

- **主风险是配置接线**（测试集字段 + API 校验 + runner 传递链），非 settle 算法本身；
  按 `report_enabled` 既有模式照搬，风险可控。
- **延迟代价**：开启后每步测试墙钟至多增加一个静默窗；静默窗内补发会连续重置。用默认 0
  保证不付费。
- **有界但不完备**：超过 cap 的补发仍丢——这是有意的（有界性 > 完备性）。
- **语义说明**：评审 / 断言看到的是"静默期后的最终文本"，中间进度消息仍混在文本里
  （与现状一致），不是结构化多条消息。若要"只评最终结果"，属规则设计问题，不在本方案。

### 备选方案对比

- **A2 定稿后晚写（late write）**：摘要先定稿，晚到回复再更新 record + 重发事件。
  与 A1 相比多出"部分摘要已在面板闪现 / 评审读到旧快照"的竞态，且测试集顺序语义模糊。
  不选：A1 的"延后定稿"更确定。
- **A3 固定延迟（无防抖）**：pipeline 后固定等 N 秒一次定稿。实现更简，但对 burst 不完整
  （一次晚到即断）。作为 A1 的降级备选保留；若实现成本超出预期可退化为 A3。

## 8. 结论

缺口 A 可修复，推荐 A1：settle 静默窗口（防抖 + cap），测试集级配置 `settle_grace_seconds`
缺省 0，纯插件内、零回归。按需实施；当前仅记录评估。
