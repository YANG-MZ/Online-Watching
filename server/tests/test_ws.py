"""WebSocket 协议测试(api.md 第 3 节):鉴权/快照/踢旧/控制广播/close code。"""

import time

import contextlib

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from server.app.ws import ConnectionRegistry, heartbeat_pass
from server.tests.conftest import auth_headers


def _connect_auth(stack, client: TestClient, room_id: str, token: str):
    """建立 WS 并完成鉴权;返回 (会话, 快照)。

    自己的 member_joined 广播(注册后全体广播)在此一并消费。
    """
    ws = stack.enter_context(client.websocket_connect(f"/ws/rooms/{room_id}"))
    ws.send_json({"type": "auth", "token": token})
    snap = ws.receive_json()
    assert snap["type"] == "auth_ok"
    join = ws.receive_json()
    assert join["type"] == "member_joined"
    return ws, snap


def test_auth_ok_snapshot_structure(p3_env) -> None:
    """快照含 you/room(members/playback/video)/server_ts(api.md 3.2,需求 3.7)。"""
    with TestClient(p3_env.app) as client, contextlib.ExitStack() as stack:
        host = client.post("/api/rooms", json={"nickname": "Alice"}).json()
        ws, snap = _connect_auth(stack, client, host["room_id"], host["session_token"])

        assert snap["you"] == {"user_id": host["user_id"], "is_host": True}
        inner = snap["room"]
        assert inner["room_id"] == host["room_id"]
        assert inner["host_user_id"] == host["user_id"]
        assert inner["members"][0]["nickname"] == "Alice"
        assert inner["members"][0]["is_host"] is True
        assert set(inner["playback"]) == {"state", "position", "rate", "updated_at", "version"}
        assert inner["video"] is None
        assert isinstance(snap["server_ts"], float)


def test_auth_bad_token_close_4001(p3_env) -> None:
    with TestClient(p3_env.app) as client, contextlib.ExitStack() as stack:
        host = client.post("/api/rooms", json={"nickname": "Alice"}).json()
        ws = stack.enter_context(client.websocket_connect(f"/ws/rooms/{host['room_id']}"))
        ws.send_json({"type": "auth", "token": "not-a-token"})
        assert ws.receive_json()["code"] == "UNAUTHORIZED"  # 先回 error 消息(api.md 3.1)
        with pytest.raises(WebSocketDisconnect) as ei:
            ws.receive_json()
        assert ei.value.code == 4001


def test_auth_room_not_found_close_4003(p3_env) -> None:
    with TestClient(p3_env.app) as client, contextlib.ExitStack() as stack:
        ws = stack.enter_context(client.websocket_connect("/ws/rooms/NOPE0000"))
        ws.send_json({"type": "auth", "token": "whatever"})
        assert ws.receive_json()["code"] == "ROOM_NOT_FOUND"
        with pytest.raises(WebSocketDisconnect) as ei:
            ws.receive_json()
        assert ei.value.code == 4003


def test_auth_room_full_close_4002(p3_env) -> None:
    """在线满员后新 WS 被拒(在线口径,验收 16)。"""
    with TestClient(p3_env.app) as client, contextlib.ExitStack() as stack:
        host = client.post("/api/rooms", json={"nickname": "host"}).json()
        _connect_auth(stack, client, host["room_id"], host["session_token"])
        for i in range(9):  # 再进入 9 人,达在线上限 10
            cred = client.post(
                f"/api/rooms/{host['room_id']}/join", json={"nickname": f"m{i}"}
            ).json()
            _connect_auth(stack, client, host["room_id"], cred["session_token"])

        eleventh = client.post(
            f"/api/rooms/{host['room_id']}/join", json={"nickname": "over"}
        ).json()
        ws = stack.enter_context(client.websocket_connect(f"/ws/rooms/{host['room_id']}"))
        ws.send_json({"type": "auth", "token": eleventh["session_token"]})
        assert ws.receive_json()["code"] == "ROOM_FULL"
        with pytest.raises(WebSocketDisconnect) as ei:
            ws.receive_json()
        assert ei.value.code == 4002


