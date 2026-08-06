"""前端脚本静态检查：不依赖 astrbot，任何环境均可运行。

仅读取插件页面脚本做静态检查，用于捕获 ES module import 重名、bridge 端点
内嵌查询串等会导致整页失效的回归。
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = REPO_ROOT  # 插件根目录即仓库根，页面文件在 pages/testbench/

# 页面全部 ES module（入口 app.js + 视图/状态/工具模块；events.js 事件驱动反馈层、
# testset_run.js 测试集运行编排、testset_editor.js 测试集编辑器、identity_list.js
# 「身份与群聊」视图为拆分后新增模块）
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
    "events",
    "testset_run",
    "identity_list",
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
    list_js = _read_module("testset_list")
    assert "astrbot-testbench-testset" in editor_js, "导出信封缺少 format 标识"
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
    """会话面板须可在「LLM 历史 / 消息流」间切换。

    群消息流与 LLM 历史并行记录：面板页头须有 view-toggle 按钮，app.js 须实现
    setPanelView（切换并加载对应视图）与 loadStream（拉取消息流渲染）。
    """
    app_js = _read_module("app")
    assert 'data-action="view-toggle"' in app_js, "面板页头缺少视图切换按钮"
    assert "function setPanelView(" in app_js, "缺少 setPanelView 视图切换函数"
    assert "async function loadStream(" in app_js, "缺少 loadStream 消息流加载函数"
    assert "streamCache" in app_js, "app.js 未使用消息流缓存 streamCache"
    assert 'state.panelView.get(id) === "stream"' in app_js, (
        "未按 panelView 状态判断当前视图"
    )


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
    assert "collectSender(" in editor_js, "缺少行内身份收集函数 collectSender"
    assert "const sender = collectSender(" in editor_js, (
        "collectEditorRows 未收集行内身份"
    )
    assert (
        "messages.push({ text, rule, ...sender, auto_at: atCb.checked })" in editor_js
    ), "收集的消息未合并 sender / auto_at 字段"
    assert "message.sender_id = m.sender_id" in editor_js, "导入信封未保留 sender_id"
    assert "message.sender_name = m.sender_name" in editor_js, (
        "导入信封未保留 sender_name"
    )
    assert "message.auto_at = m.auto_at" in editor_js, "导入信封未保留 auto_at"
    assert "atCb.checked = !msg || msg.auto_at !== false" in editor_js, (
        "消息行 @ 勾选未按 auto_at 缺省开启渲染"
    )


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
