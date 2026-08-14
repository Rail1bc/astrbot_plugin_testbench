"""持久化 store 测试：groups / identities / chat_groups / streams / reports /
reviewers 的 CRUD、原子写与损坏备份、并发写。"""

import asyncio
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
# 插件模块用相对导入（from .group_store import ...），必须以包形式加载。
# 与 AstrBot 在 data/plugins 下加载插件的方式一致：把插件根目录的父目录加入
# sys.path，以 namespace package（astrbot_plugin_testbench）导入。
sys.path.insert(0, str(REPO_ROOT.parent))

pytest.importorskip("astrbot")

import astrbot_plugin_testbench.store.group_store as gs_mod  # noqa: E402
import astrbot_plugin_testbench.store.identity_store as ids_mod  # noqa: E402
import astrbot_plugin_testbench.store.report_store as rps_mod  # noqa: E402
import astrbot_plugin_testbench.store.reviewer_store as rvs_mod  # noqa: E402
import astrbot_plugin_testbench.store.stream_store as stm_mod  # noqa: E402

ChatGroupStore = ids_mod.ChatGroupStore
IdentityStore = ids_mod.IdentityStore
MAX_STREAM_MESSAGES = stm_mod.MAX_STREAM_MESSAGES
ReportStore = rps_mod.ReportStore
ReviewerStore = rvs_mod.ReviewerStore
StreamStore = stm_mod.StreamStore
VirtualGroupManager = gs_mod.VirtualGroupManager
umo_of = gs_mod.umo_of

from fakes import (  # noqa: E402
    make_session,
)


def test_umo_of():
    assert umo_of(make_session(1)) == "webchat:FriendMessage:vs_1"
    assert (
        umo_of({"id": "vs_1", "platform_id": "aiocqhttp"})
        == "aiocqhttp:FriendMessage:vs_1"
    )


def test_group_manager_create_persist(tmp_path):
    mgr = VirtualGroupManager(data_dir=tmp_path)
    group = mgr.create_group("组A", count=3, platform_id="webchat", name_prefix="测试")
    assert len(group["sessions"]) == 3
    assert [s["name"] for s in group["sessions"]] == ["测试1", "测试2", "测试3"]

    # 重新加载（新实例）确认数据已持久化
    mgr2 = VirtualGroupManager(data_dir=tmp_path)
    assert len(mgr2.list_groups()) == 1
    g = mgr2.get_group(group["id"])
    assert g["name"] == "组A"
    assert len(g["sessions"]) == 3

    # 组内新增会话，编号接续
    added = mgr2.add_sessions(group["id"], 2, name_prefix="测试")
    assert [s["name"] for s in added] == ["测试4", "测试5"]
    assert len(mgr2.get_group(group["id"])["sessions"]) == 5

    # 删除单个会话
    sid = added[0]["id"]
    removed = mgr2.delete_sessions([sid])
    assert len(removed) == 1
    assert mgr2.find_session(sid) is None

    # 删除整个组
    deleted = mgr2.delete_groups([group["id"]])
    assert len(deleted) == 4
    assert mgr2.list_groups() == []
    assert (tmp_path / "virtual_session" / "groups.json").exists()


def test_group_manager_delete_group_with_no_sessions(tmp_path):
    """0 会话的测试组也必须能删除：删除条件不能依赖 removed 非空。

    组内会话可被逐个删光，此时 delete_groups 的 removed 恒为空列表，
    曾因此跳过 _save() 导致组永远删不掉。
    """
    mgr = VirtualGroupManager(data_dir=tmp_path)
    group = mgr.create_group("空组", count=1)
    mgr.delete_sessions([group["sessions"][0]["id"]])
    assert mgr.get_group(group["id"])["sessions"] == []

    removed = mgr.delete_groups([group["id"]])
    assert removed == []  # 无会话可清，返回空对
    assert mgr.list_groups() == []  # 组必须被真正删除

    # 重新加载（新实例）确认删除已持久化
    mgr2 = VirtualGroupManager(data_dir=tmp_path)
    assert mgr2.list_groups() == []


