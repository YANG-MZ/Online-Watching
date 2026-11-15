"""P4 集成测试:真实装配(build_app + lifespan)下的跨层场景。

覆盖:
  - 完整观影旅程:创建→上线→上传→播放同步→拉流→退出→解散→清理;
  - 重启即清理(需求 4.5:重建应用即模拟重启,DB 行与文件全清、房间失效);
  - 容量上限生效与修改后生效(验收 16/17)+ 环境变量覆盖;
  - 空房过期端到端(创建未上线→倒计时→清理);
  - 心跳超时端到端(真实定时器:判死→移除→空房→清理→404)。

定时器类用例使用短周期配置(过期 1s / 扫描 0.2s),数秒内完成。
"""

import contextlib
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from server.app.config import (
    CleanupSettings,
    HeartbeatSettings,
    LimitsSettings,
    RoomSettings,
    Settings,
    StorageSettings,
    load_settings,
)
from server.app.main import build_app
from server.tests.conftest import auth_headers, connect_and_auth, upload_headers


@pytest.fixture
def isettings(tmp_path) -> Settings:
    """短周期配置:定时器类集成用例在数秒内完成。"""
    return Settings(
        storage=StorageSettings(data_dir=str(tmp_path / "data")),
        limits=LimitsSettings(max_rooms=2, max_members_per_room=3),
        room=RoomSettings(empty_expire_seconds=1.0),
        heartbeat=HeartbeatSettings(
            client_ping_seconds=0.5, server_timeout_seconds=1.0, scan_interval_seconds=0.2
        ),
        cleanup=CleanupSettings(scan_interval_seconds=0.2),
    )


def _video_file(tmp_path: Path, video_id: str) -> Path:
    return tmp_path / "data" / "videos" / f"{video_id}.mp4"


# ---------- 完整观影旅程(验收 1~5/8/15 串联) ----------

def test_full_journey(isettings, tmp_path) -> None:
    app = build_app(isettings)
    with TestClient(app) as client, contextlib.ExitStack() as stack:
        # 1. 创建房间 + 成员 HTTP 加入
        host = client.post("/api/rooms", json={"nickname": "Alice"}).json()
        bob = client.post(
            f"/api/rooms/{host['room_id']}/join", json={"nickname": "Bob"}
        ).json()
        assert host["is_host"] is True and bob["is_host"] is False

        # 2. 房主上线 → 快照(无视频)
        ws_host, snap = connect_and_auth(stack, client, host["room_id"], host["session_token"])
        assert snap["room"]["video"] is None

        # 3. 房主上传 → video_ready 广播
        resp = client.put(
            f"/api/rooms/{host['room_id']}/video",
            content=b"MOVIE",
            headers=upload_headers(host["session_token"], "film.mp4"),
        )
        assert resp.status_code == 200
        video_id = resp.json()["video_id"]
        assert ws_host.receive_json()["type"] == "video_ready"
        assert _video_file(tmp_path, video_id).is_file()

        # 4. 成员上线 → 快照携带视频;房主收到 Bob 加入
        ws_bob, snap_bob = connect_and_auth(stack, client, host["room_id"], bob["session_token"])
        assert snap_bob["room"]["video"]["video_id"] == video_id
        assert ws_host.receive_json()["user"]["nickname"] == "Bob"

        # 5. 房主播放 → 全员 state_update(version 递增)
        ws_host.send_json({"type": "play"})
        for ws in (ws_host, ws_bob):
            su = ws.receive_json()
            assert su["type"] == "state_update" and su["state"] == "playing"

        # 6. 成员经 HTTP Range 拉流(验收 2)
        part = client.get(f"/media/{video_id}.mp4", headers={"Range": "bytes=0-2"})
        assert part.status_code == 206 and part.content == b"MOV"

        # 7. 成员退出 → 连接关闭 + member_left
        resp = client.post(
            f"/api/rooms/{host['room_id']}/leave", headers=auth_headers(bob["session_token"])
        )
        assert resp.status_code == 200
        with pytest.raises(WebSocketDisconnect):
            ws_bob.receive_json()
        left = ws_host.receive_json()
        assert left["type"] == "member_left" and left["reason"] == "leave"

        # 8. 房主解散 → room_dissolved + 4005 + 文件清理(验收 15)
        resp = client.post(
            f"/api/rooms/{host['room_id']}/dissolve",
            headers=auth_headers(host["session_token"]),
        )
        assert resp.status_code == 200
        assert ws_host.receive_json()["type"] == "room_dissolved"
        with pytest.raises(WebSocketDisconnect) as ei:
            ws_host.receive_json()
        assert ei.value.code == 4005

        assert not _video_file(tmp_path, video_id).exists()
        assert client.get(f"/media/{video_id}.mp4").status_code == 404
        join_again = client.post(
            f"/api/rooms/{host['room_id']}/join", json={"nickname": "Late"}
        )
        assert join_again.status_code == 404


