"""WebSocket 端点与连接管理(design.md 4.1,协议见 api.md 第 3 节)。

本阶段仅定义接口签名,业务逻辑后续实现。

职责:
  - WS 端点 /ws/rooms/{room_id}:连接后首条消息 auth(10s 超时,
    close 4001),鉴权成功回 auth_ok 全量快照并广播 member_joined;
  - 连接管理器:广播事件(member_joined/left、host_changed、
    state_update、video_ready/changed、room_dissolved、kicked、error);
  - 消息路由:play/pause/seek/rate(仅房主,验收 9)、ping→pong;
  - 心跳扫描协程:server_timeout_seconds 判死 → 移除成员 → 广播
    member_left{reason:"timeout"}(需求 3.6.2,掉线不移交);
  - close code 映射(api.md 3.5):4001 鉴权失败 / 4002 满员 /
    4003 房间不存在 / 4004 被踢 / 4005 解散;4002~4005 为终态。

依赖方向:ws → room_manager / playback / config。
"""

from __future__ import annotations

from fastapi import APIRouter
from starlette.websockets import WebSocket, WebSocketDisconnect

from server.app.config import Settings
from server.app.room_manager import RoomManager

# close code 常量(api.md 3.5)
WS_CLOSE_AUTH_FAILED = 4001
WS_CLOSE_ROOM_FULL = 4002
WS_CLOSE_ROOM_NOT_FOUND = 4003
WS_CLOSE_DUPLICATE_CONNECTION = 4004
WS_CLOSE_ROOM_DISSOLVED = 4005


class ConnectionRegistry:
    """房间连接注册表:room_id → user_id → 活跃 WebSocket。

    负责向房间内全体(或单人)投递 JSON 消息;被踢/断开的连接
    由 room_manager.authenticate(踢旧)与心跳扫描清理。
    """

    def __init__(self) -> None:
        self._conns: dict[str, dict[str, WebSocket]] = {}

    def register(self, room_id: str, user_id: str, conn: WebSocket) -> None:
        """登记连接(在 authenticate 成功后调用)。"""

    def remove(self, room_id: str, user_id: str, conn: WebSocket) -> None:
        """移除指定连接(仅在仍为当前活跃连接时移除,防误删接管后的新连接)。"""

    def get(self, room_id: str, user_id: str) -> WebSocket | None:
        """返回成员当前活跃连接;不在线返回 None。"""

    async def broadcast(self, room_id: str, payload: dict, exclude: str | None = None) -> None:
        """向房间全体在线成员广播;exclude 为排除的 user_id。"""

    async def send_to(self, room_id: str, user_id: str, payload: dict) -> None:
        """向单个成员投递(如 kicked、error)。"""


def create_ws_router(settings: Settings, room_manager: RoomManager) -> APIRouter:
    """构造 WS 路由与心跳扫描任务。"""

    registry = ConnectionRegistry()
    router = APIRouter()

    @router.websocket("/ws/rooms/{room_id}")
    async def room_ws(websocket: WebSocket, room_id: str) -> None:
        """房间 WS 端点:auth 鉴权 → 消息循环 → 断开清理。"""

    async def heartbeat_scan() -> None:
        """周期心跳扫描(design.md 4.4):sweep_timeouts → 关闭死连接 →
        广播 member_left{reason:"timeout"};由 main.lifespan 启动。"""

    return router
