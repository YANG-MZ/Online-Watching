"""RoomManager 领域服务测试(design.md 4.3/4.4/4.5)。

使用真实 P1 实现(Sqlite 仓储 + LocalVideoStorage,临时目录)聚焦领域逻辑;
WebSocket 连接以轻量对象替身(RoomManager 仅持有引用,不调用其方法)。
"""

from pathlib import Path

import pytest

from server.app.config import Settings, StorageSettings
from server.app.db import SqliteDatabase, SqliteRoomRepository, SqliteVideoRepository
from server.app.models import VideoMeta
from server.app.playback import PlaybackService
from server.app.room_manager import (
    InvalidNicknameError,
    InvalidPasswordError,
    PasswordIncorrectError,
    PasswordRequiredError,
    RoomFullError,
    RoomLimitReachedError,
    RoomManager,
    RoomNotFoundError,
    UnauthorizedError,
    ForbiddenError,
    UploadInProgressError,
)
from server.app.video_storage import LocalVideoStorage


class FakeConn:
    """WS 连接替身:仅作身份引用(kicked 事件断言用)。"""

    def __init__(self) -> None:
        self.token = object()


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(storage=StorageSettings(data_dir=str(tmp_path / "data")))


@pytest.fixture
async def env(settings, tmp_path):
    """组装 RoomManager 及其依赖(真实 P1 实现 + 临时目录)。"""
    db = SqliteDatabase(tmp_path / "data" / "app.db")
    room_repo = SqliteRoomRepository(db)
    video_repo = SqliteVideoRepository(db)
    storage = LocalVideoStorage(tmp_path / "data", max_upload_bytes=1024 * 1024)
    manager = RoomManager(settings, room_repo, video_repo, storage, PlaybackService())
    yield manager, room_repo, video_repo, storage, db
    db.close()


async def enter_host(m: RoomManager, nickname: str, t: float):
    """建房并完成 WS 进入,返回 (凭证, AuthResult)。"""
    cred = await m.create_room(nickname, t)
    result = await m.authenticate(cred.room_id, cred.session_token, FakeConn(), t)
    return cred, result


async def enter_member(m: RoomManager, room_id: str, nickname: str, t: float):
    """join + WS 进入,返回 (凭证, AuthResult)。"""
    cred = await m.join_room(room_id, nickname, t)
    result = await m.authenticate(cred.room_id, cred.session_token, FakeConn(), t)
    return cred, result


def video_meta(vid: str, size: int = 10) -> VideoMeta:
    return VideoMeta(video_id=vid, original_name=f"{vid}.mp4", size=size)


# ---------- 创建房间(需求 3.1.1) ----------

async def test_create_room_host_credentials_and_db_row(env) -> None:
    m, _, _, _, db = env
    cred = await m.create_room("Alice", now=1000.0)
    assert cred.is_host is True
    assert len(cred.room_id) == 8
    assert len(cred.session_token) == 43

    room = m.get_room(cred.room_id)
    assert room is not None
    assert room.host_user_id == cred.user_id
    assert room.members == {}  # 创建不占在线名额(design.md 4.4)
    assert m.room_count() == 1
    assert db.conn.execute("SELECT COUNT(*) FROM rooms").fetchone()[0] == 1


async def test_create_room_invalid_nickname(env) -> None:
    m, *_ = env
    for bad in ("", "   ", "x" * 33, None):
        with pytest.raises(InvalidNicknameError):
            await m.create_room(bad, now=0.0)  # type: ignore[arg-type]


async def test_create_room_starts_empty_countdown(env) -> None:
    """创建即空房倒计时,首个 WS auth 复活(api.md 2.1,决策 T14)。"""
    m, *_ = env
    cred = await m.create_room("Alice", now=1000.0)
    room = m.get_room(cred.room_id)
    assert room.empty_since == 1000.0  # 未上线即倒计时

    await m.authenticate(cred.room_id, cred.session_token, FakeConn(), now=1001.0)
    assert room.empty_since is None  # 上线复活


