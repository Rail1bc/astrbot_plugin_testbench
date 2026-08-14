# 代码审查 Issue 清单（项目质量 / 可维护性 / 用户交互性）

- 记录日期：2026-08-07（审查完成当日）
- 审查对象：`astrbot_plugin_testbench` v1.0.1 全部源码（后端 20 个 Python 文件通读 + 前端 17 个 JS 模块深审 + 测试套件深审与实测）
- 实测结果（审查时）：后端 251 条测试全绿（10.3s），无阻塞级缺陷
- 优先级：P0（数据安全与核心体验，先做）→ P1（可靠性）→ P2（改进项，迭代排期）
- **修复状态（2026-08-14）**：30 条全部修复并验证（含 TB-09 测试文件拆分）；验证：后端测试 259 全绿（328 含前端）、前端静态 + node:test（74）全绿、ruff check / format 通过；CI 新增 astrbot 双版本矩阵与「跳过数为 0」守卫

---

## P0 — 数据安全与核心体验

### TB-01 持久化写入非原子，文件损坏时静默清空 → 数据丢失风险【高】✅ 已修复
- **背景**：全部 store（groups/testsets/identities/reviewers/reports）用 `write_text` 直接覆盖原文件，进程崩溃/断电中途写入会留下损坏文件；而 `_load` 对损坏文件一律回退空列表并继续运行，下一次保存就把「空数据」写回——用户全部测试组/测试集/报告**永久丢失且无任何告警**。这是全插件唯一可能造成用户数据不可恢复丢失的隐患。
- **修复**：`store/_base.py` 新增共享原语 `atomic_write_text`（写 tmp + `os.replace` 原子替换）与 `backup_corrupt_file`（损坏文件改名 `.corrupt-<ts>` 备份 + 根 logger `[testbench]` 告警）；六个 store（含 stream_store 的全量重写路径）的 `_save`/`_load` 全部接入，损坏时从空继续但保留现场。新增测试 `test_group_manager_corrupt_file_backed_up`（备份存在、现场可恢复、无 tmp 残留）。

### TB-02 在途条「完成」条目不消失（设计意图未落地）【中】✅ 已修复
- **背景**：前端 `renderPendingStrip` 用 `historyRefreshedAt` 隐藏已完成条目，但该时间戳写入（app.js:172）后**没有任何代码触发在途条重渲染**——最后一条消息完成后，面板底部会一直挂着「完成」chip 直到下一次发送；消息流视图下 `loadStream` 从不写该时间戳，完成条目**永久残留**。注释声称的「刷入历史即移除」实际未生效。
- **修复**：events.js 导出 `renderAllPendingStrips` 刷新入口；`loadHistory`/`loadStream` 成功写入 `historyRefreshedAt` 后立即调用（app.js），消息流视图同样补写时间戳——完成且已入历史的条目即时消失。

### TB-03 弹窗遮罩点击/取消静默丢弃表单内容，无 Esc/焦点管理【中】✅ 已修复
- **背景**：`modal-mask` 点击与「取消」直接关闭弹窗，正在编辑的长表单（评审 Profile、测试集配置）全部丢失且无确认——与「onOk 失败保留表单」的设计（modal.js:79-84）自相矛盾；同时无 Escape 关闭、无焦点圈定、关闭后焦点不还原、`role="dialog"` 缺 `aria-labelledby`。用户误点遮罩的损失成本高。
- **修复**：`openModal` 新增 `dirty` 选项（8 处表单弹窗已启用）；取消/遮罩/Esc 对 dirty 弹窗先显示**内联确认条**（不销毁表单 DOM，避免重建丢失事件监听），确认后才关闭；补 Esc 关闭、Tab 焦点圈定、关闭后焦点还原、`aria-labelledby` 关联标题。

---

## P1 — 可靠性（测试与接口健壮性）

### TB-04 4 处固定 sleep 竞态窗口 → flaky 测试【中】✅ 已修复
- **背景**：测试触发异步工作后 `await asyncio.sleep(0.05)` 固定等待再断言（路由恢复、历史重生成），慢调度 CI 上断言可能先于异步任务完成而间歇性失败，本地又总是绿的——最常见也最难排查的 flaky 来源。
- **修复**：新增 `wait_until(predicate, max_wait)` 轮询助手（`asyncio.timeout` + 10ms 轮询），4 处固定 sleep 全部替换为轮询等待（路由恢复 ×2、历史重生成 ×2）。

