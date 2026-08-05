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
)


def _read_module(name: str) -> str:
    return (PLUGIN_DIR / "pages" / "testbench" / f"{name}.js").read_text(
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