def test_group_manager_corrupt_file_backed_up(tmp_path):
    """TB-01: 损坏的 groups.json 改名备份（.corrupt-<ts>）而非静默清空。

    直接覆盖写 + 损坏时静默回退空，会让下一次保存把「空」写回、用户数据
    永久丢失。损坏文件必须保留现场供人工恢复。
    """
    mgr = VirtualGroupManager(data_dir=tmp_path)
    mgr.create_group("组A", count=2)

    # 模拟崩溃留下的半截 JSON
    data_file = tmp_path / "virtual_session" / "groups.json"
    data_file.write_text(
        '{"groups": [{"id": "g_1", "name": "组A", "sessions": [{"id"',
        encoding="utf-8",
    )

    mgr2 = VirtualGroupManager(data_dir=tmp_path)
    assert mgr2.list_groups() == []  # 从空数据继续（不崩溃、不迁移旧文件）
    backups = list((tmp_path / "virtual_session").glob("groups.json.corrupt-*"))
    assert len(backups) == 1  # 损坏文件被备份而非删除
    assert '"组A"' in backups[0].read_text(encoding="utf-8")  # 现场可人工恢复

    # 下一次保存为原子写：不留 .tmp 残留
    mgr2.create_group("新组")
    assert not list((tmp_path / "virtual_session").glob("groups.json.tmp"))


def test_group_create_stores_conf(tmp_path):
    mgr = VirtualGroupManager(data_dir=tmp_path)
    group = mgr.create_group("组A", count=2, conf_id="conf_a")
    assert group["conf_id"] == "conf_a"
    # 会话默认继承组配置（覆盖字段为 None）
    assert all(s["conf_id"] is None for s in group["sessions"])


def test_effective_resolution(tmp_path):
    mgr = VirtualGroupManager(data_dir=tmp_path)
    group = mgr.create_group(
        "组A",
        count=1,
        platform_id="aiocqhttp",
        conf_id="conf_a",
        sender_id="group_sender",
        sender_name="组发送者",
    )
    session = group["sessions"][0]
    eff = mgr.effective(group, session)
    assert eff["platform_id"] == "aiocqhttp"
    assert eff["conf_id"] == "conf_a"
    assert eff["sender_id"] == "group_sender"
    assert eff["sender_name"] == "组发送者"

    # 会话覆盖单个字段，其余继承组配置
    mgr.update_session(
        session["id"], platform_id="telegram", conf_id="conf_b", sender_id="me"
    )
    eff2 = mgr.effective(group, session)
    assert eff2["platform_id"] == "telegram"
    assert eff2["conf_id"] == "conf_b"
    assert eff2["sender_id"] == "me"
    assert eff2["sender_name"] == "组发送者"

    # 传 None 恢复继承组配置
    mgr.update_session(session["id"], platform_id=None, conf_id=None)
    eff3 = mgr.effective(group, session)
    assert eff3["platform_id"] == "aiocqhttp"
    assert eff3["conf_id"] == "conf_a"

    # conf_id 空串 = 显式使用默认配置档案（不绑定）
    mgr.update_session(session["id"], conf_id="")
    assert mgr.effective(group, session)["conf_id"] is None


def test_effective_defaults(tmp_path):
    """无组配置时的默认值：平台 webchat，发送者 testbench / 测试台。"""
    mgr = VirtualGroupManager(data_dir=tmp_path)
    group = mgr.create_group("组A", count=1)
    eff = mgr.effective(group, group["sessions"][0])
    assert eff["platform_id"] == "webchat"
    assert eff["sender_id"] == "testbench"
    assert eff["sender_name"] == "测试台"
    assert umo_of(eff) == f"webchat:FriendMessage:{group['sessions'][0]['id']}"


def test_effective_many_order_and_skip(tmp_path):
    mgr = VirtualGroupManager(data_dir=tmp_path)
    group = mgr.create_group("组A", count=3)
    ids = [s["id"] for s in group["sessions"]]
    resolved = mgr.effective_many([ids[2], ids[0], "vs_none"])
    assert [r["id"] for r in resolved] == [ids[2], ids[0]]


def test_update_session_not_found(tmp_path):
    mgr = VirtualGroupManager(data_dir=tmp_path)
    assert mgr.update_session("vs_none", conf_id="x") is None