async def test_create_room_limit_rejected(env) -> None:
    """第 6 个房间被拒(默认上限 5,验收 16)。"""
    m, *_ = env
    for i in range(5):
        await m.create_room(f"user{i}", now=float(i))
    with pytest.raises(RoomLimitReachedError):
        await m.create_room("sixth", now=10.0)


# ---------- 加入与 WS 鉴权(需求 3.1.2,决策 T13/T15) ----------

async def test_join_room_and_authenticate(env) -> None:
    m, *_ = env
    host_cred, _ = await enter_host(m, "Alice", t=1000.0)

    cred, result = await enter_member(m, host_cred.room_id, "Bob", t=1001.0)
    assert cred.is_host is False
    room = m.get_room(host_cred.room_id)
    assert len(room.members) == 2
    types = [e.message["type"] for e in result.events]
    assert types == ["member_joined"]


async def test_authenticate_room_not_found(env) -> None:
    m, *_ = env
    with pytest.raises(RoomNotFoundError):
        await m.authenticate("NOPE0000", "token", FakeConn(), now=0.0)


async def test_authenticate_bad_token(env) -> None:
    m, *_ = env
    host_cred, _ = await enter_host(m, "Alice", t=1000.0)
    with pytest.raises(UnauthorizedError):
        await m.authenticate(host_cred.room_id, "not-a-token", FakeConn(), now=1001.0)


async def test_authenticate_room_full_online_capacity(env) -> None:
    """满员按在线口径:10 人在线后第 11 人 WS 鉴权被拒(验收 16)。"""
    m, *_ = env
    host_cred, _ = await enter_host(m, "host", t=1000.0)
    for i in range(9):
        await enter_member(m, host_cred.room_id, f"m{i}", t=1001.0 + i)
    with pytest.raises(RoomFullError):
        await enter_member(m, host_cred.room_id, "eleventh", t=1100.0)


async def test_duplicate_connection_kicks_old(env) -> None:
    """同 token 第二条连接:旧连接被标记踢除,成员不重复(决策 T15/验收 8)。"""
    m, *_ = env
    host_cred, first = await enter_host(m, "Alice", t=1000.0)
    old_conn = first.member.conn

    new_conn = FakeConn()
    result = await m.authenticate(host_cred.room_id, host_cred.session_token, new_conn, now=1010.0)

    types = [e.message["type"] for e in result.events]
    assert types == ["kicked", "member_joined"]
    kicked_evt = result.events[0]
    assert kicked_evt.kick_conn is old_conn  # ws 层将向其单发并 close 4004
    room = m.get_room(host_cred.room_id)
    assert len(room.members) == 1  # 不重复
    assert room.members[host_cred.user_id].conn is new_conn


async def test_reconnect_keeps_joined_at_and_nickname(env) -> None:
    """重连沿用首次 joined_at 与昵称(决策 T15,移交排序依据)。"""
    m, *_ = env
    host_cred, first = await enter_host(m, "Alice", t=1000.0)
    original_joined = first.member.joined_at

    second = await m.authenticate(host_cred.room_id, host_cred.session_token, FakeConn(), now=5000.0)
    assert second.member.joined_at == original_joined
    assert second.member.nickname == "Alice"


# ---------- 退出 / 移交 / 复活(需求 3.1.4/3.1.5/3.1.7) ----------

async def test_leave_room_idempotent(env) -> None:
    m, *_ = env
    host_cred, _ = await enter_host(m, "Alice", t=1000.0)
    events = await m.leave_room(host_cred.room_id, host_cred.session_token, now=1001.0)
    assert [e.message["type"] for e in events] == ["member_left"]
    assert events[0].message["reason"] == "leave"

    again = await m.leave_room(host_cred.room_id, host_cred.session_token, now=1002.0)
    assert again == []  # 不在线幂等成功


