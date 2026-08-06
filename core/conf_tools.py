"""配置档案工具能力判定（安全警告的数据源）。

判定一个配置档案（AstrBotConfig，dict 子类）启用了哪些可调用的工具能力，
与 AstrBot 运行时挂载逻辑一致（astrbot/core/astr_main_agent.py：按
``provider_settings.computer_use_runtime`` 挂载计算机工具、按 ``web_search``
挂载联网搜索、按 ``kb_agentic_mode`` 挂载知识库查询、按
``proactive_capability.add_cron_tools`` 挂载定时任务工具）。纯函数，
不依赖 AstrBot 运行时状态，供 list_confs（前端弹窗警告）与 list_groups
（组安全标记）共用。
"""

from __future__ import annotations

from typing import Any

# 配置缺省值语义：add_cron_tools 默认开启（astrbot/core/config/default.py），
# 故 cron 工具在大多数配置下都可调用——用户要求「只要存在可调用的工具都要警告」，
# 默认配置本身即命中安全标记，属预期行为。
CRON_TOOLS_DEFAULT = True


def conf_tool_info(conf: dict | None) -> dict[str, Any]:
    """返回配置的工具能力判定。

    Returns:
        ``{"has_callable_tools", "computer_use_runtime", "web_search",
        "kb_agentic_mode", "cron_tools"}``；非 dict / None 一律判定无工具。
    """
    if not isinstance(conf, dict):
        return {
            "has_callable_tools": False,
            "computer_use_runtime": None,
            "web_search": False,
            "kb_agentic_mode": False,
            "cron_tools": False,
        }
    ps = conf.get("provider_settings") or {}
    pc = ps.get("proactive_capability") or {}
    info: dict[str, Any] = {
        "computer_use_runtime": ps.get("computer_use_runtime"),
        "web_search": bool(ps.get("web_search")),
        "kb_agentic_mode": bool(conf.get("kb_agentic_mode")),
        "cron_tools": bool(pc.get("add_cron_tools", CRON_TOOLS_DEFAULT)),
    }
    info["has_callable_tools"] = bool(
        info["computer_use_runtime"] in ("local", "sandbox")
        or info["web_search"]
        or info["kb_agentic_mode"]
        or info["cron_tools"]
    )
    return info


def conf_has_callable_tools(conf: dict | None) -> bool:
    """配置是否启用了任何可调用的工具（安全警告/标记的判定条件）。"""
    return conf_tool_info(conf)["has_callable_tools"]
