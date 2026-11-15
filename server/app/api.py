"""HTTP API 端点(design.md 4.1,协议见 api.md 第 2 节)。

端点清单(api.md 第 2 节):
  POST /api/rooms                       创建房间(2.1)
  POST /api/rooms/{room_id}/join        加入房间(2.2)
  POST /api/rooms/{room_id}/leave       主动退出(2.3)
  POST /api/rooms/{room_id}/dissolve    解散房间,仅房主(2.4)
  PUT  /api/rooms/{room_id}/video       上传/替换视频,仅房主(2.5)
  GET  /media/{video_id}.mp4            视频文件访问,支持 Range(2.6)
  HEAD /media/{video_id}.mp4            视频文件头(2.6)

错误映射(api.md 1.1/1.2):RoomError 按 ERROR_STATUS_MAP 映射 HTTP
状态码;api 层自有错误(ApiError:扩展名/大小/Range)直接携带状态码;
响应统一 {code, message}。

依赖方向:api → room_manager / video_storage / ws(连接注册表)/ config。
"""

from __future__ import annotations

import re
import time
import urllib.parse
from pathlib import Path

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.websockets import WebSocket

from server.app.config import Settings
from server.app.models import VideoMeta
from server.app.room_manager import (
    RoomError,
    RoomManager,
    UnauthorizedError,
    UploadInProgressError,
)
from server.app.security import generate_upload_id, generate_video_id
from server.app.video_storage import (
    UploadAbortedError,
    UploadSizeExceededError,
    VideoStorage,
)
from server.app.ws import WS_CLOSE_ROOM_DISSOLVED, ConnectionRegistry, apply_room_events

# RoomError.code → HTTP 状态码(api.md 1.2)
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


class ApiError(Exception):
    """api 层自有错误(非房间领域):携带 HTTP 状态码与 api.md 1.1 结构。"""

    def __init__(self, status: int, code: str, message: str) -> None:
        self.status = status
        self.code = code
        self.message = message
        super().__init__(message)


def _bearer_token(request: Request) -> str:
    """解析 Authorization: Bearer;缺失/格式错误按 401(api.md 1.2)。"""
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer ") and len(auth) > 7:
        return auth[7:].strip()
    raise UnauthorizedError()


def _invite_url(settings: Settings, room_id: str) -> str:
    """邀请链接:配置了前端地址则拼绝对地址,否则相对路径(design.md 4.10)。"""
    base = settings.frontend.base_url.rstrip("/")
    return f"{base}/rooms/{room_id}" if base else f"/rooms/{room_id}"


def _parse_range(header: str, size: int) -> tuple[int, int]:
    """解析单段 Range 头(api.md 2.6),返回 (start, end) 含端。

    支持 bytes=start-end / bytes=start- / bytes=-suffix(后缀式);
    end 超出文件尾钳制到 size-1;start 越界/区间非法抛 416。
    """
    m = re.fullmatch(r"bytes=(\d*)-(\d*)", header.strip())
    if m is None or (not m.group(1) and not m.group(2)):
        raise ApiError(416, "RANGE_NOT_SATISFIABLE", "Range 请求不合法")
    start_s, end_s = m.group(1), m.group(2)

    if start_s == "":
        # 后缀式:取文件末尾 N 字节
        suffix = int(end_s)
        if suffix == 0 or size == 0:
            raise ApiError(416, "RANGE_NOT_SATISFIABLE", "Range 请求不合法")
        length = min(suffix, size)
        return (size - length, size - 1)

    start = int(start_s)
    if start >= size:
        raise ApiError(416, "RANGE_NOT_SATISFIABLE", "Range 起始位置越界")
    if end_s == "":
        return (start, size - 1)
    end = int(end_s)
    if end < start:
        raise ApiError(416, "RANGE_NOT_SATISFIABLE", "Range 区间非法")
    return (start, min(end, size - 1))


async def _close_conn(conn: WebSocket, code: int) -> None:
    try:
        await conn.close(code=code)
    except Exception:
        pass


