"""UCR 配置档案路由辅助。

虚拟会话按 unified_msg_origin（umo）绑定配置档案（`umo → conf_id`）。持久路由
（main.py：创建测试组、会话配置变更时应用与清理）与临时路由（runner.py：测试
运行时指定 conf_id，结束后恢复原路由）共用同一套 umo → conf_id 操作，集中在本
模块，避免两处实现对 UCR API 的双份维护。
"""

from __future__ import annotations

from typing import Any

from ..store.group_store import umo_of


async def delete_route_if_exists(ucr: Any, umop: str) -> None:
    """删除 umo 对应的路由（不存在则跳过）。"""
    if umop in ucr.umop_to_conf_id:
        await ucr.delete_route(umop)


async def apply_routes(ucr: Any, sessions: list[dict], conf_id: str) -> None:
    """把每个会话路由到指定配置档案（精确到 umo，不互相影响）。"""
    for session in sessions:
        await ucr.update_route(umo_of(session), conf_id)


async def clear_routes(ucr: Any, sessions: list[dict]) -> None:
    """删除会话对应的配置档案路由。"""
    for session in sessions:
        await delete_route_if_exists(ucr, umo_of(session))


async def sync_route(ucr: Any, session: dict) -> None:
    """按会话的有效配置档案同步路由（有绑定则应用，无绑定则确保路由不存在）。"""
    umop = umo_of(session)
    conf_id = session.get("conf_id")
    if conf_id:
        await ucr.update_route(umop, conf_id)
    else:
        await delete_route_if_exists(ucr, umop)


async def save_and_apply_routes(
    ucr: Any, sessions: list[dict], conf_id: str
) -> list[tuple[str, str | None]]:
    """保存每个会话的原路由并应用临时路由，返回 ``[(umo, 原 conf_id)]``。

    测试结束后用 :func:`restore_routes` 恢复原路由（原无路由的删除临时路由）。
    """
    saved: list[tuple[str, str | None]] = []
    for session in sessions:
        umop = umo_of(session)
        saved.append((umop, ucr.umop_to_conf_id.get(umop)))
        await ucr.update_route(umop, conf_id)
    return saved


async def restore_routes(ucr: Any, saved_routes: list[tuple[str, str | None]]) -> None:
    """恢复 :func:`save_and_apply_routes` 保存的原路由。"""
    for umop, prev_conf_id in saved_routes:
        if prev_conf_id is None:
            await delete_route_if_exists(ucr, umop)
        else:
            await ucr.update_route(umop, prev_conf_id)