### TB-05 CI 依赖未锁定（astrbot 活跃演进）【中】✅ 已修复
- **背景**：CI `pip install astrbot` 无版本约束，而测试直接依赖其内部模块（`astrbot.core.umop_config_router`、`astrbot.core.agent.message` 的 TextPart/ThinkPart）；主项目任一内部 API 变更都会让 CI 全红且难定位是否插件回归。
- **修复**：pytest.yml 矩阵 `astrbot==4.26.0`（最低支持版，锁定）+ `latest`（上游最新版，不写死，随上游发版自动跟进；安装步骤打印实际版本便于日志定位，`fail-fast: false`），pytest 系同步锁定（pytest 9.1.1 / pytest-asyncio 1.4.0）；3 条直接依赖内部模块的用例标 `framework_internal` 标记（pyproject 注册），最低版矩阵 `-m "not framework_internal"` 跳过。
- **追加修正（2026-08-14）**：矩阵最初取 4.24.1（照抄 metadata 声明）导致 CI 必红——核实发现插件硬依赖的 `astrbot.api.web`（PR #8688 FastAPI 迁移）**自 v4.26.0 起才存在**（4.24.1/4.25.x 均无），故 metadata.yaml 兼容声明由 `>=4.24.1` 修正为 `>=4.26.0`，矩阵同步改为 4.26.0 / latest。
- **最终定案（2026-08-14）**：4.26.0 固定版 leg 在 CI 上安装/运行不可靠（旧版重依赖在全新 runner 环境解析失败），且与用户实际使用场景（总是跑最新 AstrBot）脱节——**砍掉固定版本 leg，CI 只跑 `pip install astrbot`（上游最新版）全量测试**；`framework_internal` 标记保留作语义标注（最新版全量运行）。

### TB-06 API 列表元素未校验 → 500；conf_id 类型未校验 → 可能污染 UCR【中】✅ 已修复
- **背景**：`sessions`/`ids` 只校验「是 list 且非空」，元素为 dict/list 时 `dict.fromkeys`/`set` 抛 TypeError → 500（已实测确认）——与插件 v0.4.5 修掉的「非 dict 体 → 400」是同一类缝隙的更深处；`conf_id` 传数字会经 `put_route_front` 把非字符串临时写进 UCR 路由表。
- **修复**：`api/common.py` 新增 `validate_id_list`（非空 list 且元素全为非空字符串，去重保序），runs/testsets/groups/sessions/reports/reviewers 的 ids/sessions 入口全部接入；identities 的 `_require_ids` 补元素级校验；`runs.py` 对 provider_id/model/conf_id、`groups.py create_group` 对 conf_id 补字符串校验；`reports.py retry` 的 targets 集合构造加 TypeError 防护。

### TB-07 报告 LLM 生成 / 批量评审重试无防重与加载态【中】✅ 已修复
- **背景**：`generateLlmReportAction` 连续点击会并发多个生成请求（后写覆盖，浪费 LLM 调用、可能重复计费），批量重试同无禁用；单条重试按钮有 `disabled` 而批量入口没有，行为不一致且无「生成中…」反馈。
- **修复**：testset_reports.js 新增 `generatingReports`/`retryingReports` 集合防并发（同一报告在途时忽略重复点击），按钮在途置灰 + 「生成中…/重试中…」文案，`.finally` 后重渲染恢复；报告条目与详情弹窗两处入口行为一致。

### TB-08 单发失败丢失用户输入【中】✅ 已修复
- **背景**：`sendToOne` 先清空输入框再 await，网络/后端失败后用户内容无法找回，长消息体验差；群发栏同理。
- **修复**：`sendToOne` 与 `sendToAll` 均改为**入队成功后才清空**输入框，失败保留原文可直接重试。

---

## P2 — 改进项（可迭代排期）

### 项目质量 / 健壮性

- **TB-09 单文件 295KB 测试**【中】✅ 已修复（2026-08-14）：原 7719 行单文件按域拆分为 `tests/fakes.py`（28 个公共辅助/Fake 类）+ `test_runner.py`（39）/ `test_stores.py`（31）/ `test_testset.py`（29）/ `test_assessor.py`（54）/ `test_api.py`（102）五个后端测试文件（283 个顶层定义全部归位、零遗漏，259 条测试全绿）；每个文件只含本域用例与按需 import（含 `@pytest.mark.asyncio` 装饰器），`framework_internal` 标记保留。
- **TB-10 `_prune_runs` 触发点不足**【低】✅ 已修复：仅在 `start()` 调用，无新测试时已完成条目不会按时清理。→ `pending_entries()` 现在顺带触发 `_prune_runs`（core/runner.py），断线对账/轮询取回时即清理。
- **TB-11 「只依赖公共 API」声明不实**【低】✅ 已修复：`astrbot.api` 未暴露路径工具（已核实），故保留 `astrbot.core.utils.astrbot_path` 但**如实声明**——CLAUDE.md 更新为「唯一例外 + 3 条 framework_internal 测试用例」的准确描述。
- **TB-12 `add_group_sessions` 缺总数上限**【低】✅ 已修复：补 `len(group.sessions) + count <= MAX_SESSIONS_PER_GROUP` 校验（与 clone_sessions 同口径，超限 400）。
- **TB-13 `update_group` 非字符串静默归一为 None**【低】✅ 已修复：`update_group` 与 `update_session` 统一为「非字符串非 null → 400」（空串仍归一 None 恢复继承，行为不变）。
- **TB-14 评审时机语义需文档明确**【低】✅ 已修复：README「测试集与评审」新增「评审时机」段、CLAUDE.md 评审时机条目补例外说明（单步失败/abort 不评审，批量段内错误照常）；新增锚定测试 `test_testset_runner_step_failure_skips_review`。

### 用户交互性