def create_api_router(
    settings: Settings,
    room_manager: RoomManager,
    storage: VideoStorage,
    registry: ConnectionRegistry,
) -> APIRouter:
    """构造 HTTP 路由(依赖注入,便于测试替换假实现)。"""

    router = APIRouter(prefix="/api")

    @router.post("/rooms")
    async def create_room(payload: dict) -> dict:
        """创建房间(api.md 2.1):校验 → 签发房主凭证 → 写 DB 行。"""
        cred = await room_manager.create_room(payload.get("nickname", ""), now=time.time())
        return {
            "room_id": cred.room_id,
            "invite_url": _invite_url(settings, cred.room_id),
            "user_id": cred.user_id,
            "session_token": cred.session_token,
            "is_host": cred.is_host,
        }

    @router.post("/rooms/{room_id}/join")
    async def join_room(room_id: str, payload: dict) -> dict:
        """加入房间(api.md 2.2):校验存在与昵称 → 签发普通成员凭证。

        满员(在线口径)在 WS auth 时以 close 4002 拒绝(design.md 4.4)。
        """
        cred = await room_manager.join_room(room_id, payload.get("nickname", ""), now=time.time())
        return {
            "room_id": cred.room_id,
            "user_id": cred.user_id,
            "session_token": cred.session_token,
            "is_host": cred.is_host,
        }

    @router.post("/rooms/{room_id}/leave")
    async def leave_room(room_id: str, request: Request) -> dict:
        """主动退出(api.md 2.3):移除成员 → 关闭其 WS → 广播事件。

        房主退出时移交事件(host_changed)由 RoomManager 一并返回。
        """
        token = _bearer_token(request)
        resolved = room_manager.resolve_member(room_id, token)
        conn = resolved[1].conn if resolved else None
        user_id = resolved[1].user_id if resolved else None

        events = await room_manager.leave_room(room_id, token, now=time.time())

        # 先移除并关闭退出者连接(不收自己的 member_left),再广播
        if conn is not None and user_id is not None:
            registry.remove(room_id, user_id, conn)
            await _close_conn(conn, 1000)
        await apply_room_events(registry, events)
        return {"ok": True}

    @router.post("/rooms/{room_id}/dissolve")
    async def dissolve_room(room_id: str, request: Request) -> dict:
        """解散房间(api.md 2.4):仅房主;广播 room_dissolved → 关闭全部
        连接(4005)→ RoomManager 立即清理文件与 DB 行。"""
        token = _bearer_token(request)
        events = await room_manager.dissolve_room(room_id, token, now=time.time())
        await registry.close_room(room_id, events[0].message, WS_CLOSE_ROOM_DISSOLVED)
        return {"ok": True}

    @router.put("/rooms/{room_id}/video")
    async def upload_video(room_id: str, request: Request) -> dict:
        """上传/替换视频(api.md 2.5,design.md 4.7 流程):

        预检顺序:房主(403)→ 扩展名(400)→ Content-Length(413)
        → 上传互斥(409);流式写 tmp → 原子重命名 → DB 行 → 状态重置
        → 广播 video_ready / video_changed。
        """
        token = _bearer_token(request)

        # 步骤 1:房主校验;409 互斥暂存,保持 design.md 4.7 校验顺序
        mutex_error: UploadInProgressError | None = None
        try:
            room_manager.begin_upload(room_id, token)
        except UploadInProgressError as e:
            mutex_error = e

        def release_mutex() -> None:
            if mutex_error is None:
                room_manager.fail_upload(room_id)

        # 步骤 2:扩展名(X-Filename URL 解码后须 .mp4)
        filename = urllib.parse.unquote(request.headers.get("x-filename", ""))
        if not filename.lower().endswith(".mp4"):
            release_mutex()
            raise ApiError(400, "INVALID_FORMAT", "仅支持 .mp4 文件")

        # 步骤 3:Content-Length 预检(缺失时由流式累计兜底)
        content_length = request.headers.get("content-length", "")
        if content_length.isdigit() and int(content_length) > settings.limits.max_upload_bytes:
            release_mutex()
            raise ApiError(
                413, "FILE_TOO_LARGE", f"文件超过大小上限({settings.limits.max_upload_bytes} 字节)"
            )

        # 步骤 4:互斥拒绝
        if mutex_error is not None:
            raise mutex_error

        upload_id = generate_upload_id()
        video_id = generate_video_id()

        def abort_check() -> bool:
            # design.md 4.7 步骤 6:写盘中途复查房主身份与房间存续
            return room_manager.upload_abort_check(room_id, token)

        try:
            # 步骤 5/6:流式写盘 + 周期中止复查
            size = await storage.write_upload(upload_id, request.stream(), abort_check=abort_check)
            # 步骤 7:fsync(写入末尾已完成)后原子重命名
            await storage.promote_upload(upload_id, video_id)
            # 步骤 8/9:替换删旧行旧文件、写新行、状态重置
            meta = VideoMeta(video_id=video_id, original_name=filename, size=size)
            event = await room_manager.complete_upload(
                room_id, token, meta, Path("videos") / f"{video_id}.mp4", now=time.time()
            )
        except UploadSizeExceededError as e:
            await storage.abort_upload(upload_id)
            room_manager.fail_upload(room_id)
            raise ApiError(
                413, "FILE_TOO_LARGE", f"文件超过大小上限({settings.limits.max_upload_bytes} 字节)"
            ) from e
        except UploadAbortedError:
            # 中止复查命中:房主已变更(403)或房间已解散(404),api.md 2.5
            await storage.abort_upload(upload_id)
            room_manager.fail_upload(room_id)
            if room_manager.get_room(room_id) is None:
                raise ApiError(404, "ROOM_NOT_FOUND", "房间不存在或已关闭")
            raise ApiError(403, "FORBIDDEN", "仅房主可执行此操作")
        except RoomError:
            await storage.abort_upload(upload_id)
            room_manager.fail_upload(room_id)
            raise
        except Exception:
            # 客户端断开等未知异常:清理临时文件与互斥锁后原样抛出
            await storage.abort_upload(upload_id)
            room_manager.fail_upload(room_id)
            raise

        # 步骤 10:广播 video_ready / video_changed
        await apply_room_events(registry, [event])
        return {"video_id": video_id, "original_name": filename, "size": size}

    return router


