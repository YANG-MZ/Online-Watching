"""WebSocket 端点与连接管理(design.md 4.1,协议见 api.md 第 3 节)。

职责:
  - ConnectionRegistry:room_id → user_id → 活跃 WebSocket 注册表,
    提供单发/广播/关闭房间全部连接;apply_room_events 消费领域事件
    (kick 单发并 close 4004,其余广播);
  - WS 端点 /ws/rooms/{room_id}:accept → 首条 auth(10s 超时)→
    authenticate → 回 auth_ok 快照 → 广播 member_joined → 消息循环;
  - 消息路由:ping→pong;play/pause/seek/rate(仅房主,非房主回
    error FORBIDDEN 不断连,验收 9);控制生效后广播 state_update;
  - 心跳扫描协程 heartbeat_scan:周期 sweep_timeouts → 关闭死连接 →
    广播 member_left{timeout}(需求 3.6.2);由 main lifespan 启动;
  - close code(api.md 3.5):4001 鉴权失败 / 4002 满员 / 4003 房间
    不存在 / 4004 被踢 / 4005 解散;鉴权失败先回 error 消息再关闭。

依赖方向:ws → room_manager / playback / config。
"""

from __future__ import annotations

import asyncio
import json
import time

from fastapi import APIRouter
from loguru import logger
from starlette.websockets import WebSocket, WebSocketDisconnect

from server.app.config import Settings
from server.app.playback import PlaybackService
from server.app.room_manager import (
    RoomError,
    RoomEvent,
    RoomFullError,
    RoomManager,
    RoomNotFoundError,
    UnauthorizedError,
)

# close code 常量(api.md 3.5)
WS_CLOSE_AUTH_FAILED = 4001
WS_CLOSE_ROOM_FULL = 4002
WS_CLOSE_ROOM_NOT_FOUND = 4003
WS_CLOSE_DUPLICATE_CONNECTION = 4004
WS_CLOSE_ROOM_DISSOLVED = 4005

AUTH_TIMEOUT_SECONDS = 10.0  # api.md 3.1:连接后首条 auth 的超时
CONTROL_TYPES = frozenset({"play", "pause", "seek", "rate"})


async def _close_quiet(conn: WebSocket, code: int | None = None) -> None:
    """关闭连接;对端已断开等异常静默(清理路径不抛错)。"""
    try:
        if code is None:
            await conn.close()
        else:
            await conn.close(code=code)
    except Exception:
        pass


async def _send_error(conn: WebSocket, code: str, message: str) -> None:
    """发送 error 消息(api.md 3.4);连接不可用时静默。"""
    try:
        await conn.send_json({"type": "error", "code": code, "message": message})
    except Exception:
        pass


class ConnectionRegistry:
    """房间连接注册表:room_id → user_id → 当前活跃 WebSocket。

    同一 user 被新连接接管后,旧连接条目被覆盖;remove 仅在传入
    conn 与当前活跃连接一致时生效,防止旧连接断开时误删新连接。
    """

    def __init__(self) -> None:
        self._conns: dict[str, dict[str, WebSocket]] = {}

    def register(self, room_id: str, user_id: str, conn: WebSocket) -> None:
        self._conns.setdefault(room_id, {})[user_id] = conn

    def remove(self, room_id: str, user_id: str, conn: WebSocket) -> None:
        room_conns = self._conns.get(room_id)
        if room_conns is not None and room_conns.get(user_id) is conn:
            room_conns.pop(user_id, None)
            if not room_conns:
                self._conns.pop(room_id, None)

    def get(self, room_id: str, user_id: str) -> WebSocket | None:
        return self._conns.get(room_id, {}).get(user_id)

    async def broadcast(self, room_id: str, payload: dict, exclude: str | None = None) -> None:
        """向房间全体在线成员广播;单个连接失败不阻断其余成员(记录日志便于排查)。"""
        for uid, conn in list(self._conns.get(room_id, {}).items()):
            if uid == exclude:
                continue
            try:
                await conn.send_json(payload)
            except Exception as e:
                logger.warning("broadcast 失败 room={} user={} err={}", room_id, uid, e)
                continue

    async def close_room(self, room_id: str, payload: dict, code: int) -> None:
        """向房间全部连接发送 payload 后关闭(解散场景,api.md 2.4),并清空注册。"""
        conns = list(self._conns.pop(room_id, {}).values())
        for conn in conns:
            try:
                await conn.send_json(payload)
            except Exception:
                pass
            await _close_quiet(conn, code)


