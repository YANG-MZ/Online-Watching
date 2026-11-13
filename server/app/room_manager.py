"""房间领域服务(design.md 4.3/4.4/4.5):生命周期、成员、满员、移交、复活。

本阶段仅定义接口与依赖注入形式,业务逻辑后续实现。行为规范:

- 房间生命周期状态机与空房复活:design.md 4.3(决策 T14);
- 进入房间以 WS auth 为准,满员按在线人数口径(决策 T13);
- 同 token 第二条连接踢旧连接(决策 T15);
- 房主移交:仅主动退出触发,移交给加入最早成员(需求 3.1.5);
- 事件广播(成员增减/房主变更/状态更新/视频事件)由 ws 层的
  连接管理器完成,本服务通过事件回调/返回值向 ws 层传达需广播的事件。

依赖方向:room_manager → models / db / video_storage / playback / security / config。
所有方法在事件循环线程内调用(design.md 4.6)。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from server.app.config import Settings
from server.app.db import RoomRepository, VideoRepository
from server.app.models import Room, SessionCredentials
from server.app.playback import PlaybackService
from server.app.video_storage import VideoStorage

if TYPE_CHECKING:
    from starlette.websockets import WebSocket


class RoomError(Exception):
    """房间领域错误基类;子类携带 api.md 第 4 节错误码语义。"""

    code: str = "ROOM_ERROR"


class RoomLimitReachedError(RoomError):
    """房间数达到上限(HTTP 429,验收 16)。"""

    code = "ROOM_LIMIT_REACHED"


class RoomNotFoundError(RoomError):
    """房间不存在/已过期/重启失效(HTTP 404 / WS 4003)。"""

    code = "ROOM_NOT_FOUND"


class RoomFullError(RoomError):
    """满员,按在线口径(WS 4002,决策 T13)。"""

    code = "ROOM_FULL"


class InvalidNicknameError(RoomError):
    """昵称为空或超过 32 字符(HTTP 400)。"""

    code = "INVALID_NICKNAME"


class UnauthorizedError(RoomError):
    """session_token 无效(HTTP 401 / WS 4001)。"""

    code = "UNAUTHORIZED"


class ForbiddenError(RoomError):
    """非房主调用房主操作(HTTP 403 / WS error,需求 3.2.2)。"""

    code = "FORBIDDEN"


class RoomManager:
    """房间状态的唯一持有者(内存态,需求 4.5)。"""

    def __init__(
        self,
        settings: Settings,
        room_repo: RoomRepository,
        video_repo: VideoRepository,
        storage: VideoStorage,
        playback: PlaybackService,
    ) -> None:
        self._settings = settings
        self._room_repo = room_repo
        self._video_repo = video_repo
        self._storage = storage
        self._playback = playback

    # ---------- 房间生命周期(需求 3.1.1/3.1.2/3.1.7) ----------

    async def create_room(self, nickname: str, now: float) -> SessionCredentials:
        """创建房间:校验昵称与房间数上限(内存口径)→ 生成 ID/凭证 →
        写 SQLite rooms 行 → 返回房主凭证。不占在线名额。"""

    async def join_room(self, room_id: str, nickname: str, now: float) -> SessionCredentials:
        """加入(HTTP 侧):校验房间存在与昵称,签发凭证;
        满员校验延后至 WS auth(在线口径,design.md 4.4)。"""

    async def dissolve_room(self, room_id: str, session_token: str, now: float) -> None:
        """解散(仅房主,需求 3.1.6):校验 → 广播事件由 ws 层发出 →
        删视频文件与 DB 行 → 销毁内存房间。立即完成。"""

    def get_room(self, room_id: str) -> Room | None:
        """按 ID 取内存房间;不存在返回 None。"""

    def room_count(self) -> int:
        """当前未清理房间数(创建上限判定口径)。"""

    # ---------- 成员与在线管理(需求 3.1.3/3.6.2,决策 T13/T15) ----------

    async def authenticate(
        self,
        room_id: str,
        session_token: str,
        conn: "WebSocket",
        now: float,
    ) -> Room:
        """WS 鉴权与进入房间:校验房间存在(含空房复活,决策 T14)→
        校验 token → 满员拒绝(在线口径)→ 同 token 踢旧连接(决策 T15)→
        加入在线成员(joined_at 经 known_users 沿用)。

        Raises:
            RoomNotFoundError / UnauthorizedError / RoomFullError
            (ws 层映射为 close 4003/4001/4002)。
        """

    def resolve_member(self, room_id: str, session_token: str) -> tuple[Room, object] | None:
        """按 token 定位在线成员,供后续 WS 消息鉴权;无效返回 None。

        返回 (room, member)。
        """

    def touch(self, room: Room, member_user_id: str, now: float) -> None:
        """刷新成员 last_seen(任意 WS 消息均刷新,需求 3.6.2)。"""

    async def leave_room(self, room_id: str, session_token: str, now: float) -> None:
        """主动退出(需求 3.1.4):移除成员并触发房主移交(需求 3.1.5)。"""

    def sweep_timeouts(self, now: float) -> list[object]:
        """心跳超时扫描(需求 3.6.2):移除 last_seen 超时成员,不触发移交
        (房主掉线不移交);返回待广播的 member_left(timeout)事件列表。"""

    # ---------- 快照(需求 3.7) ----------

    def snapshot_payload(self, room: Room, for_user_id: str) -> dict:
        """构造全量状态快照(api.md 3.2 结构):you + room(members/
        playback/video)+ server_ts 由 ws 层补齐。"""

    # ---------- 视频事件回调(由 api 层上传完成后调用) ----------

    async def on_video_ready(self, room_id: str, now: float) -> None:
        """上传/替换完成:重置权威状态(playback.reset_for_new_video)。"""