# ---------- 重启即清理(需求 4.5) ----------

def test_restart_wipes_everything(isettings, tmp_path) -> None:
    app1 = build_app(isettings)
    with TestClient(app1) as client:
        room = client.post("/api/rooms", json={"nickname": "Alice"}).json()
        resp = client.put(
            f"/api/rooms/{room['room_id']}/video",
            content=b"DATA",
            headers=upload_headers(room["session_token"], "a.mp4"),
        )
        video_id = resp.json()["video_id"]
        assert _video_file(tmp_path, video_id).is_file()

    # "重启":同一配置重建应用,lifespan 启动清理删除全部残留
    app2 = build_app(isettings)
    with TestClient(app2) as client2:
        assert not _video_file(tmp_path, video_id).exists()  # 文件已清
        resp = client2.post(
            f"/api/rooms/{room['room_id']}/join", json={"nickname": "Bob"}
        )
        assert resp.status_code == 404  # 房间失效(验收 8 场景的服务端前提)
        assert resp.json()["code"] == "ROOM_NOT_FOUND"
        # 干净状态:可重新创建(旧房间不占名额)
        assert client2.post("/api/rooms", json={"nickname": "Fresh"}).status_code == 200


# ---------- 容量上限生效与修改后生效(验收 16/17) ----------

def test_room_limit_enforced_then_updated(isettings, tmp_path) -> None:
    # 上限 2:第 3 个房间被拒(验收 16)
    app1 = build_app(isettings)
    with TestClient(app1) as client:
        assert client.post("/api/rooms", json={"nickname": "A"}).status_code == 200
        assert client.post("/api/rooms", json={"nickname": "B"}).status_code == 200
        resp = client.post("/api/rooms", json={"nickname": "C"})
        assert resp.status_code == 429
        assert resp.json()["code"] == "ROOM_LIMIT_REACHED"

    # "修改配置 + 重启":上限调到 3,新上限生效(验收 17)
    new_settings = isettings.model_copy(
        update={"limits": LimitsSettings(max_rooms=3, max_members_per_room=3)}
    )
    app2 = build_app(new_settings)
    with TestClient(app2) as client2:
        for nickname in ("A", "B", "C"):
            assert client2.post("/api/rooms", json={"nickname": nickname}).status_code == 200
        assert client2.post("/api/rooms", json={"nickname": "D"}).status_code == 429


def test_member_limit_over_http_and_ws(isettings) -> None:
    """每房在线人数上限 3:第 4 人 HTTP 可获凭证,WS auth 被拒(4002)。"""
    app = build_app(isettings)
    with TestClient(app) as client, contextlib.ExitStack() as stack:
        host = client.post("/api/rooms", json={"nickname": "h"}).json()
        connect_and_auth(stack, client, host["room_id"], host["session_token"])
        tokens = []
        for i in range(2):  # 再进 2 人,达上限 3
            cred = client.post(
                f"/api/rooms/{host['room_id']}/join", json={"nickname": f"m{i}"}
            ).json()
            connect_and_auth(stack, client, host["room_id"], cred["session_token"])
            tokens.append(cred["session_token"])

        fourth = client.post(
            f"/api/rooms/{host['room_id']}/join", json={"nickname": "over"}
        ).json()  # HTTP 侧不拒(在线口径在 WS 判定)
        ws = stack.enter_context(client.websocket_connect(f"/ws/rooms/{host['room_id']}"))
        ws.send_json({"type": "auth", "token": fourth["session_token"]})
        assert ws.receive_json()["code"] == "ROOM_FULL"
        with pytest.raises(WebSocketDisconnect) as ei:
            ws.receive_json()
        assert ei.value.code == 4002