async def test_empty_since_and_revival(env) -> None:
    """全员离开进入倒计时;窗口内重连即复活(决策 T14)。"""
    m, *_ = env
    host_cred, _ = await enter_host(m, "Alice", t=1000.0)
    room = m.get_room(host_cred.room_id)
    assert room.empty_since is None

    await m.leave_room(host_cred.room_id, host_cred.session_token, now=1010.0)
    assert room.empty_since == 1010.0

    await m.authenticate(host_cred.room_id, host_cred.session_token, FakeConn(), now=1100.0)
    assert room.empty_since is None  # 复活


async def test_host_transfer_to_earliest_member(env) -> None:
    """房主主动退出移交给加入最早成员;原房主再进为普通成员(验收 13)。"""
    m, *_ = env
    host_cred, _ = await enter_host(m, "Alice", t=1000.0)
    bob_cred, _ = await enter_member(m, host_cred.room_id, "Bob", t=1001.0)
    carol_cred, _ = await enter_member(m, host_cred.room_id, "Carol", t=1002.0)

    events = await m.leave_room(host_cred.room_id, host_cred.session_token, now=1010.0)
    types = [e.message["type"] for e in events]
    assert types == ["member_left", "host_changed"]
    transfer = events[1].message
    assert transfer["host_user_id"] == bob_cred.user_id  # Bob(t=1001)最早

    # 房主权限验证:B 可解散,A 重新加入后不可
    assert m.is_host(m.get_room(host_cred.room_id), bob_cred.user_id)
    assert not m.is_host(m.get_room(host_cred.room_id), carol_cred.user_id)

    back_cred, _ = await enter_member(m, host_cred.room_id, "Alice", t=1020.0)
    assert not m.is_host(m.get_room(host_cred.room_id), back_cred.user_id)


async def test_host_leave_alone_no_transfer(env) -> None:
    """房主最后一人退出:不移交,房间进入倒计时(需求 3.1.5)。"""
    m, *_ = env
    host_cred, _ = await enter_host(m, "Alice", t=1000.0)
    events = await m.leave_room(host_cred.room_id, host_cred.session_token, now=1001.0)
    assert [e.message["type"] for e in events] == ["member_left"]
    room = m.get_room(host_cred.room_id)
    assert room.host_user_id == host_cred.user_id
    assert room.empty_since is not None


async def test_host_timeout_no_transfer_reconnect_restores(env) -> None:
    """房主掉线(心跳超时)不移交;重连恢复控制权(验收 14)。"""
    m, *_ = env
    host_cred, _ = await enter_host(m, "Alice", t=1000.0)
    bob_cred, _ = await enter_member(m, host_cred.room_id, "Bob", t=1001.0)

    # Alice 于 t=1000 后无消息(t=1100 时已超 90s 阈值);Bob 在 t=1050 有活动
    m.touch(m.get_room(host_cred.room_id), bob_cred.user_id, now=1050.0)
    events = m.sweep_timeouts(now=1100.0)  # Alice 100s 无消息超时,Bob 50s 正常

    assert [e.message for e in events] == [
        {"type": "member_left", "user_id": host_cred.user_id, "reason": "timeout"}
    ]
    room = m.get_room(host_cred.room_id)
    assert room.host_user_id == host_cred.user_id  # 不移交
    assert len(room.members) == 1
    assert room.empty_since is None  # Bob 仍在线

    # Alice 重连恢复房主身份
    result = await m.authenticate(host_cred.room_id, host_cred.session_token, FakeConn(), now=1110.0)
    assert result.member.user_id == host_cred.user_id
    assert m.is_host(room, host_cred.user_id)


async def test_sweep_all_timeout_sets_empty_since(env) -> None:
    m, *_ = env
    host_cred, _ = await enter_host(m, "Alice", t=1000.0)
    events = m.sweep_timeouts(now=2000.0)
    assert len(events) == 1
    room = m.get_room(host_cred.room_id)
    assert room.members == {}
    assert room.empty_since == 2000.0


# ---------- 解散(需求 3.1.6,验收 15) ----------