def test_group_update_fields(tmp_path):
    mgr = VirtualGroupManager(data_dir=tmp_path)
    group = mgr.create_group("组A", count=2, platform_id="webchat", conf_id="conf_a")
    gid = group["id"]

    updated = mgr.update_group(
        gid,
        name="组B",
        platform_id="telegram",
        conf_id="conf_b",
        sender_id="s1",
        sender_name="S1",
    )
    assert updated["name"] == "组B"
    assert updated["platform_id"] == "telegram"
    assert updated["conf_id"] == "conf_b"
    assert updated["sender_id"] == "s1"

    # 未单独覆盖的会话跟随组配置
    eff = mgr.effective(updated, updated["sessions"][0])
    assert eff["platform_id"] == "telegram"
    assert eff["conf_id"] == "conf_b"

    # 空平台/档案归一为 None；空组名回退默认
    mgr.update_group(gid, platform_id="", conf_id="", name="")
    g = mgr.get_group(gid)
    assert g["platform_id"] is None
    assert g["conf_id"] is None
    assert g["name"] == "测试组"

    # 会话覆盖优先于组配置
    mgr.update_session(updated["sessions"][0]["id"], platform_id="webchat")
    eff2 = mgr.effective(g, updated["sessions"][0])
    assert eff2["platform_id"] == "webchat"


def test_group_update_not_found(tmp_path):
    mgr = VirtualGroupManager(data_dir=tmp_path)
    assert mgr.update_group("g_none", name="x") is None


def test_add_sessions_unknown_group(tmp_path):
    mgr = VirtualGroupManager(data_dir=tmp_path)
    with pytest.raises(KeyError):
        mgr.add_sessions("g_none", 1)


def test_group_migration_legacy(tmp_path):
    sess_dir = tmp_path / "virtual_session"
    sess_dir.mkdir(parents=True)
    (sess_dir / "sessions.json").write_text(
        json.dumps(
            [
                {
                    "id": "vs_1",
                    "name": "旧会话",
                    "platform_id": "aiocqhttp",
                    "conf_id": "conf_a",
                    "created_at": 0,
                }
            ]
        ),
        encoding="utf-8",
    )
    mgr = VirtualGroupManager(data_dir=tmp_path)
    assert len(mgr.list_groups()) == 1
    group = mgr.list_groups()[0]
    assert group["name"] == "默认测试组"
    assert len(group["sessions"]) == 1
    eff = mgr.effective(group, group["sessions"][0])
    assert eff["platform_id"] == "aiocqhttp"
    assert eff["conf_id"] == "conf_a"
    assert (tmp_path / "virtual_session" / "groups.json").exists()


def test_umo_of_uses_message_type():
    """umo 格式随消息类型变化：FriendMessage 与 GroupMessage 的键不同。"""
    assert umo_of(make_session(1)) == "webchat:FriendMessage:vs_1"
    assert (
        umo_of({"id": "vs_1", "platform_id": "webchat", "message_type": "GroupMessage"})
        == "webchat:GroupMessage:vs_1"
    )
    assert (
        umo_of(
            {"id": "vs_1", "platform_id": "aiocqhttp", "message_type": "GroupMessage"}
        )
        == "aiocqhttp:GroupMessage:vs_1"
    )


def test_effective_resolves_message_type_and_chat_group(tmp_path):
    """message_type / chat_group_id 的三态解析（会话 → 组 → 默认）。

    auto@ 已改为发送时选项（群发栏 / 测试集消息级），不再属于有效配置。
    """
    mgr = VirtualGroupManager(data_dir=tmp_path)
    group = mgr.create_group(
        "群聊组",
        count=1,
        message_type="GroupMessage",
        chat_group_id="cg_1",
    )
    session = group["sessions"][0]
    eff = mgr.effective(group, session)
    assert eff["message_type"] == "GroupMessage"
    assert eff["chat_group_id"] == "cg_1"
    assert "auto_at" not in eff

    # 默认：私聊 + 无绑定
    group2 = mgr.create_group("默认组", count=1)
    eff2 = mgr.effective(group2, group2["sessions"][0])
    assert eff2["message_type"] == "FriendMessage"
    assert eff2["chat_group_id"] is None
    assert "auto_at" not in eff2

    # 会话覆盖组配置；None 恢复继承组
    mgr.update_session(session["id"], message_type="FriendMessage")
    eff3 = mgr.effective(group, session)
    assert eff3["message_type"] == "FriendMessage"
    mgr.update_session(session["id"], message_type=None, chat_group_id=None)
    eff4 = mgr.effective(group, session)
    assert eff4["message_type"] == "GroupMessage"
    assert eff4["chat_group_id"] == "cg_1"