def create_media_router(settings: Settings, storage: VideoStorage) -> APIRouter:
    """构造视频下发路由(api.md 2.6,手写 Range 处理,选型 T9)。"""

    router = APIRouter()

    @router.get("/media/{video_id}.mp4")
    async def get_video(video_id: str, request: Request):
        """视频访问:206(Range 单段)/ 200 全量 / 404 / 416;分块异步流式输出。"""
        size = storage.stat_size(video_id)
        if size is None:
            raise ApiError(404, "VIDEO_NOT_FOUND", "视频不存在或已被清理")

        range_header = request.headers.get("range")
        if range_header:
            start, end = _parse_range(range_header, size)
            length = end - start + 1
            return StreamingResponse(
                storage.open_range(video_id, start, length),
                status_code=206,
                media_type="video/mp4",
                headers={
                    "Content-Range": f"bytes {start}-{end}/{size}",
                    "Accept-Ranges": "bytes",
                    "Content-Length": str(length),
                },
            )
        return StreamingResponse(
            storage.open_range(video_id, 0, size),
            media_type="video/mp4",
            headers={"Accept-Ranges": "bytes", "Content-Length": str(size)},
        )

    @router.head("/media/{video_id}.mp4")
    async def head_video(video_id: str):
        """文件头:大小与类型,无响应体。"""
        size = storage.stat_size(video_id)
        if size is None:
            raise ApiError(404, "VIDEO_NOT_FOUND", "视频不存在或已被清理")
        return Response(
            status_code=200,
            media_type="video/mp4",
            headers={"Accept-Ranges": "bytes", "Content-Length": str(size)},
        )

    return router


def register_error_handlers(app: FastAPI) -> None:
    """统一错误映射(api.md 1.1):响应体统一为 {code, message}。"""

    @app.exception_handler(RoomError)
    async def _room_error_handler(request: Request, exc: RoomError):
        status = ERROR_STATUS_MAP.get(exc.code, 400)
        return JSONResponse(status_code=status, content={"code": exc.code, "message": exc.message})

    @app.exception_handler(ApiError)
    async def _api_error_handler(request: Request, exc: ApiError):
        return JSONResponse(status_code=exc.status, content={"code": exc.code, "message": exc.message})