def test_duplicate_connection_kicks_old_4004(p3_env) -> None:
    """同 token 第二条连接:旧连接收 kicked 后被关闭(决策 T15,验收 8)。"""
    with TestClient(p3_env.app) as client, contextlib.ExitStack() as stack:
        host = client.post("/api/rooms", json={"nickname": "Alice"}).json()
        token = host["session_token"]

        ws1 = stack.enter_context(client.websocket_connect(f"/ws/rooms/{host['room_id']}"))
        ws1.send_json({"type": "auth", "token": token})
        assert ws1.receive_json()["type"] == "auth_ok"
        assert ws1.receive_json()["type"] == "member_joined"

        ws2 = stack.enter_context(client.websocket_connect(f"/ws/rooms/{host['room_id']}"))
        ws2.send_json({"type": "auth", "token": token})
        assert ws2.receive_json()["type"] == "auth_ok"
        assert ws2.receive_json()["type"] == "member_joined"

        assert ws1.receive_json()["reason"] == "duplicate_connection"  # kicked
        with pytest.raises(WebSocketDisconnect) as ei:
            ws1.receive_json()
        assert ei.value.code == 4004


def test_play_control_broadcast_to_all(p3_env) -> None:
    """房主 play → 全员(含房主)收 state_update,version 递增(api.md 3.3/3.4)。"""
    with TestClient(p3_env.app) as client, contextlib.ExitStack() as stack:
        host = client.post("/api/rooms", json={"nickname": "Alice"}).json()
        member = client.post(
            f"/api/rooms/{host['room_id']}/join", json={"nickname": "Bob"}
        ).json()

        ws_host, _ = _connect_auth(stack, client, host["room_id"], host["session_token"])
        ws_member, _ = _connect_auth(stack, client, host["room_id"], member["session_token"])
        assert ws_host.receive_json()["user"]["nickname"] == "Bob"  # Bob 加入广播

        ws_host.send_json({"type": "play"})
        for ws in (ws_host, ws_member):
            su = ws.receive_json()
            assert su["type"] == "state_update"
            assert su["state"] == "playing"
            assert su["version"] == 1
            assert isinstance(su["server_ts"], float)


def test_pause_seek_rate_sequence(p3_env) -> None:
    """seek/rate/pause 依次结算并广播(sync-protocol §2)。"""
    with TestClient(p3_env.app) as client, contextlib.ExitStack() as stack:
        host = client.post("/api/rooms", json={"nickname": "Alice"}).json()
        ws, _ = _connect_auth(stack, client, host["room_id"], host["session_token"])

        ws.send_json({"type": "play"})
        assert ws.receive_json()["version"] == 1
        ws.send_json({"type": "seek", "position": 300})
        su = ws.receive_json()
        assert su["state"] == "playing" and su["position"] == 300 and su["version"] == 2
        ws.send_json({"type": "rate", "rate": 1.5})
        su = ws.receive_json()
        assert su["rate"] == 1.5 and su["version"] == 3
        ws.send_json({"type": "pause"})
        su = ws.receive_json()
        assert su["state"] == "paused" and su["version"] == 4


def test_member_control_forbidden_stays_connected(p3_env) -> None:
    """非房主发控制消息:回 error FORBIDDEN 且连接保持(验收 9)。"""
    with TestClient(p3_env.app) as client, contextlib.ExitStack() as stack:
        host = client.post("/api/rooms", json={"nickname": "Alice"}).json()
        member = client.post(
            f"/api/rooms/{host['room_id']}/join", json={"nickname": "Bob"}
        ).json()
        ws_member, _ = _connect_auth(stack, client, host["room_id"], member["session_token"])

        ws_member.send_json({"type": "play"})
        err = ws_member.receive_json()
        assert err["type"] == "error" and err["code"] == "FORBIDDEN"

        # 连接未被关闭:ping 仍可得 pong
        ws_member.send_json({"type": "ping", "t0": 1.0})
        pong = ws_member.receive_json()
        assert pong["type"] == "pong" and pong["t0"] == 1.0