def test_identity_store_crud(tmp_path):
    store = IdentityStore(data_dir=tmp_path)
    ident = store.create_identity("小明", "xiaoming", "小明同学")
    assert ident["sender_id"] == "xiaoming"
    assert ident["sender_name"] == "小明同学"

    # 重新加载（新实例）确认持久化
    store2 = IdentityStore(data_dir=tmp_path)
    assert store2.get_identity(ident["id"])["sender_id"] == "xiaoming"

    # sender 缺省回退名称
    ident2 = store2.create_identity("小红")
    assert ident2["sender_id"] == "小红"
    assert ident2["sender_name"] == "小红"

    # 更新；未传字段保持不变
    updated = store2.update_identity(ident["id"], name="小刚", sender_id="xiaogang")
    assert updated["sender_id"] == "xiaogang"
    assert updated["sender_name"] == "小明同学"

    # 空串重置为名称回退
    store2.update_identity(ident["id"], sender_id="")
    assert store2.get_identity(ident["id"])["sender_id"] == "小刚"

    # 删除
    assert store2.delete_identities([ident["id"]]) == 1
    assert store2.get_identity(ident["id"]) is None
    # 更新不存在的身份 → None
    assert store2.update_identity("id_none", name="x") is None


def test_chat_group_store_crud(tmp_path):
    store = ChatGroupStore(data_dir=tmp_path)
    grp = store.create_chat_group("测试群", ["id_a", "id_b"])
    assert grp["member_ids"] == ["id_a", "id_b"]
    # 清洗：非字符串 / 空串 / 重复去除
    grp2 = store.create_chat_group("空群", ["", None, "id_x", "id_x"])
    assert grp2["member_ids"] == ["id_x"]

    # 重新加载（新实例）确认持久化
    store2 = ChatGroupStore(data_dir=tmp_path)
    assert store2.get_chat_group(grp["id"])["name"] == "测试群"

    # 更新成员
    updated = store2.update_chat_group(grp["id"], member_ids=["id_c"])
    assert updated["member_ids"] == ["id_c"]

    # 删除
    assert store2.delete_chat_groups([grp["id"]]) == 1
    assert store2.get_chat_group(grp["id"]) is None


@pytest.mark.asyncio
async def test_stream_store_append_read_clear(tmp_path):
    store = StreamStore(data_dir=tmp_path)
    mid = await store.append(
        "vs_1",
        {
            "role": "user",
            "sender_id": "u1",
            "sender_name": "用户1",
            "text": "hi",
            "at_bot": True,
        },
    )
    msgs = await store.read_stream("vs_1")
    assert len(msgs) == 1
    assert msgs[0]["id"] == mid
    assert msgs[0]["text"] == "hi"
    assert msgs[0]["at_bot"] is True
    # 不存在的会话返回空列表
    assert await store.read_stream("vs_none") == []
    # 更新回复状态
    await store.update_reply("vs_1", mid, "ok")
    assert (await store.read_stream("vs_1"))[0]["reply_status"] == "ok"
    # 清空
    await store.clear("vs_1")
    assert await store.read_stream("vs_1") == []


@pytest.mark.asyncio
async def test_stream_store_truncate_oldest(tmp_path):
    store = StreamStore(data_dir=tmp_path)
    for i in range(MAX_STREAM_MESSAGES + 5):
        await store.append("vs_1", {"role": "user", "text": str(i)})
    msgs = await store.read_stream("vs_1")
    assert len(msgs) == MAX_STREAM_MESSAGES
    assert msgs[0]["text"] == str(5)  # 最旧 5 条被截断
    assert msgs[-1]["text"] == str(MAX_STREAM_MESSAGES + 4)


@pytest.mark.asyncio
async def test_stream_store_delete_sessions(tmp_path):
    store = StreamStore(data_dir=tmp_path)
    await store.append("vs_1", {"role": "user", "text": "a"})
    await store.append("vs_2", {"role": "user", "text": "b"})
    await store.delete_sessions(["vs_1"])
    assert await store.read_stream("vs_1") == []
    assert len(await store.read_stream("vs_2")) == 1