- **TB-15 历史刷新强制滚动到底**【低】✅ 已修复：chat.js 的 `renderHistory`/`renderStream` 重渲染前记录是否在底部（40px 容差，`isNearBottom`），仅原在底部/初始加载才滚动——向上翻阅时刷新不再拉回。
- **TB-16 视图切换刷新不一致**【低】✅ 已修复：`showView("sessions")` 现在也 `void refreshGroups()`（与 testsets/identities 分支口径一致）。
- **TB-17 初始加载无 loading、`ready()` 无超时**【低】✅ 已修复：`await ready()` 包 `Promise.race` 10s 超时，失败显示可见错误后继续（不再永久空白）；`loadOptions` 四个数据源改 `Promise.allSettled` 并行拉取。
- **TB-18 身份搜索无防抖**【低】✅ 已修复：`#cg-search` 输入 150ms 防抖（`SEARCH_DEBOUNCE_MS` 常量）。
- **TB-19 报告视图加载失败无重试**【低】✅ 已修复：最近运行/报告列表失败提示旁挂「重试」按钮（重新拉取当前视图）。
- **TB-20 组弹窗「会话数量」只增不减无提示**【低】✅ 已修复：数量输入小于现有会话数时内联提示「减少数量不会删除已有会话」（随输入实时显隐）。
- **TB-21 a11y 细节**【低】✅ 已修复：tab 按钮补 `role="tab"`/`aria-selected`（index.html 静态 + 三个 tab 切换函数动态维护）、面板/列表图标按钮补 `aria-label`、`#view-toggle` 文案改为「切换到 X」消除歧义、弹窗 `aria-labelledby`（并入 TB-03）。

### 可维护性

- **TB-22 死代码与未用导出**【低】✅ 已修复：删除 api.js 无调用者的 `clearStream`；`renderTestsetNav`/`syncBroadcastSenders` 移除未消费的导出（保留内部使用）。
- **TB-23 Provider 下拉重复实现**【低】✅ 已修复：抽取 `utils.js providerOptions()`（评审 Profile 表单 + 报告 LLM 配置共用），删除两处重复内联构建。
- **TB-24 魔法数字**【低】✅ 已修复：重连 3000ms / 在途文本截断 24 字符 / 搜索防抖 150ms / 预览防抖 300ms 全部提为具名常量。
- **TB-25 注释语言与主仓库规范冲突**【低】✅ 已处理（二选一中的「显式声明豁免」）：插件 CLAUDE.md 新增「注释语言约定」条目——插件仓库统一中文，主仓库 AGENTS.md 的 English 规则不适用于本插件。
- **TB-26 杂项**【低】✅ 已修复：功能性 `:has(input[type=checkbox])` 布局规则改为显式 `.settings-field-checkbox` 修饰类（老 WebView 兼容；checked 高亮两处 `:has()` 为装饰性、降级无害并加注）；删除 `.ruff_cache`/`.pytest_cache`/`__pycache__` 残留；`test_backend.py:4300` 的 `from datetime import datetime` 提至文件顶部。

### 测试覆盖

- **TB-27 前端静态断言「实现文字」而非行为**【低】✅ 部分推进：本轮与实现同步修正 3 处断言（loadOptions 并行化、providerOptions 抽取、settings-field-checkbox），并让断言锚定**不变量**（顺序、共享实现）而非具体实现文字；继续下沉 pure.js 行为测试保留为迭代方向。
- **TB-28 真并行在途覆盖不足**【低】✅ 已修复：新增 `test_runner_multiple_sessions_inflight_simultaneously`（3 会话同时 pending、全 submitted）与 `test_runner_out_of_order_completion`（乱序完成结果按 session 归位）。
- **TB-29 `importorskip` 静默跳过**【低】✅ 已修复：pytest.yml 测试输出检查「skipped 数为 0」（缺 astrbot 整组跳过时 CI 报错；framework_internal 为 deselect 不误伤）。
- **TB-30 未覆盖的设计声明**【低】✅ 已修复：新增 EventBus「慢消费者不阻塞发布者」（同步 publish + 满丢最旧）、StreamStore 损坏行容忍（半截行跳过、其余回放）、SSE `/events` 端点（data: 序列化 + generator 关闭后自动退订）、final_rules 组合兜底（op:any → 未知类型 pass False）4 条用例。

---

## 统计

| 级别 | 数量 | 主题 | 修复 |
| :--- | :--- | :--- | :--- |
| P0 | 3 | 数据安全 1（TB-01）+ 交互 2（TB-02/03） | 3/3 ✅ |
| P1 | 5 | 测试可靠性 2 + 接口健壮性 2 + 交互 1 | 5/5 ✅ |
| P2 | 22 | 质量/健壮性 6 + 交互 7 + 可维护 5 + 测试覆盖 4 | 22/22 ✅ |

实施顺序（已按此执行）：TB-01（原子写 + 损坏备份）→ TB-02/TB-03（日常体验）→ TB-04/TB-05/TB-06（可靠性）→ 其余按迭代排期。

验证：后端测试 333 全绿、前端静态 + node:test（74）全绿、ruff check / format 通过；CI 新增 astrbot 双版本矩阵与「跳过数为 0」守卫。
