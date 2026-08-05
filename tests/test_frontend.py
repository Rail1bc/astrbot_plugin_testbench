"""前端脚本静态检查：不依赖 astrbot，任何环境均可运行。

仅读取插件页面脚本做静态检查，用于捕获 ES module import 重名、bridge 端点
内嵌查询串等会导致整页失效的回归。
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = REPO_ROOT  # 插件根目录即仓库根，页面文件在 pages/testbench/


# ---------- 前端脚本静态检查 ----------


def test_frontend_no_import_redeclaration():
    """app.js 的 import 名不得与顶层声明重名。

    重构拆分 api.js 后曾把本地函数 createGroup 与 import 的 createGroup
    重名，ES module 解析期报 SyntaxError，整页 JS 失效（下拉框因此为空）。
    """
    import re

    app_js = (PLUGIN_DIR / "pages" / "testbench" / "app.js").read_text(encoding="utf-8")

    imports = set()
    for match in re.finditer(r"import\s*\{([^}]*)\}\s*from", app_js):
        for name in match.group(1).split(","):
            name = name.strip().split(" as ")[-1].strip()
            if name:
                imports.add(name)

    declarations = set(
        re.findall(
            r"^(?:async\s+)?function\s+([A-Za-z_$][\w$]*)",
            app_js,
            re.MULTILINE,
        )
    ) | set(
        re.findall(
            r"^(?:const|let|var|class)\s+([A-Za-z_$][\w$]*)", app_js, re.MULTILINE
        )
    )

    assert imports.isdisjoint(declarations), (
        f"app.js 存在 import 与顶层声明重名: {sorted(imports & declarations)}"
    )


def test_frontend_bridge_endpoint_has_no_query_string():
    """api.js 的 bridge 端点不得内嵌查询串（`?`）。

    父窗口 PluginPagePage.vue 的 normalizePluginEndpoint 拒绝含 `?` 的端点，
    查询参数必须通过 apiGet(endpoint, params) 的第二个参数传递。曾把
    test/run/status?test_id=... 拼进端点，导致 runStatus 恒 reject、
    前端轮询永不触发会话刷新（群发/单发/重新生成都不更新）。
    """
    import re

    api_js = (PLUGIN_DIR / "pages" / "testbench" / "api.js").read_text(encoding="utf-8")
    endpoints = re.findall(r'api(?:Get|Post)\(\s*["`]([^"`]*)["`]', api_js)
    assert endpoints, "未找到任何 bridge 调用"
    for endpoint in endpoints:
        assert "?" not in endpoint, f"bridge 端点不能内嵌查询串: {endpoint}"


def test_frontend_effective_view_resolves_sender_fields():
    """app.js 的 effectiveView 返回对象必须含 sender_id / sender_name。

    曾漏掉这两个字段，导致会话展开配置里发送者 ID / 昵称恒显示「—」——
    effectiveView 是客户端对组配置 + 会话覆盖的解析，缺失字段即无值可显。
    """
    import re

    app_js = (PLUGIN_DIR / "pages" / "testbench" / "app.js").read_text(encoding="utf-8")
    match = re.search(
        r"function effectiveView\(id\)[\s\S]*?return \{([\s\S]*?)\};", app_js
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
    继承组配置。本检查确保选项映射/过滤只保留共享实现一处。
    """
    import re

    app_js = (PLUGIN_DIR / "pages" / "testbench" / "app.js").read_text(encoding="utf-8")
    assert len(re.findall(r"\.map\(\s*\(p\) =>", app_js)) == 1, (
        "平台选项映射必须只出现在 platformOptions() 一处"
    )
    assert app_js.count('confs.filter((c) => c.id !== "default")') == 1, (
        "配置档案过滤必须只出现在 confOptions() 一处"
    )
