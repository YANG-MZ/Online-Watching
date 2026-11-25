"""FastAPI 应用装配(design.md 4.1,选型 T1)。

职责仅限组装与生命周期,不含业务逻辑:
  - 构建依赖图:config → logger → SQLite 仓储/存储实现 → RoomManager →
    ConnectionRegistry → api/ws/cleanup → FastAPI 装配;
  - CORS 中间件(分离部署,决策 T16,配置 [cors].allowed_origins);
  - lifespan:ensure_dirs → 启动清理 → 启动周期清理任务与心跳扫描;
    shutdown 时全部取消;
  - workers 固定为 1(design.md 4.6 硬约束),由启动命令保证。

启动命令(design.md 第 8 节):
  uv run uvicorn server.app.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from server.app.api import create_api_router, create_media_router, register_error_handlers
from server.app.cleanup import CleanupTask
from server.app.config import Settings, load_settings
from server.app.db import SqliteDatabase, SqliteRoomRepository, SqliteVideoRepository
from server.app.logger import setup_logging
from server.app.playback import PlaybackService
from server.app.room_manager import RoomManager
from server.app.video_storage import LocalVideoStorage
from server.app.ws import ConnectionRegistry, create_ws_router


def build_app(settings: Settings | None = None) -> FastAPI:
    """构建并装配 FastAPI 应用。

    Args:
        settings: 显式传入配置(测试用);None 时按配置文件/环境变量加载。
    """
    settings = settings or load_settings()

    # 基础设施:日志(data 目录下的 logs/)
    data_dir = Path(settings.storage.data_dir)
    setup_logging(settings.log, data_dir / "logs")

    # P1 生产实现:SQLite 仓储 + 本地视频存储
    db = SqliteDatabase(data_dir / "app.db")
    room_repo = SqliteRoomRepository(db)
    video_repo = SqliteVideoRepository(db)
    storage = LocalVideoStorage(data_dir, settings.limits.max_upload_bytes)

    # 领域服务与连接注册表
    playback = PlaybackService()
    room_manager = RoomManager(settings, room_repo, video_repo, storage, playback)
    registry = ConnectionRegistry()

    # 后台任务
    cleanup = CleanupTask(settings, room_manager, storage, room_repo, video_repo)

    # 路由(ws 返回心跳扫描协程,由 lifespan 启动)
    api_router = create_api_router(settings, room_manager, storage, registry)
    media_router = create_media_router(settings, storage)
    ws_router, heartbeat_scan = create_ws_router(settings, room_manager, playback, registry)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # 启动:建目录 → 启动清理(需求 4.5)→ 周期清理 + 心跳扫描
        storage.ensure_dirs()
        await cleanup.startup_cleanup()
        cleanup.start()
        heartbeat_task = asyncio.create_task(heartbeat_scan())
        yield
        # 关闭:停止后台任务
        heartbeat_task.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat_task
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

    # 路由挂载
    app.include_router(api_router)
    app.include_router(media_router)
    app.include_router(ws_router)

    @app.get("/healthz")
    async def healthz() -> dict:
        """健康检查(api.md 2.7)。"""
        return {"status": "ok"}

    # 单 URL 模式(design.md T18):后端托管前端构建产物,同源无 CORS。
    # 必须在 API/media/ws 路由之后注册,catch-all 才不会抢占接口路径。
    if settings.frontend.serve_dist:
        _mount_spa(app, Path(settings.frontend.dist_dir))

    register_error_handlers(app)
    return app


def _mount_spa(app: FastAPI, dist_dir: Path) -> None:
    """托管前端构建产物(design.md T18,serve_dist 开启时)。

    - /assets/* 由 StaticFiles 服务(vite 产物,带指纹);
    - 其余 GET 路径:命中真实文件则返回,否则回退 index.html(SPA 深链接);
    - api/media/ws 前缀与路径穿越始终 404,不返回页面。
    """
    index = dist_dir / "index.html"
    assets = dist_dir / "assets"
    if not (index.is_file() and assets.is_dir()):
        return  # 产物不存在:跳过托管,按分离部署使用

    app.mount("/assets", StaticFiles(directory=assets), name="spa-assets")

    @app.get("/{full_path:path}", include_in_schema=False, response_model=None)
    async def spa_fallback(full_path: str) -> FileResponse | JSONResponse:
        if full_path.startswith(("api/", "media/", "ws/", "healthz")):
            return JSONResponse(
                status_code=404, content={"code": "NOT_FOUND", "message": "资源不存在"}
            )
        candidate = (dist_dir / full_path).resolve()
        # 路径穿越防护:解析后必须仍在 dist 目录内
        if (
            full_path
            and candidate.is_file()
            and str(candidate).startswith(str(dist_dir.resolve()))
        ):
            return FileResponse(candidate)
        return FileResponse(index)


app = build_app()  # 供 uvicorn 引用:server.app.main:app
