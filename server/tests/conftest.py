"""pytest 共享装置:组装真实 P1/P2/P3 组件的测试用 FastAPI 应用。

说明:此处应用不带 lifespan(周期任务不启动,保持用例确定性);
lifespan 装配(启动清理/后台任务)由 P4 集成测试覆盖。
"""

from types import SimpleNamespace
from pathlib import Path

import pytest
from fastapi import FastAPI

from server.app.api import create_api_router, create_media_router, register_error_handlers
from server.app.config import LimitsSettings, Settings, StorageSettings
from server.app.db import SqliteDatabase, SqliteRoomRepository, SqliteVideoRepository
from server.app.playback import PlaybackService
from server.app.room_manager import RoomManager
from server.app.video_storage import LocalVideoStorage
from server.app.ws import ConnectionRegistry, create_ws_router


@pytest.fixture
def p3_env(tmp_path):
    """组装完整依赖图并返回组件句柄(真实实现 + 临时目录)。"""
    settings = Settings(
        storage=StorageSettings(data_dir=str(tmp_path / "data")),
        limits=LimitsSettings(max_upload_bytes=1024 * 1024),  # 1 MiB,便于 413 用例
    )
    db = SqliteDatabase(tmp_path / "data" / "app.db")
    room_repo = SqliteRoomRepository(db)
    video_repo = SqliteVideoRepository(db)
    storage = LocalVideoStorage(tmp_path / "data", settings.limits.max_upload_bytes)
    manager = RoomManager(settings, room_repo, video_repo, storage, PlaybackService())
    registry = ConnectionRegistry()

    app = FastAPI()
    app.include_router(create_api_router(settings, manager, storage, registry))
    app.include_router(create_media_router(settings, storage))
    ws_router, heartbeat_scan = create_ws_router(settings, manager, PlaybackService(), registry)
    app.include_router(ws_router)

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok"}

    register_error_handlers(app)

    ctx = SimpleNamespace(
        settings=settings,
        app=app,
        db=db,
        room_repo=room_repo,
        video_repo=video_repo,
        storage=storage,
        manager=manager,
        registry=registry,
        heartbeat_scan=heartbeat_scan,
        tmp_path=tmp_path,
    )
    yield ctx
    db.close()


def connect_and_auth(stack, client, room_id: str, token: str):
    """建立 WS 会话并完成鉴权,返回 (会话, 快照)。

    会话以 stack.enter_context 管理(适配 starlette 1.10);
    自己的 member_joined 广播(注册后全体广播)在此一并消费。
    """
    ws = stack.enter_context(client.websocket_connect(f"/ws/rooms/{room_id}"))
    ws.send_json({"type": "auth", "token": token})
    snap = ws.receive_json()
    assert snap["type"] == "auth_ok"
    join = ws.receive_json()
    assert join["type"] == "member_joined"
    return ws, snap


def upload_headers(token: str, filename: str) -> dict:
    """视频上传请求头(X-Filename URL 编码,api.md 2.5)。"""
    from urllib.parse import quote

    return {
        "Authorization": f"Bearer {token}",
        "X-Filename": quote(filename),
        "Content-Type": "application/octet-stream",
    }


def auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def put_video_file(ctx, video_id: str, content: bytes) -> Path:
    """绕过 HTTP 直接落一个正式视频文件并登记 DB(清理用例造数)。"""
    from server.app.models import VideoMeta

    ctx.storage.ensure_dirs()
    await ctx.video_repo.insert_video(
        VideoMeta(video_id=video_id, original_name=f"{video_id}.mp4", size=len(content)),
        "ROOMCLEANUP",
        Path("videos") / f"{video_id}.mp4",
        1.0,
    )
    ctx.storage.video_path(video_id).write_bytes(content)
    return ctx.storage.video_path(video_id)
