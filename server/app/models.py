"""内存领域模型(design.md 4.2)。

仅定义数据结构,不含业务逻辑;房间在线状态与播放权威状态只存内存
(需求 4.5),SQLite 元数据见 db.py。

本模块为最底层,除标准库外零依赖(WebSocket 类型仅做静态注解)。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:  # 避免运行时依赖 starlette
    from starlette.websockets import WebSocket

# 播放状态字面量(需求 3.4.1)
PlaybackPhase = Literal["playing", "paused"]


@dataclass
class PlaybackState:
    """权威播放状态(需求 3.4.1,sync-protocol.md §1)。

    服务端是唯一权威来源;position 是 updated_at 时刻的位置,
    当前目标位置 = position + (now - updated_at) × rate(仅 playing)。
    """

    state: PlaybackPhase = "paused"
    position: float = 0.0
    rate: float = 1.0
    updated_at: float = 0.0
    version: int = 0


@dataclass
class VideoMeta:
    """视频元数据(需求 3.3):文件名 = video_id + ".mp4"。"""

    video_id: str
    original_name: str
    size: int


@dataclass
class Member:
    """在线成员(需求 3.1.3):有活跃 WS 连接的成员。

    joined_at 为首次加入时刻,掉线移除后经 known_users 沿用(决策 T15),
    用于房主移交时"加入时间最早"排序(需求 3.1.5)。
    """

    user_id: str
    nickname: str
    joined_at: float
    last_seen: float  # 任意 WS 消息均刷新(需求 3.6.2)
    conn: "WebSocket | None" = None  # 被踢/断开后置空


@dataclass
class KnownUser:
    """历史加入者记录:掉线移除后保留,供重连沿用昵称与 joined_at。

    joined_at 为首次加入时刻,用于房主移交"加入时间最早"排序
    (需求 3.1.5,决策 T15);credentials 由 Room 单独维护。
    """

    user_id: str
    nickname: str
    joined_at: float


@dataclass
class Room:
    """房间内存对象(design.md 4.2)。

    members 仅含在线成员;known_users 保留历史加入者(昵称与 joined_at),
    credentials 维护 session_token → user_id 映射,供 WS 重连鉴权
    (需求 3.7:凭 token 恢复身份);empty_since 非空表示处于空房过期
    倒计时(需求 3.1.7,决策 T14 可复活)。
    """

    room_id: str
    host_user_id: str
    created_at: float
    playback: PlaybackState = field(default_factory=PlaybackState)
    members: dict[str, Member] = field(default_factory=dict)
    known_users: dict[str, KnownUser] = field(default_factory=dict)
    credentials: dict[str, str] = field(default_factory=dict)  # token → user_id
    video: VideoMeta | None = None
    uploading: bool = False  # 上传互斥锁(design.md 4.7)
    empty_since: float | None = None
    password_hash: str | None = None  # 房间密码哈希(N3);None 表示无密码


@dataclass
class SessionCredentials:
    """HTTP 创建/加入房间签发的身份凭证(需求 3.2.1)。

    房主标记由服务端房间状态维护(host_user_id),不固化在凭证内。
    """

    room_id: str
    user_id: str
    session_token: str
    is_host: bool
