"""HTTP API 端点测试(api.md 第 2 节)。

TestClient 同步用例;含 WS 连接的用例用于验证 leave/dissolve 的连接关闭行为。
"""

import contextlib

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from server.app.room_manager import UploadInProgressError
from server.tests.conftest import auth_headers, upload_headers


def _create_room(client: TestClient, nickname: str = "Alice") -> dict:
    return client.post("/api/rooms", json={"nickname": nickname}).json()


def test_healthz(p3_env) -> None:
    with TestClient(p3_env.app) as client, contextlib.ExitStack() as stack:
        assert client.get("/healthz").json() == {"status": "ok"}


# ---------- 创建 / 加入(api.md 2.1/2.2) ----------

def test_create_room_success(p3_env) -> None:
    with TestClient(p3_env.app) as client, contextlib.ExitStack() as stack:
        body = _create_room(client)
        assert len(body["room_id"]) == 8
        assert body["is_host"] is True
        assert body["invite_url"].endswith(f"/rooms/{body['room_id']}")
        assert len(body["session_token"]) == 43
        # 写 DB 行(需求 D3)
        assert p3_env.db.conn.execute("SELECT COUNT(*) FROM rooms").fetchone()[0] == 1


def test_create_room_invalid_nickname_400(p3_env) -> None:
    with TestClient(p3_env.app) as client, contextlib.ExitStack() as stack:
        resp = client.post("/api/rooms", json={"nickname": "   "})
        assert resp.status_code == 400
        assert resp.json()["code"] == "INVALID_NICKNAME"


def test_create_room_limit_429(p3_env) -> None:
    """第 6 个房间被拒(默认上限 5,验收 16)。"""
    with TestClient(p3_env.app) as client, contextlib.ExitStack() as stack:
        for i in range(5):
            client.post("/api/rooms", json={"nickname": f"u{i}"})
        resp = client.post("/api/rooms", json={"nickname": "sixth"})
        assert resp.status_code == 429
        assert resp.json()["code"] == "ROOM_LIMIT_REACHED"


def test_join_room_not_found_404(p3_env) -> None:
    with TestClient(p3_env.app) as client, contextlib.ExitStack() as stack:
        resp = client.post("/api/rooms/NOPE0000/join", json={"nickname": "Bob"})
        assert resp.status_code == 404
        assert resp.json()["code"] == "ROOM_NOT_FOUND"


def test_join_room_success_and_error_response_shape(p3_env) -> None:
    with TestClient(p3_env.app) as client, contextlib.ExitStack() as stack:
        room = _create_room(client)
        resp = client.post(f"/api/rooms/{room['room_id']}/join", json={"nickname": "Bob"})
        body = resp.json()
        assert resp.status_code == 200
        assert body["is_host"] is False
        assert body["session_token"]


# ---------- 退出(api.md 2.3) ----------

def test_leave_requires_token_401(p3_env) -> None:
    with TestClient(p3_env.app) as client, contextlib.ExitStack() as stack:
        room = _create_room(client)
        resp = client.post(f"/api/rooms/{room['room_id']}/leave")
        assert resp.status_code == 401
        assert resp.json()["code"] == "UNAUTHORIZED"


def test_leave_room_not_found_404(p3_env) -> None:
    with TestClient(p3_env.app) as client, contextlib.ExitStack() as stack:
        resp = client.post("/api/rooms/NOPE0000/leave", headers=auth_headers("x"))
        assert resp.status_code == 404


def test_leave_closes_ws_and_broadcasts(p3_env) -> None:
    """退出者连接被关闭;其余成员收到 member_left{leave}(api.md 2.3)。"""
    with TestClient(p3_env.app) as client, contextlib.ExitStack() as stack:
        host = _create_room(client)
        member = client.post(
            f"/api/rooms/{host['room_id']}/join", json={"nickname": "Bob"}
        ).json()

        ws_host = stack.enter_context(client.websocket_connect(f"/ws/rooms/{host['room_id']}"))
        ws_host.send_json({"type": "auth", "token": host["session_token"]})
        assert ws_host.receive_json()["type"] == "auth_ok"
        assert ws_host.receive_json()["type"] == "member_joined"  # 自己的加入广播

        ws_member = stack.enter_context(client.websocket_connect(f"/ws/rooms/{host['room_id']}"))
        ws_member.send_json({"type": "auth", "token": member["session_token"]})
        assert ws_member.receive_json()["type"] == "auth_ok"
        assert ws_member.receive_json()["type"] == "member_joined"  # 自己的加入广播
        assert ws_host.receive_json()["user"]["nickname"] == "Bob"  # Bob 加入广播(在成员 WS auth 后)

        resp = client.post(
            f"/api/rooms/{host['room_id']}/leave", headers=auth_headers(member["session_token"])
        )
        assert resp.status_code == 200

        with pytest.raises(WebSocketDisconnect):
            ws_member.receive_json()  # 退出者连接被关闭
        left = ws_host.receive_json()
        assert left["type"] == "member_left" and left["reason"] == "leave"


