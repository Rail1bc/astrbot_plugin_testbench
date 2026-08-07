"""前端脚本静态检查：不依赖 astrbot，任何环境均可运行。

仅读取插件页面脚本做静态检查，用于捕获 ES module import 重名、bridge 端点
内嵌查询串等会导致整页失效的回归。
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = REPO_ROOT  # 插件根目录即仓库根，页面文件在 pages/testbench/

# 页面全部 ES module（入口 app.js + 视图/状态/工具模块；events.js 事件驱动反馈层、
# testset_run.js 测试集运行编排、testset_editor.js 测试集编辑器、identity_list.js
# 「身份与群聊」视图、testset_reports.js 报告视图（从 testset_editor.js 拆出）为
# 拆分后新增模块；pure.js 为零依赖纯函数层，由 node:test 动态测试、经本清单做
# 静态防回归检查）
_FRONTEND_MODULES = (
    "app",
    "api",
    "align",
    "chat",
    "state",
    "utils",
    "modal",
    "group_list",
    "testset_list",
    "testset_editor",
    "testset_reports",
    "events",
    "testset_run",
    "identity_list",
    "pure",
)


def _read_module(name: str) -> str:
    return (PLUGIN_DIR / "pages" / "testbench" / f"{name}.js").read_text(
        encoding="utf-8"
    )


def _read_html() -> str:
    return (PLUGIN_DIR / "pages" / "testbench" / "index.html").read_text(
        encoding="utf-8"
    )


# ---------- 前端脚本静态检查 ----------


def test_frontend_no_import_redeclaration():
    """各 ES module 的 import 名不得与顶层声明重名。

    重构拆分 api.js 后曾把本地函数 createGroup 与 import 的 createGroup
    重名，ES module 解析期报 SyntaxError，整页 JS 失效（下拉框因此为空）。
    前端拆分出多个模块后，对所有模块逐一检查。
    """
    import re

    for name in _FRONTEND_MODULES:
        src = _read_module(name)
        imports = set()
        for match in re.finditer(r"import\s*\{([^}]*)\}\s*from", src):
            for item in match.group(1).split(","):
                item = item.strip().split(" as ")[-1].strip()
                if item:
                    imports.add(item)

        declarations = set(
            re.findall(
                r"^(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)",
                src,
                re.MULTILINE,
            )
        ) | set(
            re.findall(
                r"^(?:export\s+)?(?:const|let|var|class)\s+([A-Za-z_$][\w$]*)",
                src,
                re.MULTILINE,
            )
        )

        assert imports.isdisjoint(declarations), (
            f"{name}.js 存在 import 与顶层声明重名: {sorted(imports & declarations)}"
        )


def test_frontend_bridge_endpoint_has_no_query_string():
    """api.js 的 bridge 端点不得内嵌查询串（`?`）。

    父窗口 PluginPagePage.vue 的 normalizePluginEndpoint 拒绝含 `?` 的端点，
    查询参数必须通过 apiGet(endpoint, params) 的第二个参数传递。曾把
    test/run/status?test_id=... 拼进端点，导致 runStatus 恒 reject、
    前端轮询永不触发会话刷新（群发/单发/重新生成都不更新）。
    """
    import re

    api_js = _read_module("api")
    endpoints = re.findall(r'api(?:Get|Post)\(\s*["`]([^"`]*)["`]', api_js)
    assert endpoints, "未找到任何 bridge 调用"
    for endpoint in endpoints:
        assert "?" not in endpoint, f"bridge 端点不能内嵌查询串: {endpoint}"


def test_frontend_effective_view_resolves_sender_fields():
    """utils.js 的 effectiveView 返回对象必须含 sender_id / sender_name。

    曾漏掉这两个字段，导致会话展开配置里发送者 ID / 昵称恒显示「—」——
    effectiveView 是客户端对组配置 + 会话覆盖的解析，缺失字段即无值可显。
    （前端拆分后 effectiveView 位于 utils.js）
    """
    import re

    utils_js = _read_module("utils")
    match = re.search(
        r"export function effectiveView\(id\)[\s\S]*?return \{([\s\S]*?)\};",
        utils_js,
    )
    assert match, "找不到 effectiveView 的 return 对象"
    obj = match.group(1)
    assert "sender_id:" in obj, "effectiveView 返回值缺少 sender_id"
    assert "sender_name:" in obj, "effectiveView 返回值缺少 sender_name"


def test_frontend_select_option_builders_are_shared():
    """平台/档案下拉的选项构建必须收敛到共享辅助函数。

    曾有两处内联构建（组编辑弹窗 buildPlatformSelect/buildConfSelect 与会话配置
    弹窗 openSettings）：会话弹窗的档案副本缺少「档案已不存在」占位——会话单独
    绑定的档案被删除后，打开弹窗回落显示「使用组配置」，保存即把绑定静默重置为
    继承组配置。本检查确保选项映射/过滤只保留共享实现一处
    （拆分后位于 group_list.js 的 platformOptions/confOptions）。
    """
    import re

    src = _read_module("group_list")
    assert len(re.findall(r"\.map\(\s*\(p\) =>", src)) == 1, (
        "平台选项映射必须只出现在 platformOptions() 一处"
    )
    assert src.count('confs.filter((c) => c.id !== "default")') == 1, (
        "配置档案过滤必须只出现在 confOptions() 一处"
    )


def test_frontend_no_use_before_declaration():
    """模块级 const/let 绑定不得在声明语句之前被顶层语句引用。

    node --check 只查语法，发现不了「引用先于声明」的运行时错误：模块求值时抛
    ReferenceError: can't access lexical declaration 'x' before initialization，
    页面初始化整体中止。拆分后曾把 $("btn-refresh") 绑定放在 createGroupList
    解构之前——refreshGroups 是 const 解构绑定，处暂时性死区，页面只剩静态骨架
    （窄条按钮失效、测试组列表不渲染）。只检查顶格（列 0）语句：函数体内的引用
    在调用时才求值，不受暂时性死区影响。
    """
    import re

    binding_re = re.compile(
        r"^(?:export\s+)?(?:const|let)\s+"
        r"(?:\{([^}]*)\}|([A-Za-z_$][\w$]*))\s*(?:=|:)"
    )
    string_re = re.compile(r'"[^"]*"|\'[^\']*\'|`[^`]*`')
    ident_re = re.compile(r"[A-Za-z_$][\w$]*")

    for name in _FRONTEND_MODULES:
        lines = _read_module(name).splitlines()
        bindings = {}  # 绑定名 -> 声明行号（1-based）
        for i, line in enumerate(lines, 1):
            m = binding_re.match(line)
            if not m:
                continue
            if m.group(2):
                names = [m.group(2)]
            else:
                names = [
                    part.strip().split(" as ")[-1].split(":")[-1].strip()
                    for part in m.group(1).split(",")
                    if part.strip()
                ]
            for n in names:
                bindings[n] = i
        for binding, decl_line in bindings.items():
            for i in range(1, decl_line):
                line = lines[i - 1]
                if (
                    not line.strip()
                    or line[0].isspace()
                    or line.startswith(("import", "export", "//", "*", "#", "/*"))
                ):
                    continue
                if binding in ident_re.findall(string_re.sub("", line)):
                    raise AssertionError(
                        f"{name}.js:{i} 顶层引用了 {binding}，但其 const/let 声明在"
                        f"第 {decl_line} 行（暂时性死区，模块求值即抛 ReferenceError）"
                    )


def test_frontend_broadcast_does_not_block_overlap():
    """群发不得用 state.runBusy 阻止并发发送。

    真实场景中用户可能在 agent 处理上一条消息时重复追问；群发若在上一轮
    未完成时直接 return，这一关键场景永远无法测试。防 runBusy guard 回归。
    """
    src = _read_module("app")
    assert "runBusy" not in src, (
        "app.js 仍引用 runBusy：群发在上一轮未完成时被阻止，无法测试重叠场景"
    )


def test_frontend_tool_call_and_result_bubbles():
    """会话视图须把工具调用 / 工具返回渲染为结构化气泡。

    曾只有「（调用工具…）」裸占位与纯文本工具返回：助手消息带 tool_calls 时
    不显示工具名与参数、工具返回消息无标签包裹（工具调用显得「挂在思维链上」）。
    现须：按 msg.tool_calls 逐个渲染 .tool-call 气泡（summary 含 function.name、
    参数区为美化后 JSON），tool 角色消息渲染 .tool-result 气泡，并经 ctx.toolNames
    用 tool_call_id 关联出「哪个工具的返回」。
    """
    src = _read_module("chat")
    assert "toolCallBlock" in src, "缺少工具调用气泡构建函数 toolCallBlock"
    assert "toolResultBlock" in src, "缺少工具返回气泡构建函数 toolResultBlock"
    assert "tool-call-args" in src, "工具调用气泡缺少参数区 tool-call-args"
    assert "toolNames" in src, "缺少 tool_call_id → 工具名的关联映射 ctx.toolNames"
    assert "tool-result-head" in src, "工具返回气泡缺少头部标注"
    assert "（调用工具…）" not in src, "旧的裸「（调用工具…）」占位仍存在"


def test_frontend_panel_menu_actions():
    """会话面板页眉「⋯」菜单须含编辑/重置/复制/克隆/粘贴/衍生六项。

    曾把编辑/重置作为页眉常显按钮、复制/粘贴/克隆/衍生不存在。现收敛为
    ⋯ 下拉菜单：app.js 须渲染 .panel-menu-dropdown 菜单项并分发到
    copyHistory/pasteHistory/cloneSession/deriveSession；api.js 须封装
    sessions/clone 与 sessions/derive；state.js 须有 clipboard 剪贴板。
    """
    app_js = _read_module("app")
    api_js = _read_module("api")
    state_js = _read_module("state")
    for action in ("history", "reset", "copy", "clone", "paste", "derive"):
        assert f'data-action="{action}"' in app_js, f"面板菜单缺少 data-action={action}"
    assert "panel-menu-dropdown" in app_js, "缺少下拉菜单容器"
    assert "setupPanelMenu" in app_js, "缺少菜单绑定函数"
    for fn in ("copyHistory", "pasteHistory", "cloneSession", "deriveSession"):
        assert f"function {fn}(" in app_js, f"缺少 {fn} 操作函数"
    assert "clipboard" in state_js, "state 缺少 clipboard 剪贴板"
    assert "sessions/clone" in api_js, "api 缺少 sessions/clone 封装"
    assert "sessions/derive" in api_js, "api 缺少 sessions/derive 封装"


def test_frontend_pending_status_labels():
    """面板在途条须覆盖四个状态文案（已入队/排队等待 LLM/LLM 生成中/完成）。

    PENDING_STATUS_TEXT 与后端 runner 条目状态一一对应；缺任一键/文案，
    重叠场景下的状态显示就会不完整（如只剩「已入队」）。
    """
    src = _read_module("events")
    for key, label in (
        ("submitted", "已入队"),
        ("waiting_llm", "排队等待 LLM"),
        ("llm", "LLM 生成中"),
        ("done", "完成"),
    ):
        assert f"{key}:" in src, f"PENDING_STATUS_TEXT 缺少状态键 {key}"
        assert label in src, f"PENDING_STATUS_TEXT 缺少状态键 {key} 的文案 {label}"


def test_frontend_pending_hides_done_after_history_refresh():
    """完成且已刷入会话历史的消息不得长期留在在途条。

    曾设计为「完成」chip 保留 30s（后端 DONE_KEEP_SECONDS）后清理，与历史
    气泡中的回复重复展示。现改为：loadHistory 成功时记录刷新时刻
    （historyRefreshedAt），renderPendingStrip 过滤掉 status=="done" 且
    完成于该时刻之前的条目（回复已在气泡中，条内只留完成后的短暂过渡）。
    """
    app_js = _read_module("app")
    events_js = _read_module("events")
    assert "historyRefreshedAt" in app_js, "缺少 historyRefreshedAt 记录历史刷新时刻"
    assert "historyRefreshedAt.set(id, Date.now())" in app_js, (
        "loadHistory 成功路径必须记录刷新时刻"
    )
    assert "status_at" in events_js, "renderPendingStrip 未按 status_at 过滤已完成条目"


def test_frontend_rail_has_testset_view():
    """测试集视图入口不得丢失，且左侧选择驱动右侧视图切换。

    左侧窄条是视图切换入口（会话列表 / 测试集）：index.html 必须含
    data-view="testsets" 按钮；app.js 必须能把该视图切换到 .testsets-card
    并刷新测试集数据，同时切换右侧工作区视图（.sessions-view / .testsets-view）
    ——右侧不再设手动切换按钮，随左侧列表选择自动切换。
    """
    html = _read_html()
    app_js = _read_module("app")
    assert 'data-view="testsets"' in html, "index.html 缺少测试集视图入口（rail 按钮）"
    assert ".testsets-card" in app_js, "app.js 未引用 .testsets-card（视图切换丢失）"
    assert 'view === "testsets"' in app_js, "app.js 缺少 testsets 视图分支"
    assert ".sessions-view" in app_js, "app.js 未切换右侧会话视图 .sessions-view"
    assert ".testsets-view" in app_js, "app.js 未切换右侧测试集视图 .testsets-view"


def test_frontend_right_view_switches_from_left():
    """右侧视图由左侧列表选择驱动（选中测试集 → 自动切「测试集」视图）。

    测试集条目不再内联展开：testset_list.js 选中条目时须经 env.switchToTestsets
    把右侧切到编辑窗口；app.js 以 showView("testsets") 注入该动作。
    """
    testset_js = _read_module("testset_list")
    app_js = _read_module("app")
    assert "switchToTestsets" in testset_js, "testset_list.js 未使用 switchToTestsets"
    assert "switchToTestsets();" in testset_js, "选中测试集时未触发右侧视图切换"
    assert 'switchToTestsets: () => showView("testsets")' in app_js, (
        "app.js 未把选中测试集映射到 testsets 视图"
    )


def test_frontend_testset_export_import_and_group_target():
    """测试集导出 / 导入信封与运行目标「选择测试组」。

    导出须带 format/version 信封（为未来「测试集市场」下载兼容）；导入复用现有
    createTestset 端点（无新后端接口）；运行弹窗须支持按测试组多选目标，并把
    勾选组解析为会话 id 列表后交给 runTestset。拆分后导出 / 导入在
    testset_editor.js，运行弹窗在 testset_list.js。
    """
    editor_js = _read_module("testset_editor")
    pure_js = _read_module("pure")
    list_js = _read_module("testset_list")
    assert "astrbot-testbench-testset" in pure_js, "信封 format 标识未在 pure.js 定义"
    assert "version: EXPORT_VERSION" in editor_js, "导出信封缺少 version"
    assert "format: EXPORT_FORMAT" in editor_js, "导出信封未序列化 format"
    assert "createTestset({" in editor_js, "导入未复用 createTestset 端点"
    assert "选择测试组" in list_js, "运行弹窗缺少「选择测试组」目标选项"
    assert "env.runTestset(testset, ids)" in list_js, (
        "运行未把目标会话 id 列表交给 runTestset"
    )
    # 组多选 → 会话 id 解析：勾选 data-gid checkbox，展开为该组全部会话 id
    assert "input[data-gid]:checked" in list_js, "组多选缺少按 data-gid 收集勾选态"
    assert "selectedGroupSessionIds" in list_js, "缺少把测试组解析为会话 id 列表的函数"


def test_frontend_testset_run_is_backend_driven():
    """测试集运行必须由后端驱动：前端只一次性启动运行并订阅事件流对账进度。

    若退回前端逐条驱动（循环调 runTest 逐步等待），页面刷新/关闭会中断后续
    步骤。防回归：app.js 必须调用 runTestsetApi（整场运行一次启动）并由
    handleTestsetEvent 消费 /events 的 testset 事件推进进度，而不是轮询
    pollTestsetRun。
    """
    src = _read_module("testset_run")
    assert "runTestsetApi(" in src, "testset_run.js 未调用 runTestsetApi 启动测试集运行"
    assert "handleTestsetEvent" in src, (
        "testset_run.js 未实现 testset 事件推进 handleTestsetEvent"
    )
    assert "pollTestsetRun(" not in src, (
        "testset_run.js 仍轮询 pollTestsetRun（已改事件驱动）"
    )


def test_frontend_testset_summary_counts_assertion_failures():
    """测试集运行总结须单独计数断言未通过（✗），与步骤/会话错误区分。

    曾出现：结果表格 3 个会话断言 ✗，但最终总结「运行完成（N 步）」没提任何
    失败——断言失败只落在结果单元格、不改会话 status，总结若只数
    status=="error" 就永远显示「错误 0」，误导用户以为断言全过。
    """
    src = _read_module("testset_run")
    assert "assertFails" in src, "总结未计算断言未通过数量"
    assert "条断言未通过" in src, "总结文案未包含断言未通过计数"
    # 表格行尾计数也要标注断言 ✗（与「错误 N」的 status 语义区分）
    assert "断言 ✗" in src, "结果表格行尾未单独标注断言 ✗ 计数"


def test_frontend_verdict_detail_shows_review_output():
    """verdict 标记须可点击查看 LLM 评审详情（原始输出 + 评审输入）。

    评审输出（raw）与评审输入（context_text）随 verdict 落库后，前端要在结果
    表格 / 报告详情里提供详情入口——点 ✓/✗/⚠ 打开弹窗看 LLM 原始返回与喂给
    LLM 的上下文。LLM 返回是数据不是代码，必须经 textContent 落进 <pre>（防
    XSS），不得用 innerHTML 拼接。
    """
    src = _read_module("testset_run")
    assert "verdict-chip" in src, "verdict 标记未渲染为可点击的详情入口按钮"
    assert "data-vkey" in src, "verdict 标记缺少定位用的 data-vkey"
    assert "resolveVerdict" in src, "缺少按 vkey 从 run 定位 verdict 的函数"
    assert "openVerdictDetail" in src, "缺少 verdict 详情弹窗"
    assert "评审输出（LLM 原始返回）" in src, "详情弹窗未展示评审输出"
    assert "评审输入（喂给评审 LLM 的上下文）" in src, "详情弹窗未展示评审输入"
    assert "pre.textContent" in src, "评审输出/输入未用 textContent 落盘（XSS 风险）"


def test_frontend_no_parse_int_on_user_input():
    """用户数字输入不得用 parseInt 截断解析。

    parseInt("1.5") → 1：用户填 1.5 会被静默截断成 1（数量、断言最少/最多字数
    都会悄悄改值）。须用 Number()（配合 Number.isInteger 拒绝小数），解析失败
    报错而不是截断。曾有三处 parseInt：group_list.js 新增会话数量 / 组会话数量、
    testset_editor.js min_len/max_len 断言值（拆分后位于编辑器模块）。
    """
    for name in ("group_list", "testset_list", "testset_editor"):
        assert "parseInt(" not in _read_module(name), (
            f"{name}.js 仍用 parseInt 解析用户输入（会静默截断小数）"
        )


def test_frontend_refresh_groups_defensive():
    """组列表刷新须捕获失败并降级，初始化不被单点失败阻塞。

    refreshGroups 曾无 try/catch：初始化 Promise.all 中任一 reject 会让
    pollPending() 永不执行（在途消息条全部失效）。须：refreshGroups 内部捕获
    降级（错误文案可见），且初始化改用 Promise.allSettled 隔离各步失败。
    """
    gl_js = _read_module("group_list")
    app_js = _read_module("app")
    assert "加载测试组失败" in gl_js, "refreshGroups 缺少失败降级文案"
    assert "Promise.allSettled" in app_js, "初始化未用 Promise.allSettled 隔离失败"


def test_frontend_load_options_before_initial_group_list():
    """初始化须先加载配置档案，再渲染测试组列表。

    组徽标经 confName 按 state.confs 映射档案名称（未命中回退原始 id）；
    loadOptions 与 refreshGroups 并行时，首帧渲染可能赶在档案就绪之前，把
    档案 id 直接显示成原始值（如 eadfcf07…），须手动刷新才恢复名称。
    """
    app_js = _read_module("app")
    assert app_js.index("await loadOptions()") < app_js.index("Promise.allSettled"), (
        "loadOptions 须先于 Promise.allSettled 完成（refreshGroups 首帧依赖 state.confs）"
    )


def test_frontend_event_driven_feedback():
    """前端反馈层必须为事件驱动：无轮询器，统一逐会话反馈 + 消费者注册表。

    曾有三处 setInterval async tick（pollRun / pollTestsetRun / pollPending），
    慢后端下 tick 耗时超间隔时请求堆积。现改为订阅 /events：逐会话反馈收敛到
    applySessionFeedback（手动群发与测试集运行共用同一路径），手动运行经
    testConsumers 注册表接收 session_done / test_done 事件。
    """
    app_js = _read_module("app")
    events_js = _read_module("events")
    api_js = _read_module("api")
    assert "function applySessionFeedback(" in events_js, (
        "缺少统一逐会话反馈 applySessionFeedback（手动/测试集共用）"
    )
    assert "const testConsumers = new Map()" in events_js, (
        "缺少手动运行消费者注册表 testConsumers"
    )
    assert "startPolling(" not in app_js, (
        "app.js 仍含轮询辅助 startPolling（已改事件驱动）"
    )
    assert "setInterval(" not in app_js, "app.js 仍含 setInterval 轮询"
    assert "subscribeEvents(" in api_js, "api.js 缺少 SSE 订阅封装 subscribeEvents"


def test_frontend_event_driven_robustness():
    """事件驱动的健壮性修复：并发/历史残留不互相污染，状态有界、不泄漏。

    对应代码审查 HIGH（并发测试集运行污染前端单槽状态）与 MEDIUM（reconcile
    404 时 consumer 泄漏、runReports 无界增长、历史刷新乱序）修复：
    - 测试集步骤去重键带 runId 前缀（不同运行同序号步骤互不干扰）；
    - reconcile 拉取失败时释放 consumer（防 Map 永久泄漏）；
    - 历史刷新带序号，乱序迟到响应被丢弃（防历史回退）；
    - 暂存报告有上限，超出丢最旧（防内存无界增长）。
    """
    app_js = _read_module("app")
    events_js = _read_module("events")
    run_js = _read_module("testset_run")
    assert "`${runId}:${i}`" in run_js, "步骤去重键未带 runId 前缀（并发运行会互污染）"
    assert "testConsumers.delete(tid)" in events_js, (
        "reconcile 失败时未释放 consumer（泄漏）"
    )
    assert "const historySeq = new Map()" in app_js, "缺少历史刷新序号守卫 historySeq"
    assert "historySeq.get(id) !== seq" in app_js, "历史刷新未丢弃乱序迟到响应"
    assert "const MAX_STASHED_REPORTS = 20" in run_js, "缺少暂存报告上限常量"
    assert "Object.keys(state.runReports)" in run_js, "报告暂存缺少有界清理"


def test_frontend_report_on_demand():
    """测试集结果收集但不自动弹窗：终态暂存 runReports，经「查看报告」按需查看。

    曾自动弹结果表格；现改为终态把报告暂存 state.runReports、显示「查看报告」
    按钮，用户自主查看报告或会话窗口的实际结果。防回归：handleTestsetEvent
    函数体内不得直接调用 showTestsetResults（只能暂存），弹窗只出现在
    viewTestsetRun（最近运行「查看」）与「查看报告」按钮两处按需路径。
    """
    html = _read_html()
    app_js = _read_module("app")
    run_js = _read_module("testset_run")
    assert 'id="btn-view-report"' in html, "index.html 缺少「查看报告」按钮"
    assert "runReports" in run_js, "testset_run.js 缺少运行报告暂存 runReports"
    assert "state.runReports[runId] = run" in run_js, "终态未把报告暂存进 runReports"
    assert '$("btn-view-report")' in app_js, "「查看报告」按钮未绑定"
    # handleTestsetEvent 与 showTestsetResults 是工厂内顺序定义的两个函数；
    # 前者函数体（到后者定义前）不得直接调用 showTestsetResults（只能暂存）
    start = run_js.index("function handleTestsetEvent(")
    end = run_js.index("function showTestsetResults(", start)
    assert "showTestsetResults" not in run_js[start:end], (
        "handleTestsetEvent 直接弹结果表格（应改为暂存 + 按需查看）"
    )


def test_frontend_render_panels_run_overview_bound():
    """renderPanels 调用的 updateRunOverview 必须有 app.js 模块级绑定。

    曾把 updateRunOverview 实现移到 testset_run.js 后只经 env 对象传给
    group_list.js，app.js 自身未再绑定——renderPanels 每次调用都抛
    ReferenceError，且抛错点在面板已 append 之后：表现是「打开全部」每次
    只开组内第 1 个会话（openAll 循环中断）、会话按钮标签不更新（收尾的
    renderGroupList 未执行）、但面板却已可见。
    """
    app_js = _read_module("app")
    assert "const updateRunOverview = testsetRun.updateRunOverview" in app_js, (
        "renderPanels 调用的 updateRunOverview 缺少 app.js 模块级绑定"
    )


def test_frontend_open_all_toggles_label_and_closes():
    """组「打开全部」按钮须按组内会话打开状态切换「打开全部 / 关闭全部」。

    组内会话全部打开时按钮显示「关闭全部」、点击关闭本组全部会话（其他组
    会话不受影响）；任一会话被单独关闭后按钮回到「打开全部」、点击只补开
    尚未打开的会话。group_list.js 按 allOpen 生成按钮标签，app.js 的
    openAll 按同一判定走关闭 / 补开分支。
    """
    app_js = _read_module("app")
    gl_js = _read_module("group_list")
    assert '${allOpen ? "关闭全部" : "打开全部"}' in gl_js, (
        "组按钮未按 allOpen 切换打开全部/关闭全部标签"
    )
    assert "sessions.every((s) => state.openIds.includes(s.id))" in gl_js, (
        "组按钮标签缺少「全部已打开」判定"
    )
    assert "sessions.every((s) => state.openIds.includes(s.id))" in app_js, (
        "openAll 未计算组内会话是否全部打开"
    )
    assert "state.openIds = state.openIds.filter((id) => !ids.has(id))" in app_js, (
        "全部打开时 openAll 未关闭本组会话"
    )
    assert "state.pinnedIds = state.pinnedIds.filter((id) => !ids.has(id))" in app_js, (
        "关闭本组会话时未同步清除置顶"
    )


def test_frontend_group_head_simplified():
    """组头首行只留名字与右侧按钮（打开全部 / 编辑），会话数徽标与删除入口迁移。

    组头曾同时挤着会话数徽标、＋新增、✎编辑、✕删除，名字可显示长度过小。
    现会话数徽标移到 group-meta 与平台/档案/安全徽标同行；＋（新增会话）与
    ✕（删除组）按钮移除——新增会话走编辑弹窗的「会话数量」（保存时少于目标
    值自动补建，编辑里有相同功能），删除组入口移到编辑弹窗内（danger 按钮 →
    deleteGroup 确认流程）。超长组名以 flex-grow 占满剩余宽度，悬停 title 可见
    完整名称。
    """
    gl_js = _read_module("group_list")
    css = (PLUGIN_DIR / "pages" / "testbench" / "style.css").read_text(encoding="utf-8")
    assert "${countBadge}${platformBadge}${confBadge}${warnBadge}" in gl_js, (
        "会话数徽标未与平台/档案/安全徽标同处 group-meta"
    )
    assert 'data-action="add"' not in gl_js, "组头不应保留 ＋ 新增会话按钮"
    assert 'data-action="delete-group"' not in gl_js, "组头不应保留 ✕ 删除组按钮"
    assert "删除测试组…" in gl_js, "编辑弹窗缺少删除测试组入口"
    assert "promptAddSessions" not in gl_js, "promptAddSessions 已无用应删除"
    assert 'group-name" title="${escapeHtml(g.name)}"' in gl_js, (
        "组名缺少悬停显示完整名称的 title"
    )
    assert "flex: 1 1 auto" in css, "组名未以 flex-grow 占满组头剩余宽度"
    assert "点组名右侧「＋」添加" not in gl_js, (
        "空组提示仍指引已移除的「＋」按钮（新增会话现走编辑弹窗会话数量）"
    )


def test_frontend_group_dialog_new_fields():
    """组配置弹窗须提交消息类型 / 绑定虚拟群聊字段，且不嵌管理弹窗。

    群聊虚拟会话新增 message_type / chat_group_id 两字段；auto@ 是发送时选项
    （群发栏 / 测试集消息级配置），不属于组/会话配置——弹窗不得再提交。组编辑
    弹窗提交时须带上绑定群聊（群聊模式下才传）；「管理身份与群聊」须是跳转链接
    （hideModal + switchToIdentities），而不是在组弹窗内嵌第二层管理弹窗。
    """
    gl_js = _read_module("group_list")
    assert "message_type: selType.value || null" in gl_js, "组弹窗未提交消息类型"
    assert "chat_group_id: isGroup ? selChatGroup.value || null : null" in gl_js, (
        "组弹窗未按群聊模式提交绑定虚拟群聊"
    )
    assert "message_type: selType.value || null" in gl_js, "会话弹窗未提交消息类型覆盖"
    assert "chat_group_id: selChatGroup.value || null" in gl_js, (
        "会话弹窗未提交绑定群聊覆盖"
    )
    # auto@ 不再是组/会话配置：弹窗不得再提交该字段（防回归）
    assert "auto_at:" not in gl_js, "组/会话弹窗仍提交 auto_at（应为发送时选项）"
    # 不嵌管理弹窗：链接跳转视图而非再开弹窗
    assert "管理身份与群聊 →" in gl_js, "组弹窗缺少「管理身份与群聊」跳转链接"
    assert "hideModal()" in gl_js, "跳转链接未先关闭组弹窗"
    assert "switchToIdentities()" in gl_js, "跳转链接未切到身份与群聊视图"


def test_frontend_panel_view_toggle():
    """视图切换（LLM 历史 / 消息流）须全局统一控制。

    切换按钮从单会话页眉移到与「轮次对齐」开关同一行右侧（#view-toggle），统一
    控制全部已打开的会话：index.html 须含该按钮；app.js 须实现 setGlobalView
    （统一切换并加载对应视图）与 loadStream（拉取消息流渲染），视图判断用
    state.globalView；面板页头不再有 per-panel view-toggle 按钮。
    """
    html = _read_html()
    app_js = _read_module("app")
    assert 'id="view-toggle"' in html, "index.html 缺少全局视图切换按钮"
    assert 'data-action="view-toggle"' not in app_js, "面板页头 view-toggle 未移除"
    assert "function setGlobalView(" in app_js, "缺少 setGlobalView 全局视图切换函数"
    assert "async function loadStream(" in app_js, "缺少 loadStream 消息流加载函数"
    assert "streamCache" in app_js, "app.js 未使用消息流缓存 streamCache"
    assert 'state.globalView === "stream"' in app_js, "未按 globalView 判断当前视图"
    assert '$("view-toggle").addEventListener' in app_js, "全局视图切换按钮未绑定事件"


def test_frontend_identities_view():
    """rail 第三视图「身份与群聊」入口与列表容器齐全。

    index.html 须含 data-view="identities" 的 rail 按钮与 #identity-list /
    #chat-group-list 两个列表；identity_list.js 存在且导出 createIdentityList
    （由 app.js 装配，作为 rail 第三视图的列表管理）。
    """
    html = _read_html()
    assert 'data-view="identities"' in html, "index.html 缺少身份与群聊视图入口"
    assert 'id="identity-list"' in html, "index.html 缺少身份列表容器"
    assert 'id="chat-group-list"' in html, "index.html 缺少虚拟群聊列表容器"
    src = _read_module("identity_list")
    assert "export function createIdentityList(env)" in src, (
        "identity_list.js 缺少 createIdentityList 工厂"
    )
    assert "listIdentities" in src, "身份列表未拉取身份接口"
    assert "listChatGroups" in src, "群聊列表未拉取虚拟群聊接口"


def test_frontend_identities_tab_split():
    """左侧身份与群聊须以 tab 分开为两个列表，一次只显示一个。

    身份膨胀后群聊列表被长身份列表挤到下方不好找，故 identities-card 内以
    tab 拆分：index.html 须含 data-tab 两个 tab 按钮与 data-pane 两个列表
    容器；identity_list.js 须提供 switchIdentityTab 按 dataset.tab 分发、
    pane 按 data-pane 互斥显隐。
    """
    html = _read_html()
    assert 'data-tab="identity"' in html, "index.html 缺少身份 tab"
    assert 'data-tab="chatgroup"' in html, "index.html 缺少群聊 tab"
    src = _read_module("identity_list")
    assert "switchIdentityTab(" in src, "identity_list.js 缺少 tab 切换函数"
    assert "pane.dataset.pane !== tab" in src, "tab 切换未按 pane 互斥显隐"
    assert ".identities-card .tab-btn" in src, "tab 按钮未绑定点击"


def test_frontend_show_view_three_state():
    """showView 须覆盖会话 / 测试集 / 身份与群聊三态互斥。

    新增 rail 第三视图后，左侧三张卡片（.groups-card / .testsets-card /
    .identities-card）必须按 view 互斥显隐，rail 按钮 active 同步切换；
    右侧 .sessions-view / .testsets-view / .chat-group-view 三个工作区同样互斥，
    切到 identities 时显示群聊编辑视图（renderChatGroupView 渲染）。
    """
    app_js = _read_module("app")
    assert 'view !== "sessions"' in app_js, "showView 未按 sessions 控制卡片"
    assert 'view !== "testsets"' in app_js, "showView 未按 testsets 控制卡片"
    assert 'view !== "identities"' in app_js, "showView 未按 identities 控制卡片"
    assert ".identities-card" in app_js, "showView 未引用 .identities-card"
    assert '".chat-group-view"' in app_js, "showView 未控制右侧群聊编辑视图"


def test_frontend_chat_group_editor_view():
    """群聊编辑须是右侧独立视图（搜索成员加入），而非弹窗多选。

    创建群聊不再让用户在多选弹窗里挑成员（成员多时窗口放不下）：index.html
    须含 #chat-group-view 编辑视图容器（#cg-search 搜索框 / #cg-member-list
    成员列表）；identity_list.js 须提供 openChatGroupView / renderChatGroupView /
    renderSearchResults / addMember / removeMember；app.js 的 showView 切到
    identities 时须渲染该视图。
    """
    html = _read_html()
    assert 'id="chat-group-view"' in html, "index.html 缺少群聊编辑视图容器"
    assert 'id="cg-search"' in html, "index.html 缺少成员搜索框"
    assert 'id="cg-member-list"' in html, "index.html 缺少成员列表容器"
    src = _read_module("identity_list")
    assert "openChatGroupView(" in src, "缺少打开编辑视图入口"
    assert "renderChatGroupView(" in src, "缺少编辑视图渲染函数"
    assert "renderSearchResults(" in src, "缺少搜索过滤函数"
    assert "addMember(" in src, "缺少加入成员函数"
    assert "removeMember(" in src, "缺少移除成员函数"
    app_js = _read_module("app")
    assert "renderChatGroupView" in app_js, "showView 切到 identities 时未渲染编辑视图"


def test_frontend_chat_group_create_name_only():
    """创建群聊弹窗只填名称，不再内嵌成员多选。

    成员多时多选 checkbox 在弹窗里放不下，故创建只写名称、创建后到右侧编辑
    视图搜索加入。identity_list.js 须：createChatGroup 提交仅含 name 的 payload、
    弹窗里不再渲染成员 checkbox（移除「成员（勾选加入）」）。
    """
    src = _read_module("identity_list")
    assert "createChatGroup({ name })" in src, "创建群聊未只提交名称"
    assert "成员（勾选加入）" not in src, "创建/编辑弹窗仍渲染成员多选"
    assert "inpName.value.trim()" in src, "创建群聊名称校验缺失"
    assert 'okText: "创建"' in src, "新建群聊弹窗缺少创建按钮"


def test_frontend_chat_group_member_nickname():
    """群聊编辑视图的成员与搜索结果须展示昵称（sender_name）。

    群成员多时按昵称识别比按发送者 ID 直观：renderMemberList 与
    renderSearchResults 的行内 badge 须优先展示 sender_name（无昵称回退
    sender_id）。
    """
    src = _read_module("identity_list")
    assert "ident.sender_name || ident.sender_id" in src, (
        "成员行未优先展示昵称（sender_name 回退 sender_id）"
    )
    assert src.count("const nick = ") >= 2, "昵称回退逻辑未同时用于成员行与搜索结果行"


def test_frontend_chat_group_member_admin_marks():
    """群聊编辑视图成员行须同时显示昵称与发送者ID，管理员挂管理员与警告徽标。

    成员行除昵称外还要可见发送者ID（title 为「发送者ID」的徽标，身份列表的
    title 是 sender_id/sender_name，不会误命中）；管理员成员须与身份列表一致
    挂「管理员」+「⚠ 危险」徽标（可调用需管理员权限的危险工具）。
    """
    src = _read_module("identity_list")
    assert 'title="发送者ID">${escapeHtml(ident.sender_id)}' in src, (
        "成员行缺少发送者ID徽标"
    )
    # 身份列表 1 处 + 群成员行 2 处（管理员 + 危险警告）
    assert src.count("ident.is_admin") >= 3, "管理员标记未同时用于身份列表与群成员行"


def test_frontend_identity_drag_join():
    """身份须可拖拽到群聊编辑视图加入群成员。

    拖拽加入是搜索加入的快捷路径：renderIdentityList 的身份条目须 draggable
    并在 dragstart 把身份 id 写入 dataTransfer；#cg-members 成员区须监听
    dragover（preventDefault + dropEffect）与 drop（读 id 调 addMember）。
    """
    src = _read_module("identity_list")
    assert "item.draggable = true" in src, "身份条目未设为可拖拽"
    assert 'e.dataTransfer.setData("text/plain", ident.id)' in src, (
        "dragstart 未把身份 id 写入 dataTransfer"
    )
    assert 'addEventListener("dragover"' in src, "成员区未监听 dragover"
    assert 'addEventListener("drop"' in src, "成员区未监听 drop"
    assert "void addMember(mid)" in src, "drop 未调用 addMember 加入成员"
    html = _read_html()
    assert "cg-drag-hint" in html, "index.html 缺少拖拽提示"


def test_frontend_testset_row_identity():
    """测试集消息行须收集可选发送身份（sender_id / sender_name）与自动@。

    测试集每条消息可指定身份（动态身份）与是否自动@：collectEditorRows 须从
    行内身份下拉 collectSender、读取行内 @ 勾选并合并进消息 dict；导出信封解析
    须保留可选 sender 与 auto_at 字段。
    """
    editor_js = _read_module("testset_editor")
    pure_js = _read_module("pure")
    assert "collectSender(" in editor_js, "缺少行内身份收集函数 collectSender"
    assert "const sender = collectSender(" in editor_js, (
        "collectEditorRows 未收集行内身份"
    )
    # 行收集的纯逻辑在 pure.js（collectEditorRows），编辑器只做 DOM 读取薄包装
    assert "const message = { text, rules" in pure_js, (
        "pure.js 收集的消息未携带 text 与 rules 列表"
    )
    assert "sender.sender_id !== undefined" in pure_js, (
        "pure.js 收集的消息未合并可选 sender 字段"
    )
    assert "Object.assign(message, row.sender)" in pure_js, (
        "pure.js 收集的消息未合并 sender / auto_at 字段"
    )
    assert "row.isCommand" in pure_js and "message.is_command = true" in pure_js, (
        "pure.js 命令标记勾选未收集进消息"
    )
    assert "message.auto_at = !!row.autoAt" in pure_js, "pure.js 收集的消息未带 auto_at"
    assert "message.sender_id = m.sender_id" in pure_js, "导入信封未保留 sender_id"
    assert "message.sender_name = m.sender_name" in pure_js, (
        "导入信封未保留 sender_name"
    )
    assert "message.auto_at = m.auto_at" in pure_js, "导入信封未保留 auto_at"
    assert "atCb.checked = !msg || msg.auto_at !== false" in editor_js, (
        "消息行 @ 勾选未按 auto_at 缺省开启渲染"
    )


def test_frontend_rules_editor():
    """消息行须支持多断言编辑：规则类型下拉 + 值输入 + 收集 + 行内样式。

    M1 把单条 rule 扩展为 rules 列表：renderMsgRow 渲染 .ts-msg-rules 内的
    多条 .ts-msg-rule 行（类型下拉 / 值输入 / 删除），collectRules 反向收集，
    buildRule 对需要值的类型做校验（min_len/max_len 整数）。
    """
    editor_js = _read_module("testset_editor")
    pure_js = _read_module("pure")
    assert "RULE_TYPES" in editor_js, "缺少断言类型定义 RULE_TYPES"
    assert "buildRuleRow(" in editor_js, "缺少单条断言编辑行构建 buildRuleRow"
    assert "rulesBox.appendChild(buildRuleRow(" in editor_js, "消息行未渲染多断言列表"
    assert "export function collectRules(" in pure_js, (
        "多断言收集 collectRules 未在 pure.js"
    )
    assert 'querySelectorAll(".ts-msg-rule")' in editor_js, (
        "collectEditorRows 未遍历 .ts-msg-rule 行读取输入"
    )
    assert 'wrap.className = "ts-msg-rule"' in editor_js, "断言行未使用 .ts-msg-rule 类"
    assert "ruleTypeLabel(" in editor_js, "缺少断言类型中文名映射"
    css = (PLUGIN_DIR / "pages" / "testbench" / "style.css").read_text(encoding="utf-8")
    assert ".ts-msg-rules" in css, "style.css 缺少多断言列表样式"
    assert ".ts-msg-rule-type" in css, "style.css 缺少断言类型下拉宽度样式"
    assert ".ts-msg-rule-value" in css, "style.css 缺少断言值输入样式"


def test_frontend_testset_identity_config():
    """测试集编辑窗口须含身份配置（single 单一身份 / pool 身份池）。

    index.html 须含 #ts-identity-mode / #ts-identity-ref / #ts-pool-ref；
    testset_editor.js 须经 initIdentityConfig / refreshRowSenders 按模式切换
    行内身份下拉来源，collectIdentityConfig 收集身份配置与内联快照。
    """
    html = _read_html()
    assert 'id="ts-identity-mode"' in html, "index.html 缺少身份模式下拉"
    assert 'id="ts-identity-ref"' in html, "index.html 缺少单一身份下拉"
    assert 'id="ts-pool-ref"' in html, "index.html 缺少身份池（群聊）下拉"
    editor_js = _read_module("testset_editor")
    assert "initIdentityConfig(" in editor_js, "缺少身份配置初始化 initIdentityConfig"
    assert "refreshIdentityFields()" in editor_js, "缺少身份字段显隐刷新"
    assert "refreshRowSenders()" in editor_js, "缺少行内身份下拉重建 refreshRowSenders"
    assert "collectIdentityConfig()" in editor_js, "缺少身份配置收集"
    assert "buildIdentitySnapshot(" in editor_js, "缺少单身份快照构建"
    assert "buildPoolSnapshot(" in editor_js, "缺少身份池快照构建"
    assert 'identity_mode: "pool"' in editor_js, "池模式收集未写 identity_mode"
    assert "identity_snapshot" in editor_js, "保存 payload 缺少 identity_snapshot"
    assert "pool_snapshot" in editor_js, "保存 payload 缺少 pool_snapshot"
    css = (PLUGIN_DIR / "pages" / "testbench" / "style.css").read_text(encoding="utf-8")
    assert ".ts-identity" in css, "style.css 缺少身份配置行样式"


def test_frontend_envelope_v2():
    """导出/导入信封 v2：携带身份配置（identity / pool），解析兼容 v1。

    M1 信封升到 version 2：导出按身份模式写 envelope.identity 或
    envelope.pool；parseTestsetEnvelope 接受 v1（单条 rule → rules）与 v2
    （rules / is_command / identity / pool），版本号高于 2 拒绝。
    """
    editor_js = _read_module("testset_editor")
    pure_js = _read_module("pure")
    assert "export const EXPORT_VERSION = 2" in pure_js, "信封版本未升至 v2"
    assert "envelope.identity = identity.identity_snapshot" in editor_js, (
        "single 模式导出未携带 identity 快照"
    )
    assert "envelope.pool = identity.pool_snapshot" in editor_js, (
        "pool 模式导出未携带身份池"
    )
    # 导入解析（parseTestsetEnvelope）在 pure.js
    assert 'identity_mode: data.pool ? "pool" : "single"' in pure_js, (
        "导入解析未按 envelope.pool 判定身份模式"
    )
    assert "data.version > EXPORT_VERSION" in pure_js, "导入未拒绝高于当前版本的信封"
    assert "for (const r of m.rules)" in pure_js, "导入未处理 v2 rules 列表"
    assert "m.rule != null" in pure_js, "导入未兼容 v1 单条 rule"


def test_frontend_broadcast_identity_selector():
    """群发栏身份选择器须把选择身份带进发送 payload。

    群发/单发消息都可选身份：app.js 须经 selectedBroadcastOptions() 读取
    #run-sender 下拉并把 sender 合入 runTest payload；index.html 须含该下拉。
    """
    html = _read_html()
    app_js = _read_module("app")
    assert 'id="run-sender"' in html, "index.html 缺少群发栏身份选择器"
    assert "function selectedBroadcastOptions()" in app_js, "缺少群发栏选项读取函数"
    assert "...selectedBroadcastOptions()" in app_js, "群发/单发 payload 未合并选项"


def test_frontend_broadcast_auto_at():
    """群发栏「自动@」选项须带进发送 payload 且默认开启。

    auto@ 改为发送时选项：index.html 的群发栏须含 #run-auto-at 勾选框（默认
    checked），app.js 的 runTest payload 须带 auto_at 读取该勾选框。
    """
    html = _read_html()
    app_js = _read_module("app")
    assert 'id="run-auto-at"' in html, "index.html 缺少群发栏自动@勾选框"
    assert 'id="run-auto-at" type="checkbox" checked' in html, "自动@勾选默认未开启"
    assert 'auto_at: $("run-auto-at") ? $("run-auto-at").checked : true' in app_js, (
        "群发/单发 payload 未带自动@选项"
    )


def test_frontend_identity_admin_field():
    """身份表单须提交 is_admin，身份列表须按管理员身份显示徽标。

    身份实体新增「是否管理员」配置：identity_list.js 的 openIdentityForm 表单
    须含管理员 checkbox 并把 is_admin 合入创建/更新 payload；管理员单选框须
    checkbox 在前、与标签同行（field() 的纵向布局会让方框独占一行，故不走
    field()）；renderIdentityList 须在身份 meta 区按 is_admin 渲染管理员徽标。
    管理员身份可调用危险工具，列表徽标旁须有警告提示，表单勾选管理员时须显示
    内联警告条。
    """
    src = _read_module("identity_list")
    assert "is_admin: inpAdmin.checked" in src, "身份表单未提交 is_admin"
    assert "管理员（发送时自动按管理员身份设置角色）" in src, "身份表单缺少管理员字段"
    assert "fAdmin.append(inpAdmin" in src, "管理员单选框未与标签同行（checkbox 在前）"
    assert 'fAdmin.className = "settings-field"' in src, "管理员行未复用 settings-field"
    assert 'inpAdmin.type = "checkbox"' in src, "管理员字段不是 checkbox"
    assert "ident.is_admin" in src, "身份列表未按 is_admin 渲染管理员徽标"
    # 管理员身份旁的警告提示（列表徽标 + 表单内联警告条）
    assert "⚠ 危险" in src, "身份列表缺少管理员危险警告徽标"
    assert 'class="badge warn"' in src, "危险警告未用 .badge.warn 样式"
    assert "adminWarn.hidden = !inpAdmin.checked" in src, "表单未按勾选状态切换警告条"
    assert "adminWarn" in src, "表单缺少管理员内联警告条"


def test_frontend_group_security_warning_bar():
    """组/会话配置弹窗须内联安全警告条（选中启用工具的配置即时显示）。

    创建/编辑组与会话级 conf 覆盖都检查：group_list.js 须实现 confHasTools
    （按 state.confs 的 has_callable_tools 判定，空值按默认配置）与
    buildToolWarningBar（.dialog-warn 警告条，默认 hidden）；组弹窗与会话弹窗
    都须在配置档案下拉 change 时刷新警告条显隐；会话弹窗按「有效配置」判定
    （显式默认 / 显式档案 / 继承组链）。
    """
    gl_js = _read_module("group_list")
    assert "has_callable_tools" in gl_js, "未引用 listConfs 的工具标志"
    assert "function confHasTools(" in gl_js, "缺少 confHasTools 判定函数"
    assert "function buildToolWarningBar()" in gl_js, "缺少警告条构建函数"
    assert 'className = "dialog-warn"' in gl_js, "警告条未用 .dialog-warn 样式"
    assert 'selC.addEventListener("change", refreshWarn)' in gl_js, (
        "组弹窗配置下拉未绑定警告刷新"
    )
    assert "warnBar," in gl_js, "会话弹窗未把警告条加入表单"
    assert 'session.conf_id || group.conf_id || "default"' in gl_js, (
        "会话弹窗未按继承组链解析有效配置"
    )


def test_frontend_group_security_badge():
    """组列表须按后端 security_warning 实时标记危险组。

    renderGroupList 渲染组条目时须引用 g.security_warning 显示警告徽标
    （后端每次列表实时重算，标记派生不持久化）。
    """
    gl_js = _read_module("group_list")
    assert "g.security_warning" in gl_js, "组列表未按 security_warning 渲染警告标记"
    assert "⚠ 工具" in gl_js, "组列表缺少工具警告徽标文案"


# ---------- M2 评审层前端：LLM 规则 / 最终断言 / Profile 管理 / verdict 渲染 ----------


def test_frontend_llm_rule_row():
    """消息断言须支持 LLM 评审规则行（类型下拉选 LLM → profile + context 下拉）。

    M2 把断言规则扩展出 kind=llm：testset_editor.js 的 RULE_TYPES 须含
    ["llm", "LLM 评审"]；buildRuleRow 须在类型切换 LLM 时用 .ts-msg-rule-llm
    字段区（profile / context 两个下拉）替代值输入；collectRules / buildRule
    收集为 {kind: "llm", profile_id, context?}；保存前校验未选 profile 的
    LLM 规则（否则被静默丢弃）。
    """
    editor_js = _read_module("testset_editor")
    pure_js = _read_module("pure")
    assert '["llm", "LLM 评审"]' in editor_js, "RULE_TYPES 缺少 LLM 评审类型"
    assert "buildProfileSelect(" in editor_js, "缺少评审 Profile 下拉构建"
    assert "buildContextSelect(" in editor_js, "缺少上下文模式下拉构建"
    assert 'className = "ts-msg-rule-llm"' in editor_js, "缺少 LLM 字段区容器"
    assert 'sel.value === "llm"' in editor_js, "类型切换未按 llm 显示 LLM 字段区"
    # 规则构造（buildRule）在 pure.js，编辑器把 LLM 行的 profile/context 下拉读入
    assert 'const rule = { kind: "llm", profile_id: profileId }' in pure_js, (
        "LLM 规则未收集为 {kind: 'llm', profile_id}"
    )
    assert 'llmBox.querySelector(".ts-msg-rule-profile")' in editor_js, (
        "编辑器未读取 LLM 规则 profile 下拉"
    )
    assert "未选择评审 Profile" in editor_js, "保存前未拦截未选 profile 的 LLM 规则"


def test_frontend_final_rules_editor():
    """测试集编辑窗口须含最终断言（跨轮）编辑区。

    index.html 须含 #ts-final-rules 容器与「＋ 添加」按钮；testset_editor.js
    须实现 buildFinalRuleRow（类型 / 值 / LLM 字段 / scope 范围输入）与
    collectFinalRules（scope 经 parseScope 解析为 "all" 或 {from, to}），
    保存 / 导出 payload 携带 final_rules，导入解析也保留该字段。
    """
    html = _read_html()
    editor_js = _read_module("testset_editor")
    pure_js = _read_module("pure")
    assert 'id="ts-final-rules"' in html, "index.html 缺少最终断言容器"
    assert 'id="btn-ts-add-final"' in html, "index.html 缺少添加最终断言按钮"
    assert "buildFinalRuleRow(" in editor_js, "缺少最终断言行构建"
    assert "collectFinalRules()" in editor_js, "缺少最终断言收集"
    assert "export function parseScope(" in pure_js, "scope 范围解析未在 pure.js"
    assert "final_rules: finalRules" in editor_js, "保存 payload 缺少 final_rules"
    assert "final_rules: collectFinalRules()" in editor_js, "导出信封缺少 final_rules"
    assert "result.final_rules.push(item)" in pure_js, "导入解析未保留 final_rules"
    assert "final_rules: parsed.final_rules" in editor_js, (
        "导入未把 final_rules 传给 createTestset"
    )
    css = (PLUGIN_DIR / "pages" / "testbench" / "style.css").read_text(encoding="utf-8")
    assert ".ts-final-rule" in css, "style.css 缺少最终断言行样式"


def test_frontend_reviewer_profile_form():
    """评审 Profile 管理：列表 tab + 新建/编辑/删除表单（支持多个）。

    testset_list.js 须经 listReviewers 拉取 profile、renderReviewerList 渲染
    列表（未配置提示 + 新建入口）、openProfileForm 表单（provider / 模型 /
    提示词 / 输出契约指标编辑器 buildMetricsEditor / collectMetrics），保存走
    createReviewer / updateReviewer、删除走 deleteReviewers；编辑器保留行内
    profile 下拉重建 refreshAllProfileSelects；state.js 须有 reviewers；
    app.js 的 loadOptions 须预载 reviewers。
    """
    list_js = _read_module("testset_list")
    editor_js = _read_module("testset_editor")
    state_js = _read_module("state")
    app_js = _read_module("app")
    assert "listReviewers" in list_js, "列表未拉取评审 Profile"
    assert "renderReviewerList(" in list_js, "缺少 Profile 列表渲染"
    assert "暂无评审 Profile" in list_js, "缺少未配置提示文案"
    assert "新建评审 Profile" in list_js, "缺少新建 Profile 入口"
    assert "openProfileForm(" in list_js, "缺少 Profile 表单函数"
    assert "buildMetricsEditor(" in list_js, "缺少输出指标编辑器"
    assert "collectMetrics(" in list_js, "缺少输出指标收集"
    assert "METRIC_TYPES" in list_js, "缺少指标类型定义"
    assert "createReviewer(payload)" in list_js, "新建 Profile 未调 createReviewer"
    assert "updateReviewer(existing.id, payload)" in list_js, (
        "编辑 Profile 未调 updateReviewer"
    )
    assert "deleteReviewers([profile.id])" in list_js, (
        "删除 Profile 未调 deleteReviewers"
    )
    assert "{{metrics}}" in list_js, "提示词缺少 {{metrics}} 占位符提示"
    assert "refreshAllProfileSelects" in editor_js, "编辑器缺少行内 profile 下拉重建"
    assert "reviewers: []" in state_js, "state 缺少 reviewers 状态"
    assert "listReviewers" in app_js, "app.js 未预载评审 Profile"


def test_frontend_reviewer_profile_metrics_preview():
    """评审 Profile 表单须有 {{metrics}} 展开预览 + 复制 + {{agent_system_prompt}} 提示。

    预览经后端 /reviewers/preview 实时计算（api.js 封装 previewReviewerMetrics，
    复用运行时 metrics_contract_description 保证与展开一致，前端不镜像格式化）；
    复制按钮须带剪贴板回退（插件页 iframe sandbox 无 allow-clipboard-write）。
    """
    list_js = _read_module("testset_list")
    api_js = _read_module("api")
    assert "previewReviewerMetrics" in api_js, "api.js 缺少预览接口封装"
    assert "previewReviewerMetrics" in list_js, "表单未调用预览接口"
    assert "复制预览" in list_js, "缺少复制预览按钮"
    assert "metrics-preview" in list_js, "缺少预览内容元素"
    assert "{{agent_system_prompt}}" in list_js, (
        "缺少 {{agent_system_prompt}} 占位符提示"
    )
    assert "navigator.clipboard" in list_js or "execCommand" in list_js, (
        "复制缺少剪贴板实现（沙箱回退）"
    )


def test_frontend_profile_dialog_height_and_scroll():
    """新建/编辑评审 Profile 弹窗须限高内部滚动。

    弹窗内容（Provider/模型/提示词/指标）曾整体超出视口高度、页面放不下：
    .modal 须限高（max-height + flex 列布局）、.modal-body 内部滚动
    （overflow-y: auto + flex 子项可收缩）、.modal-actions 固定在底部
    （flex-shrink: 0）；系统提示词 textarea 覆盖 .json-editor 的 360px
    默认高度（紧凑打开，拖拽拉长后弹窗内滚动而非撑爆页面）。
    """
    css = (PLUGIN_DIR / "pages" / "testbench" / "style.css").read_text(encoding="utf-8")
    assert "max-height: min(85vh, 720px)" in css, "style.css 未限制弹窗高度"
    assert ".modal-body {\n  overflow-y: auto;" in css, "弹窗正文未允许内部滚动"
    assert "gap: 8px;\n  flex-shrink: 0;" in css, "弹窗操作按钮未固定底部"
    list_js = _read_module("testset_list")
    assert 'taPrompt.style.minHeight = "120px"' in list_js, (
        "系统提示词 textarea 未覆盖 json-editor 默认高度"
    )


def test_frontend_reviewer_provider_list():
    """评审 Profile 表单的 Provider 下拉须有数据源。

    防回归：openProfileForm 读 state.providers 构建下拉，但 api.js 曾缺
    listProviders、state.js 缺 providers 初始值、loadOptions 未拉取——
    state.providers 恒为 undefined，点击「新建评审 Profile」即抛
    TypeError、弹窗打不开。三段链路（封装 / 状态 / 预载）缺一不可。
    """
    api_js = _read_module("api")
    state_js = _read_module("state")
    app_js = _read_module("app")
    list_js = _read_module("testset_list")
    assert "listProviders" in api_js, "api.js 缺少 listProviders 封装"
    assert "providers: []" in state_js, "state 缺少 providers 初始值"
    assert "listProviders" in app_js, "app.js 未 import listProviders"
    assert "state.providers = Array.isArray(data)" in app_js, (
        "loadOptions 未把 Provider 列表写入 state.providers"
    )
    assert "state.providers" in list_js, "Profile 表单未读 state.providers"


def test_frontend_reviewer_rail_location():
    """评审 Profile 管理迁到左侧测试集列表（「评审 Profile」tab）。

    放开单 profile 限制并把管理入口移到测试集列表（类似身份实体与身份池）：
    index.html 的 .testsets-card 须有「测试集 / 评审 Profile」tab 与
    #reviewer-list 容器，编辑器窗口不再有 .ts-reviewer 摘要区；编辑器只保留
    行内 profile 下拉重建并导出（refreshAllProfileSelects 经返回对象装配）。
    """
    html = _read_html()
    list_js = _read_module("testset_list")
    editor_js = _read_module("testset_editor")
    assert 'data-tab="reviewer"' in html, "测试集卡片缺少「评审 Profile」tab"
    assert 'id="reviewer-list"' in html, "缺少评审 Profile 列表容器"
    assert 'id="reviewer-count"' in html, "缺少评审 Profile 计数"
    assert "ts-reviewer-summary" not in html, "编辑器窗口仍残留 Profile 摘要区"
    assert "switchTestsetTab(" in list_js, "缺少测试集卡片 tab 切换"
    assert "editor.refreshAllProfileSelects()" in list_js, (
        "Profile 变更未重建编辑器行内下拉"
    )
    assert "refreshAllProfileSelects," in editor_js, "编辑器未导出行内 profile 下拉重建"


def test_frontend_reconcile_runs_unwrap():
    """断线对账取最近运行须解包 {runs: [...]}。

    防回归：后端 testset_runs 返回 json_response({runs: [...]})，reconcileEvents
    曾把整个响应对象当数组调 .find（TypeError「(intermediate value).find is not a
    function」，页面重开时拉取最近运行失败、无法找回运行中的测试集）。
    """
    events_js = _read_module("events")
    assert ".runs || [])" in events_js or ".runs) || [])" in events_js, (
        "reconcileEvents 未解包 listTestsetRuns 返回的 runs 数组"
    )
    assert "listTestsetRuns()" in events_js, "reconcileEvents 未取最近运行"


def test_frontend_results_render_verdicts():
    """测试集结果须渲染 verdicts 指标与最终断言，评审失败与不通过区分。

    testset_run.js 须实现 verdictChip / verdictChips（✓/✗/⚠ 三态，悬停显示
    指标摘要 metricsSummary）、renderFinalVerdicts（run.final_verdicts 跨轮
    结果表）；总结须单独计数断言未通过与评审失败（ruleFailCount /
    ruleReviewFailCount）。
    """
    run_js = _read_module("testset_run")
    assert "function verdictChip(" in run_js, "缺少单个 verdict 标记"
    assert "function verdictChips(" in run_js, "缺少结果单元格 verdict 渲染"
    assert "function metricsSummary(" in run_js, "缺少指标摘要"
    assert "function renderFinalVerdicts(" in run_js, "缺少最终断言结果表"
    assert "final_verdicts" in run_js, "结果未引用 final_verdicts"
    assert "assert-skip" in run_js, "评审失败未用 .assert-skip 标记"
    assert "ruleFailCount(" in run_js, "缺少断言未通过计数"
    assert "ruleReviewFailCount(" in run_js, "缺少评审失败计数"
    assert "条评审失败" in run_js, "总结未单独计数评审失败"
    css = (PLUGIN_DIR / "pages" / "testbench" / "style.css").read_text(encoding="utf-8")
    assert ".assert-skip" in css, "style.css 缺少评审失败样式"


def test_frontend_report_editor_toggle():
    """测试集编辑窗口页眉须有「编辑 / 报告」切换。

    M3 报告层把最近运行 / 报告迁入测试集视图：index.html 须含 #btn-ts-mode
    按钮与 #ts-report-body 容器；testset_reports.js（Phase 4 从
    testset_editor.js 拆出）实现 toggleViewMode（viewMode 在 edit / report 间
    切换）、syncViewModeUI（编辑/报告体互斥显隐、按钮文案翻转）与
    renderReportView（报告页渲染）；testset_editor.js 经 createReportView
    工厂绑定按钮点击并按当前视图刷新报告页。
    """
    html = _read_html()
    editor_js = _read_module("testset_editor")
    reports_js = _read_module("testset_reports")
    assert 'id="btn-ts-mode"' in html, "index.html 缺少编辑/报告切换按钮"
    assert 'id="ts-report-body"' in html, "index.html 缺少报告视图容器"
    assert "function toggleViewMode(" in reports_js, "缺少视图切换函数"
    assert 'viewMode === "report"' in reports_js, "缺少报告模式判定"
    assert "function renderReportView(" in reports_js, "缺少报告视图渲染"
    assert "syncViewModeUI()" in reports_js, "缺少视图显隐同步"
    assert "createReportView(" in editor_js, "编辑器未接入报告视图工厂"
    assert "reportView.toggleViewMode()" in editor_js, "未绑定「编辑/报告」切换按钮"
    assert "reportView.isReportMode()" in editor_js, "渲染时未按当前视图刷新报告页"


def test_frontend_report_enabled_checkbox():
    """测试集配置须含报告产出开关 report_enabled。

    index.html 编辑体须含 #ts-report-enabled 勾选框；testset_editor.js 渲染
    时按测试集 report_enabled 回填（renderTestsetEditor 设置 checked），保存
    payload 携带 report_enabled，勾选变更置脏。
    """
    html = _read_html()
    editor_js = _read_module("testset_editor")
    assert 'id="ts-report-enabled"' in html, "index.html 缺少报告产出开关"
    assert 'report_enabled: $("ts-report-enabled").checked' in editor_js, (
        "保存 payload 未携带 report_enabled"
    )
    assert 'ts-report-enabled").checked = !!(ts && ts.report_enabled)' in editor_js, (
        "渲染时未按测试集回填报告开关"
    )
    assert (
        '$("ts-report-enabled").addEventListener("change", markDirty)' in editor_js
    ), "报告开关变更未置脏"


def test_frontend_sidebar_recent_runs_removed():
    """左侧测试集卡片不再罗列「最近运行」（迁入报告视图，按 testset_id 过滤）。

    index.html 不得含 recent-runs 块；testset_list.js 不得再导入
    listTestsetRuns / 渲染 renderRecentRuns（listTestsetRuns API 保留，
    由报告视图经 testset_reports.js 使用）。
    """
    html = _read_html()
    list_js = _read_module("testset_list")
    assert "recent-runs" not in html, "index.html 仍含最近运行块"
    assert "listTestsetRuns" not in list_js, "testset_list.js 仍导入 listTestsetRuns"
    assert "renderRecentRuns" not in list_js, "testset_list.js 仍渲染最近运行"


def test_frontend_report_api():
    """api.js 须封装报告查询 / 删除与按测试集过滤的最近运行接口。

    报告列表走 reports/<testset_id>（id 经 encodeURIComponent）、删除走
    reports/delete（ids）；listTestsetRuns 须支持按 testset_id 过滤（查询串
    走第二参数 params，不内嵌 `?`）。
    """
    api_js = _read_module("api")
    assert "reports/${encodeURIComponent(testsetId)}" in api_js, (
        "缺少按测试集列报告的接口封装"
    )
    assert 'bridge.apiPost("reports/delete", { ids })' in api_js, "缺少报告删除接口"
    assert "{ testset_id: testsetId }" in api_js, "listTestsetRuns 缺少按测试集过滤参数"


def test_frontend_report_list_actions():
    """报告视图须实现报告条目渲染与查看 / 导出 / 删除操作。

    testset_reports.js 报告页经 listReports 拉取报告、buildReportItem 渲染
    条目（含 metrics_summary 指标聚合总览）、openReportModal 复用
    buildResultsTable / renderFinalVerdicts 展示详情、exportReport 导出 JSON、
    deleteReport 确认后走 deleteReports；最近运行区经 listTestsetRuns +
    getDeps().viewTestsetRun 找回进度。
    """
    reports_js = _read_module("testset_reports")
    assert "listReports(" in reports_js, "报告页未拉取报告列表"
    assert "listTestsetRuns(ts.id)" in reports_js, "最近运行未按测试集过滤"
    assert "function buildReportItem(" in reports_js, "缺少报告条目渲染"
    assert "function openReportModal(" in reports_js, "缺少报告详情弹窗"
    assert "function exportReport(" in reports_js, "缺少报告导出"
    assert "function deleteReport(" in reports_js, "缺少报告删除"
    assert "buildResultsTable" in reports_js, "报告详情未复用结果表格"
    assert "renderFinalVerdicts" in reports_js, "报告详情未复用最终断言表"
    assert "metrics_summary" in reports_js, "报告条目未展示指标聚合"
    assert "deleteReport(report.id)" in reports_js, "报告条目删除未传 report.id"
    assert "deleteReports([id])" in reports_js, "删除报告未调 deleteReports"
    assert "getDeps().viewTestsetRun(r.run_id)" in reports_js, (
        "最近运行查看未走 viewTestsetRun 找回"
    )


def test_frontend_pure_module_extracted():
    """纯函数抽取完整性：pure.js 承载可测纯逻辑，页面模块经 import 复用同一实现。

    Phase 2 把 testset_editor.js / testset_run.js 的行收集、规则构造、导入解析、
    verdict 计数、段文案等纯函数抽到 pages/testbench/pure.js（零依赖、不引用
    DOM/state/其它模块），由 node:test（tests/frontend/pure.test.mjs）动态测试；
    两个页面模块须从该模块 import，防止两处实现漂移。静态检查只做结构性防回归，
    行为由 node:test 覆盖。
    """
    import re

    pure_js = _read_module("pure")
    editor_js = _read_module("testset_editor")
    run_js = _read_module("testset_run")
    # 零依赖：不得 import 其它模块 / 引用 DOM
    assert not re.search(r"^\s*import\b", pure_js, re.MULTILINE), (
        "pure.js 不得 import 其它模块（保持零依赖可被 node:test 直接加载）"
    )
    assert "document." not in pure_js, "pure.js 不得引用 document（保持纯函数）"
    assert "querySelector" not in pure_js, "pure.js 不得做 DOM 查询（保持纯函数）"
    # 被抽取纯函数均在 pure.js 导出
    for fn in (
        "collectEditorRows",
        "collectRules",
        "buildRule",
        "parseScope",
        "parseTestsetEnvelope",
        "rangesFromFlags",
        "ruleFailCount",
        "ruleReviewFailCount",
        "segmentLabel",
        "segmentSummary",
    ):
        assert f"export function {fn}(" in pure_js, f"pure.js 缺少导出 {fn}"
    # 两个页面模块从 pure.js import，源文件不再重复定义
    assert 'from "./pure.js"' in editor_js, "testset_editor.js 未从 pure.js import"
    assert 'from "./pure.js"' in run_js, "testset_run.js 未从 pure.js import"
    assert "function parseScope(" not in editor_js, (
        "testset_editor.js 残留 parseScope 定义"
    )
    assert "function ruleFailCount(" not in run_js, (
        "testset_run.js 残留 ruleFailCount 定义"
    )


def test_frontend_defensive_fixes():
    """质量复审修复轮的防御性改动标记（防回归）。

    本轮（v0.4.4 不 bump）修复：事件流订阅建立即失败的延迟重连、历史/消息流
    加载失败回退缓存、群成员变更异步失败反馈、报告视图乱序响应丢弃、新建测试
    集未保存修改确认、新建测试组直接建 0 会话空组不弹窗。各标记为修复的静态
    落点，缺失即修复被回退。
    """
    reports_js = _read_module("testset_reports")
    events_js = _read_module("events")
    app_js = _read_module("app")
    list_js = _read_module("testset_list")
    identity_js = _read_module("identity_list")
    gl_js = _read_module("group_list")
    assert "reportSeq" in reports_js, "报告视图缺少渲染序号守卫 reportSeq"
    assert "事件流订阅失败" in events_js, "事件流订阅建立失败缺少延迟重连"
    assert "state.historyCache.get(id)" in app_js, "历史加载失败未回退缓存"
    assert "state.streamCache.get(id)" in app_js, "消息流加载失败未回退缓存"
    assert "doOpenNewTestset" in list_js, "新建测试集未提取 doOpenNewTestset（脏确认）"
    assert "成员加入失败" in identity_js, "群成员加入失败缺少错误反馈"
    assert "成员移除失败" in identity_js, "群成员移除失败缺少错误反馈"
    assert "群聊名称保存失败" in identity_js, "群聊名称保存失败缺少错误反馈"
    assert "createGroup({ count: 0 })" in gl_js, (
        "「＋ 新建测试组」未改为直接建 0 会话空组"
    )
    assert "showRunStatus" in gl_js, "新建测试组后缺少状态提示"