def test_env_var_override(monkeypatch, tmp_path) -> None:
    """环境变量覆盖配置(MOVIE_TOGETHER__节__键),配合文件缺失兜底。"""
    monkeypatch.setenv("MOVIE_TOGETHER_CONFIG", str(tmp_path / "not-exist.toml"))
    monkeypatch.setenv("MOVIE_TOGETHER__LIMITS__MAX_ROOMS", "1")
    settings = load_settings()
    assert settings.limits.max_rooms == 1  # 环境变量覆盖默认值 5


# ---------- 空房过期端到端(需求 3.1.7,验收 10) ----------

def test_never_joined_room_expires(isettings, tmp_path) -> None:
    """创建后从未上线:倒计时到期即清理(文件+DB),邀请链接失效。"""
    app = build_app(isettings)
    with TestClient(app) as client:
        room = client.post("/api/rooms", json={"nickname": "Ghost"}).json()
        resp = client.put(
            f"/api/rooms/{room['room_id']}/video",
            content=b"LEFT",
            headers=upload_headers(room["session_token"], "g.mp4"),
        )
        video_id = resp.json()["video_id"]
        assert _video_file(tmp_path, video_id).is_file()

        # 过期 1.0s + 清理扫描 0.2s(留裕量)
        time.sleep(1.8)
        assert not _video_file(tmp_path, video_id).exists()
        resp = client.post(f"/api/rooms/{room['room_id']}/join", json={"nickname": "Late"})
        assert resp.status_code == 404


def test_ws_auth_revives_empty_room(isettings, tmp_path) -> None:
    """倒计时窗口内 WS auth 复活(决策 T14);全员离开后再过期。"""
    app = build_app(isettings)
    with TestClient(app) as client, contextlib.ExitStack() as stack:
        host = client.post("/api/rooms", json={"nickname": "Alice"}).json()

        # 在倒计时内上线 → 复活,房间存活远超过期时长
        time.sleep(0.5)
        connect_and_auth(stack, client, host["room_id"], host["session_token"])
        assert client.post(f"/api/rooms/{host['room_id']}/join", json={"nickname": "Bob"}).status_code == 200

        # 房主主动退出 → 空房 → 过期清理
        client.post(f"/api/rooms/{host['room_id']}/leave", headers=auth_headers(host["session_token"]))
        time.sleep(1.8)
        assert client.post(f"/api/rooms/{host['room_id']}/join", json={"nickname": "Late"}).status_code == 404


# ---------- 心跳超时端到端(需求 3.6.2,真实定时器) ----------

def test_heartbeat_timeout_end_to_end(isettings, tmp_path) -> None:
    """无消息成员:判死→连接关闭→移除→空房→清理→404 全链路。"""
    app = build_app(isettings)
    with TestClient(app) as client, contextlib.ExitStack() as stack:
        host = client.post("/api/rooms", json={"nickname": "Alice"}).json()
        ws, _ = connect_and_auth(stack, client, host["room_id"], host["session_token"])

        # 服务端超时 1.0s + 扫描 0.2s → 连接被关闭(receive 阻塞直至收到)
        start = time.time()
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()
        elapsed = time.time() - start
        assert elapsed < 3.0  # 在超时+扫描周期内被关闭

        # 移除后空房 → 过期 1.0s + 清理 0.2s → 房间销毁
        time.sleep(1.6)
        assert client.post(f"/api/rooms/{host['room_id']}/join", json={"nickname": "Late"}).status_code == 404


# ---------- CORS(分离部署,决策 T16) ----------

def test_cors_preflight(isettings) -> None:
    app = build_app(isettings)
    with TestClient(app) as client:
        resp = client.options(
            "/api/rooms",
            headers={
                "Origin": "http://192.168.1.10:8001",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert resp.headers.get("access-control-allow-origin") == "*"