async def test_dissolve_room_cleans_everything(env) -> None:
    m, _, video_repo, storage, db = env
    host_cred, _ = await enter_host(m, "Alice", t=1000.0)
    room = m.get_room(host_cred.room_id)

    m.begin_upload(host_cred.room_id, host_cred.session_token)
    await storage.write_upload("up1", _chunks(b"video-bytes"))
    dest = await storage.promote_upload("up1", "vid1")
    await m.complete_upload(
        host_cred.room_id, host_cred.session_token,
        video_meta("vid1", size=len(b"video-bytes")),
        Path("videos/vid1.mp4"), now=1002.0,
    )
    assert storage.stat_size("vid1") == len(b"video-bytes")

    events = await m.dissolve_room(host_cred.room_id, host_cred.session_token, now=1003.0)
    assert [e.message["type"] for e in events] == ["room_dissolved"]

    assert m.get_room(host_cred.room_id) is None                 # 内存
    assert storage.stat_size("vid1") is None                     # 文件
    assert db.conn.execute("SELECT COUNT(*) FROM rooms").fetchone()[0] == 0
    assert await video_repo.list_by_room(host_cred.room_id) == []  # DB 行


async def test_dissolve_permission(env) -> None:
    """普通成员/无效凭证解散被拒(需求 3.2.2,验收 9)。"""
    m, *_ = env
    host_cred, _ = await enter_host(m, "Alice", t=1000.0)
    bob_cred, _ = await enter_member(m, host_cred.room_id, "Bob", t=1001.0)

    with pytest.raises(ForbiddenError):
        await m.dissolve_room(host_cred.room_id, bob_cred.session_token, now=1002.0)
    with pytest.raises(UnauthorizedError):
        await m.dissolve_room(host_cred.room_id, "bad-token", now=1002.0)
    assert m.get_room(host_cred.room_id) is not None


# ---------- 过期清理(需求 3.1.7,验收 10) ----------

async def test_purge_expired_rooms(env) -> None:
    """空房 10 分钟内保留、超时销毁(含视频文件与 DB 行)。"""
    m, _, _, storage, db = env
    host_cred, _ = await enter_host(m, "Alice", t=1000.0)
    room = m.get_room(host_cred.room_id)

    m.begin_upload(host_cred.room_id, host_cred.session_token)
    await storage.write_upload("up1", _chunks(b"data"))
    await storage.promote_upload("up1", "vid1")
    await m.complete_upload(
        host_cred.room_id, host_cred.session_token,
        video_meta("vid1", 4), Path("videos/vid1.mp4"), now=1001.0,
    )

    await m.leave_room(host_cred.room_id, host_cred.session_token, now=1010.0)
    assert room.empty_since == 1010.0

    assert await m.purge_expired_rooms(now=1010.0 + 599.0) == []  # 未到 10 分钟
    assert m.get_room(host_cred.room_id) is not None

    assert await m.purge_expired_rooms(now=1010.0 + 600.0) == [host_cred.room_id]
    assert m.get_room(host_cred.room_id) is None
    assert storage.stat_size("vid1") is None
    assert db.conn.execute("SELECT COUNT(*) FROM rooms").fetchone()[0] == 0


# ---------- 上传三段(design.md 4.7,验收 12) ----------

async def _complete(m, room_id, token, vid: str, t: float, size: int = 4):
    m.begin_upload(room_id, token)  # 同步方法:预检 + 置互斥锁
    return await m.complete_upload(
        room_id, token, video_meta(vid, size), Path(f"videos/{vid}.mp4"), now=t
    )


async def test_upload_first_sets_video_and_resets_playback(env) -> None:
    """首次上传:video_ready + 状态重置(paused/0/1.0,version+1)(验收 12)。"""
    m, *_ = env
    host_cred, _ = await enter_host(m, "Alice", t=1000.0)
    room = m.get_room(host_cred.room_id)
    room.playback.state = "playing"
    room.playback.position = 500.0
    room.playback.version = 7

    event, state_ev = await _complete(m, host_cred.room_id, host_cred.session_token, "vid1", t=1001.0)

    assert event.message["type"] == "video_ready"
    # 视频变更伴随重置后的 state_update 广播(api.md 3.4)
    assert state_ev.message["type"] == "state_update"
    assert state_ev.message["version"] == 8 and state_ev.message["state"] == "paused"
    assert room.video.video_id == "vid1"
    assert room.uploading is False
    pb = room.playback
    assert (pb.state, pb.position, pb.rate, pb.version) == ("paused", 0.0, 1.0, 8)


