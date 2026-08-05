"""前端脚本静态检查：不依赖 astrbot，任何环境均可运行。

仅读取插件页面脚本做静态检查，用于捕获 ES module import 重名、bridge 端点
内嵌查询串等会导致整页失效的回归。
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = REPO_ROOT  # 插件根目录即仓库根，页面文件在 pages/testbench/

# 页面全部 ES module（入口 app.js + 视图/状态/工具模块）
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


def test_frontend_pending_status_labels():
    """面板在途条须覆盖四个状态文案（已入队/排队等待 LLM/LLM 生成中/完成）。

    PENDING_STATUS_TEXT 与后端 runner 条目状态一一对应；缺任一键/文案，
    重叠场景下的状态显示就会不完整（如只剩「已入队」）。
    """
    src = _read_module("app")
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
    src = _read_module("app")
    assert "historyRefreshedAt" in src, "缺少 historyRefreshedAt 记录历史刷新时刻"
    assert "historyRefreshedAt.set(id, Date.now())" in src, (
        "loadHistory 成功路径必须记录刷新时刻"
    )
    assert "status_at" in src, "renderPendingStrip 未按 status_at 过滤已完成条目"


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
    勾选组解析为会话 id 列表后交给 runTestset。
    """
    src = _read_module("testset_list")
    assert "astrbot-testbench-testset" in src, "导出信封缺少 format 标识"
    assert "version: EXPORT_VERSION" in src, "导出信封缺少 version"
    assert "format: EXPORT_FORMAT" in src, "导出信封未序列化 format"
    assert "createTestset({" in src, "导入未复用 createTestset 端点"
    assert "选择测试组" in src, "运行弹窗缺少「选择测试组」目标选项"
    assert "env.runTestset(testset, ids)" in src, (
        "运行未把目标会话 id 列表交给 runTestset"
    )
    # 组多选 → 会话 id 解析：勾选 data-gid checkbox，展开为该组全部会话 id
    assert "input[data-gid]:checked" in src, "组多选缺少按 data-gid 收集勾选态"
    assert "selectedGroupSessionIds" in src, "缺少把测试组解析为会话 id 列表的函数"


def test_frontend_testset_run_is_backend_driven():
    """测试集运行必须由后端驱动：前端只一次性启动运行并轮询后端记录。

    若退回前端逐条驱动（循环调 runTest 逐步等待），页面刷新/关闭会中断后续
    步骤。防回归：app.js 必须调用 runTestsetApi（整场运行一次启动）并用
    pollTestsetRun 轮询后端运行记录，而不是逐条调用 runTest。
    """
    src = _read_module("app")
    assert "runTestsetApi(" in src, "app.js 未调用 runTestsetApi 启动测试集运行"
    assert "pollTestsetRun(" in src, "app.js 未实现 pollTestsetRun 轮询后端运行记录"


def test_frontend_testset_summary_counts_assertion_failures():
    """测试集运行总结须单独计数断言未通过（✗），与步骤/会话错误区分。

    曾出现：结果表格 3 个会话断言 ✗，但最终总结「运行完成（N 步）」没提任何
    失败——断言失败只落在结果单元格、不改会话 status，总结若只数
    status=="error" 就永远显示「错误 0」，误导用户以为断言全过。
    """
    src = _read_module("app")
    assert "assertFails" in src, "总结未计算断言未通过数量"
    assert "条断言未通过" in src, "总结文案未包含断言未通过计数"
    # 表格行尾计数也要标注断言 ✗（与「错误 N」的 status 语义区分）
    assert "断言 ✗" in src, "结果表格行尾未单独标注断言 ✗ 计数"
