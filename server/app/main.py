"""FastAPI 应用装配(design.md 4.1,选型 T1)。

职责仅限组装与生命周期,不含业务逻辑:
  - 构建依赖图:config → logger → db/存储实现 → RoomManager →
    api/ws/cleanup → FastAPI 装配;
  - CORS 中间件(分离部署,决策 T16,配置 [cors].allowed_origins);
  - lifespan:ensure_dirs → 启动清理 → 启动心跳扫描与清理任务;
    shutdown 时停止后台任务;
  - workers 固定为 1(design.md 4.6 硬约束),由启动命令保证。

启动命令(design.md 第 8 节):
  uv run uvicorn server.app.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from server.app.api import create_api_router, create_media_router, register_error_handlers
from server.app.cleanup import CleanupTask
from server.app.config import Settings, load_settings
from server.app.db import RoomRepository, VideoRepository
from server.app.logger import setup_logging
from server.app.room_manager import RoomManager
from server.app.playback import PlaybackService
from server.app.video_storage import VideoStorage
from server.app.ws import create_ws_router


def build_app(settings: Settings | None = None) -> FastAPI:
    """构建并装配 FastAPI 应用。

    Args:
        settings: 显式传入配置(测试用);None 时按配置文件/环境变量加载。
    """
    settings = settings or load_settings()

    # 基础设施:日志(数据目录下的 logs/,测试注入 tmp 时同样生效)
    log_dir = Path(settings.storage.data_dir) / "logs"
    setup_logging(settings.log, log_dir)

    # 具体实现(SqliteRoomRepository / SqliteVideoRepository /
    # LocalVideoStorage)在实现阶段补齐,此处仅声明依赖槽位。
    room_repo: RoomRepository = _make_room_repo(settings)
    video_repo: VideoRepository = _make_video_repo(settings)
    storage: VideoStorage = _make_storage(settings)

    # 领域服务
    playback = PlaybackService()
    room_manager = RoomManager(settings, room_repo, video_repo, storage, playback)

    # 后台任务
    cleanup = CleanupTask(settings, room_manager, storage, room_repo, video_repo)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # 启动:建目录 → 启动清理 → 后台任务
        storage.ensure_dirs()
        await cleanup.startup_cleanup()
        cleanup.start()
        yield
        # 关闭:停止后台任务
        await cleanup.stop()

    app = FastAPI(title="movie-togher", version="0.1.0", lifespan=lifespan)

    # CORS(前后端分离部署,决策 T16)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors.allowed_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "HEAD", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Filename"],
    )

    # 路由
    app.include_router(create_api_router(settings, room_manager, storage, room_repo, video_repo))
    app.include_router(create_media_router(settings, storage, video_repo))
    app.include_router(create_ws_router(settings, room_manager))

    @app.get("/healthz")
    async def healthz() -> dict:
        """健康检查(api.md 2.7)。"""
        return {"status": "ok"}

    register_error_handlers(app)
    return app


def _make_room_repo(settings: Settings) -> RoomRepository:
    """构造 rooms 表仓储(实现阶段返回 SqliteRoomRepository)。"""
    raise NotImplementedError


def _make_video_repo(settings: Settings) -> VideoRepository:
    """构造 videos 表仓储(实现阶段返回 SqliteVideoRepository)。"""
    raise NotImplementedError


def _make_storage(settings: Settings) -> VideoStorage:
    """构造视频存储(实现阶段返回 LocalVideoStorage)。"""
    raise NotImplementedError


app = None  # 实现阶段:app = build_app()(供 uvicorn 引用)