async def test_upload_replace_emits_changed_and_removes_old(env) -> None:
    """替换上传:video_changed + 旧文件/旧行删除 + 状态再重置(需求 D4/验收 12)。"""
    m, _, video_repo, storage, _ = env
    host_cred, _ = await enter_host(m, "Alice", t=1000.0)

    m.begin_upload(host_cred.room_id, host_cred.session_token)
    await storage.write_upload("up1", _chunks(b"old"))
    await storage.promote_upload("up1", "vid1")
    await m.complete_upload(
        host_cred.room_id, host_cred.session_token,
        video_meta("vid1", 3), Path("videos/vid1.mp4"), now=1001.0,
    )

    m.begin_upload(host_cred.room_id, host_cred.session_token)
    await storage.write_upload("up2", _chunks(b"new!"))
    await storage.promote_upload("up2", "vid2")
    event, state_ev = await m.complete_upload(
        host_cred.room_id, host_cred.session_token,
        video_meta("vid2", 4), Path("videos/vid2.mp4"), now=1002.0,
    )

    assert event.message["type"] == "video_changed"
    assert state_ev.message["type"] == "state_update"
    room = m.get_room(host_cred.room_id)
    assert room.video.video_id == "vid2"
    assert storage.stat_size("vid1") is None   # 旧文件已删
    assert storage.stat_size("vid2") == 4
    rows = await video_repo.list_by_room(host_cred.room_id)
    assert [v.video_id for v, _ in rows] == ["vid2"]  # 旧行已删


async def test_upload_mutex_and_fail_release(env) -> None:
    """上传互斥(409 语义);失败释放锁后可重新上传。"""
    m, *_ = env
    host_cred, _ = await enter_host(m, "Alice", t=1000.0)
    room = m.get_room(host_cred.room_id)

    m.begin_upload(host_cred.room_id, host_cred.session_token)
    assert room.uploading is True
    with pytest.raises(UploadInProgressError):
        m.begin_upload(host_cred.room_id, host_cred.session_token)

    m.fail_upload(host_cred.room_id)
    assert room.uploading is False
    m.begin_upload(host_cred.room_id, host_cred.session_token)  # 可重新开始
    m.fail_upload(host_cred.room_id)
    m.fail_upload(host_cred.room_id)  # 幂等
    m.fail_upload("GONE0000")  # 房间已不存在也不报错


async def test_upload_permission_and_abort_check(env) -> None:
    """上传权限:非房主 begin 被拒(403 语义);abort_check 反映房主变更/解散。"""
    m, *_ = env
    host_cred, _ = await enter_host(m, "Alice", t=1000.0)
    bob_cred, _ = await enter_member(m, host_cred.room_id, "Bob", t=1001.0)

    with pytest.raises(ForbiddenError):
        m.begin_upload(host_cred.room_id, bob_cred.session_token)

    # 房主凭证 → False(继续);成员/无效凭证/已解散 → True(中止)
    assert m.upload_abort_check(host_cred.room_id, host_cred.session_token) is False
    assert m.upload_abort_check(host_cred.room_id, bob_cred.session_token) is True
    assert m.upload_abort_check(host_cred.room_id, "bad-token") is True
    assert m.upload_abort_check("GONE0000", host_cred.session_token) is True

    # 房主主动退出移交给 Bob 后,原房主上传应被中止复查命中
    await m.leave_room(host_cred.room_id, host_cred.session_token, now=1010.0)
    assert m.upload_abort_check(host_cred.room_id, host_cred.session_token) is True
    assert m.upload_abort_check(host_cred.room_id, bob_cred.session_token) is False


