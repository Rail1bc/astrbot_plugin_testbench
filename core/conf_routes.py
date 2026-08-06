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


async def put_route_front(ucr: Any, umop: str, conf_id: str) -> None:
    """写入 umo → conf_id 路由并置于路由表**表头**。

    AstrBot UCR 的解析是「按 dict 插入顺序首个匹配即返回」（``get_conf_id_for_umop``
    顺序遍历、先命中先返回），而 ``update_route`` 对新键**追加到末尾**——若用户
    已配置「全部会话」类兜底路由（如 ``webchat::``），后追加的会话级精确路由会被
    兜底遮蔽、绑定静默失效。故本插件写入的精确路由移到表头，保证其优先于更宽的
    既有规则命中；会话级精确 umo 只匹配自身（会话 id 无通配符），不影响其他会话
    与规则的解析，未绑定的会话仍正常落到兜底路由。
    """
    ucr.umop_to_conf_id.pop(umop, None)
    ucr.umop_to_conf_id = {umop: conf_id, **ucr.umop_to_conf_id}
    # update_route 对已存在键只改值不改位置，并按整表落盘——值已就位，仅触发持久化
    await ucr.update_route(umop, conf_id)


async def apply_routes(ucr: Any, sessions: list[dict], conf_id: str) -> None:
    """把每个会话路由到指定配置档案（精确到 umo、表头优先，不互相影响）。"""
    for session in sessions:
        await put_route_front(ucr, umo_of(session), conf_id)


async def clear_routes(ucr: Any, sessions: list[dict]) -> None:
    """删除会话对应的配置档案路由。"""
    for session in sessions:
        await delete_route_if_exists(ucr, umo_of(session))


async def sync_route(ucr: Any, session: dict) -> None:
    """按会话的有效配置档案同步路由（有绑定则应用，无绑定则确保路由不存在）。"""
    umop = umo_of(session)
    conf_id = session.get("conf_id")
    if conf_id:
        await put_route_front(ucr, umop, conf_id)
    else:
        await delete_route_if_exists(ucr, umop)


async def save_and_apply_routes(
    ucr: Any, sessions: list[dict], conf_id: str
) -> list[tuple[str, str | None]]:
    """保存每个会话的原路由并应用临时路由，返回 ``[(umo, 原 conf_id)]``。

    测试结束后用 :func:`restore_routes` 恢复原路由（原无路由的删除临时路由）。
    临时路由同样置于表头：测试运行时指定的 conf_id 须优先于「全部会话」类兜底。
    """
    saved: list[tuple[str, str | None]] = []
    for session in sessions:
        umop = umo_of(session)
        saved.append((umop, ucr.umop_to_conf_id.get(umop)))
        await put_route_front(ucr, umop, conf_id)
    return saved


async def restore_routes(ucr: Any, saved_routes: list[tuple[str, str | None]]) -> None:
    """恢复 :func:`save_and_apply_routes` 保存的原路由。"""
    for umop, prev_conf_id in saved_routes:
        if prev_conf_id is None:
            await delete_route_if_exists(ucr, umop)
        else:
            await ucr.update_route(umop, prev_conf_id)