# ---------- 解散(api.md 2.4,验收 9/15) ----------

def test_dissolve_forbidden_for_member(p3_env) -> None:
    with TestClient(p3_env.app) as client, contextlib.ExitStack() as stack:
        host = _create_room(client)
        member = client.post(
            f"/api/rooms/{host['room_id']}/join", json={"nickname": "Bob"}
        ).json()
        resp = client.post(
            f"/api/rooms/{host['room_id']}/dissolve",
            headers=auth_headers(member["session_token"]),
        )
        assert resp.status_code == 403
        assert resp.json()["code"] == "FORBIDDEN"


def test_dissolve_broadcast_and_cleanup(p3_env) -> None:
    """解散:全员收 room_dissolved 并被移出(4005),文件/DB 立即清理(验收 15)。"""
    ctx = p3_env
    with TestClient(ctx.app) as client, contextlib.ExitStack() as stack:
        host = _create_room(client)
        # 上传一个视频供清理验证
        resp = client.put(
            f"/api/rooms/{host['room_id']}/video",
            content=b"video",
            headers=upload_headers(host["session_token"], "a.mp4"),
        )
        assert resp.status_code == 200
        video_id = resp.json()["video_id"]

        ws = stack.enter_context(client.websocket_connect(f"/ws/rooms/{host['room_id']}"))
        ws.send_json({"type": "auth", "token": host["session_token"]})
        snap = ws.receive_json()
        assert snap["type"] == "auth_ok"
        # 上传先于上线:video_ready 广播时无在线成员,上线者从快照得知视频
        assert snap["room"]["video"]["video_id"] == video_id
        assert ws.receive_json()["type"] == "member_joined"  # 自己的加入广播

        resp = client.post(
            f"/api/rooms/{host['room_id']}/dissolve",
            headers=auth_headers(host["session_token"]),
        )
        assert resp.status_code == 200

        assert ws.receive_json()["type"] == "room_dissolved"
        with pytest.raises(WebSocketDisconnect) as ei:
            ws.receive_json()
        assert ei.value.code == 4005

        assert ctx.storage.stat_size(video_id) is None
        assert ctx.db.conn.execute("SELECT COUNT(*) FROM rooms").fetchone()[0] == 0


# ---------- 上传(api.md 2.5,验收 11/12) ----------

def test_upload_success_and_replace(p3_env) -> None:
    ctx = p3_env
    with TestClient(ctx.app) as client, contextlib.ExitStack() as stack:
        host = _create_room(client)
        rid = host["room_id"]

        r1 = client.put(f"/api/rooms/{rid}/video", content=b"AAA", headers=upload_headers(host["session_token"], "a.mp4"))
        assert r1.status_code == 200
        v1 = r1.json()
        assert v1["original_name"] == "a.mp4" and v1["size"] == 3
        assert ctx.storage.stat_size(v1["video_id"]) == 3

        r2 = client.put(f"/api/rooms/{rid}/video", content=b"BBBBB", headers=upload_headers(host["session_token"], "b.mp4"))
        assert r2.status_code == 200
        v2 = r2.json()
        assert v2["video_id"] != v1["video_id"]
        assert ctx.storage.stat_size(v1["video_id"]) is None  # 旧文件已删(需求 D4)
        assert ctx.storage.stat_size(v2["video_id"]) == 5
        # 权威状态重置(验收 12)
        room = ctx.manager.get_room(rid)
        assert (room.playback.state, room.playback.position, room.playback.version) == ("paused", 0.0, 2)


