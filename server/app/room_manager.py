"""房间领域服务(design.md 4.3/4.4/4.5):生命周期、成员、满员、移交、复活。

行为规范(需求条款):
- 生命周期:创建(上限拒绝,3.1.1)→ 活跃 → 空房倒计时(全员离开 10min,
  3.1.7)→ 过期清理;房主解散立即清理(3.1.6);
- 进入房间以 WS auth 为准,满员按在线人数口径(决策 T13);
- 同 token 第二条连接踢旧连接(决策 T15),joined_at 经 known_users 沿用;
- 房主移交:仅主动退出触发,移交给加入最早的在房成员(3.1.5);
  掉线(心跳超时)不移交,重连恢复控制权(验收 14);
- 空房复活:倒计时窗口内 WS auth 成功即取消计时(决策 T14);
- 快照:全量状态整体覆盖(3.7),结构见 api.md 3.2。

事件机制:本服务不直接广播,所有成员/房主/视频变动以 RoomEvent 列表
返回,由 ws 层消费(kick_conn 非空时先向该连接单发并关闭 4004,
否则向房间全体广播 message)。

依赖方向:room_manager → models / db / video_storage / playback / security / config。
所有方法在事件循环线程内调用;阻塞 IO(DB/文件)内部经 to_thread(P1 实现保证)。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from server.app.config import Settings
from server.app.db import RoomRepository, VideoRepository
from server.app.models import Member, Room, SessionCredentials, VideoMeta, KnownUser
from server.app.playback import PlaybackService
from server.app.security import (
    generate_room_id,
    generate_session_token,
    generate_user_id,
)
from server.app.video_storage import VideoStorage

if TYPE_CHECKING:
    from starlette.websockets import WebSocket

NICKNAME_MAX_LEN = 32  # api.md 2.1:昵称 1~32 字符


class RoomError(Exception):
    """房间领域错误基类;code 与 api.md 第 4 节错误码一一对应。"""

    code: str = "ROOM_ERROR"
    message: str = "房间操作失败"


class RoomLimitReachedError(RoomError):
    """房间数达到上限(HTTP 429,验收 16)。"""

    code = "ROOM_LIMIT_REACHED"
    message = "房间数量已达上限,请稍后再试"


class RoomNotFoundError(RoomError):
    """房间不存在/已过期/重启失效(HTTP 404 / WS 4003)。"""

    code = "ROOM_NOT_FOUND"
    message = "房间不存在或已关闭"


class RoomFullError(RoomError):
    """满员,按在线口径(WS 4002,决策 T13)。"""

    code = "ROOM_FULL"
    message = "房间已满,无法加入"


class InvalidNicknameError(RoomError):
    """昵称为空或超过 32 字符(HTTP 400)。"""

    code = "INVALID_NICKNAME"
    message = "昵称不能为空,且不超过 32 个字符"


class UnauthorizedError(RoomError):
    """session_token 无效(HTTP 401 / WS 4001)。"""

    code = "UNAUTHORIZED"
    message = "身份凭证无效,请重新进入房间"


class ForbiddenError(RoomError):
    """非房主调用房主操作(HTTP 403 / WS error,需求 3.2.2)。"""

    code = "FORBIDDEN"
    message = "仅房主可执行此操作"


class UploadInProgressError(RoomError):
    """房间内已有进行中的上传(HTTP 409,api.md 2.5)。"""

    code = "UPLOAD_IN_PROGRESS"
    message = "已有上传正在进行,请稍候"


@dataclass
class RoomEvent:
    """领域事件:ws 层消费。

    - kick_conn 为空:message 向房间全体在线成员广播;
    - kick_conn 非空:message 仅发送给该连接,随后关闭(close 4004)。
    """

    room_id: str
    message: dict
    kick_conn: "WebSocket | None" = None


@dataclass
class AuthResult:
    """WS 鉴权成功的结果:快照所需对象与待广播事件。"""

    room: Room
    member: Member
    events: list[RoomEvent] = field(default_factory=list)


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
        self._rooms: dict[str, Room] = {}

    # ---------- 内部工具 ----------

    def _get_room(self, room_id: str) -> Room:
        room = self._rooms.get(room_id)
        if room is None:
            raise RoomNotFoundError()
        return room

    def _user_id_of(self, room: Room, session_token: str) -> str:
        user_id = room.credentials.get(session_token)
        if user_id is None:
            raise UnauthorizedError()
        return user_id

    @staticmethod
    def _validate_nickname(nickname: str) -> str:
        nick = (nickname or "").strip()
        if not nick or len(nick) > NICKNAME_MAX_LEN:
            raise InvalidNicknameError()
        return nick

    def _require_host(self, room: Room, session_token: str) -> str:
        user_id = self._user_id_of(room, session_token)
        if room.host_user_id != user_id:
            raise ForbiddenError()
        return user_id

    def _member_payload(self, room: Room, member: Member) -> dict:
        return {
            "user_id": member.user_id,
            "nickname": member.nickname,
            "is_host": room.host_user_id == member.user_id,
        }

    async def _destroy_room(self, room: Room) -> None:
        """销毁房间:删视频文件 → 删 DB 行 → 移除内存对象(design.md 第 5 节顺序)。"""
        for meta, _ in await self._video_repo.list_by_room(room.room_id):
            await self._storage.delete(meta.video_id)
            await self._video_repo.delete_video(meta.video_id)
        if room.video is not None and self._storage.stat_size(room.video.video_id) is not None:
            # 内存 video 已不在 DB(极端路径兜底):直接按内存记录删文件
            await self._storage.delete(room.video.video_id)
        await self._room_repo.delete_room(room.room_id)
        self._rooms.pop(room.room_id, None)

    # ---------- 房间生命周期(需求 3.1.1/3.1.2/3.1.6/3.1.7) ----------

    async def create_room(self, nickname: str, now: float) -> SessionCredentials:
        """创建房间:昵称/上限校验(内存口径)→ 签发房主凭证 → 写 DB。

        创建者不占在线名额(进入房间以 WS auth 为准,design.md 4.4)。
        """
        nick = self._validate_nickname(nickname)
        if len(self._rooms) >= self._settings.limits.max_rooms:
            raise RoomLimitReachedError()

        room_id = generate_room_id()
        while room_id in self._rooms:  # 8 位 Base32 冲突概率极低,循环兜底
            room_id = generate_room_id()

        user_id = generate_user_id()
        token = generate_session_token()
        room = Room(room_id=room_id, host_user_id=user_id, created_at=now)
        room.known_users[user_id] = KnownUser(user_id, nick, now)
        room.credentials[token] = user_id
        self._rooms[room_id] = room

        await self._room_repo.insert_room(room_id, user_id, now)
        return SessionCredentials(room_id=room_id, user_id=user_id, session_token=token, is_host=True)

    async def join_room(self, room_id: str, nickname: str, now: float) -> SessionCredentials:
        """加入(HTTP 侧):校验房间存在与昵称 → 签发普通成员凭证。

        满员校验延后至 WS auth(在线口径,decision T13 / design.md 4.4)。
        """
        nick = self._validate_nickname(nickname)
        room = self._get_room(room_id)

        user_id = generate_user_id()
        token = generate_session_token()
        room.known_users[user_id] = KnownUser(user_id, nick, now)
        room.credentials[token] = user_id
        return SessionCredentials(room_id=room_id, user_id=user_id, session_token=token, is_host=False)

    async def dissolve_room(self, room_id: str, session_token: str, now: float) -> list[RoomEvent]:
        """解散(仅房主,需求 3.1.6):返回广播事件后立即清理,不等清理周期。"""
        room = self._get_room(room_id)
        self._require_host(room, session_token)

        events = [RoomEvent(room_id, {"type": "room_dissolved", "reason": "host_dissolved"})]
        await self._destroy_room(room)
        return events

    def get_room(self, room_id: str) -> Room | None:
        """按 ID 取内存房间;不存在返回 None。"""
        return self._rooms.get(room_id)

    def room_count(self) -> int:
        """当前未清理房间数(创建上限判定口径)。"""
        return len(self._rooms)

    # ---------- 成员与在线管理(需求 3.1.3/3.1.4/3.6.2) ----------

    async def authenticate(
        self,
        room_id: str,
        session_token: str,
        conn: "WebSocket",
        now: float,
    ) -> AuthResult:
        """WS 鉴权与进入房间(design.md 4.4)。

        顺序:房间存在(空房可复活,T14)→ token 有效 → 满员拒绝(在线
        口径,T13;重连/重复连接不占新名额)→ 同 token 踢旧(T15)→
        加入在线成员(joined_at/昵称经 known_users 沿用)→ 取消空房倒计时。

        Raises:
            RoomNotFoundError(4003) / UnauthorizedError(4001) / RoomFullError(4002)。
        """
        room = self._get_room(room_id)
        user_id = self._user_id_of(room, session_token)

        existing = room.members.get(user_id)
        if existing is None and len(room.members) >= self._settings.limits.max_members_per_room:
            raise RoomFullError()

        events: list[RoomEvent] = []
        if existing is not None and existing.conn is not None:
            # 决策 T15:新连接接管,旧连接收到 kicked 后关闭(4004)
            events.append(
                RoomEvent(
                    room_id,
                    {"type": "kicked", "reason": "duplicate_connection"},
                    kick_conn=existing.conn,
                )
            )

        known = room.known_users[user_id]
        if existing is None:
            member = Member(
                user_id=user_id,
                nickname=known.nickname,
                joined_at=known.joined_at,
                last_seen=now,
                conn=conn,
            )
            room.members[user_id] = member
        else:
            member = existing
            member.conn = conn
            member.last_seen = now

        room.empty_since = None  # 空房复活(决策 T14)
        events.append(
            RoomEvent(room_id, {"type": "member_joined", "user": self._member_payload(room, member)})
        )
        return AuthResult(room=room, member=member, events=events)

    def resolve_member(self, room_id: str, session_token: str) -> tuple[Room, Member] | None:
        """按 token 定位在线成员(活跃 WS),供控制消息鉴权;无效返回 None。"""
        room = self._rooms.get(room_id)
        if room is None:
            return None
        user_id = room.credentials.get(session_token)
        if user_id is None:
            return None
        member = room.members.get(user_id)
        if member is None or member.conn is None:
            return None
        return room, member

    def touch(self, room: Room, member_user_id: str, now: float) -> None:
        """刷新成员 last_seen(任意 WS 消息均刷新,需求 3.6.2)。"""
        member = room.members.get(member_user_id)
        if member is not None:
            member.last_seen = now

    def is_host(self, room: Room, user_id: str) -> bool:
        """是否房主(播放控制等 WS 消息权限判定,需求 3.2.2)。"""
        return room.host_user_id == user_id

    async def leave_room(self, room_id: str, session_token: str, now: float) -> list[RoomEvent]:
        """主动退出(需求 3.1.4):移除成员;房主退出立即移交(3.1.5)。

        不在线时幂等成功(无事件);known_users/credentials 保留,
        该用户之后再进入沿用 joined_at/昵称。
        """
        room = self._get_room(room_id)
        user_id = self._user_id_of(room, session_token)

        member = room.members.get(user_id)
        if member is None:
            return []

        room.members.pop(user_id, None)
        member.conn = None
        events = [
            RoomEvent(room_id, {"type": "member_left", "user_id": user_id, "reason": "leave"})
        ]

        if room.host_user_id == user_id:
            # 移交:在房成员按 joined_at 升序取最早(需求 3.1.5)
            candidates = sorted(room.members.values(), key=lambda m: m.joined_at)
            if candidates:
                room.host_user_id = candidates[0].user_id
                events.append(
                    RoomEvent(room_id, {"type": "host_changed", "host_user_id": room.host_user_id})
                )
            # 无候选:不移交,房间进入空房倒计时

        if not room.members:
            room.empty_since = now
        return events

    def sweep_timeouts(self, now: float) -> list[RoomEvent]:
        """心跳超时扫描(需求 3.6.2):移除超时成员;房主掉线不移交。

        移除后房间变空则记 empty_since;成员连接由 ws 层依据
        member_left 事件中的 user_id 关闭。
        """
        timeout = self._settings.heartbeat.server_timeout_seconds
        events: list[RoomEvent] = []
        for room in list(self._rooms.values()):
            dead = [m for m in room.members.values() if now - m.last_seen > timeout]
            if not dead:
                continue
            for member in dead:
                room.members.pop(member.user_id, None)
                member.conn = None
                events.append(
                    RoomEvent(
                        room.room_id,
                        {"type": "member_left", "user_id": member.user_id, "reason": "timeout"},
                    )
                )
            if not room.members:
                room.empty_since = now
        return events

    async def purge_expired_rooms(self, now: float) -> list[str]:
        """过期清理(design.md 4.9 第 1 条):销毁空房超时的房间,返回 room_id 列表。

        供 cleanup 周期任务调用;过期房间无人在线,无需广播事件。
        """
        expire = self._settings.room.empty_expire_seconds
        expired = [
            room
            for room in self._rooms.values()
            if room.empty_since is not None and now - room.empty_since >= expire
        ]
        for room in expired:
            await self._destroy_room(room)
        return [room.room_id for room in expired]

    # ---------- 快照(需求 3.7,api.md 3.2) ----------

    def snapshot_payload(self, room: Room, for_user_id: str) -> dict:
        """构造全量状态快照(server_ts 由 ws 层补齐)。

        position 为 updated_at 基准位置,客户端按推算公式计算当前进度
        (sync-protocol.md §1);members 按 joined_at 升序。
        """
        pb = room.playback
        members = sorted(room.members.values(), key=lambda m: m.joined_at)
        return {
            "you": {"user_id": for_user_id, "is_host": room.host_user_id == for_user_id},
            "room": {
                "room_id": room.room_id,
                "host_user_id": room.host_user_id,
                "members": [self._member_payload(room, m) for m in members],
                "playback": {
                    "state": pb.state,
                    "position": pb.position,
                    "rate": pb.rate,
                    "updated_at": pb.updated_at,
                    "version": pb.version,
                },
                "video": None
                if room.video is None
                else {
                    "video_id": room.video.video_id,
                    "original_name": room.video.original_name,
                    "size": room.video.size,
                },
            },
        }

    # ---------- 视频上传(design.md 4.7,三段式:begin → 写盘 → complete/fail) ----------

    def begin_upload(self, room_id: str, session_token: str) -> Room:
        """上传预检:房间存在 → token 有效 → 房主(403)→ 无进行中上传(409)
        → 置上传互斥锁。扩展名/大小预检由 api 层完成(design.md 4.7 步骤 2/3)。"""
        room = self._get_room(room_id)
        self._require_host(room, session_token)
        if room.uploading:
            raise UploadInProgressError()
        room.uploading = True
        return room

    def upload_abort_check(self, room_id: str, session_token: str) -> bool:
        """存储层写盘中止复查回调(design.md 4.7 步骤 6)。

        返回 True 表示应中止:房间已解散,或凭证失效,或房主已变更。
        在工作线程执行,只读内存状态,无锁安全(单事件循环串行变更)。
        """
        room = self._rooms.get(room_id)
        if room is None:
            return True
        user_id = room.credentials.get(session_token)
        if user_id is None:
            return True
        return room.host_user_id != user_id

    async def complete_upload(
        self,
        room_id: str,
        session_token: str,
        video: VideoMeta,
        file_path: Path,
        now: float,
    ) -> RoomEvent:
        """上传完成:替换场景删旧文件与旧行(需求 D4)→ 写新 DB 行 →
        内存 video 更新 + 状态重置 → 返回 video_ready / video_changed 事件。"""
        room = self._get_room(room_id)
        self._require_host(room, session_token)  # 正常完成时防御复查

        old = room.video
        if old is not None:
            await self._storage.delete(old.video_id)
            await self._video_repo.delete_video(old.video_id)

        await self._video_repo.insert_video(video, room.room_id, file_path, now)
        room.video = video
        room.uploading = False
        self._playback.reset_for_new_video(room, now)

        event_type = "video_changed" if old is not None else "video_ready"
        return RoomEvent(
            room.room_id,
            {
                "type": event_type,
                "video": {
                    "video_id": video.video_id,
                    "original_name": video.original_name,
                    "size": video.size,
                },
            },
        )

    def fail_upload(self, room_id: str) -> None:
        """上传失败/中止:释放互斥锁(幂等,房间可能已销毁)。"""
        room = self._rooms.get(room_id)
        if room is not None:
            room.uploading = False
