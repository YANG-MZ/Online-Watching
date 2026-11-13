"""HTTP API 端点(design.md 4.1,协议见 api.md 第 2 节)。

本阶段仅定义路由签名与错误映射,业务逻辑后续实现。

端点清单(api.md 第 2 节):
  POST /api/rooms                       创建房间(2.1)
  POST /api/rooms/{room_id}/join        加入房间(2.2)
  POST /api/rooms/{room_id}/leave       主动退出(2.3)
  POST /api/rooms/{room_id}/dissolve    解散房间,仅房主(2.4)
  PUT  /api/rooms/{room_id}/video       上传/替换视频,仅房主(2.5)
  GET  /media/{video_id}.mp4            视频文件访问,支持 Range(2.6)
  GET  /healthz                         健康检查(2.7)

错误映射(api.md 1.1/1.2):RoomError 子类 → HTTP 状态码 + JSON
{code, message};依赖方向:api → room_manager / video_storage / db / config。
"""

from __future__ import annotations

from fastapi import APIRouter, FastAPI, Request

from server.app.config import Settings
from server.app.db import RoomRepository, VideoRepository
from server.app.room_manager import RoomManager
from server.app.video_storage import VideoStorage

# HTTP 状态码映射(api.md 1.2)
ERROR_STATUS_MAP: dict[str, int] = {
    "INVALID_NICKNAME": 400,
    "INVALID_FORMAT": 400,
    "UNAUTHORIZED": 401,
    "FORBIDDEN": 403,
    "ROOM_NOT_FOUND": 404,
    "UPLOAD_IN_PROGRESS": 409,
    "FILE_TOO_LARGE": 413,
    "ROOM_LIMIT_REACHED": 429,
}


def create_api_router(
    settings: Settings,
    room_manager: RoomManager,
    storage: VideoStorage,
    room_repo: RoomRepository,
    video_repo: VideoRepository,
) -> APIRouter:
    """构造 HTTP 路由(依赖注入,便于测试替换假实现)。"""

    router = APIRouter(prefix="/api")

    @router.post("/rooms")
    async def create_room(payload: dict) -> dict:
        """创建房间(api.md 2.1):校验 → 签发凭证 → 返回 room_id/token。"""

    @router.post("/rooms/{room_id}/join")
    async def join_room(room_id: str, payload: dict) -> dict:
        """加入房间(api.md 2.2):校验存在与昵称 → 签发凭证。"""

    @router.post("/rooms/{room_id}/leave")
    async def leave_room(room_id: str, request: Request) -> dict:
        """主动退出(api.md 2.3):移除成员、房主移交、空房倒计时。"""

    @router.post("/rooms/{room_id}/dissolve")
    async def dissolve_room(room_id: str, request: Request) -> dict:
        """解散房间(api.md 2.4):仅房主;立即清理文件与 DB 行。"""

    @router.put("/rooms/{room_id}/video")
    async def upload_video(room_id: str, request: Request) -> dict:
        """上传/替换视频(api.md 2.5):流式写盘 → 原子重命名 → 状态重置。"""

    return router


def create_media_router(settings: Settings, storage: VideoStorage, video_repo: VideoRepository) -> APIRouter:
    """构造视频下发路由(api.md 2.6,手写 Range 处理,选型 T9)。"""

    router = APIRouter()

    @router.get("/media/{video_id}.mp4")
    async def get_video(video_id: str, request: Request):
        """视频文件访问:206/416/404,分块异步流式输出(1 MiB/块)。"""

    @router.head("/media/{video_id}.mp4")
    async def head_video(video_id: str) -> dict:
        """返回文件大小与类型头,无响应体。"""

    return router


def register_error_handlers(app: FastAPI) -> None:
    """注册 RoomError → HTTP JSON 错误响应的统一映射(api.md 1.1)。"""