# ---------- 快照与成员解析(需求 3.7,api.md 3.2) ----------

async def test_snapshot_payload_structure(env) -> None:
    m, *_ = env
    host_cred, _ = await enter_host(m, "Alice", t=1000.0)
    bob_cred, _ = await enter_member(m, host_cred.room_id, "Bob", t=1001.0)
    await _complete(m, host_cred.room_id, host_cred.session_token, "vid1", t=1002.0)

    room = m.get_room(host_cred.room_id)
    snap = m.snapshot_payload(room, for_user_id=bob_cred.user_id)

    assert snap["you"] == {"user_id": bob_cred.user_id, "is_host": False}
    inner = snap["room"]
    assert inner["room_id"] == host_cred.room_id
    assert inner["host_user_id"] == host_cred.user_id
    assert [u["user_id"] for u in inner["members"]] == [host_cred.user_id, bob_cred.user_id]  # joined_at 序
    assert inner["members"][0]["is_host"] is True
    assert inner["members"][1]["is_host"] is False
    assert set(inner["playback"]) == {"state", "position", "rate", "updated_at", "version"}
    assert inner["video"] == {"video_id": "vid1", "original_name": "vid1.mp4", "size": 4}


async def test_snapshot_without_video(env) -> None:
    m, *_ = env
    host_cred, _ = await enter_host(m, "Alice", t=1000.0)
    snap = m.snapshot_payload(m.get_room(host_cred.room_id), host_cred.user_id)
    assert snap["room"]["video"] is None


async def test_resolve_member_and_touch(env) -> None:
    m, *_ = env
    host_cred, _ = await enter_host(m, "Alice", t=1000.0)
    bob_cred, _ = await enter_member(m, host_cred.room_id, "Bob", t=1001.0)

    found = m.resolve_member(host_cred.room_id, bob_cred.session_token)
    assert found is not None
    room, member = found
    assert member.user_id == bob_cred.user_id

    m.touch(room, bob_cred.user_id, now=1500.0)
    assert member.last_seen == 1500.0

    # 掉线移除后解析为 None;错误房间/无效 token 为 None
    room.members.pop(bob_cred.user_id)
    assert m.resolve_member(host_cred.room_id, bob_cred.session_token) is None
    assert m.resolve_member("GONE0000", host_cred.session_token) is None
    assert m.resolve_member(host_cred.room_id, "bad-token") is None


# ---------- 工具 ----------

async def _chunks(*chunks: bytes):
    for c in chunks:
        yield c


# ---------- 房间密码(N3) ----------

async def test_create_room_with_password_and_join_flow(env) -> None:
    """创建带密码房间:无密码/错密码/对密码三种加入结果。"""
    m, *_ = env
    host_cred = await m.create_room("Alice", now=1000.0, password="secret123")
    room = m.get_room(host_cred.room_id)
    assert room.password_hash is not None
    assert room.password_hash != "secret123"  # 哈希存储,不落明文

    with pytest.raises(PasswordRequiredError):
        await m.join_room(host_cred.room_id, "Bob", now=1001.0)  # 未携带
    with pytest.raises(PasswordIncorrectError):
        await m.join_room(host_cred.room_id, "Bob", now=1001.0, password="wrong")

    cred = await m.join_room(host_cred.room_id, "Bob", now=1001.0, password="secret123")
    assert cred.session_token  # 正确密码签发凭证,重连无需再输


async def test_create_room_without_password_open_join(env) -> None:
    """无密码房间:加入无需密码(兼容现状)。"""
    m, *_ = env
    host_cred = await m.create_room("Alice", now=1000.0)
    assert m.get_room(host_cred.room_id).password_hash is None
    cred = await m.join_room(host_cred.room_id, "Bob", now=1001.0)
    assert cred.session_token


async def test_password_too_long_rejected(env) -> None:
    m, *_ = env
    with pytest.raises(InvalidPasswordError):
        await m.create_room("Alice", now=1000.0, password="x" * 65)