@pytest.mark.asyncio
async def test_stream_store_jsonl_reload(tmp_path):
    """JSONL 追加式：append/reply 逐行追加（无全量包裹），新实例重载重建内存态。"""
    store = StreamStore(data_dir=tmp_path)
    mid = await store.append("vs_1", {"role": "user", "sender_id": "u1", "text": "hi"})
    await store.append("vs_1", {"role": "bot", "text": "hello"})
    await store.update_reply("vs_1", mid, "ok")
    lines = (
        (tmp_path / "virtual_session" / "streams.jsonl")
        .read_text(encoding="utf-8")
        .strip()
        .splitlines()
    )
    assert len(lines) == 3  # 2 append + 1 reply
    assert all(json.loads(line)["op"] in ("append", "reply") for line in lines)
    store2 = StreamStore(data_dir=tmp_path)
    msgs = await store2.read_stream("vs_1")
    assert [m["text"] for m in msgs] == ["hi", "hello"]
    assert msgs[0]["reply_status"] == "ok"


@pytest.mark.asyncio
async def test_stream_store_concurrent_appends(tmp_path):
    """并发 append（重叠发送）经实例锁串行写：无丢行，重载后与内存态一致。"""
    store = StreamStore(data_dir=tmp_path)
    n = 50
    await asyncio.gather(
        *[store.append("vs_1", {"role": "user", "text": str(i)}) for i in range(n)]
    )
    msgs = await store.read_stream("vs_1")
    assert len(msgs) == n
    assert {m["text"] for m in msgs} == {str(i) for i in range(n)}
    store2 = StreamStore(data_dir=tmp_path)
    msgs2 = await store2.read_stream("vs_1")
    assert len(msgs2) == n
    assert {m["text"] for m in msgs2} == {str(i) for i in range(n)}


@pytest.mark.asyncio
async def test_stream_store_compaction(tmp_path, monkeypatch):
    """日志行数超阈值后 append 改为全量重写：日志有界，重载后内容正确。"""
    monkeypatch.setattr(stm_mod, "_COMPACT_LINES", 5)
    store = StreamStore(data_dir=tmp_path)
    for i in range(12):
        mid = await store.append("vs_1", {"role": "user", "text": str(i)})
        await store.update_reply("vs_1", mid, "ok")
    lines = (
        (tmp_path / "virtual_session" / "streams.jsonl")
        .read_text(encoding="utf-8")
        .strip()
        .splitlines()
    )
    # 不压缩时 12 append + 12 reply = 24 行；压缩后日志被重写收敛
    assert len(lines) < 24
    store2 = StreamStore(data_dir=tmp_path)
    msgs = await store2.read_stream("vs_1")
    assert len(msgs) == 12
    assert all(m["reply_status"] == "ok" for m in msgs)


@pytest.mark.asyncio
async def test_stream_store_corrupt_line_tolerated(tmp_path):
    """TB-30: 单条损坏行（半截 JSON，崩溃遗留）在重载时跳过，其余行正常回放。"""
    store = StreamStore(data_dir=tmp_path)
    await store.append("vs_1", {"role": "user", "text": "ok1"})
    await store.append("vs_2", {"role": "user", "text": "ok2"})
    # 模拟崩溃遗留的半截行
    with (tmp_path / "virtual_session" / "streams.jsonl").open(
        "a", encoding="utf-8"
    ) as f:
        f.write('{"op": "append", "session_id": "vs_9", "message": {"text": "半截')
    store2 = StreamStore(data_dir=tmp_path)
    assert [m["text"] for m in await store2.read_stream("vs_1")] == ["ok1"]
    assert [m["text"] for m in await store2.read_stream("vs_2")] == ["ok2"]