async def apply_room_events(registry: ConnectionRegistry, events: list[RoomEvent]) -> None:
    """消费 RoomManager 返回的领域事件(design.md 4.4)。

    kick_conn 非空:向该连接单发消息后关闭(close 4004,决策 T15);
    否则向房间全体在线成员广播。
    """
    for evt in events:
        if evt.kick_conn is not None:
            try:
                await evt.kick_conn.send_json(evt.message)
            except Exception:
                pass
            await _close_quiet(evt.kick_conn, WS_CLOSE_DUPLICATE_CONNECTION)
        else:
            await registry.broadcast(evt.room_id, evt.message)


async def heartbeat_pass(room_manager: RoomManager, registry: ConnectionRegistry) -> None:
    """单轮心跳扫描:移除超时成员 → 关闭死连接 → 广播 member_left(timeout)。

    独立函数便于测试直接驱动;heartbeat_scan 周期调用。
    """
    events = room_manager.sweep_timeouts(now=time.time())
    for evt in events:
        user_id = evt.message.get("user_id")
        conn = registry.get(evt.room_id, user_id or "")
        if conn is not None:
            registry.remove(evt.room_id, user_id or "", conn)
            await _close_quiet(conn)
        await registry.broadcast(evt.room_id, evt.message)


def create_ws_router(
    settings: Settings,
    room_manager: RoomManager,
    playback: PlaybackService,
    registry: ConnectionRegistry | None = None,
) -> tuple[APIRouter, "asyncio.Task"]:  # noqa: UP037 - 返回 (路由, 心跳扫描协程函数)
    """构造 WS 路由与心跳扫描协程函数。

    Args:
        registry: 共享连接注册表;None 时内部新建(独立使用场景)。
            装配层必须注入与 api 层相同的实例,否则 HTTP 触发的
            leave/dissolve/上传广播无法到达 WS 连接。

    Returns:
        (router, heartbeat_scan);heartbeat_scan 由 main lifespan 以
        asyncio.create_task 启动,取消即退出。
    """
    registry = registry or ConnectionRegistry()
    router = APIRouter()

    @router.websocket("/ws/rooms/{room_id}")
    async def room_ws(websocket: WebSocket, room_id: str) -> None:
        """房间 WS 端点:auth 鉴权 → 消息循环 → 断开清理(api.md 3.1)。"""
        await websocket.accept()

        # ---- 鉴权阶段:首条消息必须为 auth,10s 超时(api.md 3.1) ----
        token: str | None = None
        try:
            raw = await asyncio.wait_for(websocket.receive_text(), timeout=AUTH_TIMEOUT_SECONDS)
            msg = json.loads(raw)
            if isinstance(msg, dict) and msg.get("type") == "auth":
                token = msg.get("token")
        except (asyncio.TimeoutError, WebSocketDisconnect, json.JSONDecodeError, ValueError):
            await _close_quiet(websocket, WS_CLOSE_AUTH_FAILED)
            return
        if not token or not isinstance(token, str):
            await _send_error(websocket, "UNAUTHORIZED", "身份凭证无效,请重新进入房间")
            await _close_quiet(websocket, WS_CLOSE_AUTH_FAILED)
            return

        # ---- 进入房间:错误回 error 消息后按 api.md 3.5 关闭 ----
        try:
            result = await room_manager.authenticate(room_id, token, websocket, now=time.time())
        except RoomNotFoundError as e:
            await _send_error(websocket, e.code, e.message)
            await _close_quiet(websocket, WS_CLOSE_ROOM_NOT_FOUND)
            return
        except RoomFullError as e:
            await _send_error(websocket, e.code, e.message)
            await _close_quiet(websocket, WS_CLOSE_ROOM_FULL)
            return
        except UnauthorizedError as e:
            await _send_error(websocket, e.code, e.message)
            await _close_quiet(websocket, WS_CLOSE_AUTH_FAILED)
            return

        user_id = result.member.user_id
        registry.register(room_id, user_id, websocket)

        # ---- 先消费 kick 事件(踢旧连接),再回快照,再广播成员事件 ----
        join_events: list[RoomEvent] = []
        for evt in result.events:
            if evt.kick_conn is not None:
                try:
                    await evt.kick_conn.send_json(evt.message)
                except Exception:
                    pass
                await _close_quiet(evt.kick_conn, WS_CLOSE_DUPLICATE_CONNECTION)
            else:
                join_events.append(evt)

        snapshot = room_manager.snapshot_payload(result.room, user_id)
        snapshot["server_ts"] = time.time()
        await websocket.send_json({"type": "auth_ok", **snapshot})

        for evt in join_events:
            await registry.broadcast(room_id, evt.message)

        # ---- 消息循环:任意消息刷新 last_seen(需求 3.6.2) ----
        async def handle_control(mtype: str, payload: dict) -> None:
            resolved = room_manager.resolve_member(room_id, token or "")
            if resolved is None:
                await _send_error(websocket, "ROOM_NOT_FOUND", "房间不存在或已关闭")
                return
            room, member = resolved
            if not room_manager.is_host(room, member.user_id):
                await _send_error(websocket, "FORBIDDEN", "仅房主可执行此操作")
                return

            now = time.time()
            if mtype == "play":
                playback.apply_play(room, now)
            elif mtype == "pause":
                playback.apply_pause(room, now)
            elif mtype == "seek":
                position = payload.get("position")
                if (
                    not isinstance(position, (int, float))
                    or isinstance(position, bool)
                    or position < 0
                ):
                    await _send_error(websocket, "SEEK_OUT_OF_RANGE", "跳转位置非法")
                    return
                playback.apply_seek(room, float(position), now)
            elif mtype == "rate":
                rate = payload.get("rate")
                lo = settings.playback.allowed_rate_min
                hi = settings.playback.allowed_rate_max
                if (
                    not isinstance(rate, (int, float))
                    or isinstance(rate, bool)
                    or not (lo <= rate <= hi)
                ):
                    await _send_error(websocket, "RATE_OUT_OF_RANGE", "倍速超出允许范围")
                    return
                playback.apply_rate(room, float(rate), now)

            pb = room.playback
            await registry.broadcast(
                room_id,
                {
                    "type": "state_update",
                    "state": pb.state,
                    "position": pb.position,
                    "rate": pb.rate,
                    "updated_at": pb.updated_at,
                    "version": pb.version,
                    "server_ts": time.time(),
                },
            )

        try:
            while True:
                raw = await websocket.receive_text()
                room_manager.touch(result.room, user_id, now=time.time())
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    await _send_error(websocket, "BAD_MESSAGE", "无法识别的消息")
                    continue
                if not isinstance(msg, dict):
                    await _send_error(websocket, "BAD_MESSAGE", "无法识别的消息")
                    continue
                mtype = msg.get("type")
                if mtype == "ping":
                    await websocket.send_json(
                        {"type": "pong", "t0": msg.get("t0"), "server_ts": time.time()}
                    )
                elif isinstance(mtype, str) and mtype in CONTROL_TYPES:
                    await handle_control(mtype, msg)
                else:
                    await _send_error(websocket, "BAD_MESSAGE", f"未知的消息类型: {mtype}")
        except WebSocketDisconnect:
            pass
        finally:
            registry.remove(room_id, user_id, websocket)

    async def heartbeat_scan() -> None:
        """周期心跳扫描(design.md 4.4);由 main lifespan 启动,取消即退出。"""
        interval = settings.heartbeat.scan_interval_seconds
        while True:
            await asyncio.sleep(interval)
            await heartbeat_pass(room_manager, registry)

    return router, heartbeat_scan