def test_upload_invalid_extension_400(p3_env) -> None:
    with TestClient(p3_env.app) as client, contextlib.ExitStack() as stack:
        host = _create_room(client)
        resp = client.put(
            f"/api/rooms/{host['room_id']}/video",
            content=b"x",
            headers=upload_headers(host["session_token"], "movie.avi"),
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == "INVALID_FORMAT"


def test_upload_too_large_413(p3_env) -> None:
    """Content-Length 预检超限即拒(验收 11)。"""
    with TestClient(p3_env.app) as client, contextlib.ExitStack() as stack:
        host = _create_room(client)
        resp = client.put(
            f"/api/rooms/{host['room_id']}/video",
            content=b"x" * (1024 * 1024 + 1),
            headers=upload_headers(host["session_token"], "big.mp4"),
        )
        assert resp.status_code == 413
        assert resp.json()["code"] == "FILE_TOO_LARGE"


def test_upload_non_host_403(p3_env) -> None:
    """普通成员直接调房主接口被拒(验收 9)。"""
    with TestClient(p3_env.app) as client, contextlib.ExitStack() as stack:
        host = _create_room(client)
        member = client.post(
            f"/api/rooms/{host['room_id']}/join", json={"nickname": "Bob"}
        ).json()
        resp = client.put(
            f"/api/rooms/{host['room_id']}/video",
            content=b"x",
            headers=upload_headers(member["session_token"], "a.mp4"),
        )
        assert resp.status_code == 403
        assert resp.json()["code"] == "FORBIDDEN"


def test_upload_in_progress_409(p3_env) -> None:
    """已有进行中上传时再次上传被拒(409)。"""
    ctx = p3_env
    with TestClient(ctx.app) as client, contextlib.ExitStack() as stack:
        host = _create_room(client)
        # 直接置上传互斥锁(模拟进行中的上传)
        ctx.manager.begin_upload(host["room_id"], host["session_token"])
        resp = client.put(
            f"/api/rooms/{host['room_id']}/video",
            content=b"x",
            headers=upload_headers(host["session_token"], "a.mp4"),
        )
        assert resp.status_code == 409
        assert resp.json()["code"] == "UPLOAD_IN_PROGRESS"
        ctx.manager.fail_upload(host["room_id"])


def test_upload_video_ready_broadcast(p3_env) -> None:
    """上传完成广播 video_ready,在线成员收到(api.md 2.5)。"""
    with TestClient(p3_env.app) as client, contextlib.ExitStack() as stack:
        host = _create_room(client)
        member = client.post(
            f"/api/rooms/{host['room_id']}/join", json={"nickname": "Bob"}
        ).json()
        ws_member = stack.enter_context(client.websocket_connect(f"/ws/rooms/{host['room_id']}"))
        ws_member.send_json({"type": "auth", "token": member["session_token"]})
        assert ws_member.receive_json()["type"] == "auth_ok"
        assert ws_member.receive_json()["type"] == "member_joined"

        resp = client.put(
            f"/api/rooms/{host['room_id']}/video",
            content=b"movie",
            headers=upload_headers(host["session_token"], "m.mp4"),
        )
        assert resp.status_code == 200
        evt = ws_member.receive_json()
        assert evt["type"] == "video_ready"
        assert evt["video"]["original_name"] == "m.mp4"


# ---------- 视频下发(api.md 2.6,验收 2) ----------

def _upload_and_get_id(client: TestClient, token: str, room_id: str, content: bytes) -> str:
    resp = client.put(
        f"/api/rooms/{room_id}/video", content=content, headers=upload_headers(token, "v.mp4")
    )
    assert resp.status_code == 200
    return resp.json()["video_id"]


def test_media_full_and_range_206(p3_env) -> None:
    with TestClient(p3_env.app) as client, contextlib.ExitStack() as stack:
        host = _create_room(client)
        vid = _upload_and_get_id(client, host["session_token"], host["room_id"], b"0123456789")

        # 全量 200
        full = client.get(f"/media/{vid}.mp4")
        assert full.status_code == 200
        assert full.content == b"0123456789"
        assert full.headers["accept-ranges"] == "bytes"
        assert full.headers["content-length"] == "10"

        # 闭区间 Range → 206 + Content-Range(验收 2)
        part = client.get(f"/media/{vid}.mp4", headers={"Range": "bytes=2-5"})
        assert part.status_code == 206
        assert part.content == b"2345"
        assert part.headers["content-range"] == "bytes 2-5/10"

        # 开区间与后缀式
        assert client.get(f"/media/{vid}.mp4", headers={"Range": "bytes=8-"}).content == b"89"
        assert client.get(f"/media/{vid}.mp4", headers={"Range": "bytes=-3"}).content == b"789"

        # end 超界钳制
        over = client.get(f"/media/{vid}.mp4", headers={"Range": "bytes=8-100"})
        assert over.status_code == 206 and over.content == b"89"


def test_media_head_404_416(p3_env) -> None:
    with TestClient(p3_env.app) as client, contextlib.ExitStack() as stack:
        host = _create_room(client)
        vid = _upload_and_get_id(client, host["session_token"], host["room_id"], b"0123456789")

        head = client.head(f"/media/{vid}.mp4")
        assert head.status_code == 200
        assert head.headers["content-length"] == "10"
        assert head.content == b""

        assert client.get("/media/deadbeef.mp4").status_code == 404
        assert client.get(f"/media/{vid}.mp4", headers={"Range": "bytes=10-"}).status_code == 416
        assert client.get(f"/media/{vid}.mp4", headers={"Range": "bytes=5-2"}).status_code == 416
