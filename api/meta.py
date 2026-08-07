"""元信息接口：LLM Provider / 配置档案 / 平台适配器列表。"""

from __future__ import annotations

from astrbot.api.web import json_response

from ..core.conf_tools import conf_has_callable_tools


class MetaAPI:
    """Provider / 配置档案 / 平台列表（前端下拉框的数据源）。"""

    async def list_providers(self):
        """列出可用的对话 LLM Provider 及其模型。

        与 list_platforms 一致采用防御式读取：单个 Provider 的元数据读取失败时
        跳过该 Provider，get_model 失败时降级为 None，不因单个异常拖垮整个接口。
        """
        providers = []
        for prov in self.context.get_all_providers():
            try:
                meta = prov.meta()
            except Exception as e:  # noqa: BLE001
                self.logger.warning(f"读取 Provider 元数据失败: {e}")
                continue
            models: list[str] = []
            try:
                models = await prov.get_models()
            except Exception as e:  # noqa: BLE001
                self.logger.warning(f"获取 Provider {meta.id} 的模型列表失败: {e}")
            try:
                current_model = prov.get_model()
            except Exception as e:  # noqa: BLE001
                self.logger.warning(f"读取 Provider {meta.id} 当前模型失败: {e}")
                current_model = None
            pc = prov.provider_config or {}
            providers.append(
                {
                    "id": pc.get("id") or meta.id,
                    "name": (
                        pc.get("name")
                        or pc.get("provider_source_id")
                        or pc.get("id")
                        or meta.id
                        or meta.type
                    ),
                    "type": meta.type,
                    "current_model": current_model,
                    "models": models,
                }
            )
        return json_response(providers)

    async def list_confs(self):
        """列出配置档案（用于测试提示词/系统设定）。

        每个档案附 ``has_callable_tools``（是否启用了任何可调用的工具），供前端
        组/会话配置弹窗即时提示安全警告。与 list_platforms 一致采用防御式读取：
        单个档案对象缺字段时回退默认值，配置管理器/档案内容缺失时不因个别异常
        拖垮整个列表接口（显示用途下未加载的档案宽松判定为无工具）。
        """
        confs_mgr = getattr(self.context, "astrbot_config_mgr", None)
        confs_map = getattr(confs_mgr, "confs", {}) if confs_mgr else {}
        confs = []
        for conf in confs_mgr.get_conf_list() if confs_mgr else []:
            conf_id = conf.get("id") or conf.get("name") or ""
            confs.append(
                {
                    "id": conf_id,
                    "name": conf.get("name") or conf.get("id") or "",
                    "path": conf.get("path"),
                    "has_callable_tools": conf_has_callable_tools(
                        confs_map.get(conf_id)
                    ),
                }
            )
        return json_response(confs)

    async def list_platforms(self):
        """列出已启用的平台适配器（虚拟会话可模拟其平台上下文）。

        单个适配器元数据读取失败时跳过该适配器，保证单个异常不会导致整个
        列表接口失败（前端下拉框因此为空）。
        """
        platforms = []
        manager = getattr(self.context, "platform_manager", None)
        insts = getattr(manager, "platform_insts", None) if manager else None
        if not insts:
            return json_response(platforms)
        for inst in insts:
            try:
                meta = inst.meta()
                platform_id = getattr(meta, "id", None)
                if not platform_id:
                    continue
                name = getattr(meta, "name", None)
                platforms.append(
                    {
                        "id": platform_id,
                        "name": name,
                        "display_name": getattr(meta, "adapter_display_name", None)
                        or name,
                    }
                )
            except Exception as e:  # noqa: BLE001
                self.logger.warning(f"读取平台适配器元数据失败: {e}")
        return json_response(platforms)