@pytest.mark.asyncio
async def test_group_store_concurrent_write(tmp_path):
    """非流 store 并发写经 write（实例锁内线程化）：无丢失更新。"""
    mgr = VirtualGroupManager(data_dir=tmp_path)

    async def make(i: int) -> str:
        group = await mgr.write(mgr.create_group, f"组{i}", count=1)
        return group["id"]

    ids = await asyncio.gather(*[make(i) for i in range(20)])
    assert len(ids) == 20
    assert len(mgr.list_groups()) == 20
    mgr2 = VirtualGroupManager(data_dir=tmp_path)
    assert len(mgr2.list_groups()) == 20
    assert {g["name"] for g in mgr2.list_groups()} == {f"组{i}" for i in range(20)}


@pytest.mark.asyncio
async def test_identity_store_concurrent_write(tmp_path):
    """_ListStore 并发写经 write 串行：不丢身份，重载后一致。"""
    store = IdentityStore(data_dir=tmp_path)

    async def make(i: int) -> dict:
        return await store.write(store.create_identity, f"身份{i}")

    await asyncio.gather(*[make(i) for i in range(20)])
    assert len(store.list_identities()) == 20
    store2 = IdentityStore(data_dir=tmp_path)
    assert len(store2.list_identities()) == 20


def test_identity_is_admin_crud(tmp_path):
    """身份 is_admin：创建默认 False，可显式 True；更新显式 false 生效、未传不变。"""
    store = IdentityStore(data_dir=tmp_path)
    ident = store.create_identity("小明", "xiaoming", "小明同学")
    assert ident["is_admin"] is False
    admin = store.create_identity("管理员", "root", is_admin=True)
    assert admin["is_admin"] is True

    # 更新：显式 false 生效（前端取消勾选必须落盘）
    store.update_identity(admin["id"], is_admin=False)
    assert store.get_identity(admin["id"])["is_admin"] is False
    # 未传字段保持不变
    store.update_identity(ident["id"], name="小刚")
    assert store.get_identity(ident["id"])["is_admin"] is False

    # 重新加载确认持久化
    store2 = IdentityStore(data_dir=tmp_path)
    assert store2.get_identity(admin["id"])["is_admin"] is False
    assert store2.get_identity(ident["id"])["is_admin"] is False

    # 旧数据缺 is_admin 键 → 加载不崩溃，读取由调用方 .get 兜底
    import json as _json

    (tmp_path / "virtual_session" / "identities.json").write_text(
        _json.dumps(
            {
                "items": [
                    {
                        "id": "id_old",
                        "name": "旧身份",
                        "sender_id": "old",
                        "sender_name": "旧",
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    store3 = IdentityStore(data_dir=tmp_path)
    old = store3.get_identity("id_old")
    assert old.get("is_admin", False) is False


def test_identity_admin_index(tmp_path):
    """is_admin_of 惰性索引：命中任一管理员身份即真；create/update/delete 失效重建。"""
    store = IdentityStore(data_dir=tmp_path)
    assert store._admin_index is None  # 惰性：查询前不构建
    assert store.is_admin_of("nobody") is False  # 空库首次构建

    admin = store.create_identity("管理员", "admin_1", is_admin=True)
    store.create_identity("普通成员", "member_1")
    # 创建使索引失效，查询重建 → 命中新管理员
    assert store.is_admin_of("admin_1") is True
    assert store.is_admin_of("member_1") is False

    # 同一 sender_id 对应多个身份：任一管理员即真（与旧 _resolve_role 语义一致）
    store.create_identity("同名成员", "admin_1")
    assert store.is_admin_of("admin_1") is True

    # 更新降级：is_admin 显式 false → 索引重建后不再命中
    store.update_identity(admin["id"], is_admin=False)
    assert store.is_admin_of("admin_1") is False

    # 更新改 sender_id：旧 id 不再命中、新 id 命中
    store.update_identity(admin["id"], sender_id="boss", is_admin=True)
    assert store.is_admin_of("boss") is True
    assert store.is_admin_of("admin_1") is False

    # 删除管理员 → 不再命中
    store.delete_identities([admin["id"]])
    assert store.is_admin_of("boss") is False

    # 重新加载：索引从持久化数据重建
    store2 = IdentityStore(data_dir=tmp_path)
    assert store2.is_admin_of("boss") is False
    assert store2.is_admin_of("admin_1") is False

    # 空串 sender_id + is_admin：索引与旧逐条扫描谓词等价（`"" == ""` 命中）
    (tmp_path / "virtual_session" / "identities.json").write_text(
        json.dumps(
            {
                "items": [
                    {"id": "id_empty", "name": "空", "sender_id": "", "is_admin": True}
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    store3 = IdentityStore(data_dir=tmp_path)
    assert store3.is_admin_of("") is True
    assert store3.is_admin_of("anything") is False


def test_identity_admin_index_degenerate_data(tmp_path):
    """索引对脏数据健壮：非 str sender_id（is_admin 真）与旧数据缺 is_admin 键均不命中且不崩。"""
    (tmp_path / "virtual_session").mkdir(parents=True, exist_ok=True)
    (tmp_path / "virtual_session" / "identities.json").write_text(
        json.dumps(
            {
                "items": [
                    {"id": "n1", "name": "null", "sender_id": None, "is_admin": True},
                    {"id": "n2", "name": "num", "sender_id": 123, "is_admin": True},
                    {"id": "n3", "name": "list", "sender_id": ["x"], "is_admin": True},
                    {"id": "l1", "name": "旧身份", "sender_id": "legacy_admin"},
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    store = IdentityStore(data_dir=tmp_path)
    # 构建索引不崩；非 str sender_id 被 isinstance 守卫排除 → 查询不命中
    assert store.is_admin_of("123") is False
    assert store.is_admin_of(123) is False
    assert store.is_admin_of("x") is False
    # 旧数据缺 is_admin 键 → 非管理员
    assert store.is_admin_of("legacy_admin") is False


def test_reviewer_store_crud(tmp_path):
    store = ReviewerStore(data_dir=tmp_path)
    assert store.list_profiles() == []
    assert store.get_profile("rp_none") is None

    p = store.create_profile(
        {
            "name": "评审",
            "provider_id": "prov_r",
            "model": "m",
            "system_prompt": "提示词",
            "context": "record",
            "metrics": [{"key": "score", "type": "number"}],
        }
    )
    assert p["id"].startswith("rp_")
    assert store.get_profile(p["id"]) is not None

    # 缺省归一：空名回退「评审」、context 缺省 reply、metrics 缺省 []
    p2 = store.create_profile({"name": "  ", "metrics": []})
    assert p2["name"] == "评审"
    assert p2["context"] == "reply"
    assert p2["metrics"] == []

    # 部分更新 + 未传字段保持不变
    updated = store.update_profile(p["id"], {"name": "新名", "context": "reply"})
    assert updated["name"] == "新名" and updated["context"] == "reply"
    assert updated["provider_id"] == "prov_r"

    # 持久化
    reloaded = ReviewerStore(data_dir=tmp_path)
    assert len(reloaded.list_profiles()) == 2

    assert store.delete_profiles([p["id"], "rp_none"]) == 1
    assert store.delete_profiles([p2["id"]]) == 1
    assert store.list_profiles() == []


def test_report_store_crud(tmp_path):
    """报告存储：创建 / 列表（按测试集过滤、倒序）/ 查询 / 删除 / 级联删除 / 持久化。"""
    store = ReportStore(data_dir=tmp_path)
    assert store.list_reports() == []
    assert store.get_report("rp_none") is None

    r1 = store.add_report("ts_1", "tr_1", {"status": "done"})
    assert r1["id"].startswith("rp_")
    store.add_report("ts_2", "tr_2", {"status": "done"})
    assert len(store.list_reports()) == 2

    # 按测试集过滤；其他测试集的报告不返回
    assert [r["id"] for r in store.list_reports(testset_id="ts_1")] == [r1["id"]]
    assert store.list_reports(testset_id="ts_none") == []

    assert store.get_report(r1["id"])["testset_id"] == "ts_1"

    # 删除（含不存在的 id 一并跳过）
    assert store.delete_reports([r1["id"], "rp_none"]) == 1
    assert store.get_report(r1["id"]) is None

    # 级联删除指定测试集产出的全部报告
    assert store.delete_for_testsets(["ts_2"]) == 1
    assert store.list_reports() == []

    # 持久化：重载后仍在
    store.add_report("ts_3", "tr_3", {"status": "error"})
    reloaded = ReportStore(data_dir=tmp_path)
    assert len(reloaded.list_reports()) == 1
    assert reloaded.list_reports()[0]["testset_id"] == "ts_3"