def test_seek_and_rate_out_of_range(p3_env) -> None:
    """seek 负值/倍速超范围回 error 不结算(需求 3.4.2,api.md 3.3)。"""
    with TestClient(p3_env.app) as client, contextlib.ExitStack() as stack:
        host = client.post("/api/rooms", json={"nickname": "Alice"}).json()
        ws, _ = _connect_auth(stack, client, host["room_id"], host["session_token"])

        ws.send_json({"type": "seek", "position": -5})
        assert ws.receive_json()["code"] == "SEEK_OUT_OF_RANGE"
        ws.send_json({"type": "rate", "rate": 10})
        assert ws.receive_json()["code"] == "RATE_OUT_OF_RANGE"

        # 房间状态未被污染(version 仍为 0)
        assert p3_env.manager.get_room(host["room_id"]).playback.version == 0


def test_bad_message_error(p3_env) -> None:
    """非法 JSON/未知类型回 error 且连接保持。"""
    with TestClient(p3_env.app) as client, contextlib.ExitStack() as stack:
        host = client.post("/api/rooms", json={"nickname": "Alice"}).json()
        ws, _ = _connect_auth(stack, client, host["room_id"], host["session_token"])

        ws.send_text("not-json{{{")
        assert ws.receive_json()["code"] == "BAD_MESSAGE"
        ws.send_json({"type": "dance"})
        assert ws.receive_json()["code"] == "BAD_MESSAGE"

        ws.send_json({"type": "ping", "t0": 2.0})  # 连接仍可用
        assert ws.receive_json()["type"] == "pong"


def test_dissolve_closes_with_4005(p3_env) -> None:
    """解散:全员收 room_dissolved 后连接以 4005 关闭(api.md 2.4/3.5)。"""
    with TestClient(p3_env.app) as client, contextlib.ExitStack() as stack:
        host = client.post("/api/rooms", json={"nickname": "Alice"}).json()
        member = client.post(
            f"/api/rooms/{host['room_id']}/join", json={"nickname": "Bob"}
        ).json()
        ws_host, _ = _connect_auth(stack, client, host["room_id"], host["session_token"])
        ws_member, _ = _connect_auth(stack, client, host["room_id"], member["session_token"])
        assert ws_host.receive_json()["user"]["nickname"] == "Bob"

        resp = client.post(
            f"/api/rooms/{host['room_id']}/dissolve",
            headers=auth_headers(host["session_token"]),
        )
        assert resp.status_code == 200

        for ws in (ws_host, ws_member):
            assert ws.receive_json()["type"] == "room_dissolved"
            with pytest.raises(WebSocketDisconnect) as ei:
                ws.receive_json()
            assert ei.value.code == 4005


# ---------- heartbeat_pass(单轮扫描,async 直驱) ----------

class StubConn:
    """WS 连接替身:记录发送与关闭调用。"""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.close_called = False

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)

    async def close(self, code: int | None = None) -> None:
        self.close_called = True


async def test_heartbeat_pass_closes_and_broadcasts(p3_env) -> None:
    """超时成员:连接关闭、注册移除、member_left(timeout) 广播;房主不移交。"""
    ctx = p3_env
    m, registry = ctx.manager, ConnectionRegistry()
    now = time.time()

    host_cred = await m.create_room("Alice", now=now)
    host_stub = StubConn()
    await m.authenticate(host_cred.room_id, host_cred.session_token, host_stub, now=now)
    member_cred = await m.join_room(host_cred.room_id, "Bob", now=now + 1)
    member_stub = StubConn()
    await m.authenticate(host_cred.room_id, member_cred.session_token, member_stub, now=now + 1)
    registry.register(host_cred.room_id, host_cred.user_id, host_stub)
    registry.register(host_cred.room_id, member_cred.user_id, member_stub)

    timeout = ctx.settings.heartbeat.server_timeout_seconds
    room = m.get_room(host_cred.room_id)
    room.members[member_cred.user_id].last_seen = now + 1 - (timeout + 5)  # Bob 超时

    await heartbeat_pass(m, registry)

    assert member_stub.close_called is True                       # 死连接被关闭
    assert registry.get(host_cred.room_id, member_cred.user_id) is None  # 注册移除
    assert room.members.get(member_cred.user_id) is None          # 成员移除
    assert room.host_user_id == host_cred.user_id                 # 房主掉线不移交(验收 14)
    assert room.empty_since is None                               # 房主仍在线
    assert host_stub.sent == [
        {"type": "member_left", "user_id": member_cred.user_id, "reason": "timeout"}
    ]
